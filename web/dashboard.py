#!/usr/bin/env python3
"""Dashboard pemantauan dan kontrol Pump Scanner Bot Binance Spot.

Dashboard menyediakan Resume Bot dan Stop Bot untuk child process bot.
Kontrol perubahan mode, kredensial, settings, reset PAPER, dan restart manual
tidak disediakan. Mode aktif dibaca dari BOT_MODE di .env saat dashboard
mulai. File state, log, lock, dan lifecycle tetap dipisahkan untuk PAPER dan
LIVE; Resume ditolak jika mode lain berjalan atau masih memiliki posisi.

Keamanan sengaja berlapis: bind default 127.0.0.1, validasi Host dan Origin,
token admin per proses untuk semua request tulis, pembatasan request, dan
mode read-only otomatis jika bind bukan loopback. Dashboard tidak memiliki
login dan tidak ditujukan untuk diekspos ke internet.

Menjalankan ``python dashboard.py`` atau ``python run.py`` membuka dashboard
dengan bot STOPPED. Bot baru berjalan setelah tombol Resume Bot ditekan.
"""

from __future__ import annotations

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
from copy import deepcopy
from datetime import datetime, timezone
from hmac import compare_digest
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request

from infrastructure.paths import PROJECT_ROOT

from config.config import (
    PUMP_CONFIG, CONFIG_LOAD_ERRORS, get_mode, get_mode_source, get_base_url,
    is_paper, backtest_enabled, get_state_file, get_log_file, get_control_file,
    watchlist_enabled,
)
from infrastructure.storage import state as state_mod
from market import market_scanner as scanner
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
            "", "", get_base_url(PUMP_CONFIG), allow_signed=False,
            rate_limit_state_file=PUMP_CONFIG.get("RATE_LIMIT_STATE_FILE"),
            rate_limit_limit=int(PUMP_CONFIG.get("RATE_LIMIT_WEIGHT_LIMIT", 6000) or 6000),
            rate_limit_safety_margin=int(PUMP_CONFIG.get("RATE_LIMIT_SAFETY_MARGIN", 100) or 100),
        )
        self._account_file = PUMP_CONFIG.get("PAPER_ACCOUNT_STATE_FILE",
                                             "data/pump_paper_account_paper.json")

    def get_price(self, symbol, max_retries: int = 3):
        return self._market.get_price(symbol, max_retries=max_retries)

    def get_ticker_24hr_all(self):
        return self._market.get_ticker_24hr_all()

    def get_klines(self, symbol, interval, limit=500, start_time_ms=None, end_time_ms=None):
        return self._market.get_klines(symbol, interval, limit=limit,
                                       start_time_ms=start_time_ms,
                                       end_time_ms=end_time_ms)

    def is_rate_limited(self):
        return self._market.is_rate_limited()

    def weight_headroom(self, limit=None):
        return self._market.weight_headroom(limit)

    def get_account(self):
        from trading.paper.paper_store import load_account_snapshot
        return load_account_snapshot(self._account_file)

from backtesting import backtest as bt
from backtesting import grid_search as gs
from backtesting import portfolio_backtest as pbt

app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"))

STATE_FILE = PUMP_CONFIG.get("STATE_FILE") or get_state_file()
LOG_FILE = PUMP_CONFIG.get("LOG_FILE") or get_log_file()
CONTROL_FILE = PUMP_CONFIG.get("CONTROL_FILE") or get_control_file()
QUOTE = PUMP_CONFIG.get("QUOTE_ASSET", "USDT")

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

# Aksi proses dijalankan di worker agar request HTTP langsung mendapat jawaban.
# STOP dapat menunggu penjualan posisi dan shutdown graceful cukup lama.
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
    return parsed.scheme in ("http", "https") and parsed.netloc.lower() == request.host.lower()


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
    expected = _expected_hosts()
    if app.config.get("TESTING"):
        expected.update({"localhost", "127.0.0.1"})
    if host not in expected:
        return jsonify({"error": "Host dashboard tidak diizinkan."}), 400

    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if not _bind_is_loopback():
            return jsonify({
                "error": "Dashboard terikat ke alamat non-loopback. Semua kontrol tulis dinonaktifkan."
            }), 403
        if not _remote_is_loopback():
            return jsonify({"error": "Request kontrol harus berasal dari alamat loopback."}), 403
        if not _request_origin_is_local():
            return jsonify({"error": "Origin atau Referer tidak valid."}), 403
        now = time.monotonic()
        rate_key = request.remote_addr or "test-client"
        with _rate_lock:
            recent = [item for item in _write_attempts.get(rate_key, []) if now - item < 60.0]
            if len(recent) >= 120:
                _write_attempts[rate_key] = recent
                return jsonify({"error": "Terlalu banyak request tulis. Coba lagi sebentar."}), 429
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
        stale = [key for key, value in _confirmations.items() if value.get("expires", 0) < now]
        for key in stale:
            _confirmations.pop(key, None)
        _confirmations[token] = {"kind": kind, "payload": deepcopy(payload), "expires": now + ttl}
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
    global _control_operation
    with _control_operation_lock:
        if not _control_operation or _control_operation.get("id") != operation_id:
            return False
        _control_operation.update(updates)
        _control_operation["updated_at"] = time.time()
        return True


def _run_control_operation(operation_id: str, action: str,
                           position_policy: str) -> None:
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
_watchlist_cache: dict = {"data": None, "ts": 0, "error": None}

_auto_refresher = None
PRICE_TTL = 5.0
BALANCE_TTL = 30.0

WATCHLIST_TTL = 20.0


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
                        rate_limit_limit=int(PUMP_CONFIG.get("RATE_LIMIT_WEIGHT_LIMIT", 6000) or 6000),
                        rate_limit_safety_margin=int(PUMP_CONFIG.get("RATE_LIMIT_SAFETY_MARGIN", 100) or 100),
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
    r"^(?P<ts>[\d\-]+ [\d:]+).*?BUY FILLED (?P<sym>\w+): qty=(?P<qty>[\d.]+) @ avg (?P<price>[\d.]+)"
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
            t.update({
                "symbol": d["sym"],
                "sell_time": d["ts"],
                "sell_price": float(d["price"]),
                "entry": float(d["entry"]),
                "pnl": float(d["pnl"]),
                "reason": d["reason"],
                "paper": is_paper(PUMP_CONFIG),
            })
            if "buy_price" not in t:
                t["buy_price"] = float(d["entry"])
            t["pnl_pct"] = (t["sell_price"] / t["buy_price"] - 1.0) * 100.0 if t.get("buy_price") else 0.0
            trades.append(t)
            open_pos = None
            continue

    for ln in lines[-120:]:
        ln = ln.rstrip("\n")
        if not ln.strip():
            continue
        lvl = "INFO"
        for l in ("CRITICAL", "ERROR", "WARNING", "INFO"):
            if f"| {l}" in ln:
                lvl = l
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
            "be_active": bool(state.get("be_active")),
            "be_stop_price": float(state.get("be_stop_price", 0) or 0),
            "trailing_active": bool(state.get("trailing_active")),
            "trailing_stop_price": float(state.get("trailing_stop_price", 0) or 0),
        },
        "account": {
            "usdt_free": usdt_free,
            "equity_live": equity_live,
            "peak_equity": state.get("peak_equity"),
            "day_start_equity": state.get("day_start_equity"),
            "day_start_date": state.get("day_start_date"),
        },
        "flags": {
            "dd_stopped": bool(state.get("dd_stopped")),
            "daily_stopped": bool(state.get("daily_stopped")),
        },
        "config": {
            "sl_pct": (
                (state.get("sl_pct") or PUMP_CONFIG.get("SL_PCT"))
                if PUMP_CONFIG.get("USE_STOP_LOSS") else None
            ),
            "tp_pct": state.get("tp_pct") or PUMP_CONFIG.get("TP_PCT"),
            "exit_source": state.get("exit_source") or "FIXED",
            "be_trigger_pct": state.get("be_trigger_pct") or PUMP_CONFIG.get("BE_TRIGGER_PCT"),
            "trail_start_pct": state.get("trail_start_pct") or PUMP_CONFIG.get("TRAILING_START_PCT"),
            "trail_step_pct": state.get("trail_step_pct") or PUMP_CONFIG.get("TRAILING_STEP_PCT"),
            "trailing_start_pct": state.get("trail_start_pct") or PUMP_CONFIG.get("TRAILING_START_PCT"),
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
    return jsonify({
        "error": "Fitur backtest dinonaktifkan saat mode LIVE. "
                 "Untuk mengaktifkannya, set SHOW_BACKTEST_IN_LIVE=True di config.py "
                 "lalu jalankan ulang dashboard.",
        "backtest_disabled": True,
    }), 403


BT_PARAM_KEYS = (
    "USE_ATR_EXIT", "ATR_PERIOD", "ATR_MULT_SL", "ATR_MULT_TP",
    "ATR_MULT_BE_TRIGGER", "ATR_MULT_BE_LOCK", "ATR_MULT_TRAIL_START",
    "ATR_MULT_TRAIL", "SL_PCT", "TP_PCT", "BE_TRIGGER_PCT", "BE_LOCK_PCT",
    "TRAILING_START_PCT", "TRAILING_STEP_PCT",
)

GRID_PARAM_KEYS = tuple(k for k in BT_PARAM_KEYS if k != "USE_ATR_EXIT")

_bt_jobs: dict = {}
_bt_jobs_lock = threading.Lock()
BT_JOB_TTL_SECONDS = 3600


def _bt_cleanup_old_jobs():
    now = time.time()
    with _bt_jobs_lock:
        stale = [jid for jid, j in _bt_jobs.items() if now - j.get("created_at", now) > BT_JOB_TTL_SECONDS]
        for jid in stale:
            _bt_jobs.pop(jid, None)


def _bt_simulation_interval(cfg: dict) -> str:
    """Interval candle simulasi backtest portofolio.

    Harus sama dengan interval konfirmasi bot (CONFIRM_INTERVAL) supaya
    jendela setup yang dievaluasi backtest identik dengan bot; lihat
    pump_scanner_bot (get_klines pakai CONFIRM_INTERVAL) dan backtest CLI
    (backtest.py juga CONFIRM_INTERVAL). MARKET_DATA_INTERVAL hanya mengatur
    monitoring pasar, bukan simulasi.
    """
    return str(cfg.get("CONFIRM_INTERVAL", "5m") or "5m")


def _bt_estimate_requests(days: int, max_symbols: int, cfg: dict) -> int:
    """Perkiraan jumlah request unduh klines: simbol x halaman (1000 bar)."""
    interval = _bt_simulation_interval(cfg)
    bars_per_hari = 1440 // bt.INTERVAL_MINUTES.get(interval, 5)
    halaman = max(1, -(-(days + 1) * bars_per_hari // 1000))
    return max_symbols * halaman


def _bt_prepare_universe(job_id: str, cfg: dict, days: int, max_symbols: int,
                         set_progress, cancelled) -> dict:
    interval = _bt_simulation_interval(cfg)
    bt.bars_per_day(interval)
    bar_ms = bt.INTERVAL_MINUTES[interval] * 60_000
    warmup_ms = bt.MS_PER_DAY + 30 * bar_ms

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * bt.MS_PER_DAY
    fetch_start_ms = start_ms - warmup_ms

    if not _HAS_CLIENT:
        raise bt.BacktestError(
            "Klien Binance tidak tersedia (modul 'requests' tidak termuat). "
            "Backtest butuh akses ke data historis publik Binance."
        )
    client = BinanceSpotClient(
        "", "", PUMP_CONFIG["LIVE_BASE_URL"], allow_signed=False,
        rate_limit_state_file=PUMP_CONFIG.get("RATE_LIMIT_STATE_FILE"),
        rate_limit_limit=int(PUMP_CONFIG.get("RATE_LIMIT_WEIGHT_LIMIT", 6000) or 6000),
        rate_limit_safety_margin=int(PUMP_CONFIG.get("RATE_LIMIT_SAFETY_MARGIN", 100) or 100),
    )

    set_progress(0.01, "mengambil daftar pasar...")
    try:
        tickers = client.get_ticker_24hr_all()
    except Exception as exc:
        raise bt.BacktestError(
            f"Gagal mengambil daftar pasar dari Binance: {exc}"
        ) from exc

    tradable_now = None
    try:
        exchange_info = client.get_exchange_info()
        tradable_now = {
            s.get("symbol") for s in exchange_info.get("symbols", [])
            if s.get("symbol") and s.get("status") == "TRADING"
            and s.get("isSpotTradingAllowed", True)
        }
    except Exception as exc:
        logger.warning("Metadata status pair tidak tersedia untuk portfolio backtest: %s", exc)
    if tradable_now is not None:
        cfg["_historical_tradable_symbols"] = tradable_now
        cfg["_tradable_status_is_current_snapshot"] = True

    universe = pbt.select_universe(tickers, cfg, max_symbols=max_symbols,
                                   tradable_symbols=tradable_now)
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
        set_progress(0.03 + frac * 0.77,
                     f"mengunduh {sym} ({int(frac * len(universe))}/{len(universe)})")

    store = None
    kline_cache = None
    try:
        store = pbt.new_backtest_store(cfg)
        kline_cache = pbt.open_kline_cache(cfg)
        symbols_with_data, failed = pbt.fetch_universe_klines(
            client, universe, interval, fetch_start_ms, end_ms, store,
            progress_cb=dl_progress, cancel_cb=cancelled, cache=kline_cache,
        )
        if not symbols_with_data:
            raise bt.BacktestError(
                "Tidak ada satu pun simbol yang berhasil diunduh datanya. "
                "Periksa koneksi ke Binance."
            )

        set_progress(0.80, "mengunduh volume harian untuk gerbang pump...")
        pbt.fetch_universe_daily_klines(
            client, symbols_with_data,
            fetch_start_ms - 8 * bt.MS_PER_DAY, end_ms, store,
            progress_cb=lambda frac, sym: set_progress(
                0.80 + frac * 0.02, f"volume harian {sym}"),
            cancel_cb=cancelled,
        )
    except Exception:
        if store is not None:
            store.cleanup()
        if kline_cache is not None:
            kline_cache.close()
        raise

    return {
        "store": store,
        "kline_cache": kline_cache,
        "universe": universe,
        "symbols_with_data": symbols_with_data,
        "failed": failed,
        "interval": interval,
        "warmup_ms": warmup_ms,
        "end_ms": end_ms,
    }


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

    try:
        cfg = bt.apply_overrides(PUMP_CONFIG, overrides)
        bt.validate_params(cfg)

        prep = _bt_prepare_universe(job_id, cfg, days, max_symbols,
                                    set_progress, cancelled)
        store = prep["store"]
        kline_cache = prep["kline_cache"]

        set_progress(0.82, "menjalankan simulasi portofolio...")
        result = pbt.run_portfolio_backtest(
            store, cfg, prep["interval"], warmup_ms=prep["warmup_ms"],
            progress_cb=lambda f: set_progress(0.82 + f * 0.17,
                                               "menjalankan simulasi portofolio..."),
            cancel_cb=cancelled,
        )
        summary = pbt.summarize_portfolio(result)

        def _ts(ms):
            if not ms:
                return None
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

        trades_out = [{
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
        } for t in result.trades]

        skipped_out = [{
            "time": _ts(s.time),
            "symbol": s.symbol,
            "reason": s.reason,
            "holding": s.holding,
        } for s in result.skipped[:200]]

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
            "start_time": (_ts(result.start_time) or "") + " UTC" if result.start_time else None,
            "end_time": (_ts(result.end_time) or "") + " UTC" if result.end_time else None,
            "params_used": {k: cfg.get(k) for k in BT_PARAM_KEYS},
            "summary": summary,
            "trades": trades_out,
            "skipped": skipped_out,
            "warnings": result.warnings,
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
                "simulasi PAPER, bukan pada harga levelnya.",
                "Exit dievaluasi per-candle " + prep["interval"] + " (bukan tiap "
                + str(PUMP_CONFIG.get("LOOP_INTERVAL_SECONDS", 15)) + " detik seperti bot asli), "
                "dengan urutan prioritas konservatif: STOP_LOSS -> TAKE_PROFIT -> BREAKEVEN -> "
                "TRAILING. Stop Loss dianggap kena lebih dulu kalau ambigu dalam satu candle, "
                "supaya hasil tidak melebih-lebihkan profit.",
                "Entry memodelkan spread (BACKTEST_ENTRY_SPREAD_PCT), slippage "
                "(BACKTEST_SLIPPAGE_PCT), dan jeda eksekusi (BACKTEST_ENTRY_DELAY_BARS). "
                "Fee taker beli dan jual sudah dipotong. Yang belum dimodelkan adalah "
                "kedalaman order book, jadi order besar di koin tipis akan lebih buruk dari ini.",
                "Rem tingkat akun (USE_EQUITY_STOP, USE_DAILY_STOP) dan gerbang eksekusi "
                "live (MAX_SPREAD_PCT, MAX_CHASE_PCT, MIN_SECONDS_BETWEEN_TRADES) tidak "
                "disimulasikan, jadi backtest bisa tampak lebih aktif daripada bot asli. "
                "COOLDOWN_MINUTES_AFTER_CLOSE sudah disimulasikan.",
                "Volume 24 jam direkonstruksi dari penjumlahan quote volume candle, "
                "sehingga bisa sedikit berbeda dari field quoteVolume di ticker.",
            ],
        }

        with _bt_jobs_lock:
            if job_id in _bt_jobs:
                _bt_jobs[job_id].update({
                    "status": "done", "progress": 1.0, "stage": "selesai",
                    "result": payload,
                })
    except bt.BacktestError as exc:
        with _bt_jobs_lock:
            if job_id in _bt_jobs:
                _bt_jobs[job_id].update({"status": "error", "error": str(exc)})
    except Exception as exc:
        with _bt_jobs_lock:
            if job_id in _bt_jobs:
                _bt_jobs[job_id].update({"status": "error", "error": f"Error tak terduga: {exc}"})
    finally:
        if store is not None:
            store.cleanup()
        if kline_cache is not None:
            kline_cache.close()


def _bt_run_grid_job(job_id: str, days: int, max_symbols: int, spec: dict,
                     rasio_latih: float, metrik: str, min_trades: int,
                     total_kombinasi: int):
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

    try:
        cfg = bt.apply_overrides(PUMP_CONFIG, {})
        bt.validate_params(cfg)

        prep = _bt_prepare_universe(job_id, cfg, days, max_symbols,
                                    set_progress, cancelled)
        store = prep["store"]
        kline_cache = prep["kline_cache"]

        set_progress(0.30, f"menjalankan grid search ({total_kombinasi} kombinasi)...")
        hasil = gs.run_portfolio_grid_search(
            store, cfg, prep["interval"], prep["warmup_ms"], spec,
            metrik=metrik, rasio_latih=rasio_latih, min_trades=min_trades,
            progress_cb=lambda f: set_progress(
                0.30 + f * 0.69,
                f"grid search {total_kombinasi} kombinasi "
                f"({int(round(f * total_kombinasi))}/{total_kombinasi})"),
            cancel_cb=cancelled,
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
            "warnings": [],
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
                "Statistik 24 jam DIREKONSTRUKSI dari candle, bukan snapshot "
                "ticker/24hr historis. Volume itulah yang menentukan simbol mana yang "
                "masuk top-N kandidat per bar.",
                "Fill memodelkan spread, slippage, dan fee taker dua sisi. Kedalaman "
                "order book TIDAK dimodelkan, jadi order besar di koin tipis akan "
                "lebih buruk dari hasil di sini.",
                "Rem tingkat akun (USE_EQUITY_STOP, USE_DAILY_STOP) dan gerbang eksekusi "
                "live (MAX_SPREAD_PCT, MAX_CHASE_PCT, MIN_SECONDS_BETWEEN_TRADES) tidak "
                "disimulasikan, jadi hasil grid bisa tampak lebih aktif daripada bot asli. "
                "COOLDOWN_MINUTES_AFTER_CLOSE sudah disimulasikan.",
            ],
        }

        with _bt_jobs_lock:
            if job_id in _bt_jobs:
                _bt_jobs[job_id].update({
                    "status": "done", "progress": 1.0, "stage": "selesai",
                    "result": payload,
                })
    except (bt.BacktestError, gs.GridSearchError) as exc:
        with _bt_jobs_lock:
            if job_id in _bt_jobs:
                _bt_jobs[job_id].update({"status": "error", "error": str(exc)})
    except Exception as exc:
        with _bt_jobs_lock:
            if job_id in _bt_jobs:
                _bt_jobs[job_id].update({"status": "error", "error": f"Error tak terduga: {exc}"})
    finally:
        if store is not None:
            store.cleanup()
        if kline_cache is not None:
            kline_cache.close()


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
        return jsonify({"error": "Jumlah hari minimal 2 (satu hari pertama dipakai warmup statistik 24 jam)."}), 400

    try:
        max_symbols = int(data.get("max_symbols", 150))
    except (TypeError, ValueError):
        return jsonify({"error": "Jumlah simbol tidak valid."}), 400
    if max_symbols < 2:
        return jsonify({"error": "Jumlah simbol minimal 2 (kalau hanya 1, tidak ada persaingan antar-simbol untuk disimulasikan)."}), 400
    if max_symbols > 600:
        return jsonify({"error": "Jumlah simbol maksimal 600."}), 400

    est_requests = _bt_estimate_requests(days, max_symbols, PUMP_CONFIG)
    if est_requests > 20000:
        return jsonify({
            "error": f"Permintaan terlalu besar (perkiraan {est_requests:,} request ke Binance). "
                     f"Kurangi jumlah simbol atau jumlah hari."
        }), 400

    overrides = {k: data.get(k) for k in BT_PARAM_KEYS}
    try:
        cfg_preview = bt.apply_overrides(PUMP_CONFIG, overrides)
        bt.validate_params(cfg_preview)
    except bt.BacktestError as exc:
        return jsonify({"error": str(exc)}), 400

    with _bt_jobs_lock:
        running = sum(1 for j in _bt_jobs.values() if j["status"] == "running")
        if running >= 1:
            return jsonify({"error": "Sudah ada backtest berjalan. Tunggu selesai atau batalkan dulu."}), 429

        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        _bt_jobs[job_id] = {
            "status": "running", "progress": 0.0, "stage": "memulai...",
            "created_at": now, "started_at": now, "updated_at": now,
            "days": days, "max_symbols": max_symbols, "cancel": False,
        }

    thread = threading.Thread(target=_bt_run_job,
                              args=(job_id, days, overrides, max_symbols), daemon=True)
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
        return jsonify({"error": "Jumlah hari minimal 2 (satu hari pertama dipakai warmup statistik 24 jam)."}), 400

    try:
        max_symbols = int(data.get("max_symbols", 150))
    except (TypeError, ValueError):
        return jsonify({"error": "Jumlah simbol tidak valid."}), 400
    if max_symbols < 2 or max_symbols > 600:
        return jsonify({"error": "Jumlah simbol harus di antara 2 dan 600."}), 400

    est_requests = _bt_estimate_requests(days, max_symbols, PUMP_CONFIG)
    if est_requests > 20000:
        return jsonify({
            "error": f"Permintaan terlalu besar (perkiraan {est_requests:,} request ke Binance). "
                     f"Kurangi jumlah simbol atau jumlah hari."
        }), 400

    try:
        rasio_latih = float(data.get("rasio_latih", 0.7))
    except (TypeError, ValueError):
        return jsonify({"error": "Rasio periode latih tidak valid."}), 400
    if not 0.1 <= rasio_latih <= 1.0:
        return jsonify({"error": "Rasio periode latih harus di antara 0.1 dan 1.0."}), 400

    metrik = str(data.get("metrik", "total_return_pct"))
    if metrik not in gs.METRIK_TERSEDIA:
        return jsonify({"error": f"Metrik '{metrik}' tidak dikenal. "
                                 f"Pilihan: {', '.join(gs.METRIK_TERSEDIA)}."}), 400

    try:
        min_trades = int(data.get("min_trades", 5))
    except (TypeError, ValueError):
        return jsonify({"error": "Jumlah trade minimal tidak valid."}), 400
    if min_trades < 1 or min_trades > 1000:
        return jsonify({"error": "Jumlah trade minimal harus di antara 1 dan 1000."}), 400

    spec_raw = data.get("spec")
    if not isinstance(spec_raw, dict) or not spec_raw:
        return jsonify({"error": "Spec grid kosong. Pilih minimal satu parameter "
                                 "beserta nilai rentangnya."}), 400
    pakai_atr = bool(PUMP_CONFIG.get("USE_ATR_EXIT", False))
    spec = {}
    for key, values in spec_raw.items():
        if key not in GRID_PARAM_KEYS:
            return jsonify({"error": f"Parameter '{key}' tidak diperbolehkan untuk grid. "
                                     f"Pilihan: {', '.join(GRID_PARAM_KEYS)}."}), 400
        if pakai_atr and key in gs.KUNCI_PERSEN:
            return jsonify({"error": f"Parameter '{key}' tidak berpengaruh karena exit "
                                     "ATR sedang aktif (USE_ATR_EXIT=true): mesin "
                                     "mengabaikan seluruh parameter persen. Matikan "
                                     "exit ATR di Pengaturan dulu, atau pilih "
                                     "parameter ATR."}), 400
        if not pakai_atr and key in gs.KUNCI_ATR:
            return jsonify({"error": f"Parameter '{key}' tidak berpengaruh karena exit "
                                     "ATR sedang mati (USE_ATR_EXIT=false): mesin "
                                     "mengabaikan seluruh parameter ATR. Aktifkan "
                                     "exit ATR di Pengaturan dulu, atau pilih "
                                     "parameter persen."}), 400
        if not isinstance(values, (list, tuple)) or not values:
            return jsonify({"error": f"Nilai parameter '{key}' harus daftar angka "
                                     f"yang tidak kosong."}), 400
        bersih = []
        for v in values:
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not math.isfinite(float(v)):
                return jsonify({"error": f"Nilai parameter '{key}' harus angka "
                                         f"(dapat: {v!r})."}), 400
            if v not in bersih:
                bersih.append(v)
        if not bersih:
            return jsonify({"error": f"Nilai parameter '{key}' kosong setelah "
                                     f"duplikat dibuang."}), 400
        if len(bersih) > 50:
            return jsonify({"error": f"Parameter '{key}' maksimal 50 nilai."}), 400
        spec[key] = bersih

    total = 1
    for values in spec.values():
        total *= len(values)
    if total > gs.MAX_KOMBINASI_PORTFOLIO:
        return jsonify({"error": f"Grid menghasilkan {total} kombinasi, melebihi batas "
                                 f"{gs.MAX_KOMBINASI_PORTFOLIO} untuk simulasi portofolio. "
                                 f"Persempit rentang atau perbesar langkah."}), 400

    with _bt_jobs_lock:
        running = sum(1 for j in _bt_jobs.values() if j["status"] == "running")
        if running >= 1:
            return jsonify({"error": "Sudah ada backtest/grid berjalan. "
                                     "Tunggu selesai atau batalkan dulu."}), 429

        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        _bt_jobs[job_id] = {
            "kind": "grid", "status": "running", "progress": 0.0,
            "stage": "memulai...", "created_at": now, "started_at": now,
            "updated_at": now, "days": days, "max_symbols": max_symbols,
            "grid": {"metrik": metrik, "rasio_latih": rasio_latih,
                     "min_trades": min_trades, "total_kombinasi": total},
            "cancel": False,
        }

    thread = threading.Thread(
        target=_bt_run_grid_job,
        args=(job_id, days, max_symbols, spec, rasio_latih, metrik,
              min_trades, total),
        daemon=True)
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
            return jsonify({"error": "Job backtest tidak ditemukan (mungkin sudah kedaluwarsa)."}), 404
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
    return jsonify(out)


@app.route("/")
def index():
    return render_template("dashboard.html", admin_token=_ADMIN_TOKEN)


def build_watchlist() -> dict:
    if not watchlist_enabled(PUMP_CONFIG):
        return {"enabled": False, "items": [], "summary": {}, "error": None}

    now = time.time()
    tickers = None
    error = None

    with _cache_lock:
        cached_watchlist = dict(_watchlist_cache)
    if cached_watchlist["data"] is not None and now - cached_watchlist["ts"] < WATCHLIST_TTL:
        tickers = cached_watchlist["data"]
        error = cached_watchlist["error"]
    else:
        client = get_client()
        if client is None:
            error = "Klien Binance tidak tersedia (requests belum terpasang?)."
            tickers = cached_watchlist["data"]
        else:
            try:
                raw = client.get_ticker_24hr_all()
                tickers = {t["symbol"]: t for t in raw if isinstance(t, dict) and "symbol" in t}
                with _cache_lock:
                    _watchlist_cache["data"] = tickers
                    _watchlist_cache["ts"] = now
                    _watchlist_cache["error"] = None
                error = None
            except Exception as exc:
                error = f"Gagal mengambil data pasar: {str(exc)[:120]}"
                with _cache_lock:
                    tickers = _watchlist_cache["data"]
                    _watchlist_cache["error"] = error

    min_vol = float(PUMP_CONFIG.get("MIN_QUOTE_VOLUME_USDT_24H", 0))
    try:
        top_n = max(1, int(PUMP_CONFIG.get("WATCHLIST_TOP_N", 15) or 15))
    except (TypeError, ValueError):
        top_n = 15

    selected: list[tuple[float, float, str, dict]] = []
    for sym, t in (tickers or {}).items():
        if not scanner.is_structurally_allowed_symbol(str(sym), PUMP_CONFIG):
            continue
        try:
            qv = float(t.get("quoteVolume", 0) or 0)
            price = float(t.get("lastPrice", 0) or 0)
            chg = float(t.get("priceChangePercent", 0) or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or qv < min_vol:
            continue
        selected.append((chg, qv, sym, t))
    selected.sort(key=lambda x: (-x[0], -x[1]))
    selected = selected[:top_n]

    items = []
    for _chg, qv, sym, t in selected:
        try:
            price = float(t.get("lastPrice", 0) or 0)
            chg = float(t.get("priceChangePercent", 0) or 0)
            hi = float(t.get("highPrice", 0) or 0)
            lo = float(t.get("lowPrice", 0) or 0)
        except (TypeError, ValueError):
            price = chg = hi = lo = 0.0

        rng = hi - lo
        rpos = ((price - lo) / rng) if rng > 0 else None

        monitoring_score = round(min(100.0, max(0.0, (chg + 100.0) / 2.0)), 1)
        items.append({
            "symbol": sym,
            "monitoring_score": monitoring_score,
            "price": price, "change_24h": chg, "quote_volume_24h": qv,
            "high_24h": hi, "low_24h": lo,
            "trades_24h": int(t.get("count", 0) or 0),
            "pass_volume": True,
            "status": "LIKUID",
            "range_position": round(rpos, 3) if rpos is not None else None,
        })

    items.sort(key=lambda r: (-r["change_24h"], -r["quote_volume_24h"]))

    counts = {}
    for r in items:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    return {
        "enabled": True,
        "items": items,
        "summary": {
            "total": len(items),
            "counts": counts,
            "with_data": len(items),
        },
        "config": _watchlist_config(),
        "error": error,
    }


def _watchlist_config() -> dict:
    return {
        "min_quote_volume_24h": PUMP_CONFIG.get("MIN_QUOTE_VOLUME_USDT_24H"),
        "quote_asset": PUMP_CONFIG.get("QUOTE_ASSET", "USDT"),
        "top_n": PUMP_CONFIG.get("WATCHLIST_TOP_N", 15),
    }


def _bot_has_open_position() -> bool:
    try:
        st = load_state()
    except Exception:
        return True
    if not isinstance(st, dict):
        return True
    try:
        qty = float(st.get("qty", 0) or 0)
    except (TypeError, ValueError):
        return True
    return bool(st.get("current_symbol")) and qty > 0


def start_auto_refresher() -> None:
    pass


@app.route("/api/watchlist")
def api_watchlist():
    return jsonify(build_watchlist())


@app.route("/api/status")
def api_status():
    return jsonify(build_status())


@app.route("/api/trades")
def api_trades():
    trades, _, level_count = parse_log()
    return jsonify({
        "summary": build_trade_summary(trades),
        "trades": trades[:100],
        "log_levels": level_count,
    })


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
            return jsonify({"error": "Tunggu sebentar, permintaan sebelumnya baru saja dikirim."}), 429
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
        return jsonify({"error": "Tidak ada posisi terbuka saat ini untuk dijual."}), 400

    data = request.get_json(force=True, silent=True) or {}
    confirm_symbol = str(data.get("symbol", "")).strip().upper()
    if confirm_symbol and confirm_symbol != symbol:
        _release_cooldown()
        return jsonify({
            "error": f"Simbol tidak cocok (diminta {confirm_symbol}, posisi saat ini {symbol}). "
                     "Muat ulang dashboard dan coba lagi."
        }), 409

    state_mod.save_control(CONTROL_FILE, {
        "action": "CLOSE_POSITION",
        "symbol": symbol,
        "requested_at": int(now * 1000),
    })

    bot_alive = _bot_looks_alive()
    loop_interval = PUMP_CONFIG.get("LOOP_INTERVAL_SECONDS", 15)
    return jsonify({
        "ok": True,
        "symbol": symbol,
        "bot_alive": bot_alive,
        "message": (
            f"Perintah jual untuk {symbol} terkirim. Bot akan memprosesnya dalam "
            f"maksimal {loop_interval} detik pada iterasi berikutnya."
            if bot_alive else
            f"Perintah jual untuk {symbol} tersimpan, TAPI proses bot sepertinya "
            "sedang TIDAK berjalan (file state tidak diperbarui baru-baru ini). "
            "Perintah baru akan dieksekusi setelah bot dijalankan lagi, dan akan "
            "diabaikan otomatis kalau sudah lebih dari 2 menit."
        ),
    })


@app.route("/api/manual/close/status")
def api_manual_close_status():
    pending = state_mod.load_control(CONTROL_FILE)
    state = load_state()
    return jsonify({
        "pending": bool(pending),
        "pending_symbol": pending.get("symbol") if pending else None,
        "has_position": bool(state.get("current_symbol") and float(state.get("qty", 0) or 0) > 0),
        "current_symbol": state.get("current_symbol"),
        "bot_alive": _bot_looks_alive(),
    })


@app.route("/api/all")
def api_all():
    trades, events, level_count = parse_log()
    return jsonify({
        "status": build_status(),
        "summary": build_trade_summary(trades),
        "trades": trades[:100],
        "events": events,
        "log_levels": level_count,
        "watchlist": build_watchlist(),
    })


@app.route("/api/control/status")
def api_control_status():
    mode = get_mode(PUMP_CONFIG)
    process = _process_manager.status(mode)
    guard = _process_manager.mode_guard(mode)
    return jsonify({
        "process": process,
        "position": _process_manager.position(),
        "operation": _control_operation_snapshot(),
        "mode": mode,
        "mode_source": get_mode_source(),
        "mode_guard": guard,
        "read_only": not _bind_is_loopback(),
        "config_errors": list(CONFIG_LOAD_ERRORS),
        "platform": {"os_name": os.name, "windows": os.name == "nt"},
    })


@app.route("/api/control/prepare", methods=["POST"])
def api_control_prepare():
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "")).strip().upper()
    if action not in ("START", "STOP"):
        return jsonify({"error": "Aksi hanya boleh START atau STOP."}), 400
    active_operation = _active_control_operation()
    if active_operation:
        return jsonify({
            "error": "Aksi bot lain masih diproses. Tunggu sampai selesai.",
            "operation": active_operation,
        }), 409
    status = _process_manager.status(get_mode(PUMP_CONFIG))
    position = _process_manager.position()
    active_statuses = ("STARTING", "RUNNING", "STOPPING")
    if action == "START" and CONFIG_LOAD_ERRORS:
        return jsonify({"error": "Konfigurasi mode tidak valid: " + "; ".join(CONFIG_LOAD_ERRORS)}), 409
    if action == "START" and status["status"] not in ("STOPPED", "CRASHED"):
        return jsonify({"error": f"Resume Bot ditolak karena bot sedang {status['status']}."}), 409
    if action == "START":
        guard = _process_manager.mode_guard(get_mode(PUMP_CONFIG))
        if not guard["safe"]:
            return jsonify({
                "error": "Resume Bot ditolak demi keamanan antar mode. " + " ".join(guard["reasons"]),
                "mode_guard": guard,
            }), 409
    if action == "STOP" and status["status"] not in active_statuses:
        return jsonify({"error": f"Stop Bot ditolak karena bot sudah {status['status']}."}), 409
    policy = str(data.get("position_policy", "REQUIRE_EMPTY")).strip().upper()
    if action == "STOP" and position["has_position"]:
        if policy not in ("SELL_FIRST", "KEEP_OPEN"):
            return jsonify({
                "error": "Ada posisi terbuka. Pilih jual dulu atau pertahankan posisi.",
                "requires_position_choice": True,
                "position": position,
            }), 409
    else:
        policy = "REQUIRE_EMPTY"
    token = _make_confirmation("process", {
        "action": action,
        "position_policy": policy,
        "mode": get_mode(PUMP_CONFIG),
        "pid": status.get("pid"),
        "status": status.get("status"),
    }, ttl=120)
    return jsonify({
        "confirmation_id": token,
        "action": action,
        "position": position,
        "warning": (
            "Posisi akan tetap terbuka tanpa SL/TP bot selama bot berhenti."
            if position["has_position"] and policy == "KEEP_OPEN" else None
        ),
    })


@app.route("/api/control/execute", methods=["POST"])
def api_control_execute():
    global _control_operation

    data = request.get_json(silent=True) or {}
    payload = _take_confirmation(data.get("confirmation_id", ""), "process")
    if payload is None:
        return jsonify({"error": "Konfirmasi kedaluwarsa atau tidak valid."}), 409
    if payload.get("mode") != get_mode(PUMP_CONFIG):
        return jsonify({"error": "Mode berubah sejak konfirmasi. Ulangi aksi."}), 409

    # Pemeriksaan dan pembuatan operation harus atomik agar klik ganda tidak
    # pernah meluncurkan dua worker START/STOP.
    with _control_operation_lock:
        if (
            _control_operation
            and _control_operation.get("status") in _CONTROL_ACTIVE_STATUSES
        ):
            return jsonify({
                "error": "Aksi bot lain masih diproses. Tunggu sampai selesai.",
                "operation": deepcopy(_control_operation),
            }), 409

        current = _process_manager.status(get_mode(PUMP_CONFIG))
        action = payload.get("action")
        if action == "START":
            if current["status"] not in ("STOPPED", "CRASHED"):
                return jsonify({
                    "error": "Status proses berubah sejak konfirmasi Resume Bot."
                }), 409
        elif (
            current["status"] not in ("STARTING", "RUNNING", "STOPPING")
            or current.get("pid") != payload.get("pid")
        ):
            return jsonify({
                "error": "PID atau status proses berubah sejak konfirmasi. Ulangi aksi."
            }), 409

        ok, left = _cooldown("process", 2.0)
        if not ok:
            return jsonify({
                "error": f"Tunggu {left:.1f} detik sebelum aksi proses berikutnya."
            }), 429

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

    # Jangan tunggu proses bot selesai di request HTTP. Browser mendapat ACK
    # cepat dan memantau hasil akhirnya melalui /api/control/status.
    return jsonify({
        "ok": True,
        "accepted": True,
        "operation": _control_operation_snapshot(),
    }), 202


def main(*, auto_start_bot: bool = False) -> int:
    start_auto_refresher()

    if not _bind_is_loopback():
        trusted = list(app.config.get("TRUSTED_HOSTS") or [])
        if _DASHBOARD_HOST not in ("0.0.0.0", "::"):
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
    print(f"Dashboard berjalan di http://{tampil}:{_DASHBOARD_PORT}  (Ctrl+C untuk berhenti)")
    try:
        app.run(host=_DASHBOARD_HOST, port=_DASHBOARD_PORT, debug=False,
                use_reloader=False, threaded=True)
    finally:
        _process_manager.shutdown_dashboard()
        if _auto_refresher is not None:
            try:
                _auto_refresher.stop()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(auto_start_bot=False))
