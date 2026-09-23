#!/usr/bin/env python3
"""
Dashboard web untuk Pump Scanner Bot Binance Spot.
===================================================

Dashboard ini hampir sepenuhnya MEMANTAU -- tidak start/stop bot, tidak
mengubah setting/config. Satu-satunya perintah yang bisa dikirim ke bot
adalah tombol "Jual Sekarang" untuk menutup paksa posisi yang sedang
terbuka (lihat check_manual_close() dan endpoint /api/manual/close di bawah).
Di luar itu, sumber datanya:

1. File state bot   -> pump_bot_state.json  (posisi, level BE/trailing, equity)
2. File log bot     -> pump_bot.log         (riwayat trade + kejadian)
3. Data live Binance (opsional) -> harga real-time koin yang dipegang, saldo
   akun (kalau API key tersedia). Kalau Binance tak terjangkau atau API key
   kosong, dashboard tetap jalan dengan data dari file saja (degradasi anggun).

Jalankan:
    pip install -r requirements.txt
    python dashboard.py
    # buka http://localhost:8080  (atau IP VPS Anda:8080)

CATATAN KEAMANAN: dashboard ini menampilkan saldo & aktivitas trading Anda,
dan bisa memicu 1 jenis order (jual paksa posisi terbuka). Kalau Anda expose
ke internet (bukan cuma localhost), batasi aksesnya (firewall / reverse
proxy + auth) -- siapa pun yang bisa membuka dashboard bisa menutup posisi
Anda kapan saja.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

from config import PUMP_CONFIG, get_mode, get_base_url, is_testnet, backtest_enabled
import state as state_mod

try:
    from binance_client import BinanceSpotClient
    _HAS_CLIENT = True
except Exception:  # pragma: no cover - kalau requests tidak ada, tetap jalan tanpa live
    _HAS_CLIENT = False

import backtest as bt
import portfolio_backtest as pbt

app = Flask(__name__)

STATE_FILE = PUMP_CONFIG.get("STATE_FILE", "pump_bot_state.json")
LOG_FILE = PUMP_CONFIG.get("LOG_FILE", "pump_bot.log")
CONTROL_FILE = PUMP_CONFIG.get("CONTROL_FILE", "pump_bot_control.json")
QUOTE = PUMP_CONFIG.get("QUOTE_ASSET", "USDT")

# Jarak minimum antar-klik tombol "Jual Sekarang" -- mencegah dobel-klik
# tak sengaja menumpuk banyak perintah sekaligus di control file (walau
# toh cuma file terakhir yang dibaca bot, ini juga mencegah spam UI).
_MANUAL_CLOSE_COOLDOWN_SECONDS = 5.0
_last_manual_close_request = {"ts": 0.0}

# --- Cache klien & harga supaya tidak spam Binance tiap refresh ---
_client = None
_price_cache: dict = {}          # {symbol: (price, ts)}
_balance_cache: dict = {"data": None, "ts": 0}
PRICE_TTL = 5.0                  # detik
BALANCE_TTL = 30.0              # detik


def get_client():
    global _client
    if not _HAS_CLIENT:
        return None
    if _client is None:
        try:
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
    if client is None or not PUMP_CONFIG.get("API_KEY"):
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
                "testnet": is_testnet(PUMP_CONFIG),
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
                "testnet": is_testnet(PUMP_CONFIG),
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
        "testnet": is_testnet(PUMP_CONFIG),
        # Dipakai dashboard untuk menyembunyikan tab Backtest. Ini hanya
        # petunjuk untuk UI -- penegakan sebenarnya ada di endpoint
        # /api/backtest/* lewat _reject_if_backtest_disabled().
        "backtest_enabled": backtest_enabled(PUMP_CONFIG),
        "live_connected": live_price is not None or balances is not None,
        "bot_alive": _bot_looks_alive(),
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
            "min_pump_pct_24h": PUMP_CONFIG.get("MIN_PUMP_PCT_24H"),
            "entry_model": PUMP_CONFIG.get("ENTRY_MODEL", "LEGACY_MOMENTUM"),
            "min_relative_quote_volume": PUMP_CONFIG.get("MIN_RELATIVE_QUOTE_VOLUME"),
            "risk_percent": PUMP_CONFIG.get("RISK_PERCENT"),
            "max_position_usdt": PUMP_CONFIG.get("MAX_POSITION_USDT"),
            "vwap_max_extension_pct": PUMP_CONFIG.get("VWAP_MAX_EXTENSION_PCT") if PUMP_CONFIG.get("USE_VWAP_FILTER") else None,
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
    "TRAILING_STEP_PCT", "MAX_HOLD_MINUTES", "MIN_PUMP_PCT_24H", "VWAP_MAX_EXTENSION_PCT",
    "ENTRY_MODEL", "VWAP_RETEST_LOOKBACK_BARS", "VWAP_RETEST_TOUCH_TOLERANCE_PCT",
    "VWAP_RETEST_MAX_BREAKDOWN_PCT", "VWAP_RETEST_MIN_RECLAIM_PCT",
    "VWAP_RETEST_SIGNAL_MIN_CLOSE_POSITION", "RVOL_LOOKBACK_BARS", "MIN_RELATIVE_QUOTE_VOLUME",
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
        warmup_ms = bt.MS_PER_DAY + cfg["CONFIRM_LOOKBACK_BARS"] * bar_ms

        end_ms = int(time.time() * 1000)
        start_ms = end_ms - days * bt.MS_PER_DAY
        fetch_start_ms = start_ms - warmup_ms

        client = get_client()
        if client is None:
            raise bt.BacktestError(
                "Klien Binance tidak tersedia (modul 'requests' tidak termuat). "
                "Backtest butuh akses ke data historis publik Binance."
            )

        # --- Tahap 1: tentukan semesta simbol -------------------------
        set_progress(0.01, "mengambil daftar pasar...")
        try:
            tickers = client.get_ticker_24hr_all()
        except Exception as exc:  # noqa: BLE001
            raise bt.BacktestError(
                f"Gagal mengambil daftar pasar dari Binance: {exc}"
            ) from exc

        universe = pbt.select_universe(tickers, cfg, max_symbols=max_symbols)
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
                "Peringkat 24 jam DIREKONSTRUKSI dari candle, bukan diambil dari snapshot "
                "ticker/24hr historis (Binance tidak menyediakannya). Nilainya sangat dekat "
                "tetapi tidak identik dengan yang dilihat bot saat itu.",
                "Exit dievaluasi per-candle " + interval + " (bukan tiap "
                + str(PUMP_CONFIG.get("LOOP_INTERVAL_SECONDS", 15)) + " detik seperti bot asli), "
                "dengan urutan prioritas konservatif: STOP_LOSS -> TAKE_PROFIT -> BREAKEVEN -> "
                "TRAILING -> MAX_HOLD -> MOMENTUM_FADE. Stop Loss dianggap kena lebih dulu kalau "
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
    out["momentum_fade_exit"] = PUMP_CONFIG.get("MOMENTUM_FADE_EXIT", False)
    out["momentum_fade_rank"] = PUMP_CONFIG.get("MOMENTUM_FADE_RANK_THRESHOLD", 30)
    out["confirm_lookback_bars"] = PUMP_CONFIG.get("CONFIRM_LOOKBACK_BARS", 20)
    out["min_quote_volume"] = PUMP_CONFIG.get("MIN_QUOTE_VOLUME_USDT_24H", 0)
    return jsonify(out)


@app.route("/")
def index():
    return render_template("dashboard.html")


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
    """Perkiraan kasar apakah proses pump_scanner_bot.py sedang berjalan --
    dashboard.py TIDAK punya akses langsung ke proses bot (dua proses
    terpisah, lihat run.py), jadi dipakai proxy: bot menulis STATE_FILE
    ulang di SETIAP iterasi loop (default tiap 15 detik, LOOP_INTERVAL_SECONDS).
    Kalau file itu tidak pernah diperbarui lebih dari beberapa kali interval,
    kemungkinan besar bot sedang tidak berjalan -- dipakai untuk memberi
    peringatan di dashboard supaya perintah "Jual Sekarang" tidak menunggu
    tanpa kepastian kalau ternyata bot memang sedang mati."""
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
    })


if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))

    # KEAMANAN: dashboard ini TIDAK punya login, dan punya endpoint yang
    # bisa menjual posisi sungguhan (/api/manual/close). Sebelumnya host
    # dipaksa "0.0.0.0", artinya siapa pun yang sejaringan (wifi kafe,
    # kos, kantor) bisa membuka dashboard Anda dan menekan "Jual Sekarang".
    #
    # Sekarang bawaannya 127.0.0.1 (hanya komputer ini). Kalau Anda memang
    # perlu mengaksesnya dari HP atau komputer lain, jalankan dengan:
    #     DASHBOARD_HOST=0.0.0.0 python dashboard.py
    # dan pastikan jaringannya tepercaya, atau pasang di belakang reverse
    # proxy yang meminta password.
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"

    if host == "0.0.0.0":
        print("=" * 62)
        print("PERINGATAN KEAMANAN")
        print("Dashboard dibuka ke SELURUH jaringan tanpa password.")
        print("Siapa pun yang sejaringan bisa melihat posisi Anda dan")
        print("menekan tombol Jual Sekarang. Pakai hanya di jaringan")
        print("yang Anda percaya sepenuhnya.")
        print("=" * 62)

    tampil = "localhost" if host == "127.0.0.1" else host
    print(f"Dashboard berjalan di http://{tampil}:{port}  (Ctrl+C untuk berhenti)")
    app.run(host=host, port=port, debug=False)
