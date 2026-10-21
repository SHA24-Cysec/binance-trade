from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from hmac import compare_digest
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, redirect, render_template, request

from infrastructure.paths import PROJECT_ROOT

from config.config import (
    PUMP_CONFIG,
    CONFIG_LOAD_ERRORS,
    get_mode,
    get_mode_source,
    get_base_url,
    is_paper,
    backtest_enabled,
    get_state_file,
    get_log_file,
    get_control_file,
)
from infrastructure.network.file_descriptors import fd_status, raise_fd_limit
from infrastructure.network.public_ip import get_public_egress_ip_status
from infrastructure.storage import state as state_mod
from infrastructure.storage.trade_ledger import load_trade_records
from trading.equity import (
    SpotEquityReader,
    account_drawdown_view,
    collect_equity,
    equity_context,
    equity_envelope,
    equity_view,
    load_equity,
)
from trading import strategy_drawdown as strategy_dd
from strategy import indicators as strategy
from infrastructure.process.runtime_control import BotModeLock, BotAlreadyRunningError
from market import coin_icons
from market.fx_rate import IdrRateProvider
from infrastructure.process.runtime_control import BotControlError, BotProcessManager

try:
    from trading.clients.binance_client import BinanceSpotClient

    _HAS_CLIENT = True
except Exception:
    _HAS_CLIENT = False

logger = logging.getLogger(__name__)


class _PaperDashboardClient:

    def __init__(self) -> None:
        self._market = BinanceSpotClient(
            "",
            "",
            get_base_url(PUMP_CONFIG),
            allow_signed=False,
            rate_limit_state_file=PUMP_CONFIG.get("RATE_LIMIT_STATE_FILE"),
            rate_limit_limit=int(
                PUMP_CONFIG.get("RATE_LIMIT_WEIGHT_LIMIT", 6000) or 6000
            ),
            rate_limit_safety_margin=int(
                PUMP_CONFIG.get("RATE_LIMIT_SAFETY_MARGIN", 100) or 100
            ),
        )
        self._account_file = PUMP_CONFIG.get(
            "PAPER_ACCOUNT_STATE_FILE", "data/pump_paper_account_paper.json"
        )

    def get_price(self, symbol, max_retries: int = 3):
        return self._market.get_price(symbol, max_retries=max_retries)

    def get_ticker_24hr_all(self):
        return self._market.get_ticker_24hr_all()

    def get_ticker_24hr(self, symbol, max_retries: int = 2):
        return self._market.get_ticker_24hr(symbol, max_retries=max_retries)

    def get_klines(
        self, symbol, interval, limit=500, start_time_ms=None, end_time_ms=None
    ):
        return self._market.get_klines(
            symbol,
            interval,
            limit=limit,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
        )

    def get_depth(self, symbol, limit=100):
        return self._market.get_depth(symbol, limit=limit)

    def is_rate_limited(self):
        return self._market.is_rate_limited()

    def get_account(self):
        from trading.paper.paper_store import load_account_snapshot

        return load_account_snapshot(self._account_file)


from backtesting import backtest as bt
from backtesting import grid_search as gs
from backtesting import portfolio_backtest as pbt

app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"))

STATE_FILE = PUMP_CONFIG.get("STATE_FILE") or get_state_file()
coin_icons.configure(PROJECT_ROOT / "data" / "coin_icons.json")
LOG_FILE = PUMP_CONFIG.get("LOG_FILE") or get_log_file()
CONTROL_FILE = PUMP_CONFIG.get("CONTROL_FILE") or get_control_file()
QUOTE = PUMP_CONFIG.get("QUOTE_ASSET", "USDT")

_idr_rate_provider = IdrRateProvider(PUMP_CONFIG)


def get_idr_rate(force: bool = False) -> dict:
    try:
        return _idr_rate_provider.refresh() if force else _idr_rate_provider.get()
    except Exception as exc:  # noqa: BLE001 - jalur tampilan tidak boleh meledak
        logger.warning("Penyedia kurs IDR gagal total: %s", exc)
        return {
            "enabled": bool(PUMP_CONFIG.get("IDR_DISPLAY_ENABLED", True)),
            "rate": None,
            "source": "NONE",
            "symbol": str(PUMP_CONFIG.get("IDR_RATE_SYMBOL", "USDTIDR")),
            "updated_at": None,
            "updated_at_unix": None,
            "age_seconds": None,
            "stale": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


_MANUAL_CLOSE_COOLDOWN_SECONDS = 5.0
_last_manual_close_request = {"ts": 0.0}
_manual_close_lock = threading.Lock()

def _load_admin_token() -> "tuple[str, str]":
    raw = str(os.environ.get("DASHBOARD_ADMIN_TOKEN", "") or "").strip()
    if len(raw) >= 24:
        return raw, "environment"
    if raw:
        logger.warning(
            "DASHBOARD_ADMIN_TOKEN diabaikan karena kurang dari 24 karakter; "
            "dipakai token acak per proses."
        )
    return secrets.token_urlsafe(32), "process"


_ADMIN_TOKEN, _ADMIN_TOKEN_SOURCE = _load_admin_token()

_process_manager = BotProcessManager()
_runtime_lock = threading.RLock()
_cache_lock = threading.RLock()
_confirm_lock = threading.RLock()
_confirmations: dict[str, dict] = {}
_rate_lock = threading.RLock()
_last_dangerous_action: dict[str, float] = {}
_write_attempts: dict[str, list[float]] = {}

_control_operation_lock = threading.RLock()
_control_operation: dict | None = None
_CONTROL_ACTIVE_STATUSES = frozenset(("PENDING", "RUNNING"))

try:
    _DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
    if not 1 <= _DASHBOARD_PORT <= 65535:
        raise ValueError("port di luar rentang")
except ValueError:
    _DASHBOARD_PORT = 8080
_DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
app.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost"]


def _bind_is_loopback() -> bool:
    if _DASHBOARD_HOST.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(_DASHBOARD_HOST).is_loopback
    except ValueError:
        return False


def _expected_hosts() -> set[str]:
    expected = {f"127.0.0.1:{_DASHBOARD_PORT}", f"localhost:{_DASHBOARD_PORT}"}
    if not _bind_is_loopback() and _DASHBOARD_HOST not in ("0.0.0.0", "::"):
        expected.add(f"{_DASHBOARD_HOST.lower()}:{_DASHBOARD_PORT}")
    return expected


def _request_origin_is_local() -> bool:
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return False
    try:
        parsed = urlsplit(source)
    except ValueError:
        return False
    return (
        parsed.scheme in ("http", "https")
        and parsed.netloc.lower() == request.host.lower()
    )


def _remote_is_loopback() -> bool:
    remote = request.remote_addr
    if not remote:
        return bool(app.config.get("TESTING"))
    try:
        return ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return False


@app.before_request
def _security_gate():
    if not _bind_is_loopback():
        return (
            jsonify({"error": "Dashboard hanya boleh diakses melalui bind loopback."}),
            403,
        )

    host = (request.host or "").lower()
    expected = _expected_hosts()
    if app.config.get("TESTING"):
        expected.update({"localhost", "127.0.0.1"})
    if host not in expected:
        return jsonify({"error": "Host dashboard tidak diizinkan."}), 400

    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if not _remote_is_loopback():
            return (
                jsonify(
                    {"error": "Request kontrol harus berasal dari alamat loopback."}
                ),
                403,
            )
        if not _request_origin_is_local():
            return jsonify({"error": "Origin atau Referer tidak valid."}), 403
        now = time.monotonic()
        rate_key = request.remote_addr or "test-client"
        with _rate_lock:
            recent = [
                item for item in _write_attempts.get(rate_key, []) if now - item < 60.0
            ]
            if len(recent) >= 120:
                _write_attempts[rate_key] = recent
                return (
                    jsonify(
                        {"error": "Terlalu banyak request tulis. Coba lagi sebentar."}
                    ),
                    429,
                )
            recent.append(now)
            _write_attempts[rate_key] = recent
        supplied = request.headers.get("X-Admin-Token", "")
        if not compare_digest(supplied, _ADMIN_TOKEN):
            return jsonify({"error": "Token admin tidak valid atau tidak ada."}), 403
    return None


@app.after_request
def _security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    return response


def _cooldown(action: str, seconds: float) -> tuple[bool, float]:
    now = time.monotonic()
    with _rate_lock:
        previous = _last_dangerous_action.get(action, 0.0)
        left = seconds - (now - previous)
        if left > 0:
            return False, left
        _last_dangerous_action[action] = now
    return True, 0.0


def _make_confirmation(kind: str, payload: dict, ttl: float = 180.0) -> str:
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _confirm_lock:
        stale = [
            key
            for key, value in _confirmations.items()
            if value.get("expires", 0) < now
        ]
        for key in stale:
            _confirmations.pop(key, None)
        _confirmations[token] = {
            "kind": kind,
            "payload": deepcopy(payload),
            "expires": now + ttl,
        }
    return token


def _take_confirmation(token: str, kind: str) -> dict | None:
    with _confirm_lock:
        item = _confirmations.pop(str(token), None)
    if not item or item.get("kind") != kind or item.get("expires", 0) < time.time():
        return None
    return item.get("payload") or {}


def _control_operation_snapshot() -> dict | None:
    with _control_operation_lock:
        return deepcopy(_control_operation) if _control_operation else None


def _active_control_operation() -> dict | None:
    operation = _control_operation_snapshot()
    if operation and operation.get("status") in _CONTROL_ACTIVE_STATUSES:
        return operation
    return None


def _update_control_operation(operation_id: str, **updates) -> bool:
    with _control_operation_lock:
        if not _control_operation or _control_operation.get("id") != operation_id:
            return False
        _control_operation.update(updates)
        _control_operation["updated_at"] = time.time()
        return True


def _run_control_operation(
    operation_id: str,
    action: str,
    position_policy: str,
    allow_missing_live_state: bool = False,
) -> None:
    _update_control_operation(
        operation_id,
        status="RUNNING",
        started_at=time.time(),
    )
    try:
        if action == "START":
            result = _process_manager.start(
                allow_missing_live_state=allow_missing_live_state
            )
        else:
            result = _process_manager.stop(position_policy=position_policy)
    except Exception as exc:
        logger.exception("Aksi proses %s gagal: %s", action, exc)
        _update_control_operation(
            operation_id,
            status="FAILED",
            finished_at=time.time(),
            error=str(exc)[:500] or type(exc).__name__,
        )
        return
    _update_control_operation(
        operation_id,
        status="SUCCEEDED",
        finished_at=time.time(),
        process=result,
        error=None,
    )


_client = None
_price_cache: dict = {}
_balance_cache: dict = {"data": None, "ts": 0}
PRICE_TTL = 5.0
VOLUME_TTL = 30.0
_volume_cache: dict = {}
BALANCE_TTL = 30.0
_equity_lock = threading.RLock()
_equity_cache = {"context": None, "envelope": None, "ts": 0.0}


def get_client():
    global _client
    if not _HAS_CLIENT:
        return None
    with _runtime_lock:
        if _client is None:
            try:
                if is_paper(PUMP_CONFIG):
                    _client = _PaperDashboardClient()
                else:
                    _client = BinanceSpotClient(
                        PUMP_CONFIG.get("API_KEY", ""),
                        PUMP_CONFIG.get("API_SECRET", ""),
                        get_base_url(PUMP_CONFIG),
                        rate_limit_state_file=PUMP_CONFIG.get("RATE_LIMIT_STATE_FILE"),
                        rate_limit_limit=int(
                            PUMP_CONFIG.get("RATE_LIMIT_WEIGHT_LIMIT", 6000) or 6000
                        ),
                        rate_limit_safety_margin=int(
                            PUMP_CONFIG.get("RATE_LIMIT_SAFETY_MARGIN", 100) or 100
                        ),
                    )
            except Exception:
                _client = None
        return _client


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def get_volume_24h_quote(symbol: str):
    if not symbol:
        return None
    now = time.time()
    with _cache_lock:
        cached = _volume_cache.get(symbol)
        if cached and now - cached[1] < VOLUME_TTL:
            return cached[0]
    client = get_client()
    if client is None or not hasattr(client, "get_ticker_24hr"):
        return None
    try:
        data = client.get_ticker_24hr(symbol)
        value = float(data.get("quoteVolume", 0) or 0)
    except Exception:
        return cached[0] if cached else None
    with _cache_lock:
        _volume_cache[symbol] = (value, now)
    return value


def get_live_price(symbol: str):
    if not symbol:
        return None
    now = time.time()
    with _cache_lock:
        cached = _price_cache.get(symbol)
        if cached and now - cached[1] < PRICE_TTL:
            return cached[0]
    client = get_client()
    if client is None:
        return None
    try:
        price = client.get_price(symbol)
        with _cache_lock:
            _price_cache[symbol] = (price, now)
        return price
    except Exception:
        return cached[0] if cached else None


def get_account_equity(process_status: dict) -> dict:
    active = process_status.get("status") in ("STARTING", "RUNNING", "STOPPING")
    with _equity_lock:
        if active:
            _equity_cache.update(context=None, envelope=None, ts=0.0)
            return equity_view(load_equity(PUMP_CONFIG), PUMP_CONFIG, process_status)
        context = equity_context(PUMP_CONFIG)
        now = time.monotonic()
        if _equity_cache["context"] != context:
            _equity_cache.update(context=context, envelope=None, ts=0.0)
        envelope = _equity_cache["envelope"]
        if envelope is None or now - _equity_cache["ts"] >= BALANCE_TTL:
            client = get_client()
            reader = SpotEquityReader(client) if client is not None and not is_paper(PUMP_CONFIG) else client
            latest = collect_equity(reader, PUMP_CONFIG, {}, source="DASHBOARD")
            previous = envelope or load_equity(PUMP_CONFIG)
            envelope = equity_envelope(latest, previous)
            _equity_cache.update(envelope=envelope, ts=time.monotonic())
        return equity_view(envelope, PUMP_CONFIG)


def get_live_balance():
    now = time.time()
    with _cache_lock:
        cached_data = _balance_cache["data"]
        cached_ts = _balance_cache["ts"]
    if cached_data is not None and now - cached_ts < BALANCE_TTL:
        return cached_data
    client = get_client()
    if client is None or (not is_paper(PUMP_CONFIG) and not PUMP_CONFIG.get("API_KEY")):
        return None
    try:
        account = client.get_account()
        balances = {}
        for b in account.get("balances", []):
            free = float(b.get("free", 0) or 0)
            locked = float(b.get("locked", 0) or 0)
            if free > 0 or locked > 0:
                balances[b["asset"]] = {"free": free, "locked": locked}
        with _cache_lock:
            _balance_cache["data"] = balances
            _balance_cache["ts"] = now
        return balances
    except Exception:
        with _cache_lock:
            return _balance_cache["data"]


_RE_BUY = re.compile(
    r"^(?P<ts>[\d\-]+ [\d:]+).*?BUY FILLED (?P<sym>\w+): qty=(?P<qty>[\d.]+)"
    r"(?: managed=(?P<managed>[\d.]+))? @ avg (?P<price>[\d.]+)"
    r".*?24h=(?P<pct>[+\-\d.]+)%"
)
_RE_SELL = re.compile(
    r"^(?P<ts>[\d\-]+ [\d:]+).*?SELL FILLED (?P<sym>\w+) \((?P<reason>[^)]+)\): qty=(?P<qty>[\d.]+) @ avg "
    r"(?P<price>[\d.]+) \| entry=(?P<entry>[\d.]+) \| estimasi PnL=(?P<pnl>[+\-\d.]+)"
)


def parse_log(max_lines: int = 4000):
    if not os.path.exists(LOG_FILE):
        try:
            return (
                load_trade_records(PUMP_CONFIG.get("TRADE_LEDGER_FILE"), limit=max_lines),
                [],
                {},
            )
        except Exception:
            return [], [], {}
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-max_lines:]
    except OSError:
        try:
            return (
                load_trade_records(PUMP_CONFIG.get("TRADE_LEDGER_FILE"), limit=max_lines),
                [],
                {},
            )
        except Exception:
            return [], [], {}

    trades = []
    open_pos = None
    level_count = {"INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
    events = []

    for ln in lines:
        ln = ln.rstrip("\n")
        for lvl in level_count:
            if f"| {lvl}" in ln or f"|{lvl}" in ln:
                level_count[lvl] += 1
                break

        m = _RE_BUY.search(ln)
        if m:
            d = m.groupdict()
            open_pos = {
                "symbol": d["sym"],
                "buy_time": d["ts"],
                "buy_price": float(d["price"]),
                "qty": float(d["qty"]),
                "pct24h": float(d.get("pct") or 0),
                "paper": is_paper(PUMP_CONFIG),
            }
            continue

        m = _RE_SELL.search(ln)
        if m:
            d = m.groupdict()
            t = dict(open_pos or {})
            t.update(
                {
                    "symbol": d["sym"],
                    "sell_time": d["ts"],
                    "sell_price": float(d["price"]),
                    "entry": float(d["entry"]),
                    "pnl": float(d["pnl"]),
                    "reason": d["reason"],
                    "paper": is_paper(PUMP_CONFIG),
                }
            )
            if "buy_price" not in t:
                t["buy_price"] = float(d["entry"])
            t.setdefault("qty", float(d["qty"]))
            t["pnl_pct"] = (
                (t["sell_price"] / t["buy_price"] - 1.0) * 100.0
                if t.get("buy_price")
                else 0.0
            )
            trades.append(t)
            open_pos = None
            continue

    for ln in lines[-120:]:
        ln = ln.rstrip("\n")
        if not ln.strip():
            continue
        lvl = "INFO"
        for nama_level in ("CRITICAL", "ERROR", "WARNING", "INFO"):
            if f"| {nama_level}" in ln:
                lvl = nama_level
                break
        events.append({"raw": ln, "level": lvl})

    ledger_path = PUMP_CONFIG.get("TRADE_LEDGER_FILE")
    try:
        ledger_trades = load_trade_records(ledger_path, limit=max_lines) if ledger_path else []
    except Exception as exc:
        logger.warning("Ledger trade dashboard gagal dibaca: %s", exc)
        ledger_trades = []
    if ledger_trades:
        trades = ledger_trades
    else:
        trades.reverse()
        for trade in trades:
            trade["accounting_status"] = "LEGACY_UNVERIFIED"
            trade["net_pnl_quote"] = None
            trade["estimated_pnl_quote"] = trade.get("pnl")
            trade["accounting_note"] = "Riwayat log lama, PnL tidak terverifikasi dari fill/komisi."
    events.reverse()
    return trades, events, level_count


def build_status():

    state = load_state()
    process_status = _process_manager.status(get_mode(PUMP_CONFIG))
    symbol = state.get("current_symbol")
    qty = float(state.get("qty", 0) or 0)
    entry = float(state.get("entry_price", 0) or 0)

    live_price = get_live_price(symbol) if symbol else None
    has_position = bool(symbol and qty > 0)

    pnl_pct = None
    pnl_usdt = None
    position_value = None
    if has_position and entry > 0 and live_price:
        pnl_pct = (live_price / entry - 1.0) * 100.0
        pnl_usdt = (live_price - entry) * qty
        position_value = live_price * qty

    account_equity = get_account_equity(process_status)

    hold_minutes = None
    if has_position and state.get("entry_time"):
        hold_minutes = (int(time.time() * 1000) - int(state["entry_time"])) / 60000.0

    use_tp = bool(PUMP_CONFIG.get("USE_TP"))
    use_stop_loss = bool(PUMP_CONFIG.get("USE_STOP_LOSS"))
    use_breakeven = bool(PUMP_CONFIG.get("USE_BREAKEVEN"))
    use_trailing = bool(PUMP_CONFIG.get("USE_TRAILING"))
    trade_accounting = state.get("trade_accounting")
    trade_accounting = trade_accounting if isinstance(trade_accounting, dict) else {}
    pending_accounting = state.get("accounting_pending_trades")
    pending_accounting = pending_accounting if isinstance(pending_accounting, list) else []

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "mode": get_mode(PUMP_CONFIG),
        "paper": is_paper(PUMP_CONFIG),
        "backtest_enabled": backtest_enabled(PUMP_CONFIG),
        "live_connected": live_price is not None or account_equity["equity_status"] == "VALID",
        "bot_alive": process_status["status"] in ("STARTING", "RUNNING", "STOPPING"),
        "process": process_status,
        "config_errors": list(CONFIG_LOAD_ERRORS),
        "read_only": not _bind_is_loopback(),
        "has_api_key": bool(PUMP_CONFIG.get("API_KEY")),
        "quote": QUOTE,
        "fx": get_idr_rate(),
        "position": {
            "has_position": has_position,
            "symbol": symbol,
            "qty": qty,
            "entry_price": entry,
            "live_price": live_price,
            "pnl_pct": pnl_pct,
            "pnl_usdt": pnl_usdt,
            "position_value": position_value,
            "hold_minutes": hold_minutes,
            "volume_24h_quote": get_volume_24h_quote(symbol) if has_position else None,
            "be_active": bool(state.get("be_active")),
            "be_stop_price": float(state.get("be_stop_price", 0) or 0),
            "trailing_active": bool(state.get("trailing_active")),
            "trailing_stop_price": float(state.get("trailing_stop_price", 0) or 0),
            "native_protection": {
                "active": isinstance(state.get("native_oco"), dict)
                or isinstance(state.get("native_stop"), dict),
                "stop_price": _native_protection_level(state),
                "amend_pending": isinstance(state.get("native_amend"), dict),
                "exit_blocked": bool(state.get("_native_stop_exit_blocked")),
            },
        },
        "drawdown_strategy": strategy_dd.strategy_view(PUMP_CONFIG),
        "drawdown_account": account_drawdown_view(state, account_equity, process_status, PUMP_CONFIG),
        "accounting": {
            "blocked": bool(state.get("accounting_blocked")),
            "block_reason": str(state.get("accounting_block_reason") or ""),
            "active_status": str(trade_accounting.get("status") or ""),
            "pending_count": len(pending_accounting),
            "unpriced_fee_count": len(trade_accounting.get("unpriced_fees") or []),
        },
        "account": {
            **account_equity,
            "peak_equity": state.get("peak_equity"),
            "peak_equity_all_time": state.get("peak_equity_all_time"),
            "max_drawdown_pct": float(state.get("max_dd_pct") or 0.0),
            "dd_reset_count": int(state.get("dd_reset_count") or 0),
            "max_drawdown_limit_pct": float(
                PUMP_CONFIG.get("MAX_DRAWDOWN_PERCENT") or 0.0
            ),
        },
        "flags": {
            "dd_stopped": bool(state.get("dd_stopped")),
            "state_integrity_error": str(state.get("state_integrity_error") or ""),
        },
        "config": {
            "sl_pct": (
                (state.get("sl_pct") or PUMP_CONFIG.get("SL_PCT"))
                if use_stop_loss
                else None
            ),
            "tp_pct": (
                (state.get("tp_pct") or PUMP_CONFIG.get("TP_PCT"))
                if use_tp
                else None
            ),
            "exit_source": state.get("exit_source") or "FIXED",
            "be_trigger_pct": (
                (state.get("be_trigger_pct") or PUMP_CONFIG.get("BE_TRIGGER_PCT"))
                if use_breakeven
                else None
            ),
            "trail_start_pct": (
                (state.get("trail_start_pct") or PUMP_CONFIG.get("TRAILING_START_PCT"))
                if use_trailing
                else None
            ),
            "trail_step_pct": (
                (state.get("trail_step_pct") or PUMP_CONFIG.get("TRAILING_STEP_PCT"))
                if use_trailing
                else None
            ),
            "trailing_start_pct": (
                (state.get("trail_start_pct") or PUMP_CONFIG.get("TRAILING_START_PCT"))
                if use_trailing
                else None
            ),
            "use_tp": use_tp,
            "use_stop_loss": use_stop_loss,
            "use_breakeven": use_breakeven,
            "use_trailing": use_trailing,
            "use_atr_exit": bool(PUMP_CONFIG.get("USE_ATR_EXIT")),
            "atr_mult_sl": PUMP_CONFIG.get("ATR_MULT_SL"),
            "atr_mult_tp": PUMP_CONFIG.get("ATR_MULT_TP"),
        },
    }


def build_trade_summary(trades):
    all_closed = [t for t in trades if t.get("sell_time")]
    verified = []
    for trade in all_closed:
        if str(trade.get("accounting_status") or "LEGACY_UNVERIFIED").upper() != "VERIFIED":
            continue
        try:
            pnl = float(trade.get("net_pnl_quote", trade.get("pnl")))
            if not math.isfinite(pnl):
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        verified.append((trade, pnl))
    wins = [(trade, pnl) for trade, pnl in verified if pnl > 0]
    losses = [(trade, pnl) for trade, pnl in verified if pnl <= 0]
    total = len(verified)
    total_pnl = sum(pnl for _, pnl in verified)
    win_rate = (len(wins) / total * 100.0) if total else 0.0
    avg_win = (sum(pnl for _, pnl in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(pnl for _, pnl in losses) / len(losses)) if losses else 0.0
    verified_pcts = []
    for trade, _pnl in verified:
        try:
            pct = float(trade.get("pnl_pct"))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(pct):
            verified_pcts.append(pct)
    return {
        "total_trades": total,
        "unverified_trades": len(all_closed) - total,
        "total_closed_trades": len(all_closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "best_pct": max(verified_pcts, default=0.0),
        "worst_pct": min(verified_pcts, default=0.0),
    }


def _reject_if_backtest_disabled():
    if backtest_enabled(PUMP_CONFIG):
        return None
    return (
        jsonify(
            {
                "error": "Fitur backtest dinonaktifkan saat mode LIVE. "
                "Untuk mengaktifkannya, set SHOW_BACKTEST_IN_LIVE=True di config.py "
                "lalu jalankan ulang dashboard.",
                "backtest_disabled": True,
            }
        ),
        403,
    )


BT_PARAM_KEYS = (
    "USE_ATR_EXIT",
    "ATR_PERIOD",
    "ATR_MULT_SL",
    "ATR_MULT_TP",
    "ATR_MULT_BE_TRIGGER",
    "ATR_MULT_BE_LOCK",
    "ATR_MULT_TRAIL_START",
    "ATR_MULT_TRAIL",
    "MAX_HOLD_HOURS",
    "SL_PCT",
    "TP_PCT",
    "BE_TRIGGER_PCT",
    "BE_LOCK_PCT",
    "TRAILING_START_PCT",
    "TRAILING_STEP_PCT",
    "CONFIRM_LOOKBACK_BARS",
    "ROLLING_VOLUME_FILTER_ENABLED",
    "ROLLING_VOLUME_LOOKBACK_BARS",
    "ROLLING_VOLUME_SURGE_MULT",
    "ROLLING_VOLUME_CONFIRMATION_BARS",
    "DEMAND_ZONE_FILTER_ENABLED",
    "DEMAND_LOOKBACK_BARS",
    "DEMAND_ZONE_BUFFER_PCT",
    "DEMAND_MAX_DISTANCE_PCT",
    "DEMAND_MIN_CLOSE_POSITION",
    "DEMAND_REQUIRE_INSIDE_ZONE",
    "DIRECTION_FILTER_ENABLED",
    "DIRECTION_INTERVAL",
    "DIRECTION_EMA_FAST",
    "DIRECTION_EMA_SLOW",
    "DIRECTION_ADX_PERIOD",
    "DIRECTION_ADX_MIN",
    "DIRECTION_LOOKBACK_BARS",
    "DIRECTION_ZONE_FILTER_ENABLED",
    "DIRECTION_ZONE_LOOKBACK_BARS",
    "DIRECTION_ZONE_BUFFER_PCT",
    "DIRECTION_ZONE_MAX_DISTANCE_PCT",
    "DIRECTION_ZONE_MIN_CLOSE_POSITION",
    "DIRECTION_ZONE_REQUIRE_INSIDE_ZONE",
    "ZONE_REQUIRE_INSIDE_ZONE",
    "ZONE_FILTER_ENABLED",
    "ZONE_INTERVAL",
    "ZONE_LOOKBACK_BARS",
    "ZONE_BUFFER_PCT",
    "ZONE_MAX_DISTANCE_PCT",
    "ZONE_MIN_CLOSE_POSITION",
    "ZONE_EMA_FAST",
    "ZONE_EMA_SLOW",
    "ZONE_ADX_PERIOD",
    "ZONE_ADX_MIN",
    "ZONE_TREND_LOOKBACK_BARS",
    "TIMING_FILTER_ENABLED",
    "TIMING_EMA_FAST",
    "TIMING_EMA_SLOW",
    "TIMING_ADX_PERIOD",
    "TIMING_ADX_MIN",
    "TIMING_LOOKBACK_BARS",
    "BREAKOUT_RETEST_FILTER_ENABLED",
    "BREAKOUT_LOOKBACK_BARS",
    "BREAKOUT_PIVOT_WING_BARS",
    "BREAKOUT_ATR_PERIOD",
    "BREAKOUT_RETEST_TOLERANCE_ATR",
    "BREAKOUT_RETEST_MAX_BARS",
    "BREAKOUT_RETEST_MAX_TOUCHES",
    "BREAKOUT_RETEST_REQUIRE_BULLISH",
)

GRID_PARAM_KEYS = tuple(
    key
    for key in BT_PARAM_KEYS
    if key != "USE_ATR_EXIT" and key in gs.KUNCI_GRID_PORTOFOLIO
)

_bt_jobs: dict = {}
_bt_jobs_lock = threading.Lock()
BT_JOB_TTL_SECONDS = 3600


def _bt_cleanup_old_jobs():
    now = time.time()
    with _bt_jobs_lock:
        stale = [
            jid
            for jid, j in _bt_jobs.items()
            if now - j.get("created_at", now) > BT_JOB_TTL_SECONDS
        ]
        for jid in stale:
            _bt_jobs.pop(jid, None)


def _bt_simulation_interval(cfg: dict) -> str:
    return str(cfg.get("CONFIRM_INTERVAL", "5m") or "5m")


def _bt_native_htf_intervals(cfg: dict) -> list[str]:
    """Interval timeframe tinggi yang diambil langsung dari bursa."""
    out: list[str] = []
    if bool(cfg.get("DIRECTION_FILTER_ENABLED", False)) or bool(
        cfg.get("DIRECTION_ZONE_FILTER_ENABLED", False)
    ):
        out.append(str(cfg.get("DIRECTION_INTERVAL", "1d") or "1d"))
    if bool(cfg.get("ZONE_FILTER_ENABLED", False)):
        out.append(str(cfg.get("ZONE_INTERVAL", "1h") or "1h"))
    return out


def _bt_native_prebuilt(prep: dict, cfg: dict):
    """Menyusun prebuilt dari candle timeframe tinggi native.

    Kalau permintaan native tidak menghasilkan satu candle pun, backtest
    dihentikan dengan pesan jelas. Tanpa ini seluruh kandidat ditolak
    lapisan arah/zona dan hasilnya 0 trade yang terlihat seperti "strategi
    tidak pernah sinyal".
    """
    arah = prep.get("native_arah")
    zona = prep.get("native_zona")
    if arah is None or zona is None:
        return None
    missing = []
    if (
        bool(cfg.get("DIRECTION_FILTER_ENABLED", False))
        or bool(cfg.get("DIRECTION_ZONE_FILTER_ENABLED", False))
    ) and not arah:
        missing.append(str(cfg.get("DIRECTION_INTERVAL", "1d") or "1d"))
    if bool(cfg.get("ZONE_FILTER_ENABLED", False)) and not zona:
        missing.append(str(cfg.get("ZONE_INTERVAL", "1h") or "1h"))
    if missing:
        raise bt.BacktestError(
            "Candle timeframe tinggi native tidak berhasil diambil untuk "
            + ", ".join(missing)
            + ". Simulasi dihentikan supaya tidak menampilkan 0 trade yang "
            "menyesatkan. Periksa koneksi ke Binance lalu jalankan ulang."
        )
    return (None, None, arah, zona, None)


def _bt_simulation_warmup_bars(cfg: dict) -> int:
    """Jumlah candle simulasi yang harus diunduh sebelum rentang uji.

    Bila layer arah atau layer zona aktif, candle Daily dan H1 diambil
    langsung dari bursa, jadi pemanasan agregasi yang panjang tidak
    diperlukan pada rentang simulasi.
    """
    interval = _bt_simulation_interval(cfg)
    dasar = max(
        bt.bars_per_day(interval) + 30,
        strategy.required_lookback_bars(cfg),
    )
    if _bt_native_htf_intervals(cfg):
        return dasar
    try:
        return dasar + pbt.parity.gate_warmup_bars(cfg, interval)
    except ValueError:
        return dasar


def _bt_estimate_requests(days: int, max_symbols: int, cfg: dict) -> int:
    interval = _bt_simulation_interval(cfg)
    bars_per_hari = 1440 // bt.INTERVAL_MINUTES.get(interval, 5)
    warmup_bars = _bt_simulation_warmup_bars(cfg)
    total_bars = (days + 1) * bars_per_hari + warmup_bars
    halaman = max(1, -(-total_bars // 1000))
    per_interval = max_symbols * halaman
    htf = max_symbols * len(_bt_native_htf_intervals(cfg))
    return per_interval + htf


def _bt_direction_warmup_note(cfg: dict, interval: str) -> str:
    arah_active = bool(cfg.get("DIRECTION_FILTER_ENABLED", False))
    daily_zone_active = bool(cfg.get("DIRECTION_ZONE_FILTER_ENABLED", False))
    if not arah_active and not daily_zone_active:
        return "Gerbang Daily nonaktif, jadi candle harian tidak diambil."
    try:
        hari = pbt.parity.direction_warmup_ms(cfg, interval) / float(bt.MS_PER_DAY)
    except ValueError:
        return (
            "Gerbang Daily aktif, tetapi intervalnya tidak sepadan dengan interval "
            "simulasi sehingga backtest ini akan menolak berjalan."
        )
    return (
        f"Gerbang Daily memakai candle {cfg.get('DIRECTION_INTERVAL', '1d')} asli dari "
        "bursa, sama seperti bot live, sehingga tidak menambah warmup pada rentang "
        f"simulasi {interval}. Kalau memakai jalur agregasi, pemanasan gerbang ini "
        f"butuh sekitar {hari:.1f} hari pertama."
    )


def _bt_zone_warmup_note(cfg: dict, interval: str) -> str:
    if not bool(cfg.get("ZONE_FILTER_ENABLED", False)):
        return "Layer zona sedang NONAKTIF, jadi candle zona tidak diambil."
    try:
        jam = pbt.parity.zone_warmup_ms(cfg, interval) / 3_600_000
    except ValueError as exc:
        return f"Layer zona tidak bisa disimulasikan: {exc}"
    return (
        f"Layer zona memakai candle {cfg.get('ZONE_INTERVAL', '1h')} asli dari bursa, "
        "sama persis seperti bot live. Warmup lapisan ini sekitar "
        f"{jam:.2f} jam pada jalur agregasi dan otomatis ikut diunduh bila jalur "
        "agregasi dipakai. Lihat penghitung sinyal yang disaring pada hasil."
    )


def _bt_timing_note(cfg: dict, interval: str) -> str:
    timing_active = bool(cfg.get("TIMING_FILTER_ENABLED", False))
    breakout_active = bool(cfg.get("BREAKOUT_RETEST_FILTER_ENABLED", False))
    rolling_active = bool(cfg.get("ROLLING_VOLUME_FILTER_ENABLED", False))
    demand_active = bool(cfg.get("DEMAND_ZONE_FILTER_ENABLED", False))
    if not any((timing_active, breakout_active, rolling_active, demand_active)):
        return "Seluruh gerbang tambahan M5 nonaktif."
    try:
        candle = strategy.required_lookback_bars(cfg)
        jam = candle * strategy.interval_to_ms(interval) / 3_600_000
    except (ValueError, KeyError) as exc:
        return f"Gerbang M5 tidak bisa disimulasikan: {exc}"
    aktif = []
    if timing_active:
        aktif.append("tren M5")
    if breakout_active:
        aktif.append("breakout-retest")
    if rolling_active:
        aktif.append("volume rolling")
    if demand_active:
        aktif.append("demand M5")
    return (
        f"{' lalu '.join(aktif)} memakai candle {cfg.get('CONFIRM_INTERVAL', '5m')} "
        f"yang sama, tanpa unduhan tambahan. Gerbang M5 membutuhkan {candle} candle "
        f"tertutup, sekitar {jam:.2f} jam."
    )


def _bt_prepare_universe(
    job_id: str, cfg: dict, days: int, max_symbols: int, set_progress, cancelled
) -> dict:
    interval = _bt_simulation_interval(cfg)
    bt.bars_per_day(interval)
    bar_ms = bt.INTERVAL_MINUTES[interval] * 60_000
    warmup_ms = bt.MS_PER_DAY + 30 * bar_ms
    if not (bool(cfg.get("DIRECTION_FILTER_ENABLED", False)) or bool(
        cfg.get("ZONE_FILTER_ENABLED", False)
    )):
        try:
            butuh_gerbang_ms = pbt.parity.gate_warmup_ms(cfg, interval)
        except ValueError as exc:
            raise bt.BacktestError(str(exc)) from exc
        if butuh_gerbang_ms > warmup_ms:
            logger.info(
                "Warmup backtest dinaikkan dari %.1f jam menjadi %.1f jam karena "
                "layer arah %s dan layer zona %s butuh paling sedikit %d candle %s "
                "sebagai pemanasan.",
                warmup_ms / 3_600_000.0,
                butuh_gerbang_ms / 3_600_000.0,
                cfg.get("DIRECTION_INTERVAL", "1d"),
                cfg.get("ZONE_INTERVAL", "1h"),
                pbt.parity.gate_warmup_bars(cfg, interval),
                interval,
            )
            warmup_ms = butuh_gerbang_ms

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * bt.MS_PER_DAY
    fetch_start_ms = start_ms - warmup_ms

    def klien_pembuat(cfg: dict) -> BinanceSpotClient:
        return BinanceSpotClient(
            "",
            "",
            cfg["LIVE_BASE_URL"],
            allow_signed=False,
            rate_limit_state_file=cfg.get("RATE_LIMIT_STATE_FILE"),
            rate_limit_limit=int(cfg.get("RATE_LIMIT_WEIGHT_LIMIT", 6000) or 6000),
            rate_limit_safety_margin=int(
                cfg.get("RATE_LIMIT_SAFETY_MARGIN", 100) or 100
            ),
        )


    klien = None
    if not _HAS_CLIENT:
        raise bt.BacktestError(
            "Klien Binance tidak tersedia (modul 'requests' tidak termuat). "
            "Backtest butuh akses ke data historis publik Binance."
        )
    klien = klien_pembuat(cfg)

    set_progress(0.01, "mengambil daftar pasar...")
    try:
        tickers = klien.get_ticker_24hr_all()
    except Exception as exc:
        raise bt.BacktestError(
            f"Gagal mengambil daftar pasar dari Binance: {exc}"
        ) from exc

    tradable_now = None
    try:
        exchange_info = klien.get_exchange_info()
        tradable_now = {
            s.get("symbol")
            for s in exchange_info.get("symbols", [])
            if s.get("symbol")
            and s.get("status") == "TRADING"
            and s.get("isSpotTradingAllowed", True)
        }
    except Exception as exc:
        logger.warning(
            "Metadata status pair tidak tersedia untuk portfolio backtest: %s", exc
        )
    if tradable_now is not None:
        cfg["_historical_tradable_symbols"] = tradable_now
        cfg["_tradable_status_is_current_snapshot"] = True

    universe = pbt.select_universe(
        tickers, cfg, max_symbols=max_symbols, tradable_symbols=tradable_now
    )
    if not universe:
        raise bt.BacktestError(
            "Tidak ada simbol yang lolos saringan pasar. Periksa QUOTE_ASSET "
            "dan MIN_QUOTE_VOLUME_USDT_24H di config.py."
        )

    with _bt_jobs_lock:
        if job_id in _bt_jobs:
            _bt_jobs[job_id]["universe_size"] = len(universe)

    set_progress(0.03, f"mengunduh data {len(universe)} simbol...")

    def dl_progress(frac, sym):
        set_progress(
            0.03 + frac * 0.77,
            f"mengunduh {sym} ({int(frac * len(universe))}/{len(universe)})",
        )

    store = None
    kline_cache = None
    try:
        store = pbt.new_backtest_store(cfg)
        kline_cache = pbt.open_kline_cache(cfg)
        symbols_with_data, failed = pbt.fetch_universe_klines(
            klien,
            universe,
            interval,
            fetch_start_ms,
            end_ms,
            store,
            progress_cb=dl_progress,
            cancel_cb=cancelled,
            cache=kline_cache,
        )
        if not symbols_with_data:
            raise bt.BacktestError(
                "Tidak ada satu pun simbol yang berhasil diunduh datanya. "
                "Periksa koneksi ke Binance."
            )

        arah_of = {}
        zona_of = {}
        native_htf_aktif = (
            bool(cfg.get("DIRECTION_FILTER_ENABLED", False))
            or bool(cfg.get("DIRECTION_ZONE_FILTER_ENABLED", False))
            or bool(cfg.get("ZONE_FILTER_ENABLED", False))
        )
        if native_htf_aktif:
            set_progress(
                0.81,
                "mengambil candle timeframe tinggi native (Daily/H1)...",
            )

            def htf_progress(frac, sym):
                set_progress(
                    0.81 + frac * 0.01,
                    f"mengambil candle {sym} ({int(frac * len(symbols_with_data))}"
                    f"/{len(symbols_with_data)})",
                )

            arah_of, zona_of = pbt.fetch_native_lookups(
                klien,
                list(symbols_with_data),
                cfg,
                start_ms,
                end_ms,
                cache=kline_cache,
                max_workers=4,
                progress_cb=htf_progress,
            )
            if not arah_of and not zona_of:
                logger.warning(
                    "Tidak ada candle timeframe tinggi native yang berhasil "
                    "diambil. Layer arah dan layer zona akan menolak semua "
                    "kandidat karena riwayat tidak cukup."
                )

        btc_klines = None
        btc_error = ""
        if cfg.get("BTC_FILTER_ENABLED", False):
            btc_symbol = "BTC" + str(cfg.get("QUOTE_ASSET", "USDT"))
            set_progress(0.82, f"mengunduh {btc_symbol} untuk filter BTC...")
            try:
                btc_klines = pbt._klines_untuk_simbol(
                    klien, btc_symbol, interval, fetch_start_ms, end_ms, kline_cache
                )
                if not btc_klines:
                    btc_klines = None
                    btc_error = f"candle {btc_symbol} kosong"
            except Exception as exc:
                if cancelled():
                    raise
                btc_klines = None
                btc_error = str(exc)[:200]
    except Exception:
        if store is not None:
            store.cleanup()
        if kline_cache is not None:
            kline_cache.close()
        if klien is not None:
            try:
                klien.close()
            except Exception:
                logger.debug("Gagal menutup klien backtest", exc_info=True)
        raise

    return {
        "client": klien,
        "store": store,
        "kline_cache": kline_cache,
        "universe": universe,
        "symbols_with_data": symbols_with_data,
        "failed": failed,
        "interval": interval,
        "warmup_ms": warmup_ms,
        "end_ms": end_ms,
        "btc_klines": btc_klines,
        "btc_error": btc_error,
        "native_arah": arah_of,
        "native_zona": zona_of,
    }


def _native_protection_level(state: dict) -> float:
    oco = state.get("native_oco")
    if isinstance(oco, dict):
        below = oco.get("below")
        if isinstance(below, dict):
            try:
                return float(below.get("stop_price") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    stop = state.get("native_stop")
    if isinstance(stop, dict):
        try:
            return float(stop.get("stop_price") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _json_safe(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _bt_days_guard(cfg: dict, days: int) -> None:
    interval = _bt_simulation_interval(cfg)
    minimal, catatan = _bt_min_days_note(cfg, interval)
    if int(days) < minimal:
        raise bt.BacktestError(
            f"Jumlah hari minimal {minimal} untuk setelan ini, sedangkan yang diminta "
            f"{int(days)} hari. {catatan}".strip()
        )


def _bt_run_job(job_id: str, days: int, overrides: dict, max_symbols: int):
    def set_progress(frac, stage=""):
        with _bt_jobs_lock:
            if job_id in _bt_jobs:
                _bt_jobs[job_id]["progress"] = round(float(frac), 3)
                if stage:
                    _bt_jobs[job_id]["stage"] = stage
                _bt_jobs[job_id]["updated_at"] = time.time()

    def cancelled():
        with _bt_jobs_lock:
            job = _bt_jobs.get(job_id)
            return bool(job and job.get("cancel"))

    store = None
    kline_cache = None
    klien = None

    try:
        cfg = bt.apply_overrides(PUMP_CONFIG, overrides)
        bt.validate_params(cfg)
        _bt_days_guard(cfg, days)

        prep = _bt_prepare_universe(
            job_id, cfg, days, max_symbols, set_progress, cancelled
        )
        store = prep["store"]
        kline_cache = prep["kline_cache"]
        klien = prep.get("client")

        set_progress(0.82, "menjalankan simulasi portofolio...")
        prebuilt = _bt_native_prebuilt(prep, cfg)
        result = pbt.run_portfolio_backtest(
            store,
            cfg,
            prep["interval"],
            warmup_ms=prep["warmup_ms"],
            progress_cb=lambda f: set_progress(
                0.82 + f * 0.17, "menjalankan simulasi portofolio..."
            ),
            cancel_cb=cancelled,
            btc_klines=prep.get("btc_klines"),
            prebuilt=prebuilt,
        )
        summary = pbt.summarize_portfolio(result)

        def _ts(ms):
            if not ms:
                return None
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            )

        trades_out = [
            {
                "symbol": t.symbol,
                "entry_time": _ts(t.entry_time),
                "exit_time": _ts(t.exit_time),
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "reason": t.reason,
                "hold_minutes": t.hold_minutes,
                "pnl_pct": t.pnl_pct,
                "rank_at_entry": t.rank_at_entry,
                "pct24h_at_entry": t.pct24h_at_entry,
            }
            for t in result.trades
        ]

        skipped_out = [
            {
                "time": _ts(s.time),
                "symbol": s.symbol,
                "reason": s.reason,
                "holding": s.holding,
            }
            for s in result.skipped[:200]
        ]

        payload = {
            "mode": "portfolio",
            "kind": "portfolio",
            "interval": prep["interval"],
            "days": days,
            "universe_requested": len(prep["universe"]),
            "universe_with_data": len(prep["symbols_with_data"]),
            "symbols_failed": prep["failed"][:50],
            "symbols_failed_count": len(prep["failed"]),
            "cache": (kline_cache.stats() if kline_cache is not None else None),
            "bars_total": result.bars_total,
            "start_time": (
                (_ts(result.start_time) or "") + " UTC" if result.start_time else None
            ),
            "end_time": (
                (_ts(result.end_time) or "") + " UTC" if result.end_time else None
            ),
            "params_used": {k: cfg.get(k) for k in BT_PARAM_KEYS},
            "summary": summary,
            "direction": {
                "enabled": bool(cfg.get("DIRECTION_FILTER_ENABLED", False)),
                "interval": cfg.get("DIRECTION_INTERVAL", "1d"),
                "ema_fast": int(cfg.get("DIRECTION_EMA_FAST", 20) or 20),
                "ema_slow": int(cfg.get("DIRECTION_EMA_SLOW", 50) or 50),
                "adx_period": int(cfg.get("DIRECTION_ADX_PERIOD", 14) or 14),
                "adx_min": float(cfg.get("DIRECTION_ADX_MIN", 0.0) or 0.0),
                "lookback_bars": int(cfg.get("DIRECTION_LOOKBACK_BARS", 60) or 60),
                "skips": int(getattr(result, "direction_skips", 0)),
                "scans": int(getattr(result, "direction_scans", 0)),
            },
            "daily_zone": {
                "enabled": bool(cfg.get("DIRECTION_ZONE_FILTER_ENABLED", False)),
                "interval": cfg.get("DIRECTION_INTERVAL", "1d"),
                "lookback_bars": int(
                    cfg.get("DIRECTION_ZONE_LOOKBACK_BARS", 20) or 20
                ),
                "zone_buffer_pct": float(
                    cfg.get("DIRECTION_ZONE_BUFFER_PCT", 2.0) or 0.0
                ),
                "max_distance_pct": float(
                    cfg.get("DIRECTION_ZONE_MAX_DISTANCE_PCT", 5.0) or 0.0
                ),
                "min_close_position": float(
                    cfg.get("DIRECTION_ZONE_MIN_CLOSE_POSITION", 0.35) or 0.0
                ),
                "require_inside": bool(
                    cfg.get("DIRECTION_ZONE_REQUIRE_INSIDE_ZONE", False)
                ),
                "skips": int(getattr(result, "daily_zone_skips", 0)),
                "scans": int(getattr(result, "daily_zone_scans", 0)),
            },
            "zone": {
                "enabled": bool(cfg.get("ZONE_FILTER_ENABLED", False)),
                "interval": cfg.get("ZONE_INTERVAL", "1h"),
                "lookback_bars": int(cfg.get("ZONE_LOOKBACK_BARS", 72) or 72),
                "zone_buffer_pct": float(cfg.get("ZONE_BUFFER_PCT", 1.5) or 0.0),
                "max_distance_pct": float(cfg.get("ZONE_MAX_DISTANCE_PCT", 4.0) or 0.0),
                "require_inside": bool(cfg.get("ZONE_REQUIRE_INSIDE_ZONE", False)),
                "min_close_position": float(cfg.get("ZONE_MIN_CLOSE_POSITION", 0.35) or 0.0),
                "ema_fast": int(cfg.get("ZONE_EMA_FAST", 20) or 20),
                "ema_slow": int(cfg.get("ZONE_EMA_SLOW", 50) or 50),
                "adx_period": int(cfg.get("ZONE_ADX_PERIOD", 14) or 14),
                "adx_min": float(cfg.get("ZONE_ADX_MIN", 25.0) or 0.0),
                "trend_lookback_bars": int(
                    cfg.get("ZONE_TREND_LOOKBACK_BARS", 60) or 60
                ),
                "skips": int(getattr(result, "zone_skips", 0)),
                "scans": int(getattr(result, "zone_scans", 0)),
            },
            "timing": {
                "enabled": bool(cfg.get("TIMING_FILTER_ENABLED", False)),
                "interval": str(cfg.get("CONFIRM_INTERVAL", "5m")),
                "ema_fast": int(cfg.get("TIMING_EMA_FAST", 20) or 20),
                "ema_slow": int(cfg.get("TIMING_EMA_SLOW", 50) or 50),
                "adx_period": int(cfg.get("TIMING_ADX_PERIOD", 14) or 14),
                "adx_min": float(cfg.get("TIMING_ADX_MIN", 0.0) or 0.0),
                "lookback_bars": int(cfg.get("TIMING_LOOKBACK_BARS", 60) or 60),
                "skips": int(getattr(result, "timing_skips", 0)),
                "scans": int(getattr(result, "timing_scans", 0)),
            },
            "breakout_retest": {
                "enabled": bool(cfg.get("BREAKOUT_RETEST_FILTER_ENABLED", False)),
                "interval": str(cfg.get("CONFIRM_INTERVAL", "5m")),
                "lookback_bars": int(cfg.get("BREAKOUT_LOOKBACK_BARS", 30) or 30),
                "pivot_wing_bars": int(
                    cfg.get("BREAKOUT_PIVOT_WING_BARS", 2) or 2
                ),
                "atr_period": int(cfg.get("BREAKOUT_ATR_PERIOD", 14) or 14),
                "tolerance_atr": float(
                    cfg.get("BREAKOUT_RETEST_TOLERANCE_ATR", 0.2) or 0.0
                ),
                "max_bars": int(cfg.get("BREAKOUT_RETEST_MAX_BARS", 12) or 12),
                "max_touches": int(
                    cfg.get("BREAKOUT_RETEST_MAX_TOUCHES", 2) or 2
                ),
                "require_bullish": bool(
                    cfg.get("BREAKOUT_RETEST_REQUIRE_BULLISH", True)
                ),
                "skips": int(getattr(result, "breakout_skips", 0)),
                "scans": int(getattr(result, "breakout_scans", 0)),
            },
            "rolling_volume": {
                "enabled": bool(cfg.get("ROLLING_VOLUME_FILTER_ENABLED", False)),
                "interval": str(cfg.get("CONFIRM_INTERVAL", "5m")),
                "lookback_bars": int(
                    cfg.get("ROLLING_VOLUME_LOOKBACK_BARS", 20) or 20
                ),
                "surge_mult": float(cfg.get("ROLLING_VOLUME_SURGE_MULT", 1.2) or 0.0),
                "confirmation_bars": int(
                    cfg.get("ROLLING_VOLUME_CONFIRMATION_BARS", 1) or 1
                ),
            },
            "demand_m5": {
                "enabled": bool(cfg.get("DEMAND_ZONE_FILTER_ENABLED", False)),
                "interval": str(cfg.get("CONFIRM_INTERVAL", "5m")),
                "lookback_bars": int(cfg.get("DEMAND_LOOKBACK_BARS", 20) or 20),
                "zone_buffer_pct": float(
                    cfg.get("DEMAND_ZONE_BUFFER_PCT", 0.8) or 0.0
                ),
                "max_distance_pct": float(
                    cfg.get("DEMAND_MAX_DISTANCE_PCT", 4.5) or 0.0
                ),
                "min_close_position": float(
                    cfg.get("DEMAND_MIN_CLOSE_POSITION", 0.35) or 0.0
                ),
                "require_inside": bool(
                    cfg.get("DEMAND_REQUIRE_INSIDE_ZONE", False)
                ),
            },
            "trades": trades_out,
            "skipped": skipped_out,
            "warnings": (
                list(result.warnings)
                + _bt_peringatan_timing(cfg, days)
                + (
                    [
                        f"Data BTC gagal diunduh ({prep.get('btc_error')}) sehingga filter BTC "
                        "tidak diterapkan."
                    ]
                    if (
                        cfg.get("BTC_FILTER_ENABLED", False)
                        and prep.get("btc_klines") is None
                        and not any("BTC" in w for w in result.warnings)
                    )
                    else []
                )
            ),
            "limitations": [
                "SURVIVORSHIP BIAS, dan ini tidak bisa diperbaiki: Binance hanya menyediakan "
                "data historis untuk pair yang MASIH listing hari ini. Koin yang sudah "
                "didelisting, sering justru yang kolaps setelah pump, tidak ada dalam data. "
                "Hasil di sini karenanya masih cenderung lebih baik daripada kenyataan.",
                "Statistik 24 jam DIREKONSTRUKSI dari candle, bukan diambil dari snapshot "
                "ticker/24hr historis (Binance tidak menyediakannya). Nilainya sangat dekat "
                "tetapi tidak identik dengan yang dilihat bot saat itu. Volume itulah yang "
                "menentukan simbol mana yang masuk top-N kandidat per bar.",
                "Fill exit memperhitungkan gap: candle yang DIBUKA sudah menembus level "
                "SL/TP/BE/Trailing diisi pada harga pembukaan candle itu, konsisten dengan "
                "simulasi PAPER, bukan pada harga levelnya. Candle tempat posisi dibuka "
                "(termasuk candle entry saat ada jeda eksekusi) juga ikut dievaluasi.",
                "Exit dievaluasi per-candle "
                + prep["interval"]
                + " (bukan tiap "
                + str(PUMP_CONFIG.get("LOOP_INTERVAL_SECONDS", 15))
                + " detik seperti bot asli). "
                "Urutan konservatif: stop BE/trailing yang sudah aktif dari candle sebelumnya, "
                "lalu STOP_LOSS, TAKE_PROFIT, dan BE/trailing yang baru aktif di candle yang "
                "sama. Stop Loss dianggap kena lebih dulu kalau ambigu dalam satu candle. "
                "TIME_STOP (MAX_HOLD_HOURS) diperiksa paling akhir dan menutup posisi di "
                "harga close candle terakhir yang umurnya masih di dalam batas.",
                "Entry memodelkan spread (BACKTEST_ENTRY_SPREAD_PCT), slippage "
                "(BACKTEST_SLIPPAGE_PCT), dan jeda eksekusi (BACKTEST_ENTRY_DELAY_BARS). "
                "Fee taker beli dan jual sudah dipotong. Kedalaman order book belum "
                "dimodelkan, jadi order besar di koin tipis akan lebih buruk dari ini. "
                "Filter kedalaman, ketimpangan bid/ask, dan sell wall juga hanya berlaku di "
                "PAPER dan LIVE (snapshot order book tidak tersedia historis). "
                "Batas minimum kenaikan 24 jam (PUMP_MIN_24H_CHANGE_PCT) "
                "diterapkan sesuai konfigurasi, tanpa batas maksimum kenaikan.",
                "Layer arah (DIRECTION_FILTER_ENABLED) SUDAH disimulasikan memakai aturan "
                "yang sama dengan bot live: DIRECTION_LOOKBACK_BARS candle "
                + str(cfg.get("DIRECTION_INTERVAL", "1d"))
                + " terakhir yang sudah tutup pada saat candle sinyal ditutup, lalu close > "
                "EMA cepat, EMA cepat > EMA lambat, dan ADX >= DIRECTION_ADX_MIN. Candle "
                "arah diambil langsung dari bursa, sama persis seperti bot live, dan "
                "riwayat sebelumnya rentang uji diunduh otomatis. Bedanya dengan live: "
                "backtest memakai candle yang benar-benar ada di bursa pada rentang itu, "
                "sedangkan live memakai candle terbaru yang sudah tutup. "
                + _bt_direction_warmup_note(cfg, prep["interval"])
                + _bt_demand_limitations(cfg, prep["interval"])
                + _bt_timing_note(cfg, prep["interval"])
                + " Filter live yang SUDAH disimulasikan dari candle: filter BTC (BTC_MAX_DROP_PCT, "
                "memakai candle BTC historis), MAX_CHASE_PCT, MIN_SECONDS_BETWEEN_TRADES, "
                "COOLDOWN_MINUTES_AFTER_CLOSE, equity stop (drawdown), dan "
                "CLOSE_ALL_AT_LIMIT. Kontrol akun dicek saat candle ditutup, bukan tiap "
                + str(PUMP_CONFIG.get("LOOP_INTERVAL_SECONDS", 15))
                + " detik, sehingga "
                "penutupan paksa di bot asli bisa terjadi lebih awal dari di sini.",
                "Filter live yang BELUM disimulasikan (butuh data yang tidak tersedia dari "
                "candle historis): spread order book (MAX_SPREAD_PCT), filter kedalaman, "
                "ketimpangan bid/ask dan sell wall, usia listing, serta "
                "aturan lot size dan min notional. Stop-limit native bot bisa gagal terisi "
                "saat harga gap melewati buffer, sedangkan backtest selalu terisi. Bot asli "
                "juga memindai tiap beberapa menit sehingga bisa masuk sampai sekitar satu "
                "candle lebih lambat dari simulasi.",
                "Drawdown dan kurva equity dihitung dari trade yang sudah selesai, bukan "
                "mark-to-market harian. Modal awal mengikuti saldo PAPER bila "
                "BACKTEST_INITIAL_EQUITY_USDT = 0. Persen return bergantung pada rasio ukuran "
                "posisi terhadap modal, jadi bandingkan strategi lewat metrik per trade "
                "(rata-rata per trade, payoff ratio) bila modal berbeda.",
                "Volume 24 jam direkonstruksi dari penjumlahan quote volume candle, "
                "sehingga bisa sedikit berbeda dari field quoteVolume di ticker.",
            ],
        }

        with _bt_jobs_lock:
            if job_id in _bt_jobs:
                _bt_jobs[job_id].update(
                    {
                        "status": "done",
                        "progress": 1.0,
                        "stage": "selesai",
                        "result": _json_safe(payload),
                    }
                )
    except bt.BacktestError as exc:
        with _bt_jobs_lock:
            if job_id in _bt_jobs:
                _bt_jobs[job_id].update({"status": "error", "error": str(exc)})
    except Exception as exc:
        with _bt_jobs_lock:
            if job_id in _bt_jobs:
                _bt_jobs[job_id].update(
                    {"status": "error", "error": f"Error tak terduga: {exc}"}
                )
    finally:
        if store is not None:
            store.cleanup()
        if kline_cache is not None:
            kline_cache.close()
        if klien is not None:
            try:
                klien.close()
            except Exception:
                logger.debug("Gagal menutup klien backtest", exc_info=True)


def _bt_run_grid_job(
    job_id: str,
    days: int,
    max_symbols: int,
    spec: dict,
    rasio_latih: float,
    metrik: str,
    min_trades: int,
    total_kombinasi: int,
    overrides: dict | None = None,
):
    def set_progress(frac, stage=""):
        with _bt_jobs_lock:
            if job_id in _bt_jobs:
                _bt_jobs[job_id]["progress"] = round(float(frac), 3)
                if stage:
                    _bt_jobs[job_id]["stage"] = stage
                _bt_jobs[job_id]["updated_at"] = time.time()

    def cancelled():
        with _bt_jobs_lock:
            job = _bt_jobs.get(job_id)
            return bool(job and job.get("cancel"))

    store = None
    kline_cache = None
    klien = None

    try:
        cfg = bt.apply_overrides(PUMP_CONFIG, overrides or {})
        bt.validate_params(cfg)
        _bt_days_guard(cfg, days)

        prep = _bt_prepare_universe(
            job_id, cfg, days, max_symbols, set_progress, cancelled
        )
        store = prep["store"]
        kline_cache = prep["kline_cache"]
        klien = prep.get("client")

        prebuilt = _bt_native_prebuilt(prep, cfg)
        set_progress(0.30, f"menjalankan grid search ({total_kombinasi} kombinasi)...")
        hasil = gs.run_portfolio_grid_search(
            store,
            cfg,
            prep["interval"],
            prep["warmup_ms"],
            spec,
            metrik=metrik,
            rasio_latih=rasio_latih,
            min_trades=min_trades,
            progress_cb=lambda f: set_progress(
                0.30 + f * 0.69,
                f"grid search {total_kombinasi} kombinasi "
                f"({int(round(f * total_kombinasi))}/{total_kombinasi})",
            ),
            cancel_cb=cancelled,
            btc_klines=prep.get("btc_klines"),
            native_prebuilt=prebuilt,
        )
        rows = gs.ringkas_untuk_tabel(hasil, top_n=20)

        payload = {
            "kind": "grid",
            "mode": "grid",
            "interval": prep["interval"],
            "days": days,
            "universe_requested": len(prep["universe"]),
            "universe_with_data": len(prep["symbols_with_data"]),
            "symbols_failed_count": len(prep["failed"]),
            "cache": (kline_cache.stats() if kline_cache is not None else None),
            "params_used": {k: cfg.get(k) for k in BT_PARAM_KEYS},
            "grid": {
                "total_kombinasi": hasil.total_kombinasi,
                "dipangkas": hasil.dipangkas,
                "dilewati": hasil.dilewati,
                "dibatalkan": hasil.dibatalkan,
                "metrik": metrik,
                "rasio_latih": rasio_latih,
                "min_trades": min_trades,
                "bar_latih": hasil.bar_latih,
                "bar_uji": hasil.bar_uji,
                "peringatan": hasil.peringatan,
                "detik": round(hasil.detik, 1),
            },
            "rows": rows,
            "warnings": (
                [
                    f"Filter BTC aktif tetapi data BTC gagal diunduh ({prep.get('btc_error')}); "
                    "filter BTC TIDAK diterapkan pada grid ini."
                ]
                if (
                    cfg.get("BTC_FILTER_ENABLED", False)
                    and prep.get("btc_klines") is None
                )
                else []
            ),
            "limitations": [
                "Grid ini berjalan di atas simulasi PORTOFOLIO (satu posisi lintas "
                "simbol, prioritas volume) dengan parameter exit yang sama seperti "
                "form backtest, sehingga hasilnya konsisten dengan mode Simulasi "
                "Portofolio pada data yang sama.",
                "SURVIVORSHIP BIAS, dan ini tidak bisa diperbaiki: Binance hanya "
                "menyediakan data historis untuk pair yang MASIH listing hari ini. "
                "Koin yang sudah didelisting, sering justru yang kolaps setelah "
                "pump, tidak ada dalam data. Hasil di sini karenanya masih cenderung "
                "lebih baik daripada kenyataan.",
                "Peringkat disusun dari skor LATIH, bukan skor uji, agar periode uji "
                "tetap menjadi data yang belum pernah dilihat. Saat memilih kombinasi, "
                "utamakan skor uji yang wajar dengan degradasi kecil, bukan skor latih "
                "tertinggi.",
                "Setiap kombinasi menguji ULANG data yang sama. Semakin banyak "
                "kombinasi, semakin besar peluang hasil terbaik muncul karena "
                "kebetulan; perlakukan hasil grid sebagai kandidat yang harus lolos "
                "periode uji, bukan janji kinerja.",
                "Split latih/uji di sini satu kali berdasarkan urutan waktu (bukan "
                "walk-forward bergulir). Untuk keyakinan lebih, ulangi dengan beberapa "
                "rasio dan rentang hari yang berbeda.",
                "Yang disapu grid hanya parameter EXIT. Layer arah (DIRECTION_*), layer "
                "zona (ZONE_*), layer timing (TIMING_*), breakout-retest (BREAKOUT_*), "
                "rolling volume (ROLLING_*), demand M5 (DEMAND_*), dan USE_ATR_EXIT tidak "
                "ikut disapu: nilainya diambil dari konfigurasi backtest dan tetap sama pada "
                "semua kombinasi. Lookup timeframe tinggi digunakan ulang. Untuk "
                "membandingkan setelan trend, jalankan backtest portofolio terpisah per setelan.",
                "Statistik 24 jam DIREKONSTRUKSI dari candle, bukan snapshot "
                "ticker/24hr historis. Volume itulah yang menentukan simbol mana yang "
                "masuk top-N kandidat per bar.",
                "Fill memodelkan spread, slippage, dan fee taker dua sisi. Kedalaman "
                "order book TIDAK dimodelkan, jadi order besar di koin tipis akan "
                "lebih buruk dari hasil di sini. Filter kedalaman, ketimpangan bid/ask, dan "
                "sell wall hanya berlaku di PAPER dan LIVE. Batas minimum kenaikan "
                "24 jam (PUMP_MIN_24H_CHANGE_PCT) diterapkan sesuai konfigurasi, "
                "tanpa batas maksimum kenaikan.",
                "Filter live yang SUDAH disimulasikan dari candle: filter BTC, "
                "MAX_CHASE_PCT, MIN_SECONDS_BETWEEN_TRADES, COOLDOWN_MINUTES_AFTER_CLOSE, "
                "equity stop, dan CLOSE_ALL_AT_LIMIT (dicek saat candle "
                "ditutup, bukan tiap beberapa detik). Yang BELUM: spread order book "
                "(MAX_SPREAD_PCT), usia listing, lot size dan min notional, serta gap "
                "yang melewati buffer stop-limit native.",
                "Persen return dan drawdown bergantung pada rasio ukuran posisi terhadap "
                "modal. Modal awal mengikuti saldo PAPER bila BACKTEST_INITIAL_EQUITY_USDT "
                "= 0. Untuk membandingkan kombinasi, perhatikan juga metrik per trade.",
            ],
        }

        with _bt_jobs_lock:
            if job_id in _bt_jobs:
                _bt_jobs[job_id].update(
                    {
                        "status": "done",
                        "progress": 1.0,
                        "stage": "selesai",
                        "result": _json_safe(payload),
                    }
                )
    except (bt.BacktestError, gs.GridSearchError) as exc:
        with _bt_jobs_lock:
            if job_id in _bt_jobs:
                _bt_jobs[job_id].update({"status": "error", "error": str(exc)})
    except Exception as exc:
        with _bt_jobs_lock:
            if job_id in _bt_jobs:
                _bt_jobs[job_id].update(
                    {"status": "error", "error": f"Error tak terduga: {exc}"}
                )
    finally:
        if store is not None:
            store.cleanup()
        if kline_cache is not None:
            kline_cache.close()
        if klien is not None:
            try:
                klien.close()
            except Exception:
                logger.debug("Gagal menutup klien backtest", exc_info=True)


def _bt_demand_limitations(cfg: dict, interval: str) -> str:
    return (
        "Layer zona H1 juga SUDAH disimulasikan dengan aturan yang sama seperti bot "
        "live: memakai candle "
        + str(cfg.get("ZONE_INTERVAL", "1h"))
        + " yang sudah tutup untuk memeriksa demand zone, EMA"
        + str(int(cfg.get("ZONE_EMA_FAST", 20) or 20))
        + "/"
        + str(int(cfg.get("ZONE_EMA_SLOW", 50) or 50))
        + ", dan ADX"
        + str(int(cfg.get("ZONE_ADX_PERIOD", 14) or 14))
        + " minimal "
        + f"{float(cfg.get('ZONE_ADX_MIN', 25.0) or 0.0):g}. "
        "Lapisan ini diperiksa SETELAH layer arah, dan "
        "fail closed bila candle tidak cukup. Penghitung "
        "\"disaring X dari Y sinyal\" pada ringkasan hanya menghitung sinyal yang "
        "SUDAH lolos gerbang sebelumnya: angka 0 berarti lapisan itu tidak pernah "
        "diperiksa (karena gerbang di atasnya menolak lebih dulu), bukan berarti "
        "lapisan itu meloloskan semuanya. "
        + _bt_zone_warmup_note(cfg, interval)
    )


def _bt_min_days_note(cfg: dict, interval: str) -> tuple[int, str]:
    dasar = 2
    arah_aktif = bool(cfg.get("DIRECTION_FILTER_ENABLED", False)) or bool(
        cfg.get("DIRECTION_ZONE_FILTER_ENABLED", False)
    )
    zona_aktif = bool(cfg.get("ZONE_FILTER_ENABLED", False))
    timing_aktif = bool(cfg.get("TIMING_FILTER_ENABLED", False))
    breakout_aktif = bool(cfg.get("BREAKOUT_RETEST_FILTER_ENABLED", False))
    rolling_aktif = bool(cfg.get("ROLLING_VOLUME_FILTER_ENABLED", False))
    demand_aktif = bool(cfg.get("DEMAND_ZONE_FILTER_ENABLED", False))
    m5_aktif = any((timing_aktif, breakout_aktif, rolling_aktif, demand_aktif))
    if not arah_aktif and not zona_aktif and not m5_aktif:
        return dasar, ""

    native_htf = bool(_bt_native_htf_intervals(cfg))
    minimal = dasar
    catatan = ""
    if arah_aktif or zona_aktif:
        catatan = (
            "Layer arah (Daily) dan layer zona (H1) memakai candle asli bursa, "
            "sama seperti bot live, jadi riwayat timeframe tinggi tidak menambah "
            "batas minimum hari pada rentang simulasi. Riwayat yang dibutuhkan "
            "tetap diunduh otomatis sebelum rentang uji."
        )
    if not native_htf:
        try:
            jam_gerbang = pbt.parity.gate_warmup_ms(cfg, interval) / 3_600_000
        except ValueError as exc:
            return dasar, str(exc)
        if jam_gerbang > 0:
            minimal = max(minimal, int(math.ceil(jam_gerbang / 24.0)) + 1)
            catatan += (
                f" Tanpa candle timeframe tinggi native, gerbang Daily dan H1 harus "
                f"dirangkai dari candle {interval} sehingga butuh sekitar "
                f"{jam_gerbang:.2f} jam pemanasan."
            )
    if m5_aktif:
        try:
            m5_bars = strategy.required_lookback_bars(cfg)
            jam_timing = m5_bars * strategy.interval_to_ms(interval) / 3_600_000
        except (ValueError, KeyError) as exc:
            return dasar, str(exc)
        minimal = max(minimal, int(math.ceil(jam_timing / 24.0)) + 1)
        catatan += (
            f" Gerbang M5 ({cfg.get('CONFIRM_INTERVAL', '5m')}) butuh sekitar "
            f"{jam_timing:.2f} jam riwayat sebelum bar pertama bisa dievaluasi."
        )
    return minimal, catatan.strip()


def _bt_peringatan_timing(cfg: dict, days: int) -> list:
    m5_active = any(
        bool(cfg.get(key, False))
        for key in (
            "TIMING_FILTER_ENABLED",
            "BREAKOUT_RETEST_FILTER_ENABLED",
            "ROLLING_VOLUME_FILTER_ENABLED",
            "DEMAND_ZONE_FILTER_ENABLED",
        )
    )
    if not m5_active:
        return []
    interval = str(cfg.get("CONFIRM_INTERVAL", "5m") or "5m")
    bar_uji = int(days) * 1440 // max(1, bt.INTERVAL_MINUTES.get(interval, 5))
    butuh = strategy.required_lookback_bars(cfg)
    if bar_uji >= butuh:
        return []
    return [
        f"Rentang uji {int(days)} hari mencakup sekitar {bar_uji} candle {interval}, "
        f"lebih pendek dari kebutuhan gerbang M5 ({butuh} candle). Gerbang tetap "
        "dijalankan, tetapi hasilnya kurang representatif; perpanjang rentang uji."
    ]


@app.route("/api/backtest/start", methods=["POST"])
def api_backtest_start():
    blocked = _reject_if_backtest_disabled()
    if blocked is not None:
        return blocked
    _bt_cleanup_old_jobs()
    data = request.get_json(force=True, silent=True) or {}

    try:
        days = int(data.get("days", 30))
    except (TypeError, ValueError):
        return jsonify({"error": "Jumlah hari tidak valid."}), 400
    if days < 2:
        return (
            jsonify(
                {
                    "error": "Jumlah hari minimal 2 (satu hari pertama dipakai warmup statistik 24 jam)."
                }
            ),
            400,
        )

    try:
        cfg_hari = bt.apply_overrides(
            PUMP_CONFIG, {k: data.get(k) for k in BT_PARAM_KEYS if k in data}
        )
    except bt.BacktestError:
        cfg_hari = (
            PUMP_CONFIG
        )
    minimal_hari, catatan_hari = _bt_min_days_note(
        cfg_hari, _bt_simulation_interval(cfg_hari)
    )
    if days < minimal_hari:
        return (
            jsonify(
                {
                    "error": (
                        f"Jumlah hari minimal {minimal_hari} untuk setelan ini, "
                        f"sedangkan yang diminta {days} hari. {catatan_hari}"
                    ).strip()
                }
            ),
            400,
        )

    try:
        max_symbols = int(data.get("max_symbols", 150))
    except (TypeError, ValueError):
        return jsonify({"error": "Jumlah simbol tidak valid."}), 400
    if max_symbols < 2:
        return (
            jsonify(
                {
                    "error": "Jumlah simbol minimal 2 (kalau hanya 1, tidak ada persaingan antar-simbol untuk disimulasikan)."
                }
            ),
            400,
        )
    if max_symbols > 600:
        return jsonify({"error": "Jumlah simbol maksimal 600."}), 400

    est_requests = _bt_estimate_requests(days, max_symbols, PUMP_CONFIG)
    if est_requests > 20000:
        return (
            jsonify(
                {
                    "error": f"Permintaan terlalu besar (perkiraan {est_requests:,} request ke Binance). "
                    f"Kurangi jumlah simbol atau jumlah hari."
                }
            ),
            400,
        )

    overrides = {k: data.get(k) for k in BT_PARAM_KEYS}
    try:
        cfg_preview = bt.apply_overrides(PUMP_CONFIG, overrides)
        bt.validate_params(cfg_preview)
    except bt.BacktestError as exc:
        return jsonify({"error": str(exc)}), 400

    with _bt_jobs_lock:
        running = sum(1 for j in _bt_jobs.values() if j["status"] == "running")
        if running >= 1:
            return (
                jsonify(
                    {
                        "error": "Sudah ada backtest berjalan. Tunggu selesai atau batalkan dulu."
                    }
                ),
                429,
            )

        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        _bt_jobs[job_id] = {
            "status": "running",
            "progress": 0.0,
            "stage": "memulai...",
            "created_at": now,
            "started_at": now,
            "updated_at": now,
            "days": days,
            "max_symbols": max_symbols,
            "cancel": False,
        }

    thread = threading.Thread(
        target=_bt_run_job, args=(job_id, days, overrides, max_symbols), daemon=True
    )
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/backtest/grid/start", methods=["POST"])
def api_backtest_grid_start():
    blocked = _reject_if_backtest_disabled()
    if blocked is not None:
        return blocked
    _bt_cleanup_old_jobs()
    data = request.get_json(force=True, silent=True) or {}

    try:
        days = int(data.get("days", 30))
    except (TypeError, ValueError):
        return jsonify({"error": "Jumlah hari tidak valid."}), 400
    if days < 2:
        return (
            jsonify(
                {
                    "error": "Jumlah hari minimal 2 (satu hari pertama dipakai warmup statistik 24 jam)."
                }
            ),
            400,
        )

    try:
        max_symbols = int(data.get("max_symbols", 150))
    except (TypeError, ValueError):
        return jsonify({"error": "Jumlah simbol tidak valid."}), 400
    if max_symbols < 2 or max_symbols > 600:
        return jsonify({"error": "Jumlah simbol harus di antara 2 dan 600."}), 400

    est_requests = _bt_estimate_requests(days, max_symbols, PUMP_CONFIG)
    if est_requests > 20000:
        return (
            jsonify(
                {
                    "error": f"Permintaan terlalu besar (perkiraan {est_requests:,} request ke Binance). "
                    f"Kurangi jumlah simbol atau jumlah hari."
                }
            ),
            400,
        )

    try:
        rasio_latih = float(data.get("rasio_latih", 0.7))
    except (TypeError, ValueError):
        return jsonify({"error": "Rasio periode latih tidak valid."}), 400
    if not 0.1 <= rasio_latih <= 1.0:
        return (
            jsonify({"error": "Rasio periode latih harus di antara 0.1 dan 1.0."}),
            400,
        )

    metrik = str(data.get("metrik", "total_return_pct"))
    if metrik not in gs.METRIK_TERSEDIA:
        return (
            jsonify(
                {
                    "error": f"Metrik '{metrik}' tidak dikenal. "
                    f"Pilihan: {', '.join(gs.METRIK_TERSEDIA)}."
                }
            ),
            400,
        )

    try:
        min_trades = int(data.get("min_trades", 5))
    except (TypeError, ValueError):
        return jsonify({"error": "Jumlah trade minimal tidak valid."}), 400
    if min_trades < 1 or min_trades > 1000:
        return (
            jsonify({"error": "Jumlah trade minimal harus di antara 1 dan 1000."}),
            400,
        )

    spec_raw = data.get("spec")
    if not isinstance(spec_raw, dict) or not spec_raw:
        return (
            jsonify(
                {
                    "error": "Spec grid kosong. Pilih minimal satu parameter "
                    "beserta nilai rentangnya."
                }
            ),
            400,
        )
    overrides = {k: data.get(k) for k in BT_PARAM_KEYS if k in data}
    try:
        cfg_preview = bt.apply_overrides(PUMP_CONFIG, overrides)
        bt.validate_params(cfg_preview)
    except bt.BacktestError as exc:
        return jsonify({"error": str(exc)}), 400

    minimal_hari, catatan_hari = _bt_min_days_note(
        cfg_preview, _bt_simulation_interval(cfg_preview)
    )
    if days < minimal_hari:
        return (
            jsonify(
                {
                    "error": (
                        f"Jumlah hari minimal {minimal_hari} untuk setelan ini, "
                        f"sedangkan yang diminta {days} hari. {catatan_hari}"
                    ).strip()
                }
            ),
            400,
        )

    pakai_atr = bool(cfg_preview.get("USE_ATR_EXIT", False))
    spec = {}
    for key, values in spec_raw.items():
        if key not in GRID_PARAM_KEYS:
            return (
                jsonify(
                    {
                        "error": f"Parameter '{key}' tidak diperbolehkan untuk grid. "
                        f"Pilihan: {', '.join(GRID_PARAM_KEYS)}."
                    }
                ),
                400,
            )
        if pakai_atr and key in gs.KUNCI_PERSEN:
            return (
                jsonify(
                    {
                        "error": f"Parameter '{key}' tidak berpengaruh karena exit "
                        "ATR sedang aktif (USE_ATR_EXIT=true): mesin "
                        "mengabaikan seluruh parameter persen. Matikan "
                        "exit ATR di Pengaturan dulu, atau pilih "
                        "parameter ATR."
                    }
                ),
                400,
            )
        if not pakai_atr and key in gs.KUNCI_ATR:
            return (
                jsonify(
                    {
                        "error": f"Parameter '{key}' tidak berpengaruh karena exit "
                        "ATR sedang mati (USE_ATR_EXIT=false): mesin "
                        "mengabaikan seluruh parameter ATR. Aktifkan "
                        "exit ATR di Pengaturan dulu, atau pilih "
                        "parameter persen."
                    }
                ),
                400,
            )
        if not isinstance(values, (list, tuple)) or not values:
            return (
                jsonify(
                    {
                        "error": f"Nilai parameter '{key}' harus daftar angka "
                        f"yang tidak kosong."
                    }
                ),
                400,
            )
        bersih = []
        for v in values:
            if (
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(float(v))
            ):
                return (
                    jsonify(
                        {
                            "error": f"Nilai parameter '{key}' harus angka "
                            f"(dapat: {v!r})."
                        }
                    ),
                    400,
                )
            if v not in bersih:
                bersih.append(v)
        if not bersih:
            return (
                jsonify(
                    {
                        "error": f"Nilai parameter '{key}' kosong setelah "
                        f"duplikat dibuang."
                    }
                ),
                400,
            )
        if len(bersih) > 50:
            return jsonify({"error": f"Parameter '{key}' maksimal 50 nilai."}), 400
        spec[key] = bersih

    total = 1
    for values in spec.values():
        total *= len(values)
    if total > gs.MAX_KOMBINASI_PORTFOLIO:
        return (
            jsonify(
                {
                    "error": f"Grid menghasilkan {total} kombinasi, melebihi batas "
                    f"{gs.MAX_KOMBINASI_PORTFOLIO} untuk simulasi portofolio. "
                    f"Persempit rentang atau perbesar langkah."
                }
            ),
            400,
        )

    with _bt_jobs_lock:
        running = sum(1 for j in _bt_jobs.values() if j["status"] == "running")
        if running >= 1:
            return (
                jsonify(
                    {
                        "error": "Sudah ada backtest/grid berjalan. "
                        "Tunggu selesai atau batalkan dulu."
                    }
                ),
                429,
            )

        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        _bt_jobs[job_id] = {
            "kind": "grid",
            "status": "running",
            "progress": 0.0,
            "stage": "memulai...",
            "created_at": now,
            "started_at": now,
            "updated_at": now,
            "days": days,
            "max_symbols": max_symbols,
            "grid": {
                "metrik": metrik,
                "rasio_latih": rasio_latih,
                "min_trades": min_trades,
                "total_kombinasi": total,
            },
            "cancel": False,
        }

    thread = threading.Thread(
        target=_bt_run_grid_job,
        args=(
            job_id,
            days,
            max_symbols,
            spec,
            rasio_latih,
            metrik,
            min_trades,
            total,
            overrides,
        ),
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/backtest/status/<job_id>")
def api_backtest_status(job_id):
    blocked = _reject_if_backtest_disabled()
    if blocked is not None:
        return blocked
    with _bt_jobs_lock:
        job = _bt_jobs.get(job_id)
        if job is None:
            return (
                jsonify(
                    {
                        "error": "Job backtest tidak ditemukan (mungkin sudah kedaluwarsa)."
                    }
                ),
                404,
            )
        out = dict(job)

    started_at = out.get("started_at")
    progress = out.get("progress", 0.0) or 0.0
    if out.get("status") == "running" and started_at and progress >= 0.02:
        elapsed = max(0.0, time.time() - started_at)
        total_est = elapsed / progress
        eta = max(0.0, total_est - elapsed)
        out["elapsed_sec"] = round(elapsed, 1)
        out["eta_sec"] = round(eta, 1)
    elif out.get("status") == "running" and started_at:
        out["elapsed_sec"] = round(max(0.0, time.time() - started_at), 1)
        out["eta_sec"] = None
    else:
        out["eta_sec"] = None

    return jsonify(out)


@app.route("/api/backtest/cancel/<job_id>", methods=["POST"])
def api_backtest_cancel(job_id):
    blocked = _reject_if_backtest_disabled()
    if blocked is not None:
        return blocked
    with _bt_jobs_lock:
        job = _bt_jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job tidak ditemukan."}), 404
        if job["status"] != "running":
            return jsonify({"ok": True, "message": "Job sudah selesai."})
        job["cancel"] = True
    return jsonify({"ok": True, "message": "Permintaan batal dikirim."})


@app.route("/api/backtest/defaults")
def api_backtest_defaults():
    blocked = _reject_if_backtest_disabled()
    if blocked is not None:
        return blocked
    out = {k: PUMP_CONFIG.get(k) for k in BT_PARAM_KEYS}
    out["quote_asset"] = QUOTE
    out["default_days"] = 30
    out["default_max_symbols"] = 150
    out["bars_per_day"] = bt.bars_per_day(_bt_simulation_interval(PUMP_CONFIG))
    out["mode"] = "portfolio"
    out["min_quote_volume"] = PUMP_CONFIG.get("MIN_QUOTE_VOLUME_USDT_24H", 0)
    out["grid_max_kombinasi"] = gs.MAX_KOMBINASI_PORTFOLIO
    out["grid_metrik"] = list(gs.METRIK_TERSEDIA)
    out["grid_params"] = list(GRID_PARAM_KEYS)
    out["warmup_bars"] = int(_bt_simulation_warmup_bars(PUMP_CONFIG))
    out["warmup_days"] = round(
        out["warmup_bars"] / float(bt.bars_per_day(_bt_simulation_interval(PUMP_CONFIG))),
        1,
    )
    out["native_intervals"] = _bt_native_htf_intervals(PUMP_CONFIG)
    out["native_history_days"] = round(
        strategy.native_history_ms(PUMP_CONFIG) / float(bt.MS_PER_DAY), 1
    )
    out["direction_enabled"] = bool(
        PUMP_CONFIG.get("DIRECTION_FILTER_ENABLED", False)
    )
    out["direction_interval"] = str(PUMP_CONFIG.get("DIRECTION_INTERVAL", "1d"))
    out["direction_lookback_bars"] = int(
        PUMP_CONFIG.get("DIRECTION_LOOKBACK_BARS", 60) or 60
    )
    out["direction_zone_enabled"] = bool(
        PUMP_CONFIG.get("DIRECTION_ZONE_FILTER_ENABLED", False)
    )
    out["direction_zone_interval"] = str(PUMP_CONFIG.get("DIRECTION_INTERVAL", "1d"))
    out["direction_zone_lookback_bars"] = int(
        PUMP_CONFIG.get("DIRECTION_ZONE_LOOKBACK_BARS", 20) or 20
    )
    out["direction_zone_max_distance_pct"] = float(
        PUMP_CONFIG.get("DIRECTION_ZONE_MAX_DISTANCE_PCT", 5.0) or 0.0
    )
    out["direction_zone_require_inside"] = bool(
        PUMP_CONFIG.get("DIRECTION_ZONE_REQUIRE_INSIDE_ZONE", False)
    )
    out["zone_require_inside"] = bool(
        PUMP_CONFIG.get("ZONE_REQUIRE_INSIDE_ZONE", False)
    )
    out["demand_require_inside"] = bool(
        PUMP_CONFIG.get("DEMAND_REQUIRE_INSIDE_ZONE", False)
    )
    out["zone_enabled"] = bool(PUMP_CONFIG.get("ZONE_FILTER_ENABLED", False))
    out["zone_interval"] = str(PUMP_CONFIG.get("ZONE_INTERVAL", "1h"))
    out["zone_lookback_bars"] = int(PUMP_CONFIG.get("ZONE_LOOKBACK_BARS", 72) or 72)
    out["timing_enabled"] = bool(PUMP_CONFIG.get("TIMING_FILTER_ENABLED", False))
    out["timing_interval"] = str(PUMP_CONFIG.get("CONFIRM_INTERVAL", "5m"))
    out["timing_lookback_bars"] = int(PUMP_CONFIG.get("TIMING_LOOKBACK_BARS", 60) or 60)
    return jsonify(out)


@app.route("/favicon.ico")
def favicon():
    return redirect("/static/favicon.ico", code=302)


@app.route("/")
def index():
    return render_template("dashboard.html", admin_token=_ADMIN_TOKEN)


@app.route("/api/status")
def api_status():
    return jsonify(build_status())


@app.route("/api/coin-icon/<symbol>")
def api_coin_icon(symbol):
    url = coin_icons.resolve_icon_url(symbol)
    if not url:
        abort(404)
    return redirect(url, code=302)


@app.route("/api/fx")
def api_fx():
    forced = str(request.args.get("refresh", "")).strip().lower() in (
        "1",
        "true",
        "yes",
        "ya",
    )
    return jsonify(get_idr_rate(force=forced))


@app.route("/api/trades")
def api_trades():
    trades, _, level_count = parse_log()
    return jsonify(
        {
            "summary": build_trade_summary(trades),
            "trades": trades[:100],
            "log_levels": level_count,
        }
    )


@app.route("/api/logs")
def api_logs():
    _, events, _ = parse_log()
    return jsonify({"events": events})


def _bot_looks_alive() -> bool:
    try:
        status = _process_manager.status(get_mode(PUMP_CONFIG))
        if status["status"] in ("STARTING", "RUNNING", "STOPPING"):
            return True
        if status["status"] in ("STOPPED", "CRASHED"):
            return False
    except Exception:
        pass
    if not os.path.exists(STATE_FILE):
        return False
    try:
        age = time.time() - os.path.getmtime(STATE_FILE)
    except OSError:
        return False
    loop_interval = PUMP_CONFIG.get("LOOP_INTERVAL_SECONDS", 15)
    return age <= max(60.0, loop_interval * 5)


@app.route("/api/manual/close", methods=["POST"])
def api_manual_close():
    if not compare_digest(request.headers.get("X-Admin-Token", ""), _ADMIN_TOKEN):
        return jsonify({"error": "Token admin tidak valid atau tidak ada."}), 403

    now = time.time()
    with _manual_close_lock:
        if now - _last_manual_close_request["ts"] < _MANUAL_CLOSE_COOLDOWN_SECONDS:
            return (
                jsonify(
                    {
                        "error": "Tunggu sebentar, permintaan sebelumnya baru saja dikirim."
                    }
                ),
                429,
            )
        _last_manual_close_request["ts"] = now

    def _release_cooldown() -> None:
        with _manual_close_lock:
            if _last_manual_close_request["ts"] == now:
                _last_manual_close_request["ts"] = 0.0

    state = load_state()
    symbol = state.get("current_symbol")
    qty = float(state.get("qty", 0) or 0)
    if not symbol or qty <= 0:
        _release_cooldown()
        return (
            jsonify({"error": "Tidak ada posisi terbuka saat ini untuk dijual."}),
            400,
        )

    data = request.get_json(force=True, silent=True) or {}
    confirm_symbol = str(data.get("symbol", "")).strip().upper()
    if confirm_symbol and confirm_symbol != symbol:
        _release_cooldown()
        return (
            jsonify(
                {
                    "error": f"Simbol tidak cocok (diminta {confirm_symbol}, posisi saat ini {symbol}). "
                    "Muat ulang dashboard dan coba lagi."
                }
            ),
            409,
        )

    state_mod.save_control(
        CONTROL_FILE,
        {
            "action": "CLOSE_POSITION",
            "symbol": symbol,
            "requested_at": int(now * 1000),
        },
    )

    bot_alive = _bot_looks_alive()
    loop_interval = PUMP_CONFIG.get("LOOP_INTERVAL_SECONDS", 15)
    return jsonify(
        {
            "ok": True,
            "symbol": symbol,
            "bot_alive": bot_alive,
            "message": (
                f"Perintah jual untuk {symbol} terkirim. Bot akan memprosesnya dalam "
                f"maksimal {loop_interval} detik pada iterasi berikutnya."
                if bot_alive
                else f"Perintah jual untuk {symbol} tersimpan, TAPI proses bot sepertinya "
                "sedang TIDAK berjalan (file state tidak diperbarui baru-baru ini). "
                "Perintah baru akan dieksekusi setelah bot dijalankan lagi, dan akan "
                "diabaikan otomatis kalau sudah lebih dari 2 menit."
            ),
        }
    )


@app.route("/api/manual/close/status")
def api_manual_close_status():
    pending = state_mod.load_control(CONTROL_FILE)
    state = load_state()
    return jsonify(
        {
            "pending": bool(pending),
            "pending_symbol": pending.get("symbol") if pending else None,
            "has_position": bool(
                state.get("current_symbol") and float(state.get("qty", 0) or 0) > 0
            ),
            "current_symbol": state.get("current_symbol"),
            "bot_alive": _bot_looks_alive(),
        }
    )


@app.route("/api/all")
def api_all():
    trades, events, level_count = parse_log()
    return jsonify(
        {
            "status": build_status(),
            "summary": build_trade_summary(trades),
            "trades": trades[:100],
            "events": events,
            "log_levels": level_count,
        }
    )


@contextmanager
def _drawdown_maintenance():
    with _control_operation_lock:
        if _active_control_operation() or CONFIG_LOAD_ERRORS:
            raise strategy_dd.DrawdownError("Operasi bot masih aktif atau konfigurasi belum valid.")
        with _process_manager._operation_lock:
            status = _process_manager.status(get_mode(PUMP_CONFIG))
            if status.get("status") != "STOPPED" or _process_manager._auto_restart_mode:
                raise strategy_dd.DrawdownError("Hentikan bot secara normal sebelum mengatur periode strategi.")
            if not _process_manager.mode_guard(get_mode(PUMP_CONFIG)).get("safe"):
                raise strategy_dd.DrawdownError("Konteks mode lain belum aman untuk pemeliharaan.")
            reservation = BotModeLock(get_mode(PUMP_CONFIG), global_scope=True)
            reservation.acquire()
            try:
                yield
            finally:
                reservation.release()


def _strategy_initial_balance():
    client = get_client()
    if client is None:
        raise strategy_dd.DrawdownError("Client akun belum tersedia.")
    if is_paper(PUMP_CONFIG):
        path = PUMP_CONFIG.get("PAPER_ACCOUNT_STATE_FILE")
        if not path:
            raise strategy_dd.DrawdownError("File akun PAPER tidak tersedia.")
        with open(path, "rb") as handle:
            account_file = strategy_dd.strict_json(handle.read())
        if not isinstance(account_file, dict) or account_file.get("open_orders") != []:
            raise strategy_dd.DrawdownError("Akun PAPER masih memiliki order atau datanya tidak valid.")
        reader = client
    else:
        orders = client.get_open_orders()
        if orders != []:
            raise strategy_dd.DrawdownError("Akun Spot masih memiliki order terbuka atau respons order tidak valid.")
        reader = SpotEquityReader(client)
    snapshot = collect_equity(reader, PUMP_CONFIG, {}, source="DASHBOARD")
    if snapshot["status"] != "VALID":
        raise strategy_dd.DrawdownError("Saldo belum terverifikasi. Tidak dapat mengusulkan modal awal.")
    if int(time.time() * 1000) - snapshot["captured_at_ms"] > 30000:
        raise strategy_dd.DrawdownError("Pemeriksaan saldo terlalu lama. Ulangi pemeriksaan.")
    return snapshot["quote_free"]


@app.route("/api/strategy-dd/prepare", methods=["POST"])
def api_strategy_dd_prepare():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get("acknowledge") is not True:
        return jsonify({"error": "Konfirmasikan bahwa periode baru hanya mengubah statistik strategi."}), 400
    try:
        capital = strategy_dd.amount(data.get("initial_capital"), positive=True)
        with _drawdown_maintenance():
            inspection = strategy_dd.inspect_initialization(PUMP_CONFIG)
            if inspection["period_id"] is None:
                raise strategy_dd.DrawdownError("Periode pertama dibuat otomatis oleh bot sebelum entry. Tidak perlu menetapkan modal awal melalui dashboard.")
            free = _strategy_initial_balance()
            if capital > strategy_dd.amount(free):
                raise strategy_dd.DrawdownError("Modal awal tidak boleh melebihi saldo quote bebas yang terverifikasi.")
            token = _make_confirmation("strategy-dd", {**inspection, "initial_capital": str(capital)}, ttl=120)
        return jsonify({"confirmation_id": token, "initial_capital": str(capital), "quote": QUOTE, "previous_period_id": inspection["period_id"], "expires_in": 120})
    except (strategy_dd.DrawdownError, BotControlError, BotAlreadyRunningError, OSError, ValueError, TypeError) as exc:
        text = str(exc) if isinstance(exc, strategy_dd.DrawdownError) else "Pemeriksaan periode gagal: " + type(exc).__name__
        return jsonify({"error": text}), 409
    except Exception as exc:
        logger.warning("Pemeriksaan DD strategi gagal: %s", type(exc).__name__)
        return jsonify({"error": "Pemeriksaan akun gagal. Tidak ada konfirmasi sukses; periksa status periode sebelum mencoba lagi."}), 503


@app.route("/api/strategy-dd/execute", methods=["POST"])
def api_strategy_dd_execute():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get("confirmed") is not True:
        return jsonify({"error": "Konfirmasi akhir diperlukan."}), 400
    token = data.get("confirmation_id")
    if not isinstance(token, str):
        return jsonify({"error": "ID konfirmasi tidak valid."}), 400
    payload = _take_confirmation(token, "strategy-dd")
    if not payload:
        return jsonify({"error": "Konfirmasi tidak valid, sudah dipakai, atau kedaluwarsa."}), 409
    try:
        if payload["context"] != strategy_dd.context(PUMP_CONFIG):
            raise strategy_dd.DrawdownError("Konteks akun berubah. Ulangi konfirmasi.")
        with _drawdown_maintenance():
            free = _strategy_initial_balance()
            if strategy_dd.amount(payload["initial_capital"]) > strategy_dd.amount(free):
                raise strategy_dd.DrawdownError("Saldo bebas berubah dan tidak mencukupi modal yang dikonfirmasi.")
            period = strategy_dd.initialize(PUMP_CONFIG, payload["initial_capital"], payload["fingerprint"])
        return jsonify({"period_id": period["period_id"], "started_at_ms": period["started_at_ms"], "message": "Periode strategi dimulai. Kontrol risiko akun dan ledger tidak direset."})
    except (strategy_dd.DrawdownError, BotControlError, BotAlreadyRunningError, OSError, ValueError, TypeError) as exc:
        text = str(exc) if isinstance(exc, strategy_dd.DrawdownError) else "Periode belum dapat dibuat: " + type(exc).__name__
        return jsonify({"error": text}), 409
    except Exception as exc:
        logger.warning("Pemeriksaan DD strategi gagal: %s", type(exc).__name__)
        return jsonify({"error": "Pemeriksaan akun gagal. Tidak ada konfirmasi sukses; periksa status periode sebelum mencoba lagi."}), 503


@app.route("/api/control/status")
def api_control_status():
    mode = get_mode(PUMP_CONFIG)
    process = _process_manager.status(mode)
    guard = _process_manager.mode_guard(mode)
    return jsonify(
        {
            "process": process,
            "position": _process_manager.position(),
            "public_egress_ip": get_public_egress_ip_status(),
            "operation": _control_operation_snapshot(),
            "mode": mode,
            "mode_source": get_mode_source(),
            "mode_guard": guard,
            "live_state_missing": (
                mode == "LIVE"
                and not os.path.exists(
                    PUMP_CONFIG.get("STATE_FILE") or get_state_file()
                )
            ),
            "read_only": not _bind_is_loopback(),
            "config_errors": list(CONFIG_LOAD_ERRORS),
            "platform": {
                "os_name": os.name,
                "windows": os.name == "nt",
                "file_descriptors": fd_status(),
            },
        }
    )


@app.route("/api/control/prepare", methods=["POST"])
def api_control_prepare():
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "")).strip().upper()
    if action not in ("START", "STOP"):
        return jsonify({"error": "Aksi hanya boleh START atau STOP."}), 400
    mode = get_mode(PUMP_CONFIG)
    live_state_missing = (
        action == "START"
        and mode == "LIVE"
        and not os.path.exists(PUMP_CONFIG.get("STATE_FILE") or get_state_file())
    )
    acknowledge_missing_live_state = (
        data.get("acknowledge_missing_live_state") is True
    )
    if live_state_missing and not acknowledge_missing_live_state:
        return (
            jsonify(
                {
                    "error": (
                        "File state LIVE tidak ditemukan. Periksa posisi dan order "
                        "akun, lalu centang konfirmasi inisialisasi state baru."
                    ),
                    "requires_live_state_acknowledgement": True,
                }
            ),
            409,
        )
    active_operation = _active_control_operation()
    if active_operation:
        return (
            jsonify(
                {
                    "error": "Aksi bot lain masih diproses. Tunggu sampai selesai.",
                    "operation": active_operation,
                }
            ),
            409,
        )
    status = _process_manager.status(get_mode(PUMP_CONFIG))
    position = _process_manager.position()
    active_statuses = ("STARTING", "RUNNING", "STOPPING")
    if action == "START" and CONFIG_LOAD_ERRORS:
        return (
            jsonify(
                {
                    "error": "Konfigurasi mode tidak valid: "
                    + "; ".join(CONFIG_LOAD_ERRORS)
                }
            ),
            409,
        )
    if action == "START" and status["status"] not in ("STOPPED", "CRASHED"):
        return (
            jsonify(
                {"error": f"Resume Bot ditolak karena bot sedang {status['status']}."}
            ),
            409,
        )
    if action == "START":
        guard = _process_manager.mode_guard(get_mode(PUMP_CONFIG))
        if not guard["safe"]:
            return (
                jsonify(
                    {
                        "error": "Resume Bot ditolak demi keamanan antar mode. "
                        + " ".join(guard["reasons"]),
                        "mode_guard": guard,
                    }
                ),
                409,
            )
    if action == "STOP" and status["status"] not in active_statuses:
        return (
            jsonify(
                {"error": f"Stop Bot ditolak karena bot sudah {status['status']}."}
            ),
            409,
        )
    policy = str(data.get("position_policy", "REQUIRE_EMPTY")).strip().upper()
    if action == "STOP" and position["has_position"]:
        if policy not in ("SELL_FIRST", "KEEP_OPEN"):
            return (
                jsonify(
                    {
                        "error": "Ada posisi terbuka. Pilih jual dulu atau pertahankan posisi.",
                        "requires_position_choice": True,
                        "position": position,
                    }
                ),
                409,
            )
    else:
        policy = "REQUIRE_EMPTY"
    token = _make_confirmation(
        "process",
        {
            "action": action,
            "position_policy": policy,
            "mode": mode,
            "pid": status.get("pid"),
            "status": status.get("status"),
            "allow_missing_live_state": (
                live_state_missing and acknowledge_missing_live_state
            ),
        },
        ttl=120,
    )
    return jsonify(
        {
            "confirmation_id": token,
            "action": action,
            "position": position,
            "warning": (
                "Posisi akan tetap terbuka tanpa SL/TP bot selama bot berhenti."
                if position["has_position"] and policy == "KEEP_OPEN"
                else None
            ),
        }
    )


@app.route("/api/control/execute", methods=["POST"])
def api_control_execute():
    global _control_operation

    data = request.get_json(silent=True) or {}
    payload = _take_confirmation(data.get("confirmation_id", ""), "process")
    if payload is None:
        return jsonify({"error": "Konfirmasi kedaluwarsa atau tidak valid."}), 409
    if payload.get("mode") != get_mode(PUMP_CONFIG):
        return jsonify({"error": "Mode berubah sejak konfirmasi. Ulangi aksi."}), 409
    if (
        payload.get("action") == "START"
        and get_mode(PUMP_CONFIG) == "LIVE"
        and not os.path.exists(PUMP_CONFIG.get("STATE_FILE") or get_state_file())
        and payload.get("allow_missing_live_state") is not True
    ):
        return (
            jsonify(
                {
                    "error": "File state LIVE hilang setelah konfirmasi. Tinjau ulang "
                    "peringatan dan mulai proses konfirmasi lagi.",
                    "requires_live_state_acknowledgement": True,
                }
            ),
            409,
        )

    with _control_operation_lock:
        if (
            _control_operation
            and _control_operation.get("status") in _CONTROL_ACTIVE_STATUSES
        ):
            return (
                jsonify(
                    {
                        "error": "Aksi bot lain masih diproses. Tunggu sampai selesai.",
                        "operation": deepcopy(_control_operation),
                    }
                ),
                409,
            )

        current = _process_manager.status(get_mode(PUMP_CONFIG))
        action = payload.get("action")
        if action == "START":
            if current["status"] not in ("STOPPED", "CRASHED"):
                return (
                    jsonify(
                        {"error": "Status proses berubah sejak konfirmasi Resume Bot."}
                    ),
                    409,
                )
        elif current["status"] not in (
            "STARTING",
            "RUNNING",
            "STOPPING",
        ) or current.get("pid") != payload.get("pid"):
            return (
                jsonify(
                    {
                        "error": "PID atau status proses berubah sejak konfirmasi. Ulangi aksi."
                    }
                ),
                409,
            )

        ok, left = _cooldown("process", 2.0)
        if not ok:
            return (
                jsonify(
                    {
                        "error": f"Tunggu {left:.1f} detik sebelum aksi proses berikutnya."
                    }
                ),
                429,
            )

        now = time.time()
        operation_id = uuid.uuid4().hex[:16]
        _control_operation = {
            "id": operation_id,
            "action": action,
            "position_policy": payload.get("position_policy", "REQUIRE_EMPTY"),
            "allow_missing_live_state": (
                action == "START"
                and get_mode(PUMP_CONFIG) == "LIVE"
                and payload.get("allow_missing_live_state") is True
            ),
            "status": "PENDING",
            "requested_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "error": None,
            "process": None,
        }
        operation = deepcopy(_control_operation)

    worker = threading.Thread(
        target=_run_control_operation,
        args=(
            operation_id,
            action,
            operation["position_policy"],
            operation["allow_missing_live_state"],
        ),
        name=f"bot-control-{action.lower()}-{operation_id[:6]}",
        daemon=True,
    )
    try:
        worker.start()
    except RuntimeError as exc:
        _update_control_operation(
            operation_id,
            status="FAILED",
            finished_at=time.time(),
            error=f"Worker kontrol gagal dimulai: {exc}",
        )
        return jsonify({"error": "Worker kontrol gagal dimulai."}), 500

    return (
        jsonify(
            {
                "ok": True,
                "accepted": True,
                "operation": _control_operation_snapshot(),
            }
        ),
        202,
    )


def main(*, auto_start_bot: bool = False) -> int:
    if not _bind_is_loopback():
        print(
            "ERROR: DASHBOARD_HOST harus loopback (misalnya 127.0.0.1). "
            "Bind non-loopback ditolak demi melindungi data dashboard."
        )
        return 2

    soft, hard, berubah = raise_fd_limit()
    if berubah:
        print(f"Batas file descriptor dinaikkan menjadi {soft} (hard {hard}).")

    if auto_start_bot:
        try:
            _process_manager.start()
        except (BotControlError, ValueError) as exc:
            print(f"Bot tidak dapat dimulai otomatis: {exc}")

    tampil = "localhost" if _DASHBOARD_HOST == "127.0.0.1" else _DASHBOARD_HOST
    if _ADMIN_TOKEN_SOURCE == "environment":
        print("Token admin diambil dari DASHBOARD_ADMIN_TOKEN di environment.")
    else:
        print(
            "Token admin dibuat acak untuk proses ini dan ikut dirender ke halaman "
            "dashboard. Isi DASHBOARD_ADMIN_TOKEN (minimal 24 karakter) bila token "
            "perlu tetap sama antar restart."
        )
    print(
        f"Dashboard berjalan di http://{tampil}:{_DASHBOARD_PORT}  (Ctrl+C untuk berhenti)"
    )
    from infrastructure.process import runtime_cleanup

    runtime_cleanup.register_dashboard()
    try:
        app.run(
            host=_DASHBOARD_HOST,
            port=_DASHBOARD_PORT,
            debug=False,
            use_reloader=False,
            threaded=True,
        )
    finally:
        _process_manager.shutdown_dashboard()
        runtime_cleanup.unregister_dashboard()
        try:
            laporan = runtime_cleanup.cleanup_runtime_leftovers()
        except Exception as exc:
            laporan = {"deleted": [], "aborted": f"galat: {exc}"}
        if laporan.get("aborted"):
            print(f"Catatan pembersihan runtime: {laporan['aborted']}")
        dihapus = laporan.get("deleted") or []
        if dihapus:
            print(
                f"File runtime dibersihkan ({len(dihapus)}): "
                + ", ".join(str(item) for item in dihapus)
            )
    return 0


if __name__ == "__main__":
    _parser = argparse.ArgumentParser(description="Dashboard bot")
    _parser.add_argument(
        "--selftest",
        action="store_true",
        help=(
            "Jalankan pemeriksaan aturan entry dan exit tanpa menjalankan "
            "server dashboard."
        ),
    )
    _args = _parser.parse_args()
    if _args.selftest:
        from backtesting.entry_selftest import run as _run_entry_selftest
        from backtesting.exit_selftest import run as _run_exit_selftest

        _entry_ok, _ = _run_entry_selftest(verbose=True)
        _exit_ok, _ = _run_exit_selftest(verbose=True)
        raise SystemExit(0 if _entry_ok and _exit_ok else 1)
    raise SystemExit(main(auto_start_bot=False))
