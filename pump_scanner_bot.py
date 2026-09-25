#!/usr/bin/env python3
"""
Bot rotasi PULLBACK dan RETEST untuk Binance Spot. Bot memantau pair QUOTE
yang likuid, mencari struktur breakout di atas swing high lalu pullback
kembali ke level itu pada candle konfirmasi yang SUDAH tertutup, lalu masuk
dengan SATU entry long (tanpa martingale dan tanpa averaging-down). Keluar
lewat Take Profit, Breakeven, Trailing, batas waktu hold, atau invalidasi
setup (close tertutup di bawah level breakout dikurangi ATR).

Nama file masih pump_scanner_bot.py demi kompatibilitas skrip dan layanan
yang sudah ada. Pengurutan top gainer memang sudah dihapus (kandidat diurut
berdasarkan kualitas setup), tetapi seleksi semesta kembali memakai GERBANG
PUMP yang wajib: naik >= PUMP_MIN_24H_CHANGE_PCT dalam 24 jam DAN volume
kuotasi 24 jam >= PUMP_VOLUME_SURGE_MULT x rata-rata 7 hari penuh sebelumnya.
Lihat market_scanner.is_pumping_today().

INI BUKAN PREDIKSI. Bot ini bereaksi terhadap struktur yang SUDAH terbentuk.
Baca README.md bagian strategi sebelum menjalankan dengan uang sungguhan.

CARA PAKAI (sama seperti bot.py):
    pip install -r requirements.txt
    set BINANCE_API_KEY / BINANCE_API_SECRET (lihat README.md)
    python pump_scanner_bot.py --selftest      # audit logika, tanpa jaringan
    python pump_scanner_bot.py                 # jalan (MODE="PAPER" dulu, default)

MODE RUNTIME:
    "PAPER" -> simulasi eksekusi lokal penuh; data pasar ASLI dari Binance
               produksi publik (REST + WebSocket), tanpa API key. Saldo/order
               virtual disimpan ke file. (default, aman)
    "LIVE"  -> order sungguhan ke Binance produksi (uang asli)
Mode dipilih lewat tab Kontrol dan disimpan di pump_bot_runtime.json. Nilai
config.py tetap menjadi default immutable.
Keduanya memakai jalur LOGIKA STRATEGI yang SAMA PERSIS lewat antarmuka
ExchangeClient; yang berbeda hanya lapisan eksekusi order dan sumber saldo.

File state, log, dan kontrol otomatis DIPISAH per mode (contoh:
pump_bot_state_paper.json vs pump_bot_state_live.json), dihitung di
config.py, jadi data posisi/riwayat PAPER dan LIVE tidak pernah tercampur.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
import uuid

from binance_client import (
    BinanceAPIError, SymbolFilters, build_filters_cache, build_trading_symbols,
)
from exchange_client import ExchangeClient, create_exchange_client
from config import (
    PUMP_CONFIG, CONFIG_LOAD_ERRORS, get_base_url, is_paper, require_valid_mode,
)
import market_scanner as scanner
import state as state_mod
import strategy


logger = logging.getLogger("pump_bot")
_shutdown_requested = False
_shutdown_event = threading.Event()

DEFAULT_STATE = {
    "current_symbol": None,
    "entry_price": 0.0,
    "qty": 0.0,
    "entry_time": 0,
    "be_active": False,
    "be_stop_price": 0.0,
    "trailing_active": False,
    "trailing_stop_price": 0.0,
    # Level exit yang DIKUNCI saat entry (lihat open_position). 0.0 berarti
    # belum di-set, dan manage_exit akan jatuh ke SL_PCT/TP_PCT config.
    "sl_pct": 0.0,
    "tp_pct": 0.0,
    "be_trigger_pct": 0.0,
    "be_lock_pct": 0.0,
    "trail_start_pct": 0.0,
    "trail_step_pct": 0.0,
    "exit_source": "",
    "atr_pct_at_entry": 0.0,
    # Level setup yang DIKUNCI saat entry untuk exit SETUP_INVALIDATED.
    # Sengaja tidak dihitung ulang dari data baru: alasan keluar harus sama
    # dengan alasan masuk. 0.0 berarti tidak tersedia (posisi lama dari versi
    # sebelumnya), dan exit invalidasi otomatis dilewati untuk posisi itu.
    "breakout_level": 0.0,
    "setup_invalidation_price": 0.0,
    "atr_abs_at_entry": 0.0,
    # close_time candle terakhir yang sudah diperiksa untuk invalidasi setup,
    # supaya satu candle tidak diperiksa berulang dan REST tidak dipanggil
    # lebih sering daripada satu kali per candle.
    "last_setup_check_close_time": 0,
    "last_scan_time": 0,
    "cooldown_until": 0,
    "last_trade_time": 0,
    "day_start_equity": None,
    "day_start_date": None,
    "peak_equity": None,
    "dd_stopped": False,
    "dd_stop_until": 0,
    "daily_stopped": False,
    # Penanda episode CLOSE_ALL_AT_LIMIT: True = posisi sudah ditutup paksa
    # oleh kill switch pada episode stop yang sedang berjalan, supaya tidak
    # ditutup berulang kali. Direset saat episode stop berakhir.
    "_limit_close_done": False,
    # Penghitung kegagalan SELL berturut-turut untuk eskalasi alarm (lihat
    # close_position). Direset ke 0 saat entry baru atau SELL berhasil.
    "sell_fail_count": 0,
    # Intent order disimpan SEBELUM request dikirim. Jika proses mati atau
    # respons jaringan hilang setelah exchange menerima order, startup dapat
    # menanyakannya kembali lewat clientOrderId dan tidak menganggap posisi
    # nyata sebagai state kosong.
    "pending_order": None,
    # Posisi/aset yang tidak dapat dipetakan aman ke state bot memblokir entry
    # baru sampai operator melakukan rekonsiliasi, bukan diabaikan diam-diam.
    "reconciliation_required": False,
    "reconciliation_assets": [],
}



def setup_logging(config: dict) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    file_handler = logging.handlers.RotatingFileHandler(
        config["LOG_FILE"], maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def _handle_signal(signum, frame):
    global _shutdown_requested
    logger.info("Menerima sinyal berhenti (%s). Bot akan berhenti setelah iterasi ini selesai.", signum)
    _shutdown_requested = True
    _shutdown_event.set()


def load_pump_state(path: str) -> dict:
    if not os.path.exists(path):
        return dict(DEFAULT_STATE)
    raw = state_mod.load_state(path)
    merged = dict(DEFAULT_STATE)
    merged.update(raw)
    return merged


def get_balance(account: dict, asset: str) -> float:
    """Saldo free aset, dipertahankan untuk caller yang akan mengirim MARKET."""
    for b in account.get("balances", []):
        if b.get("asset") == asset:
            return float(b.get("free", 0.0))
    return 0.0


def get_total_balance(account: dict, asset: str) -> float:
    """Saldo free + locked untuk rekonsiliasi kepemilikan aset.

    Aset locked tetap milik akun. Menganggapnya nol akan membuat limit SELL
    manual terlihat seperti posisi hilang lalu state bot dihapus salah.
    """
    for b in account.get("balances", []):
        if b.get("asset") == asset:
            return float(b.get("free", 0.0)) + float(b.get("locked", 0.0))
    return 0.0


def get_equity(client: ExchangeClient, config: dict, state: dict) -> "float | None":
    """Equity total (USDT free + nilai posisi saat ini).

    Return None kalau harga posisi TIDAK bisa diambil dari API. Ini disengaja
    (perbaikan audit 2026-09-24, temuan T-05): versi lama menelan error dan
    mengembalikan equity TANPA nilai posisi, sehingga gangguan API 15 detik
    membuat drawdown semu mendekati 100% dan bisa salah memicu kill switch.
    Lebih baik melewati satu iterasi evaluasi risiko daripada menghitung dari
    angka yang keliru.
    """
    account = client.get_account()
    usdt_free = get_balance(account, config["QUOTE_ASSET"])
    if state["current_symbol"] and state["qty"] > 0:
        try:
            price = client.get_price(state["current_symbol"])
        except BinanceAPIError as exc:
            logger.warning(
                "Harga %s tidak bisa diambil untuk hitung equity (%s). "
                "Evaluasi batas risiko dilewati satu iterasi.",
                state["current_symbol"], exc,
            )
            return None
        usdt_free += state["qty"] * price
    return usdt_free


def update_equity_controls(state: dict, equity: float, config: dict) -> bool:
    today = state_mod.today_str()
    if state.get("day_start_date") != today:
        state["day_start_date"] = today
        state["day_start_equity"] = equity
        state["daily_stopped"] = False
        logger.info("Hari baru (UTC): %s. Equity awal hari = %.2f %s", today, equity, config["QUOTE_ASSET"])

    if state.get("peak_equity") is None or equity > state["peak_equity"]:
        state["peak_equity"] = equity

    if config["USE_EQUITY_STOP"] and not state.get("dd_stopped") and state["peak_equity"]:
        dd_pct = (state["peak_equity"] - equity) / state["peak_equity"] * 100.0
        if dd_pct >= config["MAX_DRAWDOWN_PERCENT"]:
            state["dd_stopped"] = True
            state["dd_stop_until"] = state_mod.now_ms() + config["DD_COOLDOWN_HOURS"] * 3600 * 1000
            logger.critical("STOP DRAWDOWN: turun %.2f%% dari puncak equity. Entry baru dijeda %d jam.",
                             dd_pct, config["DD_COOLDOWN_HOURS"])

    if state.get("dd_stopped") and state.get("dd_stop_until", 0) and state_mod.now_ms() >= state["dd_stop_until"]:
        state["dd_stopped"] = False
        state["peak_equity"] = equity
        logger.info("Cooldown drawdown selesai. Entry baru diaktifkan lagi.")

    if config.get("USE_DAILY_STOP", True) and not state.get("daily_stopped") and state.get("day_start_equity"):
        change_pct = (equity - state["day_start_equity"]) / state["day_start_equity"] * 100.0
        if change_pct <= -config["MAX_DAILY_LOSS_PERCENT"]:
            state["daily_stopped"] = True
            logger.warning("STOP HARIAN: rugi harian %.2f%%. Tidak ada entry baru sampai hari berikutnya (UTC).", change_pct)
        elif change_pct >= config["DAILY_PROFIT_TARGET_PERCENT"]:
            state["daily_stopped"] = True
            logger.info("TARGET HARIAN TERCAPAI: profit harian %.2f%%. Tidak ada entry baru sampai hari berikutnya (UTC).", change_pct)

    return bool(state.get("dd_stopped") or state.get("daily_stopped"))


def maybe_force_close_at_risk_limit(client: ExchangeClient, config: dict,
                                     filters_cache: dict, state: dict,
                                     entries_paused: bool, current_price) -> None:
    """Implementasi CLOSE_ALL_AT_LIMIT (perbaikan audit 2026-09-24, temuan
    T-06: parameter ini sebelumnya tidak pernah dibaca kode sama sekali).

    Saat kill switch (drawdown stop / daily stop) AKTIF dan config meminta,
    posisi terbuka ditutup paksa SATU KALI per episode stop -- bukan hanya
    menjeda entry baru. Penanda _limit_close_done mencegah penutupan berulang
    dan direset otomatis begitu episode stop selesai (cooldown DD habis atau
    hari UTC berganti).

    Dipisah jadi fungsi kecil supaya bisa diuji di selftest tanpa menjalankan
    loop utama.
    """
    limit_now = bool(state.get("dd_stopped") or state.get("daily_stopped"))
    if (
        config.get("CLOSE_ALL_AT_LIMIT")
        and entries_paused
        and limit_now
        and not state.get("_limit_close_done")
        and state["current_symbol"]
        and current_price is not None
    ):
        logger.critical(
            "CLOSE_ALL_AT_LIMIT: limit risiko tercapai, posisi %s ditutup paksa di harga pasar.",
            state["current_symbol"],
        )
        close_position(client, config, filters_cache, state, "RISK_LIMIT_TRIGGERED")
        state["_limit_close_done"] = True

    if not entries_paused and state.get("_limit_close_done"):
        state["_limit_close_done"] = False


def _restore_pending_buy(config: dict, state: dict, pending: dict, order: dict,
                         account: dict) -> bool:
    """Pulihkan state minimal dari BUY yang terisi tetapi responsnya hilang."""
    executed = float(order.get("executedQty", 0.0) or 0.0)
    quoted = float(order.get("cummulativeQuoteQty", 0.0) or 0.0)
    symbol = str(pending.get("symbol") or order.get("symbol") or "")
    if not symbol or executed <= 0 or quoted <= 0:
        return False
    quote = config["QUOTE_ASSET"]
    if not symbol.endswith(quote):
        return False
    base = symbol[: -len(quote)]
    # PAPER dapat memotong fee dari base. Untuk pengelolaan posisi, jangan
    # pernah menyimpan qty lebih besar dari saldo free yang benar-benar bisa
    # dijual sekarang.
    qty = min(executed, get_balance(account, base))
    if qty <= 0:
        return False
    levels = pending.get("levels") if isinstance(pending.get("levels"), dict) else {}
    setup = pending.get("setup") if isinstance(pending.get("setup"), dict) else {}
    state["current_symbol"] = symbol
    state["entry_price"] = quoted / executed
    state["qty"] = qty
    state["entry_time"] = int(order.get("transactTime") or state_mod.now_ms())
    state["be_active"] = False
    state["be_stop_price"] = 0.0
    state["trailing_active"] = False
    state["trailing_stop_price"] = 0.0
    for key in ("sl_pct", "tp_pct", "be_trigger_pct", "be_lock_pct",
                "trail_start_pct", "trail_step_pct", "exit_source", "atr_pct_at_entry"):
        if key in levels:
            state[key] = levels[key]
    state["breakout_level"] = float(setup.get("breakout_level") or 0.0)
    state["setup_invalidation_price"] = float(setup.get("invalidation_price") or 0.0)
    state["atr_abs_at_entry"] = float(setup.get("atr_abs") or 0.0)
    state["last_setup_check_close_time"] = 0
    state["last_trade_time"] = state_mod.now_ms()
    state["sell_fail_count"] = 0
    return True


def reconcile_state_with_exchange(client: ExchangeClient, config: dict, state: dict) -> None:
    """Selaraskan intent/state posisi dengan saldo exchange saat startup.

    Rekonsiliasi tidak hanya menangani state yang terlalu besar. Ia juga
    memulihkan BUY ber-intent yang responsnya hilang, menghitung saldo locked
    sebagai kepemilikan, dan memblokir entry bila ada aset base yang tidak dapat
    dipetakan aman ke posisi bot.
    """
    quote = config["QUOTE_ASSET"]
    try:
        account = client.get_account()
    except BinanceAPIError as exc:
        logger.warning(
            "Rekonsiliasi startup dilewati (gagal ambil saldo: %s). "
            "State lama dipakai apa adanya; entry baru tidak akan berjalan bila ada intent order.",
            exc,
        )
        return

    changed = False
    pending = state.get("pending_order")
    if isinstance(pending, dict) and pending.get("client_order_id"):
        symbol = str(pending.get("symbol") or "")
        try:
            order = client.get_order(symbol,
                                     orig_client_order_id=pending["client_order_id"])
        except BinanceAPIError as exc:
            # -2013 berarti order tidak pernah tercatat di exchange. Error lain
            # tidak boleh menghapus intent karena statusnya masih tidak pasti.
            if getattr(exc, "code", None) == -2013:
                logger.warning("Intent %s untuk %s tidak ditemukan di exchange; dibersihkan.",
                               pending.get("side"), symbol)
                state["pending_order"] = None
                changed = True
            else:
                state["reconciliation_required"] = True
                state["reconciliation_assets"] = [symbol] if symbol else []
                changed = True
                logger.critical("Intent order %s belum dapat diverifikasi (%s). Entry baru diblokir.",
                                symbol, exc)
        else:
            side = str(pending.get("side") or order.get("side") or "").upper()
            status = str(order.get("status") or "").upper()
            if side == "BUY" and float(order.get("executedQty", 0.0) or 0.0) > 0:
                if _restore_pending_buy(config, state, pending, order, account):
                    logger.critical("BUY %s dipulihkan dari intent/order setelah respons hilang.", symbol)
                    state["reconciliation_required"] = False
                    state["reconciliation_assets"] = []
                else:
                    state["reconciliation_required"] = True
                    state["reconciliation_assets"] = [symbol] if symbol else []
                state["pending_order"] = None
                changed = True
            elif status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
                # SELL finalized akan diselaraskan dengan saldo di bawah.
                state["pending_order"] = None
                changed = True
            else:
                state["reconciliation_required"] = True
                state["reconciliation_assets"] = [symbol] if symbol else []
                changed = True

    symbol = state.get("current_symbol")
    qty_state = float(state.get("qty") or 0.0)
    if symbol and qty_state > 0 and symbol.endswith(quote):
        base_asset = symbol[: -len(quote)]
        total_base = get_total_balance(account, base_asset)
        free_base = get_balance(account, base_asset)
        if total_base <= 0:
            logger.warning(
                "REKONSILIASI: state bilang pegang %s qty=%.8f, tapi saldo total %s = 0. "
                "Posisi hantu direset.", symbol, qty_state, base_asset,
            )
            reset_position(state)
            changed = True
        elif total_base < qty_state:
            logger.warning(
                "REKONSILIASI: qty state %s (%.8f) lebih besar dari saldo total nyata %.8f. "
                "Qty disesuaikan.", symbol, qty_state, total_base,
            )
            state["qty"] = total_base
            changed = True
        if free_base <= 0 and total_base > 0:
            # Ada order manual/open order. Jangan reset posisi dan jangan
            # berpura-pura MARKET SELL dapat mengelolanya.
            state["reconciliation_required"] = True
            state["reconciliation_assets"] = [base_asset]
            changed = True
            logger.critical("%s seluruhnya locked. Entry baru diblokir sampai order manual direkonsiliasi.",
                            base_asset)
    elif not state.get("current_symbol") and not state.get("pending_order"):
        # State kosong tidak cukup untuk menyimpulkan akun kosong. Base asset
        # yang tersisa bisa berasal dari BUY yang respons/state-nya hilang.
        foreign = []
        for bal in account.get("balances", []):
            asset = str(bal.get("asset") or "")
            if not asset or asset in (quote, "BNB"):
                continue
            try:
                amount = float(bal.get("free", 0.0)) + float(bal.get("locked", 0.0))
            except (TypeError, ValueError):
                continue
            if amount > 0:
                foreign.append(asset)
        if foreign:
            state["reconciliation_required"] = True
            state["reconciliation_assets"] = sorted(set(foreign))
            changed = True
            logger.critical("State kosong tetapi akun masih punya aset base %s. Entry baru diblokir sampai rekonsiliasi manual.",
                            ", ".join(state["reconciliation_assets"]))

    if changed:
        state_mod.save_state(config["STATE_FILE"], state)


# Cache permanen usia listing per simbol (usia tidak pernah menyusut),
# supaya hanya kandidat BARU yang memakan 1 panggilan klines (weight 2).
_listing_age_cache: dict = {}


def listing_age_days(client: ExchangeClient, symbol: str, now_ms: int) -> float:
    """Usia pair sejak candle harian pertamanya, dalam hari (perbaikan audit
    2026-09-24, temuan S-08).

    Dipakai untuk menolak entry ke koin yang baru listing: riwayat tipis,
    spread lebar, dan fase pump artifisial "hari listing" yang sering langsung
    kolaps. Dipanggil HANYA untuk kandidat yang sudah lolos konfirmasi entry.
    """
    if symbol in _listing_age_cache:
        return _listing_age_cache[symbol]
    raw = client.get_klines(symbol, "1d", limit=1, start_time_ms=0)
    age = 0.0 if not raw else max(0.0, (now_ms - int(raw[0][0])) / 86_400_000.0)
    _listing_age_cache[symbol] = age
    return age


def reset_position(state: dict) -> None:
    state["current_symbol"] = None
    state["entry_price"] = 0.0
    state["qty"] = 0.0
    state["entry_time"] = 0
    state["be_active"] = False
    state["be_stop_price"] = 0.0
    state["trailing_active"] = False
    state["trailing_stop_price"] = 0.0
    state["sl_pct"] = 0.0
    state["tp_pct"] = 0.0
    state["be_trigger_pct"] = 0.0
    state["be_lock_pct"] = 0.0
    state["trail_start_pct"] = 0.0
    state["trail_step_pct"] = 0.0
    state["exit_source"] = ""
    state["atr_pct_at_entry"] = 0.0
    state["breakout_level"] = 0.0
    state["setup_invalidation_price"] = 0.0
    state["atr_abs_at_entry"] = 0.0
    state["last_setup_check_close_time"] = 0
    state["pending_order"] = None


def try_dust_sweep(client: ExchangeClient, config: dict, symbol: "str | None") -> None:
    """Dipanggil SETELAH posisi `symbol` ditutup (SL/TP/BE/Trailing/manual/dsb)
    untuk mengecek apakah masih ada sisa saldo kecil (dust) dari koin itu di
    akun -- biasanya muncul karena pembulatan qty ke LOT_SIZE bursa, atau
    sisa yang tidak lolos MIN_NOTIONAL saat dijual. Kalau Binance mengakui
    sisa itu sebagai "dust convertible", langsung dikonversi ke BNB lewat
    endpoint resmi POST /sapi/v1/asset/dust (dicek developers.binance.com
    2026-09-23).

    PROTEKSI KERAS terhadap modal (TIDAK bisa dimatikan lewat config,
    disengaja demi keamanan dana):
    1. HANYA base asset dari `symbol` yang BARU SAJA ditutup yang pernah
       disentuh -- TIDAK PERNAH "menyapu semua aset kecil di akun" secara
       serampangan. Ambang "dust" Binance (nilainya < 0.001 BTC, bisa
       100+ USD tergantung harga BTC) jauh lebih besar dari modal trading
       kecil bot ini, jadi kalau modal USDT/BNB ikut disapu, bot bisa
       kehabisan modal untuk trading berikutnya.
    2. Quote asset (USDT) dan BNB itu sendiri SELALU dikecualikan secara
       eksplisit di kode ini, apa pun isi config -- bukan cuma "defaultnya
       tidak termasuk", tapi memang tidak mungkin lolos pengecekan di bawah.
    3. Di mode PAPER, fungsi ini otomatis DILEWATI: konversi dust memakai
       Network hanya menyediakan endpoint /api/*, sedangkan dust convert
       memakai /sapi/v1/asset/dust yang memang tidak ada di sana (sumber:
       endpoint /sapi/* yang BERTANDA TANGAN, sedangkan PAPER dilarang keras
       mengirim request bertanda tangan. Bukan bagian dari simulasi, jadi tidak
       dipanggil sama sekali dan cukup dicatat di log.
    4. Kegagalan (rate limit Binance untuk endpoint ini -- dilaporkan sekitar
       tiap 6-24 jam sekali per akun, aset tidak/belum diakui sebagai dust,
       dsb) SELALU ditangani sebagai hal wajar (dicoba lagi di kesempatan
       berikutnya), bukan dianggap error yang menghentikan bot.
    """
    if not config.get("USE_DUST_SWEEP", True):
        return
    if not symbol:
        return
    quote_asset = config["QUOTE_ASSET"]
    if not symbol.endswith(quote_asset):
        return
    base_asset = symbol[: -len(quote_asset)]
    if not base_asset or base_asset in (quote_asset, "BNB"):
        # Proteksi keras: tidak pernah convert quote asset (modal) atau BNB itu sendiri.
        return

    if is_paper(config):
        logger.info(
            "[PAPER] Dust sweep dilewati untuk %s: konversi dust memakai endpoint "
            "/sapi/* bertanda tangan yang dilarang di mode PAPER. Di mode LIVE fitur ini tetap jalan.",
            base_asset,
        )
        return

    try:
        convertible = client.get_dust_convertible()
    except BinanceAPIError as exc:
        logger.warning("Dust sweep: gagal ambil daftar aset convertible (%s). Dilewati, dicoba lagi nanti.", exc)
        return

    details = convertible.get("details", []) if isinstance(convertible, dict) else []
    match = next((d for d in details if d.get("asset") == base_asset), None)
    if not match:
        # Wajar: sisa saldo mungkin nol, atau di atas/bawah ambang dust
        # Binance saat ini, atau datanya belum "segar". Bukan error.
        logger.info("Dust sweep: %s tidak (lagi) terdaftar sebagai dust convertible saat ini, dilewati.",
                     base_asset)
        return

    try:
        result = client.convert_dust([base_asset])
    except BinanceAPIError as exc:
        # Termasuk rate limit endpoint dust Binance (per akun, bukan dibatasi
        # kode ini) -- SEMUA ditangani sebagai "coba lagi nanti", bukan bug.
        logger.warning(
            "Dust sweep %s -> BNB gagal (%s). Sisa saldo dibiarkan, dicoba lagi di kesempatan berikutnya.",
            base_asset, exc,
        )
        return

    transferred = result.get("totalTransfered", "0") if isinstance(result, dict) else "0"
    logger.info("DUST SWEEP OK: sisa %s dikonversi ke %s BNB (sudah dikurangi biaya layanan Binance).",
                base_asset, transferred)


def _new_client_order_id(prefix: str) -> str:
    """ID singkat, unik, dan dapat dipakai untuk recovery order Binance."""
    return f"pump-{prefix}-{uuid.uuid4().hex[:24]}"


def _submit_market_order(client: ExchangeClient, symbol: str, side: str, quantity: float,
                         client_order_id: str) -> dict:
    """Kirim market order dengan idempotency key.

    Fallback TypeError hanya untuk fake client lama di selftest. Implementasi
    ExchangeClient nyata mendukung argumen ini.
    """
    try:
        return client.new_market_order(symbol, side, quantity=quantity,
                                       new_client_order_id=client_order_id)
    except TypeError:
        return client.new_market_order(symbol, side, quantity=quantity)


def close_position(client: ExchangeClient, config: dict, filters_cache: dict,
                   state: dict, reason: str) -> None:
    symbol = state["current_symbol"]
    if not symbol:
        return

    pending = state.get("pending_order")
    if isinstance(pending, dict):
        logger.critical("SELL %s (%s) belum dapat dikonfirmasi (intent %s). Tidak mengirim SELL duplikat.",
                        symbol, reason, pending.get("client_order_id"))
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        return

    filters = filters_cache.get(symbol)
    qty_to_sell = float(state["qty"])
    base_asset = symbol[: -len(config["QUOTE_ASSET"])]

    try:
        account = client.get_account()
        free_base = get_balance(account, base_asset)
        qty_to_sell = min(qty_to_sell, free_base)
    except BinanceAPIError as exc:
        logger.error("Gagal ambil saldo sebelum SELL %s: %s", symbol, exc)

    if filters:
        qty_to_sell = filters.round_qty(qty_to_sell)
        if qty_to_sell < float(filters.min_qty):
            logger.warning("Qty jual %s (%.8f) di bawah minQty bursa. Posisi direset sebagai dust.",
                           symbol, qty_to_sell)
            reset_position(state)
            state["sell_fail_count"] = 0
            state["cooldown_until"] = state_mod.now_ms() + config["COOLDOWN_MINUTES_AFTER_CLOSE"] * 60 * 1000
            state_mod.save_state(config["STATE_FILE"], state)
            try_dust_sweep(client, config, symbol)
            return

    if qty_to_sell <= 0:
        logger.critical("SELL %s (%s) tidak dikirim karena qty yang bisa dijual nol. Rekonsiliasi manual diperlukan.",
                        symbol, reason)
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [base_asset]
        state_mod.save_state(config["STATE_FILE"], state)
        return

    entry_price = state["entry_price"]
    client_order_id = _new_client_order_id("sell")
    state["pending_order"] = {
        "side": "SELL", "symbol": symbol, "qty": qty_to_sell,
        "client_order_id": client_order_id, "reason": reason,
        "created_at": state_mod.now_ms(),
    }
    state_mod.save_state(config["STATE_FILE"], state)

    try:
        resp = _submit_market_order(client, symbol, "SELL", qty_to_sell, client_order_id)
    except BinanceAPIError as exc:
        # Respons gagal dapat berarti exchange sudah menerima order. Intent
        # sengaja dipertahankan dan entry baru diblokir sampai get_order()
        # merekonsiliasinya, bukan mencoba SELL kedua secara buta.
        state["sell_fail_count"] = int(state.get("sell_fail_count", 0)) + 1
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [base_asset]
        n = state["sell_fail_count"]
        logger.critical("SELL %s (%s) status tidak pasti (%d): %s. Intent disimpan; jangan kirim order duplikat.",
                        symbol, reason, n, exc)
        state_mod.save_state(config["STATE_FILE"], state)
        if n == 5:
            try:
                info = client.get_exchange_info(symbol)
                syms = info.get("symbols", []) if isinstance(info, dict) else []
                if syms:
                    filters_cache[symbol] = SymbolFilters.from_symbol_data(syms[0])
            except BinanceAPIError as refresh_exc:
                logger.warning("Penyegaran filter %s juga gagal: %s", symbol, refresh_exc)
        return

    # Respons diterima, sehingga intent tidak lagi ambigu walau fill parsial.
    state["pending_order"] = None
    executed_qty = max(0.0, float(resp.get("executedQty", 0.0) or 0.0))
    cumm_quote = max(0.0, float(resp.get("cummulativeQuoteQty", 0.0) or 0.0))
    status = str(resp.get("status") or "").upper()
    sell_price = (cumm_quote / executed_qty) if executed_qty > 0 else 0.0
    pnl = (sell_price - entry_price) * executed_qty if entry_price > 0 else 0.0

    remaining = max(0.0, float(state["qty"]) - executed_qty)
    try:
        post_account = client.get_account()
        remaining = min(remaining, get_balance(post_account, base_asset))
    except BinanceAPIError:
        # Pengurangan dari qty state tetap lebih aman daripada reset penuh.
        pass

    min_qty = float(filters.min_qty) if filters else 0.0
    fully_closed = executed_qty >= qty_to_sell - 1e-12 and (remaining <= 0 or remaining < min_qty)
    if fully_closed:
        logger.info("SELL FILLED %s (%s): qty=%.8f @ avg %.6f | entry=%.6f | estimasi PnL=%.2f %s",
                    symbol, reason, executed_qty, sell_price, entry_price, pnl, config["QUOTE_ASSET"])
        reset_position(state)
        state["sell_fail_count"] = 0
        state["cooldown_until"] = state_mod.now_ms() + config["COOLDOWN_MINUTES_AFTER_CLOSE"] * 60 * 1000
        state["last_trade_time"] = state_mod.now_ms()
        state_mod.save_state(config["STATE_FILE"], state)
        try_dust_sweep(client, config, symbol)
        return

    # Market order dapat EXPIRED dengan partial fill. Posisi harus tetap ada
    # agar SL/TP/recovery berikutnya tahu aset yang belum terjual.
    state["qty"] = remaining
    state["sell_fail_count"] = 0
    state["reconciliation_required"] = False
    state["reconciliation_assets"] = []
    state_mod.save_state(config["STATE_FILE"], state)
    logger.critical("SELL PARTIAL %s (%s): status=%s filled=%.8f dari %.8f, sisa state=%.8f. Posisi TIDAK direset.",
                    symbol, reason, status or "UNKNOWN", executed_qty, qty_to_sell, remaining)


def open_position(client: ExchangeClient, config: dict, filters_cache: dict,
                   state: dict, candidate: "scanner.Candidate",
                   klines: "list | None" = None,
                   reference_price: "float | None" = None) -> None:
    filters = filters_cache.get(candidate.symbol)
    if filters is None:
        logger.warning("Tidak ada data filter untuk %s, entry dilewati.", candidate.symbol)
        return

    # Harga acuan ukuran posisi (temuan S-07): utamakan harga ASK segar dari
    # bookTicker yang baru diambil pemanggil, bukan candidate.last_price dari
    # ticker 24 jam yang bisa sudah beberapa menit basi saat order dikirim.
    # Pada koin pump yang bergerak cepat, bedanya menentukan apakah cek
    # qty/MIN_NOTIONAL masih valid di harga eksekusi riil.
    price_ref = reference_price if (reference_price and reference_price > 0) else candidate.last_price

    # Kedua mode sizing perlu saldo aktual: mode persen menghitung proporsi,
    # mode fixed perlu ditolak sebelum order bila nominal melebihi saldo.
    try:
        account = client.get_account()
    except BinanceAPIError as exc:
        logger.error("Gagal ambil saldo sebelum BUY %s: %s. Entry dilewati.", candidate.symbol, exc)
        return
    usdt_free = get_balance(account, config["QUOTE_ASSET"])
    sizing = strategy.resolve_position_notional(config, usdt_free)
    usdt_amount = sizing["notional"]
    if sizing["cap_active"]:
        asal = (f"RISK_PERCENT={float(config.get('RISK_PERCENT', 0) or 0):.2f}%"
                if sizing["mode"] == "PERCENT"
                else f"POSITION_SIZE_USDT={float(config.get('POSITION_SIZE_USDT', 0) or 0):.2f}")
        logger.warning(
            "Ukuran posisi %s dipotong plafon MAX_POSITION_USDT: %.2f -> %.2f %s. "
            "Sumber nominal=%s; eksposur efektif %.2f%% dari saldo free %.2f.",
            candidate.symbol, sizing["requested_notional"], usdt_amount,
            config["QUOTE_ASSET"], asal, sizing["effective_pct_of_free"], usdt_free,
        )
    if usdt_amount > usdt_free:
        logger.warning("Entry %s dilewati: nominal %.2f %s melebihi saldo free %.2f %s.",
                       candidate.symbol, usdt_amount, config["QUOTE_ASSET"],
                       usdt_free, config["QUOTE_ASSET"])
        return

    qty = filters.round_qty(usdt_amount / price_ref)
    notional = qty * price_ref
    if qty < float(filters.min_qty) or notional < float(filters.min_notional):
        logger.warning(
            "Entry %s dilewati: qty/notional di bawah batas bursa (qty=%.8f, notional=%.2f, "
            "minQty=%.8f, minNotional=%.2f). Nominal order yang dihitung bot cuma %.4f %s -- "
            "kalau ini jauh lebih kecil dari perkiraan Anda, cek saldo Spot wallet Anda di "
            "Binance (Wallet > Overview), pastikan USDT ada di Spot bukan di Funding/Earn.",
            candidate.symbol, qty, notional, float(filters.min_qty), float(filters.min_notional),
            usdt_amount, config["QUOTE_ASSET"],
        )
        return

    # Simpan intent dan preview level SEBELUM request. Bila respons hilang
    # setelah exchange mengisi BUY, startup dapat memulihkan posisi dengan
    # clientOrderId tanpa menganggap akun kosong.
    setup = getattr(candidate, "setup", None)
    preview = strategy.resolve_exit_levels(config, klines, price_ref)
    client_order_id = _new_client_order_id("buy")
    state["pending_order"] = {
        "side": "BUY", "symbol": candidate.symbol, "qty": qty,
        "client_order_id": client_order_id, "created_at": state_mod.now_ms(),
        "levels": {
            "sl_pct": preview["sl_pct"], "tp_pct": preview["tp_pct"],
            "be_trigger_pct": preview["be_trigger_pct"], "be_lock_pct": preview["be_lock_pct"],
            "trail_start_pct": preview["trail_start_pct"], "trail_step_pct": preview["trail_step_pct"],
            "exit_source": preview["source"], "atr_pct_at_entry": preview["atr_pct"] or 0.0,
        },
        "setup": {
            "breakout_level": float(getattr(setup, "breakout_level", 0.0) or 0.0),
            "invalidation_price": float(getattr(setup, "invalidation_price", 0.0) or 0.0),
            "atr_abs": float(getattr(setup, "atr_abs", 0.0) or 0.0),
        },
    }
    pending_intent = dict(state["pending_order"])
    state_mod.save_state(config["STATE_FILE"], state)
    try:
        resp = _submit_market_order(client, candidate.symbol, "BUY", qty, client_order_id)
    except BinanceAPIError as exc:
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [candidate.symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical("Order BUY %s status tidak pasti: %s. Intent disimpan dan entry baru diblokir sampai rekonsiliasi.",
                        candidate.symbol, exc)
        return

    state["pending_order"] = None
    executed_qty = float(resp.get("executedQty", 0.0))
    cumm_quote = float(resp.get("cummulativeQuoteQty", 0.0))
    if executed_qty <= 0 or cumm_quote <= 0:
        # Respons diterima tetapi belum cukup untuk membangun posisi. Simpan
        # intent agar startup dapat memeriksa get_order(), bukan scan lagi.
        state["pending_order"] = pending_intent
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [candidate.symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical("Order BUY %s tidak memberi fill lengkap. Intent dipertahankan untuk rekonsiliasi: %s",
                        candidate.symbol, resp)
        return
    fill_price = cumm_quote / executed_qty

    logger.info(
        "BUY FILLED %s: qty=%.8f @ avg %.6f | 24h=%.2f%% | vol24h=%.0f | alasan: %s",
        candidate.symbol, executed_qty, fill_price, candidate.price_change_pct,
        candidate.quote_volume, candidate.confirm_reason,
    )
    # Fee BUY dapat dipotong dari base asset. Selaraskan qty state dengan
    # saldo yang benar-benar bisa dijual agar equity dan close tidak memakai
    # executedQty gross secara keliru, terutama di PAPER.
    base_asset = candidate.symbol[: -len(config["QUOTE_ASSET"])]
    managed_qty = executed_qty
    try:
        managed_qty = min(executed_qty, get_balance(client.get_account(), base_asset))
    except BinanceAPIError:
        pass
    if managed_qty <= 0:
        state["pending_order"] = pending_intent
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [candidate.symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical("BUY %s terisi tetapi saldo base tidak dapat dikonfirmasi. Entry diblokir sampai rekonsiliasi.",
                        candidate.symbol)
        return

    state["current_symbol"] = candidate.symbol
    state["entry_price"] = fill_price
    state["qty"] = managed_qty
    state["entry_time"] = state_mod.now_ms()
    state["last_trade_time"] = state_mod.now_ms()
    state["be_active"] = False
    state["be_stop_price"] = 0.0
    state["trailing_active"] = False
    state["trailing_stop_price"] = 0.0
    state["reconciliation_required"] = False
    state["reconciliation_assets"] = []

    # Level exit dihitung SEKALI di sini lalu DIKUNCI di state, memakai harga
    # fill sungguhan sebagai acuan. Sengaja tidak dihitung ulang tiap iterasi:
    # ATR bergerak, dan stop yang ikut bergerak TURUN setelah posisi dibuka
    # berarti risiko per-trade membengkak diam-diam setelah Anda sudah
    # berkomitmen. Stop hanya boleh mengetat lewat Breakeven/Trailing, tidak
    # pernah melonggar.
    levels = strategy.resolve_exit_levels(config, klines, fill_price)
    state["sl_pct"] = levels["sl_pct"]
    state["tp_pct"] = levels["tp_pct"]
    state["be_trigger_pct"] = levels["be_trigger_pct"]
    state["be_lock_pct"] = levels["be_lock_pct"]
    state["trail_start_pct"] = levels["trail_start_pct"]
    state["trail_step_pct"] = levels["trail_step_pct"]
    state["exit_source"] = levels["source"]
    state["atr_pct_at_entry"] = levels["atr_pct"] or 0.0
    state["sell_fail_count"] = 0

    # Level setup dikunci di sini, sekali, dari hasil deteksi yang MEMICU
    # entry ini. Kalau dihitung ulang belakangan dari candle yang lebih baru,
    # level bisa bergeser dan posisi ditutup karena alasan yang tidak pernah
    # menjadi dasar entry.
    setup = getattr(candidate, "setup", None)
    if setup is not None and setup.breakout_level and setup.invalidation_price:
        state["breakout_level"] = float(setup.breakout_level)
        state["setup_invalidation_price"] = float(setup.invalidation_price)
        state["atr_abs_at_entry"] = float(setup.atr_abs or 0.0)
        # RELASI DENGAN STOP LOSS: level invalidasi berada di bawah level
        # breakout, sedangkan Stop Loss dihitung dari harga entry. Keduanya
        # sengaja dibiarkan berdiri sendiri, tetapi kalau Stop Loss jauh lebih
        # longgar daripada level invalidasi, praktis exit invalidasi yang
        # selalu lebih dulu bekerja. Log di bawah membuat hubungan itu
        # terlihat, bukan tersembunyi.
        sl_price = fill_price * (1 - levels["sl_pct"] / 100.0)
        logger.info(
            "%s: setup dikunci -> level %.8g, batas invalidasi %.8g, harga Stop Loss %.8g "
            "(%s yang lebih dulu tersentuh akan menutup posisi)",
            candidate.symbol, state["breakout_level"], state["setup_invalidation_price"],
            sl_price,
            "invalidasi setup" if state["setup_invalidation_price"] > sl_price else "Stop Loss",
        )
    else:
        state["breakout_level"] = 0.0
        state["setup_invalidation_price"] = 0.0
        state["atr_abs_at_entry"] = 0.0
        logger.warning("%s: level setup tidak tersedia saat entry, exit SETUP_INVALIDATED "
                       "dilewati untuk posisi ini.", candidate.symbol)
    state["last_setup_check_close_time"] = 0
    # Simpan SEKARANG (temuan T-04), jangan menunggu akhir iterasi loop:
    # crash beberapa ratus milidetik setelah BUY FILLED tidak boleh
    # meninggalkan POSISI YATIM (ada di exchange, tapi state di disk masih
    # kosong sehingga bot restart tanpa tahu posisi ini ada dan tanpa SL/TP).
    state_mod.save_state(config["STATE_FILE"], state)
    logger.info("%s: level exit dikunci -> %s | %s",
                candidate.symbol, levels["source"], levels["note"])


def check_manual_control(client: ExchangeClient, config: dict, filters_cache: dict,
                          state: dict) -> None:
    """Cek "control file" yang bisa ditulis dashboard.py (proses terpisah)
    untuk perintah manual, mis. tombol "Jual Sekarang". Dipanggil tiap
    iterasi loop utama (maks setiap LOOP_INTERVAL_SECONDS, default 15 detik)
    supaya perintah dari dashboard direspons cepat tanpa perlu bot di-restart.

    Kenapa lewat file, bukan panggilan langsung? Bot dan dashboard sengaja
    berjalan sebagai DUA PROSES terpisah (lihat run.py) supaya crash di satu
    proses tidak menjatuhkan proses lain. Satu-satunya cara komunikasi antar
    proses yang sudah dipakai di proyek ini adalah file (file state, mis.
    pump_bot_state_paper.json), jadi kontrol manual memakai pola yang sama
    demi konsistensi."""
    # Nama file kontrol ikut terpisah per mode (pump_bot_control_paper.json
    # vs ..._live.json), sudah otomatis dihitung di config.py. Fallback ke
    # get_control_file() supaya dict config custom tanpa kunci ini pun tetap
    # mendapat nama yang benar untuk mode aktif, bukan nama tanpa akhiran.
    control_path = config.get("CONTROL_FILE") or get_control_file(config)
    cmd = state_mod.load_control(control_path)
    if not cmd:
        return

    # Perintah kadaluarsa (mis. bot sempat mati/lama tidak jalan lalu baru
    # nyala lagi) TIDAK dieksekusi -- mencegah "Jual Sekarang" yang diklik
    # user berjam-jam lalu tiba-tiba dieksekusi tanpa konteks saat ini.
    requested_at = int(cmd.get("requested_at", 0) or 0)
    age_sec = (state_mod.now_ms() - requested_at) / 1000.0
    MAX_AGE_SECONDS = 120
    if requested_at <= 0 or age_sec > MAX_AGE_SECONDS:
        logger.warning("Perintah manual dari dashboard diabaikan (kadaluarsa, umur %.0f detik): %s",
                        age_sec, cmd)
        state_mod.clear_control(control_path)
        return

    action = cmd.get("action")
    if action != "CLOSE_POSITION":
        logger.warning("Perintah manual dari dashboard tidak dikenali: %s", cmd)
        state_mod.clear_control(control_path)
        return

    # Selalu hapus file SEBELUM eksekusi (bukan sesudah) -- kalau proses
    # crash di tengah close_position(), file tidak akan "menyangkut" dan
    # dieksekusi ulang berkali-kali begitu bot menyala lagi.
    state_mod.clear_control(control_path)

    if not state["current_symbol"] or state["qty"] <= 0:
        logger.info("Perintah 'Jual Sekarang' dari dashboard diabaikan: tidak ada posisi terbuka saat ini.")
        return

    requested_symbol = cmd.get("symbol")
    if requested_symbol and requested_symbol != state["current_symbol"]:
        logger.warning(
            "Perintah 'Jual Sekarang' dari dashboard diabaikan: diminta untuk %s, "
            "tapi posisi saat ini adalah %s (kemungkinan posisi sudah berganti "
            "sejak tombol diklik).",
            requested_symbol, state["current_symbol"],
        )
        return

    logger.info("Perintah 'Jual Sekarang' diterima dari dashboard untuk %s. Menutup posisi...",
                state["current_symbol"])
    close_position(client, config, filters_cache, state, "MANUAL_CLOSE_DASHBOARD")


def check_setup_invalidation(client: ExchangeClient, config: dict, filters_cache: dict,
                             state: dict) -> bool:
    """Tutup posisi bila candle tertutup menembus batas invalidasi setup.

    Semua candle sejak cursor state diambil kronologis. Jangan memakai limit
    kecil tetap: downtime lebih dari beberapa candle tidak boleh menghapus
    bukti invalidasi hanya karena harga kemudian rebound.
    """
    if not config.get("SETUP_INVALIDATION_EXIT"):
        return False
    symbol = state.get("current_symbol")
    if not symbol or float(state.get("qty", 0) or 0) <= 0:
        return False

    batas = float(state.get("setup_invalidation_price") or 0.0)
    if batas <= 0:
        return False

    interval = config.get("CONFIRM_INTERVAL", "5m")
    interval_ms = strategy.interval_to_ms(interval)
    now_ms = state_mod.now_ms()
    last_checked = int(state.get("last_setup_check_close_time") or 0)
    if last_checked and now_ms < last_checked + interval_ms:
        return False

    # Mulai satu interval sebelum close cursor. Filter close_time di bawah
    # menghilangkan duplikat, sementara offset ini tetap aman untuk API yang
    # memaknai startTime sebagai open_time.
    entry_time = int(state.get("entry_time", 0) or 0)
    after_time = max(last_checked, entry_time)
    start_time = max(0, after_time - interval_ms + 1) if after_time else None
    raw_all: list = []
    cursor = start_time
    max_pages = 20  # 20.000 candle, pagar terhadap respons API aneh.
    try:
        for _ in range(max_pages):
            raw = client.get_klines(symbol, interval, limit=1000,
                                    start_time_ms=cursor, end_time_ms=now_ms)
            if not raw:
                break
            raw_all.extend(raw)
            try:
                next_cursor = int(raw[-1][0]) + interval_ms
            except (IndexError, TypeError, ValueError):
                raise ValueError("open_time candle invalid saat cek invalidasi")
            if cursor is not None and next_cursor <= cursor:
                raise ValueError("cursor candle tidak maju saat cek invalidasi")
            cursor = next_cursor
            if len(raw) < 1000:
                break
        else:
            raise ValueError("terlalu banyak halaman candle untuk cek invalidasi")
    except BinanceAPIError as exc:
        logger.warning("Gagal mengambil candle %s untuk cek invalidasi setup: %s", symbol, exc)
        return False
    except (ValueError, TypeError) as exc:
        logger.warning("Data candle %s untuk cek invalidasi setup tidak terbaca: %s", symbol, exc)
        return False

    try:
        parsed = strategy.parse_klines(raw_all)
    except ValueError as exc:
        logger.warning("Candle %s ditolak parser saat cek invalidasi setup: %s", symbol, exc)
        return False
    # Hilangkan duplikat antar halaman, urutkan, dan proses hanya candle baru.
    by_close = {k.close_time: k for k in parsed if k.close_time < now_ms and k.close_time > after_time}
    closed = [by_close[t] for t in sorted(by_close)]
    if not closed:
        return False

    for k in closed:
        if k.close < batas:
            logger.info(
                "%s: candle %s tertutup di %.8g, di bawah batas invalidasi setup %.8g "
                "(level breakout %.8g). Posisi ditutup.",
                symbol, interval, k.close, batas, float(state.get("breakout_level") or 0.0),
            )
            close_position(client, config, filters_cache, state, "SETUP_INVALIDATED")
            return True

    # Cursor baru hanya disimpan setelah seluruh batch tervalidasi dan tidak
    # ada kegagalan fetch/parser, sehingga candle tidak hilang saat error.
    state["last_setup_check_close_time"] = closed[-1].close_time
    state_mod.save_state(config["STATE_FILE"], state)
    return False


def manage_exit(client: ExchangeClient, config: dict, filters_cache: dict,
                 state: dict, current_price: float) -> None:
    if not state["current_symbol"] or state["qty"] <= 0 or state["entry_price"] <= 0:
        return

    pnl_pct = (current_price / state["entry_price"] - 1.0) * 100.0
    hold_minutes = (state_mod.now_ms() - state["entry_time"]) / 60000.0

    # Ambil level yang dikunci saat entry. Fallback ke config dipakai untuk
    # posisi lama yang dibuka sebelum fitur ini ada (state file versi lama),
    # supaya bot yang di-upgrade saat sedang memegang posisi tidak kehilangan
    # stop loss-nya.
    sl_pct = abs(float(state.get("sl_pct") or 0.0)) or abs(float(config["SL_PCT"]))
    tp_pct = abs(float(state.get("tp_pct") or 0.0)) or abs(float(config["TP_PCT"]))
    be_trigger = abs(float(state.get("be_trigger_pct") or 0.0)) or abs(float(config["BE_TRIGGER_PCT"]))
    be_lock = abs(float(state.get("be_lock_pct") or 0.0)) or abs(float(config["BE_LOCK_PCT"]))
    trail_start = abs(float(state.get("trail_start_pct") or 0.0)) or abs(float(config["TRAILING_START_PCT"]))
    trail_step = abs(float(state.get("trail_step_pct") or 0.0)) or abs(float(config["TRAILING_STEP_PCT"]))

    # Stop Loss: batas kerugian maksimum dari harga entry. Dicek PALING AWAL
    # dan TIDAK bergantung pada Breakeven/Trailing aktif atau tidak -- ini
    # jaring pengaman kalau harga langsung turun sejak entry dan tidak pernah
    # sempat untung (BE/Trailing baru aktif setelah profit menyentuh trigger-nya
    # masing-masing, jadi TIDAK melindungi skenario ini tanpa Stop Loss).
    if config["USE_STOP_LOSS"] and pnl_pct <= -sl_pct:
        close_position(client, config, filters_cache, state, "STOP_LOSS")
        return

    if config["USE_BREAKEVEN"] and not state["be_active"] and pnl_pct >= be_trigger:
        state["be_active"] = True
        state["be_stop_price"] = state["entry_price"] * (1 + be_lock / 100.0)
        logger.info("%s: Breakeven diaktifkan, stop dikunci di %.6f", state["current_symbol"], state["be_stop_price"])

    if config["USE_TRAILING"]:
        if not state["trailing_active"] and pnl_pct >= trail_start:
            state["trailing_active"] = True
            state["trailing_stop_price"] = current_price * (1 - trail_step / 100.0)
            logger.info("%s: Trailing stop diaktifkan di %.6f", state["current_symbol"], state["trailing_stop_price"])
        elif state["trailing_active"]:
            candidate_stop = current_price * (1 - trail_step / 100.0)
            if candidate_stop > state["trailing_stop_price"]:
                state["trailing_stop_price"] = candidate_stop

    reasons = []
    if config["USE_TP"] and pnl_pct >= tp_pct:
        reasons.append("TAKE_PROFIT")
    if state["be_active"] and current_price <= state["be_stop_price"]:
        reasons.append("BREAKEVEN")
    if state["trailing_active"] and current_price <= state["trailing_stop_price"]:
        reasons.append("TRAILING_STOP")
    if reasons:
        close_position(client, config, filters_cache, state, "+".join(reasons))


def run(config: dict, lifecycle=None) -> int:
    global _shutdown_requested
    _shutdown_requested = False
    _shutdown_event.clear()
    setup_logging(config)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    # Windows hanya dapat mengirim CTRL_BREAK_EVENT secara graceful ke child
    # yang dibuat dengan CREATE_NEW_PROCESS_GROUP.
    if os.name == "nt" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_signal)

    if CONFIG_LOAD_ERRORS:
        logger.critical("Konfigurasi runtime tidak aman untuk dipakai: %s",
                        "; ".join(CONFIG_LOAD_ERRORS))
        return 2

    # Validasi MODE secara KETAT paling awal: nilai tak dikenal/kosong/typo
    # menghentikan bot dengan pesan jelas, TIDAK pernah jatuh diam-diam ke LIVE.
    from config import InvalidModeError
    try:
        mode = require_valid_mode(config)
    except InvalidModeError as exc:
        logger.critical(str(exc))
        return 2
    base_url = get_base_url(config)

    if mode == "LIVE" and (not config["API_KEY"] or not config["API_SECRET"]):
        logger.error(
            "API key/secret belum di-set. Mode LIVE mengirim order dengan UANG ASLI, "
            "jadi kredensial produksi wajib ada di file .env. Bot dihentikan. "
            "(Mode PAPER tidak memerlukan API key.)"
        )
        return 1

    logger.info("=" * 70)
    logger.info("Pump Scanner Bot mulai berjalan. MODE=%s | endpoint=%s", mode, base_url)
    logger.info("File data (otomatis per mode) -> state=%s | log=%s | kontrol=%s",
                config["STATE_FILE"], config["LOG_FILE"],
                config.get("CONTROL_FILE", "-"))
    if mode == "PAPER":
        logger.warning(
            "MODE PAPER AKTIF: eksekusi order, fee, dan saldo DISIMULASIKAN lokal. "
            "Data pasar tetap ASLI dari Binance produksi publik (REST + WebSocket), tanpa API key. "
            "Tidak ada order sungguhan yang dikirim. Hasil PAPER BUKAN jaminan hasil LIVE."
        )
    else:
        logger.warning("MODE LIVE AKTIF: order memakai UANG ASLI di Binance produksi.")
    need_setup = strategy.required_lookback_bars(config)
    have_bars = int(config.get("CONFIRM_LOOKBACK_BARS", 48))
    if have_bars < need_setup:
        logger.warning(
            "CONFIRM_LOOKBACK_BARS=%d lebih kecil dari %d candle yang dibutuhkan ATR dan "
            "struktur setup. Bot tetap mengambil %d candle per konfirmasi agar deteksi "
            "tidak selalu gagal, tetapi perbaiki nilai config ini supaya backtest dan live "
            "benar-benar memakai angka yang sama.",
            have_bars, need_setup, strategy.confirm_window_bars(config),
        )
    if config.get("USE_ATR_EXITS"):
        need = int(config.get("ATR_PERIOD", 14)) + 1
        have = strategy.confirm_window_bars(config)
        if have < need:
            logger.warning(
                "USE_ATR_EXITS aktif tapi jendela konfirmasi hanya %d candle, sedangkan "
                "ATR(%d) butuh minimal %d candle. Akibatnya ATR akan selalu gagal dihitung "
                "dan bot selalu jatuh ke SL/TP tetap.",
                have, config.get("ATR_PERIOD", 14), need,
            )
        else:
            logger.info("Mode exit: ATR adaptif (periode %d, SL %gx, batas %.2f%%-%.2f%%, RR %g:1)",
                        config.get("ATR_PERIOD", 14), config.get("ATR_MULTIPLIER_SL", 2.0),
                        config.get("ATR_SL_MIN_PCT", 1.2), config.get("ATR_SL_MAX_PCT", 4.0),
                        config.get("ATR_TP_RR_RATIO", 2.0))
            logger.info("           Breakeven & Trailing juga ikut ATR "
                        "(BE %gx, lock %gx, trail mulai %gx, jarak %gx)",
                        config.get("ATR_BE_TRIGGER_MULT", 0.5), config.get("ATR_BE_LOCK_MULT", 0.1),
                        config.get("ATR_TRAILING_START_MULT", 1.0),
                        config.get("ATR_TRAILING_STEP_MULT", 1.5))
    else:
        logger.info("Mode exit: SL/TP tetap (SL %.2f%%, TP %.2f%%)",
                    config.get("SL_PCT", 0), config.get("TP_PCT", 0))
    logger.info("=" * 70)

    client = create_exchange_client(config)
    client.sync_time()

    logger.info("Mengambil exchangeInfo untuk semua simbol (sekali di awal)...")
    exchange_info = client.get_exchange_info()
    filters_cache = build_filters_cache(exchange_info)
    tradable_symbols = build_trading_symbols(exchange_info)
    filters_cache_time = time.time()
    logger.info("Filter untuk %d simbol berhasil dimuat.", len(filters_cache))

    state = load_pump_state(config["STATE_FILE"])
    if state["current_symbol"]:
        logger.info("Melanjutkan posisi yang sudah ada: %s qty=%.8f @ %.6f",
                    state["current_symbol"], state["qty"], state["entry_price"])
    # Rekonsiliasi startup (temuan S-02): pastikan posisi di state benar-benar
    # masih ada di exchange. Tanpa ini, penjualan manual atau reset state atau penjualan
    # manual membuat bot mengelola "posisi hantu" dan SL/TP-nya menembak
    # order yang tidak masuk akal.
    reconcile_state_with_exchange(client, config, state)
    if lifecycle is not None:
        lifecycle.write("RUNNING")

    consecutive_errors = 0
    last_time_sync = time.time()
    last_heartbeat = 0.0
    TIME_SYNC_INTERVAL_SECONDS = 15 * 60
    FILTERS_REFRESH_INTERVAL_SECONDS = 6 * 3600

    def klines_fetcher(symbol: str):
        """Ambil tepat window candle TERTUTUP untuk keputusan entry.

        Endpoint kline Binance hampir selalu menyertakan candle interval saat
        ini yang belum selesai. Memakai candle itu untuk sinyal live sementara
        backtest memakai candle final menciptakan look-ahead/repaint mismatch.
        Karena itu satu candle ekstra diminta, candle yang close_time-nya
        belum lewat dibuang, lalu hanya window terbaru yang sudah selesai
        dikembalikan. Cukup untuk deteksi setup dan ATR entry.

        Jumlah candle memakai strategy.confirm_window_bars(), fungsi yang
        sama dengan yang dipakai backtest, dashboard, dan watchlist, sehingga
        keempat jalur melihat jendela identik. Limit endpoint klines adalah
        1000 candle per panggilan dengan bobot IP 2 (dicek 2026-09-25 di
        developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market#klines).
        """
        lookback = strategy.confirm_window_bars(config)
        raw = client.get_klines(symbol, config["CONFIRM_INTERVAL"], limit=lookback + 1)
        now_ms = state_mod.now_ms()
        closed = [k for k in strategy.parse_klines(raw) if k.close_time < now_ms]
        return closed[-lookback:]

    exit_code = 0
    while not _shutdown_requested:
        loop_start = time.time()
        try:
            # Jalur shutdown utama di Windows dan Linux: file perintah stop.
            # Dipisahkan dari file CLOSE_POSITION agar kedua perintah tidak
            # saling menimpa.
            if state_mod.consume_stop_request(config["CONTROL_FILE"]):
                _shutdown_requested = True
                _shutdown_event.set()
                if lifecycle is not None:
                    lifecycle.write("STOPPING", reason="Permintaan stop dari dashboard.")
                state_mod.save_state(config["STATE_FILE"], state)
                break

            if time.time() - last_time_sync > TIME_SYNC_INTERVAL_SECONDS:
                client.sync_time()
                last_time_sync = time.time()

            if time.time() - filters_cache_time > FILTERS_REFRESH_INTERVAL_SECONDS:
                exchange_info = client.get_exchange_info()
                filters_cache = build_filters_cache(exchange_info)
                tradable_symbols = build_trading_symbols(exchange_info)
                filters_cache_time = time.time()
                logger.info("Filter simbol disegarkan ulang (%d simbol).", len(filters_cache))

            # MARKET order normalnya selesai segera. Namun timeout jaringan
            # dapat terjadi setelah Binance menerima order. Intent pending
            # direkonsiliasi aktif pada loop berikutnya (bukan hanya saat
            # restart), dan semua entry tetap diblokir sampai status pasti.
            if state.get("pending_order"):
                logger.warning("Merekonsiliasi intent order pending %s sebelum melanjutkan loop.",
                               state["pending_order"].get("client_order_id"))
                reconcile_state_with_exchange(client, config, state)

            # Perintah manual dari dashboard (mis. "Jual Sekarang") dicek
            # PALING AWAL setiap iterasi, sebelum logika exit otomatis --
            # kalau user memintanya, itu harus didahulukan.
            check_manual_control(client, config, filters_cache, state)

            current_price = None
            if state["current_symbol"]:
                # Jalur kritis posisi (temuan S-10): gagal cepat (1x), biar
                # loop yang mencoba lagi 15 detik kemudian -- bukan tertahan
                # sampai 180 detik di dalam retry panjang sementara SL/TP/
                # BE/Trailing membeku.
                current_price = client.get_price(state["current_symbol"], max_retries=1)

            equity = get_equity(client, config, state)
            if equity is None:
                # Harga API sedang bermasalah (temuan T-05): JANGAN ubah
                # kontrol risiko sama sekali berdasarkan equity yang keliru.
                # Status berhenti yang sudah ada sebelumnya tetap dihormati.
                entries_paused = bool(state.get("dd_stopped") or state.get("daily_stopped"))
            else:
                entries_paused = update_equity_controls(state, equity, config)

            # CLOSE_ALL_AT_LIMIT (temuan T-06): kill switch aktif sekarang
            # benar-benar menutup posisi, bukan cuma menjeda entry baru.
            maybe_force_close_at_risk_limit(client, config, filters_cache, state,
                                            entries_paused, current_price)

            if state["current_symbol"] and current_price is not None:
                manage_exit(client, config, filters_cache, state, current_price)

            # Exit invalidasi setup diperiksa SETELAH exit berbasis harga
            # berjalan. Urutannya sengaja sama dengan urutan prioritas di
            # backtest (Stop Loss lebih dulu, invalidasi setup paling akhir),
            # supaya hasil backtest tetap mewakili perilaku bot saat dua exit
            # bisa terpicu pada saat yang hampir bersamaan.
            if state["current_symbol"]:
                check_setup_invalidation(client, config, filters_cache, state)

            do_scan = time.time() * 1000 - state.get("last_scan_time", 0) > config["MARKET_SCAN_INTERVAL_SECONDS"] * 1000
            if do_scan:
                state["last_scan_time"] = state_mod.now_ms()
                tickers = client.get_ticker_24hr_all()

                now = state_mod.now_ms()
                can_enter = (
                    not state["current_symbol"]
                    and not state.get("pending_order")
                    and not state.get("reconciliation_required")
                    and now >= state.get("cooldown_until", 0)
                    and not entries_paused
                    and now - state.get("last_trade_time", 0) >= config["MIN_SECONDS_BETWEEN_TRADES"] * 1000
                )
                if can_enter:
                    # Semesta dibatasi ke simbol berstatus TRADING dari
                    # exchangeInfo, bukan sekadar apa pun yang muncul di
                    # ticker 24 jam. Simbol HALT atau BREAK tetap mengirim
                    # ticker, dan order ke simbol seperti itu pasti ditolak.
                    # Gerbang pump butuh candle harian, tetapi HANYA untuk
                    # simbol yang sudah lolos syarat kenaikan 24 jam. Cache
                    # dibuat baru tiap siklus scan supaya candle harian tidak
                    # pernah dipakai ulang dari siklus sebelumnya (bisa basi),
                    # namun satu simbol tidak diminta dua kali dalam satu
                    # siklus yang sama.
                    daily_fetcher = scanner.make_daily_klines_fetcher(client, cache={})
                    best = scanner.find_best_candidate(tickers, klines_fetcher, config,
                                                       tradable_symbols,
                                                       daily_klines_fetcher=daily_fetcher,
                                                       reference_ms=state_mod.now_ms())
                    if best:
                        book = client.get_book_ticker(best.symbol)
                        bid, ask = float(book["bidPrice"]), float(book["askPrice"])
                        spread_pct = scanner.spread_pct_from_book(bid, ask)
                        if spread_pct <= config["MAX_SPREAD_PCT"]:
                            logger.info(
                                "Kandidat terpilih: %s (vol24h=%.0f, 24h=%.2f%%, spread=%.3f%%) | %s",
                                best.symbol, best.quote_volume, best.price_change_pct,
                                spread_pct, best.confirm_reason,
                            )
                            # klines kandidat diambil ulang di sini supaya
                            # ATR dihitung dari data yang sama dengan yang
                            # dipakai saat deteksi setup.
                            # Filter usia listing (temuan S-08): koin yang
                            # baru listing sering pump buatan lalu kolaps.
                            # Dicek hanya untuk kandidat yang sudah lolos.
                            min_age = float(config.get("MIN_LISTING_AGE_DAYS", 0) or 0)
                            if min_age > 0:
                                try:
                                    age = listing_age_days(client, best.symbol, state_mod.now_ms())
                                except BinanceAPIError as exc:
                                    logger.warning("Usia listing %s tidak bisa diverifikasi (%s). "
                                                    "Entry dilewati demi keamanan.", best.symbol, exc)
                                    continue_scan_entry = False
                                    age = None
                                else:
                                    continue_scan_entry = True
                                if age is not None and age < min_age:
                                    logger.info("Kandidat %s dilewati: baru listing %.1f hari "
                                                "(batas minimal %.0f hari).",
                                                best.symbol, age, min_age)
                                    continue_scan_entry = False
                            else:
                                continue_scan_entry = True
                            if continue_scan_entry:
                                try:
                                    entry_klines = klines_fetcher(best.symbol)
                                except BinanceAPIError as exc:
                                    logger.warning("Gagal ambil klines %s untuk hitung ATR: %s. "
                                                    "Level exit akan pakai SL/TP tetap.", best.symbol, exc)
                                    entry_klines = None
                                open_position(client, config, filters_cache, state, best,
                                              entry_klines, reference_price=ask)
                        else:
                            logger.info("Kandidat %s dilewati: spread %.3f%% > batas %.3f%%.",
                                        best.symbol, spread_pct, config["MAX_SPREAD_PCT"])
                    else:
                        logger.info("Tidak ada setup pullback retest yang sah pada scan ini.")

            if time.time() - last_heartbeat >= config["HEARTBEAT_INTERVAL_SECONDS"]:
                last_heartbeat = time.time()
                if state["current_symbol"]:
                    pnl_pct = (current_price / state["entry_price"] - 1.0) * 100.0 if current_price else 0.0
                    posisi_info = f"pegang {state['current_symbol']} (PnL={pnl_pct:+.2f}%)"
                else:
                    posisi_info = "tidak ada posisi"
                flags = []
                if state.get("dd_stopped"):
                    flags.append("DD-STOP")
                if state.get("daily_stopped"):
                    flags.append("DAILY-STOP")
                flag_str = f" | status: {', '.join(flags)}" if flags else ""
                equity_str = f"{equity:.2f}" if equity is not None else "n/a (API harga gangguan)"
                logger.info("[HEARTBEAT] Bot masih berjalan | equity=%s %s | %s%s",
                            equity_str, config["QUOTE_ASSET"], posisi_info, flag_str)

            state_mod.save_state(config["STATE_FILE"], state)
            if lifecycle is not None:
                lifecycle.heartbeat("RUNNING")
            consecutive_errors = 0

        except BinanceAPIError as exc:
            consecutive_errors += 1
            logger.error("BinanceAPIError (%d berturut-turut): %s", consecutive_errors, exc)
        except Exception as exc:  # noqa: BLE001
            consecutive_errors += 1
            logger.exception("Error tak terduga (%d berturut-turut): %s", consecutive_errors, exc)

        max_errors = int(config.get("MAX_CONSECUTIVE_ERRORS", 10) or 10)
        if consecutive_errors >= max_errors:
            logger.critical(
                "%d error berturut-turut. Bot berhenti total untuk keamanan. "
                "Posisi terbuka (kalau ada) tanpa pengelolaan sampai bot dinyalakan lagi "
                "atau posisi dijual manual -- jalankan bot di bawah supervisor (systemd "
                "Restart=always) supaya proses otomatis hidup kembali.",
                max_errors,
            )
            exit_code = 1
            break

        elapsed = time.time() - loop_start
        # Event membuat SIGTERM/SIGBREAK membangunkan sleep segera, sehingga
        # state masih sempat disimpan sebelum timeout fallback berakhir.
        _shutdown_event.wait(max(1.0, config["LOOP_INTERVAL_SECONDS"] - elapsed))

    if lifecycle is not None and _shutdown_requested:
        lifecycle.write("STOPPING", reason="Shutdown graceful sedang menyimpan state.")
    # Simpan sekali lagi setelah loop agar perubahan iterasi terakhir tidak
    # hilang saat sinyal datang di antara dua operasi.
    try:
        state_mod.save_state(config["STATE_FILE"], state)
    except OSError as exc:
        logger.error("Gagal menyimpan state saat shutdown: %s", exc)
        exit_code = 1

    # Tutup sumber daya klien (mis. thread WebSocket di PAPER/LIVE) dengan
    # rapi. Aman dipanggil untuk klien apa pun (default no-op).
    try:
        client.close()
    except Exception:  # noqa: BLE001 - penutupan best-effort saat shutdown
        pass
    logger.info("Bot berhenti.")
    return exit_code


def selftest() -> None:
    import tempfile

    cfg = dict(PUMP_CONFIG)
    # WAJIB: open_position/close_position sekarang memanggil save_state SEGERA
    # setelah order terisi (perbaikan T-04). Tanpa pengalihan ini, selftest
    # (yang memakai client tiruan) akan MENULIS state palsu ke file state asli
    # dan bisa merusak state bot yang sedang berjalan.
    cfg["STATE_FILE"] = os.path.join(tempfile.gettempdir(), "pump_bot_selftest_state.json")

    print("=== SELFTEST: saringan semesta, gerbang pump, dan urutan volume ===")
    # Gerbang pump WAJIB: naik >= PUMP_MIN_24H_CHANGE_PCT dalam 24 jam DAN
    # volume kuotasi 24 jam >= PUMP_VOLUME_SURGE_MULT x rata-rata 7 hari.
    HARI_MS = 86_400_000

    def _harian(quote_volume_harian: float):
        """Tujuh candle harian PENUH dengan volume kuotasi tertentu."""
        return [strategy.Kline(open_time=i * HARI_MS, open=1.0, high=1.0, low=1.0,
                               close=1.0, close_time=(i + 1) * HARI_MS - 1,
                               volume=quote_volume_harian, quote_volume=quote_volume_harian)
                for i in range(7)]

    # Rata-rata harian per simbol dibuat supaya rasio volumenya jelas:
    #   AUSDT  5.000.000 / 2.000.000 = 2,50x  -> lolos
    #   BUSDT  3.000.000 / 1.000.000 = 3,00x  -> lolos
    #   EUSDT  4.000.000 / 4.000.000 = 1,00x  -> GAGAL syarat volume
    #   FUSDT  belum punya 7 candle harian     -> GAGAL (koin baru listing)
    RATA_HARIAN = {
        "AUSDT": 2_000_000.0, "BUSDT": 1_000_000.0, "CUSDT": 1_000_000.0,
        "DUSDT": 1_000.0, "EUSDT": 4_000_000.0, "BTCUPUSDT": 1_000.0,
        "USDCUSDT": 1_000.0, "HALTUSDT": 1_000.0,
    }

    def daily_fetcher(symbol: str):
        if symbol == "FUSDT":          # baru listing: hanya 3 candle harian
            return _harian(1_000.0)[:3]
        if symbol == "GUSDT":          # simbol bermasalah: request gagal
            raise RuntimeError("timeout simulasi")
        return _harian(RATA_HARIAN.get(symbol, 1_000.0))

    ref_ms = 7 * HARI_MS + 1           # semua candle harian di atas sudah tertutup

    tickers = [
        {"symbol": "AUSDT", "priceChangePercent": "15.0", "quoteVolume": "5000000", "lastPrice": "1.0"},
        {"symbol": "BUSDT", "priceChangePercent": "25.0", "quoteVolume": "3000000", "lastPrice": "2.0"},
        {"symbol": "CUSDT", "priceChangePercent": "-3.0", "quoteVolume": "9000000", "lastPrice": "0.5"},   # gagal: turun 24 jam
        {"symbol": "DUSDT", "priceChangePercent": "40.0", "quoteVolume": "10000", "lastPrice": "0.1"},     # gagal: volume kurang
        {"symbol": "EUSDT", "priceChangePercent": "20.0", "quoteVolume": "4000000", "lastPrice": "1.0"},   # gagal: volume tidak naik
        {"symbol": "FUSDT", "priceChangePercent": "30.0", "quoteVolume": "8000000", "lastPrice": "1.0"},   # gagal: riwayat harian < 7
        {"symbol": "GUSDT", "priceChangePercent": "30.0", "quoteVolume": "8000000", "lastPrice": "1.0"},   # gagal: klines harian error
        {"symbol": "BTCUPUSDT", "priceChangePercent": "50.0", "quoteVolume": "9000000", "lastPrice": "3.0"},  # gagal: leveraged token
        {"symbol": "USDCUSDT", "priceChangePercent": "20.0", "quoteVolume": "9000000", "lastPrice": "1.0"},   # gagal: stablecoin
        {"symbol": "HALTUSDT", "priceChangePercent": "10.0", "quoteVolume": "8000000", "lastPrice": "1.0"},   # gagal: status bukan TRADING
    ]
    tradable = {"AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT", "FUSDT", "GUSDT",
                "BTCUPUSDT", "USDCUSDT"}
    ranked = scanner.filter_and_rank_candidates(
        tickers, cfg, tradable, get_daily_klines_fn=daily_fetcher, reference_ms=ref_ms)
    symbols = [c.symbol for c in ranked]
    print("  Lolos saringan + gerbang pump, urut volume kuotasi:", symbols)
    assert symbols == ["AUSDT", "BUSDT"], f"Hasil saringan/urutan salah: {symbols}"
    print("  -> OK (leveraged token, stablecoin, volume rendah, simbol non-TRADING,")
    print("      koin yang TURUN 24 jam, volume yang tidak naik, koin baru listing,")
    print("      dan simbol yang gagal diambil candle hariannya semuanya ter-exclude)")

    # Gerbang ini WAJIB: tanpa sumber candle harian, semua simbol ditolak.
    tanpa_sumber = scanner.filter_and_rank_candidates(tickers, cfg, tradable)
    assert tanpa_sumber == [], \
        "Tanpa sumber candle harian, gerbang pump harus menolak semua simbol (fail closed)"
    print("  -> OK (tanpa sumber candle harian, gerbang pump fail closed)")

    print("\n=== SELFTEST: deteksi setup pullback dan retest ===")
    # Data sintetis WAJIB mengisi volume dan quote_volume, karena anchored
    # VWAP membaginya. Kline.volume default 0.0, dan VWAP dari volume nol
    # akan mengembalikan None sehingga setup selalu ditolak.
    from synthetic_data import skenario_pullback_retest

    kl_ok = skenario_pullback_retest("lolos")
    hasil = scanner.detect_pullback_retest(kl_ok, cfg)
    print(f"  Skenario breakout, pullback, close kembali di atas level -> ok={hasil.ok} ({hasil.reason})")
    assert hasil.ok, "Skenario retest sah harusnya lolos"
    assert hasil.breakout_level and hasil.invalidation_price, "Level setup harus ikut dikembalikan"

    kl_wick = skenario_pullback_retest("wick_saja")
    hasil_wick = scanner.detect_pullback_retest(kl_wick, cfg)
    print(f"  Skenario breakout hanya lewat sumbu -> ok={hasil_wick.ok} ({hasil_wick.reason})")
    assert not hasil_wick.ok, "Sumbu yang menembus tanpa close di atas level bukan breakout"

    kl_jauh = skenario_pullback_retest("terlalu_jauh")
    hasil_jauh = scanner.detect_pullback_retest(kl_jauh, cfg)
    print(f"  Skenario harga sudah terlalu jauh di atas level -> ok={hasil_jauh.ok} ({hasil_jauh.reason})")
    assert not hasil_jauh.ok, "Anti-kejar harus menolak entry yang sudah jauh di atas level"

    kl_gagal = skenario_pullback_retest("close_di_bawah_level")
    hasil_gagal = scanner.detect_pullback_retest(kl_gagal, cfg)
    print(f"  Skenario retest close di bawah level -> ok={hasil_gagal.ok} ({hasil_gagal.reason})")
    assert not hasil_gagal.ok, "Retest yang close di bawah level harus ditolak"

    ok_ce, reason_ce = scanner.confirm_entry(kl_ok, cfg)
    assert ok_ce and reason_ce == hasil.reason, "confirm_entry harus memakai detect_pullback_retest"
    print("  -> OK (confirm_entry konsisten dengan detect_pullback_retest)")

    print("\n=== SELFTEST: exit SETUP_INVALIDATED ===")
    from decimal import Decimal as _D
    from binance_client import SymbolFilters as _SymbolFilters

    class FakeKlineClient:
        """Client tiruan yang hanya melayani get_klines dan order jual."""

        def __init__(self, closes):
            self.closes = closes
            self.orders = []

        def get_klines(self, symbol, interval, limit=500, start_time_ms=None, end_time_ms=None):
            now = state_mod.now_ms()
            rows = []
            n = len(self.closes)
            for idx, c in enumerate(self.closes):
                # close_time dibuat SUDAH lewat supaya candle dianggap tertutup.
                close_time = now - (n - idx) * 300_000
                rows.append([close_time - 299_999, str(c), str(c + 0.2), str(c - 0.2),
                             str(c), "1000", close_time, "100000", 10, "500", "50000", "0"])
            return rows[-limit:]

        def get_account(self):
            return {"balances": [{"asset": "TEST", "free": "1", "locked": "0"},
                                 {"asset": "USDT", "free": "1000", "locked": "0"}]}

        def new_market_order(self, symbol, side, quantity=None, quote_order_qty=None):
            self.orders.append((symbol, side, quantity))
            qty = float(quantity or 0.0)
            return {"executedQty": str(qty), "cummulativeQuoteQty": str(qty * 99.0)}

        def get_dust_convertible(self, account_type="SPOT"):
            return {"details": []}

        def convert_dust(self, assets, account_type="SPOT"):
            return {"totalTransfered": "0"}

    fc_inval = {"TESTUSDT": _SymbolFilters(step_size=_D("0.01"), min_qty=_D("0.01"),
                                           min_notional=_D("5"), tick_size=_D("0.0001"))}
    cfg_inval = dict(cfg)
    cfg_inval["SETUP_INVALIDATION_EXIT"] = True

    def _state_posisi():
        st = dict(DEFAULT_STATE)
        st["current_symbol"] = "TESTUSDT"
        st["entry_price"] = 101.9
        st["qty"] = 1.0
        st["entry_time"] = state_mod.now_ms() - 3_600_000
        st["breakout_level"] = 101.0
        st["setup_invalidation_price"] = 100.1
        st["atr_abs_at_entry"] = 0.9
        return st

    st_kena = _state_posisi()
    client_kena = FakeKlineClient([101.5, 101.2, 99.8])
    ditutup = check_setup_invalidation(client_kena, cfg_inval, fc_inval, st_kena)
    print(f"  Candle tertutup di 99.8 (batas 100.1) -> ditutup={ditutup}")
    assert ditutup and st_kena["current_symbol"] is None, "Exit invalidasi harus menutup posisi"
    assert client_kena.orders and client_kena.orders[-1][1] == "SELL", "Harus mengirim SELL"

    st_aman = _state_posisi()
    client_aman = FakeKlineClient([101.5, 101.2, 101.4])
    ditutup2 = check_setup_invalidation(client_aman, cfg_inval, fc_inval, st_aman)
    print(f"  Harga bertahan di atas level -> ditutup={ditutup2}")
    assert not ditutup2 and st_aman["current_symbol"] == "TESTUSDT", \
        "Posisi tidak boleh ditutup saat harga bertahan di atas batas invalidasi"

    st_mati = _state_posisi()
    cfg_mati = dict(cfg_inval)
    cfg_mati["SETUP_INVALIDATION_EXIT"] = False
    assert not check_setup_invalidation(FakeKlineClient([99.0]), cfg_mati, fc_inval, st_mati), \
        "Fitur yang dimatikan tidak boleh menutup posisi"

    st_lama = _state_posisi()
    st_lama["setup_invalidation_price"] = 0.0   # posisi dari versi lama tanpa level
    assert not check_setup_invalidation(FakeKlineClient([50.0]), cfg_inval, fc_inval, st_lama), \
        "Posisi lama tanpa level tersimpan harus dilewati, bukan ditebak"
    print("  -> OK (terpicu, tidak terpicu, bisa dimatikan, aman untuk state lama)")

    print("\n=== SELFTEST: simulasi exit (TP/Breakeven/Trailing) ===")
    from decimal import Decimal as D
    from binance_client import SymbolFilters

    class FakeTradeClient:
        """Client palsu untuk selftest exit/kontrol manual.

        close_position() SELALU benar-benar memanggil get_account()
        lalu new_market_order() -- persis seperti di
        PAPER maupun LIVE. Jadi selftest butuh client tiruan (bukan None)
        supaya bisa menguji logika exit tanpa menyentuh jaringan sama sekali.
        """

        def __init__(self, base_asset="TEST", free=1.0, price=100.0):
            self.base_asset = base_asset
            self.free = free
            self.price = price
            self.orders = []

        def get_account(self):
            return {"balances": [
                {"asset": self.base_asset, "free": str(self.free), "locked": "0"},
                {"asset": "USDT", "free": "1000", "locked": "0"},
            ]}

        def new_market_order(self, symbol, side, quantity=None, quote_order_qty=None):
            self.orders.append((symbol, side, quantity))
            qty = float(quantity or 0.0)
            return {"executedQty": str(qty), "cummulativeQuoteQty": str(qty * self.price)}

        def get_dust_convertible(self, account_type="SPOT"):
            return {"details": []}

        def convert_dust(self, assets, account_type="SPOT"):
            return {"totalTransfered": "0"}

    filters_cache = {"TESTUSDT": SymbolFilters(step_size=D("0.01"), min_qty=D("0.01"),
                                                min_notional=D("5"), tick_size=D("0.0001"))}
    # Ambang exit dikunci eksplisit di selftest ini supaya hasilnya
    # deterministik dan tidak ikut berubah setiap kali nilai di config.py
    # di-tuning (sebelumnya selftest memakai nilai config langsung padahal
    # angka pembandingnya hardcode, sehingga gagal begitu SL_PCT diubah).
    cfg_exit = dict(cfg)
    cfg_exit.update({
        "USE_TP": True, "TP_PCT": 6.0,
        "USE_STOP_LOSS": True, "SL_PCT": 3.0,
        "USE_BREAKEVEN": True, "BE_TRIGGER_PCT": 3.0, "BE_LOCK_PCT": 0.15,
        "USE_TRAILING": True, "TRAILING_START_PCT": 4.0, "TRAILING_STEP_PCT": 1.0,
    })

    state = dict(DEFAULT_STATE)
    state["current_symbol"] = "TESTUSDT"
    state["entry_price"] = 100.0
    state["qty"] = 1.0
    state["entry_time"] = state_mod.now_ms()

    manage_exit(FakeTradeClient(), cfg_exit, filters_cache, state, 103.5)  # >= BE_TRIGGER_PCT (3.0%)
    assert state["be_active"], "Breakeven harusnya sudah aktif di profit 3.5%"
    assert state["current_symbol"] == "TESTUSDT", "Belum boleh close, baru breakeven aktif"
    print(f"  Setelah profit +3.5%: be_active={state['be_active']}, be_stop={state['be_stop_price']:.4f} -> OK")

    manage_exit(FakeTradeClient(), cfg_exit, filters_cache, state, 106.5)  # TP_PCT = 6.0
    assert state["current_symbol"] is None, "Posisi harusnya sudah tertutup kena TAKE_PROFIT"
    print("  Setelah profit +6.5%: posisi tertutup (TAKE_PROFIT) -> OK")

    print("\n=== SELFTEST: Stop Loss (harga langsung turun sejak entry, TIDAK sempat untung) ===")
    assert cfg_exit["USE_STOP_LOSS"], "USE_STOP_LOSS harus aktif di skenario ini"
    state2 = dict(DEFAULT_STATE)
    state2["current_symbol"] = "TESTUSDT"
    state2["entry_price"] = 100.0
    state2["qty"] = 1.0
    state2["entry_time"] = state_mod.now_ms()

    # Rugi -2% dulu -- masih di atas ambang SL_PCT (3.0%), posisi harus TETAP terbuka.
    manage_exit(FakeTradeClient(), cfg_exit, filters_cache, state2, 98.0)
    assert state2["current_symbol"] == "TESTUSDT", "Rugi -2% belum boleh kena Stop Loss (ambang 3.0%)"
    assert not state2["be_active"], "Breakeven tidak boleh aktif kalau posisi rugi"
    print("  Rugi -2%: posisi masih terbuka, BE/Trailing tidak aktif -> OK")

    # Rugi -3.5% -- melewati SL_PCT (3.0%), posisi harus dipaksa tertutup STOP_LOSS,
    # walau BE_TRIGGER_PCT/TRAILING_START_PCT tidak pernah tersentuh sama sekali.
    manage_exit(FakeTradeClient(), cfg_exit, filters_cache, state2, 96.5)
    assert state2["current_symbol"] is None, "Posisi harusnya sudah tertutup kena STOP_LOSS di rugi -3.5%"
    print("  Rugi -3.5%: posisi tertutup (STOP_LOSS) -> OK")

    print("\n=== SELFTEST: ukuran posisi (RISK_PERCENT, plafon, bantalan saldo) ===")

    class SizingClient(FakeTradeClient):
        """Client tiruan dengan saldo USDT yang bisa diatur, untuk memeriksa
        PERSIS berapa nominal yang dipakai open_position saat BUY."""

        def __init__(self, usdt_free):
            super().__init__(base_asset="TESTB", free=0.0, price=1.0)
            self.usdt_free = usdt_free

        def get_account(self):
            return {"balances": [
                {"asset": "USDT", "free": str(self.usdt_free), "locked": "0"},
                {"asset": "TESTB", "free": str(self.free), "locked": "0"},
            ]}

        def new_market_order(self, symbol, side, quantity=None, quote_order_qty=None):
            resp = super().new_market_order(symbol, side, quantity, quote_order_qty)
            if side == "BUY":
                self.free += float(quantity or 0.0)
            return resp

    from decimal import Decimal as D2
    from binance_client import SymbolFilters as SF2
    size_filters = {"TESTBUSDT": SF2(step_size=D2("0.00000001"), min_qty=D2("0.00000001"),
                                      min_notional=D2("1"), tick_size=D2("0.0001"))}
    cand = scanner.Candidate(symbol="TESTBUSDT", base_asset="TESTB", price_change_pct=20.0,
                              quote_volume=9e6, last_price=1.0, confirmed=True,
                              confirm_reason="selftest")

    def nominal_dipakai(cfg_size, saldo):
        """Jalankan open_position lalu kembalikan nominal USDT yang benar-benar
        dibelanjakan (harga = 1.0, jadi qty = nominal)."""
        cl = SizingClient(saldo)
        st = dict(DEFAULT_STATE)
        open_position(cl, cfg_size, size_filters, st, cand)
        assert cl.orders, "Order BUY seharusnya terkirim"
        return float(cl.orders[-1][2])

    cfg_size = dict(cfg)
    cfg_size.update({"USE_RISK_PERCENT": True, "RISK_PERCENT": 95.0,
                      "BALANCE_BUFFER_PCT": 0.5, "MAX_POSITION_USDT": 0,
                      "USE_ATR_EXITS": False})

    # Tanpa plafon: persentase harus BENAR-BENAR terpakai dan ikut tumbuh
    # bersama saldo. Inilah yang dulu tidak terjadi karena plafon 10 USDT.
    for saldo, harap in ((100.0, 100 * 0.995 * 0.95), (1000.0, 1000 * 0.995 * 0.95),
                          (5000.0, 5000 * 0.995 * 0.95)):
        got = nominal_dipakai(cfg_size, saldo)
        assert abs(got - harap) < 0.01, f"saldo {saldo}: harap {harap:.2f}, dapat {got:.2f}"
        print(f"  Saldo {saldo:>7.0f} -> pakai {got:>8.2f} USDT ({got / saldo * 100:.2f}% saldo) -> OK")

    # Plafon aktif harus benar-benar membatasi (dan bot memperingatkan di log).
    cfg_cap = dict(cfg_size)
    cfg_cap["MAX_POSITION_USDT"] = 10.0
    got_cap = nominal_dipakai(cfg_cap, 1000.0)
    assert abs(got_cap - 10.0) < 1e-6, f"Plafon 10 USDT harus mengikat, dapat {got_cap}"
    print(f"  Plafon 10 USDT aktif, saldo 1000 -> pakai {got_cap:.2f} USDT "
          f"({got_cap / 1000 * 100:.2f}% saldo) -> OK (inilah bug lama)")

    # RISK_PERCENT 100 + bantalan: tidak boleh melebihi saldo, harus menyisakan
    # ruang untuk fee supaya order tidak ditolak bursa (-2010).
    cfg_allin = dict(cfg_size)
    cfg_allin["RISK_PERCENT"] = 100.0
    got_allin = nominal_dipakai(cfg_allin, 1000.0)
    assert got_allin < 1000.0, "All-in tidak boleh membelanjakan 100% saldo persis (butuh ruang fee)"
    assert got_allin >= 1000.0 * 0.98, f"Bantalan terlalu besar: {got_allin}"
    print(f"  RISK_PERCENT=100, saldo 1000 -> pakai {got_allin:.2f} USDT "
          f"(sisa {1000 - got_allin:.2f} untuk fee) -> OK")

    # Mode nominal tetap harus tetap bekerja seperti dulu.
    cfg_fixed_size = dict(cfg_size)
    cfg_fixed_size.update({"USE_RISK_PERCENT": False, "POSITION_SIZE_USDT": 25.0})
    got_fixed = nominal_dipakai(cfg_fixed_size, 1000.0)
    assert abs(got_fixed - 25.0) < 1e-6, f"Mode nominal tetap harus pakai 25 USDT, dapat {got_fixed}"
    print(f"  Mode nominal tetap (USE_RISK_PERCENT=False) -> {got_fixed:.2f} USDT -> OK")

    print("\n=== SELFTEST: SL/TP adaptif berbasis ATR dipakai manage_exit ===")
    # Membuktikan manage_exit benar-benar MEMAKAI level yang dikunci di state,
    # bukan diam-diam kembali ke SL_PCT config. Kalau integrasi ini putus,
    # bot akan tampak "punya fitur ATR" padahal exit-nya masih pakai nilai lama.
    cfg_atr = dict(cfg_exit)
    cfg_atr["SL_PCT"] = 3.0          # nilai config yang TIDAK boleh terpakai
    state_atr = dict(DEFAULT_STATE)
    state_atr["current_symbol"] = "TESTUSDT"
    state_atr["entry_price"] = 100.0
    state_atr["qty"] = 1.0
    state_atr["entry_time"] = state_mod.now_ms()
    state_atr["sl_pct"] = 1.0        # level terkunci dari ATR, jauh lebih ketat
    state_atr["tp_pct"] = 2.0

    # Rugi -1.5%: masih aman menurut SL_PCT config (3%), tapi SUDAH melewati
    # level ATR yang dikunci (1%). Posisi HARUS tertutup.
    manage_exit(FakeTradeClient(), cfg_atr, filters_cache, state_atr, 98.5)
    assert state_atr["current_symbol"] is None, \
        "manage_exit harus memakai sl_pct dari state (1%), bukan SL_PCT config (3%)"
    print("  SL terkunci dari ATR (1%) dipakai, bukan SL_PCT config (3%) -> OK")

    # TP juga harus memakai level terkunci.
    state_tp = dict(DEFAULT_STATE)
    state_tp["current_symbol"] = "TESTUSDT"
    state_tp["entry_price"] = 100.0
    state_tp["qty"] = 1.0
    state_tp["entry_time"] = state_mod.now_ms()
    state_tp["sl_pct"] = 1.0
    state_tp["tp_pct"] = 2.0
    cfg_tp = dict(cfg_atr)
    cfg_tp["USE_BREAKEVEN"] = False
    cfg_tp["USE_TRAILING"] = False
    manage_exit(FakeTradeClient(), cfg_tp, filters_cache, state_tp, 102.5)
    assert state_tp["current_symbol"] is None, \
        "manage_exit harus memakai tp_pct dari state (2%), bukan TP_PCT config (6%)"
    print("  TP terkunci dari ATR (2%) dipakai, bukan TP_PCT config (6%) -> OK")

    # State lama (dari versi bot sebelum fitur ini) tidak punya sl_pct sama
    # sekali. Bot yang di-upgrade saat sedang memegang posisi TIDAK BOLEH
    # kehilangan stop loss-nya -- harus jatuh ke SL_PCT config.
    state_old = dict(DEFAULT_STATE)
    del state_old["sl_pct"]
    del state_old["tp_pct"]
    state_old["current_symbol"] = "TESTUSDT"
    state_old["entry_price"] = 100.0
    state_old["qty"] = 1.0
    state_old["entry_time"] = state_mod.now_ms()
    manage_exit(FakeTradeClient(), cfg_atr, filters_cache, state_old, 96.0)  # -4%, lewat SL config 3%
    assert state_old["current_symbol"] is None, \
        "State versi lama tanpa sl_pct harus tetap terlindungi oleh SL_PCT config"
    print("  State versi lama (tanpa sl_pct) tetap terlindungi SL_PCT config -> OK")

    print("\n=== SELFTEST: Breakeven & Trailing ikut skala ATR ===")
    # Ini menutup celah pincang: kalau hanya SL/TP yang ikut ATR sementara
    # BE/Trailing memakai angka tetap, trailing yang jauh lebih sempit dari
    # ATR akan menutup posisi sebelum TP tercapai dan risk:reward terbalik.
    cfg_full = dict(cfg)
    cfg_full.update({"USE_ATR_EXITS": True, "ATR_PERIOD": 14,
                      "ATR_MULTIPLIER_SL": 2.0, "ATR_SL_MIN_PCT": 0.5,
                      "ATR_SL_MAX_PCT": 20.0, "ATR_TP_RR_RATIO": 2.0,
                      "ATR_BE_TRIGGER_MULT": 0.5, "ATR_BE_LOCK_MULT": 0.1,
                      "ATR_TRAILING_START_MULT": 1.0, "ATR_TRAILING_STEP_MULT": 1.5})

    # Koin dengan ATR 2%: semua level harus berskala ATR, bukan angka config.
    ks_2pct = [strategy.Kline(0, 100, 101, 99, 100, 0) for _ in range(20)]
    lv_full = strategy.resolve_exit_levels(cfg_full, ks_2pct, 100.0)
    assert abs(lv_full["atr_pct"] - 2.0) < 1e-6, lv_full
    assert abs(lv_full["be_trigger_pct"] - 1.0) < 1e-6, "BE trigger harus 0.5x ATR = 1.0%"
    assert abs(lv_full["be_lock_pct"] - 0.2) < 1e-6, "BE lock harus 0.1x ATR = 0.2%"
    assert abs(lv_full["trail_start_pct"] - 2.0) < 1e-6, "Trailing start harus 1.0x ATR = 2.0%"
    assert abs(lv_full["trail_step_pct"] - 3.0) < 1e-6, "Trailing step harus 1.5x ATR = 3.0%"
    print(f"  ATR 2% -> BE@{lv_full['be_trigger_pct']:.2f}% kunci {lv_full['be_lock_pct']:.2f}%, "
          f"Trail@{lv_full['trail_start_pct']:.2f}% jarak {lv_full['trail_step_pct']:.2f}% -> OK")

    # Trailing step TIDAK BOLEH lebih longgar dari SL. Kalau lebih longgar,
    # SL selalu kena duluan dan trailing cuma ilusi.
    cfg_wide = dict(cfg_full)
    cfg_wide["ATR_SL_MAX_PCT"] = 2.5          # SL dibatasi ketat
    cfg_wide["ATR_TRAILING_STEP_MULT"] = 5.0  # trailing sengaja dibuat sangat longgar
    lv_wide = strategy.resolve_exit_levels(cfg_wide, ks_2pct, 100.0)
    assert lv_wide["trail_step_pct"] <= lv_wide["sl_pct"] + 1e-9, \
        (f"Trailing step ({lv_wide['trail_step_pct']}) tidak boleh melebihi SL "
         f"({lv_wide['sl_pct']}) -- SL akan selalu kena duluan")
    print(f"  Trailing step dibatasi agar <= SL ({lv_wide['trail_step_pct']:.2f}% "
          f"vs SL {lv_wide['sl_pct']:.2f}%) -> OK")

    # Breakeven harus terpicu SEBELUM trailing, kalau tidak urutannya kacau.
    cfg_order = dict(cfg_full)
    cfg_order["ATR_BE_TRIGGER_MULT"] = 9.0    # sengaja dibuat lebih besar dari trailing start
    lv_order = strategy.resolve_exit_levels(cfg_order, ks_2pct, 100.0)
    assert lv_order["be_trigger_pct"] <= lv_order["trail_start_pct"] + 1e-9, \
        "Breakeven harus terpicu sebelum atau bersamaan dengan Trailing"
    print(f"  BE dipaksa terpicu sebelum Trailing ({lv_order['be_trigger_pct']:.2f}% "
          f"<= {lv_order['trail_start_pct']:.2f}%) -> OK")

    # manage_exit harus MEMAKAI level BE/Trailing dari state, bukan config.
    st_be = dict(DEFAULT_STATE)
    st_be["current_symbol"] = "TESTUSDT"
    st_be["entry_price"] = 100.0
    st_be["qty"] = 1.0
    st_be["entry_time"] = state_mod.now_ms()
    st_be["sl_pct"] = 10.0
    st_be["tp_pct"] = 20.0
    st_be["be_trigger_pct"] = 5.0     # jauh lebih tinggi dari BE_TRIGGER_PCT config
    st_be["be_lock_pct"] = 1.0
    st_be["trail_start_pct"] = 8.0
    st_be["trail_step_pct"] = 3.0
    cfg_be_cfg = dict(cfg_exit)
    cfg_be_cfg["BE_TRIGGER_PCT"] = 1.0   # nilai config yang TIDAK boleh terpakai
    # Profit +2%: sudah lewat BE config (1%) tapi BELUM lewat BE state (5%).
    # Kalau integrasi benar, Breakeven belum boleh aktif.
    manage_exit(FakeTradeClient(), cfg_be_cfg, filters_cache, st_be, 102.0)
    assert not st_be["be_active"], \
        "Breakeven memakai BE_TRIGGER_PCT config, seharusnya memakai be_trigger_pct dari state"
    print("  Profit +2% -> BE belum aktif (pakai trigger state 5%, bukan config 1%) -> OK")

    # Profit +6%: sudah lewat BE state (5%), Breakeven harus aktif.
    manage_exit(FakeTradeClient(), cfg_be_cfg, filters_cache, st_be, 106.0)
    assert st_be["be_active"], "Breakeven harus aktif setelah melewati trigger dari state"
    assert abs(st_be["be_stop_price"] - 101.0) < 1e-6, \
        f"BE stop harus entry x (1 + be_lock 1%) = 101.0, dapat {st_be['be_stop_price']}"
    print(f"  Profit +6% -> BE aktif, stop dikunci di {st_be['be_stop_price']:.2f} -> OK")

    print("\n=== SELFTEST: perintah manual 'Jual Sekarang' dari dashboard (control file) ===")
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        control_path = f"{tmpdir}/pump_bot_control.json"
        cfg_ctrl = dict(cfg_exit)
        cfg_ctrl["CONTROL_FILE"] = control_path

        # Skenario A: ada posisi terbuka, perintah CLOSE_POSITION untuk simbol
        # yang sesuai dan masih segar (baru saja ditulis) -> posisi harus
        # tertutup dengan alasan MANUAL_CLOSE_DASHBOARD.
        state3 = dict(DEFAULT_STATE)
        state3["current_symbol"] = "TESTUSDT"
        state3["entry_price"] = 100.0
        state3["qty"] = 1.0
        state3["entry_time"] = state_mod.now_ms()
        state_mod.save_control(control_path, {
            "action": "CLOSE_POSITION", "symbol": "TESTUSDT", "requested_at": state_mod.now_ms(),
        })
        check_manual_control(FakeTradeClient(), cfg_ctrl, filters_cache, state3)
        assert state3["current_symbol"] is None, "Posisi harusnya tertutup oleh perintah manual yang valid"
        assert not state_mod.load_control(control_path), "Control file harus terhapus setelah diproses"
        print("  Perintah valid untuk simbol yang sesuai -> posisi ditutup, control file dibersihkan -> OK")

        # Skenario B: perintah kadaluarsa (requested_at sangat lampau) -> HARUS
        # diabaikan, posisi tetap terbuka.
        state4 = dict(DEFAULT_STATE)
        state4["current_symbol"] = "TESTUSDT"
        state4["entry_price"] = 100.0
        state4["qty"] = 1.0
        state4["entry_time"] = state_mod.now_ms()
        state_mod.save_control(control_path, {
            "action": "CLOSE_POSITION", "symbol": "TESTUSDT",
            "requested_at": state_mod.now_ms() - 10 * 60 * 1000,  # 10 menit lalu
        })
        check_manual_control(FakeTradeClient(), cfg_ctrl, filters_cache, state4)
        assert state4["current_symbol"] == "TESTUSDT", "Perintah kadaluarsa (>2 menit) harus DIABAIKAN"
        print("  Perintah kadaluarsa (10 menit lalu) -> diabaikan, posisi tetap terbuka -> OK")

        # Skenario C: perintah untuk simbol yang BEDA dari posisi saat ini
        # (mis. posisi sudah berganti sejak tombol diklik) -> HARUS diabaikan.
        state5 = dict(DEFAULT_STATE)
        state5["current_symbol"] = "LAINUSDT"
        state5["entry_price"] = 50.0
        state5["qty"] = 2.0
        state5["entry_time"] = state_mod.now_ms()
        state_mod.save_control(control_path, {
            "action": "CLOSE_POSITION", "symbol": "TESTUSDT", "requested_at": state_mod.now_ms(),
        })
        check_manual_control(FakeTradeClient(), cfg_ctrl, filters_cache, state5)
        assert state5["current_symbol"] == "LAINUSDT", "Perintah untuk simbol berbeda dari posisi aktif harus DIABAIKAN"
        print("  Perintah untuk simbol yang sudah tidak dipegang -> diabaikan, posisi lain tetap aman -> OK")

        # Skenario D: tidak ada posisi sama sekali saat perintah diproses ->
        # tidak boleh error, cukup diabaikan dengan aman.
        state6 = dict(DEFAULT_STATE)
        state_mod.save_control(control_path, {
            "action": "CLOSE_POSITION", "symbol": "TESTUSDT", "requested_at": state_mod.now_ms(),
        })
        check_manual_control(FakeTradeClient(), cfg_ctrl, filters_cache, state6)
        assert state6["current_symbol"] is None, "Tanpa posisi terbuka, perintah manual harus diabaikan dengan aman"
        print("  Tidak ada posisi terbuka saat perintah diproses -> diabaikan dengan aman, tidak error -> OK")

    print("\n=== SELFTEST: dust sweep ke BNB setelah posisi ditutup ===")

    class FakeDustClient:
        """Client palsu utk mensimulasikan endpoint dust Binance tanpa jaringan.
        Mencatat panggilan (convert_calls) supaya selftest bisa memverifikasi
        PERSIS aset apa yang coba dikonversi -- ini krusial karena proteksi
        modal di try_dust_sweep() harus terbukti tidak pernah menyentuh
        quote asset atau BNB, bukan cuma "kelihatannya begitu"."""

        def __init__(self, convertible_assets):
            self.convertible_assets = convertible_assets  # list of asset code, mis. ["PEPE"]
            self.convert_calls = []
            self.fail_convert = False

        def get_dust_convertible(self, account_type="SPOT"):
            return {"details": [{"asset": a, "amountFree": "1.0", "toBNB": "0.0001"}
                                 for a in self.convertible_assets]}

        def convert_dust(self, assets, account_type="SPOT"):
            self.convert_calls.append(list(assets))
            if self.fail_convert:
                raise BinanceAPIError(400, -5001, "Asset not supported (simulasi)")
            return {"totalTransfered": "0.0001", "totalServiceCharge": "0.000002", "transferResult": []}

    assert cfg["USE_DUST_SWEEP"], "USE_DUST_SWEEP harusnya True di config default"
    # Dust sweep hanya aktif di mode LIVE, jadi skenario di bawah memakai
    # salinan config dengan MODE="LIVE" (tanpa menyentuh config asli).
    cfg = dict(cfg)
    cfg["MODE"] = "LIVE"

    # Skenario A: base asset dari simbol yang baru ditutup MEMANG terdaftar
    # sebagai dust convertible -> harus dikonversi (convert_dust dipanggil
    # persis dengan asset itu saja).
    fake_a = FakeDustClient(convertible_assets=["PEPE"])
    try_dust_sweep(fake_a, cfg, "PEPEUSDT")
    assert fake_a.convert_calls == [["PEPE"]], f"Harusnya convert PEPE saja, dapat: {fake_a.convert_calls}"
    print("  Sisa PEPE terdaftar dust convertible -> convert_dust(['PEPE']) dipanggil -> OK")

    # Skenario B: base asset TIDAK terdaftar sebagai dust convertible (mis.
    # saldo sudah nol atau di atas ambang) -> convert_dust TIDAK boleh dipanggil.
    fake_b = FakeDustClient(convertible_assets=[])
    try_dust_sweep(fake_b, cfg, "PEPEUSDT")
    assert fake_b.convert_calls == [], "Tidak boleh convert kalau asset tidak terdaftar sebagai dust"
    print("  Sisa PEPE TIDAK terdaftar dust convertible -> convert_dust tidak dipanggil -> OK")

    # Skenario C (PROTEKSI MODAL -- paling penting): symbol yang ditutup
    # adalah quote asset itu sendiri seharusnya mustahil terjadi di alur
    # normal (symbol selalu "<BASE>USDT"), tapi diuji eksplisit bahwa base
    # asset "USDT" atau "BNB" TIDAK PERNAH dikonversi walau seandainya lolos
    # sampai ke fungsi ini.
    fake_c = FakeDustClient(convertible_assets=["USDT", "BNB"])
    try_dust_sweep(fake_c, cfg, "BNBUSDT")  # base asset = "BNB"
    assert fake_c.convert_calls == [], "BNB tidak boleh pernah dikonversi (proteksi keras)"
    print("  Simbol dengan base asset BNB -> TIDAK PERNAH dikonversi (proteksi modal) -> OK")

    # Skenario D: MODE=PAPER -> konversi dust memakai /sapi/* bertanda tangan
    # yang dilarang di PAPER, jadi convert_dust TIDAK boleh dipanggil sama sekali.
    cfg_paper = dict(cfg)
    cfg_paper["MODE"] = "PAPER"
    fake_d = FakeDustClient(convertible_assets=["PEPE"])
    try_dust_sweep(fake_d, cfg_paper, "PEPEUSDT")
    assert fake_d.convert_calls == [], "Mode PAPER tidak boleh memanggil convert_dust (endpoint /sapi bertanda tangan)"
    print("  Mode PAPER -> dust sweep dilewati, tidak ada panggilan /sapi -> OK")

    # Skenario E: endpoint convert_dust gagal (mis. kena rate limit Binance)
    # -> harus ditangani dengan aman, TIDAK boleh melempar exception ke pemanggil.
    fake_e = FakeDustClient(convertible_assets=["PEPE"])
    fake_e.fail_convert = True
    try:
        try_dust_sweep(fake_e, cfg, "PEPEUSDT")
        gagal_ditangani = True
    except BinanceAPIError:
        gagal_ditangani = False
    assert gagal_ditangani, "Kegagalan convert_dust (mis. rate limit) harus ditangani, bukan dilempar ke pemanggil"
    print("  convert_dust gagal (simulasi rate limit Binance) -> ditangani dengan aman, tidak crash -> OK")

    # Skenario F: USE_DUST_SWEEP=False -> fitur nonaktif total, tidak ada
    # panggilan API apa pun walau semua syarat lain terpenuhi.
    cfg_no_dust = dict(cfg)
    cfg_no_dust["USE_DUST_SWEEP"] = False
    fake_f = FakeDustClient(convertible_assets=["PEPE"])
    try_dust_sweep(fake_f, cfg_no_dust, "PEPEUSDT")
    assert fake_f.convert_calls == [], "USE_DUST_SWEEP=False harusnya menonaktifkan fitur ini sepenuhnya"
    print("  USE_DUST_SWEEP=False -> fitur nonaktif total -> OK")

    print("\n=== SELFTEST: get_equity None-safe saat API harga gangguan (T-05) ===")

    class EquityClient:
        def __init__(self, fail_price=False):
            self.fail_price = fail_price

        def get_account(self):
            return {"balances": [
                {"asset": "USDT", "free": "500", "locked": "0"},
                {"asset": "TEST", "free": "1.0", "locked": "0"},
            ]}

        def get_price(self, symbol):
            if self.fail_price:
                raise BinanceAPIError(500, None, "simulasi gangguan API")
            return 100.0

    st_eq = dict(DEFAULT_STATE)
    st_eq["current_symbol"] = "TESTUSDT"
    st_eq["qty"] = 1.0
    eq_ok = get_equity(EquityClient(), cfg, st_eq)
    assert abs(eq_ok - 600.0) < 1e-9, f"equity harus 500 + 1x100 = 600, dapat {eq_ok}"
    eq_none = get_equity(EquityClient(fail_price=True), cfg, st_eq)
    assert eq_none is None, "API harga gagal -> equity harus None, BUKAN 500 (posisi hilang semu)"
    print("  Equity normal = 600; API gagal -> None (bukan angka keliru pemicu stop semu) -> OK")

    print("\n=== SELFTEST: CLOSE_ALL_AT_LIMIT benar-benar menutup posisi (K-01/T-06) ===")
    cfg_limit = dict(cfg_exit)
    cfg_limit["CLOSE_ALL_AT_LIMIT"] = True

    # Skenario 1: kill switch aktif + posisi terbuka -> ditutup paksa SATU KALI.
    st_lim = dict(DEFAULT_STATE)
    st_lim["current_symbol"] = "TESTUSDT"
    st_lim["entry_price"] = 100.0
    st_lim["qty"] = 1.0
    st_lim["entry_time"] = state_mod.now_ms()
    st_lim["dd_stopped"] = True
    st_lim["dd_stop_until"] = state_mod.now_ms() + 3600 * 1000
    maybe_force_close_at_risk_limit(FakeTradeClient(), cfg_limit, filters_cache, st_lim, True, 100.0)
    assert st_lim["current_symbol"] is None, "Posisi harus ditutup paksa saat DD stop aktif"
    assert st_lim["_limit_close_done"] is True, "Penanda episode harus di-set setelah penutupan paksa"
    print("  DD stop aktif + posisi terbuka -> SELL paksa, _limit_close_done=True -> OK")

    # Skenario 2: dipanggil lagi di episode yang sama -> tidak menutup dua kali.
    st_lim2 = dict(DEFAULT_STATE)
    st_lim2["current_symbol"] = "TESTUSDT"
    st_lim2["qty"] = 1.0
    st_lim2["dd_stopped"] = True
    st_lim2["_limit_close_done"] = True
    klien2 = FakeTradeClient()
    maybe_force_close_at_risk_limit(klien2, cfg_limit, filters_cache, st_lim2, True, 100.0)
    assert klien2.orders == [], "Episode yang sama tidak boleh menutup dua kali"
    assert st_lim2["current_symbol"] == "TESTUSDT"
    print("  Penanda episode -> tidak ada penutupan berulang -> OK")

    # Skenario 3: fitur dimatikan di config -> tidak menutup apa pun.
    cfg_limit_off = dict(cfg_limit)
    cfg_limit_off["CLOSE_ALL_AT_LIMIT"] = False
    st_lim3 = dict(DEFAULT_STATE)
    st_lim3["current_symbol"] = "TESTUSDT"
    st_lim3["qty"] = 1.0
    st_lim3["dd_stopped"] = True
    klien3 = FakeTradeClient()
    maybe_force_close_at_risk_limit(klien3, cfg_limit_off, filters_cache, st_lim3, True, 100.0)
    assert klien3.orders == [], "CLOSE_ALL_AT_LIMIT=False tidak boleh menutup posisi"
    print("  CLOSE_ALL_AT_LIMIT=False -> posisi dibiarkan, hanya entry dijeda -> OK")

    # Skenario 4: episode selesai (tidak paused) -> penanda direset otomatis.
    st_lim4 = dict(DEFAULT_STATE)
    st_lim4["_limit_close_done"] = True
    maybe_force_close_at_risk_limit(FakeTradeClient(), cfg_limit, filters_cache, st_lim4, False, 100.0)
    assert st_lim4["_limit_close_done"] is False, "Penanda harus direset saat episode stop berakhir"
    print("  Episode stop berakhir -> penanda direset otomatis -> OK")

    print("\n=== SELFTEST: rekonsiliasi state vs saldo exchange saat startup (S-02) ===")

    class ReconClient:
        def __init__(self, balances, fail=False):
            self.balances = balances
            self.fail = fail
            self.calls = 0

        def get_account(self):
            self.calls += 1
            if self.fail:
                raise BinanceAPIError(500, None, "simulasi gangguan")
            return {"balances": [{"asset": a, "free": str(v), "locked": "0"}
                                 for a, v in self.balances.items()]}

    cfg_rec = dict(cfg)
    cfg_rec["QUOTE_ASSET"] = "USDT"
    with tempfile.TemporaryDirectory() as tmprec:
        cfg_rec["STATE_FILE"] = f"{tmprec}/state.json"

        # a. Saldo 0 (mis. state di-reset) -> posisi hantu direset.
        st_r = dict(DEFAULT_STATE)
        st_r["current_symbol"] = "PEPEUSDT"
        st_r["qty"] = 1000.0
        st_r["entry_price"] = 0.01
        reconcile_state_with_exchange(ReconClient({}), cfg_rec, st_r)
        assert st_r["current_symbol"] is None and st_r["qty"] == 0.0, \
            "Saldo 0 -> posisi hantu harus direset"
        print("  Saldo 0 di exchange -> posisi hantu direset -> OK")

        # b. Qty state > saldo nyata -> disesuaikan ke saldo nyata.
        st_r2 = dict(DEFAULT_STATE)
        st_r2["current_symbol"] = "SOLUSDT"
        st_r2["qty"] = 10.0
        st_r2["entry_price"] = 100.0
        reconcile_state_with_exchange(ReconClient({"SOL": 9.5}), cfg_rec, st_r2)
        assert abs(st_r2["qty"] - 9.5) < 1e-12 and st_r2["entry_price"] == 100.0, \
            "Qty harus disesuaikan ke saldo nyata"
        print("  Qty state > saldo nyata -> qty disesuaikan -> OK")

        # c. State kosong tetap perlu satu kali cek saldo untuk mendeteksi
        # orphan asset hasil BUY yang responsnya hilang.
        cl_idle = ReconClient({})
        st_idle = dict(DEFAULT_STATE)
        reconcile_state_with_exchange(cl_idle, cfg_rec, st_idle)
        assert cl_idle.calls == 1 and not st_idle["reconciliation_required"], \
            "State kosong harus cek saldo sekali tetapi tidak boleh memblokir akun benar-benar kosong"
        # d. API gagal -> tidak crash, state dibiarkan.
        st_r4 = dict(DEFAULT_STATE)
        st_r4["current_symbol"] = "PEPEUSDT"
        st_r4["qty"] = 10.0
        reconcile_state_with_exchange(ReconClient({}, fail=True), cfg_rec, st_r4)
        assert st_r4["current_symbol"] == "PEPEUSDT", "API gagal -> state lama dipertahankan"
        print("  State kosong -> cek saldo sekali; API gagal -> aman tanpa crash -> OK")

    print("\n=== SELFTEST: filter usia listing (S-08) ===")
    _listing_age_cache.clear()

    class AgeClient:
        def __init__(self, first_open):
            self.first_open = first_open
            self.calls = 0

        def get_klines(self, symbol, interval, limit=500, start_time_ms=None, end_time_ms=None):
            self.calls += 1
            if self.first_open is None:
                return []
            return [[self.first_open, "1", "1", "1", "1", "1", 0, "1"]]

    NOW10 = 10 * 86_400_000
    age10 = listing_age_days(AgeClient(0), "LAMAUSDT", NOW10)
    assert abs(age10 - 10.0) < 1e-9, f"usia harus 10 hari, dapat {age10}"
    cl_age = AgeClient(NOW10 - 2 * 86_400_000)
    age2 = listing_age_days(cl_age, "BARUUSDT", NOW10)
    assert abs(age2 - 2.0) < 1e-9, f"usia harus 2 hari, dapat {age2}"
    age2b = listing_age_days(cl_age, "BARUUSDT", NOW10)
    assert cl_age.calls == 1, "Hasil kedua harus dari cache, bukan panggilan API baru"
    assert listing_age_days(AgeClient(None), "KOSONGUSDT", NOW10) == 0.0, \
        "Tanpa riwayat -> usia 0 (akan ditolak ambang minimum)"
    print(f"  Usia 10 hari / 2 hari dihitung benar, cache hemat API, tanpa riwayat -> 0 -> OK")

    print("\nSEMUA SELFTEST LULUS.")
    print("(Selftest ini TIDAK menghubungi Binance sama sekali -- murni logika lokal.)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Pump Scanner Bot Binance Spot")
    parser.add_argument("--selftest", action="store_true",
                         help="Jalankan audit logika murni (tanpa jaringan) lalu keluar.")
    args = parser.parse_args()
    if args.selftest:
        selftest()
        return 0

    from config import InvalidModeError
    from runtime_control import BotAlreadyRunningError, BotRuntime

    if CONFIG_LOAD_ERRORS:
        print("Konfigurasi runtime rusak: " + "; ".join(CONFIG_LOAD_ERRORS), file=sys.stderr)
        return 2
    try:
        mode = require_valid_mode(PUMP_CONFIG)
    except InvalidModeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    runtime = BotRuntime(mode)
    try:
        with runtime as lifecycle:
            code = run(PUMP_CONFIG, lifecycle=lifecycle)
            runtime.finish(code, None if code == 0 else "Bot berhenti dengan kode error.")
            return code
    except BotAlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())