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

from config import PUMP_CONFIG, get_mode, get_base_url, is_testnet
import state as state_mod

try:
    from binance_client import BinanceSpotClient
    _HAS_CLIENT = True
except Exception:  # pragma: no cover - kalau requests tidak ada, tetap jalan tanpa live
    _HAS_CLIENT = False

import backtest as bt

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
            "be_trigger_pct": state.get("be_trigger_pct") or PUMP_CONFIG.get("BE_TRIGGER_PCT"),
            "trail_start_pct": state.get("trail_start_pct") or PUMP_CONFIG.get("TRAILING_START_PCT"),
            "trail_step_pct": state.get("trail_step_pct") or PUMP_CONFIG.get("TRAILING_STEP_PCT"),
            "use_atr_exits": bool(PUMP_CONFIG.get("USE_ATR_EXITS")),
            "be_trigger_pct": PUMP_CONFIG.get("BE_TRIGGER_PCT"),
            "trailing_start_pct": PUMP_CONFIG.get("TRAILING_START_PCT"),
            "max_hold_minutes": PUMP_CONFIG.get("MAX_HOLD_MINUTES"),
            "min_pump_pct_24h": PUMP_CONFIG.get("MIN_PUMP_PCT_24H"),
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

BT_PARAM_KEYS = (
    "USE_ATR_EXITS",
    "SL_PCT", "TP_PCT", "BE_TRIGGER_PCT", "BE_LOCK_PCT", "TRAILING_START_PCT",
    "TRAILING_STEP_PCT", "MAX_HOLD_MINUTES", "MIN_PUMP_PCT_24H", "VWAP_MAX_EXTENSION_PCT",
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


def _bt_run_job(job_id: str, symbol: str, days: int, overrides: dict, compare: bool = False):
    def set_progress(frac, stage=""):
        with _bt_jobs_lock:
            if job_id in _bt_jobs:
                _bt_jobs[job_id]["progress"] = round(float(frac), 3)
                if stage:
                    _bt_jobs[job_id]["stage"] = stage
                _bt_jobs[job_id]["updated_at"] = time.time()

    try:
        cfg = bt.apply_overrides(PUMP_CONFIG, overrides)
        cfg["_symbol"] = symbol
        bt.validate_params(cfg)

        interval = cfg.get("CONFIRM_INTERVAL", "5m")
        window_bars = bt.bars_per_day(interval)
        warmup_ms = bt.MS_PER_DAY + cfg["CONFIRM_LOOKBACK_BARS"] * bt.INTERVAL_MINUTES.get(interval, 5) * 60_000

        end_ms = int(time.time() * 1000)
        start_ms = end_ms - days * bt.MS_PER_DAY
        fetch_start_ms = start_ms - warmup_ms

        client = get_client()
        if client is None:
            raise bt.BacktestError(
                "Klien Binance tidak tersedia (modul 'requests' tidak termuat). "
                "Backtest butuh akses ke data historis publik Binance."
            )

        set_progress(0.02, "mengambil data historis dari Binance...")
        klines = bt.fetch_full_klines(
            client, symbol, interval, fetch_start_ms, end_ms,
            progress_cb=lambda f: set_progress(0.02 + f * 0.7, "mengambil data historis dari Binance..."),
        )
        if len(klines) < window_bars + cfg["CONFIRM_LOOKBACK_BARS"] + 5:
            raise bt.BacktestError(
                f"Data historis terlalu sedikit ({len(klines)} candle) untuk simbol '{symbol}'. "
                "Kemungkinan simbol salah/tidak ada di Binance Spot, atau rentang hari terlalu pendek."
            )

        if compare:
            # Dua simulasi pada candle yang SAMA PERSIS: sekali SL/TP tetap,
            # sekali berbasis ATR. Semua parameter lain identik, jadi selisih
            # hasil benar-benar berasal dari metode exit.
            set_progress(0.75, "menjalankan simulasi TETAP lalu ATR...")
            cmp_out = bt.compare_fixed_vs_atr(klines, cfg, warmup_bars=window_bars)
            set_progress(0.99, "menyusun perbandingan...")
            result = cmp_out["result_atr"]
            summary = cmp_out["atr"]
        else:
            set_progress(0.75, "menjalankan simulasi...")
            result = bt.run_backtest(
                klines, cfg, warmup_bars=window_bars,
                progress_cb=lambda f: set_progress(0.75 + f * 0.24, "menjalankan simulasi..."),
            )
            summary = bt.summarize(result)
            cmp_out = None

        trades_out = [{
            "entry_time": datetime.fromtimestamp(t.entry_time / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "exit_time": datetime.fromtimestamp(t.exit_time / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "reason": t.reason,
            "hold_minutes": t.hold_minutes,
            "pnl_pct": t.pnl_pct,
        } for t in result.trades]

        payload = {
            "symbol": symbol,
            "interval": interval,
            "days": days,
            "bars_total": result.bars_total,
            "bars_usable": result.bars_usable,
            "start_time": datetime.fromtimestamp(result.start_time / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if result.start_time else None,
            "end_time": datetime.fromtimestamp(result.end_time / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if result.end_time else None,
            "params_used": {k: cfg.get(k) for k in BT_PARAM_KEYS},
            "summary": summary,
            "compare": None if not compare else {
                "fixed": cmp_out["fixed"],
                "atr": cmp_out["atr"],
                "atr_info": cmp_out.get("atr_info") or {},
            },
            "trades": trades_out,
            "warnings": result.warnings,
            "limitations": [
                "Persaingan antar-simbol TIDAK disimulasikan -- ini menguji \"jika bot memantau "
                "simbol ini dan lolos filter\", bukan peluang bot benar-benar memilihnya dari "
                "seluruh pasar.",
                "Exit dievaluasi per-candle 5 menit (bukan tick real-time seperti bot asli), "
                "dengan urutan prioritas tetap dan sengaja konservatif: STOP_LOSS -> TAKE_PROFIT -> "
                "BREAKEVEN -> TRAILING -> MAX_HOLD (Stop Loss dianggap kena lebih dulu kalau ambigu "
                "dalam satu candle, supaya hasil tidak melebih-lebihkan profit).",
                "MOMENTUM_FADE_EXIT (keluar dini dari ranking top-N) tidak disimulasikan.",
                "Filter VWAP (USE_VWAP_FILTER) memakai VWAP BERGULIR jangka pendek (window = "
                "candle konfirmasi momentum yang sama), bukan VWAP sesi/harian -- kandidat ditolak "
                "kalau harga di bawah VWAP atau lebih dari VWAP_MAX_EXTENSION_PCT di atasnya.",
                "Return total memakai compounding sederhana (reinvest 100% saldo tiap trade), "
                "sesuai RISK_PERCENT bot, TANPA memperhitungkan slippage atau fee trading.",
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
    _bt_cleanup_old_jobs()
    data = request.get_json(force=True, silent=True) or {}

    symbol = str(data.get("symbol", "")).strip().upper()
    if not symbol:
        return jsonify({"error": "Simbol wajib diisi (contoh: SOLUSDT)."}), 400
    if not re.fullmatch(r"[A-Z0-9]{5,20}", symbol):
        return jsonify({"error": "Format simbol tidak valid. Contoh yang benar: SOLUSDT, PEPEUSDT."}), 400
    if not symbol.endswith(QUOTE):
        return jsonify({"error": f"Simbol harus berakhiran {QUOTE} (pair spot), contoh: SOL{QUOTE}."}), 400

    try:
        days = int(data.get("days", 90))
    except (TypeError, ValueError):
        return jsonify({"error": "Jumlah hari tidak valid."}), 400
    # TIDAK ADA batas atas yang dipaksakan di sini -- rentang backtest
    # sepenuhnya fleksibel. Batas alaminya hanya sejak kapan simbol itu
    # listing di Binance: kalau data historis yang diminta lebih panjang
    # dari yang tersedia, fetch_full_klines() akan mengembalikan apa yang
    # ADA (bukan error), lalu backtest jalan dengan data yang tersedia itu.
    if days < 1:
        return jsonify({"error": "Jumlah hari minimal 1."}), 400

    compare = bool(data.get("compare"))
    overrides = {k: data.get(k) for k in BT_PARAM_KEYS}
    if compare:
        # Mode banding mengatur USE_ATR_EXITS sendiri (dijalankan dua kali),
        # jadi toggle dari form tidak relevan dan sengaja diabaikan.
        overrides.pop("USE_ATR_EXITS", None)
    try:
        cfg_preview = bt.apply_overrides(PUMP_CONFIG, overrides)
        bt.validate_params(cfg_preview)
    except bt.BacktestError as exc:
        return jsonify({"error": str(exc)}), 400

    # Batasi jumlah job yang berjalan bersamaan (backtest lumayan berat &
    # membebani rate-limit publik Binance kalau dijalankan paralel banyak).
    with _bt_jobs_lock:
        running = sum(1 for j in _bt_jobs.values() if j["status"] == "running")
        if running >= 2:
            return jsonify({"error": "Sudah ada backtest lain sedang berjalan. Tunggu selesai dulu."}), 429

        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        _bt_jobs[job_id] = {
            "status": "running", "progress": 0.0, "stage": "memulai...",
            "created_at": now, "started_at": now, "updated_at": now,
            "symbol": symbol, "days": days,
        }

    thread = threading.Thread(target=_bt_run_job,
                              args=(job_id, symbol, days, overrides, compare), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/backtest/status/<job_id>")
def api_backtest_status(job_id):
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


@app.route("/api/backtest/defaults")
def api_backtest_defaults():
    """Nilai default form backtest, diambil langsung dari config.py yang
    sedang dipakai bot supaya form selalu mencerminkan konfigurasi terkini."""
    out = {k: PUMP_CONFIG.get(k) for k in BT_PARAM_KEYS}
    out["quote_asset"] = QUOTE
    out["default_days"] = 90
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
    print(f"Dashboard berjalan di http://0.0.0.0:{port}  (Ctrl+C untuk berhenti)")
    app.run(host="0.0.0.0", port=port, debug=False)
