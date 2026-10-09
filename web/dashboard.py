from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import re
import secrets
import sys
import threading
import time
import uuid
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
from infrastructure.storage import state as state_mod
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

# Penyedia kurs rupiah untuk lapisan tampilan. Objek ini tidak pernah
# melempar exception ke pemanggil: kegagalan bursa dikembalikan sebagai
# payload berisi error dan dashboard menyembunyikan elemen rupiah.
_idr_rate_provider = IdrRateProvider(PUMP_CONFIG)


def get_idr_rate(force: bool = False) -> dict:
    """Payload kurs IDR terkini (selalu berbentuk dict, tidak pernah None)."""
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

_ADMIN_TOKEN = secrets.token_urlsafe(32)

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
    host = (request.host or "").lower()
    if _bind_is_loopback():
        expected = _expected_hosts()
        if app.config.get("TESTING"):
            expected.update({"localhost", "127.0.0.1"})
        if host not in expected:
            return jsonify({"error": "Host dashboard tidak diizinkan."}), 400
    # Saat diikat ke alamat non-loopback, dashboard memang dimaksudkan sebagai
    # pantauan baca-saja untuk perangkat lain di jaringan: pemeriksaan Host
    # dilewati, tetapi SEMUA endpoint tulis sudah diblokir terpisah di bawah.

    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if not _bind_is_loopback():
            return (
                jsonify(
                    {
                        "error": "Dashboard terikat ke alamat non-loopback. Semua kontrol tulis dinonaktifkan."
                    }
                ),
                403,
            )
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
    operation_id: str, action: str, position_policy: str
) -> None:
    _update_control_operation(
        operation_id,
        status="RUNNING",
        started_at=time.time(),
    )
    try:
        if action == "START":
            result = _process_manager.start()
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
    """Volume transaksi 24 jam dalam quote asset (USDT), atau None bila tidak tersedia."""
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
        return [], [], {}
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-max_lines:]
    except OSError:
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

    trades.reverse()
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

    balances = get_live_balance()
    usdt_free = None
    equity_live = None
    if balances is not None:
        usdt_free = balances.get(QUOTE, {}).get("free", 0.0)
        equity_live = usdt_free
        if has_position and live_price:
            equity_live += qty * live_price

    hold_minutes = None
    if has_position and state.get("entry_time"):
        hold_minutes = (int(time.time() * 1000) - int(state["entry_time"])) / 60000.0

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "mode": get_mode(PUMP_CONFIG),
        "paper": is_paper(PUMP_CONFIG),
        "backtest_enabled": backtest_enabled(PUMP_CONFIG),
        "live_connected": live_price is not None or balances is not None,
        "bot_alive": process_status["status"] in ("STARTING", "RUNNING", "STOPPING"),
        "process": process_status,
        "config_errors": list(CONFIG_LOAD_ERRORS),
        "read_only": not _bind_is_loopback(),
        "has_api_key": bool(PUMP_CONFIG.get("API_KEY")),
        "quote": QUOTE,
        # Kurs rupiah untuk sub judul tampilan. Angka konversi dihitung di
        # sisi klien dari satu kurs ini agar tidak ada dua sumber kebenaran.
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
        },
        "account": {
            "usdt_free": usdt_free,
            "equity_live": equity_live,
            "peak_equity": state.get("peak_equity"),
            "max_drawdown_pct": float(state.get("max_dd_pct") or 0.0),
            "dd_reset_count": int(state.get("dd_reset_count") or 0),
            "max_drawdown_limit_pct": float(
                PUMP_CONFIG.get("MAX_DRAWDOWN_PERCENT") or 0.0
            ),
        },
        "flags": {
            "dd_stopped": bool(state.get("dd_stopped")),
        },
        "config": {
            "sl_pct": (
                (state.get("sl_pct") or PUMP_CONFIG.get("SL_PCT"))
                if PUMP_CONFIG.get("USE_STOP_LOSS")
                else None
            ),
            "tp_pct": state.get("tp_pct") or PUMP_CONFIG.get("TP_PCT"),
            "exit_source": state.get("exit_source") or "FIXED",
            "be_trigger_pct": state.get("be_trigger_pct")
            or PUMP_CONFIG.get("BE_TRIGGER_PCT"),
            "trail_start_pct": state.get("trail_start_pct")
            or PUMP_CONFIG.get("TRAILING_START_PCT"),
            "trail_step_pct": state.get("trail_step_pct")
            or PUMP_CONFIG.get("TRAILING_STEP_PCT"),
            "trailing_start_pct": state.get("trail_start_pct")
            or PUMP_CONFIG.get("TRAILING_START_PCT"),
            "use_atr_exit": bool(PUMP_CONFIG.get("USE_ATR_EXIT")),
            "atr_mult_sl": PUMP_CONFIG.get("ATR_MULT_SL"),
            "atr_mult_tp": PUMP_CONFIG.get("ATR_MULT_TP"),
        },
    }


def build_trade_summary(trades):
    closed = [t for t in trades if "pnl" in t and t.get("sell_time")]
    total = len(closed)
    wins = [t for t in closed if t.get("pnl", 0) > 0]
    losses = [t for t in closed if t.get("pnl", 0) <= 0]
    total_pnl = sum(t.get("pnl", 0) for t in closed)
    win_rate = (len(wins) / total * 100.0) if total else 0.0
    avg_win = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0
    best = max((t.get("pnl_pct", 0) for t in closed), default=0.0)
    worst = min((t.get("pnl_pct", 0) for t in closed), default=0.0)
    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "best_pct": best,
        "worst_pct": worst,
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
    "SL_PCT",
    "TP_PCT",
    "BE_TRIGGER_PCT",
    "BE_LOCK_PCT",
    "TRAILING_START_PCT",
    "TRAILING_STEP_PCT",
    "TREND_FILTER_ENABLED",
    "TREND_INTERVAL",
    "TREND_EMA_FAST",
    "TREND_EMA_SLOW",
    "TREND_ADX_PERIOD",
    "TREND_ADX_MIN",
    "TREND_LOOKBACK_BARS",
    "DAILY_TREND_FILTER_ENABLED",
    "DAILY_TREND_INTERVAL",
    "DAILY_TREND_EMA_FAST",
    "DAILY_TREND_EMA_SLOW",
    "DAILY_TREND_ADX_PERIOD",
    "DAILY_TREND_ADX_MIN",
    "DAILY_TREND_LOOKBACK_BARS",
)

# Grid parameter SENGAJA hanya berisi parameter exit: sinyal entry (konfirmasi 5m
# dan gerbang trend) dihitung sekali dari candle yang sama, jadi mengubah
# parameter entry di dalam grid tidak akan mengubah hasil apa pun selain
# menyesatkan. Untuk membandingkan setelan trend, jalankan backtest portofolio
# biasa dengan nilai TREND_* yang berbeda.
GRID_PARAM_KEYS = tuple(
    k
    for k in BT_PARAM_KEYS
    if k != "USE_ATR_EXIT" and not k.startswith(("TREND_", "DAILY_TREND_"))
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


def _bt_estimate_requests(days: int, max_symbols: int, cfg: dict) -> int:
    interval = _bt_simulation_interval(cfg)
    bars_per_hari = 1440 // bt.INTERVAL_MINUTES.get(interval, 5)
    # Rentang unduhan = periode uji + warmup gerbang timeframe tinggi, jadi warmup
    # WAJIB ikut dihitung: gerbang demand harian default menambah sekitar 22 hari
    # candle 5m (6336 bar) per simbol, dan tanpa itu estimasi request di layar
    # akan jauh lebih kecil dari kenyataan.
    try:
        warmup_bars = pbt.parity.htf_gate_warmup_bars(cfg, interval)
    except ValueError:
        warmup_bars = 0
    total_bars = (days + 1) * bars_per_hari + warmup_bars
    halaman = max(1, -(-total_bars // 1000))
    return max_symbols * halaman


def _bt_trend_warmup_note(cfg: dict, interval: str) -> str:
    """Kalimat keterangan berapa lama rentang yang habis untuk pemanasan trend."""
    if not bool(cfg.get("TREND_FILTER_ENABLED", False)):
        return (
            "Gerbang trend sedang NONAKTIF, jadi candle trend tidak diambil dan tidak ada "
            "sinyal yang disaring di atas."
        )
    try:
        hari = pbt.parity.trend_warmup_ms(cfg, interval) / float(bt.MS_PER_DAY)
    except ValueError:
        return (
            "Gerbang trend aktif, tetapi interval trend tidak sepadan dengan interval "
            "simulasi sehingga backtest ini akan menolak berjalan."
        )
    return (
        f"Gerbang trend aktif: pemanasan gerbang ini memakai sekitar {hari:.1f} hari pertama "
        f"dari rentang yang diunduh pada interval {interval}, sehingga rentang yang benar-benar "
        "diperdagangkan dimulai setelah pemanasan itu. Pakai rentang hari yang lebih panjang "
        "(misalnya 14 hari ke atas) supaya jumlah trade tidak terlalu tipis, dan bandingkan "
        "penghitung sinyal yang disaring gerbang trend pada ringkasan di atas."
    )


def _bt_demand_harian_note(cfg: dict, interval: str) -> str:
    """Kalimat keterangan warmup gerbang demand harian (kosong kalau nonaktif)."""
    if not bool(cfg.get("DAILY_DEMAND_FILTER_ENABLED", False)):
        return (
            "Gerbang demand harian sedang NONAKTIF, jadi tidak ada candle harian yang "
            "dirangkai dan tidak ada sinyal yang disaring olehnya."
        )
    try:
        hari = pbt.parity.htf_gate_warmup_ms(cfg, interval) / float(bt.MS_PER_DAY)
    except ValueError:
        return (
            "Gerbang demand harian aktif, tetapi intervalnya tidak sepadan dengan "
            "interval simulasi sehingga backtest ini akan menolak berjalan."
        )
    return (
        f"Gerbang demand harian {cfg.get('DAILY_DEMAND_INTERVAL', '1d')} aktif dan "
        "dihitung terpisah dari lapisan H1. Candle hariannya dirangkai dari candle "
        f"{interval} yang diunduh dari Binance, jadi tidak ada permintaan data "
        f"terpisah, tetapi rentang unduhan ditarik sekitar {hari:.1f} hari lebih awal "
        f"sebagai pemanasan ({hari:.1f} hari pertama rentang itu tidak menghasilkan "
        "bar yang bisa ditradingkan). Candle harian yang jamnya bolong sebagian "
        "dibuang di backtest, sedangkan bot live memakai candle harian asli dari "
        "bursa. Lihat penghitung sinyal yang disaring gerbang demand harian pada "
        "ringkasan di atas."
    )


def _bt_tren_harian_note(cfg: dict, interval: str) -> str:
    """Kalimat keterangan warmup gerbang EMA + ADX harian (nonaktif kalau gerbang mati)."""
    if not bool(cfg.get("DAILY_TREND_FILTER_ENABLED", False)):
        return (
            "Gerbang EMA + ADX harian sedang NONAKTIF, jadi tidak ada candle harian yang "
            "dirangkai untuk gerbang ini dan tidak ada sinyal yang disaring olehnya."
        )
    try:
        hari = pbt.parity.daily_trend_warmup_ms(cfg, interval) / float(bt.MS_PER_DAY)
    except ValueError:
        return (
            "Gerbang EMA + ADX harian aktif, tetapi intervalnya tidak sepadan dengan "
            "interval simulasi sehingga backtest ini akan menolak berjalan."
        )
    return (
        f"Gerbang EMA + ADX harian {cfg.get('DAILY_TREND_INTERVAL', '1d')} aktif dan "
        f"butuh sekitar {hari:.1f} hari pemanasan (jendela "
        f"{pbt.parity.daily_trend_window_bars(cfg)} candle harian ditambah satu candle "
        "penyangga). Rentang itu otomatis ikut diunduh, dan batas hari minimal pada form "
        "ikut naik. Lihat penghitung sinyal yang disaring gerbang EMA + ADX harian pada hasil."
    )


def _bt_tren_harian_limitations(cfg: dict, interval: str) -> str:
    """Paragraf batasan untuk gerbang EMA + ADX harian (kosong kalau nonaktif)."""
    if not bool(cfg.get("DAILY_TREND_FILTER_ENABLED", False)):
        return ""
    return (
        " Gerbang EMA + ADX harian (DAILY_TREND_*) SUDAH disimulasikan dengan aturan yang "
        "sama seperti bot live: DAILY_TREND_LOOKBACK_BARS candle harian terakhir yang sudah "
        "tutup pada saat candle sinyal ditutup, lalu close > EMA cepat, EMA cepat > EMA "
        "lambat, dan ADX >= DAILY_TREND_ADX_MIN. Koin yang candle hariannya belum cukup "
        "ditolak, sama seperti di live. Candle harian dirangkai dari candle "
        f"{interval} yang diunduh, jadi tidak ada unduhan tambahan. "
        + _bt_tren_harian_note(cfg, interval)
    )


def _bt_prepare_universe(
    job_id: str, cfg: dict, days: int, max_symbols: int, set_progress, cancelled
) -> dict:
    interval = _bt_simulation_interval(cfg)
    bt.bars_per_day(interval)
    bar_ms = bt.INTERVAL_MINUTES[interval] * 60_000
    warmup_ms = bt.MS_PER_DAY + 30 * bar_ms
    try:
        butuh_gerbang_ms = pbt.parity.htf_gate_warmup_ms(cfg, interval)
    except ValueError as exc:
        raise bt.BacktestError(str(exc)) from exc
    if butuh_gerbang_ms > warmup_ms:
        logger.info(
            "Warmup backtest dinaikkan dari %.1f jam menjadi %.1f jam karena "
            "gerbang trend/demand pada %s, gerbang demand harian %s, dan gerbang EMA + ADX "
            "harian %s butuh paling sedikit %d candle %s sebagai pemanasan.",
            warmup_ms / 3_600_000.0,
            butuh_gerbang_ms / 3_600_000.0,
            cfg.get("TREND_INTERVAL", "1h"),
            cfg.get("DAILY_DEMAND_INTERVAL", "1d"),
            cfg.get("DAILY_TREND_INTERVAL", "1d"),
            pbt.parity.htf_gate_warmup_bars(cfg, interval),
            interval,
        )
        warmup_ms = butuh_gerbang_ms

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * bt.MS_PER_DAY
    fetch_start_ms = start_ms - warmup_ms

    def klien_pembuat(cfg: dict) -> BinanceSpotClient:
        # Satu klien untuk satu pekerjaan backtest. Kalau tidak ditutup, setiap
        # pekerjaan meninggalkan sesi HTTP beserta soket dan file descriptor-nya;
        # tumpukan inilah yang dahulu menghabiskan fd proses.
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
    }


def _json_safe(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _bt_days_guard(cfg: dict, days: int) -> None:
    """Tolak rentang hari yang seluruhnya habis untuk pemanasan.

    Tanpa ini, pengguna memilih 2 hari dengan gerbang trend 1h aktif dan hasilnya
    selalu nol trade tanpa penjelasan.
    """
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
            # Setelan gerbang trend yang BENAR-BENAR dipakai simulasi ini, supaya
            # angka di layar bisa diverifikasi tanpa menebak dari config.py.
            "trend": {
                "enabled": bool(cfg.get("TREND_FILTER_ENABLED", False)),
                "interval": cfg.get("TREND_INTERVAL", "1h"),
                "ema_fast": int(cfg.get("TREND_EMA_FAST", 20) or 20),
                "ema_slow": int(cfg.get("TREND_EMA_SLOW", 50) or 50),
                "adx_period": int(cfg.get("TREND_ADX_PERIOD", 14) or 14),
                "adx_min": float(cfg.get("TREND_ADX_MIN", 0.0) or 0.0),
                "lookback_bars": int(cfg.get("TREND_LOOKBACK_BARS", 120) or 120),
                "skips": int(getattr(result, "trend_skips", 0)),
                "scans": int(getattr(result, "trend_scans", 0)),
            },
            # Setelan gerbang demanda yang BENAR-BENAR dipakai simulasi ini, supaya
            # angka penyaring di layar bisa diverifikasi tanpa menebak dari config.py.
            "htf_demand": {
                "enabled": bool(cfg.get("HTF_DEMAND_FILTER_ENABLED", False)),
                "interval": cfg.get("TREND_INTERVAL", "1h"),
                "lookback_bars": int(cfg.get("HTF_DEMAND_LOOKBACK_BARS", 72) or 72),
                "zone_buffer_pct": float(cfg.get("HTF_DEMAND_ZONE_BUFFER_PCT", 1.5) or 0.0),
                "max_distance_pct": float(cfg.get("HTF_DEMAND_MAX_DISTANCE_PCT", 20.0) or 0.0),
                "min_close_position": float(cfg.get("HTF_DEMAND_MIN_CLOSE_POSITION", 0.4) or 0.0),
                "skips": int(getattr(result, "htf_demand_skips", 0)),
                "scans": int(getattr(result, "htf_demand_scans", 0)),
            },
            "daily_demand": {
                "enabled": bool(cfg.get("DAILY_DEMAND_FILTER_ENABLED", False)),
                "interval": cfg.get("DAILY_DEMAND_INTERVAL", "1d"),
                "lookback_bars": int(cfg.get("DAILY_DEMAND_LOOKBACK_BARS", 20) or 20),
                "zone_buffer_pct": float(cfg.get("DAILY_DEMAND_ZONE_BUFFER_PCT", 2.0) or 0.0),
                "max_distance_pct": float(cfg.get("DAILY_DEMAND_MAX_DISTANCE_PCT", 12.0) or 0.0),
                "min_close_position": float(cfg.get("DAILY_DEMAND_MIN_CLOSE_POSITION", 0.4) or 0.0),
                "skips": int(getattr(result, "daily_demand_skips", 0)),
                "scans": int(getattr(result, "daily_demand_scans", 0)),
            },
            # Setelan gerbang EMA + ADX harian yang BENAR-BENAR dipakai simulasi ini.
            "daily_trend": {
                "enabled": bool(cfg.get("DAILY_TREND_FILTER_ENABLED", False)),
                "interval": cfg.get("DAILY_TREND_INTERVAL", "1d"),
                "ema_fast": int(cfg.get("DAILY_TREND_EMA_FAST", 20) or 20),
                "ema_slow": int(cfg.get("DAILY_TREND_EMA_SLOW", 50) or 50),
                "adx_period": int(cfg.get("DAILY_TREND_ADX_PERIOD", 14) or 14),
                "adx_min": float(cfg.get("DAILY_TREND_ADX_MIN", 0.0) or 0.0),
                "lookback_bars": int(cfg.get("DAILY_TREND_LOOKBACK_BARS", 120) or 120),
                "skips": int(getattr(result, "daily_trend_skips", 0)),
                "scans": int(getattr(result, "daily_trend_scans", 0)),
            },
            "trades": trades_out,
            "skipped": skipped_out,
            "warnings": (
                list(result.warnings)
                + _bt_peringatan_harian(cfg, days)
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
                "sama. Stop Loss dianggap kena lebih dulu kalau ambigu dalam satu candle.",
                "Entry memodelkan spread (BACKTEST_ENTRY_SPREAD_PCT), slippage "
                "(BACKTEST_SLIPPAGE_PCT), dan jeda eksekusi (BACKTEST_ENTRY_DELAY_BARS). "
                "Fee taker beli dan jual sudah dipotong. Kedalaman order book belum "
                "dimodelkan, jadi order besar di koin tipis akan lebih buruk dari ini. "
                "Filter kedalaman, ketimpangan bid/ask, dan sell wall juga hanya berlaku di "
                "PAPER dan LIVE (snapshot order book tidak tersedia historis). Batas atas "
                "kenaikan 24 jam (PUMP_MAX_24H_CHANGE_PCT) SUDAH diterapkan di backtest.",
                "Gerbang trend timeframe tinggi (TREND_FILTER_ENABLED) SUDAH disimulasikan dan "
                "memakai aturan yang sama dengan bot live: TREND_LOOKBACK_BARS candle trend "
                "terakhir yang sudah tutup pada saat candle sinyal ditutup, lalu close > EMA cepat, "
                "EMA cepat > EMA lambat, dan ADX >= TREND_ADX_MIN. Candle trend di backtest "
                "dirangkai dari candle "
                + prep["interval"]
                + " yang diunduh (nilai OHLCV-nya sama "
                "dengan candle timeframe tinggi asli Binance), jadi tidak ada unduhan tambahan. "
                "Bedanya dengan live: candle trend yang jamnya bolong sebagian (data tidak lengkap) "
                "dibuang, sedangkan live memakai candle asli dari bursa; dan riwayat EMA/ADX di "
                "backtest dimulai dari awal rentang data yang diunduh, bukan riwayat penuh simbol. "
                + _bt_trend_warmup_note(cfg, prep["interval"])
                + _bt_demand_limitations(cfg, prep["interval"])
                + _bt_tren_harian_limitations(cfg, prep["interval"])
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
        # Nilai di luar kunci yang disapu grid (termasuk gerbang trend dan interval
        # konfirmasi) mengikuti form backtest supaya gate entry di grid sama dengan
        # yang dijalankan form itu, bukan diam-diam memakai nilai default config.py.
        cfg = bt.apply_overrides(PUMP_CONFIG, overrides or {})
        bt.validate_params(cfg)
        _bt_days_guard(cfg, days)

        prep = _bt_prepare_universe(
            job_id, cfg, days, max_symbols, set_progress, cancelled
        )
        store = prep["store"]
        kline_cache = prep["kline_cache"]
        klien = prep.get("client")

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
                "Yang disapu grid hanya parameter EXIT. Gerbang trend (TREND_*), gerbang "
                "EMA + ADX harian (DAILY_TREND_*), dan USE_ATR_EXIT tidak ikut disapu: nilainya diambil dari form backtest karena "
                "sinyal entry dihitung sekali lalu dipakai ulang oleh semua kombinasi. Untuk "
                "membandingkan setelan trend, jalankan backtest portofolio terpisah per setelan.",
                "Statistik 24 jam DIREKONSTRUKSI dari candle, bukan snapshot "
                "ticker/24hr historis. Volume itulah yang menentukan simbol mana yang "
                "masuk top-N kandidat per bar.",
                "Fill memodelkan spread, slippage, dan fee taker dua sisi. Kedalaman "
                "order book TIDAK dimodelkan, jadi order besar di koin tipis akan "
                "lebih buruk dari hasil di sini. Filter kedalaman, ketimpangan bid/ask, dan "
                "sell wall hanya berlaku di PAPER dan LIVE. Batas atas kenaikan 24 jam "
                "(PUMP_MAX_24H_CHANGE_PCT) SUDAH diterapkan di backtest.",
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
    """Paragraf batasan untuk kedua gerbang zona demand timeframe tinggi.

    Dipisah sebagai fungsi supaya teks yang benar-benar dikirim ke layar bisa
    diuji selftest, bukan disalin ulang di dua tempat.
    """
    return (
        "Gerbang zona demand timeframe tinggi juga SUDAH disimulasikan dengan aturan "
        "yang sama seperti bot live: lapisan H1 (HTF_DEMAND_*, memakai candle "
        + str(cfg.get("TREND_INTERVAL", "1h"))
        + " yang sudah tutup) dan lapisan harian (DAILY_DEMAND_*, candle "
        + str(cfg.get("DAILY_DEMAND_INTERVAL", "1d"))
        + " yang sudah tutup). Keduanya memakai mesin zona yang sama dengan filter "
        "demand M5, hanya parameternya berbeda, keduanya diperiksa SETELAH gerbang "
        "tren, dan keduanya fail closed bila candle tidak cukup. Penghitung "
        "\"disaring X dari Y sinyal\" pada ringkasan hanya menghitung sinyal yang "
        "SUDAH lolos gerbang sebelumnya: angka 0 berarti lapisan itu tidak pernah "
        "diperiksa (karena gerbang di atasnya menolak lebih dulu), bukan berarti "
        "lapisan itu meloloskan semuanya. "
        + _bt_demand_harian_note(cfg, interval)
    )


def _bt_min_days_note(cfg: dict, interval: str) -> tuple[int, str]:
    """Berapa hari minimal supaya backtest masih menyisakan bar yang bisa ditradingkan.

    Statistik 24 jam butuh satu hari. Gerbang timeframe tinggi yang aktif butuh
    riwayatnya sendiri sebagai pemanasan: trend dan demand H1 pada TREND_INTERVAL,
    serta EMA + ADX harian pada DAILY_TREND_INTERVAL. Tanpa rentang tambahan itu,
    simulasi hanya berisi pemanasan dan hasilnya nol trade tanpa penjelasan.
    Kebutuhan yang dipakai adalah yang TERBESAR. Demand harian sengaja TIDAK ikut
    menaikkan batas ini (lihat _bt_catatan_harian_min).
    """
    dasar = 2
    tren_h1_aktif = bool(cfg.get("TREND_FILTER_ENABLED", False)) or bool(
        cfg.get("HTF_DEMAND_FILTER_ENABLED", False)
    )
    tren_harian_aktif = bool(cfg.get("DAILY_TREND_FILTER_ENABLED", False))
    if not tren_h1_aktif and not tren_harian_aktif:
        return dasar, _bt_catatan_harian_min(cfg, interval)

    minimal = dasar
    catatan = ""
    if tren_h1_aktif:
        try:
            hari_warmup = pbt.parity.trend_warmup_ms(cfg, interval) / float(
                bt.MS_PER_DAY
            )
        except ValueError as exc:
            return dasar, str(exc)
        minimal = max(minimal, int(math.ceil(hari_warmup)) + 1)
        catatan = (
            f"Gerbang timeframe tinggi {cfg.get('TREND_INTERVAL', '1h')} butuh sekitar "
            f"{hari_warmup:.1f} hari riwayat sebelum bar pertama bisa dievaluasi."
        )
    if tren_harian_aktif:
        try:
            hari_tren_harian = pbt.parity.daily_trend_warmup_ms(
                cfg, interval
            ) / float(bt.MS_PER_DAY)
        except ValueError as exc:
            return dasar, str(exc)
        minimal = max(minimal, int(math.ceil(hari_tren_harian)) + 1)
        catatan += (
            f" Gerbang EMA + ADX {cfg.get('DAILY_TREND_INTERVAL', '1d')} butuh sekitar "
            f"{hari_tren_harian:.1f} hari riwayat sebelum bar pertama bisa dievaluasi."
        )
    return minimal, catatan.strip() + _bt_catatan_harian_min(cfg, interval)

def _bt_catatan_harian_min(cfg: dict, interval: str) -> str:
    """Catatan tambahan soal kebutuhan hari gerbang demand harian.

    Gerbang demand harian TIDAK menaikkan jumlah hari minimum yang ditolak
    (agar rentang pendek tetap bisa diuji), tetapi angkanya dilaporkan supaya
    pengguna tahu berapa lama pemanasannya dan kapan rentang uji terlalu pendek
    untuk menilai lapisan ini. Peringatannya sendiri menempel di hasil job.
    """
    if not bool(cfg.get("DAILY_DEMAND_FILTER_ENABLED", False)):
        return ""
    try:
        hari_harian = pbt.parity.htf_gate_warmup_ms(cfg, interval) / float(bt.MS_PER_DAY)
    except ValueError as exc:
        return f" Gerbang demand harian: {exc}"
    return (
        f" Gerbang demand {cfg.get('DAILY_DEMAND_INTERVAL', '1d')} sendiri butuh "
        f"sekitar {hari_harian:.1f} hari pemanasan (rentang itu otomatis ikut "
        "diunduh), dan periode uji di bawah "
        f"{pbt.parity.daily_demand_lookback_bars(cfg)} hari ditandai belum cukup "
        "untuk menilai lapisan ini."
    )


def _bt_peringatan_harian(cfg: dict, days: int) -> list:
    """Peringatan rentang uji terlalu pendek untuk gerbang demand harian."""
    if not bool(cfg.get("DAILY_DEMAND_FILTER_ENABLED", False)):
        return []
    lookback = pbt.parity.daily_demand_lookback_bars(cfg)
    if int(days) >= int(lookback):
        return []
    return [
        f"Rentang uji {int(days)} hari lebih pendek dari jendela zona demand "
        f"{cfg.get('DAILY_DEMAND_INTERVAL', '1d')} (lookback {lookback} hari). "
        "Gerbang demand harian tetap dijalankan, tetapi hasilnya belum bisa "
        "dinilai dari rentang sesingkat ini: perpanjang rentang hari atau "
        "kecilkan DAILY_DEMAND_LOOKBACK_BARS."
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

    # Rentang bisa perlu lebih panjang kalau gerbang trend aktif: tanpa pemeriksaan ini
    # pengguna memilih 2 hari dan hasilnya nol trade karena seluruh rentang habis
    # untuk pemanasan.
    try:
        cfg_hari = bt.apply_overrides(
            PUMP_CONFIG, {k: data.get(k) for k in BT_PARAM_KEYS if k in data}
        )
    except bt.BacktestError:
        cfg_hari = (
            PUMP_CONFIG  # galat nilainya dilaporkan di pemeriksaan parameter di bawah
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
    out["mode"] = "portfolio"
    out["min_quote_volume"] = PUMP_CONFIG.get("MIN_QUOTE_VOLUME_USDT_24H", 0)
    out["grid_max_kombinasi"] = gs.MAX_KOMBINASI_PORTFOLIO
    out["grid_metrik"] = list(gs.METRIK_TERSEDIA)
    out["grid_params"] = list(GRID_PARAM_KEYS)
    # Info pemanasan gerbang timeframe tinggi supaya perkiraan beban unduhan di
    # layar (dan catatan tanggal minimal) memakai angka yang SAMA dengan yang
    # benar-benar diunduh job, termasuk lapisan demand dan EMA + ADX harian.
    try:
        out["warmup_bars"] = int(
            pbt.parity.htf_gate_warmup_bars(
                PUMP_CONFIG, _bt_simulation_interval(PUMP_CONFIG)
            )
        )
    except ValueError:
        out["warmup_bars"] = 0
    out["warmup_days"] = round(
        out["warmup_bars"] / float(bt.bars_per_day(_bt_simulation_interval(PUMP_CONFIG))),
        1,
    )
    out["daily_demand_enabled"] = bool(
        PUMP_CONFIG.get("DAILY_DEMAND_FILTER_ENABLED", False)
    )
    out["daily_demand_interval"] = PUMP_CONFIG.get("DAILY_DEMAND_INTERVAL", "1d")
    out["daily_demand_lookback_bars"] = int(
        PUMP_CONFIG.get("DAILY_DEMAND_LOOKBACK_BARS", 20) or 20
    )
    out["daily_trend_enabled"] = bool(
        PUMP_CONFIG.get("DAILY_TREND_FILTER_ENABLED", False)
    )
    out["daily_trend_interval"] = PUMP_CONFIG.get("DAILY_TREND_INTERVAL", "1d")
    out["daily_trend_lookback_bars"] = int(
        PUMP_CONFIG.get("DAILY_TREND_LOOKBACK_BARS", 120) or 120
    )
    return jsonify(out)


@app.route("/favicon.ico")
def favicon():
    # Browser meminta /favicon.ico di akar situs; arahkan ke aset statis.
    return redirect("/static/favicon.ico", code=302)


@app.route("/")
def index():
    return render_template("dashboard.html", admin_token=_ADMIN_TOKEN)


@app.route("/api/status")
def api_status():
    return jsonify(build_status())


@app.route("/api/coin-icon/<symbol>")
def api_coin_icon(symbol):
    # Mengalihkan ke gambar ikon koin. 404 bila tidak ada, sehingga klien
    # bisa lanjut ke sumber cadangan (CDN) atau huruf inisial.
    url = coin_icons.resolve_icon_url(symbol)
    if not url:
        abort(404)
    return redirect(url, code=302)


@app.route("/api/fx")
def api_fx():
    """Kurs rupiah untuk tampilan.

    Parameter opsional ``refresh=1`` memaksa pengambilan kurs baru dari
    bursa. Penyedia kurs sendiri memberi jeda minimum antar permintaan
    paksa supaya endpoint ini tidak bisa dipakai menghajar bursa.
    """
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


@app.route("/api/control/status")
def api_control_status():
    mode = get_mode(PUMP_CONFIG)
    process = _process_manager.status(mode)
    guard = _process_manager.mode_guard(mode)
    return jsonify(
        {
            "process": process,
            "position": _process_manager.position(),
            "operation": _control_operation_snapshot(),
            "mode": mode,
            "mode_source": get_mode_source(),
            "mode_guard": guard,
            "read_only": not _bind_is_loopback(),
            "config_errors": list(CONFIG_LOAD_ERRORS),
            "platform": {
                "os_name": os.name,
                "windows": os.name == "nt",
                # Gauge pemakaian file descriptor. Angka inilah yang dulu jebol
                # sampai OSError(24) dan membuat dashboard menjawab 500.
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
            "mode": get_mode(PUMP_CONFIG),
            "pid": status.get("pid"),
            "status": status.get("status"),
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
        args=(operation_id, action, operation["position_policy"]),
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


def selftest() -> int:
    """Uji lokal dashboard: kunci parameter backtest dan warmup.

    Tidak menghubungi Binance sama sekali. Klien bursa diganti klien palsu.
    """
    print(
        "=== SELFTEST web/dashboard.py: kunci parameter backtest dan gerbang trend ==="
    )
    gagal = 0

    def cek(nama: str, syarat: bool, info: str = "") -> None:
        nonlocal gagal
        if not syarat:
            gagal += 1
        print(
            ("  LULUS " if syarat else "  GAGAL ")
            + nama
            + (f"  -> {info}" if info else "")
        )

    cek(
        "form backtest memuat semua kunci trend",
        {
            "TREND_FILTER_ENABLED",
            "TREND_INTERVAL",
            "TREND_EMA_FAST",
            "TREND_EMA_SLOW",
            "TREND_ADX_PERIOD",
            "TREND_ADX_MIN",
            "TREND_LOOKBACK_BARS",
        }
        <= set(BT_PARAM_KEYS),
    )
    cek(
        "pencarian grid tidak menyapu parameter trend (sinyal entry dihitung sekali)",
        not any("TREND" in k for k in GRID_PARAM_KEYS)
        and "USE_ATR_EXIT" not in GRID_PARAM_KEYS,
    )

    # warmup backtest: dinaikkan sebelum jaringan disentuh, dan galat interval dibungkus rapi
    import logging as _logging

    kelas_log = []

    class Perekam(_logging.Handler):
        def emit(self, record):
            kelas_log.append(record.getMessage())

    perekam = Perekam()
    logger.addHandler(perekam)
    level_asli = logger.level
    logger.setLevel(_logging.INFO)
    asli_klien, asli_ada = globals().get("BinanceSpotClient"), _HAS_CLIENT
    try:
        globals()["_HAS_CLIENT"] = True

        class KlienGagal:
            def __init__(self, *a, **k):
                pass

            def get_ticker_24hr_all(self):
                raise RuntimeError("jaringan uji putus")

        globals()["BinanceSpotClient"] = KlienGagal
        cfg_uji = dict(PUMP_CONFIG, CONFIRM_INTERVAL="5m")
        try:
            _bt_prepare_universe("uji", cfg_uji, 30, 5, lambda *a: None, lambda: False)
            cek(
                "warmup trend memblokir sebelum jaringan?",
                False,
                "tidak melempar apa pun",
            )
        except bt.BacktestError as exc:
            cek(
                "warmup trend dinaikkan sebelum jaringan dan pesannya jelas",
                any(
                    "Warmup backtest dinaikkan" in m and "gerbang trend" in m
                    for m in kelas_log
                )
                and "jaringan uji putus" in str(exc),
                f"{len(kelas_log)} catatan log",
            )

        try:
            _bt_prepare_universe(
                "uji",
                dict(PUMP_CONFIG, CONFIRM_INTERVAL="5m", TREND_INTERVAL="3m"),
                30,
                5,
                lambda *a: None,
                lambda: False,
            )
            cek("interval trend tidak sepadan ditolak", False, "tidak melempar apa pun")
        except bt.BacktestError as exc:
            cek(
                "interval trend tidak sepadan ditolak dengan pesan yang bisa dibaca",
                "TREND_INTERVAL" in str(exc) or "kelipatan" in str(exc),
                str(exc)[:70],
            )
    finally:
        logger.removeHandler(perekam)
        logger.setLevel(level_asli)
        globals()["_HAS_CLIENT"] = asli_ada
        if asli_klien is not None:
            globals()["BinanceSpotClient"] = asli_klien


    # tanggal minimum: rentang hari tidak boleh habis untuk pemanasan saja.
    # Gerbang demand harian dimatikan dulu supaya uji ini murni mengukur kebutuhan
    # gerbang trend; kebutuhan gerbang harian diuji terpisah di bawah.
    cfg_hari = dict(
        PUMP_CONFIG,
        TREND_FILTER_ENABLED=True,
        TREND_INTERVAL="1h",
        TREND_LOOKBACK_BARS=120,
        CONFIRM_INTERVAL="5m",
        HTF_DEMAND_FILTER_ENABLED=False,
        DAILY_DEMAND_FILTER_ENABLED=False,
    )
    minimal, catatan = _bt_min_days_note(cfg_hari, "5m")
    cek(
        "rentang hari minimum dihitung dari kebutuhan pemanasan gerbang trend",
        minimal >= 6 and "hari" in catatan,
        f"minimal {minimal} hari | {catatan[:60]}",
    )
    try:
        _bt_days_guard(cfg_hari, 2)
        cek(
            "rentang 2 hari dengan gerbang trend ditolak",
            False,
            "tidak melempar apa pun",
        )
    except bt.BacktestError as exc:
        cek(
            "rentang 2 hari dengan gerbang trend ditolak dengan pesan jelas",
            "minimal" in str(exc) and "hari" in str(exc),
            str(exc)[:70],
        )
    _bt_days_guard(cfg_hari, minimal)
    _bt_days_guard(dict(cfg_hari, TREND_FILTER_ENABLED=False), 2)
    cek("rentang cukup dan filter nonaktif tetap diloloskan", True)

    # Gerbang demand harian punya jendela terpanjang (default lookback 20 hari
    # pada candle 1d berarti pemanasan sekitar 22 hari simulasi 5m). Pilihan
    # produknya: rentang pendek TIDAK ditolak (biar tetap bisa diuji), tetapi
    # kebutuhannya dilaporkan di catatan dan hasil job memberi peringatan keras.
    cfg_harian_hari = dict(
        cfg_hari, HTF_DEMAND_FILTER_ENABLED=True, DAILY_DEMAND_FILTER_ENABLED=True
    )
    minimal_harian, catatan_harian = _bt_min_days_note(cfg_harian_hari, "5m")
    cek(
        "catatan rentang hari menyebut kebutuhan pemanasan gerbang demand harian",
        "1d" in catatan_harian and "22" in catatan_harian,
        f"minimal {minimal_harian} hari | {catatan_harian[:70]}",
    )
    _bt_days_guard(cfg_harian_hari, 14)
    cek(
        "rentang 14 hari dengan gerbang demand harian tetap boleh dijalankan",
        True,
        f"minimal tetap {minimal_harian} hari",
    )
    peringatan_pendek = _bt_peringatan_harian(cfg_harian_hari, 14)
    peringatan_panjang = _bt_peringatan_harian(cfg_harian_hari, 30)
    cek(
        "rentang uji lebih pendek dari lookback harian memunculkan peringatan",
        len(peringatan_pendek) == 1
        and "lookback 20 hari" in peringatan_pendek[0]
        and not peringatan_panjang,
        (peringatan_pendek[0][:70] if peringatan_pendek else "kosong"),
    )
    cek(
        "tanpa gerbang demand harian tidak ada peringatan apa pun",
        not _bt_peringatan_harian(dict(cfg_harian_hari, DAILY_DEMAND_FILTER_ENABLED=False), 2),
    )
    cek(
        "catatan gerbang demand harian nonaktif menyebut nonaktif",
        "NONAKTIF"
        in _bt_demand_harian_note(
            dict(cfg_hari, DAILY_DEMAND_FILTER_ENABLED=False), "5m"
        ),
    )
    note_limitations = _bt_demand_limitations(
        dict(
            PUMP_CONFIG,
            TREND_FILTER_ENABLED=True,
            TREND_INTERVAL="1h",
            DAILY_DEMAND_FILTER_ENABLED=True,
        ),
        "5m",
    )
    cek(
        "keterangan batasan hasil menyebut kedua lapisan gerbang demand",
        "HTF_DEMAND_" in note_limitations
        and "DAILY_DEMAND_" in note_limitations
        and "1d" in note_limitations
        and _bt_demand_limitations(
            dict(PUMP_CONFIG, DAILY_DEMAND_FILTER_ENABLED=False), "5m"
        ).endswith("olehnya."),
        note_limitations[:60],
    )

    # Estimasi request harus ikut menghitung warmup, bukan hanya periode uji.
    est_tanpa_harian = _bt_estimate_requests(
        30, 10, dict(PUMP_CONFIG, CONFIRM_INTERVAL="5m", DAILY_DEMAND_FILTER_ENABLED=False)
    )
    est_dengan_harian = _bt_estimate_requests(
        30, 10, dict(PUMP_CONFIG, CONFIRM_INTERVAL="5m", DAILY_DEMAND_FILTER_ENABLED=True)
    )
    cek(
        "estimasi request ikut menghitung warmup gerbang demand harian",
        est_dengan_harian > est_tanpa_harian,
        f"{est_tanpa_harian} -> {est_dengan_harian}",
    )

    # --- gerbang EMA + ADX harian (DAILY_TREND_*) ---
    cek(
        "form backtest memuat semua kunci EMA + ADX harian",
        {
            "DAILY_TREND_FILTER_ENABLED",
            "DAILY_TREND_INTERVAL",
            "DAILY_TREND_EMA_FAST",
            "DAILY_TREND_EMA_SLOW",
            "DAILY_TREND_ADX_PERIOD",
            "DAILY_TREND_ADX_MIN",
            "DAILY_TREND_LOOKBACK_BARS",
        }
        <= set(BT_PARAM_KEYS),
    )
    cek(
        "pencarian grid tidak menyapu EMA + ADX harian",
        not any(k.startswith("DAILY_TREND_") for k in GRID_PARAM_KEYS),
    )
    cfg_tren_harian = dict(
        PUMP_CONFIG,
        CONFIRM_INTERVAL="5m",
        TREND_FILTER_ENABLED=False,
        HTF_DEMAND_FILTER_ENABLED=False,
        DAILY_DEMAND_FILTER_ENABLED=False,
        DAILY_TREND_FILTER_ENABLED=True,
        DAILY_TREND_INTERVAL="1d",
    )
    minimal_th, catatan_th = _bt_min_days_note(cfg_tren_harian, "5m")
    cek(
        "batas hari minimal menghitung pemanasan EMA + ADX harian",
        minimal_th >= pbt.parity.daily_trend_window_bars(cfg_tren_harian) + 2
        and "1d" in catatan_th,
        f"minimal {minimal_th} hari | {catatan_th[:70]}",
    )
    try:
        _bt_days_guard(cfg_tren_harian, minimal_th - 1)
        cek(
            "rentang di bawah minimal dengan EMA + ADX harian ditolak",
            False,
            "tidak melempar apa pun",
        )
    except bt.BacktestError as exc:
        cek(
            "rentang di bawah minimal dengan EMA + ADX harian ditolak dengan pesan jelas",
            "minimal" in str(exc) and "1d" in str(exc),
            str(exc)[:70],
        )
    _bt_days_guard(cfg_tren_harian, minimal_th)
    cek(
        "rentang sebatas minimal dengan EMA + ADX harian diloloskan",
        True,
        f"minimal {minimal_th} hari",
    )
    cek(
        "catatan EMA + ADX harian nonaktif menyebut NONAKTIF",
        "NONAKTIF"
        in _bt_tren_harian_note(
            dict(cfg_tren_harian, DAILY_TREND_FILTER_ENABLED=False), "5m"
        ),
    )
    cek(
        "catatan EMA + ADX harian aktif menyebut pemanasan",
        "pemanasan" in _bt_tren_harian_note(cfg_tren_harian, "5m"),
    )
    cek(
        "gerbang EMA + ADX harian nonaktif tidak menambah batas hari",
        _bt_min_days_note(
            dict(cfg_tren_harian, DAILY_TREND_FILTER_ENABLED=False), "5m"
        )[0]
        == 2,
    )
    batasan_th = _bt_tren_harian_limitations(cfg_tren_harian, "5m")
    cek(
        "keterangan batasan hasil menyebut DAILY_TREND_ saat aktif dan kosong saat nonaktif",
        "DAILY_TREND_" in batasan_th
        and _bt_tren_harian_limitations(
            dict(cfg_tren_harian, DAILY_TREND_FILTER_ENABLED=False), "5m"
        )
        == "",
        batasan_th[:60],
    )
    est_tanpa_th = _bt_estimate_requests(
        30, 10, dict(cfg_tren_harian, DAILY_TREND_FILTER_ENABLED=False)
    )
    est_dengan_th = _bt_estimate_requests(30, 10, cfg_tren_harian)
    cek(
        "estimasi request ikut menghitung pemanasan EMA + ADX harian",
        est_dengan_th > est_tanpa_th,
        f"{est_tanpa_th} -> {est_dengan_th}",
    )

    # --- peta rute dan smoke test HTTP ---
    # Penjaga kelas bug yang pernah lolos ke pengguna: fungsi bantu disisipkan tepat
    # di antara decorator @app.route dan fungsi view, sehingga rute menunjuk ke
    # fungsi yang salah dan browser menerima halaman HTML 500 ("Unexpected token '<'")
    # alih-alih JSON.
    import inspect as _inspect

    def _view(aturan_akhir: str):
        for aturan in app.url_map.iter_rules():
            if str(aturan) == aturan_akhir:
                return app.view_functions[aturan.endpoint], aturan.endpoint
        return None, None

    for jalur, diharapkan in (
        ("/api/backtest/start", "api_backtest_start"),
        ("/api/backtest/grid/start", "api_backtest_grid_start"),
        ("/api/backtest/cancel/<job_id>", "api_backtest_cancel"),
        ("/api/backtest/status/<job_id>", "api_backtest_status"),
        ("/api/backtest/defaults", "api_backtest_defaults"),
    ):
        view, endpoint = _view(jalur)
        cek(
            f"rute {jalur} menunjuk fungsi view yang benar",
            view is not None and endpoint == diharapkan,
            f"endpoint={endpoint}",
        )

    salah = [
        aturan.endpoint
        for aturan in app.url_map.iter_rules()
        if aturan.endpoint.startswith("_")
    ]
    cek(
        "tidak ada view yang terpasang pada fungsi bantu (nama diawali garis bawah)",
        not salah,
        salah,
    )

    viewsalah = []
    for aturan in app.url_map.iter_rules():
        view = app.view_functions[aturan.endpoint]
        try:
            wajib = [
                p
                for p in _inspect.signature(view).parameters.values()
                if p.default is _inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
        except (TypeError, ValueError):
            continue
        # Flask mengisi sendiri argumen yang namanya ada di pola URL (mis. <job_id>),
        # jadi yang dicari adalah argumen wajib DI LUAR pola itu.
        wajib = [p for p in wajib if p.name not in (aturan.arguments or set())]
        if wajib:
            viewsalah.append(f"{aturan.endpoint}({', '.join(p.name for p in wajib)})")
    cek(
        "semua view bisa dipanggil Flask tanpa argumen posisi tambahan",
        not viewsalah,
        viewsalah,
    )

    # Diambil lewat globals() supaya penetapan di bawah tidak membuat nama lokal
    # yang menutupi fungsi aslinya.
    asli_run = globals().get("_bt_run_job")
    asli_grid = globals().get("_bt_run_grid_job")
    globals()["_bt_run_job"] = lambda *a, **k: None
    globals()["_bt_run_grid_job"] = lambda *a, **k: None
    app.config["TESTING"] = True
    klien = app.test_client()
    kepala = {"X-Admin-Token": _ADMIN_TOKEN, "Origin": "http://localhost"}
    try:
        with _bt_jobs_lock:
            _bt_jobs.clear()
        resp = klien.post(
            "/api/backtest/start", json={"days": 30, "max_symbols": 150}, headers=kepala
        )
        tipe = str(resp.headers.get("Content-Type", ""))
        isi = resp.get_data(as_text=True)
        cek(
            "POST /api/backtest/start menjawab JSON, bukan HTML",
            resp.status_code == 200
            and tipe.startswith("application/json")
            and '"job_id"' in isi,
            f"{resp.status_code} {tipe[:30]} {isi[:40]}",
        )
        if resp.status_code != 200:
            print("    pesan server:", isi[:160])

        with _bt_jobs_lock:
            _bt_jobs.clear()
        resp2 = klien.post(
            "/api/backtest/start", json={"days": 2, "max_symbols": 150}, headers=kepala
        )
        isi2 = resp2.get_data(as_text=True)
        cek(
            "rentang hari terlalu pendek dijawab JSON 400 yang jelas",
            resp2.status_code == 400
            and str(resp2.headers.get("Content-Type", "")).startswith(
                "application/json"
            )
            and "minimal" in isi2
            and "hari" in isi2,
            f"{resp2.status_code} {isi2[:60]}",
        )

        # Layar backtest memakai info pemanasan dari server supaya perkiraan beban
        # unduhan dan catatan "minimal N hari" memakai angka yang sama dengan job.
        resp_def = klien.get("/api/backtest/defaults", headers=kepala)
        data_def = resp_def.get_json() or {}
        cek(
            "GET /api/backtest/defaults membawa info pemanasan gerbang demand",
            resp_def.status_code == 200
            and int(data_def.get("warmup_bars", 0)) >= 1440
            and float(data_def.get("warmup_days", 0)) >= 5.0
            and "daily_demand_interval" in data_def
            and int(data_def.get("daily_demand_lookback_bars", 0)) == 20,
            f"warmup_bars={data_def.get('warmup_bars')} "
            f"warmup_days={data_def.get('warmup_days')} "
            f"daily={data_def.get('daily_demand_enabled')}",
        )

        with _bt_jobs_lock:
            _bt_jobs.clear()
        resp3 = klien.post(
            "/api/backtest/grid/start",
            json={
                "days": 30,
                "max_symbols": 10,
                "spec": {"TP_PCT": [2.0, 3.0]},
                "USE_ATR_EXIT": False,
            },
            headers=kepala,
        )
        isi3 = resp3.get_data(as_text=True)
        cek(
            "POST /api/backtest/grid/start menjawab JSON, bukan HTML",
            resp3.status_code == 200
            and str(resp3.headers.get("Content-Type", "")).startswith(
                "application/json"
            )
            and '"job_id"' in isi3,
            f"{resp3.status_code} {isi3[:60]}",
        )

        with _bt_jobs_lock:
            _bt_jobs.clear()
        resp3b = klien.post(
            "/api/backtest/grid/start",
            json={
                "days": 2,
                "max_symbols": 10,
                "spec": {"TP_PCT": [2.0]},
                "USE_ATR_EXIT": False,
                "TREND_FILTER_ENABLED": True,
            },
            headers=kepala,
        )
        isi3b = resp3b.get_data(as_text=True)
        cek(
            "grid dengan rentang terlalu pendek dijawab JSON 400 yang jelas",
            resp3b.status_code == 400
            and str(resp3b.headers.get("Content-Type", "")).startswith(
                "application/json"
            )
            and "minimal" in isi3b,
            f"{resp3b.status_code} {isi3b[:70]}",
        )

        with _bt_jobs_lock:
            _bt_jobs.clear()
            _bt_jobs["uji"] = {
                "status": "running",
                "cancel": False,
                "progress": 0.0,
                "stage": "",
                "updated_at": time.time(),
                "created_at": time.time(),
            }
        resp4 = klien.post("/api/backtest/cancel/uji", headers=kepala)
        cek(
            "POST /api/backtest/cancel menjawab JSON",
            str(resp4.headers.get("Content-Type", "")).startswith("application/json"),
            f"{resp4.status_code} {str(resp4.headers.get('Content-Type'))[:30]}",
        )
    finally:
        with _bt_jobs_lock:
            _bt_jobs.clear()
        if asli_run is not None:
            globals()["_bt_run_job"] = asli_run
        if asli_grid is not None:
            globals()["_bt_run_grid_job"] = asli_grid
        app.config["TESTING"] = False

    print(
        "HASIL SELFTEST dashboard: "
        + ("SEMUA LULUS" if not gagal else f"{gagal} GAGAL")
    )
    return 0 if not gagal else 1


def main(*, auto_start_bot: bool = False) -> int:
    # Batas fd bawaan (1024 di banyak distro Linux) terlalu sempit untuk
    # dashboard bertread banyak + bot + lock file. Naikkan dulu, dan hanya
    # berhenti di bawah batas keras sistem; kegagalan di sini tidak fatal.
    soft, hard, berubah = raise_fd_limit()
    if berubah:
        print(f"Batas file descriptor dinaikkan menjadi {soft} (hard {hard}).")

    if not _bind_is_loopback():
        if _DASHBOARD_HOST in ("0.0.0.0", "::"):
            # Mode baca-saja di semua antarmuka: Host apa pun boleh membuka
            # halaman (lapisan Werkzeug), kontrol tulis tetap diblokir total.
            app.config["TRUSTED_HOSTS"] = None
        else:
            trusted = list(app.config.get("TRUSTED_HOSTS") or [])
            trusted.append(_DASHBOARD_HOST)
            app.config["TRUSTED_HOSTS"] = trusted
        print("=" * 68)
        print("PERINGATAN: DASHBOARD_HOST bukan loopback.")
        print("Semua endpoint tulis dinonaktifkan. Dashboard hanya read-only.")
        print("=" * 68)
        auto_start_bot = False

    if auto_start_bot:
        try:
            _process_manager.start()
        except (BotControlError, ValueError) as exc:
            print(f"Bot tidak dapat dimulai otomatis: {exc}")

    tampil = "localhost" if _DASHBOARD_HOST == "127.0.0.1" else _DASHBOARD_HOST
    print(
        f"Dashboard berjalan di http://{tampil}:{_DASHBOARD_PORT}  (Ctrl+C untuk berhenti)"
    )
    from infrastructure.process import runtime_cleanup

    # Registri PID dashboard: dipakai gerbang keamanan pembersihan runtime
    # supaya proses lain tidak menghapus file yang masih dashboard pakai.
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
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    raise SystemExit(main(auto_start_bot=False))
