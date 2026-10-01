#!/usr/bin/env python3
"""
Bot rotasi Binance Spot. Bot tetap memantau pair QUOTE, mengelola posisi yang
sudah terbuka, dan mempertahankan logika Take Profit, Stop Loss, Breakeven,
Trailing, serta rekonsiliasi. Seluruh entry baru dinonaktifkan.

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

from trading.clients.binance_client import (
    BinanceAPIError, BinanceRateLimitError, SymbolFilters, build_filters_cache,
    build_trading_symbols,
)
from trading.clients.exchange_client import ExchangeClient, create_exchange_client
from config.config import (
    PUMP_CONFIG, CONFIG_LOAD_ERRORS, get_base_url, get_control_file, is_paper,
    require_valid_mode,
)
from market import market_scanner as scanner
from infrastructure.storage import state as state_mod
from strategy import indicators as strategy


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
    "sl_pct": 0.0,
    "tp_pct": 0.0,
    "be_trigger_pct": 0.0,
    "be_lock_pct": 0.0,
    "trail_start_pct": 0.0,
    "trail_step_pct": 0.0,
    "exit_source": "",
    "last_scan_time": 0,
    "day_start_equity": None,
    "day_start_date": None,
    "peak_equity": None,
    "dd_stopped": False,
    "dd_stop_until": 0,
    "daily_stopped": False,
    "daily_stop_source": None,
    "_limit_close_done": False,
    "sell_fail_count": 0,
    "pending_order": None,
    "native_stop": None,
    "native_oco": None,
    "native_protection_retry_at": 0,
    "_native_stop_exit_blocked": False,
    "reconciliation_required": False,
    "reconciliation_assets": [],
}


_LOGGING_MARKER = "_pump_bot_handler"


def setup_logging(config: dict) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    for handler in list(root.handlers):
        if getattr(handler, _LOGGING_MARKER, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except OSError:
                pass

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    setattr(console, _LOGGING_MARKER, True)
    root.addHandler(console)
    file_handler = logging.handlers.RotatingFileHandler(
        config["LOG_FILE"], maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    setattr(file_handler, _LOGGING_MARKER, True)
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
    for b in account.get("balances", []):
        if b.get("asset") == asset:
            return float(b.get("free", 0.0))
    return 0.0


def get_total_balance(account: dict, asset: str) -> float:
    for b in account.get("balances", []):
        if b.get("asset") == asset:
            return float(b.get("free", 0.0)) + float(b.get("locked", 0.0))
    return 0.0


def get_equity(client: ExchangeClient, config: dict, state: dict,
               position_price: float | None = None) -> "float | None":
    account = client.get_account()
    usdt_free = get_balance(account, config["QUOTE_ASSET"])
    if state["current_symbol"] and state["qty"] > 0:
        if position_price is not None and float(position_price) > 0:
            price = float(position_price)
        else:
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


def account_risk_gate(config: dict) -> "tuple[bool, str]":
    if str(config.get("MODE", "")).strip().upper() != "LIVE":
        return True, ""
    if config.get("USE_EQUITY_STOP") or config.get("USE_DAILY_STOP"):
        return True, ""
    override = str(os.environ.get("ALLOW_LIVE_WITHOUT_ACCOUNT_STOP", "")).strip().lower()
    if override in ("1", "true", "yes", "on"):
        logger.critical(
            "ALLOW_LIVE_WITHOUT_ACCOUNT_STOP aktif: bot LIVE dijalankan TANPA rem "
            "drawdown maupun rem kerugian harian atas permintaan eksplisit operator."
        )
        return True, ""
    return False, (
        "MODE=LIVE ditolak: USE_EQUITY_STOP dan USE_DAILY_STOP dua-duanya nonaktif. "
        "Tidak ada rem drawdown maupun rem kerugian harian, dan CLOSE_ALL_AT_LIMIT "
        "tidak akan pernah terpicu. Aktifkan minimal salah satu di Pengaturan, atau "
        "setel environment ALLOW_LIVE_WITHOUT_ACCOUNT_STOP=1 bila risiko ini memang "
        "disengaja."
    )


def describe_exit_mode(config: dict, state: dict) -> str:
    atr_active = str(state.get("exit_source", "")).upper() == "ATR"
    sl_pct = float(config.get("SL_PCT", 0) or 0)
    tp_pct = float(config.get("TP_PCT", 0) or 0)
    if atr_active:
        return (
            f"ATR aktif pada posisi berjalan (jarak SL {float(state.get('sl_pct', 0) or 0):.6f}, "
            f"TP {float(state.get('tp_pct', 0) or 0):.6f} dalam satuan harga)"
        )
    if config.get("USE_ATR_EXIT", False):
        return (
            f"persen tetap (SL {sl_pct:.2f}%, TP {tp_pct:.2f}%). "
            f"USE_ATR_EXIT=True tetapi ATR tidak aktif: posisi tidak membawa "
            f"exit_source=ATR, sehingga ATR_MULT_SL/ATR_MULT_TP tidak dipakai"
        )
    return f"persen tetap (SL {sl_pct:.2f}%, TP {tp_pct:.2f}%)"


def update_equity_controls(state: dict, equity: float, config: dict) -> bool:
    today = state_mod.today_str()
    if state.get("day_start_date") != today:
        state["day_start_date"] = today
        state["day_start_equity"] = equity
        state["daily_stopped"] = False
        state["daily_stop_source"] = None
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

    if state.get("dd_stopped"):
        deadline = state.get("dd_stop_until") or 0
        if not config.get("USE_EQUITY_STOP"):
            state["dd_stopped"] = False
            state["dd_stop_until"] = 0
            state["peak_equity"] = equity
            logger.info(
                "USE_EQUITY_STOP nonaktif: status stop drawdown yang tersisa dilepas."
            )
        elif not deadline:
            state["dd_stopped"] = False
            state["dd_stop_until"] = 0
            state["peak_equity"] = equity
            logger.warning(
                "Status stop drawdown tidak punya batas waktu (state lama atau rusak). "
                "Status dilepas dan puncak equity dihitung ulang dari nilai sekarang."
            )
        elif state_mod.now_ms() >= deadline:
            state["dd_stopped"] = False
            state["dd_stop_until"] = 0
            state["peak_equity"] = equity
            logger.info("Cooldown drawdown selesai. Entry baru diaktifkan lagi.")

    if config.get("USE_DAILY_STOP", True) and not state.get("daily_stopped") and state.get("day_start_equity"):
        change_pct = (equity - state["day_start_equity"]) / state["day_start_equity"] * 100.0
        if change_pct <= -config["MAX_DAILY_LOSS_PERCENT"]:
            state["daily_stopped"] = True
            state["daily_stop_source"] = "LOSS"
            logger.warning("STOP HARIAN: rugi harian %.2f%%. Tidak ada entry baru sampai hari berikutnya (UTC).", change_pct)
        elif change_pct >= config["DAILY_PROFIT_TARGET_PERCENT"]:
            state["daily_stopped"] = True
            state["daily_stop_source"] = "PROFIT"
            logger.info("TARGET HARIAN TERCAPAI: profit harian %.2f%%. Tidak ada entry baru sampai hari berikutnya (UTC).", change_pct)

    return bool(state.get("dd_stopped") or state.get("daily_stopped"))


def maybe_force_close_at_risk_limit(client: ExchangeClient, config: dict,
                                     filters_cache: dict, state: dict,
                                     entries_paused: bool, current_price) -> None:
    profit_stop_only = (
        bool(state.get("daily_stopped"))
        and not state.get("dd_stopped")
        and str(state.get("daily_stop_source") or "LOSS").upper() == "PROFIT"
    )
    limit_now = bool(state.get("dd_stopped") or state.get("daily_stopped")) and not profit_stop_only
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
        close_position(client, config, filters_cache, state, "RISK_LIMIT_TRIGGERED",
                       reference_price=current_price)
        state["_limit_close_done"] = True

    if not entries_paused and state.get("_limit_close_done"):
        state["_limit_close_done"] = False


def reconcile_state_with_exchange(client: ExchangeClient, config: dict, state: dict,
                                  filters_cache: dict | None = None) -> None:
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
            executed_qty = float(order.get("executedQty", 0.0) or 0.0)
            if side == "BUY" and executed_qty > 0 and (
                _order_status_is_terminal(status) or not status
            ):
                if _restore_pending_buy(config, state, pending, order, account):
                    logger.critical("BUY %s dipulihkan dari intent/order setelah respons hilang.", symbol)
                    state["reconciliation_required"] = False
                    state["reconciliation_assets"] = []
                else:
                    state["reconciliation_required"] = True
                    state["reconciliation_assets"] = [symbol] if symbol else []
                state["pending_order"] = None
                changed = True
            elif _order_status_is_terminal(status):
                state["pending_order"] = None
                changed = True
            else:
                pending["last_status"] = status or "UNKNOWN"
                pending["executed_qty"] = executed_qty
                state["reconciliation_required"] = True
                state["reconciliation_assets"] = [symbol] if symbol else []
                changed = True

    symbol = state.get("current_symbol")
    qty_state = float(state.get("qty") or 0.0)
    pending_unresolved = isinstance(state.get("pending_order"), dict)
    if symbol and qty_state > 0 and symbol.endswith(quote):
        base_asset = symbol[: -len(quote)]
        total_base = get_total_balance(account, base_asset)
        free_base = get_balance(account, base_asset)
        if total_base <= 0 and not pending_unresolved:
            logger.warning(
                "REKONSILIASI: state bilang pegang %s qty=%.8f, tapi saldo total %s = 0. "
                "Posisi hantu direset.", symbol, qty_state, base_asset,
            )
            reset_position(state)
            changed = True
        elif total_base <= 0 and pending_unresolved:
            state["reconciliation_required"] = True
            state["reconciliation_assets"] = [base_asset]
            changed = True
            logger.critical(
                "Intent %s untuk %s masih unresolved dan saldo total sementara nol; "
                "posisi dipertahankan, bukan direset.",
                state["pending_order"].get("side"), symbol,
            )
        elif total_base < qty_state:
            logger.warning(
                "REKONSILIASI: qty state %s (%.8f) lebih besar dari saldo total nyata %.8f. "
                "Qty disesuaikan.", symbol, qty_state, total_base,
            )
            state["qty"] = total_base
            changed = True
        if free_base <= 0 and total_base > 0:
            state["reconciliation_required"] = True
            state["reconciliation_assets"] = [base_asset]
            changed = True
            logger.critical("%s seluruhnya locked. Entry baru diblokir sampai order manual direkonsiliasi.",
                            base_asset)
    elif not state.get("current_symbol") and not state.get("pending_order"):
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

    if (
        _native_protection_enabled(config)
        and state.get("current_symbol")
        and float(state.get("qty") or 0.0) > 0
        and not state.get("pending_order")
        and filters_cache is not None
    ):
        filters = filters_cache.get(state["current_symbol"])
        _ensure_native_protection(client, config, filters, state)

    if changed:
        state_mod.save_state(config["STATE_FILE"], state)


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
    state["pending_order"] = None
    state["native_stop"] = None
    state["native_oco"] = None
    state["native_protection_retry_at"] = 0
    state["_native_stop_exit_blocked"] = False


def try_dust_sweep(client: ExchangeClient, config: dict, symbol: "str | None") -> None:
    if not config.get("USE_DUST_SWEEP", True):
        return
    if not symbol:
        return
    quote_asset = config["QUOTE_ASSET"]
    if not symbol.endswith(quote_asset):
        return
    base_asset = symbol[: -len(quote_asset)]
    if not base_asset or base_asset in (quote_asset, "BNB"):
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
        logger.info("Dust sweep: %s tidak (lagi) terdaftar sebagai dust convertible saat ini, dilewati.",
                     base_asset)
        return

    try:
        result = client.convert_dust([base_asset])
    except BinanceAPIError as exc:
        logger.warning(
            "Dust sweep %s -> BNB gagal (%s). Sisa saldo dibiarkan, dicoba lagi di kesempatan berikutnya.",
            base_asset, exc,
        )
        return

    transferred = result.get("totalTransfered", "0") if isinstance(result, dict) else "0"
    logger.info("DUST SWEEP OK: sisa %s dikonversi ke %s BNB (sudah dikurangi biaya layanan Binance).",
                base_asset, transferred)


def _new_client_order_id(prefix: str) -> str:
    return f"pump-{prefix}-{uuid.uuid4().hex[:24]}"


def _submit_market_order(client: ExchangeClient, symbol: str, side: str,
                         quantity: float | None, client_order_id: str,
                         quote_order_qty: float | None = None) -> dict:
    return client.new_market_order(
        symbol, side, quantity=quantity, quote_order_qty=quote_order_qty,
        new_client_order_id=client_order_id,
    )


_TERMINAL_ORDER_STATUSES = frozenset({
    "FILLED", "EXPIRED", "CANCELED", "REJECTED",
})
_NONTERMINAL_ORDER_STATUSES = frozenset({
    "NEW", "PARTIALLY_FILLED",
})


def _order_status_is_terminal(status: str) -> bool:
    return str(status or "").upper() in _TERMINAL_ORDER_STATUSES


def _native_oco_enabled(config: dict) -> bool:
    return (
        str(config.get("MODE", "PAPER")).upper() == "LIVE"
        and bool(config.get("USE_NATIVE_OCO", False))
        and bool(config.get("USE_STOP_LOSS", False))
        and bool(config.get("USE_TP", False))
    )


def _native_stop_enabled(config: dict) -> bool:
    return (
        str(config.get("MODE", "PAPER")).upper() == "LIVE"
        and bool(config.get("USE_NATIVE_STOP_LOSS", False))
        and bool(config.get("USE_STOP_LOSS", False))
    )


def _native_protection_enabled(config: dict) -> bool:
    return _native_oco_enabled(config) or _native_stop_enabled(config)


def _exit_distance(state: dict, config: dict, state_key: str, cfg_key: str,
                   atr_mode: bool, entry: float) -> float:
    locked = abs(float(state.get(state_key) or 0.0))
    if locked > 0:
        return locked
    pct = abs(float(config.get(cfg_key, 0.0) or 0.0))
    return entry * pct / 100.0 if atr_mode else pct


def _native_oco_levels(state: dict, filters: SymbolFilters | None,
                       config: dict) -> dict:
    entry = float(state.get("entry_price") or 0.0)
    atr_mode = str(state.get("exit_source", "")).upper() == "ATR"
    sl = _exit_distance(state, config, "sl_pct", "SL_PCT", atr_mode, entry)
    tp = _exit_distance(state, config, "tp_pct", "TP_PCT", atr_mode, entry)
    raw_sl = entry - sl if atr_mode else entry * (1.0 - sl / 100.0)
    raw_tp = entry + tp if atr_mode else entry * (1.0 + tp / 100.0)
    buffer_pct = max(0.01, float(config.get("NATIVE_OCO_LIMIT_BUFFER_PCT", 0.10) or 0.10))
    raw_sl_limit = raw_sl * (1.0 - buffer_pct / 100.0)
    raw_tp_limit = raw_tp * (1.0 - buffer_pct / 100.0)
    if filters is not None:
        return {
            "above_stop_price": filters.round_price(raw_tp),
            "above_price": filters.round_price(raw_tp_limit),
            "below_stop_price": filters.round_price(raw_sl),
            "below_price": filters.round_price(raw_sl_limit),
        }
    return {
        "above_stop_price": raw_tp,
        "above_price": raw_tp_limit,
        "below_stop_price": raw_sl,
        "below_price": raw_sl_limit,
    }


def _native_stop_price(state: dict, filters: SymbolFilters | None,
                       config: dict | None = None) -> float:
    entry = float(state.get("entry_price") or 0.0)
    atr_mode = str(state.get("exit_source", "")).upper() == "ATR"
    sl = _exit_distance(state, config or {}, "sl_pct", "SL_PCT", atr_mode, entry)
    if atr_mode:
        raw = entry - sl
    else:
        raw = entry * (1.0 - sl / 100.0)
    if filters is not None:
        raw = filters.round_price(raw)
    return raw


_DEFINITIVE_REJECT_CODES = {
    -1013,
    -1021,
    -1100, -1101, -1102, -1103, -1104, -1106,
    -1111,
    -1121,
    -2010,
    -1116,
    -1020,
}
_NOT_FOUND_CODES = {-2013}
_NOT_FOUND_MIN_INTENT_AGE_MS = 10_000


def _is_definitive_reject(exc: BinanceAPIError) -> bool:
    if isinstance(exc, BinanceRateLimitError):
        return False
    return getattr(exc, "code", None) in _DEFINITIVE_REJECT_CODES


def _is_order_not_found(exc: BinanceAPIError) -> bool:
    if getattr(exc, "code", None) in _NOT_FOUND_CODES:
        return True
    return "does not exist" in str(getattr(exc, "msg", "") or "").lower()


def _arm_native_oco(client: ExchangeClient, config: dict,
                    filters: SymbolFilters | None, state: dict) -> bool:
    if not _native_oco_enabled(config):
        return False
    symbol = state.get("current_symbol")
    qty = float(state.get("qty") or 0.0)
    entry = float(state.get("entry_price") or 0.0)
    if not symbol or qty <= 0 or entry <= 0:
        return False
    now = state_mod.now_ms()
    if now < int(state.get("native_protection_retry_at", 0) or 0):
        return False
    levels = _native_oco_levels(state, filters, config)
    bid = _executable_bid(client, symbol)
    above_price = float(levels["above_price"])
    above_stop = float(levels["above_stop_price"])
    below_price = float(levels["below_price"])
    below_stop = float(levels["below_stop_price"])
    valid = (
        below_price > 0
        and below_price < below_stop < entry
        and above_price > entry
        and above_stop >= above_price > entry
        and (bid is None or below_price < below_stop < bid < above_price <= above_stop)
    )
    if not valid:
        state["native_protection_retry_at"] = now + 60_000
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        logger.critical(
            "OCO native %s tidak dipasang: relasi harga invalid "
            "belowLimit=%.12g belowStop=%.12g bid=%.12g aboveLimit=%.12g aboveStop=%.12g.",
            symbol, below_price, below_stop, bid or 0.0, above_price, above_stop,
        )
        state_mod.save_state(config["STATE_FILE"], state)
        return False

    list_client_order_id = _new_client_order_id("oc")
    above_client_order_id = _new_client_order_id("oa")
    below_client_order_id = _new_client_order_id("ob")
    state["native_oco"] = {
        "symbol": symbol,
        "side": "SELL",
        "quantity": qty,
        "list_client_order_id": list_client_order_id,
        "order_list_id": None,
        "above": {
            "type": "TAKE_PROFIT_LIMIT",
            "client_order_id": above_client_order_id,
            "order_id": None,
            "price": above_price,
            "stop_price": above_stop,
        },
        "below": {
            "type": "STOP_LOSS_LIMIT",
            "client_order_id": below_client_order_id,
            "order_id": None,
            "price": below_price,
            "stop_price": below_stop,
        },
        "status": "PENDING",
        "created_at": now,
    }
    state_mod.save_state(config["STATE_FILE"], state)
    try:
        response = client.place_native_oco(
            symbol, quantity=qty,
            above_price=above_price, above_stop_price=above_stop,
            below_price=below_price, below_stop_price=below_stop,
            list_client_order_id=list_client_order_id,
            above_client_order_id=above_client_order_id,
            below_client_order_id=below_client_order_id,
        )
    except NotImplementedError as exc:
        intent = state.get("native_oco")
        if isinstance(intent, dict):
            intent["status"] = "FAILED"
            intent["last_error"] = str(exc)
        state["native_protection_retry_at"] = now + 60_000
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical("OCO native %s tidak didukung client: %s. Fallback stop akan dicoba.", symbol, exc)
        return False
    except BinanceAPIError as exc:
        intent = state.get("native_oco")
        if _is_definitive_reject(exc):
            if isinstance(intent, dict):
                intent["status"] = "FAILED"
                intent["last_error"] = str(exc)
            state["_native_stop_exit_blocked"] = False
            state["native_protection_retry_at"] = now + 60_000
            state["reconciliation_required"] = True
            state["reconciliation_assets"] = [symbol]
            state_mod.save_state(config["STATE_FILE"], state)
            logger.critical(
                "OCO native %s DITOLAK deterministik (code=%s): %s. "
                "Order dipastikan tidak tercipta; local SL/TP tetap aktif "
                "dan fallback stop akan dicoba.",
                symbol, getattr(exc, "code", None), exc,
            )
            return False
        if isinstance(intent, dict):
            intent["status"] = "UNKNOWN"
            intent["last_error"] = str(exc)
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "OCO native %s gagal/status tidak pasti: %s. Tidak memasang proteksi kedua secara buta.",
            symbol, exc,
        )
        return False

    intent = state.get("native_oco")
    if not isinstance(intent, dict):
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    intent["order_list_id"] = response.get("orderListId")
    intent["list_status_type"] = str(response.get("listStatusType") or "EXEC_STARTED").upper()
    intent["status"] = str(response.get("listOrderStatus") or "EXECUTING").upper()
    for leg_name, _client_key in (("above", "aboveClientOrderId"), ("below", "belowClientOrderId")):
        leg = intent.get(leg_name)
        if not isinstance(leg, dict):
            continue
        report = next((x for x in response.get("orders", [])
                       if x.get("clientOrderId") == leg.get("client_order_id")), None)
        if report is None:
            report = next((x for x in response.get("orderReports", [])
                           if x.get("clientOrderId") == leg.get("client_order_id")), None)
        if isinstance(report, dict):
            leg["order_id"] = report.get("orderId")
    state["_native_stop_exit_blocked"] = False
    state["native_protection_retry_at"] = 0
    if intent["status"] in ("ALL_DONE", "REJECT"):
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
    else:
        state["reconciliation_required"] = False
        state["reconciliation_assets"] = []
    state_mod.save_state(config["STATE_FILE"], state)
    logger.info(
        "OCO native aktif %s: TP_LIMIT %.12g/trigger %.12g, SL_LIMIT %.12g/trigger %.12g, list=%s",
        symbol, above_price, above_stop, below_price, below_stop, list_client_order_id,
    )
    return intent["status"] not in ("ALL_DONE", "REJECT")


def _arm_native_stop(client: ExchangeClient, config: dict,
                     filters: SymbolFilters | None, state: dict) -> bool:
    if not _native_stop_enabled(config):
        return True
    state["_native_stop_exit_blocked"] = False
    symbol = state.get("current_symbol")
    qty = float(state.get("qty") or 0.0)
    entry = float(state.get("entry_price") or 0.0)
    if not symbol or qty <= 0 or entry <= 0:
        return False

    stop_price = _native_stop_price(state, filters, config)
    bid = _executable_bid(client, symbol)
    if stop_price <= 0 or stop_price >= entry or (bid is not None and stop_price >= bid):
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        logger.critical(
            "Proteksi native %s tidak dipasang: stopPrice %.12g tidak valid "
            "terhadap entry %.12g/bid %.12g. Local stop tetap aktif; entry baru diblokir.",
            symbol, stop_price, entry, bid or 0.0,
        )
        return False

    client_order_id = _new_client_order_id("sl")
    state["native_stop"] = {
        "symbol": symbol,
        "side": "SELL",
        "type": "STOP_LOSS",
        "quantity": qty,
        "stop_price": stop_price,
        "client_order_id": client_order_id,
        "order_id": None,
        "status": "PENDING",
        "created_at": state_mod.now_ms(),
    }
    state_mod.save_state(config["STATE_FILE"], state)
    try:
        response = client.place_native_stop_loss(
            symbol, quantity=qty, stop_price=stop_price,
            new_client_order_id=client_order_id,
        )
    except (BinanceAPIError, NotImplementedError) as exc:
        intent = state.get("native_stop")
        definitive = (isinstance(exc, NotImplementedError)
                      or (isinstance(exc, BinanceAPIError) and _is_definitive_reject(exc)))
        if isinstance(intent, dict):
            intent["status"] = "FAILED" if definitive else "UNKNOWN"
            intent["last_error"] = str(exc)
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "Proteksi native %s gagal/status tidak pasti: %s. "
            "Local stop tetap aktif, entry baru diblokir, dan ID %s harus direkonsiliasi.",
            symbol, exc, client_order_id,
        )
        return False

    intent = state.get("native_stop")
    if not isinstance(intent, dict):
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    intent["order_id"] = response.get("orderId")
    intent["status"] = str(response.get("status") or "NEW").upper()
    intent["last_response"] = {
        key: response.get(key) for key in ("orderId", "clientOrderId", "status")
        if key in response
    }
    if intent["status"] in ("FILLED", "CANCELED", "EXPIRED", "REJECTED"):
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        logger.critical("Proteksi native %s langsung berstatus %s; rekonsiliasi diwajibkan.",
                        symbol, intent["status"])
    else:
        state["reconciliation_required"] = False
        state["reconciliation_assets"] = []
    state_mod.save_state(config["STATE_FILE"], state)
    logger.info("Proteksi native aktif %s: STOP_LOSS stopPrice=%.12g qty=%.12g id=%s",
                symbol, stop_price, qty, client_order_id)
    return intent["status"] not in ("FILLED", "CANCELED", "EXPIRED", "REJECTED")


def _ensure_native_protection(client: ExchangeClient, config: dict,
                              filters: SymbolFilters | None, state: dict) -> bool:
    if not _native_protection_enabled(config):
        return True
    if isinstance(state.get("native_oco"), dict) or isinstance(state.get("native_stop"), dict):
        return True
    if _native_oco_enabled(config):
        if state_mod.now_ms() < int(state.get("native_protection_retry_at", 0) or 0):
            return False
        if _arm_native_oco(client, config, filters, state):
            return True
        oco_status = (state.get("native_oco") or {}).get("status")
        if oco_status in (None, "FAILED") and _native_stop_enabled(config):
            state["native_oco"] = None
            return _arm_native_stop(client, config, filters, state)
        return False
    return _arm_native_stop(client, config, filters, state)


def _oco_response_has_fill(response: dict) -> bool:
    reports = response.get("orderReports", []) if isinstance(response, dict) else []
    return any(
        str(report.get("status") or "").upper() == "FILLED"
        or float(report.get("executedQty", 0.0) or 0.0) > 0
        for report in reports if isinstance(report, dict)
    )


def _settle_native_protective_fill(client: ExchangeClient, config: dict,
                                   state: dict, symbol: str, reason: str) -> None:
    entry_price = float(state.get("entry_price") or 0.0)
    base = symbol[: -len(config["QUOTE_ASSET"])]
    state["reconciliation_required"] = False
    state["reconciliation_assets"] = []
    reconcile_state_with_exchange(client, config, state)
    if state.get("current_symbol") or state.get("pending_order"):
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [base]
        state_mod.save_state(config["STATE_FILE"], state)
        return
    state["sell_fail_count"] = 0
    state["cooldown_until"] = state_mod.now_ms() + config["COOLDOWN_MINUTES_AFTER_CLOSE"] * 60 * 1000
    state["last_trade_time"] = state_mod.now_ms()
    state_mod.save_state(config["STATE_FILE"], state)
    logger.info(
        "EXIT NATIVE %s (%s): posisi ditutup oleh order proteksi exchange-side "
        "(entry=%.6f). Rekonsiliasi bersih; bot lanjut scan setelah cooldown.",
        symbol, reason, entry_price,
    )
    try_dust_sweep(client, config, symbol)


def _reconcile_native_oco(client: ExchangeClient, config: dict,
                          state: dict) -> bool:
    intent = state.get("native_oco")
    symbol = state.get("current_symbol")
    if not isinstance(intent, dict) or not symbol:
        return True
    status = str(intent.get("status") or "UNKNOWN").upper()
    if status == "FAILED":
        state["native_oco"] = None
        state["_native_stop_exit_blocked"] = False
        state["native_protection_retry_at"] = state_mod.now_ms() + 60_000
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    try:
        response = client.get_order_list(
            order_list_id=(int(intent["order_list_id"])
                           if intent.get("order_list_id") is not None else None),
            list_client_order_id=(
                None if intent.get("order_list_id") is not None
                else intent.get("list_client_order_id")
            ),
        )
    except BinanceAPIError as exc:
        never_confirmed = intent.get("order_list_id") is None
        age_ms = state_mod.now_ms() - int(intent.get("created_at") or 0)
        if (_is_order_not_found(exc) and never_confirmed
                and age_ms >= _NOT_FOUND_MIN_INTENT_AGE_MS):
            state["native_oco"] = None
            state["_native_stop_exit_blocked"] = False
            state["native_protection_retry_at"] = state_mod.now_ms() + 60_000
            state["reconciliation_required"] = True
            state["reconciliation_assets"] = [symbol]
            state_mod.save_state(config["STATE_FILE"], state)
            logger.critical(
                "OCO %s dipastikan TIDAK PERNAH tercipta di bursa (%s). "
                "Exit lokal diaktifkan kembali dan proteksi akan dipasang ulang.",
                symbol, exc,
            )
            return False
        intent["status"] = "UNKNOWN"
        intent["last_error"] = str(exc)
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical("Status OCO %s tidak dapat diverifikasi: %s. Exit market ditahan.", symbol, exc)
        return False

    intent["order_list_id"] = response.get("orderListId", intent.get("order_list_id"))
    intent["list_status_type"] = str(response.get("listStatusType") or "UNKNOWN").upper()
    status = str(response.get("listOrderStatus") or "UNKNOWN").upper()
    intent["status"] = status
    for leg_name in ("above", "below"):
        leg = intent.get(leg_name)
        if not isinstance(leg, dict):
            continue
        report = next((x for x in response.get("orders", [])
                       if x.get("clientOrderId") == leg.get("client_order_id")), None)
        if report is None:
            report = next((x for x in response.get("orderReports", [])
                           if x.get("clientOrderId") == leg.get("client_order_id")), None)
        if isinstance(report, dict):
            leg["order_id"] = report.get("orderId", leg.get("order_id"))
            leg["status"] = str(report.get("status") or "UNKNOWN").upper()
            leg["executed_qty"] = float(report.get("executedQty", 0.0) or 0.0)

    filled = _oco_response_has_fill(response)
    if not filled and status == "ALL_DONE":
        for leg_name in ("above", "below"):
            leg = intent.get(leg_name)
            if not isinstance(leg, dict) or leg.get("order_id") is None:
                state["_native_stop_exit_blocked"] = True
                state["reconciliation_required"] = True
                state["reconciliation_assets"] = [symbol]
                state_mod.save_state(config["STATE_FILE"], state)
                logger.critical("OCO %s ALL_DONE tanpa detail leg yang dapat diverifikasi.", symbol)
                return False
            try:
                child = client.get_order(symbol, order_id=int(leg["order_id"]))
            except BinanceAPIError as exc:
                state["_native_stop_exit_blocked"] = True
                state["reconciliation_required"] = True
                state["reconciliation_assets"] = [symbol]
                state_mod.save_state(config["STATE_FILE"], state)
                logger.critical("Leg OCO %s tidak dapat diverifikasi: %s", symbol, exc)
                return False
            leg["status"] = str(child.get("status") or "UNKNOWN").upper()
            leg["executed_qty"] = float(child.get("executedQty", 0.0) or 0.0)
            filled = filled or leg["status"] == "FILLED" or leg["executed_qty"] > 0

    if filled:
        base = symbol[: -len(config["QUOTE_ASSET"])]
        state["native_oco"] = None
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [base]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical("OCO %s salah satu leg FILLED; tidak mengirim SELL kedua. Rekonsiliasi saldo dijalankan.", symbol)
        _settle_native_protective_fill(client, config, state, symbol, "NATIVE_OCO_FILLED")
        return False
    if status in ("ALL_DONE", "REJECT"):
        state["native_oco"] = None
        state["_native_stop_exit_blocked"] = False
        state["native_protection_retry_at"] = state_mod.now_ms() + 60_000
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    if status != "EXECUTING":
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    state_mod.save_state(config["STATE_FILE"], state)
    return True


def _reconcile_native_stop(client: ExchangeClient, config: dict,
                           state: dict) -> bool:
    intent = state.get("native_stop")
    symbol = state.get("current_symbol")
    if not isinstance(intent, dict) or not symbol:
        return True
    status = str(intent.get("status") or "UNKNOWN").upper()
    if status == "FAILED":
        state["native_stop"] = None
        state["_native_stop_exit_blocked"] = False
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    if status in ("CANCELED", "EXPIRED", "REJECTED"):
        state["native_stop"] = None
        state["_native_stop_exit_blocked"] = False
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    try:
        order = client.get_order(
            symbol,
            order_id=int(intent["order_id"]) if intent.get("order_id") is not None else None,
            orig_client_order_id=(
                None if intent.get("order_id") is not None
                else intent.get("client_order_id")
            ),
        )
    except BinanceAPIError as exc:
        never_confirmed = intent.get("order_id") is None
        age_ms = state_mod.now_ms() - int(intent.get("created_at") or 0)
        if (_is_order_not_found(exc) and never_confirmed
                and age_ms >= _NOT_FOUND_MIN_INTENT_AGE_MS):
            state["native_stop"] = None
            state["_native_stop_exit_blocked"] = False
            state["native_protection_retry_at"] = state_mod.now_ms() + 60_000
            state["reconciliation_required"] = True
            state["reconciliation_assets"] = [symbol]
            state_mod.save_state(config["STATE_FILE"], state)
            logger.critical(
                "Proteksi native %s dipastikan TIDAK PERNAH tercipta di bursa (%s). "
                "Exit lokal diaktifkan kembali dan proteksi akan dipasang ulang.",
                symbol, exc,
            )
            return False
        intent["status"] = "UNKNOWN"
        intent["last_error"] = str(exc)
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical("Status proteksi native %s tidak dapat diverifikasi: %s. Exit market ditahan.",
                        symbol, exc)
        return False
    status = str(order.get("status") or "UNKNOWN").upper()
    intent["status"] = status
    intent["order_id"] = order.get("orderId", intent.get("order_id"))
    if status == "FILLED":
        base = symbol[: -len(config["QUOTE_ASSET"])]
        state["native_stop"] = None
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [base]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical("Proteksi native %s FILLED; tidak mengirim SELL kedua. Rekonsiliasi saldo dijalankan.", symbol)
        _settle_native_protective_fill(client, config, state, symbol, "NATIVE_STOP_FILLED")
        return False
    if status in ("CANCELED", "EXPIRED", "REJECTED"):
        state["native_stop"] = None
        state["_native_stop_exit_blocked"] = False
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    if status not in ("NEW", "PARTIALLY_FILLED"):
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    state_mod.save_state(config["STATE_FILE"], state)
    return True


def _cancel_native_oco_before_exit(client: ExchangeClient, config: dict,
                                   state: dict) -> bool:
    intent = state.get("native_oco")
    symbol = state.get("current_symbol")
    if not isinstance(intent, dict) or not symbol:
        return True
    if not _reconcile_native_oco(client, config, state):
        if not state.get("native_oco") and not state.get("_native_stop_exit_blocked"):
            state["reconciliation_required"] = False
            state["reconciliation_assets"] = []
            state_mod.save_state(config["STATE_FILE"], state)
            return True
        return False
    intent = state.get("native_oco")
    if not isinstance(intent, dict):
        return not state.get("_native_stop_exit_blocked", False)
    try:
        response = client.cancel_order_list(
            symbol,
            order_list_id=(int(intent["order_list_id"])
                           if intent.get("order_list_id") is not None else None),
            list_client_order_id=(
                None if intent.get("order_list_id") is not None
                else intent.get("list_client_order_id")
            ),
        )
    except BinanceAPIError as exc:
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical("OCO %s gagal dibatalkan: %s. SELL manual ditahan.", symbol, exc)
        return False
    if _oco_response_has_fill(response):
        state["native_oco"] = None
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical("OCO %s terisi saat cancel. SELL manual ditahan untuk rekonsiliasi.", symbol)
        _settle_native_protective_fill(client, config, state, symbol, "NATIVE_OCO_FILLED_ON_CANCEL")
        return False
    final_status = str(response.get("listOrderStatus") or "").upper()
    if final_status != "ALL_DONE":
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical("Cancel OCO %s belum terverifikasi: %s. SELL manual ditahan.",
                        symbol, final_status or "UNKNOWN")
        return False
    state["native_oco"] = None
    state["native_protection_retry_at"] = 0
    state["_native_stop_exit_blocked"] = False
    state["reconciliation_required"] = False
    state["reconciliation_assets"] = []
    state_mod.save_state(config["STATE_FILE"], state)
    return True


def _cancel_native_stop_before_exit(client: ExchangeClient, config: dict,
                                    state: dict) -> bool:
    if isinstance(state.get("native_oco"), dict):
        if not _cancel_native_oco_before_exit(client, config, state):
            return False
    intent = state.get("native_stop")
    symbol = state.get("current_symbol")
    if not isinstance(intent, dict) or not symbol:
        return True
    if not _reconcile_native_stop(client, config, state):
        if not state.get("native_stop") and not state.get("_native_stop_exit_blocked"):
            state["reconciliation_required"] = False
            state["reconciliation_assets"] = []
            state_mod.save_state(config["STATE_FILE"], state)
            return True
        return False
    intent = state.get("native_stop")
    if not isinstance(intent, dict):
        return not state.get("_native_stop_exit_blocked", False)
    try:
        response = client.cancel_order(
            symbol,
            order_id=int(intent["order_id"]) if intent.get("order_id") is not None else None,
            orig_client_order_id=(
                None if intent.get("order_id") is not None
                else intent.get("client_order_id")
            ),
        )
    except BinanceAPIError as exc:
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical("Proteksi native %s gagal dibatalkan: %s. SELL manual ditahan.", symbol, exc)
        return False
    final_status = str(response.get("status") or "").upper()
    if final_status not in ("CANCELED", "EXPIRED", "REJECTED"):
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical("Cancel proteksi native %s belum terverifikasi: %s. SELL manual ditahan.",
                        symbol, final_status or "UNKNOWN")
        return False
    state["native_stop"] = None
    state["reconciliation_required"] = False
    state["reconciliation_assets"] = []
    state_mod.save_state(config["STATE_FILE"], state)
    return True


def _executable_bid(client: ExchangeClient, symbol: str,
                    reference_price: float | None = None) -> float | None:
    if reference_price is not None:
        try:
            bid = float(reference_price)
            if bid > 0:
                return bid
        except (TypeError, ValueError):
            pass
    getter = getattr(client, "get_book_ticker", None)
    if getter is None:
        return None
    try:
        try:
            book = getter(symbol, max_retries=1)
        except TypeError:
            book = getter(symbol)
        bid = float(book.get("bidPrice", 0.0))
        return bid if bid > 0 else None
    except (BinanceAPIError, TypeError, ValueError, AttributeError) as exc:
        logger.warning("Bid executable %s tidak dapat diambil: %s", symbol, exc)
        return None


_listing_age_cache: dict = {}


def listing_age_days(client: ExchangeClient, symbol: str, now_ms: int) -> float:
    if symbol in _listing_age_cache:
        return _listing_age_cache[symbol]
    raw = client.get_klines(symbol, "1d", limit=1, start_time_ms=0)
    age = 0.0 if not raw else max(0.0, (now_ms - int(raw[0][0])) / 86_400_000.0)
    _listing_age_cache[symbol] = age
    return age

def _restore_pending_buy(config: dict, state: dict, pending: dict, order: dict,
                         account: dict) -> bool:
    executed = float(order.get("executedQty", 0.0) or 0.0)
    quoted = float(order.get("cummulativeQuoteQty", 0.0) or 0.0)
    symbol = str(pending.get("symbol") or order.get("symbol") or "")
    if not symbol or executed <= 0 or quoted <= 0:
        return False
    quote = config["QUOTE_ASSET"]
    if not symbol.endswith(quote):
        return False
    base = symbol[: -len(quote)]
    qty = min(executed, get_balance(account, base))
    if qty <= 0:
        return False
    levels = pending.get("levels") if isinstance(pending.get("levels"), dict) else {}
    state["current_symbol"] = symbol
    state["entry_price"] = quoted / executed
    state["qty"] = qty
    state["entry_time"] = int(order.get("transactTime") or state_mod.now_ms())
    state["be_active"] = False
    state["be_stop_price"] = 0.0
    state["trailing_active"] = False
    state["trailing_stop_price"] = 0.0
    for key in ("sl_pct", "tp_pct", "be_trigger_pct", "be_lock_pct",
                "trail_start_pct", "trail_step_pct", "exit_source"):
        if key in levels:
            state[key] = levels[key]
    state["last_trade_time"] = state_mod.now_ms()
    state["sell_fail_count"] = 0
    return True

def open_position(client: ExchangeClient, config: dict, filters_cache: dict,
                   state: dict, candidate: "scanner.Candidate",
                   reference_price: "float | None" = None) -> None:
    filters = filters_cache.get(candidate.symbol)
    if filters is None:
        logger.warning("Tidak ada data filter untuk %s, entry dilewati.", candidate.symbol)
        return

    price_ref = reference_price if (reference_price and reference_price > 0) else candidate.last_price

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

    if price_ref <= 0:
        logger.warning("Entry %s dilewati: harga ASK/acuan tidak valid %.8f.",
                       candidate.symbol, price_ref)
        return

    order_quote_qty = usdt_amount
    if filters.max_notional > 0:
        order_quote_qty = min(order_quote_qty, float(filters.max_notional))
    qty = filters.round_qty(order_quote_qty / price_ref)
    if filters.max_qty > 0 and qty > float(filters.max_qty):
        qty = filters.round_qty(float(filters.max_qty))
        order_quote_qty = min(order_quote_qty, qty * price_ref)
    notional = qty * price_ref
    if (
        qty < float(filters.min_qty)
        or notional < float(filters.min_notional)
        or order_quote_qty < float(filters.min_notional)
    ):
        logger.warning(
            "Entry %s dilewati: qty/notional di bawah atau melampaui batas bursa "
            "(qty=%.8f, notional=%.2f, minQty=%.8f, maxQty=%.8f, "
            "minNotional=%.2f, maxNotional=%.2f). Nominal order %.4f %s.",
            candidate.symbol, qty, notional, float(filters.min_qty),
            float(filters.max_qty), float(filters.min_notional),
            float(filters.max_notional), order_quote_qty, config["QUOTE_ASSET"],
        )
        return

    use_quote_order_qty = bool(filters.quote_order_qty_market_allowed)
    order_quantity = None if use_quote_order_qty else qty
    if not use_quote_order_qty:
        order_quote_qty = None

    level_cfg = dict(config)
    if candidate.setup is not None and candidate.setup.atr_value is not None:
        level_cfg["_atr_value"] = candidate.setup.atr_value
    elif bool(config.get("USE_ATR_EXIT", False)):
        logger.warning(
            "Entry %s dilewati: USE_ATR_EXIT aktif tetapi nilai ATR kandidat "
            "tidak tersedia, level exit tidak dapat dikunci dengan aman.",
            candidate.symbol,
        )
        return
    preview = strategy.resolve_exit_levels(level_cfg)
    if str(preview.get("source", "")).upper() == "ATR" and not (
        0.0 < float(preview.get("sl_pct") or 0.0) < price_ref
    ):
        logger.critical(
            "Entry %s dilewati: jarak SL ATR %.10g tidak masuk akal terhadap "
            "harga acuan %.10g. Cek ATR_MULT_SL/ATR kandidat.",
            candidate.symbol, float(preview.get("sl_pct") or 0.0), price_ref,
        )
        return
    client_order_id = _new_client_order_id("buy")
    state["pending_order"] = {
        "side": "BUY", "symbol": candidate.symbol, "qty": qty,
        "client_order_id": client_order_id, "created_at": state_mod.now_ms(),
        "levels": {
            "sl_pct": preview["sl_pct"], "tp_pct": preview["tp_pct"],
            "be_trigger_pct": preview["be_trigger_pct"], "be_lock_pct": preview["be_lock_pct"],
            "trail_start_pct": preview["trail_start_pct"], "trail_step_pct": preview["trail_step_pct"],
            "exit_source": preview["source"],
        },
    }
    pending_intent = dict(state["pending_order"])
    state_mod.save_state(config["STATE_FILE"], state)
    try:
        resp = _submit_market_order(
            client, candidate.symbol, "BUY", order_quantity, client_order_id,
            quote_order_qty=order_quote_qty,
        )
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

    levels = strategy.resolve_exit_levels(level_cfg)
    state["sl_pct"] = levels["sl_pct"]
    state["tp_pct"] = levels["tp_pct"]
    state["be_trigger_pct"] = levels["be_trigger_pct"]
    state["be_lock_pct"] = levels["be_lock_pct"]
    state["trail_start_pct"] = levels["trail_start_pct"]
    state["trail_step_pct"] = levels["trail_step_pct"]
    state["exit_source"] = levels["source"]
    state["sell_fail_count"] = 0

    state_mod.save_state(config["STATE_FILE"], state)
    logger.info("%s: level exit dikunci -> %s | %s",
                candidate.symbol, levels["source"], levels["note"])

    if _native_protection_enabled(config):
        _ensure_native_protection(client, config, filters, state)


def close_position(client: ExchangeClient, config: dict, filters_cache: dict,
                   state: dict, reason: str,
                   reference_price: float | None = None) -> None:
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

    if not _cancel_native_stop_before_exit(client, config, state):
        return

    filters = filters_cache.get(symbol)
    qty_to_sell = float(state["qty"])
    base_asset = symbol[: -len(config["QUOTE_ASSET"])]
    executable_bid = _executable_bid(client, symbol, reference_price)
    account_loaded = False
    locked_base = 0.0

    try:
        account = client.get_account()
        account_loaded = True
        free_base = get_balance(account, base_asset)
        locked_base = next(
            (float(b.get("locked", 0.0) or 0.0)
             for b in account.get("balances", [])
             if b.get("asset") == base_asset),
            0.0,
        )
        qty_to_sell = min(qty_to_sell, free_base)
    except BinanceAPIError as exc:
        logger.error("Gagal ambil saldo sebelum SELL %s: %s", symbol, exc)

    if filters:
        if filters.max_qty > 0:
            qty_to_sell = min(qty_to_sell, float(filters.max_qty))
        qty_to_sell = filters.round_qty(qty_to_sell)
        if (
            executable_bid is not None
            and filters.max_notional > 0
            and qty_to_sell * executable_bid > float(filters.max_notional)
        ):
            qty_to_sell = filters.round_qty(
                float(filters.max_notional) / executable_bid
            )
        if account_loaded and locked_base > 0 and qty_to_sell < float(filters.min_qty):
            logger.critical(
                "SELL %s ditahan: free base %.8f di bawah minQty tetapi masih "
                "ada locked base %.8f. Rekonsiliasi open order diperlukan.",
                symbol, qty_to_sell, locked_base,
            )
            state["reconciliation_required"] = True
            state["reconciliation_assets"] = [base_asset]
            state_mod.save_state(config["STATE_FILE"], state)
            return
        if qty_to_sell < float(filters.min_qty):
            logger.warning("Qty jual %s (%.8f) di bawah minQty bursa. Posisi direset sebagai dust.",
                           symbol, qty_to_sell)
            reset_position(state)
            state["sell_fail_count"] = 0
            state["cooldown_until"] = state_mod.now_ms() + config["COOLDOWN_MINUTES_AFTER_CLOSE"] * 60 * 1000
            state_mod.save_state(config["STATE_FILE"], state)
            try_dust_sweep(client, config, symbol)
            return
        if (
            executable_bid is not None
            and filters.min_notional > 0
            and qty_to_sell * executable_bid < float(filters.min_notional)
        ):
            logger.critical(
                "SELL %s ditahan: notional executable %.8f di bawah minNotional %.8f. "
                "Saldo dipertahankan untuk rekonsiliasi/dust sweep.",
                symbol, qty_to_sell * executable_bid, float(filters.min_notional),
            )
            state["reconciliation_required"] = True
            state["reconciliation_assets"] = [base_asset]
            state_mod.save_state(config["STATE_FILE"], state)
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

    executed_qty = max(0.0, float(resp.get("executedQty", 0.0) or 0.0))
    cumm_quote = max(0.0, float(resp.get("cummulativeQuoteQty", 0.0) or 0.0))
    status = str(resp.get("status") or "").upper()

    if status in _NONTERMINAL_ORDER_STATUSES or (
        not status and executed_qty < qty_to_sell - 1e-12
    ):
        pending_now = state.get("pending_order")
        if isinstance(pending_now, dict):
            pending_now["last_status"] = status or "UNKNOWN"
            pending_now["executed_qty"] = executed_qty
            pending_now["cummulative_quote_qty"] = cumm_quote
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [base_asset]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "SELL %s (%s) belum terminal: status=%s filled=%.8f dari %.8f. "
            "Intent dipertahankan; jangan kirim SELL duplikat.",
            symbol, reason, status or "UNKNOWN", executed_qty, qty_to_sell,
        )
        return

    if not status:
        status = "FILLED"

    state["pending_order"] = None
    sell_price = (cumm_quote / executed_qty) if executed_qty > 0 else 0.0
    pnl = (sell_price - entry_price) * executed_qty if entry_price > 0 else 0.0

    remaining = max(0.0, float(state["qty"]) - executed_qty)
    post_loaded = False
    post_locked = 0.0
    try:
        post_account = client.get_account()
        post_loaded = True
        post_total = get_total_balance(post_account, base_asset)
        post_locked = next(
            (float(b.get("locked", 0.0) or 0.0)
             for b in post_account.get("balances", [])
             if b.get("asset") == base_asset),
            0.0,
        )
        remaining = min(remaining, post_total)
    except BinanceAPIError:
        pass

    min_qty = float(filters.min_qty) if filters else 0.0
    fully_closed = (
        status == "FILLED"
        and executed_qty >= qty_to_sell - 1e-12
        and (remaining <= 0 or remaining < min_qty)
        and post_locked <= 0
    )
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

    state["qty"] = remaining
    state["sell_fail_count"] = 0
    state["reconciliation_required"] = bool(post_loaded and post_locked > 0)
    state["reconciliation_assets"] = [base_asset] if state["reconciliation_required"] else []
    state_mod.save_state(config["STATE_FILE"], state)
    logger.critical("SELL PARTIAL/TERMINAL %s (%s): status=%s filled=%.8f dari %.8f, sisa state=%.8f. Posisi TIDAK direset.",
                    symbol, reason, status or "UNKNOWN", executed_qty, qty_to_sell, remaining)


def check_manual_control(client: ExchangeClient, config: dict, filters_cache: dict,
                          state: dict) -> None:
    control_path = config.get("CONTROL_FILE") or get_control_file(config)
    cmd = state_mod.load_control(control_path)
    if not cmd:
        return

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


def manage_exit(client: ExchangeClient, config: dict, filters_cache: dict,
                 state: dict, current_price: float) -> None:
    if not state["current_symbol"] or state["qty"] <= 0 or state["entry_price"] <= 0:
        return

    if _native_protection_enabled(config):
        if isinstance(state.get("native_oco"), dict):
            if not _reconcile_native_oco(client, config, state):
                if not state.get("current_symbol") or state.get("_native_stop_exit_blocked"):
                    return
        if isinstance(state.get("native_stop"), dict):
            if not _reconcile_native_stop(client, config, state):
                if not state.get("current_symbol") or state.get("_native_stop_exit_blocked"):
                    return
        if (
            state.get("current_symbol")
            and not state.get("native_oco")
            and not state.get("native_stop")
            and not state.get("_native_stop_exit_blocked")
        ):
            filters = filters_cache.get(state["current_symbol"])
            if not _ensure_native_protection(client, config, filters, state):
                if state.get("_native_stop_exit_blocked"):
                    return

    atr_mode = str(state.get("exit_source", "")).upper() == "ATR"
    entry = float(state["entry_price"])
    sl = _exit_distance(state, config, "sl_pct", "SL_PCT", atr_mode, entry)
    tp = _exit_distance(state, config, "tp_pct", "TP_PCT", atr_mode, entry)
    be_trigger = _exit_distance(state, config, "be_trigger_pct", "BE_TRIGGER_PCT", atr_mode, entry)
    be_lock = _exit_distance(state, config, "be_lock_pct", "BE_LOCK_PCT", atr_mode, entry)
    trail_start = _exit_distance(state, config, "trail_start_pct", "TRAILING_START_PCT", atr_mode, entry)
    trail_step = _exit_distance(state, config, "trail_step_pct", "TRAILING_STEP_PCT", atr_mode, entry)
    if atr_mode:
        pnl_unit = current_price - state["entry_price"]
        sl_hit = current_price <= state["entry_price"] - sl
        tp_hit = current_price >= state["entry_price"] + tp
        be_trigger_hit = pnl_unit >= be_trigger
        trail_start_hit = pnl_unit >= trail_start
    else:
        pnl_unit = (current_price / state["entry_price"] - 1.0) * 100.0
        sl_hit = pnl_unit <= -sl
        tp_hit = pnl_unit >= tp
        be_trigger_hit = pnl_unit >= be_trigger
        trail_start_hit = pnl_unit >= trail_start

    if config["USE_STOP_LOSS"] and sl_hit:
        close_position(client, config, filters_cache, state, "STOP_LOSS",
                       reference_price=current_price)
        return
    if config["USE_BREAKEVEN"] and not state["be_active"] and be_trigger_hit:
        state["be_active"] = True
        state["be_stop_price"] = (state["entry_price"] + be_lock if atr_mode
                                   else state["entry_price"] * (1 + be_lock / 100.0))
    if config["USE_TRAILING"]:
        if not state["trailing_active"] and trail_start_hit:
            state["trailing_active"] = True
            state["trailing_stop_price"] = (current_price - trail_step if atr_mode
                                             else current_price * (1 - trail_step / 100.0))
        elif state["trailing_active"]:
            candidate_stop = (current_price - trail_step if atr_mode
                              else current_price * (1 - trail_step / 100.0))
            if candidate_stop > state["trailing_stop_price"]:
                state["trailing_stop_price"] = candidate_stop
    reasons = []
    if config["USE_TP"] and tp_hit:
        reasons.append("TAKE_PROFIT")
    if state["be_active"] and current_price <= state["be_stop_price"]:
        reasons.append("BREAKEVEN")
    if state["trailing_active"] and current_price <= state["trailing_stop_price"]:
        reasons.append("TRAILING_STOP")
    if reasons:
        close_position(client, config, filters_cache, state, "+".join(reasons),
                       reference_price=current_price)


def run(config: dict, lifecycle=None) -> int:
    global _shutdown_requested
    _shutdown_requested = False
    _shutdown_event.clear()
    setup_logging(config)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    if os.name == "nt" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_signal)

    if CONFIG_LOAD_ERRORS:
        logger.critical("Konfigurasi runtime tidak aman untuk dipakai: %s",
                        "; ".join(CONFIG_LOAD_ERRORS))
        return 2

    from config.config import InvalidModeError
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

    risk_ok, risk_reason = account_risk_gate(config)
    if not risk_ok:
        logger.critical("%s", risk_reason)
        if lifecycle is not None:
            lifecycle.write("STOPPED", reason=risk_reason)
        return 2
    need_setup = strategy.required_lookback_bars(config)
    have_bars = int(config.get("CONFIRM_LOOKBACK_BARS", 48))
    if have_bars < need_setup:
        logger.warning(
            "CONFIRM_LOOKBACK_BARS=%d lebih kecil dari %d candle yang dibutuhkan "
            "struktur setup. Bot tetap mengambil %d candle per konfirmasi agar deteksi "
            "tidak selalu gagal, tetapi perbaiki nilai config ini supaya backtest dan live "
            "benar-benar memakai angka yang sama.",
            have_bars, need_setup, strategy.confirm_window_bars(config),
        )
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
    logger.info("Mode exit efektif: %s", describe_exit_mode(config, state))
    if state["current_symbol"]:
        logger.info("Melanjutkan posisi yang sudah ada: %s qty=%.8f @ %.6f",
                    state["current_symbol"], state["qty"], state["entry_price"])
    reconcile_state_with_exchange(client, config, state, filters_cache)
    if lifecycle is not None:
        lifecycle.write("RUNNING")

    consecutive_errors = 0
    last_time_sync = time.time()
    last_heartbeat = 0.0
    TIME_SYNC_INTERVAL_SECONDS = 15 * 60
    FILTERS_REFRESH_INTERVAL_SECONDS = 6 * 3600

    def klines_fetcher(symbol: str):
        lookback = strategy.confirm_window_bars(config)
        raw = client.get_klines(symbol, config["CONFIRM_INTERVAL"], limit=lookback + 1)
        now_ms = state_mod.now_ms()
        closed = [k for k in strategy.parse_klines(raw) if k.close_time < now_ms]
        return closed[-lookback:]

    exit_code = 0
    while not _shutdown_requested:
        loop_start = time.time()
        try:
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
                held = state.get("current_symbol")
                if held and tradable_symbols and held not in tradable_symbols:
                    logger.critical(
                        "Simbol %s yang sedang dipegang TIDAK lagi berstatus TRADING di "
                        "Binance. Order SELL akan ditolak bursa. Perlu tindakan manual.",
                        held,
                    )

            if state.get("pending_order"):
                logger.warning("Merekonsiliasi intent order pending %s sebelum melanjutkan loop.",
                               state["pending_order"].get("client_order_id"))
                reconcile_state_with_exchange(client, config, state)

            check_manual_control(client, config, filters_cache, state)

            current_price = None
            if state["current_symbol"]:
                book = client.get_book_ticker(state["current_symbol"], max_retries=1)
                current_price = float(book.get("bidPrice", 0.0))
                if current_price <= 0:
                    raise BinanceAPIError(
                        502, None,
                        f"bid executable {state['current_symbol']} tidak valid",
                    )

            equity = get_equity(client, config, state,
                                position_price=current_price)
            if equity is None:
                entries_paused = bool(state.get("dd_stopped") or state.get("daily_stopped"))
            else:
                entries_paused = update_equity_controls(state, equity, config)

            maybe_force_close_at_risk_limit(client, config, filters_cache, state,
                                            entries_paused, current_price)

            if state["current_symbol"] and current_price is not None:
                manage_exit(client, config, filters_cache, state, current_price)

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
                    daily_fetcher = scanner.make_daily_klines_fetcher(client, cache={})
                    scan_config = dict(config)
                    if config.get("BTC_FILTER_ENABLED", False):
                        scan_config["_btc_filter_fail_closed"] = True
                        try:
                            btc_raw = client.get_klines(
                                "BTC" + config["QUOTE_ASSET"], config["CONFIRM_INTERVAL"],
                                limit=int(config.get("BTC_LOOKBACK_BARS", 3) or 3) + 1)
                            btc_closed = [k for k in strategy.parse_klines(btc_raw)
                                          if k.close_time < state_mod.now_ms()]
                            look = int(config.get("BTC_LOOKBACK_BARS", 3) or 3)
                            if len(btc_closed) >= look + 1:
                                scan_config["_btc_drop_pct"] = (btc_closed[-1].close /
                                    btc_closed[-look-1].close - 1.0) * 100.0
                        except Exception as exc:
                            logger.warning("Filter BTC tidak dapat dihitung; kandidat ditolak: %s", exc)
                    best = scanner.find_best_candidate(tickers, klines_fetcher, scan_config,
                                                       tradable_symbols,
                                                       daily_klines_fetcher=daily_fetcher,
                                                       reference_ms=state_mod.now_ms())
                    if best:
                        book = client.get_book_ticker(best.symbol)
                        bid, ask = float(book["bidPrice"]), float(book["askPrice"])
                        spread_pct = scanner.spread_pct_from_book(bid, ask)
                        max_chase = float(config.get("MAX_CHASE_PCT", 0) or 0)
                        signal_close = float(best.setup.breakout_level or 0.0) if (
                            best.setup is not None and best.setup.breakout_level
                        ) else 0.0
                        chase_ok = True
                        if max_chase > 0 and signal_close > 0 and ask > signal_close * (
                            1.0 + max_chase / 100.0
                        ):
                            chase_ok = False
                            logger.info(
                                "Kandidat %s dilewati: ask %.8f sudah %+.2f%% di atas "
                                "close candle sinyal %.8f (batas MAX_CHASE_PCT %.2f%%).",
                                best.symbol, ask,
                                (ask / signal_close - 1.0) * 100.0,
                                signal_close, max_chase,
                            )
                        if spread_pct <= config["MAX_SPREAD_PCT"] and chase_ok:
                            logger.info(
                                "Kandidat terpilih: %s (vol24h=%.0f, 24h=%.2f%%, spread=%.3f%%) | %s",
                                best.symbol, best.quote_volume, best.price_change_pct,
                                spread_pct, best.confirm_reason,
                            )
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
                                open_position(client, config, filters_cache, state, best,
                                              reference_price=ask)
                        else:
                            logger.info("Kandidat %s dilewati: spread %.3f%% > batas %.3f%%.",
                                        best.symbol, spread_pct, config["MAX_SPREAD_PCT"])
                    else:
                        logger.info("Tidak ada setup momentum (3-dari-4 konfirmasi) yang sah pada scan ini.")

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
        except Exception as exc:
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
        _shutdown_event.wait(max(1.0, config["LOOP_INTERVAL_SECONDS"] - elapsed))

    if lifecycle is not None and _shutdown_requested:
        lifecycle.write("STOPPING", reason="Shutdown graceful sedang menyimpan state.")
    try:
        state_mod.save_state(config["STATE_FILE"], state)
    except OSError as exc:
        logger.error("Gagal menyimpan state saat shutdown: %s", exc)
        exit_code = 1

    try:
        client.close()
    except Exception:
        pass
    logger.info("Bot berhenti.")
    return exit_code


def selftest() -> None:
    import tempfile

    cfg = dict(PUMP_CONFIG)
    cfg["STATE_FILE"] = os.path.join(tempfile.gettempdir(), "pump_bot_selftest_state.json")
    cfg["MIN_QUOTE_VOLUME_USDT_24H"] = 1_000_000

    print("=== SELFTEST: saringan semesta, gerbang pump, dan urutan volume ===")
    HARI_MS = 86_400_000

    def _harian(quote_volume_harian: float):
        return [strategy.Kline(open_time=i * HARI_MS, open=1.0, high=1.0, low=1.0,
                               close=1.0, close_time=(i + 1) * HARI_MS - 1,
                               volume=quote_volume_harian, quote_volume=quote_volume_harian)
                for i in range(7)]

    RATA_HARIAN = {
        "AUSDT": 2_000_000.0, "BUSDT": 1_000_000.0, "CUSDT": 1_000_000.0,
        "DUSDT": 1_000.0, "EUSDT": 4_000_000.0, "BTCUPUSDT": 1_000.0,
        "USDCUSDT": 1_000.0, "HALTUSDT": 1_000.0,
    }

    def daily_fetcher(symbol: str):
        if symbol == "FUSDT":
            return _harian(1_000.0)[:3]
        if symbol == "GUSDT":
            raise RuntimeError("timeout simulasi")
        return _harian(RATA_HARIAN.get(symbol, 1_000.0))

    ref_ms = 7 * HARI_MS + 1

    tickers = [
        {"symbol": "AUSDT", "priceChangePercent": "15.0", "quoteVolume": "6000000", "lastPrice": "1.0"},
        {"symbol": "BUSDT", "priceChangePercent": "25.0", "quoteVolume": "4000000", "lastPrice": "2.0"},
        {"symbol": "CUSDT", "priceChangePercent": "-3.0", "quoteVolume": "9000000", "lastPrice": "0.5"},
        {"symbol": "DUSDT", "priceChangePercent": "40.0", "quoteVolume": "10000", "lastPrice": "0.1"},
        {"symbol": "EUSDT", "priceChangePercent": "20.0", "quoteVolume": "4000000", "lastPrice": "1.0"},
        {"symbol": "FUSDT", "priceChangePercent": "30.0", "quoteVolume": "8000000", "lastPrice": "1.0"},
        {"symbol": "GUSDT", "priceChangePercent": "30.0", "quoteVolume": "8000000", "lastPrice": "1.0"},
        {"symbol": "BTCUPUSDT", "priceChangePercent": "50.0", "quoteVolume": "9000000", "lastPrice": "3.0"},
        {"symbol": "USDCUSDT", "priceChangePercent": "20.0", "quoteVolume": "9000000", "lastPrice": "1.0"},
        {"symbol": "HALTUSDT", "priceChangePercent": "10.0", "quoteVolume": "8000000", "lastPrice": "1.0"},
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

    tanpa_sumber = scanner.filter_and_rank_candidates(tickers, cfg, tradable)
    assert tanpa_sumber == [], \
        "Tanpa sumber candle harian, gerbang pump harus menolak semua simbol (fail closed)"
    print("  -> OK (tanpa sumber candle harian, gerbang pump fail closed)")

    print("\n=== SELFTEST: deteksi setup pullback dan retest ===")

    vals = [100.0] * 30 + [100.2, 100.4, 99.4, 98.4, 97.4, 97.6,
                            98.6, 98.1, 98.3, 97.8, 98.8, 99.8, 98.8,
                            99.8, 99.3, 99.5, 98.5, 99.5, 98.5, 100.0, 100.5]
    kl_ok = [strategy.Kline(i * 300_000, v, v + 1, max(0.01, v - 1), v,
                            i * 300_000 + 299_999,
                            3000.0 if i >= len(vals) - 2 else 1000.0,
                            v * (3000.0 if i >= len(vals) - 2 else 1000.0))
             for i, v in enumerate(vals)]
    hasil = scanner.detect_pullback_retest(kl_ok, cfg)
    print(f"  Skenario 3 dari 4 konfirmasi momentum -> ok={hasil.ok} ({hasil.reason})")
    assert hasil.ok, "Skenario momentum sah harusnya lolos"
    assert hasil.atr_value is not None, "ATR harus ikut tersedia pada setup"
    ok_ce, reason_ce = scanner.confirm_entry(kl_ok, cfg)
    assert ok_ce and reason_ce == hasil.reason, "confirm_entry harus memakai deteksi momentum"
    print("  -> OK (confirm_entry tetap mengembalikan (bool, str))")

    print("\n=== SELFTEST: simulasi exit (TP/Breakeven/Trailing) ===")
    from decimal import Decimal as D
    from trading.clients.binance_client import SymbolFilters

    class FakeTradeClient:

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

        def new_market_order(self, symbol, side, quantity=None, quote_order_qty=None,
                             new_client_order_id=None):
            qty = float(quantity or 0.0)
            if qty <= 0 and quote_order_qty is not None and self.price > 0:
                qty = float(quote_order_qty) / self.price
            self.orders.append((symbol, side, qty))
            return {"status": "FILLED", "executedQty": str(qty),
                    "cummulativeQuoteQty": str(qty * self.price)}

        def get_dust_convertible(self, account_type="SPOT"):
            return {"details": []}

        def convert_dust(self, assets, account_type="SPOT"):
            return {"totalTransfered": "0"}

    filters_cache = {"TESTUSDT": SymbolFilters(step_size=D("0.01"), min_qty=D("0.01"),
                                                min_notional=D("5"), tick_size=D("0.0001"))}
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

    manage_exit(FakeTradeClient(), cfg_exit, filters_cache, state, 103.5)
    assert state["be_active"], "Breakeven harusnya sudah aktif di profit 3.5%"
    assert state["current_symbol"] == "TESTUSDT", "Belum boleh close, baru breakeven aktif"
    print(f"  Setelah profit +3.5%: be_active={state['be_active']}, be_stop={state['be_stop_price']:.4f} -> OK")

    manage_exit(FakeTradeClient(), cfg_exit, filters_cache, state, 106.5)
    assert state["current_symbol"] is None, "Posisi harusnya sudah tertutup kena TAKE_PROFIT"
    print("  Setelah profit +6.5%: posisi tertutup (TAKE_PROFIT) -> OK")

    print("\n=== SELFTEST: Stop Loss (harga langsung turun sejak entry, TIDAK sempat untung) ===")
    assert cfg_exit["USE_STOP_LOSS"], "USE_STOP_LOSS harus aktif di skenario ini"
    state2 = dict(DEFAULT_STATE)
    state2["current_symbol"] = "TESTUSDT"
    state2["entry_price"] = 100.0
    state2["qty"] = 1.0
    state2["entry_time"] = state_mod.now_ms()

    manage_exit(FakeTradeClient(), cfg_exit, filters_cache, state2, 98.0)
    assert state2["current_symbol"] == "TESTUSDT", "Rugi -2% belum boleh kena Stop Loss (ambang 3.0%)"
    assert not state2["be_active"], "Breakeven tidak boleh aktif kalau posisi rugi"
    print("  Rugi -2%: posisi masih terbuka, BE/Trailing tidak aktif -> OK")

    manage_exit(FakeTradeClient(), cfg_exit, filters_cache, state2, 96.5)
    assert state2["current_symbol"] is None, "Posisi harusnya sudah tertutup kena STOP_LOSS di rugi -3.5%"
    print("  Rugi -3.5%: posisi tertutup (STOP_LOSS) -> OK")

    print("\n=== SELFTEST: ukuran posisi (RISK_PERCENT, plafon, bantalan saldo) ===")

    class SizingClient(FakeTradeClient):

        def __init__(self, usdt_free):
            super().__init__(base_asset="TESTB", free=0.0, price=1.0)
            self.usdt_free = usdt_free

        def get_account(self):
            return {"balances": [
                {"asset": "USDT", "free": str(self.usdt_free), "locked": "0"},
                {"asset": "TESTB", "free": str(self.free), "locked": "0"},
            ]}

        def new_market_order(self, symbol, side, quantity=None, quote_order_qty=None,
                             new_client_order_id=None):
            resp = super().new_market_order(symbol, side, quantity, quote_order_qty,
                                            new_client_order_id)
            if side == "BUY":
                bought = float(quantity or 0.0)
                if bought <= 0 and quote_order_qty is not None and self.price > 0:
                    bought = float(quote_order_qty) / self.price
                self.free += bought
            return resp

    from decimal import Decimal as D2
    from trading.clients.binance_client import SymbolFilters as SF2
    size_filters = {"TESTBUSDT": SF2(step_size=D2("0.00000001"), min_qty=D2("0.00000001"),
                                      min_notional=D2("1"), tick_size=D2("0.0001"))}
    cand = scanner.Candidate(symbol="TESTBUSDT", base_asset="TESTB", price_change_pct=20.0,
                              quote_volume=9e6, last_price=1.0, confirmed=True,
                              confirm_reason="selftest")

    def nominal_dipakai(cfg_size, saldo):
        cl = SizingClient(saldo)
        st = dict(DEFAULT_STATE)
        open_position(cl, cfg_size, size_filters, st, cand)
        assert cl.orders, "Order BUY seharusnya terkirim"
        return float(cl.orders[-1][2])

    cfg_size = dict(cfg)
    cfg_size.update({"USE_RISK_PERCENT": True, "RISK_PERCENT": 95.0,
                      "BALANCE_BUFFER_PCT": 0.5, "MAX_POSITION_USDT": 0,
                      "USE_ATR_EXIT": False})

    for saldo, harap in ((100.0, 100 * 0.995 * 0.95), (1000.0, 1000 * 0.995 * 0.95),
                          (5000.0, 5000 * 0.995 * 0.95)):
        got = nominal_dipakai(cfg_size, saldo)
        assert abs(got - harap) < 0.01, f"saldo {saldo}: harap {harap:.2f}, dapat {got:.2f}"
        print(f"  Saldo {saldo:>7.0f} -> pakai {got:>8.2f} USDT ({got / saldo * 100:.2f}% saldo) -> OK")

    cfg_cap = dict(cfg_size)
    cfg_cap["MAX_POSITION_USDT"] = 10.0
    got_cap = nominal_dipakai(cfg_cap, 1000.0)
    assert abs(got_cap - 10.0) < 1e-6, f"Plafon 10 USDT harus mengikat, dapat {got_cap}"
    print(f"  Plafon 10 USDT aktif, saldo 1000 -> pakai {got_cap:.2f} USDT "
          f"({got_cap / 1000 * 100:.2f}% saldo) -> OK (inilah bug lama)")

    cfg_allin = dict(cfg_size)
    cfg_allin["RISK_PERCENT"] = 100.0
    got_allin = nominal_dipakai(cfg_allin, 1000.0)
    assert got_allin < 1000.0, "All-in tidak boleh membelanjakan 100% saldo persis (butuh ruang fee)"
    assert got_allin >= 1000.0 * 0.98, f"Bantalan terlalu besar: {got_allin}"
    print(f"  RISK_PERCENT=100, saldo 1000 -> pakai {got_allin:.2f} USDT "
          f"(sisa {1000 - got_allin:.2f} untuk fee) -> OK")

    cfg_fixed_size = dict(cfg_size)
    cfg_fixed_size.update({"USE_RISK_PERCENT": False, "POSITION_SIZE_USDT": 25.0})
    got_fixed = nominal_dipakai(cfg_fixed_size, 1000.0)
    assert abs(got_fixed - 25.0) < 1e-6, f"Mode nominal tetap harus pakai 25 USDT, dapat {got_fixed}"
    print(f"  Mode nominal tetap (USE_RISK_PERCENT=False) -> {got_fixed:.2f} USDT -> OK")

    print("\n=== SELFTEST: level exit yang dikunci di state dipakai manage_exit ===")
    cfg_locked = dict(cfg_exit)
    cfg_locked["SL_PCT"] = 3.0
    state_locked = dict(DEFAULT_STATE)
    state_locked["current_symbol"] = "TESTUSDT"
    state_locked["entry_price"] = 100.0
    state_locked["qty"] = 1.0
    state_locked["entry_time"] = state_mod.now_ms()
    state_locked["sl_pct"] = 1.0
    state_locked["tp_pct"] = 2.0

    manage_exit(FakeTradeClient(), cfg_locked, filters_cache, state_locked, 98.5)
    assert state_locked["current_symbol"] is None,         "manage_exit harus memakai sl_pct dari state (1%), bukan SL_PCT config (3%)"
    print("  SL terkunci di state (1%) dipakai, bukan SL_PCT config (3%) -> OK")

    state_tp = dict(DEFAULT_STATE)
    state_tp["current_symbol"] = "TESTUSDT"
    state_tp["entry_price"] = 100.0
    state_tp["qty"] = 1.0
    state_tp["entry_time"] = state_mod.now_ms()
    state_tp["sl_pct"] = 1.0
    state_tp["tp_pct"] = 2.0
    cfg_tp = dict(cfg_locked)
    cfg_tp["USE_BREAKEVEN"] = False
    cfg_tp["USE_TRAILING"] = False
    manage_exit(FakeTradeClient(), cfg_tp, filters_cache, state_tp, 102.5)
    assert state_tp["current_symbol"] is None,         "manage_exit harus memakai tp_pct dari state (2%), bukan TP_PCT config (6%)"
    print("  TP terkunci di state (2%) dipakai, bukan TP_PCT config (6%) -> OK")

    state_old = dict(DEFAULT_STATE)
    del state_old["sl_pct"]
    del state_old["tp_pct"]
    state_old["current_symbol"] = "TESTUSDT"
    state_old["entry_price"] = 100.0
    state_old["qty"] = 1.0
    state_old["entry_time"] = state_mod.now_ms()
    manage_exit(FakeTradeClient(), cfg_locked, filters_cache, state_old, 96.0)
    assert state_old["current_symbol"] is None,         "State versi lama tanpa sl_pct harus tetap terlindungi oleh SL_PCT config"
    print("  State versi lama tanpa sl_pct tetap terlindungi SL_PCT config -> OK")

    print("\n=== SELFTEST: invariant Breakeven & Trailing tetap ===")
    cfg_full = dict(cfg)
    cfg_full.update({"SL_PCT": 2.5, "TP_PCT": 5.0,
                     "BE_TRIGGER_PCT": 9.0, "BE_LOCK_PCT": 12.0,
                     "TRAILING_START_PCT": 4.0, "TRAILING_STEP_PCT": 9.0})
    lv_full = strategy.resolve_exit_levels(cfg_full)
    assert lv_full["trail_step_pct"] <= lv_full["sl_pct"] + 1e-9
    assert lv_full["be_trigger_pct"] <= lv_full["trail_start_pct"] + 1e-9
    assert lv_full["be_lock_pct"] <= lv_full["be_trigger_pct"] + 1e-9
    print("  Invariant exit tetap diterapkan -> OK")

    st_be = dict(DEFAULT_STATE)
    st_be["current_symbol"] = "TESTUSDT"
    st_be["entry_price"] = 100.0
    st_be["qty"] = 1.0
    st_be["entry_time"] = state_mod.now_ms()
    st_be["sl_pct"] = 10.0
    st_be["tp_pct"] = 20.0
    st_be["be_trigger_pct"] = 5.0
    st_be["be_lock_pct"] = 1.0
    st_be["trail_start_pct"] = 8.0
    st_be["trail_step_pct"] = 3.0
    cfg_be_cfg = dict(cfg_exit)
    cfg_be_cfg["BE_TRIGGER_PCT"] = 1.0
    manage_exit(FakeTradeClient(), cfg_be_cfg, filters_cache, st_be, 102.0)
    assert not st_be["be_active"],         "Breakeven memakai BE_TRIGGER_PCT config, seharusnya memakai be_trigger_pct dari state"
    manage_exit(FakeTradeClient(), cfg_be_cfg, filters_cache, st_be, 106.0)
    assert st_be["be_active"], "Breakeven harus aktif setelah melewati trigger dari state"
    assert abs(st_be["be_stop_price"] - 101.0) < 1e-6
    print("  BE/Trailing memakai level state saat tersedia -> OK")

    print("\n=== SELFTEST: perintah manual 'Jual Sekarang' dari dashboard (control file) ===")
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        control_path = f"{tmpdir}/pump_bot_control.json"
        cfg_ctrl = dict(cfg_exit)
        cfg_ctrl["CONTROL_FILE"] = control_path

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

        state4 = dict(DEFAULT_STATE)
        state4["current_symbol"] = "TESTUSDT"
        state4["entry_price"] = 100.0
        state4["qty"] = 1.0
        state4["entry_time"] = state_mod.now_ms()
        state_mod.save_control(control_path, {
            "action": "CLOSE_POSITION", "symbol": "TESTUSDT",
            "requested_at": state_mod.now_ms() - 10 * 60 * 1000,
        })
        check_manual_control(FakeTradeClient(), cfg_ctrl, filters_cache, state4)
        assert state4["current_symbol"] == "TESTUSDT", "Perintah kadaluarsa (>2 menit) harus DIABAIKAN"
        print("  Perintah kadaluarsa (10 menit lalu) -> diabaikan, posisi tetap terbuka -> OK")

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

        state6 = dict(DEFAULT_STATE)
        state_mod.save_control(control_path, {
            "action": "CLOSE_POSITION", "symbol": "TESTUSDT", "requested_at": state_mod.now_ms(),
        })
        check_manual_control(FakeTradeClient(), cfg_ctrl, filters_cache, state6)
        assert state6["current_symbol"] is None, "Tanpa posisi terbuka, perintah manual harus diabaikan dengan aman"
        print("  Tidak ada posisi terbuka saat perintah diproses -> diabaikan dengan aman, tidak error -> OK")

    print("\n=== SELFTEST: dust sweep ke BNB setelah posisi ditutup ===")

    class FakeDustClient:

        def __init__(self, convertible_assets):
            self.convertible_assets = convertible_assets
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
    cfg = dict(cfg)
    cfg["MODE"] = "LIVE"

    fake_a = FakeDustClient(convertible_assets=["PEPE"])
    try_dust_sweep(fake_a, cfg, "PEPEUSDT")
    assert fake_a.convert_calls == [["PEPE"]], f"Harusnya convert PEPE saja, dapat: {fake_a.convert_calls}"
    print("  Sisa PEPE terdaftar dust convertible -> convert_dust(['PEPE']) dipanggil -> OK")

    fake_b = FakeDustClient(convertible_assets=[])
    try_dust_sweep(fake_b, cfg, "PEPEUSDT")
    assert fake_b.convert_calls == [], "Tidak boleh convert kalau asset tidak terdaftar sebagai dust"
    print("  Sisa PEPE TIDAK terdaftar dust convertible -> convert_dust tidak dipanggil -> OK")

    fake_c = FakeDustClient(convertible_assets=["USDT", "BNB"])
    try_dust_sweep(fake_c, cfg, "BNBUSDT")
    assert fake_c.convert_calls == [], "BNB tidak boleh pernah dikonversi (proteksi keras)"
    print("  Simbol dengan base asset BNB -> TIDAK PERNAH dikonversi (proteksi modal) -> OK")

    cfg_paper = dict(cfg)
    cfg_paper["MODE"] = "PAPER"
    fake_d = FakeDustClient(convertible_assets=["PEPE"])
    try_dust_sweep(fake_d, cfg_paper, "PEPEUSDT")
    assert fake_d.convert_calls == [], "Mode PAPER tidak boleh memanggil convert_dust (endpoint /sapi bertanda tangan)"
    print("  Mode PAPER -> dust sweep dilewati, tidak ada panggilan /sapi -> OK")

    fake_e = FakeDustClient(convertible_assets=["PEPE"])
    fake_e.fail_convert = True
    try:
        try_dust_sweep(fake_e, cfg, "PEPEUSDT")
        gagal_ditangani = True
    except BinanceAPIError:
        gagal_ditangani = False
    assert gagal_ditangani, "Kegagalan convert_dust (mis. rate limit) harus ditangani, bukan dilempar ke pemanggil"
    print("  convert_dust gagal (simulasi rate limit Binance) -> ditangani dengan aman, tidak crash -> OK")

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

        st_r = dict(DEFAULT_STATE)
        st_r["current_symbol"] = "PEPEUSDT"
        st_r["qty"] = 1000.0
        st_r["entry_price"] = 0.01
        reconcile_state_with_exchange(ReconClient({}), cfg_rec, st_r)
        assert st_r["current_symbol"] is None and st_r["qty"] == 0.0, \
            "Saldo 0 -> posisi hantu harus direset"
        print("  Saldo 0 di exchange -> posisi hantu direset -> OK")

        st_r2 = dict(DEFAULT_STATE)
        st_r2["current_symbol"] = "SOLUSDT"
        st_r2["qty"] = 10.0
        st_r2["entry_price"] = 100.0
        reconcile_state_with_exchange(ReconClient({"SOL": 9.5}), cfg_rec, st_r2)
        assert abs(st_r2["qty"] - 9.5) < 1e-12 and st_r2["entry_price"] == 100.0, \
            "Qty harus disesuaikan ke saldo nyata"
        print("  Qty state > saldo nyata -> qty disesuaikan -> OK")

        cl_idle = ReconClient({})
        st_idle = dict(DEFAULT_STATE)
        reconcile_state_with_exchange(cl_idle, cfg_rec, st_idle)
        assert cl_idle.calls == 1 and not st_idle["reconciliation_required"], \
            "State kosong harus cek saldo sekali tetapi tidak boleh memblokir akun benar-benar kosong"
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
    assert age2b == age2, "Hasil cache harus sama dengan hasil panggilan pertama"
    assert cl_age.calls == 1, "Hasil kedua harus dari cache, bukan panggilan API baru"
    assert listing_age_days(AgeClient(None), "KOSONGUSDT", NOW10) == 0.0, \
        "Tanpa riwayat -> usia 0 (akan ditolak ambang minimum)"
    print("  Usia 10 hari / 2 hari dihitung benar, cache hemat API, tanpa riwayat -> 0 -> OK")

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

    from config.config import InvalidModeError
    from infrastructure.process.runtime_control import BotAlreadyRunningError, BotRuntime

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
