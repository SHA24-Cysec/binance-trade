#!/usr/bin/env python3
"""
Bot rotasi "pump scanner" untuk Binance Spot -- memantau SEMUA pair USDT,
mencari koin yang sedang naik tajam + volume tinggi (24 jam), mengkonfirmasi
momentum jangka pendek lewat candle 5 menit, lalu masuk dengan SATU entry
(tanpa martingale/averaging-down). Keluar lewat Take Profit / Breakeven /
Trailing / batas waktu hold / momentum pudar.

INI BUKAN PREDIKSI. Bot ini bereaksi terhadap pergerakan yang SUDAH terjadi.
Baca README.md bagian "Mode Pump Scanner" sebelum menjalankan dengan uang
sungguhan.

CARA PAKAI (sama seperti bot.py):
    pip install -r requirements.txt
    set BINANCE_API_KEY / BINANCE_API_SECRET (lihat README.md)
    python pump_scanner_bot.py --selftest      # audit logika, tanpa jaringan
    python pump_scanner_bot.py                 # jalan (DRY_RUN dulu, default True)
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
import time

from binance_client import BinanceSpotClient, BinanceAPIError, build_filters_cache
from config import PUMP_CONFIG
import market_scanner as scanner
import state as state_mod
import strategy


logger = logging.getLogger("pump_bot")
_shutdown_requested = False

DEFAULT_STATE = {
    "current_symbol": None,
    "entry_price": 0.0,
    "qty": 0.0,
    "entry_time": 0,
    "be_active": False,
    "be_stop_price": 0.0,
    "trailing_active": False,
    "trailing_stop_price": 0.0,
    "last_scan_time": 0,
    "cooldown_until": 0,
    "last_trade_time": 0,
    "day_start_equity": None,
    "day_start_date": None,
    "peak_equity": None,
    "dd_stopped": False,
    "dd_stop_until": 0,
    "daily_stopped": False,
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


def load_pump_state(path: str) -> dict:
    if not __import__("os").path.exists(path):
        return dict(DEFAULT_STATE)
    raw = state_mod.load_state(path)
    merged = dict(DEFAULT_STATE)
    merged.update(raw)
    return merged


def get_balance(account: dict, asset: str) -> float:
    for b in account.get("balances", []):
        if b.get("asset") == asset:
            return float(b.get("free", 0.0))
    return 0.0


def get_equity(client: BinanceSpotClient, config: dict, state: dict) -> float:
    account = client.get_account()
    usdt_free = get_balance(account, config["QUOTE_ASSET"])
    if state["current_symbol"] and state["qty"] > 0:
        try:
            price = client.get_price(state["current_symbol"])
            usdt_free += state["qty"] * price
        except BinanceAPIError:
            pass
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


def reset_position(state: dict) -> None:
    state["current_symbol"] = None
    state["entry_price"] = 0.0
    state["qty"] = 0.0
    state["entry_time"] = 0
    state["be_active"] = False
    state["be_stop_price"] = 0.0
    state["trailing_active"] = False
    state["trailing_stop_price"] = 0.0


def try_dust_sweep(client: BinanceSpotClient, config: dict, symbol: "str | None", dry_run: bool) -> None:
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
    3. Di mode DRY_RUN, fungsi ini TIDAK PERNAH memanggil API sungguhan
       (hanya mencatat log simulasi), konsisten dengan seluruh bagian lain
       bot ini yang tidak mengirim apa pun ke Binance saat DRY_RUN=True.
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

    if dry_run:
        logger.info("[DRY_RUN] Dust sweep dilewati (simulasi): sisa saldo %s (kalau ada) tidak dikonversi.",
                     base_asset)
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


def close_position(client: BinanceSpotClient, config: dict, filters_cache: dict,
                    state: dict, reason: str, dry_run: bool) -> None:
    symbol = state["current_symbol"]
    if not symbol:
        return
    filters = filters_cache.get(symbol)
    qty_to_sell = state["qty"]

    if not dry_run:
        try:
            account = client.get_account()
            base_asset = symbol[: -len(config["QUOTE_ASSET"])]
            free_base = get_balance(account, base_asset)
            qty_to_sell = min(qty_to_sell, free_base)
        except BinanceAPIError as exc:
            logger.error("Gagal ambil saldo sebelum SELL %s: %s", symbol, exc)

    if filters:
        qty_to_sell = filters.round_qty(qty_to_sell)
        if qty_to_sell < float(filters.min_qty):
            # Ini justru kasus dust paling umum: sisa qty setelah pembulatan
            # LOT_SIZE terlalu kecil untuk dijual lewat order biasa. Coba
            # sapu sisa itu ke BNB lewat jalur dust convert Binance sebelum
            # dianggap selesai.
            logger.warning("Qty jual %s (%.8f) di bawah minQty bursa. Posisi direset manual di state.",
                            symbol, qty_to_sell)
            reset_position(state)
            state["cooldown_until"] = state_mod.now_ms() + config["COOLDOWN_MINUTES_AFTER_CLOSE"] * 60 * 1000
            try_dust_sweep(client, config, symbol, dry_run)
            return

    entry_price = state["entry_price"]

    if dry_run:
        logger.info("[DRY_RUN] SELL MARKET %s qty=%.8f (alasan: %s)", symbol, qty_to_sell, reason)
        reset_position(state)
        state["cooldown_until"] = state_mod.now_ms() + config["COOLDOWN_MINUTES_AFTER_CLOSE"] * 60 * 1000
        state["last_trade_time"] = state_mod.now_ms()
        try_dust_sweep(client, config, symbol, dry_run)
        return

    try:
        resp = client.new_market_order(symbol, "SELL", quantity=qty_to_sell)
    except BinanceAPIError as exc:
        logger.error("Order SELL %s (%s) gagal: %s. Posisi TIDAK direset, akan dicoba lagi.", symbol, reason, exc)
        return

    executed_qty = float(resp.get("executedQty", 0.0))
    cumm_quote = float(resp.get("cummulativeQuoteQty", 0.0))
    sell_price = (cumm_quote / executed_qty) if executed_qty > 0 else 0.0
    pnl = (sell_price - entry_price) * executed_qty if entry_price > 0 else 0.0
    logger.info(
        "SELL FILLED %s (%s): qty=%.8f @ avg %.6f | entry=%.6f | estimasi PnL=%.2f %s",
        symbol, reason, executed_qty, sell_price, entry_price, pnl, config["QUOTE_ASSET"],
    )
    reset_position(state)
    state["cooldown_until"] = state_mod.now_ms() + config["COOLDOWN_MINUTES_AFTER_CLOSE"] * 60 * 1000
    state["last_trade_time"] = state_mod.now_ms()
    # Setelah SELL FILLED sungguhan, sisa qty yang tidak terjual (kalau ada,
    # mis. executed_qty < qty_to_sell karena pembulatan bursa) mungkin
    # menyisakan dust kecil -- coba sapu ke BNB.
    try_dust_sweep(client, config, symbol, dry_run)


def open_position(client: BinanceSpotClient, config: dict, filters_cache: dict,
                   state: dict, candidate: "scanner.Candidate", dry_run: bool) -> None:
    filters = filters_cache.get(candidate.symbol)
    if filters is None:
        logger.warning("Tidak ada data filter untuk %s, entry dilewati.", candidate.symbol)
        return

    if config.get("USE_RISK_PERCENT"):
        account = client.get_account() if not dry_run else None
        usdt_free = get_balance(account, config["QUOTE_ASSET"]) if account else 1000.0
        usdt_amount = usdt_free * config["RISK_PERCENT"] / 100.0
    else:
        usdt_amount = config["POSITION_SIZE_USDT"]
    usdt_amount = min(usdt_amount, config["MAX_POSITION_USDT"])
    qty = filters.round_qty(usdt_amount / candidate.last_price)
    notional = qty * candidate.last_price
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

    if dry_run:
        logger.info(
            "[DRY_RUN] BUY MARKET %s qty=%.8f (~%.2f %s @ %.6f) | 24h=%.2f%% | vol24h=%.0f | alasan: %s",
            candidate.symbol, qty, notional, config["QUOTE_ASSET"], candidate.last_price,
            candidate.price_change_pct, candidate.quote_volume, candidate.confirm_reason,
        )
        state["current_symbol"] = candidate.symbol
        state["entry_price"] = candidate.last_price
        state["qty"] = qty
        state["entry_time"] = state_mod.now_ms()
        state["last_trade_time"] = state_mod.now_ms()
        return

    try:
        resp = client.new_market_order(candidate.symbol, "BUY", quantity=qty)
    except BinanceAPIError as exc:
        logger.error("Order BUY %s gagal: %s", candidate.symbol, exc)
        return

    executed_qty = float(resp.get("executedQty", 0.0))
    cumm_quote = float(resp.get("cummulativeQuoteQty", 0.0))
    if executed_qty <= 0:
        logger.error("Order BUY %s terkirim tapi executedQty=0. Respons: %s", candidate.symbol, resp)
        return
    fill_price = cumm_quote / executed_qty

    logger.info(
        "BUY FILLED %s: qty=%.8f @ avg %.6f | 24h=%.2f%% | vol24h=%.0f | alasan: %s",
        candidate.symbol, executed_qty, fill_price, candidate.price_change_pct,
        candidate.quote_volume, candidate.confirm_reason,
    )
    state["current_symbol"] = candidate.symbol
    state["entry_price"] = fill_price
    state["qty"] = executed_qty
    state["entry_time"] = state_mod.now_ms()
    state["last_trade_time"] = state_mod.now_ms()


def check_manual_control(client: BinanceSpotClient, config: dict, filters_cache: dict,
                          state: dict, dry_run: bool) -> None:
    """Cek "control file" yang bisa ditulis dashboard.py (proses terpisah)
    untuk perintah manual, mis. tombol "Jual Sekarang". Dipanggil tiap
    iterasi loop utama (maks setiap LOOP_INTERVAL_SECONDS, default 15 detik)
    supaya perintah dari dashboard direspons cepat tanpa perlu bot di-restart.

    Kenapa lewat file, bukan panggilan langsung? Bot dan dashboard sengaja
    berjalan sebagai DUA PROSES terpisah (lihat run.py) supaya crash di satu
    proses tidak menjatuhkan proses lain. Satu-satunya cara komunikasi antar
    proses yang sudah dipakai di proyek ini adalah file (pump_bot_state.json),
    jadi kontrol manual memakai pola yang sama demi konsistensi."""
    control_path = config.get("CONTROL_FILE", "pump_bot_control.json")
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
    close_position(client, config, filters_cache, state, "MANUAL_CLOSE_DASHBOARD", dry_run)


def manage_exit(client: BinanceSpotClient, config: dict, filters_cache: dict,
                 state: dict, current_price: float, dry_run: bool) -> None:
    if not state["current_symbol"] or state["qty"] <= 0 or state["entry_price"] <= 0:
        return

    pnl_pct = (current_price / state["entry_price"] - 1.0) * 100.0
    hold_minutes = (state_mod.now_ms() - state["entry_time"]) / 60000.0

    # Stop Loss: batas kerugian maksimum dari harga entry. Dicek PALING AWAL
    # dan TIDAK bergantung pada Breakeven/Trailing aktif atau tidak -- ini
    # jaring pengaman kalau harga langsung turun sejak entry dan tidak pernah
    # sempat untung (BE/Trailing baru aktif setelah profit menyentuh trigger-nya
    # masing-masing, jadi TIDAK melindungi skenario ini tanpa Stop Loss).
    if config["USE_STOP_LOSS"] and pnl_pct <= -abs(config["SL_PCT"]):
        close_position(client, config, filters_cache, state, "STOP_LOSS", dry_run)
        return

    if config["USE_BREAKEVEN"] and not state["be_active"] and pnl_pct >= config["BE_TRIGGER_PCT"]:
        state["be_active"] = True
        state["be_stop_price"] = state["entry_price"] * (1 + config["BE_LOCK_PCT"] / 100.0)
        logger.info("%s: Breakeven diaktifkan, stop dikunci di %.6f", state["current_symbol"], state["be_stop_price"])

    if config["USE_TRAILING"]:
        if not state["trailing_active"] and pnl_pct >= config["TRAILING_START_PCT"]:
            state["trailing_active"] = True
            state["trailing_stop_price"] = current_price * (1 - config["TRAILING_STEP_PCT"] / 100.0)
            logger.info("%s: Trailing stop diaktifkan di %.6f", state["current_symbol"], state["trailing_stop_price"])
        elif state["trailing_active"]:
            candidate_stop = current_price * (1 - config["TRAILING_STEP_PCT"] / 100.0)
            if candidate_stop > state["trailing_stop_price"]:
                state["trailing_stop_price"] = candidate_stop

    reasons = []
    if config["USE_TP"] and pnl_pct >= config["TP_PCT"]:
        reasons.append("TAKE_PROFIT")
    if state["be_active"] and current_price <= state["be_stop_price"]:
        reasons.append("BREAKEVEN")
    if state["trailing_active"] and current_price <= state["trailing_stop_price"]:
        reasons.append("TRAILING_STOP")
    if hold_minutes >= config["MAX_HOLD_MINUTES"]:
        reasons.append("MAX_HOLD_TIME")

    if reasons:
        close_position(client, config, filters_cache, state, "+".join(reasons), dry_run)


def run(config: dict) -> None:
    setup_logging(config)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if not config["DRY_RUN"] and (not config["API_KEY"] or not config["API_SECRET"]):
        logger.error("DRY_RUN=False tapi API key/secret belum di-set. Bot dihentikan demi keamanan.")
        sys.exit(1)

    logger.info("=" * 70)
    logger.info("Pump Scanner Bot mulai berjalan. DRY_RUN=%s", config["DRY_RUN"])
    if config["DRY_RUN"]:
        logger.warning("MODE DRY_RUN AKTIF: tidak ada order sungguhan yang dikirim.")
    logger.info("=" * 70)

    client = BinanceSpotClient(config["API_KEY"], config["API_SECRET"], config["BASE_URL"])
    client.sync_time()

    logger.info("Mengambil exchangeInfo untuk semua simbol (sekali di awal)...")
    exchange_info = client.get_exchange_info()
    filters_cache = build_filters_cache(exchange_info)
    filters_cache_time = time.time()
    logger.info("Filter untuk %d simbol berhasil dimuat.", len(filters_cache))

    state = load_pump_state(config["STATE_FILE"])
    if state["current_symbol"]:
        logger.info("Melanjutkan posisi yang sudah ada: %s qty=%.8f @ %.6f",
                    state["current_symbol"], state["qty"], state["entry_price"])

    consecutive_errors = 0
    last_time_sync = time.time()
    last_heartbeat = 0.0
    TIME_SYNC_INTERVAL_SECONDS = 15 * 60
    FILTERS_REFRESH_INTERVAL_SECONDS = 6 * 3600

    def klines_fetcher(symbol: str):
        raw = client.get_klines(symbol, config["CONFIRM_INTERVAL"], limit=config["CONFIRM_LOOKBACK_BARS"])
        return strategy.parse_klines(raw)

    while not _shutdown_requested:
        loop_start = time.time()
        try:
            if time.time() - last_time_sync > TIME_SYNC_INTERVAL_SECONDS:
                client.sync_time()
                last_time_sync = time.time()

            if time.time() - filters_cache_time > FILTERS_REFRESH_INTERVAL_SECONDS:
                exchange_info = client.get_exchange_info()
                filters_cache = build_filters_cache(exchange_info)
                filters_cache_time = time.time()
                logger.info("Filter simbol disegarkan ulang (%d simbol).", len(filters_cache))

            # Perintah manual dari dashboard (mis. "Jual Sekarang") dicek
            # PALING AWAL setiap iterasi, sebelum logika exit otomatis --
            # kalau user memintanya, itu harus didahulukan.
            check_manual_control(client, config, filters_cache, state, config["DRY_RUN"])

            current_price = None
            if state["current_symbol"]:
                current_price = client.get_price(state["current_symbol"])

            equity = get_equity(client, config, state) if not config["DRY_RUN"] else (
                config["MAX_POSITION_USDT"] * 5
            )
            entries_paused = update_equity_controls(state, equity, config)

            if state["current_symbol"] and current_price is not None:
                manage_exit(client, config, filters_cache, state, current_price, config["DRY_RUN"])

            do_scan = time.time() * 1000 - state.get("last_scan_time", 0) > config["MARKET_SCAN_INTERVAL_SECONDS"] * 1000
            if do_scan:
                state["last_scan_time"] = state_mod.now_ms()
                tickers = client.get_ticker_24hr_all()

                if state["current_symbol"] and config["MOMENTUM_FADE_EXIT"]:
                    still_ranked = scanner.is_symbol_still_ranked(
                        state["current_symbol"], tickers, config, config["MOMENTUM_FADE_RANK_THRESHOLD"]
                    )
                    if not still_ranked and current_price is not None:
                        logger.info("%s sudah keluar dari top-%d gainer, momentum dianggap pudar.",
                                    state["current_symbol"], config["MOMENTUM_FADE_RANK_THRESHOLD"])
                        close_position(client, config, filters_cache, state, "MOMENTUM_FADE", config["DRY_RUN"])

                now = state_mod.now_ms()
                can_enter = (
                    not state["current_symbol"]
                    and now >= state.get("cooldown_until", 0)
                    and not entries_paused
                    and now - state.get("last_trade_time", 0) >= config["MIN_SECONDS_BETWEEN_TRADES"] * 1000
                )
                if can_enter:
                    best = scanner.find_best_candidate(tickers, klines_fetcher, config)
                    if best:
                        book = client.get_book_ticker(best.symbol)
                        bid, ask = float(book["bidPrice"]), float(book["askPrice"])
                        mid = (bid + ask) / 2.0
                        spread_pct = ((ask - bid) / mid * 100.0) if mid > 0 else 999.0
                        if spread_pct <= config["MAX_SPREAD_PCT"]:
                            logger.info(
                                "Kandidat terpilih: %s (24h=%.2f%%, vol=%.0f, spread=%.3f%%)",
                                best.symbol, best.price_change_pct, best.quote_volume, spread_pct,
                            )
                            open_position(client, config, filters_cache, state, best, config["DRY_RUN"])
                        else:
                            logger.info("Kandidat %s dilewati: spread %.3f%% > batas %.3f%%.",
                                        best.symbol, spread_pct, config["MAX_SPREAD_PCT"])
                    else:
                        logger.debug("Tidak ada kandidat pump yang lolos filter+konfirmasi saat ini.")

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
                logger.info("[HEARTBEAT] Bot masih berjalan | equity=%.2f %s | %s%s",
                            equity, config["QUOTE_ASSET"], posisi_info, flag_str)

            state_mod.save_state(config["STATE_FILE"], state)
            consecutive_errors = 0

        except BinanceAPIError as exc:
            consecutive_errors += 1
            logger.error("BinanceAPIError (%d berturut-turut): %s", consecutive_errors, exc)
        except Exception as exc:  # noqa: BLE001
            consecutive_errors += 1
            logger.exception("Error tak terduga (%d berturut-turut): %s", consecutive_errors, exc)

        if consecutive_errors >= 10:
            logger.critical("10 error berturut-turut. Bot berhenti total untuk keamanan.")
            break

        elapsed = time.time() - loop_start
        time.sleep(max(1.0, config["LOOP_INTERVAL_SECONDS"] - elapsed))

    logger.info("Bot berhenti.")


def selftest() -> None:
    print("=== SELFTEST: filter & ranking kandidat pump ===")
    cfg = dict(PUMP_CONFIG)
    tickers = [
        {"symbol": "AUSDT", "priceChangePercent": "15.0", "quoteVolume": "5000000", "lastPrice": "1.0"},
        {"symbol": "BUSDT", "priceChangePercent": "25.0", "quoteVolume": "3000000", "lastPrice": "2.0"},
        {"symbol": "CUSDT", "priceChangePercent": "3.0", "quoteVolume": "9000000", "lastPrice": "0.5"},   # gagal: %naik kurang
        {"symbol": "DUSDT", "priceChangePercent": "40.0", "quoteVolume": "10000", "lastPrice": "0.1"},     # gagal: volume kurang
        {"symbol": "BTCUPUSDT", "priceChangePercent": "50.0", "quoteVolume": "9000000", "lastPrice": "3.0"},  # gagal: leveraged token
        {"symbol": "USDCUSDT", "priceChangePercent": "20.0", "quoteVolume": "9000000", "lastPrice": "1.0"},   # gagal: stablecoin
    ]
    ranked = scanner.filter_and_rank_candidates(tickers, cfg)
    symbols = [c.symbol for c in ranked]
    print("  Lolos filter & terurut:", symbols)
    assert symbols == ["BUSDT", "AUSDT"], f"Hasil filter/ranking salah: {symbols}"
    print("  -> OK (leveraged token, stablecoin, volume rendah, %naik rendah semua ter-exclude benar)")

    print("\n=== SELFTEST: konfirmasi momentum ===")
    t = 0

    def bar(o, c):
        nonlocal t
        h = max(o, c) * 1.001
        l = min(o, c) * 0.999
        k = strategy.Kline(open_time=t, open=o, high=h, low=l, close=c, close_time=t + 299999)
        t += 300000
        return k

    # Skenario 1: momentum masih naik jelas -> harus lolos
    klines_up = [bar(100 + i, 100 + i + 0.8) for i in range(10)]
    ok, reason = scanner.confirm_momentum(klines_up, cfg)
    print(f"  Skenario momentum naik konsisten -> confirmed={ok} ({reason})")
    assert ok, "Momentum naik konsisten harusnya lolos konfirmasi"

    # Skenario 2: candle terakhir reversal kuat (dibuka tinggi, ditutup dekat low) -> harus gagal
    klines_reversal = [bar(100 + i, 100 + i + 0.8) for i in range(9)]
    klines_reversal.append(bar(115, 108))  # candle merah besar menutup dekat low
    ok2, reason2 = scanner.confirm_momentum(klines_reversal, cfg)
    print(f"  Skenario candle reversal kuat di akhir -> confirmed={ok2} ({reason2})")
    assert not ok2, "Candle reversal kuat harusnya GAGAL konfirmasi"

    print("\n=== SELFTEST: filter VWAP (USE_VWAP_FILTER, VWAP_MAX_EXTENSION_PCT) ===")
    assert cfg["USE_VWAP_FILTER"], "USE_VWAP_FILTER harusnya True di config default"

    def bar_vol(o, c, vol=1000.0):
        nonlocal t
        h = max(o, c) * 1.001
        l = min(o, c) * 0.999
        qvol = vol * ((o + c) / 2.0)
        k = strategy.Kline(open_time=t, open=o, high=h, low=l, close=c, close_time=t + 299999,
                            volume=vol, quote_volume=qvol)
        t += 300000
        return k

    # Skenario A: harga naik landai (extension dari VWAP kecil) -> harus lolos
    klines_wajar = [bar_vol(100 + i * 0.1, 100 + i * 0.1 + 0.08) for i in range(20)]
    vok, vreason = scanner.check_vwap_extension(klines_wajar, cfg)
    print(f"  Kenaikan landai -> lolos={vok} ({vreason})")
    assert vok, "Kenaikan landai (extension kecil dari VWAP) harusnya lolos filter VWAP"

    # Skenario B: candle terakhir loncat tajam jauh di atas rata-rata window
    # (harga >> VWAP_MAX_EXTENSION_PCT dari VWAP window) -> harus DITOLAK.
    klines_ekstrem = list(klines_wajar[:-1])
    lompat = klines_wajar[-2].close * 1.10  # loncat +10%, jauh melebihi ambang 5%
    klines_ekstrem.append(bar_vol(lompat, lompat * 1.001))
    vok2, vreason2 = scanner.check_vwap_extension(klines_ekstrem, cfg)
    print(f"  Loncat +10% dari harga sebelumnya -> lolos={vok2} ({vreason2})")
    assert not vok2, "Harga yang melompat jauh di atas VWAP harusnya DITOLAK filter VWAP"

    # Skenario C: harga turun di bawah VWAP window -> harus DITOLAK (tekanan
    # beli belum dominan), meskipun jaraknya tidak "ekstrem" secara persentase.
    klines_bawah = list(klines_wajar[:-1])
    turun = klines_wajar[-2].close * 0.95
    klines_bawah.append(bar_vol(turun, turun * 1.001))
    vok3, vreason3 = scanner.check_vwap_extension(klines_bawah, cfg)
    print(f"  Harga di bawah VWAP window -> lolos={vok3} ({vreason3})")
    assert not vok3, "Harga di bawah VWAP harusnya DITOLAK filter VWAP"

    # Skenario D: USE_VWAP_FILTER=False -> filter dilewati sepenuhnya, kasus
    # ekstrem yang sama di skenario B harus lolos begitu filter dimatikan.
    cfg_no_vwap = dict(cfg)
    cfg_no_vwap["USE_VWAP_FILTER"] = False
    vok4, vreason4 = scanner.check_vwap_extension(klines_ekstrem, cfg_no_vwap)
    print(f"  Filter VWAP dimatikan, kasus ekstrem yang sama -> lolos={vok4} ({vreason4})")
    assert vok4, "Kalau USE_VWAP_FILTER=False, filter harus dilewati sepenuhnya (selalu lolos)"

    print("\n=== SELFTEST: simulasi exit (TP/Breakeven/Trailing) ===")
    from decimal import Decimal as D
    from binance_client import SymbolFilters
    filters_cache = {"TESTUSDT": SymbolFilters(step_size=D("0.01"), min_qty=D("0.01"),
                                                min_notional=D("5"), tick_size=D("0.0001"))}
    state = dict(DEFAULT_STATE)
    state["current_symbol"] = "TESTUSDT"
    state["entry_price"] = 100.0
    state["qty"] = 1.0
    state["entry_time"] = state_mod.now_ms()

    manage_exit(None, cfg, filters_cache, state, 103.5, dry_run=True)  # >= BE_TRIGGER_PCT (3.0%)
    assert state["be_active"], "Breakeven harusnya sudah aktif di profit 3.5%"
    assert state["current_symbol"] == "TESTUSDT", "Belum boleh close, baru breakeven aktif"
    print(f"  Setelah profit +3.5%: be_active={state['be_active']}, be_stop={state['be_stop_price']:.4f} -> OK")

    manage_exit(None, cfg, filters_cache, state, 106.5, dry_run=True)  # TP_PCT = 6.0
    assert state["current_symbol"] is None, "Posisi harusnya sudah tertutup kena TAKE_PROFIT"
    print("  Setelah profit +6.5%: posisi tertutup (TAKE_PROFIT) -> OK")

    print("\n=== SELFTEST: Stop Loss (harga langsung turun sejak entry, TIDAK sempat untung) ===")
    assert cfg["USE_STOP_LOSS"], "USE_STOP_LOSS harusnya True di config default"
    state2 = dict(DEFAULT_STATE)
    state2["current_symbol"] = "TESTUSDT"
    state2["entry_price"] = 100.0
    state2["qty"] = 1.0
    state2["entry_time"] = state_mod.now_ms()

    # Rugi -2% dulu -- masih di atas ambang SL_PCT (3.0%), posisi harus TETAP terbuka.
    manage_exit(None, cfg, filters_cache, state2, 98.0, dry_run=True)
    assert state2["current_symbol"] == "TESTUSDT", "Rugi -2% belum boleh kena Stop Loss (ambang 3.0%)"
    assert not state2["be_active"], "Breakeven tidak boleh aktif kalau posisi rugi"
    print("  Rugi -2%: posisi masih terbuka, BE/Trailing tidak aktif -> OK")

    # Rugi -3.5% -- melewati SL_PCT (3.0%), posisi harus dipaksa tertutup STOP_LOSS,
    # walau BE_TRIGGER_PCT/TRAILING_START_PCT tidak pernah tersentuh sama sekali.
    manage_exit(None, cfg, filters_cache, state2, 96.5, dry_run=True)
    assert state2["current_symbol"] is None, "Posisi harusnya sudah tertutup kena STOP_LOSS di rugi -3.5%"
    print("  Rugi -3.5%: posisi tertutup (STOP_LOSS) -> OK")

    print("\n=== SELFTEST: perintah manual 'Jual Sekarang' dari dashboard (control file) ===")
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        control_path = f"{tmpdir}/pump_bot_control.json"
        cfg_ctrl = dict(cfg)
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
        check_manual_control(None, cfg_ctrl, filters_cache, state3, dry_run=True)
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
        check_manual_control(None, cfg_ctrl, filters_cache, state4, dry_run=True)
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
        check_manual_control(None, cfg_ctrl, filters_cache, state5, dry_run=True)
        assert state5["current_symbol"] == "LAINUSDT", "Perintah untuk simbol berbeda dari posisi aktif harus DIABAIKAN"
        print("  Perintah untuk simbol yang sudah tidak dipegang -> diabaikan, posisi lain tetap aman -> OK")

        # Skenario D: tidak ada posisi sama sekali saat perintah diproses ->
        # tidak boleh error, cukup diabaikan dengan aman.
        state6 = dict(DEFAULT_STATE)
        state_mod.save_control(control_path, {
            "action": "CLOSE_POSITION", "symbol": "TESTUSDT", "requested_at": state_mod.now_ms(),
        })
        check_manual_control(None, cfg_ctrl, filters_cache, state6, dry_run=True)
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

    # Skenario A: base asset dari simbol yang baru ditutup MEMANG terdaftar
    # sebagai dust convertible -> harus dikonversi (convert_dust dipanggil
    # persis dengan asset itu saja).
    fake_a = FakeDustClient(convertible_assets=["PEPE"])
    try_dust_sweep(fake_a, cfg, "PEPEUSDT", dry_run=False)
    assert fake_a.convert_calls == [["PEPE"]], f"Harusnya convert PEPE saja, dapat: {fake_a.convert_calls}"
    print("  Sisa PEPE terdaftar dust convertible -> convert_dust(['PEPE']) dipanggil -> OK")

    # Skenario B: base asset TIDAK terdaftar sebagai dust convertible (mis.
    # saldo sudah nol atau di atas ambang) -> convert_dust TIDAK boleh dipanggil.
    fake_b = FakeDustClient(convertible_assets=[])
    try_dust_sweep(fake_b, cfg, "PEPEUSDT", dry_run=False)
    assert fake_b.convert_calls == [], "Tidak boleh convert kalau asset tidak terdaftar sebagai dust"
    print("  Sisa PEPE TIDAK terdaftar dust convertible -> convert_dust tidak dipanggil -> OK")

    # Skenario C (PROTEKSI MODAL -- paling penting): symbol yang ditutup
    # adalah quote asset itu sendiri seharusnya mustahil terjadi di alur
    # normal (symbol selalu "<BASE>USDT"), tapi diuji eksplisit bahwa base
    # asset "USDT" atau "BNB" TIDAK PERNAH dikonversi walau seandainya lolos
    # sampai ke fungsi ini.
    fake_c = FakeDustClient(convertible_assets=["USDT", "BNB"])
    try_dust_sweep(fake_c, cfg, "BNBUSDT", dry_run=False)  # base asset = "BNB"
    assert fake_c.convert_calls == [], "BNB tidak boleh pernah dikonversi (proteksi keras)"
    print("  Simbol dengan base asset BNB -> TIDAK PERNAH dikonversi (proteksi modal) -> OK")

    # Skenario D: DRY_RUN=True -> tidak boleh ada panggilan API sama sekali,
    # walaupun asset-nya terdaftar convertible.
    fake_d = FakeDustClient(convertible_assets=["PEPE"])
    try_dust_sweep(fake_d, cfg, "PEPEUSDT", dry_run=True)
    assert fake_d.convert_calls == [], "Mode DRY_RUN tidak boleh memanggil convert_dust sama sekali"
    print("  Mode DRY_RUN aktif -> tidak ada panggilan API sungguhan -> OK")

    # Skenario E: endpoint convert_dust gagal (mis. kena rate limit Binance)
    # -> harus ditangani dengan aman, TIDAK boleh melempar exception ke pemanggil.
    fake_e = FakeDustClient(convertible_assets=["PEPE"])
    fake_e.fail_convert = True
    try:
        try_dust_sweep(fake_e, cfg, "PEPEUSDT", dry_run=False)
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
    try_dust_sweep(fake_f, cfg_no_dust, "PEPEUSDT", dry_run=False)
    assert fake_f.convert_calls == [], "USE_DUST_SWEEP=False harusnya menonaktifkan fitur ini sepenuhnya"
    print("  USE_DUST_SWEEP=False -> fitur nonaktif total -> OK")

    print("\nSEMUA SELFTEST LULUS.")
    print("(Selftest ini TIDAK menghubungi Binance sama sekali -- murni logika lokal.)")


def main():
    parser = argparse.ArgumentParser(description="Pump Scanner Bot Binance Spot")
    parser.add_argument("--selftest", action="store_true",
                         help="Jalankan audit logika murni (tanpa jaringan) lalu keluar.")
    args = parser.parse_args()
    if args.selftest:
        selftest()
        return
    run(PUMP_CONFIG)


if __name__ == "__main__":
    main()