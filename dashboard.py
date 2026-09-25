#!/usr/bin/env python3
"""Dashboard pemantauan dan kontrol Pump Scanner Bot Binance Spot.

Dashboard dapat memulai, menghentikan, dan me-restart child process bot,
mengelola settings per mode, mengganti PAPER/LIVE, menyimpan kredensial,
menguji endpoint account read-only, serta mereset akun PAPER. File state,
log, settings, lock, dan lifecycle dipisahkan untuk PAPER dan LIVE.

Keamanan sengaja berlapis: bind default 127.0.0.1, validasi Host dan Origin,
token admin per proses untuk semua request tulis, pembatasan request, dan
mode read-only otomatis jika bind bukan loopback. Dashboard tidak memiliki
login dan tidak ditujukan untuk diekspos ke internet.

Menjalankan ``python dashboard.py`` membuka dashboard dengan bot STOPPED.
Menjalankan ``python run.py`` membuka dashboard dan auto-start bot.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from hmac import compare_digest
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request

import config as config_mod
from config import (
    PUMP_CONFIG, CONFIG_LOAD_ERRORS, get_mode, get_base_url, is_paper,
    backtest_enabled, get_state_file, get_log_file, get_control_file,
    get_watchlist, watchlist_enabled, watchlist_auto_enabled,
)
import state as state_mod
import strategy
from atomic_io import replace_with_retry, timestamp_tag
from credential_store import (
    ENV_PATH, credential_status, read_credentials, update_env,
)
from runtime_control import BotControlError, BotProcessManager
from settings_schema import (
    PARAMETER_SCHEMA, audit_change, compute_overrides, dangerous_relaxations,
    diff_values, load_mode_override, public_schema, read_audit,
    save_mode_override, save_runtime_mode, validate_balances,
    validate_candidate,
)

try:
    from binance_client import BinanceAPIError, BinanceSpotClient
    _HAS_CLIENT = True
except Exception:  # pragma: no cover - kalau requests tidak ada, tetap jalan tanpa live
    _HAS_CLIENT = False


class _PaperDashboardClient:
    """Klien READ-ONLY untuk dashboard saat MODE=PAPER.

    Dashboard adalah proses TERPISAH dari bot. Untuk menghindari balapan tulis
    pada file state PAPER, klien ini TIDAK memakai PaperStore/engine (yang
    menulis) dan TIDAK membuka WebSocket kedua. Saldo virtual dibaca langsung
    dari file state (read-only), sedangkan harga/ticker diambil dari REST
    publik keyless (allow_signed=False) -- mustahil menyentuh endpoint signed.
    """

    def __init__(self) -> None:
        self._market = BinanceSpotClient(
            "", "", get_base_url(PUMP_CONFIG), allow_signed=False)
        self._account_file = PUMP_CONFIG.get("PAPER_ACCOUNT_STATE_FILE",
                                             "pump_paper_account_paper.json")

    def get_price(self, symbol, max_retries: int = 3):
        return self._market.get_price(symbol, max_retries=max_retries)

    def get_ticker_24hr_all(self):
        return self._market.get_ticker_24hr_all()

    def get_account(self):
        from paper_store import load_account_snapshot
        return load_account_snapshot(self._account_file)

import backtest as bt
import portfolio_backtest as pbt

try:
    import watchlist_auto as wl_auto
except Exception:  # pragma: no cover - panel tetap jalan dengan daftar statis
    wl_auto = None

app = Flask(__name__)

# Nama file state/log/kontrol sudah otomatis mengandung akhiran mode aktif
# (mis. pump_bot_state_paper.json), dihitung sekali di config.py saat
# di-import. Fallback get_state_file() dst. hanya terpakai kalau kunci config
# hilang, dan tetap mode-aware supaya dashboard tidak diam-diam membaca file
# mode yang salah.
STATE_FILE = PUMP_CONFIG.get("STATE_FILE") or get_state_file()
LOG_FILE = PUMP_CONFIG.get("LOG_FILE") or get_log_file()
CONTROL_FILE = PUMP_CONFIG.get("CONTROL_FILE") or get_control_file()
QUOTE = PUMP_CONFIG.get("QUOTE_ASSET", "USDT")

# Jarak minimum antar-klik tombol "Jual Sekarang" -- mencegah dobel-klik
# tak sengaja menumpuk banyak perintah sekaligus di control file (walau
# toh cuma file terakhir yang dibaca bot, ini juga mencegah spam UI).
_MANUAL_CLOSE_COOLDOWN_SECONDS = 5.0
_last_manual_close_request = {"ts": 0.0}

# Token acak PER PROSES untuk melindungi endpoint POST /api/manual/close dari
# CSRF (perbaikan audit 2026-09-24, temuan S-05). Tanpa ini, situs jahat yang
# kebetulan dibuka di browser yang sama bisa mengirim POST ke
# 127.0.0.1:8080 dan menutup posisi Anda. Token dibuat ulang setiap
# dashboard dinyalakan dan hanya diketahu halaman dashboard itu sendiri
# (disisipkan ke template), lalu wajib dikirim balik lewat header
# X-Admin-Token. Permintaan cross-site dari situs lain tidak membawa token
# ini, sehingga ditolak 403.
_ADMIN_TOKEN = secrets.token_urlsafe(32)

# Dashboard adalah satu-satunya pemilik proses bot yang dimulainya.
_process_manager = BotProcessManager()
_runtime_lock = threading.RLock()
_confirm_lock = threading.RLock()
_confirmations: dict[str, dict] = {}
_rate_lock = threading.RLock()
_last_dangerous_action: dict[str, float] = {}
_write_attempts: dict[str, list[float]] = {}
_credential_test_state: dict = {"fingerprint": None, "tested_at": None, "account": None}

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
    # Bila operator memilih alamat jaringan yang spesifik, alamat itu boleh
    # menampilkan UI read-only. Wildcard 0.0.0.0 tidak pernah dipercaya
    # sebagai Host karena bukan alamat tujuan browser yang nyata.
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
    """Host, origin, token, dan mode read-only untuk semua endpoint."""
    host = (request.host or "").lower()
    expected = _expected_hosts()
    # Flask test_client memakai Host localhost tanpa port secara default.
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
        # Batas global mencakup token salah, sedangkan endpoint berbahaya
        # tetap memiliki cooldown khusus yang lebih ketat.
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


# --- Cache klien & harga supaya tidak spam Binance tiap refresh ---
_client = None
_price_cache: dict = {}          # {symbol: (price, ts)}
_balance_cache: dict = {"data": None, "ts": 0}
_watchlist_cache: dict = {"data": None, "ts": 0, "error": None}
_auto_refresher = None           # diisi start_auto_refresher() saat dashboard start
PRICE_TTL = 5.0                  # detik
BALANCE_TTL = 30.0              # detik

# Watchlist memakai SATU panggilan ticker/24hr untuk SELURUH pasar, bukan
# satu panggilan per simbol. Dengan cache 20 detik, panel ini menambah
# paling banyak 3 request per menit ke Binance berapa pun panjang daftarnya.
# Ini penting karena bot berbagi jatah rate-limit IP yang sama; panel pantau
# tidak boleh sampai mengganggu kemampuan bot menutup posisi tepat waktu.
WATCHLIST_TTL = 20.0            # detik


def _refresh_runtime_globals() -> None:
    """Segarkan path dan cache setelah override atau mode berubah."""
    global STATE_FILE, LOG_FILE, CONTROL_FILE, QUOTE, _client, _auto_refresher
    with _runtime_lock:
        old_client = _client
        if old_client is not None:
            try:
                close = getattr(old_client, "close", None)
                if close:
                    close()
            except Exception:
                pass
        _client = None
        if _auto_refresher is not None:
            try:
                _auto_refresher.stop()
            except Exception:
                pass
            _auto_refresher = None
        STATE_FILE = PUMP_CONFIG.get("STATE_FILE") or get_state_file()
        LOG_FILE = PUMP_CONFIG.get("LOG_FILE") or get_log_file()
        CONTROL_FILE = PUMP_CONFIG.get("CONTROL_FILE") or get_control_file()
        QUOTE = PUMP_CONFIG.get("QUOTE_ASSET", "USDT")
        _price_cache.clear()
        _balance_cache.update({"data": None, "ts": 0})
        _watchlist_cache.update({"data": None, "ts": 0, "error": None})
        start_auto_refresher()


def get_client():
    global _client
    if not _HAS_CLIENT:
        return None
    if _client is None:
        try:
            if is_paper(PUMP_CONFIG):
                # PAPER: klien read-only (saldo virtual dari file, market data
                # REST publik keyless). Tidak pernah menyentuh endpoint signed.
                _client = _PaperDashboardClient()
            else:
                # LIVE: klien bertanda tangan untuk menampilkan saldo asli.
                _client = BinanceSpotClient(
                    PUMP_CONFIG.get("API_KEY", ""),
                    PUMP_CONFIG.get("API_SECRET", ""),
                    get_base_url(PUMP_CONFIG),
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
    """Harga live dengan cache pendek. Return None kalau gagal."""
    if not symbol:
        return None
    now = time.time()
    cached = _price_cache.get(symbol)
    if cached and now - cached[1] < PRICE_TTL:
        return cached[0]
    client = get_client()
    if client is None:
        return None
    try:
        price = client.get_price(symbol)
        _price_cache[symbol] = (price, now)
        return price
    except Exception:
        return cached[0] if cached else None


def get_live_balance():
    """Saldo akun (butuh API key). Return dict {asset: free} atau None."""
    now = time.time()
    if _balance_cache["data"] is not None and now - _balance_cache["ts"] < BALANCE_TTL:
        return _balance_cache["data"]
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
        _balance_cache["data"] = balances
        _balance_cache["ts"] = now
        return balances
    except Exception:
        return _balance_cache["data"]


# --- Parsing log untuk riwayat trade & event ---
_RE_BUY = re.compile(
    r"^(?P<ts>[\d\-]+ [\d:]+).*?BUY FILLED (?P<sym>\w+): qty=(?P<qty>[\d.]+) @ avg (?P<price>[\d.]+)"
    r".*?24h=(?P<pct>[+\-\d.]+)%"
)
_RE_SELL = re.compile(
    r"^(?P<ts>[\d\-]+ [\d:]+).*?SELL FILLED (?P<sym>\w+) \((?P<reason>[^)]+)\): qty=(?P<qty>[\d.]+) @ avg "
    r"(?P<price>[\d.]+) \| entry=(?P<entry>[\d.]+) \| estimasi PnL=(?P<pnl>[+\-\d.]+)"
)


def parse_log(max_lines: int = 4000):
    """Kembalikan (trades, events, level). Trades = pasangan buy/sell yang
    terdeteksi; events = baris log terbaru; level = ringkasan level log."""
    if not os.path.exists(LOG_FILE):
        return [], [], {}
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-max_lines:]
    except OSError:
        return [], [], {}

    trades = []
    open_pos = None  # trade yang belum ditutup
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

    trades.reverse()  # terbaru dulu
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

    now_ms = int(time.time() * 1000)
    cooldown_left = max(0, (int(state.get("cooldown_until", 0) or 0) - now_ms) / 1000.0)

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "mode": get_mode(PUMP_CONFIG),
        "paper": is_paper(PUMP_CONFIG),
        # Dipakai dashboard untuk menyembunyikan tab Backtest. Ini hanya
        # petunjuk untuk UI -- penegakan sebenarnya ada di endpoint
        # /api/backtest/* lewat _reject_if_backtest_disabled().
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
            "cooldown_left_sec": cooldown_left,
        },
        "config": {
            # Kalau ada posisi terbuka, tampilkan level yang BENAR-BENAR
            # berlaku untuk posisi itu (dikunci saat entry, bisa dari ATR),
            # bukan nilai config. Kalau ditampilkan nilai config sementara
            # posisi memakai level ATR yang berbeda, dashboard akan
            # menyesatkan justru saat informasinya paling dibutuhkan.
            "sl_pct": (
                (state.get("sl_pct") or PUMP_CONFIG.get("SL_PCT"))
                if PUMP_CONFIG.get("USE_STOP_LOSS") else None
            ),
            "tp_pct": state.get("tp_pct") or PUMP_CONFIG.get("TP_PCT"),
            "exit_source": state.get("exit_source") or (
                "ATR" if PUMP_CONFIG.get("USE_ATR_EXITS") else "FIXED"),
            "atr_pct_at_entry": state.get("atr_pct_at_entry") or None,
            # BE/Trailing juga mengikuti ATR, jadi tampilkan level yang
            # benar-benar berlaku untuk posisi terbuka, bukan nilai config.
            #
            # CATATAN BUG (diperbaiki): dulu "be_trigger_pct" ditulis dua
            # kali di dict yang sama. Python diam-diam memakai yang
            # TERAKHIR, yaitu nilai config statis, sehingga level ATR yang
            # sebenarnya terkunci pada posisi tidak pernah sampai ke layar.
            # Persis kebalikan dari maksud komentar di atas. Jangan
            # menambahkan kunci dengan nama sama lagi di sini.
            "be_trigger_pct": state.get("be_trigger_pct") or PUMP_CONFIG.get("BE_TRIGGER_PCT"),
            "trail_start_pct": state.get("trail_start_pct") or PUMP_CONFIG.get("TRAILING_START_PCT"),
            "trail_step_pct": state.get("trail_step_pct") or PUMP_CONFIG.get("TRAILING_STEP_PCT"),
            "use_atr_exits": bool(PUMP_CONFIG.get("USE_ATR_EXITS")),
            # Alias lama yang dipakai template. Ikut mengambil nilai posisi
            # supaya angka di layar konsisten dengan kunci di atas.
            "trailing_start_pct": state.get("trail_start_pct") or PUMP_CONFIG.get("TRAILING_START_PCT"),
            "max_hold_minutes": PUMP_CONFIG.get("MAX_HOLD_MINUTES"),
            "setup_invalidation_exit": bool(PUMP_CONFIG.get("SETUP_INVALIDATION_EXIT")),
            "setup_invalidation_price": state.get("setup_invalidation_price") or 0.0,
            "setup_breakout_level": state.get("setup_breakout_level") or 0.0,
            "risk_percent": PUMP_CONFIG.get("RISK_PERCENT"),
            "max_position_usdt": PUMP_CONFIG.get("MAX_POSITION_USDT"),
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


# ======================================================================
# Backtest (uji parameter pada data historis, dijalankan atas permintaan)
# ======================================================================
# Catatan penting: backtest TETAP read-only terhadap bot (tidak menyentuh
# state/posisi bot yang sedang berjalan) -- ia hanya membaca data historis
# candle dari Binance lalu mensimulasikan di memori. Job berjalan di thread
# terpisah karena bisa memakan waktu (mengambil ribuan candle dengan paging).

def _reject_if_backtest_disabled():
    """Penjaga untuk semua endpoint /api/backtest/*.

    Kembalikan response penolakan (tuple Flask) kalau backtest sedang
    dimatikan, atau None kalau boleh lanjut. Dicek ULANG di setiap
    permintaan, bukan sekali saat startup, supaya perubahan config saat
    proses direstart langsung berlaku.

    Penegakan diletakkan di server, bukan cuma menyembunyikan tab di
    dashboard, karena menyembunyikan elemen HTML sama sekali bukan
    pengamanan -- endpoint-nya masih bisa dipanggil langsung dengan curl.
    """
    if backtest_enabled(PUMP_CONFIG):
        return None
    return jsonify({
        "error": "Fitur backtest dinonaktifkan saat mode LIVE. "
                 "Untuk mengaktifkannya, set SHOW_BACKTEST_IN_LIVE=True di config.py "
                 "lalu jalankan ulang dashboard.",
        "backtest_disabled": True,
    }), 403


BT_PARAM_KEYS = (
    "USE_ATR_EXITS",
    "SL_PCT", "TP_PCT", "BE_TRIGGER_PCT", "BE_LOCK_PCT", "TRAILING_START_PCT",
    "TRAILING_STEP_PCT", "MAX_HOLD_MINUTES",
    "SETUP_INVALIDATION_EXIT", "INVALIDATION_ATR_MULT", "MAX_EXTENSION_ATR_MULT",
    "RETEST_ZONE_ATR_MULT", "MAX_BARS_BREAKOUT_TO_RETEST",
    "ATR_PERIOD", "ATR_MULTIPLIER_SL", "ATR_SL_MIN_PCT", "ATR_SL_MAX_PCT", "ATR_TP_RR_RATIO",
    "ATR_BE_TRIGGER_MULT", "ATR_BE_LOCK_MULT", "ATR_TRAILING_START_MULT", "ATR_TRAILING_STEP_MULT",
)

_bt_jobs: dict = {}
_bt_jobs_lock = threading.Lock()
BT_JOB_TTL_SECONDS = 3600  # buang hasil job lama dari memori setelah 1 jam


def _bt_cleanup_old_jobs():
    now = time.time()
    with _bt_jobs_lock:
        stale = [jid for jid, j in _bt_jobs.items() if now - j.get("created_at", now) > BT_JOB_TTL_SECONDS]
        for jid in stale:
            _bt_jobs.pop(jid, None)


def _bt_run_job(job_id: str, days: int, overrides: dict, max_symbols: int):
    """Jalankan backtest PORTOFOLIO di thread terpisah.

    Alurnya meniru bot live: pindai seluruh pasar, ranking per bar, ambil
    satu kandidat terbaik, pegang satu posisi. Lihat portfolio_backtest.py
    untuk penjelasan lengkap dan daftar keterbatasan.
    """
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

    try:
        cfg = bt.apply_overrides(PUMP_CONFIG, overrides)
        bt.validate_params(cfg)

        interval = cfg.get("CONFIRM_INTERVAL", "5m")
        # Validasi interval. Ini WAJIB dipanggil walau hasilnya tidak
        # dipakai: bars_per_day() melempar BacktestError untuk interval
        # yang tidak didukung, sedangkan baris bar_ms di bawah memakai
        # .get(interval, 5) yang diam-diam jatuh ke 5 menit. Tanpa cek
        # ini, CONFIRM_INTERVAL yang salah ketik di config.py (misalnya
        # "7m") akan menghasilkan backtest yang berjalan mulus tetapi
        # seluruh perhitungan waktunya meleset tanpa peringatan.
        bt.bars_per_day(interval)
        bar_ms = bt.INTERVAL_MINUTES[interval] * 60_000
        # Warmup: 24 jam penuh untuk statistik bergulir, ditambah jendela
        # konfirmasi. Tanpa ini bar-bar awal tidak punya pct24h sama sekali.
        warmup_ms = bt.MS_PER_DAY + strategy.confirm_window_bars(cfg) * bar_ms

        end_ms = int(time.time() * 1000)
        start_ms = end_ms - days * bt.MS_PER_DAY
        fetch_start_ms = start_ms - warmup_ms

        # Sumber data backtest (perbaikan audit 2026-09-24, temuan S-04):
        # SELALU endpoint publik produksi (api.binance.com), TIDAK PERNAH
        # Backtest SELALU memakai data historis PRODUKSI publik (bukan sumber
        # lain), supaya kalibrasi parameter benar-benar relevan untuk LIVE.
        # Endpoint market data bersifat publik sehingga tidak butuh API key,
        # dan client ini sengaja dipisah dari client dashboard (yang ikut MODE
        # aktif) serta dibuat keyless (allow_signed=False).
        if not _HAS_CLIENT:
            raise bt.BacktestError(
                "Klien Binance tidak tersedia (modul 'requests' tidak termuat). "
                "Backtest butuh akses ke data historis publik Binance."
            )
        client = BinanceSpotClient("", "", PUMP_CONFIG["LIVE_BASE_URL"], allow_signed=False)

        # --- Tahap 1: tentukan semesta simbol -------------------------
        set_progress(0.01, "mengambil daftar pasar...")
        try:
            tickers = client.get_ticker_24hr_all()
        except Exception as exc:  # noqa: BLE001
            raise bt.BacktestError(
                f"Gagal mengambil daftar pasar dari Binance: {exc}"
            ) from exc

        # Metadata exchange saat ini memberi parity status TRADING dengan
        # scanner live. Binance tidak menyediakan snapshot status historis,
        # sehingga run_portfolio_backtest tetap menandainya sebagai batasan.
        tradable_now = None
        try:
            exchange_info = client.get_exchange_info()
            tradable_now = {
                s.get("symbol") for s in exchange_info.get("symbols", [])
                if s.get("symbol") and s.get("status") == "TRADING"
                and s.get("isSpotTradingAllowed", True)
            }
        except Exception as exc:  # noqa: BLE001
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

        # --- Tahap 2: unduh candle semua simbol -----------------------
        # Bagian terberat, diberi porsi progres paling besar (0.03 - 0.80).
        set_progress(0.03, f"mengunduh data {len(universe)} simbol...")

        def dl_progress(frac, sym):
            set_progress(0.03 + frac * 0.77,
                         f"mengunduh {sym} ({int(frac * len(universe))}/{len(universe)})")

        data, failed = pbt.fetch_universe_klines(
            client, universe, interval, fetch_start_ms, end_ms,
            progress_cb=dl_progress, cancel_cb=cancelled,
        )
        if not data:
            raise bt.BacktestError(
                "Tidak ada satu pun simbol yang berhasil diunduh datanya. "
                "Periksa koneksi ke Binance."
            )

        # --- Tahap 3: simulasi ----------------------------------------
        set_progress(0.82, "menjalankan simulasi portofolio...")
        result = pbt.run_portfolio_backtest(
            data, cfg, interval, warmup_ms=warmup_ms,
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
            "interval": interval,
            "days": days,
            "universe_requested": len(universe),
            "universe_with_data": len(data),
            "symbols_failed": failed[:50],
            "symbols_failed_count": len(failed),
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
                "Exit dievaluasi per-candle " + interval + " (bukan tiap "
                + str(PUMP_CONFIG.get("LOOP_INTERVAL_SECONDS", 15)) + " detik seperti bot asli), "
                "dengan urutan prioritas konservatif: STOP_LOSS -> TAKE_PROFIT -> BREAKEVEN -> "
                "TRAILING -> MAX_HOLD -> SETUP_INVALIDATED. Stop Loss dianggap kena lebih dulu kalau "
                "ambigu dalam satu candle, supaya hasil tidak melebih-lebihkan profit.",
                "Entry dianggap terjadi tepat di harga penutupan candle sinyal. Slippage market "
                "order dan spread belum dimodelkan. Fee taker beli+jual SUDAH dipotong.",
                "Volume 24 jam juga direkonstruksi dari penjumlahan quote volume candle, "
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
    except Exception as exc:  # noqa: BLE001
        with _bt_jobs_lock:
            if job_id in _bt_jobs:
                _bt_jobs[job_id].update({"status": "error", "error": f"Error tak terduga: {exc}"})


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
        # Minimal 2 hari: satu hari penuh habis untuk warmup statistik 24 jam,
        # jadi rentang 1 hari tidak menyisakan bar yang bisa ditradingkan.
        return jsonify({"error": "Jumlah hari minimal 2 (satu hari pertama dipakai warmup statistik 24 jam)."}), 400

    try:
        max_symbols = int(data.get("max_symbols", 150))
    except (TypeError, ValueError):
        return jsonify({"error": "Jumlah simbol tidak valid."}), 400
    if max_symbols < 2:
        return jsonify({"error": "Jumlah simbol minimal 2 (kalau hanya 1, tidak ada persaingan antar-simbol untuk disimulasikan)."}), 400
    if max_symbols > 600:
        return jsonify({"error": "Jumlah simbol maksimal 600."}), 400

    # Perlindungan dari permintaan yang tidak realistis. Setiap simbol butuh
    # sekitar satu request per 1000 candle, jadi beban total tumbuh sebagai
    # perkalian simbol x hari. Batas ini mencegah satu klik tak sengaja
    # memicu puluhan ribu request ke Binance.
    est_requests = max_symbols * max(1, -(-(days + 1) * 288 // 1000))
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

    # Backtest portofolio jauh lebih berat dari versi satu simbol, jadi hanya
    # SATU yang boleh berjalan pada satu waktu. Dua job paralel akan saling
    # berebut jatah rate-limit Binance dan justru memperlambat keduanya.
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

    # Estimasi sisa waktu dihitung dari kecepatan progres NYATA sejauh ini
    # (elapsed / progress * sisa), bukan angka tebakan tetap -- supaya makin
    # akurat semakin lama job berjalan. Sengaja tidak dihitung kalau progress
    # masih sangat kecil (<2%) karena elapsed/progress akan sangat tidak
    # stabil di awal (estimasi bisa melonjak liar).
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
    """Batalkan job yang sedang berjalan.

    Backtest portofolio bisa berjalan belasan menit, jadi pengguna harus
    bisa menghentikannya. Pembatalan bersifat kooperatif: flag diset di
    sini, lalu thread pekerja memeriksanya di sela pengunduhan tiap simbol
    dan tiap 200 bar simulasi. Thread TIDAK dibunuh paksa, karena
    menghentikan thread di tengah request HTTP bisa meninggalkan koneksi
    menggantung.
    """
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
    """Nilai default form backtest, diambil langsung dari config.py yang
    sedang dipakai bot supaya form selalu mencerminkan konfigurasi terkini."""
    blocked = _reject_if_backtest_disabled()
    if blocked is not None:
        return blocked
    out = {k: PUMP_CONFIG.get(k) for k in BT_PARAM_KEYS}
    out["quote_asset"] = QUOTE
    # Default sengaja jauh lebih kecil dari versi satu simbol dulu (90 hari),
    # karena backtest portofolio mengunduh ratusan simbol sekaligus. 30 hari
    # x 150 simbol sudah memberi gambaran yang layak dalam beberapa menit.
    out["default_days"] = 30
    out["default_max_symbols"] = 150
    out["mode"] = "portfolio"
    out["top_n_candidates"] = PUMP_CONFIG.get("TOP_N_CANDIDATES_TO_CONFIRM", 10)
    out["setup_invalidation_exit"] = bool(PUMP_CONFIG.get("SETUP_INVALIDATION_EXIT", False))
    out["confirm_lookback_bars"] = strategy.confirm_window_bars(PUMP_CONFIG)
    out["required_lookback_bars"] = strategy.required_lookback_bars(PUMP_CONFIG)
    out["min_quote_volume"] = PUMP_CONFIG.get("MIN_QUOTE_VOLUME_USDT_24H", 0)
    return jsonify(out)


@app.route("/")
def index():
    return render_template("dashboard.html", admin_token=_ADMIN_TOKEN)


def build_watchlist() -> dict:
    """Data panel watchlist: harga, perubahan 24 jam, volume, status filter.

    PANEL INI SEPENUHNYA READ-ONLY dan tidak memengaruhi bot sama sekali.
    Ia hanya membandingkan kondisi pasar tiap simbol di daftar dengan gerbang
    likuiditas scanner (MIN_QUOTE_VOLUME_USDT_24H), satu-satunya gerbang
    tingkat ticker yang tersisa setelah strategi pindah ke pullback retest.

    Yang TIDAK diperiksa di sini, dan sengaja tidak diklaim:
      - deteksi setup pullback retest (detect_pullback_retest): butuh unduhan
        candle per simbol setiap refresh, yang justru memakan rate-limit yang
        dipakai bot untuk mengirim order. Jadi status "LIKUID" di panel ini
        berarti "lolos gerbang volume", BUKAN "bot pasti membeli".
      - spread saat ini dan urutan kualitas setup terhadap seluruh pasar.

    Degradasi anggun: kalau Binance tidak terjangkau, panel tetap tampil
    dengan daftar simbol dan tanda strip, bukan error yang mematikan
    dashboard. Data lama dari cache dipakai kalau ada.
    """
    if not watchlist_enabled(PUMP_CONFIG):
        return {"enabled": False, "items": [], "summary": {}, "error": None}

    # Sumber daftar: hasil penyegaran otomatis kalau ada dan aktif,
    # kalau tidak jatuh ke daftar manual di config.py. Daftar manual
    # selalu jadi cadangan, jadi panel tidak pernah kosong hanya karena
    # penyegaran belum sempat berjalan atau gagal.
    source = "config"
    auto_meta = None
    entries = None
    if watchlist_auto_enabled(PUMP_CONFIG) and wl_auto is not None:
        prev = wl_auto.load_result(PUMP_CONFIG)
        if prev and prev.get("items"):
            entries = get_watchlist({"WATCHLIST": prev["items"]})
            source = "auto"
            auto_meta = {
                "generated_at": prev.get("generated_at"),
                "days": prev.get("days"),
                "symbols_examined": prev.get("symbols_examined"),
                "weight_spent": prev.get("weight_spent"),
                "duration_seconds": prev.get("duration_seconds"),
                "stopped_reason": prev.get("stopped_reason"),
            }
    if not entries:
        entries = get_watchlist(PUMP_CONFIG)

    if not entries:
        return {"enabled": True, "items": [], "summary": {}, "error": None,
                "config": _watchlist_config(), "source": source,
                "auto": _auto_status(auto_meta)}

    now = time.time()
    cache = _watchlist_cache
    tickers = None
    error = None

    if cache["data"] is not None and now - cache["ts"] < WATCHLIST_TTL:
        tickers = cache["data"]
        error = cache["error"]
    else:
        client = get_client()
        if client is None:
            error = "Klien Binance tidak tersedia (requests belum terpasang?)."
            tickers = cache["data"]
        else:
            try:
                raw = client.get_ticker_24hr_all()
                tickers = {t["symbol"]: t for t in raw if isinstance(t, dict) and "symbol" in t}
                cache["data"] = tickers
                cache["ts"] = now
                cache["error"] = None
                error = None
            except Exception as exc:  # noqa: BLE001
                # Pakai data lama kalau ada, supaya panel tidak berkedip
                # kosong setiap kali ada satu request gagal.
                error = f"Gagal mengambil data pasar: {str(exc)[:120]}"
                tickers = cache["data"]
                cache["error"] = error

    min_vol = float(PUMP_CONFIG.get("MIN_QUOTE_VOLUME_USDT_24H", 0))

    items = []
    for e in entries:
        sym = e["symbol"]
        t = (tickers or {}).get(sym)
        row = {
            "symbol": sym, "tier": e["tier"], "score": e["score"], "note": e["note"],
            "price": None, "change_24h": None, "quote_volume_24h": None,
            "high_24h": None, "low_24h": None, "trades_24h": None,
            "pass_volume": None, "status": "TIDAK ADA DATA",
            "range_position": None,
        }
        if t:
            try:
                price = float(t.get("lastPrice", 0) or 0)
                chg = float(t.get("priceChangePercent", 0) or 0)
                qv = float(t.get("quoteVolume", 0) or 0)
                hi = float(t.get("highPrice", 0) or 0)
                lo = float(t.get("lowPrice", 0) or 0)
            except (TypeError, ValueError):
                price = chg = qv = hi = lo = 0.0

            if price > 0:
                pass_vol = qv >= min_vol
                # Hanya ada satu gerbang tingkat ticker sekarang. Sisanya
                # ditentukan oleh struktur candle, yang tidak diperiksa di
                # panel ini demi menghemat rate limit.
                status = "LIKUID" if pass_vol else "TIPIS"

                # Posisi harga dalam rentang 24 jam: 1,0 berarti di puncak
                # hari ini, 0,0 di dasar. Berguna untuk melihat apakah harga
                # sudah dekat puncak harian.
                rng = hi - lo
                rpos = ((price - lo) / rng) if rng > 0 else None

                row.update({
                    "price": price, "change_24h": chg, "quote_volume_24h": qv,
                    "high_24h": hi, "low_24h": lo,
                    "trades_24h": int(t.get("count", 0) or 0),
                    "pass_volume": pass_vol,
                    "status": status,
                    "range_position": round(rpos, 3) if rpos is not None else None,
                })
        items.append(row)

    # Urutkan: yang paling dekat kondisi masuk tampil di atas, karena itu
    # yang benar-benar ingin dilihat saat memantau. Simbol tanpa data
    # didorong ke bawah alih-alih dibuang, supaya Anda sadar datanya hilang.
    # Yang likuid tampil di atas, lalu diurutkan dari volume terbesar, sama
    # dengan urutan kandidat yang dipakai bot saat mengambil candle.
    order = {"LIKUID": 0, "TIPIS": 1, "TIDAK ADA DATA": 2}
    items.sort(key=lambda r: (order.get(r["status"], 9),
                              -(r["quote_volume_24h"] if r["quote_volume_24h"] is not None else -1.0)))

    counts = {}
    for r in items:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    return {
        "enabled": True,
        "items": items,
        "summary": {
            "total": len(items),
            "counts": counts,
            "with_data": sum(1 for r in items if r["price"] is not None),
        },
        "config": _watchlist_config(),
        "source": source,
        "auto": _auto_status(auto_meta),
        "error": error,
    }


def _auto_status(meta: Optional[dict]) -> dict:
    """Status penyegar otomatis untuk ditampilkan di panel."""
    out = {
        "enabled": watchlist_auto_enabled(PUMP_CONFIG),
        "interval_hours": PUMP_CONFIG.get("WATCHLIST_AUTO_INTERVAL_HOURS"),
        "meta": meta,
        "state": "off", "message": "", "progress": 0.0,
        "last_run": None, "next_run": None, "last_error": None,
    }
    if _auto_refresher is not None:
        out.update(_auto_refresher.get_status())
    return out


def _watchlist_config() -> dict:
    """Ambang yang dipakai panel, ditampilkan supaya angkanya tidak misterius."""
    return {
        "min_quote_volume_24h": PUMP_CONFIG.get("MIN_QUOTE_VOLUME_USDT_24H"),
        "quote_asset": PUMP_CONFIG.get("QUOTE_ASSET", "USDT"),
    }


def _bot_has_open_position() -> bool:
    """REM KEAMANAN: apakah bot sedang memegang posisi terbuka.

    Dibaca dari file state bot. Saat ada posisi terbuka, bot harus bisa
    mengirim order jual kapan saja (Stop Loss/TP/trailing), jadi penyegaran
    watchlist WAJIB mengalah dan tidak ikut memakan jatah rate-limit IP
    yang sama.

    Kalau file state tidak terbaca karena alasan apa pun, fungsi ini
    mengembalikan True (anggap ada posisi). Sikap aman: lebih baik
    penyegaran tertunda daripada mengganggu bot yang sedang pegang uang.
    """
    try:
        st = load_state()
    except Exception:  # noqa: BLE001
        return True
    if not isinstance(st, dict):
        return True
    # Skema yang BENAR adalah kunci top-level "current_symbol" + "qty",
    # persis seperti yang ditulis DEFAULT_STATE di pump_scanner_bot.py.
    # Versi lama fungsi ini membaca "position.symbol" -- kunci yang TIDAK
    # PERNAH ditulis bot -- sehingga rem keamanan ini selalu bernilai False
    # dan tidak pernah benar-benar aktif (temuan audit T-02, 2026-09-24).
    try:
        qty = float(st.get("qty", 0) or 0)
    except (TypeError, ValueError):
        return True
    return bool(st.get("current_symbol")) and qty > 0


def start_auto_refresher() -> None:
    """Nyalakan penjadwal penyegaran daftar di thread latar.

    Aman dipanggil berkali-kali; kalau sudah jalan, tidak membuat thread
    kedua. Dipanggil sekali saat dashboard start.
    """
    global _auto_refresher
    if wl_auto is None or not watchlist_auto_enabled(PUMP_CONFIG):
        return
    if _auto_refresher is not None:
        return
    _auto_refresher = wl_auto.AutoRefresher(
        client_getter=get_client,
        config=PUMP_CONFIG,
        has_open_position=_bot_has_open_position,
    )
    _auto_refresher.start()


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
    """Status proses berbasis PID/lock; umur state hanya cadangan terakhir."""
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
    """Tombol "Jual Sekarang" di dashboard -- TIDAK mengirim order langsung
    dari dashboard (dashboard sengaja tidak menyimpan/menggunakan API
    secret untuk order, hanya untuk baca saldo & harga). Sebagai gantinya,
    dashboard menulis "perintah" ke CONTROL_FILE, lalu proses bot
    (pump_scanner_bot.py, terpisah) yang membaca & mengeksekusinya lewat
    jalur close_position() yang SAMA PERSIS dipakai Stop Loss/Take Profit,
    supaya perilakunya konsisten (update cooldown, dst)."""
    # Anti-CSRF (temuan S-05): perintah jual hanya diterima kalau request
    # membawa header X-Admin-Token yang cocok dengan token acak per proses
    # yang disisipkan ke halaman dashboard. compare_digest dipakai supaya
    # perbandingan tidak bocor lewat timing.
    if not compare_digest(request.headers.get("X-Admin-Token", ""), _ADMIN_TOKEN):
        return jsonify({"error": "Token admin tidak valid atau tidak ada."}), 403

    now = time.time()
    if now - _last_manual_close_request["ts"] < _MANUAL_CLOSE_COOLDOWN_SECONDS:
        return jsonify({"error": "Tunggu sebentar, permintaan sebelumnya baru saja dikirim."}), 429

    state = load_state()
    symbol = state.get("current_symbol")
    qty = float(state.get("qty", 0) or 0)
    if not symbol or qty <= 0:
        return jsonify({"error": "Tidak ada posisi terbuka saat ini untuk dijual."}), 400

    data = request.get_json(force=True, silent=True) or {}
    confirm_symbol = str(data.get("symbol", "")).strip().upper()
    if confirm_symbol and confirm_symbol != symbol:
        return jsonify({
            "error": f"Simbol tidak cocok (diminta {confirm_symbol}, posisi saat ini {symbol}). "
                     "Muat ulang dashboard dan coba lagi."
        }), 409

    _last_manual_close_request["ts"] = now
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
    """Dipakai dashboard untuk polling apakah perintah "Jual Sekarang" yang
    baru dikirim sudah dieksekusi bot (posisi sudah tertutup) atau belum."""
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
    """Satu panggilan untuk semua data (mengurangi jumlah request)."""
    trades, events, level_count = parse_log()
    return jsonify({
        "status": build_status(),
        "summary": build_trade_summary(trades),
        "trades": trades[:100],
        "events": events,
        "log_levels": level_count,
        "watchlist": build_watchlist(),
    })


# ======================================================================
# Kontrol proses, settings, mode, kredensial, dan reset PAPER
# ======================================================================
def _config_pair(mode: str) -> tuple[dict, dict, list[str]]:
    raw = str(mode).strip().upper()
    if raw not in config_mod.VALID_MODES:
        raise ValueError("Mode hanya boleh PAPER atau LIVE.")
    defaults = config_mod.default_config_for_mode(raw)
    current, errors = config_mod.build_config_for_mode(raw, validate=False)
    return defaults, current, errors


def _revision(config: dict) -> str:
    public = {k: config.get(k) for k in PARAMETER_SCHEMA if k not in ("API_KEY", "API_SECRET")}
    encoded = json.dumps(public, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _credential_fingerprint(key: str, secret: str) -> str:
    return hashlib.sha256((key + "\x00" + secret).encode("utf-8")).hexdigest()


def _write_audit(event: dict) -> str | None:
    try:
        audit_change(event)
        return None
    except OSError as exc:
        return f"Audit gagal ditulis: {exc}"


def _credentials_tested_for_active_values() -> bool:
    key = str(PUMP_CONFIG.get("API_KEY", ""))
    secret = str(PUMP_CONFIG.get("API_SECRET", ""))
    if not key or not secret:
        return False
    return compare_digest(
        str(_credential_test_state.get("fingerprint") or ""),
        _credential_fingerprint(key, secret),
    )


def _risk_summary(config: dict) -> dict:
    return {
        "RISK_PERCENT": config.get("RISK_PERCENT"),
        "MAX_POSITION_USDT": config.get("MAX_POSITION_USDT"),
        "USE_STOP_LOSS": config.get("USE_STOP_LOSS"),
        "SL_PCT": config.get("SL_PCT"),
        "USE_TP": config.get("USE_TP"),
        "TP_PCT": config.get("TP_PCT"),
        "USE_ATR_EXITS": config.get("USE_ATR_EXITS"),
        "ATR_SL_MIN_PCT": config.get("ATR_SL_MIN_PCT"),
        "ATR_SL_MAX_PCT": config.get("ATR_SL_MAX_PCT"),
        "USE_EQUITY_STOP": config.get("USE_EQUITY_STOP"),
        "MAX_DRAWDOWN_PERCENT": config.get("MAX_DRAWDOWN_PERCENT"),
        "USE_DAILY_STOP": config.get("USE_DAILY_STOP"),
        "MAX_DAILY_LOSS_PERCENT": config.get("MAX_DAILY_LOSS_PERCENT"),
        "CLOSE_ALL_AT_LIMIT": config.get("CLOSE_ALL_AT_LIMIT"),
    }


@app.route("/api/control/status")
def api_control_status():
    process = _process_manager.status(get_mode(PUMP_CONFIG))
    return jsonify({
        "process": process,
        "position": _process_manager.position(),
        "mode": get_mode(PUMP_CONFIG),
        "read_only": not _bind_is_loopback(),
        "config_errors": list(CONFIG_LOAD_ERRORS),
        "platform": {"os_name": os.name, "windows": os.name == "nt"},
    })


@app.route("/api/control/prepare", methods=["POST"])
def api_control_prepare():
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "")).strip().upper()
    if action not in ("START", "STOP", "RESTART"):
        return jsonify({"error": "Aksi proses tidak valid."}), 400
    status = _process_manager.status(get_mode(PUMP_CONFIG))
    position = _process_manager.position()
    active_statuses = ("STARTING", "RUNNING", "STOPPING")
    if action == "START" and status["status"] not in ("STOPPED", "CRASHED"):
        return jsonify({"error": f"Start ditolak karena bot sedang {status['status']}."}), 409
    if action in ("STOP", "RESTART") and status["status"] not in active_statuses:
        return jsonify({"error": f"{action.title()} ditolak karena bot sudah {status['status']}."}), 409
    policy = str(data.get("position_policy", "REQUIRE_EMPTY")).strip().upper()
    if action in ("STOP", "RESTART") and position["has_position"]:
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
    data = request.get_json(silent=True) or {}
    payload = _take_confirmation(data.get("confirmation_id", ""), "process")
    if payload is None:
        return jsonify({"error": "Konfirmasi kedaluwarsa atau tidak valid."}), 409
    if payload.get("mode") != get_mode(PUMP_CONFIG):
        return jsonify({"error": "Mode berubah sejak konfirmasi. Ulangi aksi."}), 409
    current = _process_manager.status(get_mode(PUMP_CONFIG))
    if payload.get("action") == "START":
        if current["status"] not in ("STOPPED", "CRASHED"):
            return jsonify({"error": "Status proses berubah sejak konfirmasi Start."}), 409
    elif current["status"] not in ("STARTING", "RUNNING", "STOPPING") or current.get("pid") != payload.get("pid"):
        return jsonify({"error": "PID atau status proses berubah sejak konfirmasi. Ulangi aksi."}), 409
    ok, left = _cooldown("process", 2.0)
    if not ok:
        return jsonify({"error": f"Tunggu {left:.1f} detik sebelum aksi proses berikutnya."}), 429
    try:
        action = payload["action"]
        if action == "START":
            result = _process_manager.start()
        elif action == "STOP":
            result = _process_manager.stop(position_policy=payload["position_policy"])
        else:
            result = _process_manager.restart(position_policy=payload["position_policy"])
        return jsonify({"ok": True, "process": result})
    except (BotControlError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 409


@app.route("/api/settings")
def api_settings():
    mode = str(request.args.get("mode") or get_mode(PUMP_CONFIG)).upper()
    try:
        defaults, current, errors = _config_pair(mode)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "mode": mode,
        "fields": public_schema(defaults, current),
        "load_errors": errors,
        "active_mode": get_mode(PUMP_CONFIG),
    })


@app.route("/api/settings/history")
def api_settings_history():
    return jsonify({"items": read_audit(250)})


@app.route("/api/settings/preview", methods=["POST"])
def api_settings_preview():
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode") or "").strip().upper()
    try:
        defaults, current, load_errors = _config_pair(mode)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if load_errors:
        return jsonify({"error": "Override rusak harus diperbaiki terlebih dahulu.",
                        "details": load_errors}), 409
    values = data.get("values")
    if not isinstance(values, dict):
        return jsonify({"error": "values harus berupa object JSON."}), 400
    unknown = sorted(set(values) - set(PARAMETER_SCHEMA))
    if unknown:
        return jsonify({"error": "Kunci tidak dikenal: " + ", ".join(unknown)}), 400
    readonly = sorted(k for k in values if PARAMETER_SCHEMA[k]["read_only"])
    if readonly:
        return jsonify({"error": "Kunci read-only tidak boleh diubah: " + ", ".join(readonly)}), 400

    candidate = deepcopy(current)
    candidate.update(values)
    cleaned, errors, warnings = validate_candidate(candidate, mode)
    if errors:
        return jsonify({"error": "Validasi pengaturan gagal.", "fields": errors}), 400
    diff = diff_values(current, cleaned)
    if not diff:
        return jsonify({"error": "Tidak ada perubahan untuk disimpan."}), 400
    relaxations = dangerous_relaxations(current, cleaned) if mode == "LIVE" else []
    policy = str(data.get("position_policy", "REQUIRE_EMPTY")).strip().upper()
    process = _process_manager.status(get_mode(PUMP_CONFIG))
    position = _process_manager.position()
    if mode == get_mode(PUMP_CONFIG) and process["status"] in ("STARTING", "RUNNING", "STOPPING"):
        if position["has_position"] and policy not in ("SELL_FIRST", "KEEP_OPEN"):
            return jsonify({"error": "Pilih penanganan posisi sebelum restart.",
                            "requires_position_choice": True, "position": position}), 409
    else:
        policy = "REQUIRE_EMPTY"

    overrides = compute_overrides(defaults, cleaned)
    # Saldo awal dikelola panel reset, tetapi override itu tidak boleh hilang
    # hanya karena pengguna menyimpan parameter strategi lain.
    if current.get("PAPER_INITIAL_BALANCES") != defaults.get("PAPER_INITIAL_BALANCES"):
        overrides["PAPER_INITIAL_BALANCES"] = deepcopy(current.get("PAPER_INITIAL_BALANCES"))
    token = _make_confirmation("settings", {
        "mode": mode,
        "candidate": cleaned,
        "overrides": overrides,
        "baseline_revision": _revision(current),
        "position_policy": policy,
        "relaxations": relaxations,
    })
    return jsonify({
        "confirmation_id": token,
        "diff": diff,
        "warnings": warnings,
        "risk_warnings": relaxations,
        "requires_risk_phrase": bool(relaxations),
        "bot_will_restart": mode == get_mode(PUMP_CONFIG) and process["status"] in ("STARTING", "RUNNING"),
    })


@app.route("/api/settings/commit", methods=["POST"])
def api_settings_commit():
    data = request.get_json(silent=True) or {}
    payload = _take_confirmation(data.get("confirmation_id", ""), "settings")
    if payload is None:
        return jsonify({"error": "Konfirmasi settings kedaluwarsa atau tidak valid."}), 409
    if payload.get("relaxations") and str(data.get("risk_phrase", "")) != "SAYA PAHAM":
        return jsonify({"error": "Ketik SAYA PAHAM untuk pelonggaran risiko LIVE."}), 400
    mode = payload["mode"]
    defaults, current, load_errors = _config_pair(mode)
    if load_errors or _revision(current) != payload.get("baseline_revision"):
        return jsonify({"error": "Settings berubah sejak preview. Muat ulang dan ulangi."}), 409
    ok, left = _cooldown("settings", 3.0)
    if not ok:
        return jsonify({"error": f"Tunggu {left:.1f} detik sebelum menyimpan lagi."}), 429

    process_before = _process_manager.status(get_mode(PUMP_CONFIG))
    try:
        save_mode_override(mode, payload["overrides"])
    except (OSError, ValueError) as exc:
        return jsonify({"error": f"Settings gagal ditulis secara atomik: {exc}"}), 500
    diff = diff_values(current, payload["candidate"])
    audit_warning = None
    for item in diff:
        audit_warning = _write_audit({
            "event": "SETTING_CHANGED", "mode": mode, "key": item["key"],
            "old": item["old"], "new": item["new"],
        })
        if audit_warning:
            audit_warning = "Settings tersimpan, tetapi " + audit_warning.lower()
            break

    restarted = False
    restart_error = None
    if mode == get_mode(PUMP_CONFIG):
        if process_before["status"] in ("STARTING", "RUNNING", "STOPPING"):
            try:
                _process_manager.restart(position_policy=payload["position_policy"])
                restarted = True
            except BotControlError as exc:
                restart_error = str(exc)
        else:
            config_mod.reload_config()
        _refresh_runtime_globals()
    return jsonify({
        "ok": restart_error is None,
        "saved": True,
        "restarted": restarted,
        "restart_error": restart_error,
        "audit_warning": audit_warning,
        "diff": diff,
        "process": _process_manager.status(get_mode(PUMP_CONFIG)),
    }), (200 if restart_error is None else 409)


@app.route("/api/mode/checklist")
def api_mode_checklist():
    target = str(request.args.get("target") or "").strip().upper()
    if target not in config_mod.VALID_MODES:
        return jsonify({"error": "Target mode tidak valid."}), 400
    try:
        target_cfg, target_errors = config_mod.build_config_for_mode(target)
    except Exception as exc:
        return jsonify({"error": f"Gagal membaca konfigurasi target: {exc}"}), 400
    process = _process_manager.status(get_mode(PUMP_CONFIG))
    position = _process_manager.position()
    creds_complete = bool(PUMP_CONFIG.get("API_KEY") and PUMP_CONFIG.get("API_SECRET"))
    tested = _credentials_tested_for_active_values()
    max_position_ok = target != "LIVE" or float(target_cfg.get("MAX_POSITION_USDT", 0) or 0) > 0
    checks = {
        "different_mode": target != get_mode(PUMP_CONFIG),
        "bot_stopped": process["status"] in ("STOPPED", "CRASHED"),
        "no_open_position": not position["has_position"],
        "credentials_complete": target != "LIVE" or creds_complete,
        "connection_tested": target != "LIVE" or tested,
        "settings_valid": not target_errors and max_position_ok,
        "max_position_positive": max_position_ok,
    }
    return jsonify({
        "current_mode": get_mode(PUMP_CONFIG),
        "target_mode": target,
        "checks": checks,
        "ready": all(checks.values()),
        "position": position,
        "risk": _risk_summary(target_cfg),
        "settings_errors": target_errors,
        "state_separation_note": "State PAPER dan LIVE disimpan terpisah. Posisi tidak dipindahkan antar-mode.",
    })


@app.route("/api/mode/prepare", methods=["POST"])
def api_mode_prepare():
    data = request.get_json(silent=True) or {}
    target = str(data.get("target", "")).strip().upper()
    if target not in config_mod.VALID_MODES:
        return jsonify({"error": "Target mode tidak valid."}), 400
    # Hitung ulang tanpa memanggil endpoint melalui HTTP.
    target_cfg, target_errors = config_mod.build_config_for_mode(target)
    process = _process_manager.status(get_mode(PUMP_CONFIG))
    position = _process_manager.position()
    if target == get_mode(PUMP_CONFIG):
        return jsonify({"error": "Mode target sudah aktif."}), 400
    if process["status"] not in ("STOPPED", "CRASHED"):
        return jsonify({"error": "Bot harus STOPPED sebelum ganti mode."}), 409
    if position["has_position"]:
        return jsonify({"error": "Posisi mode aktif harus ditutup sebelum ganti mode.",
                        "position": position}), 409
    if target_errors:
        return jsonify({"error": "Konfigurasi target rusak.", "details": target_errors}), 409
    if target == "LIVE":
        if not PUMP_CONFIG.get("API_KEY") or not PUMP_CONFIG.get("API_SECRET"):
            return jsonify({"error": "API key dan secret belum lengkap."}), 409
        if not _credentials_tested_for_active_values():
            return jsonify({"error": "Kredensial tersimpan belum lulus Uji Koneksi pada sesi ini."}), 409
        if float(target_cfg.get("MAX_POSITION_USDT", 0) or 0) <= 0:
            return jsonify({"error": "MAX_POSITION_USDT LIVE wajib lebih besar dari nol."}), 409
    token = _make_confirmation("mode", {
        "from": get_mode(PUMP_CONFIG), "target": target,
        "risk_revision": _revision(target_cfg),
    }, ttl=180)
    return jsonify({"confirmation_id": token, "target": target,
                    "risk": _risk_summary(target_cfg),
                    "required_phrase": "LIVE" if target == "LIVE" else None})


@app.route("/api/mode/commit", methods=["POST"])
def api_mode_commit():
    data = request.get_json(silent=True) or {}
    payload = _take_confirmation(data.get("confirmation_id", ""), "mode")
    if payload is None:
        return jsonify({"error": "Konfirmasi mode kedaluwarsa atau tidak valid."}), 409
    target = payload["target"]
    if target == "LIVE" and str(data.get("phrase", "")) != "LIVE":
        return jsonify({"error": "Ketik LIVE persis untuk mengaktifkan mode uang asli."}), 400
    process = _process_manager.status(get_mode(PUMP_CONFIG))
    if process["status"] not in ("STOPPED", "CRASHED") or _process_manager.position()["has_position"]:
        return jsonify({"error": "Kondisi proses atau posisi berubah. Ulangi perpindahan mode."}), 409
    target_cfg, errors = config_mod.build_config_for_mode(target)
    if errors or _revision(target_cfg) != payload.get("risk_revision"):
        return jsonify({"error": "Konfigurasi target berubah sejak konfirmasi."}), 409
    if target == "LIVE" and not _credentials_tested_for_active_values():
        return jsonify({"error": "Status uji kredensial tidak lagi valid."}), 409
    ok, left = _cooldown("mode", 5.0)
    if not ok:
        return jsonify({"error": f"Tunggu {left:.1f} detik sebelum ganti mode."}), 429
    old = get_mode(PUMP_CONFIG)
    try:
        save_runtime_mode(target)
        config_mod.reload_config()
        _refresh_runtime_globals()
    except (OSError, ValueError) as exc:
        try:
            save_runtime_mode(old)
            config_mod.reload_config()
            _refresh_runtime_globals()
        except Exception:
            pass
        return jsonify({"error": f"Mode gagal disimpan: {exc}"}), 500
    audit_warning = _write_audit({"event": "MODE_CHANGED", "mode": target, "key": "MODE",
                                  "old": old, "new": target})
    return jsonify({"ok": True, "mode": target, "audit_warning": audit_warning,
                    "message": f"Mode berubah ke {target}. Bot tetap STOPPED sampai Anda menekan Start."})


@app.route("/api/credentials/status")
def api_credentials_status():
    status = credential_status(ENV_PATH)
    status.update({
        "connection_tested": _credentials_tested_for_active_values(),
        "tested_at": _credential_test_state.get("tested_at"),
        "recommendation": "Gunakan key tanpa izin withdrawal dan aktifkan pembatasan IP bila tersedia.",
    })
    return jsonify(status)


@app.route("/api/credentials/save", methods=["POST"])
def api_credentials_save():
    process = _process_manager.status(get_mode(PUMP_CONFIG))
    if get_mode(PUMP_CONFIG) == "LIVE" and process["status"] in ("STARTING", "RUNNING", "STOPPING"):
        return jsonify({"error": "Hentikan bot LIVE sebelum mengganti kredensial."}), 409
    ok, left = _cooldown("credentials-save", 5.0)
    if not ok:
        return jsonify({"error": f"Tunggu {left:.1f} detik sebelum menyimpan kredensial."}), 429
    data = request.get_json(silent=True) or {}
    old_key = str(PUMP_CONFIG.get("API_KEY", ""))
    old_secret = str(PUMP_CONFIG.get("API_SECRET", ""))
    key_input = data.get("api_key")
    secret_input = data.get("api_secret")
    new_key = "" if data.get("clear_api_key") else (
        str(key_input) if isinstance(key_input, str) and key_input != "" else old_key
    )
    new_secret = "" if data.get("clear_api_secret") else (
        str(secret_input) if isinstance(secret_input, str) and secret_input != "" else old_secret
    )
    try:
        permissions = update_env(api_key=new_key, api_secret=new_secret, path=ENV_PATH)
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=ENV_PATH, override=True)
        config_mod.reload_config()
        _refresh_runtime_globals()
    except (OSError, ValueError) as exc:
        return jsonify({"error": f"Gagal menyimpan kredensial: {exc}"}), 400
    _credential_test_state.update({"fingerprint": None, "tested_at": None, "account": None})
    audit_warning = _write_audit({
        "event": "CREDENTIALS_CHANGED", "mode": get_mode(PUMP_CONFIG),
        "key_changed": new_key != old_key, "secret_changed": new_secret != old_secret,
    })
    return jsonify({
        "ok": True,
        "audit_warning": audit_warning,
        "api_key_set": bool(new_key),
        "api_secret_set": bool(new_secret),
        "api_key_last4": new_key[-4:] if new_key else None,
        "permissions": permissions,
    })


@app.route("/api/credentials/test", methods=["POST"])
def api_credentials_test():
    ok, left = _cooldown("credentials-test", 5.0)
    if not ok:
        return jsonify({"error": f"Tunggu {left:.1f} detik sebelum uji koneksi berikutnya."}), 429
    data = request.get_json(silent=True) or {}
    key = str(data.get("api_key") or PUMP_CONFIG.get("API_KEY") or "")
    secret = str(data.get("api_secret") or PUMP_CONFIG.get("API_SECRET") or "")
    if not key or not secret:
        return jsonify({"error": "API key dan secret wajib terisi untuk pengujian."}), 400
    if (len(key) > 512 or len(secret) > 512 or key != key.strip() or secret != secret.strip()
            or any(ord(ch) < 33 or ord(ch) > 126 for ch in key + secret)):
        return jsonify({"error": "Format kredensial tidak valid."}), 400
    try:
        client = BinanceSpotClient(key, secret, get_base_url(PUMP_CONFIG), allow_signed=True)
        client.sync_time()
        account = client.get_account()
    except BinanceAPIError as exc:
        return jsonify({
            "error": "Binance menolak uji koneksi.",
            "binance_code": exc.code,
            "binance_message": str(exc.msg)[:240],
        }), 400
    except Exception:
        return jsonify({"error": "Uji koneksi gagal karena jaringan atau layanan Binance tidak tersedia."}), 502
    _credential_test_state.update({
        "fingerprint": _credential_fingerprint(key, secret),
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "account": {"account_type": account.get("accountType"),
                    "can_trade": bool(account.get("canTrade")),
                    "permissions": account.get("permissions", [])},
    })
    return jsonify({"ok": True, "message": "Koneksi signed read-only berhasil.",
                    "account": _credential_test_state["account"],
                    "api_key_last4": key[-4:]})


@app.route("/api/paper/reset/status")
def api_paper_reset_status():
    return jsonify({
        "eligible": get_mode(PUMP_CONFIG) == "PAPER" and
                    _process_manager.status("PAPER")["status"] in ("STOPPED", "CRASHED"),
        "mode": get_mode(PUMP_CONFIG),
        "process": _process_manager.status(get_mode(PUMP_CONFIG)),
        "default_balances": PUMP_CONFIG.get("PAPER_INITIAL_BALANCES", {"USDT": 10000.0}),
    })


@app.route("/api/paper/reset/prepare", methods=["POST"])
def api_paper_reset_prepare():
    if get_mode(PUMP_CONFIG) != "PAPER":
        return jsonify({"error": "Reset akun hanya tersedia saat mode aktif PAPER."}), 409
    process = _process_manager.status("PAPER")
    if process["status"] not in ("STOPPED", "CRASHED"):
        return jsonify({"error": "Bot PAPER harus STOPPED sebelum reset."}), 409
    data = request.get_json(silent=True) or {}
    try:
        balances = validate_balances(data.get("balances"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    files = [PUMP_CONFIG["PAPER_ACCOUNT_STATE_FILE"], PUMP_CONFIG["STATE_FILE"]]
    token = _make_confirmation("paper-reset", {"balances": balances, "files": files}, ttl=180)
    return jsonify({"confirmation_id": token, "balances": balances,
                    "archives": [f"{path}.bak-<timestamp>" for path in files if Path(path).exists()],
                    "required_phrase": "RESET"})


@app.route("/api/paper/reset/commit", methods=["POST"])
def api_paper_reset_commit():
    data = request.get_json(silent=True) or {}
    payload = _take_confirmation(data.get("confirmation_id", ""), "paper-reset")
    if payload is None:
        return jsonify({"error": "Konfirmasi reset kedaluwarsa atau tidak valid."}), 409
    if str(data.get("phrase", "")) != "RESET":
        return jsonify({"error": "Ketik RESET persis untuk melanjutkan."}), 400
    if get_mode(PUMP_CONFIG) != "PAPER" or _process_manager.status("PAPER")["status"] not in ("STOPPED", "CRASHED"):
        return jsonify({"error": "Mode atau status bot berubah. Reset dibatalkan."}), 409
    ok, left = _cooldown("paper-reset", 10.0)
    if not ok:
        return jsonify({"error": f"Tunggu {left:.1f} detik sebelum reset berikutnya."}), 429
    stamp = timestamp_tag()
    moved: list[tuple[Path, Path]] = []
    try:
        for raw in payload["files"]:
            source = Path(raw)
            if not source.exists():
                continue
            backup = Path(f"{source}.bak-{stamp}")
            if backup.exists():
                backup = Path(f"{source}.bak-{stamp}-{uuid.uuid4().hex[:6]}")
            replace_with_retry(source, backup)
            moved.append((source, backup))
    except OSError as exc:
        for source, backup in reversed(moved):
            try:
                replace_with_retry(backup, source)
            except OSError:
                pass
        return jsonify({"error": f"Pengarsipan gagal, reset dibatalkan: {exc}"}), 500

    defaults, current, errors = _config_pair("PAPER")
    if errors:
        # Kembalikan file karena settings tidak aman untuk diperbarui.
        for source, backup in reversed(moved):
            try:
                replace_with_retry(backup, source)
            except OSError:
                pass
        return jsonify({"error": "Override PAPER rusak. Reset dibatalkan."}), 409
    candidate = deepcopy(current)
    candidate["PAPER_INITIAL_BALANCES"] = payload["balances"]
    overrides = compute_overrides(defaults, candidate, include_balances=True)
    previous_overrides = compute_overrides(defaults, current, include_balances=True)
    try:
        save_mode_override("PAPER", overrides)
        config_mod.reload_config()
        _refresh_runtime_globals()
    except (OSError, ValueError) as exc:
        # Kembalikan settings dan kedua file supaya reset tidak setengah jadi.
        try:
            save_mode_override("PAPER", previous_overrides)
            config_mod.reload_config()
            _refresh_runtime_globals()
        except Exception:
            pass
        for source, backup in reversed(moved):
            try:
                replace_with_retry(backup, source)
            except OSError:
                pass
        return jsonify({"error": f"Reset gagal saat menyimpan saldo; file dipulihkan: {exc}"}), 500
    audit_warning = _write_audit({
        "event": "PAPER_RESET", "mode": "PAPER", "key": "PAPER_INITIAL_BALANCES",
        "old": current.get("PAPER_INITIAL_BALANCES"), "new": payload["balances"],
        "archives": [str(backup) for _, backup in moved],
    })
    state_mod.clear_control(PUMP_CONFIG["CONTROL_FILE"])
    state_mod.clear_stop_request(PUMP_CONFIG["CONTROL_FILE"])
    return jsonify({"ok": True, "balances": payload["balances"],
                    "archives": [str(backup) for _, backup in moved],
                    "audit_warning": audit_warning,
                    "message": "Akun PAPER diarsipkan. Saldo baru dibuat saat bot berikutnya Start."})


def main(*, auto_start_bot: bool = False) -> int:
    """Jalankan dashboard. run.py memakai auto_start_bot=True."""
    start_auto_refresher()
    if watchlist_auto_enabled(PUMP_CONFIG):
        print(f"Penyegaran watchlist otomatis aktif "
              f"(tiap {PUMP_CONFIG.get('WATCHLIST_AUTO_INTERVAL_HOURS')} jam, "
              f"dilewati saat ada posisi terbuka).")

    if not _bind_is_loopback():
        # Agar halaman peringatan read-only dapat dibuka pada bind yang
        # dipilih eksplisit, host itu dipercaya untuk request baca saja.
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
