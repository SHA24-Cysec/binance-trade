#!/usr/bin/env python3
"""
Bot Grid Martingale untuk Binance Spot (BTCUSDT) -- adaptasi dari
Gold_Grid_Martingale_Pro.mq5.

CARA PAKAI
----------
1. Baca README.md dulu, terutama bagian "Perbedaan Penting dari EA Asli"
   dan "Peringatan Risiko".
2. Install dependency:      pip install -r requirements.txt
3. Set kredensial:          export BINANCE_API_KEY=...
                             export BINANCE_API_SECRET=...
4. Cek dulu logikanya tanpa koneksi apa pun:
                             python bot.py --selftest
5. Jalankan dengan DRY_RUN=True dulu (default di config.py) untuk melihat
   bot "berpikir" pakai data pasar ASLI tanpa benar-benar kirim order:
                             python bot.py
6. Kalau sudah yakin, baru ubah DRY_RUN jadi False di config.py.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
import time
from decimal import Decimal

from binance_client import BinanceSpotClient, SymbolFilters, BinanceAPIError
from config import CONFIG
import state as state_mod
import strategy


logger = logging.getLogger("bot")
_shutdown_requested = False


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


# -----------------------------------------------------------------------
# Helper akun & equity
# -----------------------------------------------------------------------
def get_balance(account: dict, asset: str) -> float:
    for b in account.get("balances", []):
        if b.get("asset") == asset:
            return float(b.get("free", 0.0))
    return 0.0


def get_equity(client: BinanceSpotClient, config: dict, current_price: float) -> float:
    account = client.get_account()
    usdt_free = get_balance(account, config["QUOTE_ASSET"])
    base_free = get_balance(account, config["BASE_ASSET"])
    return usdt_free + base_free * current_price


# -----------------------------------------------------------------------
# Manajemen posisi (state)
# -----------------------------------------------------------------------
def recompute_avg(state: dict) -> None:
    layers = state["layers"]
    total_qty = sum(l["qty"] for l in layers)
    total_cost = sum(l["qty"] * l["price"] for l in layers)
    state["total_qty"] = total_qty
    state["avg_price"] = (total_cost / total_qty) if total_qty > 0 else 0.0


def reset_position(state: dict) -> None:
    state["layers"] = []
    state["avg_price"] = 0.0
    state["total_qty"] = 0.0
    state["be_active"] = False
    state["be_stop_price"] = 0.0
    state["trailing_active"] = False
    state["trailing_stop_price"] = 0.0


def add_layer(state: dict, price: float, qty: float) -> None:
    state["layers"].append({"price": price, "qty": qty, "time": state_mod.now_ms()})
    recompute_avg(state)


# -----------------------------------------------------------------------
# Kontrol risiko (drawdown & PnL harian)
# -----------------------------------------------------------------------
def update_equity_controls(state: dict, equity: float, config: dict) -> bool:
    """Return True jika entry BARU harus dijeda (posisi yang sudah ada tetap
    dikelola exit-nya seperti biasa)."""
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
            logger.critical(
                "STOP DRAWDOWN: turun %.2f%% dari puncak equity (batas %.2f%%). "
                "Entry baru dijeda %d jam.",
                dd_pct, config["MAX_DRAWDOWN_PERCENT"], config["DD_COOLDOWN_HOURS"],
            )

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


# -----------------------------------------------------------------------
# Eksekusi order
# -----------------------------------------------------------------------
def place_buy(client: BinanceSpotClient, config: dict, filters: SymbolFilters,
              usdt_amount: float, ref_price: float, dry_run: bool) -> tuple[float, float] | None:
    qty = filters.round_qty(usdt_amount / ref_price)
    notional = qty * ref_price
    if qty < float(filters.min_qty) or notional < float(filters.min_notional):
        logger.warning(
            "Order BUY dibatalkan: qty=%.8f notional=%.2f di bawah batas bursa "
            "(minQty=%.8f, minNotional=%.2f).",
            qty, notional, float(filters.min_qty), float(filters.min_notional),
        )
        return None

    if dry_run:
        logger.info("[DRY_RUN] BUY MARKET %s qty=%.8f (~%.2f %s @ %.2f)",
                    config["SYMBOL"], qty, notional, config["QUOTE_ASSET"], ref_price)
        return ref_price, qty

    try:
        resp = client.new_market_order(config["SYMBOL"], "BUY", quantity=qty)
    except BinanceAPIError as exc:
        logger.error("Order BUY gagal: %s", exc)
        return None

    executed_qty = float(resp.get("executedQty", 0.0))
    cumm_quote = float(resp.get("cummulativeQuoteQty", 0.0))
    if executed_qty <= 0:
        logger.error("Order BUY terkirim tapi executedQty=0. Respons: %s", resp)
        return None
    fill_price = cumm_quote / executed_qty
    logger.info("BUY FILLED: qty=%.8f @ avg %.2f (order id=%s)",
                executed_qty, fill_price, resp.get("orderId"))
    return fill_price, executed_qty


def close_position(client: BinanceSpotClient, config: dict, filters: SymbolFilters,
                    state: dict, reason: str, dry_run: bool) -> None:
    qty_to_sell = state["total_qty"]
    if not dry_run:
        try:
            account = client.get_account()
            free_base = get_balance(account, config["BASE_ASSET"])
            qty_to_sell = min(qty_to_sell, free_base)
        except BinanceAPIError as exc:
            logger.error("Gagal mengambil saldo sebelum SELL: %s", exc)

    qty_to_sell = filters.round_qty(qty_to_sell)
    if qty_to_sell < float(filters.min_qty):
        logger.warning("Qty jual (%.8f) di bawah minQty bursa, basket direset manual di state.", qty_to_sell)
        reset_position(state)
        return

    avg_entry = state["avg_price"]

    if dry_run:
        logger.info("[DRY_RUN] SELL MARKET %s qty=%.8f (alasan: %s)", config["SYMBOL"], qty_to_sell, reason)
        reset_position(state)
        state["cooldown_until"] = state_mod.now_ms() + config["COOLDOWN_MINUTES_AFTER_CLOSE"] * 60 * 1000
        state["last_order_time"] = state_mod.now_ms()
        return

    try:
        resp = client.new_market_order(config["SYMBOL"], "SELL", quantity=qty_to_sell)
    except BinanceAPIError as exc:
        logger.error("Order SELL (%s) gagal: %s. Basket TIDAK direset, akan dicoba lagi.", reason, exc)
        return

    executed_qty = float(resp.get("executedQty", 0.0))
    cumm_quote = float(resp.get("cummulativeQuoteQty", 0.0))
    sell_price = (cumm_quote / executed_qty) if executed_qty > 0 else 0.0
    pnl = (sell_price - avg_entry) * executed_qty if avg_entry > 0 else 0.0
    logger.info(
        "SELL FILLED (%s): qty=%.8f @ avg %.2f | avg_entry=%.2f | estimasi PnL=%.2f %s (order id=%s)",
        reason, executed_qty, sell_price, avg_entry, pnl, config["QUOTE_ASSET"], resp.get("orderId"),
    )
    reset_position(state)
    state["cooldown_until"] = state_mod.now_ms() + config["COOLDOWN_MINUTES_AFTER_CLOSE"] * 60 * 1000
    state["last_order_time"] = state_mod.now_ms()


# -----------------------------------------------------------------------
# Manajemen exit (TP / Breakeven / Trailing) -- basket BUY-only
# -----------------------------------------------------------------------
def manage_exit(client: BinanceSpotClient, config: dict, filters: SymbolFilters,
                 state: dict, current_price: float, dry_run: bool) -> None:
    if state["total_qty"] <= 0 or state["avg_price"] <= 0:
        return

    pnl_pct = (current_price / state["avg_price"] - 1.0) * 100.0

    if config["USE_BASKET_BREAKEVEN"] and not state["be_active"]:
        if pnl_pct >= config["BE_TRIGGER_PCT"]:
            state["be_active"] = True
            state["be_stop_price"] = state["avg_price"] * (1 + config["BE_LOCK_PCT"] / 100.0)
            logger.info("Breakeven diaktifkan. Stop dikunci di %.2f", state["be_stop_price"])

    if config["USE_BASKET_TRAILING"]:
        if not state["trailing_active"] and pnl_pct >= config["TRAILING_START_PCT"]:
            state["trailing_active"] = True
            state["trailing_stop_price"] = current_price * (1 - config["TRAILING_STEP_PCT"] / 100.0)
            logger.info("Trailing stop diaktifkan di %.2f", state["trailing_stop_price"])
        elif state["trailing_active"]:
            candidate = current_price * (1 - config["TRAILING_STEP_PCT"] / 100.0)
            if candidate > state["trailing_stop_price"]:
                state["trailing_stop_price"] = candidate

    reasons = []
    if config["USE_BASKET_TP"] and pnl_pct >= config["BASKET_TP_PCT"]:
        reasons.append("TAKE_PROFIT")
    if state["be_active"] and current_price <= state["be_stop_price"]:
        reasons.append("BREAKEVEN")
    if state["trailing_active"] and current_price <= state["trailing_stop_price"]:
        reasons.append("TRAILING_STOP")

    if reasons:
        close_position(client, config, filters, state, "+".join(reasons), dry_run)


# -----------------------------------------------------------------------
# Manajemen entry (initial + grid martingale) -- BUY only
# -----------------------------------------------------------------------
def manage_entry(client: BinanceSpotClient, config: dict, filters: SymbolFilters,
                  state: dict, klines: list, current_price: float, bid: float, ask: float,
                  entries_paused: bool, dry_run: bool) -> None:
    now = state_mod.now_ms()

    if now < state.get("cooldown_until", 0):
        return
    if entries_paused:
        return
    if now - state.get("last_order_time", 0) < config["MIN_SECONDS_BETWEEN_ORDERS"] * 1000:
        return

    mid = (bid + ask) / 2.0 if (bid and ask) else current_price
    spread_pct = ((ask - bid) / mid * 100.0) if mid > 0 else 999.0
    if spread_pct > config["MAX_SPREAD_PCT"]:
        logger.debug("Entry ditahan: spread %.3f%% > batas %.3f%%", spread_pct, config["MAX_SPREAD_PCT"])
        return

    idx = -2 if config["USE_CLOSED_BAR_SIGNAL"] else -1
    last_bar_time = klines[idx].open_time

    if last_bar_time != state.get("last_signal_bar_time"):
        signal = strategy.get_trend_signal(
            klines, config["ST_ATR_PERIOD"], config["ST_MULTIPLIER"],
            config["EMA_PERIOD"], config["USE_CLOSED_BAR_SIGNAL"],
        )
        state["last_signal_bar_time"] = last_bar_time
        state["current_trend"] = signal
        logger.debug("Bar baru terdeteksi (t=%d). Sinyal trend = %d", last_bar_time, signal)

    current_trend = state.get("current_trend", 0)

    # --- Entry awal ---
    if state["total_qty"] <= 0:
        if current_trend == 1:
            if config["USE_RISK_PERCENT"]:
                account = client.get_account() if not dry_run else None
                usdt_free = get_balance(account, config["QUOTE_ASSET"]) if account else 1000.0
                usdt_amount = usdt_free * config["RISK_PERCENT"] / 100.0
            else:
                usdt_amount = config["INITIAL_ORDER_USDT"]
            usdt_amount = min(usdt_amount, config["MAX_ORDER_USDT"])

            result = place_buy(client, config, filters, usdt_amount, current_price, dry_run)
            if result:
                fill_price, fill_qty = result
                add_layer(state, fill_price, fill_qty)
                state["last_order_time"] = now
        return

    # --- Tambah layer grid (martingale) ---
    if len(state["layers"]) >= config["MAX_GRID_LAYERS"]:
        return
    if config["ONLY_ADD_IF_TREND_VALID"] and current_trend != 1:
        return

    last_layer = state["layers"][-1]
    drop_pct = (last_layer["price"] / current_price - 1.0) * 100.0
    grid_step_pct = strategy.compute_grid_step_pct(klines, config)

    if drop_pct < grid_step_pct:
        return

    current_exposure = sum(l["qty"] * l["price"] for l in state["layers"])
    next_usdt = last_layer["qty"] * last_layer["price"] * config["LOT_MULTIPLIER"]
    next_usdt = min(next_usdt, config["MAX_ORDER_USDT"])
    if current_exposure + next_usdt > config["MAX_TOTAL_EXPOSURE_USDT"]:
        logger.warning(
            "Layer grid berikutnya (%.2f %s) akan melebihi batas total eksposur (%.2f). Dilewati.",
            next_usdt, config["QUOTE_ASSET"], config["MAX_TOTAL_EXPOSURE_USDT"],
        )
        return

    logger.info(
        "Kondisi grid terpenuhi: harga turun %.2f%% dari entry terakhir (butuh >= %.2f%%). "
        "Menambah layer #%d.",
        drop_pct, grid_step_pct, len(state["layers"]) + 1,
    )
    result = place_buy(client, config, filters, next_usdt, current_price, dry_run)
    if result:
        fill_price, fill_qty = result
        add_layer(state, fill_price, fill_qty)
        state["last_order_time"] = now


# -----------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------
def run(config: dict) -> None:
    setup_logging(config)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if not config["DRY_RUN"] and (not config["API_KEY"] or not config["API_SECRET"]):
        logger.error(
            "DRY_RUN=False tapi BINANCE_API_KEY/BINANCE_API_SECRET belum di-set. "
            "Bot dihentikan demi keamanan."
        )
        sys.exit(1)

    logger.info("=" * 70)
    logger.info("Bot mulai berjalan. Symbol=%s | Interval=%s | DRY_RUN=%s",
                config["SYMBOL"], config["INTERVAL"], config["DRY_RUN"])
    if config["DRY_RUN"]:
        logger.warning("MODE DRY_RUN AKTIF: tidak ada order sungguhan yang dikirim.")
    logger.info("=" * 70)

    client = BinanceSpotClient(config["API_KEY"], config["API_SECRET"], config["BASE_URL"])
    client.sync_time()

    exchange_info = client.get_exchange_info(config["SYMBOL"])
    filters = SymbolFilters.from_exchange_info(exchange_info, config["SYMBOL"])
    logger.info(
        "Filter simbol %s -> stepSize=%s minQty=%s minNotional=%s",
        config["SYMBOL"], filters.step_size, filters.min_qty, filters.min_notional,
    )

    state = state_mod.load_state(config["STATE_FILE"])
    if state["total_qty"] > 0:
        logger.info(
            "Melanjutkan basket yang sudah ada: %d layer, total qty=%.8f, avg=%.2f",
            len(state["layers"]), state["total_qty"], state["avg_price"],
        )

    consecutive_errors = 0
    last_time_sync = time.time()
    TIME_SYNC_INTERVAL_SECONDS = 15 * 60  # sinkronisasi ulang tiap 15 menit
    last_heartbeat = 0.0  # 0 = paksa heartbeat pertama langsung tampil

    while not _shutdown_requested:
        loop_start = time.time()
        try:
            if time.time() - last_time_sync > TIME_SYNC_INTERVAL_SECONDS:
                client.sync_time()
                last_time_sync = time.time()

            current_price = client.get_price(config["SYMBOL"])
            book = client.get_book_ticker(config["SYMBOL"])
            bid = float(book["bidPrice"])
            ask = float(book["askPrice"])

            raw_klines = client.get_klines(config["SYMBOL"], config["INTERVAL"], limit=500)
            klines = strategy.parse_klines(raw_klines)

            equity = get_equity(client, config, current_price) if not config["DRY_RUN"] else (
                config["MAX_TOTAL_EXPOSURE_USDT"] * 2
            )
            entries_paused = update_equity_controls(state, equity, config)

            if time.time() - last_heartbeat >= config["HEARTBEAT_INTERVAL_SECONDS"]:
                last_heartbeat = time.time()
                if state["total_qty"] > 0:
                    pnl_pct = (current_price / state["avg_price"] - 1.0) * 100.0
                    posisi_info = (
                        f"posisi TERBUKA ({len(state['layers'])} layer, qty={state['total_qty']:.8f}, "
                        f"avg={state['avg_price']:.2f}, PnL={pnl_pct:+.2f}%)"
                    )
                else:
                    posisi_info = "tidak ada posisi terbuka"
                status_flag = []
                if state.get("dd_stopped"):
                    status_flag.append("DD-STOP")
                if state.get("daily_stopped"):
                    status_flag.append("DAILY-STOP")
                if state_mod.now_ms() < state.get("cooldown_until", 0):
                    status_flag.append("COOLDOWN")
                flag_str = f" | status: {', '.join(status_flag)}" if status_flag else ""
                logger.info(
                    "[HEARTBEAT] Bot masih berjalan | harga=%.2f | trend=%s | equity=%.2f %s | %s%s",
                    current_price, state.get("current_trend", 0), equity, config["QUOTE_ASSET"],
                    posisi_info, flag_str,
                )

            if entries_paused and state.get("dd_stopped") and config["CLOSE_ALL_AT_LIMIT"] and state["total_qty"] > 0:
                close_position(client, config, filters, state, "DD_STOP_FORCE_CLOSE", config["DRY_RUN"])

            manage_exit(client, config, filters, state, current_price, config["DRY_RUN"])
            manage_entry(client, config, filters, state, klines, current_price, bid, ask,
                         entries_paused, config["DRY_RUN"])

            state_mod.save_state(config["STATE_FILE"], state)
            consecutive_errors = 0

        except BinanceAPIError as exc:
            consecutive_errors += 1
            logger.error("BinanceAPIError (%d berturut-turut): %s", consecutive_errors, exc)
        except Exception as exc:  # noqa: BLE001 - loop bot tidak boleh mati karena 1 error
            consecutive_errors += 1
            logger.exception("Error tak terduga di loop utama (%d berturut-turut): %s", consecutive_errors, exc)

        if consecutive_errors >= 10:
            logger.critical(
                "10 error berturut-turut. Bot berhenti total untuk keamanan. "
                "Cek log, perbaiki masalah, lalu jalankan ulang manual."
            )
            break

        elapsed = time.time() - loop_start
        sleep_for = max(1.0, config["LOOP_INTERVAL_SECONDS"] - elapsed)
        time.sleep(sleep_for)

    logger.info("Bot berhenti.")


# -----------------------------------------------------------------------
# Selftest: audit logika murni tanpa koneksi jaringan sama sekali
# -----------------------------------------------------------------------
def selftest() -> None:
    import math
    import random

    print("=== SELFTEST: pembulatan quantity (LOT_SIZE) ===")
    filters = SymbolFilters(
        step_size=Decimal("0.00001"), min_qty=Decimal("0.00001"),
        min_notional=Decimal("5"), tick_size=Decimal("0.01"),
    )
    tests = [(0.123456789, 0.12345), (1.0, 1.0), (0.000001, 0.0)]
    for qty_in, expected in tests:
        got = filters.round_qty(qty_in)
        status = "OK" if abs(got - expected) < 1e-9 else "GAGAL"
        print(f"  round_qty({qty_in}) = {got} (harap {expected}) -> {status}")
        assert abs(got - expected) < 1e-9, "round_qty salah!"

    print("\n=== SELFTEST: signature HMAC (contoh resmi dari dokumentasi Binance) ===")
    from binance_client import _build_query
    import hmac as _hmac
    import hashlib as _hashlib

    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    params = {
        "symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC",
        "quantity": "1", "price": "0.1", "recvWindow": "5000", "timestamp": "1499827319559",
    }
    query = _build_query(params)
    sig = _hmac.new(secret.encode(), query.encode(), _hashlib.sha256).hexdigest()
    expected_sig = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    status = "OK" if sig == expected_sig else "GAGAL"
    print(f"  query    = {query}")
    print(f"  signature= {sig}")
    print(f"  harap    = {expected_sig} -> {status}")
    assert sig == expected_sig, "Signature tidak cocok dengan contoh resmi Binance!"

    print("\n=== SELFTEST: EMA & ATR & SuperTrend dengan data sintetis ===")
    random.seed(42)
    n = 400
    price = 50000.0
    klines = []
    t = 1_700_000_000_000
    for i in range(n):
        change = random.uniform(-0.01, 0.011)  # sedikit bias naik
        o = price
        price = price * (1 + change)
        c = price
        h = max(o, c) * (1 + random.uniform(0, 0.003))
        low = min(o, c) * (1 - random.uniform(0, 0.003))
        klines.append(strategy.Kline(open_time=t, open=o, high=h, low=low, close=c, close_time=t + 299999))
        t += 300000

    closes = [k.close for k in klines]
    ema = strategy.compute_ema(closes, 200)
    assert all(v != v for v in ema[:199]), "EMA sebelum period cukup harus NaN"
    assert ema[199] == ema[199], "EMA setelah period cukup tidak boleh NaN"
    print(f"  EMA200 bar terakhir = {ema[-1]:.2f} -> OK (tidak NaN)")

    highs = [k.high for k in klines]
    lows = [k.low for k in klines]
    atr = strategy.compute_atr_wilder(highs, lows, closes, 10)
    assert atr[-1] > 0, "ATR terakhir harus > 0"
    print(f"  ATR(10) bar terakhir = {atr[-1]:.2f} -> OK (>0)")

    trend = strategy.compute_supertrend_trend(klines, atr, 3.0)
    assert all(t_ in (1, -1) for t_ in trend[10:]), "Trend harus selalu +1 atau -1"
    print(f"  Trend bar terakhir = {trend[-1]} -> OK (+1/-1 valid)")

    sig_val = strategy.get_trend_signal(klines, 10, 3.0, 200, True)
    assert sig_val in (-1, 0, 1)
    print(f"  Sinyal akhir (closed-bar) = {sig_val} -> OK")

    grid_pct = strategy.compute_grid_step_pct(klines, CONFIG)
    assert CONFIG["GRID_MIN_PCT"] <= grid_pct <= CONFIG["GRID_MAX_PCT"]
    print(f"  Grid step = {grid_pct:.3f}% -> OK (dalam batas {CONFIG['GRID_MIN_PCT']}-{CONFIG['GRID_MAX_PCT']}%)")

    print("\n=== SELFTEST: simulasi state basket (recompute avg price) ===")
    st = dict(state_mod.DEFAULT_STATE)
    add_layer(st, 50000.0, 0.001)
    add_layer(st, 49000.0, 0.0013)
    expected_avg = (50000.0 * 0.001 + 49000.0 * 0.0013) / (0.001 + 0.0013)
    assert abs(st["avg_price"] - expected_avg) < 1e-6, "Perhitungan avg_price basket salah"
    print(f"  avg_price = {st['avg_price']:.2f} (harap {expected_avg:.2f}) -> OK")

    print("\nSEMUA SELFTEST LULUS. Logika inti bot terverifikasi secara matematis.")
    print("(Selftest ini TIDAK menghubungi Binance sama sekali -- murni logika lokal.)")


def main():
    parser = argparse.ArgumentParser(description="Bot Grid Martingale Binance Spot")
    parser.add_argument("--selftest", action="store_true",
                         help="Jalankan audit logika murni (tanpa koneksi jaringan) lalu keluar.")
    args = parser.parse_args()

    if args.selftest:
        selftest()
        return

    run(CONFIG)


if __name__ == "__main__":
    main()