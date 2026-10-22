from __future__ import annotations

import argparse
import hashlib
import logging
import math
import os
import signal
import sys
import threading
import time
import uuid
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from trading.clients.binance_client import (
    BinanceAPIError,
    BinanceRateLimitError,
    SymbolFilters,
    build_filters_cache,
    build_trading_symbols,
    permission_sets_allow,
)
from trading.clients.exchange_client import (
    ExchangeClient,
    create_exchange_client,
)
from config.config import (
    PUMP_CONFIG,
    CONFIG_LOAD_ERRORS,
    get_base_url,
    get_control_file,
    is_paper,
    require_valid_mode,
)
from market import market_scanner as scanner
from infrastructure.console import configure_utf8_console
from infrastructure.storage import state as state_mod
from infrastructure.storage.log_rotation import SafeRotatingFileHandler
from infrastructure.storage.trade_ledger import append_trade_record
from trading import strategy_drawdown as strategy_dd
from trading.equity import (
    account_risk_snapshot,
    _effective_account_permissions,
    _validate_live_account_snapshot,
    collect_equity,
    publish_equity,
)
from trading.accounting import (
    TradeAccountingError,
    normalize_fills,
    recompute_trade_accounting,
    validate_fill_totals,
)
from strategy import indicators as strategy

logger = logging.getLogger("pump_bot")
_shutdown_requested = False
_shutdown_event = threading.Event()


class HtfKlineCache:

    def __init__(self, client, config: dict) -> None:
        self._client = client
        self._config = config
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[int, list]] = {}
        self._negative_cache: dict[str, tuple[float, str, bool]] = {}

    def interval(self) -> str:
        raise NotImplementedError

    def window_bars(self) -> int:
        raise NotImplementedError

    def verdict(self, symbol: str, now_ms: int) -> dict:
        raise NotImplementedError

    def _interval_ms(self) -> int:
        return int(strategy.INTERVAL_MINUTES[self.interval()]) * 60_000

    def negative_cache_ttl_seconds(self) -> float:
        return 0.0

    def require_latest_closed_bar(self) -> bool:
        return False

    def _last_closed_open_time(self, now_ms: int) -> int:
        step = self._interval_ms()
        return ((int(now_ms) // step) - 1) * step

    def _simpan_kegagalan(
        self, symbol: str, reason: str, *, gagal: bool, ttl: float
    ) -> None:
        if ttl <= 0:
            return
        with self._lock:
            self._negative_cache[symbol] = (
                time.monotonic() + ttl,
                str(reason),
                bool(gagal),
            )

    def window_klines(self, symbol: str, now_ms: int) -> list:
        jendela = self.window_bars()
        if jendela <= 0:
            return []
        terakhir_tutup = self._last_closed_open_time(now_ms)
        ttl_gagal = max(0.0, float(self.negative_cache_ttl_seconds()))
        with self._lock:
            tersimpan = self._cache.get(symbol)
            if tersimpan is not None:
                cache_cocok = (
                    tersimpan[0] == terakhir_tutup
                    if self.require_latest_closed_bar()
                    else tersimpan[0] >= terakhir_tutup
                )
                if cache_cocok:
                    return list(tersimpan[1])
            negatif = self._negative_cache.get(symbol)
            if negatif is not None:
                sampai, alasan, gagal = negatif
                if time.monotonic() < sampai:
                    if gagal:
                        raise RuntimeError(
                            f"pengambilan candle {self.interval()} sebelumnya gagal "
                            f"(retry ditahan sementara): {alasan}"
                        )
                    return []
                self._negative_cache.pop(symbol, None)

        try:
            raw = self._client.get_klines(
                symbol,
                self.interval(),
                limit=min(strategy.TREND_KLINE_LIMIT, jendela + 1),
            )
            closed = [
                k
                for k in strategy.parse_klines(raw or [])
                if int(k.close_time) < int(now_ms)
            ]
        except Exception as exc:
            self._simpan_kegagalan(symbol, str(exc), gagal=True, ttl=ttl_gagal)
            raise

        siap = closed[-jendela:]
        if not siap:
            self._simpan_kegagalan(
                symbol,
                f"tidak ada candle {self.interval()} tertutup yang tersedia",
                gagal=False,
                ttl=ttl_gagal,
            )
            return []
        if (
            self.require_latest_closed_bar()
            and int(siap[-1].open_time) != int(terakhir_tutup)
        ):
            alasan = (
                f"candle {self.interval()} tertutup terbaru belum tersedia atau basi "
                f"(open_time {int(siap[-1].open_time)}, seharusnya {int(terakhir_tutup)}; "
                "ditolak demi keamanan)"
            )
            self._simpan_kegagalan(
                symbol, alasan, gagal=True, ttl=ttl_gagal
            )
            raise RuntimeError(alasan)

        with self._lock:
            self._cache[symbol] = (int(siap[-1].open_time), siap)
            self._negative_cache.pop(symbol, None)
        return list(siap)

    def provider(self, symbol: str) -> list:
        return self.window_klines(symbol, state_mod.now_ms())


class DirectionCache(HtfKlineCache):

    def interval(self) -> str:
        return strategy.direction_interval(self._config)

    def window_bars(self) -> int:
        return strategy.direction_window_bars(self._config)

    def verdict(self, symbol: str, now_ms: int) -> dict:
        return strategy.evaluate_direction(
            self.window_klines(symbol, now_ms), self._config
        )


class HtfTrendCache(HtfKlineCache):

    def interval(self) -> str:
        return strategy.htf_interval(self._config)

    def window_bars(self) -> int:
        return strategy.htf_window_bars(self._config)

    def require_latest_closed_bar(self) -> bool:
        return True

    def negative_cache_ttl_seconds(self) -> float:
        try:
            interval = float(
                self._config.get("MARKET_SCAN_INTERVAL_SECONDS", 30) or 30
            )
        except (TypeError, ValueError):
            interval = 30.0
        if not math.isfinite(interval) or interval <= 0:
            interval = 30.0
        return max(30.0, min(300.0, interval))

    def verdict(self, symbol: str, now_ms: int) -> dict:
        return strategy.evaluate_htf(
            self.window_klines(symbol, now_ms), self._config
        )


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
    "symbol_block_until": {},
    "peak_equity": None,
    "peak_equity_all_time": None,
    "max_dd_pct": 0.0,
    "dd_reset_count": 0,
    "dd_stopped": False,
    "dd_stop_until": 0,
    "trade_accounting": None,
    "accounting_blocked": False,
    "accounting_block_reason": "",
    "accounting_retry_after_ms": 0,
    "accounting_pending_trades": [],
    "last_closed_trade": None,
    "pending_native_settlement": None,
    "_limit_close_done": False,
    "sell_fail_count": 0,
    "pending_order": None,
    "native_stop": None,
    "native_oco": None,
    "native_protection_retry_at": 0,
    "native_amend": None,
    "native_amend_last_ms": 0,
    "_native_stop_exit_blocked": False,
    "reconciliation_required": False,
    "reconciliation_assets": [],
    "reconciliation_reason": "",
    "state_integrity_error": "",
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
    Path(str(config["LOG_FILE"])).parent.mkdir(parents=True, exist_ok=True)
    file_handler = SafeRotatingFileHandler(
        config["LOG_FILE"], maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    setattr(file_handler, _LOGGING_MARKER, True)
    root.addHandler(file_handler)


def _handle_signal(signum, frame):
    global _shutdown_requested
    logger.info(
        "Menerima sinyal berhenti (%s). Bot akan berhenti setelah iterasi ini selesai.",
        signum,
    )
    _shutdown_requested = True
    _shutdown_event.set()


def load_pump_state(
    path: str,
    *,
    fail_closed: bool = False,
    allow_missing_state: bool = False,
    mode: str = "PAPER",
) -> dict:
    raw, valid, load_error = state_mod.load_state_checked(path)
    state_missing = load_error == state_mod.STATE_FILE_MISSING
    live = str(mode).strip().upper() == "LIVE"
    fresh_state = (
        fail_closed
        and state_missing
        and not valid
        and (not live or allow_missing_state)
    )
    merged = deepcopy(DEFAULT_STATE)
    merged.update(raw)
    merged["state_integrity_error"] = ""

    validation_errors: list[str] = []
    symbol = merged.get("current_symbol")
    if symbol is not None and (
        not isinstance(symbol, str)
        or not symbol
        or symbol != symbol.strip().upper()
        or not symbol.isalnum()
    ):
        validation_errors.append("current_symbol tidak valid")
    for key in (
        "entry_price",
        "qty",
        "entry_time",
        "last_scan_time",
        "cooldown_until",
        "last_trade_time",
        "native_protection_retry_at",
    ):
        value = merged.get(key, 0)
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            validation_errors.append(f"{key} bukan angka")
            continue
        if not math.isfinite(number) or number < 0:
            validation_errors.append(f"{key} tidak finite/nonnegatif")
        else:
            merged[key] = number
    for key in (
        "sl_pct",
        "tp_pct",
        "be_trigger_pct",
        "be_lock_pct",
        "trail_start_pct",
        "trail_step_pct",
        "be_stop_price",
        "trailing_stop_price",
        "dd_stop_until",
    ):
        value = merged.get(key, 0)
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            validation_errors.append(f"{key} bukan angka")
            continue
        if not math.isfinite(number) or number < 0:
            validation_errors.append(f"{key} tidak finite/nonnegatif")
        else:
            merged[key] = number
    for key in (
        "be_active",
        "trailing_active",
        "dd_stopped",
        "_limit_close_done",
        "_native_stop_exit_blocked",
        "reconciliation_required",
    ):
        if not isinstance(merged.get(key), bool):
            validation_errors.append(f"{key} harus boolean")
    for key in (
        "pending_order",
        "native_stop",
        "native_oco",
        "native_amend",
        "trade_accounting",
        "last_closed_trade",
        "pending_native_settlement",
    ):
        value = merged.get(key)
        if value is not None and not isinstance(value, dict):
            validation_errors.append(f"{key} harus object atau null")
    if not isinstance(merged.get("accounting_blocked"), bool):
        validation_errors.append("accounting_blocked harus boolean")
        merged["accounting_blocked"] = True
    if not isinstance(merged.get("accounting_block_reason"), str):
        validation_errors.append("accounting_block_reason harus string")
        merged["accounting_block_reason"] = "state accounting tidak valid"
    try:
        retry_ms = int(merged.get("accounting_retry_after_ms", 0) or 0)
        if retry_ms < 0:
            raise ValueError
        merged["accounting_retry_after_ms"] = retry_ms
    except (TypeError, ValueError, OverflowError):
        validation_errors.append("accounting_retry_after_ms tidak valid")
        merged["accounting_retry_after_ms"] = state_mod.now_ms() + 60_000
        merged["accounting_blocked"] = True
    pending_accounting = merged.get("accounting_pending_trades")
    if not isinstance(pending_accounting, list):
        validation_errors.append("accounting_pending_trades harus list")
        merged["accounting_pending_trades"] = []
        merged["accounting_blocked"] = True
    else:
        if pending_accounting:
            merged["accounting_blocked"] = True
        for index, item in enumerate(pending_accounting):
            if (
                not isinstance(item, dict)
                or not item.get("trade_id")
                or not isinstance(item.get("accounting"), dict)
            ):
                validation_errors.append(
                    f"accounting_pending_trades[{index}] tidak valid"
                )
                merged["accounting_blocked"] = True
                break
    active_accounting = merged.get("trade_accounting")
    if isinstance(active_accounting, dict):
        if not isinstance(active_accounting.get("orders", []), list):
            validation_errors.append("trade_accounting.orders harus list")
            merged["accounting_blocked"] = True
        accounting_status = str(active_accounting.get("status") or "").upper()
        if accounting_status not in ("VERIFIED", "UNVERIFIED", "LEGACY_UNVERIFIED"):
            validation_errors.append("trade_accounting.status tidak dikenal")
            merged["accounting_blocked"] = True
        elif accounting_status == "UNVERIFIED" or (
            active_accounting.get("fee_conversion_pending")
            or active_accounting.get("retryable_fill_details_pending")
        ):
            merged["accounting_blocked"] = True
            if not merged.get("accounting_retry_after_ms"):
                merged["accounting_retry_after_ms"] = state_mod.now_ms()
    if not isinstance(merged.get("reconciliation_assets"), list):
        validation_errors.append("reconciliation_assets harus list")
    blocks = merged.get("symbol_block_until")
    if not isinstance(blocks, dict):
        validation_errors.append("symbol_block_until harus object")
        merged["symbol_block_until"] = {}
    else:
        now = state_mod.now_ms()
        blocks_bersih: dict[str, float] = {}
        for sym, until in blocks.items():
            if (
                not isinstance(sym, str)
                or not sym
                or sym != sym.strip().upper()
                or not sym.isalnum()
            ):
                validation_errors.append(
                    f"symbol_block_until berisi simbol tidak valid: {sym!r}"
                )
                continue
            try:
                nilai = float(until or 0)
            except (TypeError, ValueError):
                validation_errors.append(f"symbol_block_until[{sym}] bukan angka")
                continue
            if not math.isfinite(nilai) or nilai < 0:
                validation_errors.append(
                    f"symbol_block_until[{sym}] tidak finite/nonnegatif"
                )
                continue
            if nilai <= now:
                continue
            blocks_bersih[sym] = nilai
        merged["symbol_block_until"] = blocks_bersih
    for key in ("peak_equity", "peak_equity_all_time", "max_dd_pct"):
        value = merged.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            validation_errors.append(f"{key} bukan angka/null")
            continue
        if not math.isfinite(number) or number < 0:
            validation_errors.append(f"{key} tidak finite/nonnegatif")
        else:
            merged[key] = number
    try:
        dd_reset_count = int(merged.get("dd_reset_count", 0) or 0)
        if dd_reset_count < 0:
            raise ValueError
        merged["dd_reset_count"] = dd_reset_count
    except (TypeError, ValueError):
        merged["dd_reset_count"] = 0
    try:
        sell_fail_count = int(merged.get("sell_fail_count", 0) or 0)
        if sell_fail_count < 0:
            raise ValueError
        merged["sell_fail_count"] = sell_fail_count
    except (TypeError, ValueError):
        validation_errors.append("sell_fail_count tidak valid")
    qty = float(merged.get("qty") or 0.0)
    entry = float(merged.get("entry_price") or 0.0)
    if symbol is None and (qty > 0 or entry > 0):
        validation_errors.append("qty/entry_price ada tanpa current_symbol")
    if symbol is not None and (qty <= 0 or entry <= 0):
        validation_errors.append("current_symbol ada tanpa qty/entry_price positif")

    if not valid and not fresh_state:
        validation_errors.append(load_error or "state tidak dapat dibaca")
    if fail_closed and validation_errors:
        reason = "; ".join(dict.fromkeys(validation_errors))
        merged["state_integrity_error"] = reason
        merged["reconciliation_required"] = True
        merged["reconciliation_reason"] = "STATE_INTEGRITY_UNVERIFIED"
        logger.critical(
            "Integritas state tidak dapat diverifikasi: %s. Entry baru diblokir "
            "sampai state diperbaiki, lalu dibaca ulang pada siklus berikutnya.",
            reason,
        )
    elif fresh_state:
        if live:
            logger.warning(
                "Operator mengonfirmasi inisialisasi state LIVE baru; peak equity dan "
                "posisi sebelumnya tidak dipulihkan. Rekonsiliasi tetap diwajibkan."
            )
        else:
            logger.info("File state belum ada; bot memulai state baru yang bersih.")
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


def _mark_reconciliation(
    state: dict, reason: str, assets: list[str] | None = None
) -> None:
    state["reconciliation_required"] = True
    state["reconciliation_reason"] = str(reason or "UNVERIFIED")
    state["reconciliation_assets"] = sorted({str(x) for x in (assets or []) if x})


def _clear_reconciliation(state: dict) -> None:
    if state.get("state_integrity_error"):
        _mark_reconciliation(state, "STATE_INTEGRITY_UNVERIFIED")
        return
    state["reconciliation_required"] = False
    state["reconciliation_reason"] = ""
    state["reconciliation_assets"] = []


def _known_open_order_client_ids(state: dict) -> set[str]:
    known: set[str] = set()
    pending = state.get("pending_order")
    if isinstance(pending, dict) and pending.get("client_order_id"):
        known.add(str(pending["client_order_id"]))
    native_stop = state.get("native_stop")
    if isinstance(native_stop, dict) and native_stop.get("client_order_id"):
        known.add(str(native_stop["client_order_id"]))
    native_oco = state.get("native_oco")
    if isinstance(native_oco, dict):
        for key in ("above", "below"):
            leg = native_oco.get(key)
            if isinstance(leg, dict) and leg.get("client_order_id"):
                known.add(str(leg["client_order_id"]))
    return known


def get_equity(
    client: ExchangeClient,
    config: dict,
    state: dict,
    position_price: float | None = None,
) -> "float | None":
    return collect_equity(client, config, state, position_price=position_price)["equity"]


def account_risk_gate(config: dict) -> "tuple[bool, str]":
    if str(config.get("MODE", "")).strip().upper() != "LIVE":
        return True, ""
    if config.get("USE_EQUITY_STOP"):
        return True, ""
    override = (
        str(os.environ.get("ALLOW_LIVE_WITHOUT_ACCOUNT_STOP", "")).strip().lower()
    )
    if override in ("1", "true", "yes", "on"):
        logger.critical(
            "ALLOW_LIVE_WITHOUT_ACCOUNT_STOP aktif: bot LIVE dijalankan TANPA rem "
            "drawdown atas permintaan eksplisit operator."
        )
        return True, ""
    return False, (
        "MODE=LIVE ditolak: USE_EQUITY_STOP nonaktif. "
        "Tidak ada rem drawdown tingkat akun, dan CLOSE_ALL_AT_LIMIT "
        "tidak akan pernah terpicu. Aktifkan USE_EQUITY_STOP melalui override "
        "konfigurasi, atau setel environment ALLOW_LIVE_WITHOUT_ACCOUNT_STOP=1 bila risiko ini memang "
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
        if not state.get("current_symbol") or float(state.get("qty", 0) or 0) <= 0:
            return (
                "ATR aktif untuk posisi berikutnya "
                f"(period {int(config.get('ATR_PERIOD', 14) or 14)}, "
                f"mult SL {float(config.get('ATR_MULT_SL', 0) or 0):.4g}, "
                f"mult TP {float(config.get('ATR_MULT_TP', 0) or 0):.4g})"
            )
        return (
            f"persen tetap untuk posisi lama (SL {sl_pct:.2f}%, TP {tp_pct:.2f}%). "
            "Posisi berikutnya memakai ATR karena USE_ATR_EXIT=True"
        )
    return f"persen tetap (SL {sl_pct:.2f}%, TP {tp_pct:.2f}%)"


def update_equity_controls(state: dict, equity: float, config: dict) -> bool:
    if state.get("peak_equity") is None or equity > state["peak_equity"]:
        state["peak_equity"] = equity
    all_time_peak = state.get("peak_equity_all_time")
    if all_time_peak is None or equity > float(all_time_peak or 0.0):
        state["peak_equity_all_time"] = equity

    peak = state["peak_equity"]
    dd_pct = (peak - equity) / peak * 100.0 if peak else 0.0
    dd_pct = max(dd_pct, 0.0)
    if dd_pct > float(state.get("max_dd_pct") or 0.0):
        state["max_dd_pct"] = dd_pct

    if (
        config["USE_EQUITY_STOP"]
        and not state.get("dd_stopped")
        and state["peak_equity"]
    ):
        if dd_pct >= config["MAX_DRAWDOWN_PERCENT"]:
            state["dd_stopped"] = True
            state["dd_stop_until"] = (
                state_mod.now_ms() + config["DD_COOLDOWN_HOURS"] * 3600 * 1000
            )
            state["dd_reset_count"] = int(state.get("dd_reset_count") or 0) + 1
            logger.critical(
                "STOP DRAWDOWN: turun %.2f%% dari puncak equity. Siklus risiko "
                "berikutnya dimulai dari saldo sekarang, riwayat drawdown tetap "
                "disimpan. Entry baru dijeda %d jam.",
                dd_pct,
                config["DD_COOLDOWN_HOURS"],
            )
            state["peak_equity"] = equity

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

    return bool(state.get("dd_stopped"))


def maybe_force_close_at_risk_limit(
    client: ExchangeClient,
    config: dict,
    filters_cache: dict,
    state: dict,
    entries_paused: bool,
    current_price,
) -> None:
    if state.get("_limit_close_done") and state.get("current_symbol"):
        state["_limit_close_done"] = False

    limit_now = bool(state.get("dd_stopped"))
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
        close_position(
            client,
            config,
            filters_cache,
            state,
            "RISK_LIMIT_TRIGGERED",
            reference_price=current_price,
        )
        state["_limit_close_done"] = (
            not state.get("current_symbol") and state.get("qty", 0.0) <= 0.0
        )

    if not entries_paused and state.get("_limit_close_done"):
        state["_limit_close_done"] = False


def reconcile_state_with_exchange(
    client: ExchangeClient,
    config: dict,
    state: dict,
    filters_cache: dict | None = None,
    *,
    check_open_orders: bool = False,
) -> bool:
    quote = config["QUOTE_ASSET"]
    live = str(config.get("MODE", "PAPER")).upper() == "LIVE"
    issues: list[str] = []
    issue_assets: set[str] = set()

    def issue(reason: str, *assets: str) -> None:
        issues.append(reason)
        issue_assets.update(str(asset) for asset in assets if asset)

    try:
        account = client.get_account()
        if live:
            _validate_live_account_snapshot(account, quote)
    except BinanceAPIError as exc:
        _mark_reconciliation(state, "ACCOUNT_UNVERIFIED", [quote])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "Rekonsiliasi gagal karena status/saldo akun tidak dapat diverifikasi: %s. "
            "State dipertahankan dan entry baru diblokir.",
            exc,
        )
        return False

    held_symbol = str(state.get("current_symbol") or "").upper()
    if held_symbol and float(state.get("qty") or 0.0) > 0:
        accounting = _ensure_trade_accounting(state, config, held_symbol)
        retry_after = int(state.get("accounting_retry_after_ms", 0) or 0)
        if (
            isinstance(accounting, dict)
            and (
                accounting.get("status") == "UNVERIFIED"
                or accounting.get("fee_conversion_pending")
                or accounting.get("retryable_fill_details_pending")
            )
            and state_mod.now_ms() >= retry_after
        ):
            _retry_order_fills(client, accounting)
            _refresh_accounting_state(
                client, config, state, actual_remaining_qty=float(state.get("qty") or 0.0)
            )
    _retry_pending_accounting(client, config, state)
    if live and held_symbol:
        held_filters = (filters_cache or {}).get(held_symbol)
        if held_filters is None:
            issue("HELD_SYMBOL_FILTERS_UNVERIFIED", held_symbol)
        elif not getattr(held_filters, "permission_metadata_verified", False):
            issue("HELD_SYMBOL_PERMISSIONS_UNVERIFIED", held_symbol)
        elif not permission_sets_allow(
            held_filters.permission_sets, _effective_account_permissions(account)
        ):
            issue("HELD_SYMBOL_PERMISSION_MISMATCH", held_symbol)

    pending = state.get("pending_order")
    if pending is not None and not isinstance(pending, dict):
        issue("PENDING_ORDER_MALFORMED")
    elif isinstance(pending, dict):
        client_order_id = pending.get("client_order_id")
        symbol_pending = str(pending.get("symbol") or "")
        if not client_order_id or not symbol_pending:
            issue("PENDING_ORDER_MALFORMED", symbol_pending)
        else:
            try:
                order = client.get_order(
                    symbol_pending, orig_client_order_id=str(client_order_id)
                )
            except BinanceAPIError as exc:
                age_ms = state_mod.now_ms() - int(pending.get("created_at") or 0)
                if _is_order_not_found(exc) and age_ms >= _NOT_FOUND_MIN_INTENT_AGE_MS:
                    logger.warning(
                        "Intent %s %s dipastikan tidak ditemukan setelah %.1f detik; intent dibersihkan.",
                        pending.get("side"),
                        symbol_pending,
                        age_ms / 1000.0,
                    )
                    state["pending_order"] = None
                else:
                    issue("PENDING_ORDER_UNVERIFIED", symbol_pending)
                    logger.critical(
                        "Intent order %s belum dapat diverifikasi (%s). Entry/order duplikat diblokir.",
                        symbol_pending,
                        exc,
                    )
            else:
                order_identity_valid = (
                    isinstance(order, dict)
                    and str(order.get("symbol") or "").upper() == symbol_pending.upper()
                    and str(order.get("clientOrderId") or "") == str(client_order_id)
                    and str(order.get("side") or "").upper()
                    == str(pending.get("side") or "").upper()
                    and str(order.get("type") or "").upper() == "MARKET"
                )
                if not order_identity_valid:
                    issue("PENDING_ORDER_IDENTITY_MISMATCH", symbol_pending)
                    logger.critical(
                        "Respons query intent %s tidak cocok dengan simbol/client order ID.",
                        symbol_pending,
                    )
                    order = {}
                side = str(pending.get("side") or order.get("side") or "").upper()
                status = str(order.get("status") or "").upper()
                executed_qty = float(order.get("executedQty", 0.0) or 0.0)
                terminal = _order_status_is_terminal(status)

                if side == "BUY" and executed_qty > 0 and terminal:
                    try:
                        refreshed_account = client.get_account()
                        if live:
                            _validate_live_account_snapshot(refreshed_account, quote)
                    except BinanceAPIError as exc:
                        issue("FILLED_BUY_BALANCE_UNVERIFIED", symbol_pending)
                        logger.critical(
                            "BUY %s terisi tetapi saldo setelah fill belum dapat diverifikasi: %s. "
                            "Intent dipertahankan.",
                            symbol_pending,
                            exc,
                        )
                    else:
                        account = refreshed_account
                        if _restore_pending_buy(client, config, state, pending, order, account):
                            logger.critical(
                                "BUY %s dipulihkan dari intent/order setelah respons hilang.",
                                symbol_pending,
                            )
                            state["pending_order"] = None
                        else:
                            issue("FILLED_BUY_STATE_UNRESTORABLE", symbol_pending)
                elif terminal:
                    if side == "SELL" and executed_qty > 0:
                        try:
                            refreshed_account = client.get_account()
                            if live:
                                _validate_live_account_snapshot(
                                    refreshed_account, quote
                                )
                        except BinanceAPIError as exc:
                            issue("FILLED_SELL_BALANCE_UNVERIFIED", symbol_pending)
                            logger.critical(
                                "SELL %s terminal tetapi saldo setelah fill belum dapat diverifikasi: %s. "
                                "Intent dipertahankan.",
                                symbol_pending,
                                exc,
                            )
                        else:
                            account = refreshed_account
                            if str(state.get("current_symbol") or "").upper() != symbol_pending.upper():
                                issue("FILLED_SELL_POSITION_IDENTITY_MISMATCH", symbol_pending)
                            else:
                                qty_before = max(0.0, float(state.get("qty") or 0.0))
                                base_pending = symbol_pending[: -len(quote)]
                                total_after = get_total_balance(account, base_pending)
                                remaining = min(
                                    max(0.0, qty_before - executed_qty), total_after
                                )
                                accounting = _ensure_trade_accounting(
                                    state, config, symbol_pending
                                )
                                if isinstance(accounting, dict):
                                    _attach_accounting_order(
                                        accounting,
                                        _accounting_order_record(
                                            client, order, symbol_pending, "SELL", quote
                                        ),
                                    )
                                state["qty"] = remaining
                                state["pending_order"] = None
                                _refresh_accounting_state(
                                    client,
                                    config,
                                    state,
                                    actual_remaining_qty=remaining,
                                )
                                if remaining <= 0:
                                    _finalize_trade_accounting(
                                        client,
                                        config,
                                        state,
                                        symbol_pending,
                                        str(pending.get("reason") or "RECOVERED_SELL"),
                                        remaining_qty=remaining,
                                    )
                                    reset_position(state)
                                    minutes = int(
                                        config.get("COOLDOWN_MINUTES_AFTER_CLOSE", 0) or 0
                                    )
                                    state["cooldown_until"] = (
                                        state_mod.now_ms() + minutes * 60 * 1000
                                    )
                                    state["last_trade_time"] = state_mod.now_ms()
                    else:
                        state["pending_order"] = None
                elif status in _NONTERMINAL_ORDER_STATUSES:
                    pending["last_status"] = status
                    pending["executed_qty"] = executed_qty
                    pending["cummulative_quote_qty"] = float(
                        order.get("cummulativeQuoteQty", 0.0) or 0.0
                    )
                    issue("PENDING_ORDER_NONTERMINAL", symbol_pending)
                else:
                    pending["last_status"] = status or "UNKNOWN"
                    issue("PENDING_ORDER_STATUS_UNKNOWN", symbol_pending)

    symbol = state.get("current_symbol")
    qty_state = float(state.get("qty") or 0.0)
    pending_unresolved = isinstance(state.get("pending_order"), dict)
    if symbol and qty_state > 0 and str(symbol).endswith(quote):
        base_asset = str(symbol)[: -len(quote)]
        total_base = get_total_balance(account, base_asset)
        free_base = get_balance(account, base_asset)
        native_unresolved = isinstance(state.get("native_oco"), dict) or isinstance(
            state.get("native_stop"), dict
        )
        if total_base <= 0 and not pending_unresolved and not native_unresolved:
            logger.warning(
                "REKONSILIASI: state memegang %s qty=%.8f tetapi saldo total %s nol. "
                "Posisi hantu direset berdasarkan saldo terverifikasi.",
                symbol,
                qty_state,
                base_asset,
            )
            _ensure_trade_accounting(state, config, str(symbol))
            _finalize_trade_accounting(
                client,
                config,
                state,
                str(symbol),
                "BALANCE_ZERO_UNTRACKED_EXIT",
                remaining_qty=0.0,
            )
            reset_position(state)
        elif total_base <= 0 and (pending_unresolved or native_unresolved):
            issue("POSITION_BALANCE_ZERO_WITH_PENDING", base_asset)
        elif total_base < qty_state:
            logger.warning(
                "REKONSILIASI: qty state %s %.8f lebih besar dari saldo total %.8f. "
                "Qty disesuaikan.",
                symbol,
                qty_state,
                total_base,
            )
            state["qty"] = total_base
            qty_state = total_base
            _refresh_accounting_state(
                client, config, state, actual_remaining_qty=total_base
            )

        expected_native_lock = not pending_unresolved and (
            isinstance(state.get("native_oco"), dict)
            or isinstance(state.get("native_stop"), dict)
        )
        if free_base <= 0 and total_base > 0 and not expected_native_lock:
            issue("BASE_BALANCE_LOCKED_BY_UNKNOWN_ORDER", base_asset)
    elif symbol or qty_state > 0:
        issue("POSITION_STATE_MALFORMED", str(symbol or ""))
    elif not state.get("pending_order"):
        foreign = []
        for bal in account.get("balances", []):
            asset = str(bal.get("asset") or "")
            if not asset or asset in (quote, "BNB"):
                continue
            amount = float(bal.get("free", 0.0)) + float(bal.get("locked", 0.0))
            if amount > 0:
                foreign.append(asset)
        if foreign:
            issue("UNMANAGED_BASE_ASSETS", *foreign)
            logger.critical(
                "State kosong tetapi akun memiliki aset base tidak terkelola: %s.",
                ", ".join(sorted(set(foreign))),
            )

    if live and check_open_orders:
        try:
            open_orders = client.get_open_orders()
            if not isinstance(open_orders, list):
                raise BinanceAPIError(502, None, "openOrders bukan list")
        except BinanceAPIError as exc:
            issue("OPEN_ORDERS_UNVERIFIED")
            logger.critical("Daftar open order LIVE tidak dapat diverifikasi: %s", exc)
        else:
            known_ids = _known_open_order_client_ids(state)
            unknown_orders = []
            for order in open_orders:
                if not isinstance(order, dict):
                    unknown_orders.append("<malformed>")
                    continue
                client_id = str(order.get("clientOrderId") or "")
                if not client_id or client_id not in known_ids:
                    unknown_orders.append(
                        f"{order.get('symbol') or '?'}:{client_id or '?'}"
                    )
            if unknown_orders:
                issue("UNMANAGED_OPEN_ORDERS", *unknown_orders)
                logger.critical(
                    "Ditemukan open order yang tidak tercatat di state: %s.",
                    ", ".join(unknown_orders),
                )

    symbol = state.get("current_symbol")
    if (
        _native_protection_enabled(config)
        and symbol
        and float(state.get("qty") or 0.0) > 0
        and not state.get("pending_order")
        and filters_cache is not None
    ):
        protection_ok = True
        if isinstance(state.get("native_oco"), dict):
            protection_ok = _reconcile_native_oco(client, config, state)
        if state.get("current_symbol") and isinstance(state.get("native_stop"), dict):
            protection_ok = (
                _reconcile_native_stop(client, config, state) and protection_ok
            )
        if (
            state.get("current_symbol")
            and not state.get("native_oco")
            and not state.get("native_stop")
            and not state.get("_native_stop_exit_blocked")
        ):
            protection_ok = (
                _ensure_native_protection(
                    client,
                    config,
                    filters_cache.get(str(state["current_symbol"])),
                    state,
                )
                and protection_ok
            )
        if not protection_ok or state.get("_native_stop_exit_blocked"):
            issue(
                "NATIVE_PROTECTION_UNVERIFIED",
                str(state.get("current_symbol") or symbol),
            )

    if isinstance(state.get("pending_order"), dict):
        issue(
            "PENDING_ORDER_UNRESOLVED",
            str((state.get("pending_order") or {}).get("symbol") or ""),
        )
    if state.get("state_integrity_error"):
        issue("STATE_INTEGRITY_UNVERIFIED")

    if issues:
        _mark_reconciliation(
            state,
            ";".join(dict.fromkeys(issues)),
            sorted(issue_assets),
        )
    else:
        _clear_reconciliation(state)
    state_mod.save_state(config["STATE_FILE"], state)
    return not issues


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
    state["native_amend"] = None
    state["_native_stop_exit_blocked"] = False
    state["trade_accounting"] = None


def _safe_accounting_number(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _accounting_order_record(
    client: ExchangeClient,
    response: dict,
    symbol: str,
    side: str,
    quote_asset: str,
) -> dict:
    side = str(side).upper()
    symbol = str(symbol).upper()
    order_id = response.get("orderId") if isinstance(response, dict) else None
    client_order_id = (
        str(response.get("clientOrderId") or "") if isinstance(response, dict) else ""
    )
    expected_qty = (
        max(0.0, _safe_accounting_number(response.get("executedQty"), 0.0))
        if isinstance(response, dict)
        else 0.0
    )
    expected_quote = (
        max(0.0, _safe_accounting_number(response.get("cummulativeQuoteQty"), 0.0))
        if isinstance(response, dict)
        else 0.0
    )
    time_value = (
        response.get("transactTime") or response.get("time")
        if isinstance(response, dict)
        else None
    )
    try:
        order_time_ms = int(time_value or state_mod.now_ms())
    except (TypeError, ValueError, OverflowError):
        order_time_ms = state_mod.now_ms()
    if order_id is not None and expected_qty > 0 and expected_quote <= 0:
        try:
            detailed = client.get_order(symbol, order_id=int(order_id))
        except Exception:
            detailed = None
        if (
            isinstance(detailed, dict)
            and str(detailed.get("symbol") or "").upper() == symbol
            and str(detailed.get("side") or "").upper() == side
            and str(detailed.get("orderId")) == str(order_id)
        ):
            expected_qty = max(
                0.0, _safe_accounting_number(detailed.get("executedQty"), expected_qty)
            )
            expected_quote = max(
                0.0,
                _safe_accounting_number(detailed.get("cummulativeQuoteQty"), 0.0),
            )
            if not isinstance(response.get("fills"), list) or not response.get("fills"):
                response = dict(response)
                response["fills"] = detailed.get("fills", [])
            if not response.get("transactTime") and not response.get("time"):
                order_time_ms = int(
                    _safe_accounting_number(
                        detailed.get("time") or detailed.get("transactTime"),
                        float(order_time_ms),
                    )
                )
    record = {
        "order_id": str(order_id or ""),
        "client_order_id": client_order_id,
        "side": side,
        "time_ms": order_time_ms,
        "expected_qty": expected_qty,
        "expected_quote": expected_quote,
        "fills": [],
        "fill_status": "UNVERIFIED",
        "accounting_error": "detail fill belum diverifikasi",
    }
    if expected_qty <= 0:
        record["fill_status"] = "VERIFIED"
        record["accounting_error"] = ""
        return record
    response_fills = response.get("fills") if isinstance(response, dict) else None
    candidates = [response_fills] if isinstance(response_fills, list) and response_fills else []
    last_error = record["accounting_error"]

    def _try_fills(raw_fills) -> bool:
        nonlocal last_error
        try:
            fills = normalize_fills(
                raw_fills,
                symbol=symbol,
                side=side,
                order_id=order_id,
                fallback_time_ms=order_time_ms,
            )
            if any(fill["symbol"] != symbol for fill in fills):
                raise TradeAccountingError("symbol fill tidak cocok dengan order")
            if any(fill["side"] != side for fill in fills):
                raise TradeAccountingError("side fill tidak cocok dengan order")
            if order_id is not None and any(
                fill["order_id"] and fill["order_id"] != str(order_id)
                for fill in fills
            ):
                raise TradeAccountingError("order ID fill tidak cocok")
            validate_fill_totals(fills, expected_qty, expected_quote)
        except (TradeAccountingError, TypeError, ValueError, ArithmeticError) as exc:
            last_error = str(exc)[:160]
            return False
        record["fills"] = fills
        record["fill_status"] = "VERIFIED"
        record["accounting_error"] = ""
        return True

    verified = any(_try_fills(raw_fills) for raw_fills in candidates)
    if not verified and order_id is not None:
        try:
            api_fills = client.get_my_trades(symbol, int(order_id))
        except Exception as exc:
            last_error = f"myTrades gagal: {str(exc)[:120]}"
        else:
            if isinstance(api_fills, list) and api_fills:
                _try_fills(api_fills)
    if record["fill_status"] != "VERIFIED":
        record["accounting_error"] = last_error or "detail fill tidak cocok dengan order"
    return record


def _new_trade_accounting(
    client: ExchangeClient,
    config: dict,
    response: dict,
    symbol: str,
    qty: float,
    entry_price: float,
    entry_time_ms: int,
) -> dict:
    order = _accounting_order_record(
        client, response, symbol, "BUY", str(config["QUOTE_ASSET"])
    )
    identity = "|".join(
        (
            str(symbol).upper(),
            order.get("order_id", ""),
            order.get("client_order_id", ""),
            str(int(entry_time_ms or 0)),
        )
    )
    trade_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    accounting = {
        "schema_version": 1,
        "trade_id": trade_id,
        "symbol": str(symbol).upper(),
        "quote_asset": str(config["QUOTE_ASSET"]).upper(),
        "mode": str(config.get("MODE", "PAPER")).upper(),
        "status": "VERIFIED" if order["fill_status"] == "VERIFIED" else "UNVERIFIED",
        "legacy": False,
        "opened_at_ms": int(entry_time_ms or state_mod.now_ms()),
        "entry_price": float(entry_price),
        "entry_client_order_id": order.get("client_order_id", ""),
        "entry_order_id": order.get("order_id", ""),
        "legacy_qty": 0.0,
        "legacy_cost_quote": 0.0,
        "orders": [order],
    }
    try:
        recompute_trade_accounting(
            accounting, client=client, actual_remaining_qty=float(qty)
        )
    except Exception as exc:
        accounting["status"] = "UNVERIFIED"
        accounting["accounting_error"] = str(exc)[:160] or type(exc).__name__
    return accounting


def _ensure_trade_accounting(
    state: dict, config: dict, symbol: str | None = None
) -> dict | None:
    active_symbol = str(symbol or state.get("current_symbol") or "").upper()
    qty = max(0.0, _safe_accounting_number(state.get("qty"), 0.0))
    if not active_symbol or qty <= 0:
        return None
    current = state.get("trade_accounting")
    if isinstance(current, dict) and str(current.get("symbol") or "").upper() == active_symbol:
        return current
    entry_price = max(0.0, _safe_accounting_number(state.get("entry_price"), 0.0))
    try:
        entry_time_ms = max(0, int(state.get("entry_time") or 0))
    except (TypeError, ValueError, OverflowError):
        entry_time_ms = 0
    identity = f"legacy|{active_symbol}|{entry_time_ms}|{entry_price:.16g}|{qty:.16g}"
    trade_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    accounting = {
        "schema_version": 1,
        "trade_id": trade_id,
        "symbol": active_symbol,
        "quote_asset": str(config["QUOTE_ASSET"]).upper(),
        "status": "LEGACY_UNVERIFIED",
        "legacy": True,
        "legacy_reason": "state lama tidak menyimpan entry order ID dan fill/komisi",
        "opened_at_ms": entry_time_ms,
        "entry_price": entry_price,
        "entry_client_order_id": "",
        "entry_order_id": "",
        "legacy_qty": qty,
        "legacy_cost_quote": entry_price * qty,
        "orders": [],
    }
    state["trade_accounting"] = accounting
    logger.warning(
        "Accounting posisi lama %s ditandai LEGACY_UNVERIFIED. Tidak ada pencocokan fuzzy dengan trade akun.",
        active_symbol,
    )
    return accounting


def _attach_accounting_order(accounting: dict, order_record: dict) -> None:
    orders = accounting.get("orders")
    if not isinstance(orders, list):
        orders = []
        accounting["orders"] = orders
    order_id = str(order_record.get("order_id") or "")
    client_order_id = str(order_record.get("client_order_id") or "")
    for index, old in enumerate(orders):
        if not isinstance(old, dict):
            continue
        same_id = order_id and str(old.get("order_id") or "") == order_id
        same_client_id = client_order_id and str(old.get("client_order_id") or "") == client_order_id
        if same_id or same_client_id:
            if old.get("fill_status") != "VERIFIED" or order_record.get("fill_status") == "VERIFIED":
                orders[index] = order_record
            return
    orders.append(order_record)


def _refresh_accounting_state(
    client: ExchangeClient,
    config: dict,
    state: dict,
    *,
    actual_remaining_qty: float | None = None,
) -> dict | None:
    accounting = state.get("trade_accounting")
    if not isinstance(accounting, dict):
        return None
    try:
        recompute_trade_accounting(
            accounting,
            client=client,
            actual_remaining_qty=actual_remaining_qty,
        )
    except Exception as exc:
        accounting["status"] = "LEGACY_UNVERIFIED" if accounting.get("legacy") else "UNVERIFIED"
        accounting["accounting_error"] = str(exc)[:160] or type(exc).__name__
    pending = state.get("accounting_pending_trades")
    pending = pending if isinstance(pending, list) else []
    settlement_pending = bool(
        accounting.get("fee_conversion_pending")
        or accounting.get("retryable_fill_details_pending")
    )
    if accounting.get("status") == "UNVERIFIED" or settlement_pending:
        state["accounting_blocked"] = True
        unpriced = accounting.get("unpriced_fees") or []
        unverified_orders = accounting.get("unverified_orders") or []
        detail = (
            unpriced[0].get("error")
            if unpriced and isinstance(unpriced[0], dict)
            else None
        ) or (
            unverified_orders[0].get("error")
            if unverified_orders and isinstance(unverified_orders[0], dict)
            else None
        )
        state["accounting_block_reason"] = str(
            accounting.get("accounting_error")
            or detail
            or "fill/komisi belum terverifikasi"
        )[:160]
        state["accounting_retry_after_ms"] = state_mod.now_ms() + 60_000
    elif pending:
        state["accounting_blocked"] = True
        state["accounting_block_reason"] = "ledger trade tertunda untuk verifikasi"
    else:
        state["accounting_blocked"] = False
        state["accounting_block_reason"] = ""
        state["accounting_retry_after_ms"] = 0
    return accounting


def _accounting_totals(accounting: dict) -> tuple[float, float, float, float, dict]:
    gross_buy_qty = gross_buy_quote = gross_sell_qty = gross_sell_quote = 0.0
    fees_by_asset: dict[str, float] = {}
    for order in accounting.get("orders", []):
        if not isinstance(order, dict):
            continue
        side = str(order.get("side") or "").upper()
        fills = order.get("fills") if isinstance(order.get("fills"), list) else []
        qty = sum(
            max(0.0, _safe_accounting_number(fill.get("qty"), 0.0))
            for fill in fills
            if isinstance(fill, dict)
        )
        quote = sum(
            max(0.0, _safe_accounting_number(fill.get("quote_qty"), 0.0))
            for fill in fills
            if isinstance(fill, dict)
        )
        if side == "BUY":
            gross_buy_qty += qty
            gross_buy_quote += quote
        elif side == "SELL":
            gross_sell_qty += qty
            gross_sell_quote += quote
        for fill in fills:
            if not isinstance(fill, dict):
                continue
            asset = str(fill.get("commission_asset") or "").upper()
            amount = max(0.0, _safe_accounting_number(fill.get("commission"), 0.0))
            if asset and amount > 0:
                fees_by_asset[asset] = fees_by_asset.get(asset, 0.0) + amount
    if accounting.get("legacy"):
        gross_buy_qty = max(0.0, _safe_accounting_number(accounting.get("legacy_qty"), 0.0))
        gross_buy_quote = max(0.0, _safe_accounting_number(accounting.get("legacy_cost_quote"), 0.0))
    return gross_buy_qty, gross_buy_quote, gross_sell_qty, gross_sell_quote, fees_by_asset


def _accounting_datetime(timestamp_ms: int) -> str | None:
    if timestamp_ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (OverflowError, OSError, ValueError):
        return None


def _build_trade_ledger_record(
    accounting: dict,
    *,
    closed_at_ms: int,
    reason: str,
    remaining_qty: float = 0.0,
    revision: int = 1,
) -> dict:
    status = str(accounting.get("status") or "UNVERIFIED").upper()
    net_pnl = (
        _safe_accounting_number(accounting.get("realized_pnl_quote"), 0.0)
        - _safe_accounting_number(accounting.get("remaining_cost_quote"), 0.0)
    )
    estimated = (
        _safe_accounting_number(accounting.get("gross_estimated_pnl_quote"), 0.0)
        - _safe_accounting_number(accounting.get("gross_remaining_cost_quote"), 0.0)
    )
    verified = status == "VERIFIED"
    pnl_value = net_pnl if verified else (estimated if status == "LEGACY_UNVERIFIED" else None)
    gross_buy_qty, gross_buy_quote, gross_sell_qty, gross_sell_quote, fees_by_asset = _accounting_totals(accounting)
    buy_price = (
        gross_buy_quote / gross_buy_qty
        if gross_buy_qty > 0
        else _safe_accounting_number(accounting.get("entry_price"), 0.0)
    )
    sell_price = gross_sell_quote / gross_sell_qty if gross_sell_qty > 0 else 0.0
    denominator = (
        _safe_accounting_number(accounting.get("entry_cost_quote"), 0.0)
        if verified
        else gross_buy_quote
    )
    pnl_pct = (float(pnl_value) / denominator * 100.0) if pnl_value is not None and denominator > 0 else None
    trade_id = str(accounting.get("trade_id") or "")
    note_parts = []
    unpriced_items = accounting.get("unpriced_fees")
    unpriced_items = unpriced_items if isinstance(unpriced_items, list) else []
    order_issues = accounting.get("unverified_orders")
    order_issues = order_issues if isinstance(order_issues, list) else []
    unpriced_note = (
        unpriced_items[0].get("error")
        if unpriced_items and isinstance(unpriced_items[0], dict)
        else ""
    )
    order_note = (
        order_issues[0].get("error")
        if order_issues and isinstance(order_issues[0], dict)
        else ""
    )
    for value in (
        accounting.get("legacy_reason"),
        accounting.get("accounting_error"),
        unpriced_note,
        order_note,
    ):
        text = str(value or "").strip()
        if text and text not in note_parts:
            note_parts.append(text)
    return {
        "trade_id": trade_id,
        "revision": int(revision),
        "symbol": str(accounting.get("symbol") or "").upper(),
        "quote_asset": str(accounting.get("quote_asset") or "").upper(),
        "opened_at_ms": int(accounting.get("opened_at_ms") or 0),
        "closed_at_ms": int(closed_at_ms),
        "buy_time": _accounting_datetime(int(accounting.get("opened_at_ms") or 0)),
        "sell_time": _accounting_datetime(int(closed_at_ms)),
        "buy_price": buy_price,
        "sell_price": sell_price,
        "entry": _safe_accounting_number(accounting.get("entry_price"), buy_price),
        "qty": gross_buy_qty,
        "pnl": pnl_value,
        "net_pnl_quote": net_pnl if verified else None,
        "estimated_pnl_quote": estimated,
        "pnl_pct": pnl_pct,
        "accounting_status": status,
        "accounting_note": "; ".join(note_parts),
        "reason": str(reason or ""),
        "paper": str(accounting.get("mode") or "PAPER").upper() == "PAPER",
        "fees_by_asset": fees_by_asset,
        "fee_quote_value": _safe_accounting_number(accounting.get("fee_quote_value_total"), 0.0),
        "fee_conversion_pending": bool(accounting.get("fee_conversion_pending")),
        "fill_details_pending": bool(accounting.get("fill_details_pending")),
        "unpriced_fees": deepcopy(accounting.get("unpriced_fees", [])),
        "orders": deepcopy(accounting.get("orders", [])),
        "remaining_qty_at_close": max(0.0, _safe_accounting_number(remaining_qty, 0.0)),
    }


def _finalize_trade_accounting(
    client: ExchangeClient,
    config: dict,
    state: dict,
    symbol: str,
    reason: str,
    *,
    remaining_qty: float = 0.0,
) -> dict | None:
    accounting = state.get("trade_accounting")
    if (
        not isinstance(accounting, dict)
        or str(accounting.get("symbol") or "").upper() != str(symbol).upper()
    ):
        accounting = _ensure_trade_accounting(state, config, symbol)
    if not isinstance(accounting, dict):
        return None
    accounting["mode"] = str(config.get("MODE", "PAPER")).upper()
    _refresh_accounting_state(
        client,
        config,
        state,
        actual_remaining_qty=max(0.0, _safe_accounting_number(remaining_qty, 0.0))
    )
    status = str(accounting.get("status") or "UNVERIFIED").upper()
    revision = 1
    pending_items = state.get("accounting_pending_trades")
    pending_items = pending_items if isinstance(pending_items, list) else []
    for item in pending_items:
        if isinstance(item, dict) and item.get("trade_id") == accounting.get("trade_id"):
            revision = int(item.get("revision", 1) or 1) + 1
            break
    record = _build_trade_ledger_record(
        accounting,
        closed_at_ms=state_mod.now_ms(),
        reason=reason,
        remaining_qty=remaining_qty,
        revision=revision,
    )
    write_ok = True
    try:
        append_trade_record(
            config.get("TRADE_LEDGER_FILE")
            or f"{config.get('STATE_FILE', 'data/pump_bot_state.json')}.trades.jsonl",
            record,
        )
    except Exception as exc:
        write_ok = False
        state["accounting_block_reason"] = f"ledger trade gagal disimpan: {str(exc)[:120]}"
        logger.critical("Ledger trade %s gagal disimpan: %s", symbol, exc)
    if status == "VERIFIED":
        pnl_for_block = float(record["net_pnl_quote"])
        note = "(PnL bersih dari fill terverifikasi)"
    else:
        fallback_basis = max(
            0.0,
            _safe_accounting_number(
                accounting.get("legacy_cost_quote")
                or accounting.get("entry_cost_quote"),
                0.0,
            ),
        )
        pnl_for_block = -max(fallback_basis, 1e-9)
        note = "(PnL tidak terverifikasi, diblokir konservatif sebagai loss)"
    record_symbol_block(state, config, symbol, pnl_for_block, catatan=note)
    state["last_closed_trade"] = {
        key: record.get(key)
        for key in (
            "trade_id",
            "symbol",
            "closed_at_ms",
            "pnl",
            "net_pnl_quote",
            "accounting_status",
        )
    }
    settlement_pending = bool(
        accounting.get("fee_conversion_pending")
        or accounting.get("retryable_fill_details_pending")
    )
    if status == "UNVERIFIED" or settlement_pending or not write_ok:
        pending_items.append(
            {
                "trade_id": str(accounting.get("trade_id") or record.get("trade_id")),
                "revision": revision,
                "record": record,
                "accounting": deepcopy(accounting),
                "reason": reason,
                "closed_at_ms": int(record["closed_at_ms"]),
                "remaining_qty": max(0.0, _safe_accounting_number(remaining_qty, 0.0)),
                "persistence_pending": not write_ok,
                "retry_after_ms": state_mod.now_ms() + 60_000,
            }
        )
        state["accounting_pending_trades"] = pending_items
        state["accounting_blocked"] = True
        state["accounting_block_reason"] = str(
            accounting.get("accounting_error")
            or state.get("accounting_block_reason")
            or "fill/komisi/ledger belum terverifikasi"
        )[:160]
        state["accounting_retry_after_ms"] = state_mod.now_ms() + 60_000
    else:
        state["accounting_pending_trades"] = pending_items
        state["accounting_blocked"] = bool(pending_items)
        if not pending_items:
            state["accounting_block_reason"] = ""
            state["accounting_retry_after_ms"] = 0
    state["trade_accounting"] = None
    return record


def _retry_order_fills(client: ExchangeClient, accounting: dict) -> bool:
    changed = False
    orders = accounting.get("orders")
    if not isinstance(orders, list):
        return False
    for index, old in enumerate(orders):
        if not isinstance(old, dict) or old.get("fill_status") == "VERIFIED":
            continue
        if not old.get("order_id") or _safe_accounting_number(old.get("expected_qty"), 0.0) <= 0:
            continue
        if old.get("fills"):
            try:
                existing_fills = normalize_fills(
                    old.get("fills"),
                    symbol=str(accounting.get("symbol") or ""),
                    side=str(old.get("side") or ""),
                    order_id=old.get("order_id"),
                    fallback_time_ms=old.get("time_ms"),
                )
                validate_fill_totals(
                    existing_fills,
                    old.get("expected_qty"),
                    old.get("expected_quote"),
                )
                continue
            except Exception:
                pass
        try:
            response = client.get_order(
                str(accounting.get("symbol") or ""), order_id=int(old["order_id"])
            )
        except Exception:
            continue
        if (
            not isinstance(response, dict)
            or str(response.get("symbol") or "").upper()
            != str(accounting.get("symbol") or "").upper()
            or str(response.get("side") or "").upper()
            != str(old.get("side") or "").upper()
            or str(response.get("orderId")) != str(old.get("order_id"))
        ):
            continue
        replacement = _accounting_order_record(
            client,
            response,
            str(accounting.get("symbol") or ""),
            str(old.get("side") or ""),
            str(accounting.get("quote_asset") or ""),
        )
        if replacement.get("fill_status") == "VERIFIED":
            orders[index] = replacement
            changed = True
    return changed


def _retry_pending_accounting(client: ExchangeClient, config: dict, state: dict) -> None:
    pending = state.get("accounting_pending_trades")
    if not isinstance(pending, list) or not pending:
        return
    now = state_mod.now_ms()
    retry_after = int(state.get("accounting_retry_after_ms", 0) or 0)
    if now < retry_after:
        return
    remaining_pending = []
    for item in pending:
        if not isinstance(item, dict):
            continue
        accounting = item.get("accounting")
        if not isinstance(accounting, dict):
            remaining_pending.append(item)
            continue
        _retry_order_fills(client, accounting)
        try:
            recompute_trade_accounting(
                accounting,
                client=client,
                actual_remaining_qty=max(0.0, _safe_accounting_number(item.get("remaining_qty"), 0.0)),
            )
        except Exception as exc:
            accounting["status"] = "UNVERIFIED"
            accounting["accounting_error"] = str(exc)[:160] or type(exc).__name__
        resolved_legacy = (
            accounting.get("status") == "LEGACY_UNVERIFIED"
            and not accounting.get("fee_conversion_pending")
            and not accounting.get("retryable_fill_details_pending")
        )
        if accounting.get("status") == "VERIFIED" or resolved_legacy:
            revision = int(item.get("revision", 1) or 1) + 1
            record = _build_trade_ledger_record(
                accounting,
                closed_at_ms=int(item.get("closed_at_ms") or now),
                reason=str(item.get("reason") or ""),
                remaining_qty=max(0.0, _safe_accounting_number(item.get("remaining_qty"), 0.0)),
                revision=revision,
            )
            try:
                append_trade_record(
                    config.get("TRADE_LEDGER_FILE")
                    or f"{config.get('STATE_FILE', 'data/pump_bot_state.json')}.trades.jsonl",
                    record,
                )
            except Exception as exc:
                item["retry_after_ms"] = now + 60_000
                item["accounting"] = accounting
                item["record"] = record
                item["revision"] = revision
                item["accounting_error"] = f"ledger gagal disimpan: {str(exc)[:120]}"
                remaining_pending.append(item)
                continue
            state["last_closed_trade"] = {
                key: record.get(key)
                for key in (
                    "trade_id",
                    "symbol",
                    "closed_at_ms",
                    "pnl",
                    "net_pnl_quote",
                    "accounting_status",
                )
            }
        else:
            item["retry_after_ms"] = now + 60_000
            item["accounting"] = accounting
            unpriced_items = accounting.get("unpriced_fees")
            unpriced_items = unpriced_items if isinstance(unpriced_items, list) else []
            unverified_orders = accounting.get("unverified_orders")
            unverified_orders = unverified_orders if isinstance(unverified_orders, list) else []
            detail = (
                unpriced_items[0].get("error")
                if unpriced_items and isinstance(unpriced_items[0], dict)
                else None
            ) or (
                unverified_orders[0].get("error")
                if unverified_orders and isinstance(unverified_orders[0], dict)
                else None
            )
            item["accounting_error"] = str(
                accounting.get("accounting_error")
                or detail
                or "fill/komisi belum terverifikasi"
            )[:160]
            remaining_pending.append(item)
    state["accounting_pending_trades"] = remaining_pending
    active = state.get("trade_accounting")
    active_unverified = isinstance(active, dict) and (
        active.get("status") == "UNVERIFIED"
        or active.get("fee_conversion_pending")
        or active.get("retryable_fill_details_pending")
    )
    state["accounting_blocked"] = bool(remaining_pending or active_unverified)
    if remaining_pending:
        state["accounting_block_reason"] = str(
            remaining_pending[0].get("accounting_error") or "ledger trade tertunda"
        )[:160]
        state["accounting_retry_after_ms"] = now + 60_000
    elif not active_unverified:
        state["accounting_block_reason"] = ""
        state["accounting_retry_after_ms"] = 0


def _fmt_utc_ms(ts_ms: float) -> str:
    try:
        return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except (OverflowError, OSError, ValueError):
        return str(ts_ms)


def _symbol_block_until_ts(state: dict, symbol: str) -> float:
    blocks = state.get("symbol_block_until")
    if not isinstance(blocks, dict) or not symbol:
        return 0.0
    try:
        return max(0.0, float(blocks.get(str(symbol).upper(), 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def _safe_block_ts(value) -> float:
    try:
        nilai = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return nilai if math.isfinite(nilai) else 0.0


def record_symbol_block(
    state: dict,
    config: dict,
    symbol: str,
    pnl: float,
    *,
    now_ms: int | None = None,
    catatan: str = "",
) -> bool:
    if not symbol:
        return False
    try:
        hours = float(config.get("SAME_COIN_BLOCK_HOURS", 0) or 0)
    except (TypeError, ValueError):
        hours = 0.0
    if not math.isfinite(hours) or hours <= 0:
        return False
    try:
        hasil = float(pnl)
    except (TypeError, ValueError):
        hasil = float("-inf")
    if not math.isfinite(hasil):
        hasil = -1.0 if hasil < 0 else 1.0
    if bool(config.get("SAME_COIN_BLOCK_LOSS_ONLY", True)) and hasil >= 0:
        return False
    now = state_mod.now_ms() if now_ms is None else int(now_ms)
    blocks = state.get("symbol_block_until")
    if not isinstance(blocks, dict):
        blocks = {}
    else:
        blocks = dict(blocks)
    for sym in [s for s, until in blocks.items() if _safe_block_ts(until) <= now]:
        blocks.pop(sym, None)
    sym = str(symbol).upper()
    blocks[sym] = now + int(hours * 3600 * 1000)
    state["symbol_block_until"] = blocks
    logger.info(
        "Blokir entry %s sampai %s (%.1f jam) karena hasil accounting trade sebelumnya loss "
        "(PnL %+.2f %s)%s.",
        sym,
        _fmt_utc_ms(blocks[sym]),
        hours,
        hasil,
        config.get("QUOTE_ASSET", "USDT"),
        f" {catatan}" if catatan else "",
    )
    return True


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
        logger.warning(
            "Dust sweep: gagal ambil daftar aset convertible (%s). Dilewati, dicoba lagi nanti.",
            exc,
        )
        return

    details = convertible.get("details") if isinstance(convertible, dict) else None
    if not isinstance(details, list) or any(
        not isinstance(item, dict) for item in details
    ):
        logger.warning(
            "Dust sweep: respons daftar convertible tidak valid. Dilewati fail-closed."
        )
        return
    match = next((d for d in details if d.get("asset") == base_asset), None)
    if not match:
        logger.info(
            "Dust sweep: %s tidak (lagi) terdaftar sebagai dust convertible saat ini, dilewati.",
            base_asset,
        )
        return

    try:
        result = client.convert_dust([base_asset])
    except BinanceAPIError as exc:
        logger.warning(
            "Dust sweep %s -> BNB gagal (%s). Sisa saldo dibiarkan, dicoba lagi di kesempatan berikutnya.",
            base_asset,
            exc,
        )
        return

    if (
        not isinstance(result, dict)
        or "totalTransfered" not in result
        or not isinstance(result.get("transferResult"), list)
    ):
        logger.critical(
            "Dust sweep %s mengembalikan respons yang tidak dapat diverifikasi. "
            "Jangan mengulang konversi secara buta; saldo akan diperiksa saat rekonsiliasi.",
            base_asset,
        )
        return
    transferred = result["totalTransfered"]
    logger.info(
        "DUST SWEEP OK: sisa %s dikonversi ke %s BNB (sudah dikurangi biaya layanan Binance).",
        base_asset,
        transferred,
    )


def _new_client_order_id(prefix: str) -> str:
    return f"pump-{prefix}-{uuid.uuid4().hex[:24]}"


def _submit_market_order(
    client: ExchangeClient,
    symbol: str,
    side: str,
    quantity: float | None,
    client_order_id: str,
    quote_order_qty: float | None = None,
    quote_precision: int | None = None,
) -> dict:
    return client.new_market_order(
        symbol,
        side,
        quantity=quantity,
        quote_order_qty=quote_order_qty,
        new_client_order_id=client_order_id,
        quote_precision=quote_precision,
    )


_TERMINAL_ORDER_STATUSES = frozenset(
    {
        "FILLED",
        "EXPIRED",
        "EXPIRED_IN_MATCH",
        "CANCELED",
        "REJECTED",
    }
)
_NONTERMINAL_ORDER_STATUSES = frozenset(
    {
        "NEW",
        "PARTIALLY_FILLED",
    }
)


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


def _exit_distance(
    state: dict,
    config: dict,
    state_key: str,
    cfg_key: str,
    atr_mode: bool,
    entry: float,
) -> float:
    locked = abs(float(state.get(state_key) or 0.0))
    if locked > 0:
        return locked
    pct = abs(float(config.get(cfg_key, 0.0) or 0.0))
    return entry * pct / 100.0 if atr_mode else pct


def _protection_stop_price(
    state: dict, config: dict, filters: SymbolFilters | None = None
) -> float:
    entry = float(state.get("entry_price") or 0.0)
    if entry <= 0:
        return 0.0
    atr_mode = str(state.get("exit_source", "")).upper() == "ATR"
    sl = abs(float(_exit_distance(state, config, "sl_pct", "SL_PCT", atr_mode, entry)))
    level = entry - sl if atr_mode else entry * (1.0 - sl / 100.0)
    if state.get("be_active"):
        level = max(level, float(state.get("be_stop_price") or 0.0))
    if state.get("trailing_active"):
        level = max(level, float(state.get("trailing_stop_price") or 0.0))
    if level <= 0:
        return 0.0
    if filters is not None:
        level = filters.round_price(level)
    return level


def _native_oco_levels(
    state: dict, filters: SymbolFilters | None, config: dict
) -> dict:
    entry = float(state.get("entry_price") or 0.0)
    atr_mode = str(state.get("exit_source", "")).upper() == "ATR"
    tp = _exit_distance(state, config, "tp_pct", "TP_PCT", atr_mode, entry)
    raw_sl = _protection_stop_price(state, config)
    raw_tp = entry + tp if atr_mode else entry * (1.0 + tp / 100.0)
    buffer_pct = max(
        0.01, float(config.get("NATIVE_OCO_LIMIT_BUFFER_PCT", 0.10) or 0.10)
    )
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


def _native_stop_price(
    state: dict, filters: SymbolFilters | None, config: dict | None = None
) -> float:
    return _protection_stop_price(state, config or {}, filters)


_DEFINITIVE_REJECT_CODES = {
    -1013,
    -1021,
    -1100,
    -1101,
    -1102,
    -1103,
    -1104,
    -1106,
    -1111,
    -1121,
    -2010,
    -1116,
    -1020,
}
_NOT_FOUND_CODES = {-2013}
_NOT_FOUND_MIN_INTENT_AGE_MS = 60_000


def _is_duplicate_client_id_error(exc: BinanceAPIError) -> bool:
    message = str(getattr(exc, "msg", "") or "").lower()
    return getattr(exc, "code", None) == -2010 and "duplicate" in message


def _is_definitive_reject(exc: BinanceAPIError) -> bool:
    if isinstance(exc, BinanceRateLimitError) or _is_duplicate_client_id_error(exc):
        return False
    return getattr(exc, "code", None) in _DEFINITIVE_REJECT_CODES


def _is_order_not_found(exc: BinanceAPIError) -> bool:
    if getattr(exc, "code", None) in _NOT_FOUND_CODES:
        return True
    return "does not exist" in str(getattr(exc, "msg", "") or "").lower()


def _arm_native_oco(
    client: ExchangeClient, config: dict, filters: SymbolFilters | None, state: dict
) -> bool:
    if not _native_oco_enabled(config):
        return True
    symbol = state.get("current_symbol")
    qty = float(state.get("qty") or 0.0)
    entry = float(state.get("entry_price") or 0.0)
    if not symbol or qty <= 0 or entry <= 0:
        return False
    now = state_mod.now_ms()
    if now < int(state.get("native_protection_retry_at", 0) or 0):
        return False
    if filters is None:
        _mark_reconciliation(state, "NATIVE_OCO_FILTERS_UNVERIFIED", [symbol])
        state["native_protection_retry_at"] = now + 60_000
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    qty = filters.round_limit_qty(qty)
    oco_types_supported = {"TAKE_PROFIT_LIMIT", "STOP_LOSS_LIMIT"}.issubset(
        filters.order_types
    )
    if not filters.limit_qty_valid(qty) or not oco_types_supported:
        _mark_reconciliation(state, "NATIVE_OCO_QTY_OR_TYPE_INVALID", [symbol])
        state["native_protection_retry_at"] = now + 60_000
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    levels = _native_oco_levels(state, filters, config)
    bid = _executable_bid(client, symbol)
    above_price = float(levels["above_price"])
    above_stop = float(levels["above_stop_price"])
    below_price = float(levels["below_price"])
    below_stop = float(levels["below_stop_price"])
    limit_min = float(filters.limit_min_notional)
    limit_max = float(filters.limit_max_notional)
    prices = (below_price, below_stop, above_price, above_stop)
    stop_boleh_di_atas_entry = bool(
        state.get("be_active") or state.get("trailing_active")
    )
    valid = (
        bid is not None
        and all(filters.price_valid(value) for value in prices)
        and below_price < below_stop < bid < above_price <= above_stop
        and (below_stop < entry or stop_boleh_di_atas_entry)
        and entry < above_stop
        and (limit_min <= 0 or qty * below_price >= limit_min)
        and (limit_min <= 0 or qty * above_price >= limit_min)
        and (limit_max <= 0 or qty * below_price <= limit_max)
        and (limit_max <= 0 or qty * above_price <= limit_max)
    )
    if not valid:
        state["native_protection_retry_at"] = now + 60_000
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        logger.critical(
            "OCO native %s tidak dipasang: relasi harga invalid "
            "belowLimit=%.12g belowStop=%.12g bid=%.12g aboveLimit=%.12g aboveStop=%.12g.",
            symbol,
            below_price,
            below_stop,
            bid or 0.0,
            above_price,
            above_stop,
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
            symbol,
            quantity=qty,
            above_price=above_price,
            above_stop_price=above_stop,
            below_price=below_price,
            below_stop_price=below_stop,
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
        logger.critical(
            "OCO native %s tidak didukung client: %s. Fallback stop akan dicoba.",
            symbol,
            exc,
        )
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
                symbol,
                getattr(exc, "code", None),
                exc,
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
            symbol,
            exc,
        )
        return False

    intent = state.get("native_oco")
    if not isinstance(intent, dict):
        state["_native_stop_exit_blocked"] = True
        _mark_reconciliation(state, "NATIVE_OCO_INTENT_LOST", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    orders = []
    order_reports = []
    if isinstance(response, dict):
        orders = [x for x in response.get("orders", []) if isinstance(x, dict)]
        order_reports = [
            x for x in response.get("orderReports", []) if isinstance(x, dict)
        ]
    order_ids = {str(x.get("clientOrderId") or "") for x in orders}
    report_by_id = {str(x.get("clientOrderId") or ""): x for x in order_reports}
    above_report = report_by_id.get(above_client_order_id)
    below_report = report_by_id.get(below_client_order_id)

    def report_matches(
        report: dict | None,
        *,
        expected_type: str,
        expected_price: float,
        expected_stop: float,
    ) -> bool:
        if not isinstance(report, dict):
            return False
        try:
            numeric_ok = (
                Decimal(str(report.get("origQty"))) == Decimal(str(qty))
                and Decimal(str(report.get("price"))) == Decimal(str(expected_price))
                and Decimal(str(report.get("stopPrice"))) == Decimal(str(expected_stop))
            )
        except (ArithmeticError, ValueError, TypeError):
            return False
        return (
            str(report.get("symbol") or "").upper() == str(symbol).upper()
            and str(report.get("side") or "").upper() == "SELL"
            and str(report.get("type") or "").upper() == expected_type
            and str(report.get("status") or "").upper() == "NEW"
            and numeric_ok
        )

    response_status = str(
        response.get("listOrderStatus") if isinstance(response, dict) else ""
    ).upper()
    response_valid = (
        isinstance(response, dict)
        and response.get("orderListId") is not None
        and str(response.get("symbol") or "").upper() == str(symbol).upper()
        and str(response.get("contingencyType") or "").upper() == "OCO"
        and str(response.get("listClientOrderId") or "") == list_client_order_id
        and str(response.get("listStatusType") or "").upper() == "EXEC_STARTED"
        and response_status == "EXECUTING"
        and above_client_order_id in order_ids
        and below_client_order_id in order_ids
        and report_matches(
            above_report,
            expected_type="TAKE_PROFIT_LIMIT",
            expected_price=above_price,
            expected_stop=above_stop,
        )
        and report_matches(
            below_report,
            expected_type="STOP_LOSS_LIMIT",
            expected_price=below_price,
            expected_stop=below_stop,
        )
    )
    if not response_valid:
        intent["status"] = "UNKNOWN"
        intent["last_error"] = "respons pemasangan OCO tidak lengkap/tidak konsisten"
        state["_native_stop_exit_blocked"] = True
        _mark_reconciliation(state, "NATIVE_OCO_RESPONSE_UNVERIFIED", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "Respons pemasangan OCO %s tidak dapat diverifikasi: %s", symbol, response
        )
        return False
    intent["order_list_id"] = response.get("orderListId")
    intent["list_status_type"] = str(
        response.get("listStatusType") or "EXEC_STARTED"
    ).upper()
    intent["status"] = response_status
    for leg_name, _client_key in (
        ("above", "aboveClientOrderId"),
        ("below", "belowClientOrderId"),
    ):
        leg = intent.get(leg_name)
        if not isinstance(leg, dict):
            continue
        report = next(
            (
                x
                for x in response.get("orders", [])
                if x.get("clientOrderId") == leg.get("client_order_id")
            ),
            None,
        )
        if report is None:
            report = next(
                (
                    x
                    for x in response.get("orderReports", [])
                    if x.get("clientOrderId") == leg.get("client_order_id")
                ),
                None,
            )
        if isinstance(report, dict):
            leg["order_id"] = report.get("orderId")
    state["_native_stop_exit_blocked"] = False
    state["native_protection_retry_at"] = 0
    if intent["status"] in ("ALL_DONE", "REJECT"):
        state["_native_stop_exit_blocked"] = True
        _mark_reconciliation(state, "NATIVE_OCO_TERMINAL_ON_CREATE", [symbol])
    else:
        _clear_reconciliation(state)
    state_mod.save_state(config["STATE_FILE"], state)
    logger.info(
        "OCO native aktif %s: TP_LIMIT %.12g/trigger %.12g, SL_LIMIT %.12g/trigger %.12g, list=%s",
        symbol,
        above_price,
        above_stop,
        below_price,
        below_stop,
        list_client_order_id,
    )
    return intent["status"] not in ("ALL_DONE", "REJECT")


def _arm_native_stop(
    client: ExchangeClient, config: dict, filters: SymbolFilters | None, state: dict
) -> bool:
    if not _native_stop_enabled(config):
        return True
    state["_native_stop_exit_blocked"] = False
    symbol = state.get("current_symbol")
    qty = float(state.get("qty") or 0.0)
    entry = float(state.get("entry_price") or 0.0)
    if not symbol or qty <= 0 or entry <= 0:
        return False
    now = state_mod.now_ms()
    if now < int(state.get("native_protection_retry_at", 0) or 0):
        return False
    if filters is None:
        state["native_protection_retry_at"] = now + 60_000
        _mark_reconciliation(state, "NATIVE_STOP_FILTERS_UNVERIFIED", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    qty = filters.round_qty(qty)
    bid = _executable_bid(client, symbol)
    stop_price = _native_stop_price(state, filters, config)
    stop_boleh_di_atas_entry = bool(
        state.get("be_active") or state.get("trailing_active")
    )
    valid = (
        bid is not None
        and "STOP_LOSS" in filters.order_types
        and filters.market_qty_valid(qty)
        and filters.price_valid(stop_price)
        and 0 < stop_price
        and (stop_price < entry or stop_boleh_di_atas_entry)
        and stop_price < bid
        and (
            filters.min_notional <= 0
            or Decimal(str(qty * stop_price)) >= filters.min_notional
        )
        and (
            filters.max_notional <= 0 or Decimal(str(qty * bid)) <= filters.max_notional
        )
    )
    if not valid:
        state["native_protection_retry_at"] = now + 60_000
        _mark_reconciliation(state, "NATIVE_STOP_FILTER_OR_PRICE_INVALID", [symbol])
        logger.critical(
            "Proteksi native %s tidak dipasang: qty/stopPrice/bid/filter tidak valid "
            "(qty=%.12g stopPrice=%.12g entry=%.12g bid=%.12g).",
            symbol,
            qty,
            stop_price,
            entry,
            bid or 0.0,
        )
        state_mod.save_state(config["STATE_FILE"], state)
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
            symbol,
            quantity=qty,
            stop_price=stop_price,
            new_client_order_id=client_order_id,
        )
    except (BinanceAPIError, NotImplementedError) as exc:
        intent = state.get("native_stop")
        definitive = isinstance(exc, NotImplementedError) or (
            isinstance(exc, BinanceAPIError) and _is_definitive_reject(exc)
        )
        if isinstance(intent, dict):
            intent["status"] = "FAILED" if definitive else "UNKNOWN"
            intent["last_error"] = str(exc)
        state["native_protection_retry_at"] = now + 60_000
        state["_native_stop_exit_blocked"] = not definitive
        _mark_reconciliation(state, "NATIVE_STOP_SUBMISSION_UNVERIFIED", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "Proteksi native %s gagal/status tidak pasti: %s. "
            "Local stop tetap aktif, entry baru diblokir, dan ID %s harus direkonsiliasi.",
            symbol,
            exc,
            client_order_id,
        )
        return False

    intent = state.get("native_stop")
    if not isinstance(intent, dict):
        state["_native_stop_exit_blocked"] = True
        _mark_reconciliation(state, "NATIVE_STOP_INTENT_LOST", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    response_status = str(
        response.get("status") if isinstance(response, dict) else ""
    ).upper()
    try:
        numeric_response_valid = Decimal(str(response.get("origQty"))) == Decimal(
            str(qty)
        ) and Decimal(str(response.get("stopPrice"))) == Decimal(str(stop_price))
    except (AttributeError, ArithmeticError, TypeError, ValueError):
        numeric_response_valid = False
    response_valid = (
        isinstance(response, dict)
        and response.get("orderId") is not None
        and str(response.get("symbol") or "").upper() == str(symbol).upper()
        and str(response.get("clientOrderId") or "") == client_order_id
        and str(response.get("side") or "").upper() == "SELL"
        and str(response.get("type") or "").upper() == "STOP_LOSS"
        and response_status == "NEW"
        and numeric_response_valid
    )
    if not response_valid:
        intent["status"] = "UNKNOWN"
        intent["last_error"] = (
            "respons pemasangan native stop tidak lengkap/tidak konsisten"
        )
        state["_native_stop_exit_blocked"] = True
        _mark_reconciliation(state, "NATIVE_STOP_RESPONSE_UNVERIFIED", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "Respons pemasangan native stop %s tidak dapat diverifikasi: %s",
            symbol,
            response,
        )
        return False
    intent["order_id"] = response.get("orderId")
    intent["status"] = response_status
    intent["last_response"] = {
        key: response.get(key)
        for key in ("orderId", "clientOrderId", "status")
        if key in response
    }
    state["_native_stop_exit_blocked"] = False
    state["native_protection_retry_at"] = 0
    _clear_reconciliation(state)
    state_mod.save_state(config["STATE_FILE"], state)
    logger.info(
        "Proteksi native aktif %s: STOP_LOSS stopPrice=%.12g qty=%.12g id=%s",
        symbol,
        stop_price,
        qty,
        client_order_id,
    )
    return True


def _ensure_native_protection(
    client: ExchangeClient, config: dict, filters: SymbolFilters | None, state: dict
) -> bool:
    if not _native_protection_enabled(config):
        return True
    if isinstance(state.get("native_oco"), dict) or isinstance(
        state.get("native_stop"), dict
    ):
        return True
    if _native_oco_enabled(config):
        if state_mod.now_ms() < int(state.get("native_protection_retry_at", 0) or 0):
            return False
        if _arm_native_oco(client, config, filters, state):
            return True
        oco_status = (state.get("native_oco") or {}).get("status")
        if oco_status in (None, "FAILED") and _native_stop_enabled(config):
            state["native_oco"] = None
            state["native_protection_retry_at"] = 0
            return _arm_native_stop(client, config, filters, state)
        return False
    return _arm_native_stop(client, config, filters, state)


_NATIVE_AMEND_MIN_INTERVAL_MS = 60_000
_NATIVE_AMEND_RETRY_MS = 5_000


def _native_protected_stop_level(state: dict) -> float:
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


def _native_ready_for_amend(state: dict) -> bool:
    oco = state.get("native_oco")
    if isinstance(oco, dict):
        return str(oco.get("status") or "").upper() in ("EXECUTING", "PENDING")
    stop = state.get("native_stop")
    if isinstance(stop, dict):
        return str(stop.get("status") or "").upper() == "NEW"
    return False


def _cancel_native_for_amend(
    client: ExchangeClient, config: dict, state: dict, target: str
) -> bool:
    symbol = state.get("current_symbol")
    if not symbol:
        return False
    if target == "OCO":
        if not _reconcile_native_oco(client, config, state):
            return False
        intent = state.get("native_oco")
        if not isinstance(intent, dict):
            return False
        try:
            response = client.cancel_order_list(
                symbol,
                order_list_id=(
                    int(intent["order_list_id"])
                    if intent.get("order_list_id") is not None
                    else None
                ),
                list_client_order_id=(
                    None
                    if intent.get("order_list_id") is not None
                    else intent.get("list_client_order_id")
                ),
            )
        except BinanceAPIError as exc:
            if _is_order_not_found(exc):
                state["native_oco"] = None
                state["_native_stop_exit_blocked"] = False
                _mark_reconciliation(state, "NATIVE_AMEND_CANCEL_NOT_FOUND", [symbol])
                return True
            state["_native_stop_exit_blocked"] = True
            _mark_reconciliation(state, "NATIVE_AMEND_CANCEL_UNVERIFIED", [symbol])
            state["native_protection_retry_at"] = state_mod.now_ms() + _NATIVE_AMEND_RETRY_MS
            logger.critical(
                "Amend OCO %s ditunda: pembatalan tidak terverifikasi (%s). SELL manual ditahan.",
                symbol,
                exc,
            )
            return False
        identity_ok = (
            isinstance(response, dict)
            and str(response.get("symbol") or "").upper() == str(symbol).upper()
            and str(response.get("listClientOrderId") or "")
            == str(intent.get("list_client_order_id") or "")
            and (
                intent.get("order_list_id") is None
                or str(response.get("orderListId")) == str(intent.get("order_list_id"))
            )
        )
        if not identity_ok:
            state["_native_stop_exit_blocked"] = True
            _mark_reconciliation(state, "NATIVE_AMEND_CANCEL_IDENTITY_MISMATCH", [symbol])
            return False
        if _oco_response_has_fill(response):
            state["_native_stop_exit_blocked"] = True
            _mark_reconciliation(state, "NATIVE_AMEND_OCO_FILLED_ON_CANCEL", [symbol])
            logger.critical(
                "Amend OCO %s dihentikan: ada leg yang terisi saat pembatalan sehingga "
                "posisi harus direkonsiliasi sebelum proteksi dipasang ulang.",
                symbol,
            )
            return False
        list_status = str(response.get("listOrderStatus") or "").upper()
        if list_status not in ("ALL_DONE", "REJECT"):
            state["_native_stop_exit_blocked"] = True
            _mark_reconciliation(state, "NATIVE_AMEND_CANCEL_UNVERIFIED", [symbol])
            return False
        state["native_oco"] = None
        state["_native_stop_exit_blocked"] = False
        return True

    if not _reconcile_native_stop(client, config, state):
        return False
    intent = state.get("native_stop")
    if not isinstance(intent, dict):
        return False
    try:
        response = client.cancel_order(
            symbol,
            order_id=(
                int(intent["order_id"]) if intent.get("order_id") is not None else None
            ),
            orig_client_order_id=(
                None
                if intent.get("order_id") is not None
                else intent.get("client_order_id")
            ),
        )
    except BinanceAPIError as exc:
        if _is_order_not_found(exc):
            state["native_stop"] = None
            state["_native_stop_exit_blocked"] = False
            _mark_reconciliation(state, "NATIVE_AMEND_CANCEL_NOT_FOUND", [symbol])
            return True
        state["_native_stop_exit_blocked"] = True
        _mark_reconciliation(state, "NATIVE_AMEND_CANCEL_UNVERIFIED", [symbol])
        state["native_protection_retry_at"] = state_mod.now_ms() + _NATIVE_AMEND_RETRY_MS
        logger.critical(
            "Amend stop native %s ditunda: pembatalan tidak terverifikasi (%s). "
            "SELL manual ditahan.",
            symbol,
            exc,
        )
        return False
    status = str(response.get("status") or "").upper() if isinstance(response, dict) else ""
    identity_ok = (
        isinstance(response, dict)
        and str(response.get("symbol") or "").upper() == str(symbol).upper()
        and str(response.get("orderId")) == str(intent.get("order_id"))
        and str(response.get("side") or "").upper() == "SELL"
        and str(response.get("type") or "").upper() == "STOP_LOSS"
    )
    if not identity_ok or status not in ("CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"):
        state["_native_stop_exit_blocked"] = True
        _mark_reconciliation(state, "NATIVE_AMEND_CANCEL_UNVERIFIED", [symbol])
        logger.critical(
            "Amend stop native %s ditunda: respons pembatalan tidak terverifikasi (%s).",
            symbol,
            status or "tanpa status",
        )
        return False
    state["native_stop"] = None
    state["_native_stop_exit_blocked"] = False
    return True


def _arm_native_protection_now(
    client: ExchangeClient, config: dict, filters: SymbolFilters | None, state: dict
) -> bool:
    state["native_protection_retry_at"] = 0
    if _native_oco_enabled(config):
        if _arm_native_oco(client, config, filters, state):
            return True
        status = (state.get("native_oco") or {}).get("status")
        if status in (None, "FAILED") and _native_stop_enabled(config):
            state["native_oco"] = None
            state["native_protection_retry_at"] = 0
            return _arm_native_stop(client, config, filters, state)
        return False
    return _arm_native_stop(client, config, filters, state)


def _run_native_amend(
    client: ExchangeClient,
    config: dict,
    filters: SymbolFilters | None,
    state: dict,
    bid: float | None,
) -> bool:
    plan = state.get("native_amend")
    if not isinstance(plan, dict):
        return True
    symbol = state.get("current_symbol")
    if not symbol or float(state.get("qty") or 0.0) <= 0:
        state["native_amend"] = None
        state_mod.save_state(config["STATE_FILE"], state)
        return True
    if filters is None:
        _mark_reconciliation(state, "NATIVE_AMEND_FILTERS_UNVERIFIED", [symbol])
        state["native_protection_retry_at"] = state_mod.now_ms() + _NATIVE_AMEND_RETRY_MS
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    if bid is None or bid <= 0:
        _mark_reconciliation(state, "NATIVE_AMEND_BID_UNVERIFIED", [symbol])
        state["native_protection_retry_at"] = state_mod.now_ms() + _NATIVE_AMEND_RETRY_MS
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    wanted = float(plan.get("new_stop_price") or 0.0)
    tick = float(filters.tick_size)
    current = _native_protected_stop_level(state)
    if current > 0 and wanted > 0 and current >= wanted - tick:
        state["native_amend"] = None
        state["native_amend_last_ms"] = state_mod.now_ms()
        state_mod.save_state(config["STATE_FILE"], state)
        return True
    target = str(plan.get("target") or "")
    if isinstance(state.get("native_oco"), dict) or isinstance(state.get("native_stop"), dict):
        plan["attempts"] = int(plan.get("attempts") or 0) + 1
        if not _cancel_native_for_amend(client, config, state, target):
            state_mod.save_state(config["STATE_FILE"], state)
            return False
        _mark_reconciliation(state, "NATIVE_AMEND_NO_PROTECTION", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.warning(
            "Proteksi native %s dibatalkan untuk dipindah ke %.12g. Posisi sesaat "
            "tanpa proteksi exchange-side, exit lokal tetap aktif.",
            symbol,
            wanted,
        )
    armed = _arm_native_protection_now(client, config, filters, state)
    if armed:
        state["native_amend"] = None
        state["native_amend_last_ms"] = state_mod.now_ms()
        logger.warning(
            "Proteksi native %s dipindah dari %.12g ke %.12g.",
            symbol,
            float(plan.get("old_stop_price") or 0.0),
            wanted,
        )
    else:
        state["native_protection_retry_at"] = state_mod.now_ms() + _NATIVE_AMEND_RETRY_MS
        logger.critical(
            "Proteksi native %s BELUM terpasang pada level baru %.12g. Percobaan ulang "
            "tiap %d detik, entry baru diblokir sampai proteksi terverifikasi.",
            symbol,
            wanted,
            _NATIVE_AMEND_RETRY_MS // 1000,
        )
    state_mod.save_state(config["STATE_FILE"], state)
    return armed


def _maybe_amend_native_protection(
    client: ExchangeClient,
    config: dict,
    filters: SymbolFilters | None,
    state: dict,
    bid: float | None,
) -> None:
    if not _native_protection_enabled(config):
        return
    if state.get("native_amend") is not None:
        _run_native_amend(client, config, filters, state, bid)
        return
    if state.get("_native_stop_exit_blocked") or state.get("pending_order"):
        return
    if not _native_ready_for_amend(state):
        return
    if filters is None or bid is None or bid <= 0:
        return
    current = _native_protected_stop_level(state)
    if current <= 0:
        return
    wanted = _protection_stop_price(state, config, filters)
    if wanted <= 0:
        return
    tick = float(filters.tick_size)
    if wanted - current < tick:
        return
    if not (0 < wanted < bid) or not filters.price_valid(wanted):
        return
    now = state_mod.now_ms()
    if now - int(state.get("native_amend_last_ms") or 0) < _NATIVE_AMEND_MIN_INTERVAL_MS:
        return
    state["native_amend"] = {
        "target": "OCO" if isinstance(state.get("native_oco"), dict) else "STOP_LOSS",
        "new_stop_price": wanted,
        "old_stop_price": current,
        "attempts": 0,
        "created_at": now,
    }
    state_mod.save_state(config["STATE_FILE"], state)
    _run_native_amend(client, config, filters, state, bid)


def _oco_response_has_fill(response: dict) -> bool:
    reports = response.get("orderReports", []) if isinstance(response, dict) else []
    for report in reports:
        if not isinstance(report, dict):
            continue
        if str(report.get("status") or "").upper() == "FILLED":
            return True
        try:
            if float(report.get("executedQty", 0.0) or 0.0) > 0:
                return True
        except (TypeError, ValueError):
            return True
    return False


def _filled_oco_order_reports(response: dict) -> list[dict]:
    if not isinstance(response, dict):
        return []
    reports = []
    for key in ("orderReports", "orders"):
        raw_reports = response.get(key, [])
        if not isinstance(raw_reports, list):
            continue
        for report in raw_reports:
            if not isinstance(report, dict):
                continue
            try:
                filled = (
                    str(report.get("status") or "").upper() == "FILLED"
                    or float(report.get("executedQty", 0.0) or 0.0) > 0
                )
            except (TypeError, ValueError, OverflowError):
                filled = True
            if filled and report not in reports:
                reports.append(report)
    return reports


def _settle_native_protective_fill(
    client: ExchangeClient,
    config: dict,
    state: dict,
    symbol: str,
    reason: str,
    native_orders: list[dict] | None = None,
) -> None:
    base = symbol[: -len(config["QUOTE_ASSET"])]
    accounting = _ensure_trade_accounting(state, config, symbol)
    if isinstance(accounting, dict):
        accounting["mode"] = str(config.get("MODE", "PAPER")).upper()
        for native_order in native_orders or []:
            if not isinstance(native_order, dict):
                continue
            order_detail = native_order
            try:
                executed = float(order_detail.get("executedQty", 0.0) or 0.0)
                quote_total = float(
                    order_detail.get("cummulativeQuoteQty", 0.0) or 0.0
                )
            except (TypeError, ValueError, OverflowError):
                executed = quote_total = 0.0
            order_id = order_detail.get("orderId")
            if order_id is not None and (executed <= 0 or quote_total <= 0):
                try:
                    detailed = client.get_order(symbol, order_id=int(order_id))
                except Exception:
                    detailed = None
                if (
                    isinstance(detailed, dict)
                    and str(detailed.get("symbol") or "").upper() == str(symbol).upper()
                    and str(detailed.get("orderId")) == str(order_id)
                    and str(detailed.get("side") or "").upper() == "SELL"
                    and (
                        not order_detail.get("clientOrderId")
                        or str(detailed.get("clientOrderId") or "")
                        == str(order_detail.get("clientOrderId"))
                    )
                ):
                    order_detail = detailed
                    try:
                        executed = float(order_detail.get("executedQty", 0.0) or 0.0)
                        quote_total = float(
                            order_detail.get("cummulativeQuoteQty", 0.0) or 0.0
                        )
                    except (TypeError, ValueError, OverflowError):
                        executed = quote_total = 0.0
            if executed <= 0:
                continue
            _attach_accounting_order(
                accounting,
                _accounting_order_record(
                    client,
                    order_detail,
                    symbol,
                    "SELL",
                    str(config["QUOTE_ASSET"]),
                ),
            )
    state["reconciliation_required"] = False
    state["reconciliation_assets"] = []
    reconcile_state_with_exchange(client, config, state)
    if state.get("current_symbol") or state.get("pending_order"):
        if state.get("current_symbol") and float(state.get("qty") or 0.0) > 0:
            if not isinstance(state.get("native_oco"), dict) and not isinstance(
                state.get("native_stop"), dict
            ):
                state["_native_stop_exit_blocked"] = False
                state["native_protection_retry_at"] = state_mod.now_ms() + 60_000
            else:
                state["_native_stop_exit_blocked"] = True
            logger.warning(
                "Exit native %s terisi sebagian; sisa posisi %.12g tetap dikelola.",
                symbol,
                float(state.get("qty") or 0.0),
            )
        else:
            state["reconciliation_required"] = True
            state["reconciliation_assets"] = [base]
        state_mod.save_state(config["STATE_FILE"], state)
        return
    state["sell_fail_count"] = 0
    state["cooldown_until"] = (
        state_mod.now_ms() + config["COOLDOWN_MINUTES_AFTER_CLOSE"] * 60 * 1000
    )
    state["last_trade_time"] = state_mod.now_ms()
    state_mod.save_state(config["STATE_FILE"], state)
    logger.info(
        "EXIT NATIVE %s (%s): posisi proteksi exchange-side telah direkonsiliasi. "
        "Klasifikasi memakai accounting fill/komisi, bukan bid perkiraan.",
        symbol,
        reason,
    )
    try_dust_sweep(client, config, symbol)


def _record_native_partial_fills(
    client: ExchangeClient,
    config: dict,
    state: dict,
    symbol: str,
    reason: str,
    native_orders: list[dict],
) -> None:
    accounting = _ensure_trade_accounting(state, config, symbol)
    if isinstance(accounting, dict):
        accounting["mode"] = str(config.get("MODE", "PAPER")).upper()
        for native_order in native_orders:
            if not isinstance(native_order, dict):
                continue
            order_detail = native_order
            order_id = order_detail.get("orderId")
            try:
                executed = float(order_detail.get("executedQty", 0.0) or 0.0)
                quote_total = float(order_detail.get("cummulativeQuoteQty", 0.0) or 0.0)
            except (TypeError, ValueError, OverflowError):
                executed = quote_total = 0.0
            if order_id is not None and (executed <= 0 or quote_total <= 0):
                try:
                    detailed = client.get_order(symbol, order_id=int(order_id))
                except Exception:
                    detailed = None
                if (
                    isinstance(detailed, dict)
                    and str(detailed.get("symbol") or "").upper() == str(symbol).upper()
                    and str(detailed.get("orderId")) == str(order_id)
                    and str(detailed.get("side") or "").upper() == "SELL"
                    and (
                        not order_detail.get("clientOrderId")
                        or str(detailed.get("clientOrderId") or "")
                        == str(order_detail.get("clientOrderId"))
                    )
                ):
                    order_detail = detailed
                    try:
                        executed = float(order_detail.get("executedQty", 0.0) or 0.0)
                        quote_total = float(
                            order_detail.get("cummulativeQuoteQty", 0.0) or 0.0
                        )
                    except (TypeError, ValueError, OverflowError):
                        executed = quote_total = 0.0
            if executed <= 0:
                continue
            _attach_accounting_order(
                accounting,
                _accounting_order_record(
                    client, order_detail, symbol, "SELL", str(config["QUOTE_ASSET"])
                ),
            )
    try:
        account = client.get_account()
        if str(config.get("MODE", "PAPER")).upper() == "LIVE":
            _validate_live_account_snapshot(account, str(config["QUOTE_ASSET"]))
        total_base = get_total_balance(
            account, symbol[: -len(str(config["QUOTE_ASSET"]))]
        )
    except Exception as exc:
        _mark_reconciliation(state, "NATIVE_PARTIAL_BALANCE_UNVERIFIED", [symbol])
        logger.critical("Sisa saldo setelah fill native %s tidak terverifikasi: %s", symbol, exc)
    else:
        if total_base > 0:
            state["qty"] = min(float(state.get("qty") or 0.0), total_base)
            _refresh_accounting_state(
                client, config, state, actual_remaining_qty=total_base
            )
            state["reconciliation_required"] = False
            state["reconciliation_assets"] = []
        else:
            _refresh_accounting_state(client, config, state)
            _mark_reconciliation(state, "NATIVE_PARTIAL_ZERO_BALANCE_PENDING", [symbol])
    state_mod.save_state(config["STATE_FILE"], state)
    logger.warning(
        "Fill proteksi native %s direkam (%s); sisa posisi tetap dilindungi/dikelola.",
        symbol,
        reason,
    )


def _reconcile_native_oco(client: ExchangeClient, config: dict, state: dict) -> bool:
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
            order_list_id=(
                int(intent["order_list_id"])
                if intent.get("order_list_id") is not None
                else None
            ),
            list_client_order_id=(
                None
                if intent.get("order_list_id") is not None
                else intent.get("list_client_order_id")
            ),
        )
    except BinanceAPIError as exc:
        never_confirmed = intent.get("order_list_id") is None
        age_ms = state_mod.now_ms() - int(intent.get("created_at") or 0)
        if (
            _is_order_not_found(exc)
            and never_confirmed
            and age_ms >= _NOT_FOUND_MIN_INTENT_AGE_MS
        ):
            state["native_oco"] = None
            state["_native_stop_exit_blocked"] = False
            state["native_protection_retry_at"] = state_mod.now_ms() + 60_000
            state["reconciliation_required"] = True
            state["reconciliation_assets"] = [symbol]
            state_mod.save_state(config["STATE_FILE"], state)
            logger.critical(
                "OCO %s dipastikan TIDAK PERNAH tercipta di bursa (%s). "
                "Exit lokal diaktifkan kembali dan proteksi akan dipasang ulang.",
                symbol,
                exc,
            )
            return False
        intent["status"] = "UNKNOWN"
        intent["last_error"] = str(exc)
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "Status OCO %s tidak dapat diverifikasi: %s. Exit market ditahan.",
            symbol,
            exc,
        )
        return False

    expected_list_id = intent.get("order_list_id")
    response_order_ids = {
        str(item.get("clientOrderId") or "")
        for item in (response.get("orders", []) if isinstance(response, dict) else [])
        if isinstance(item, dict)
    }
    expected_leg_ids = {
        str((intent.get(name) or {}).get("client_order_id") or "")
        for name in ("above", "below")
    }
    response_valid = (
        isinstance(response, dict)
        and str(response.get("symbol") or "").upper() == str(symbol).upper()
        and str(response.get("contingencyType") or "").upper() == "OCO"
        and expected_leg_ids.issubset(response_order_ids)
        and "" not in expected_leg_ids
        and str(response.get("listClientOrderId") or "")
        == str(intent.get("list_client_order_id") or "")
        and (
            expected_list_id is None
            or str(response.get("orderListId")) == str(expected_list_id)
        )
    )
    if not response_valid:
        intent["status"] = "UNKNOWN"
        state["_native_stop_exit_blocked"] = True
        _mark_reconciliation(state, "NATIVE_OCO_QUERY_IDENTITY_MISMATCH", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    intent["order_list_id"] = response.get("orderListId", intent.get("order_list_id"))
    intent["list_status_type"] = str(
        response.get("listStatusType") or "UNKNOWN"
    ).upper()
    status = str(response.get("listOrderStatus") or "UNKNOWN").upper()
    intent["status"] = status
    for leg_name in ("above", "below"):
        leg = intent.get(leg_name)
        if not isinstance(leg, dict):
            continue
        report = next(
            (
                x
                for x in response.get("orders", [])
                if x.get("clientOrderId") == leg.get("client_order_id")
            ),
            None,
        )
        if report is None:
            report = next(
                (
                    x
                    for x in response.get("orderReports", [])
                    if x.get("clientOrderId") == leg.get("client_order_id")
                ),
                None,
            )
        if isinstance(report, dict):
            leg["order_id"] = report.get("orderId", leg.get("order_id"))
            leg["status"] = str(report.get("status") or "UNKNOWN").upper()
            leg["executed_qty"] = float(report.get("executedQty", 0.0) or 0.0)

    filled = _oco_response_has_fill(response)
    native_fill_orders: list[dict] = []
    if not filled and status == "ALL_DONE":
        for leg_name in ("above", "below"):
            leg = intent.get(leg_name)
            if not isinstance(leg, dict) or leg.get("order_id") is None:
                state["_native_stop_exit_blocked"] = True
                state["reconciliation_required"] = True
                state["reconciliation_assets"] = [symbol]
                state_mod.save_state(config["STATE_FILE"], state)
                logger.critical(
                    "OCO %s ALL_DONE tanpa detail leg yang dapat diverifikasi.", symbol
                )
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
            child_valid = (
                isinstance(child, dict)
                and str(child.get("symbol") or "").upper() == str(symbol).upper()
                and str(child.get("orderId")) == str(leg.get("order_id"))
                and str(child.get("clientOrderId") or "")
                == str(leg.get("client_order_id") or "")
                and str(child.get("side") or "").upper() == "SELL"
                and str(child.get("type") or "").upper()
                == str(leg.get("type") or "").upper()
            )
            if not child_valid:
                state["_native_stop_exit_blocked"] = True
                _mark_reconciliation(
                    state, "NATIVE_OCO_LEG_IDENTITY_MISMATCH", [symbol]
                )
                state_mod.save_state(config["STATE_FILE"], state)
                return False
            leg["status"] = str(child.get("status") or "UNKNOWN").upper()
            leg["executed_qty"] = float(child.get("executedQty", 0.0) or 0.0)
            if leg["status"] == "FILLED" or leg["executed_qty"] > 0:
                native_fill_orders.append(child)
                filled = True

    if filled:
        for leg_name in ("above", "below"):
            leg = intent.get(leg_name)
            if not isinstance(leg, dict):
                continue
            try:
                leg_filled = (
                    str(leg.get("status") or "").upper() == "FILLED"
                    or float(leg.get("executed_qty", 0.0) or 0.0) > 0
                )
            except (TypeError, ValueError, OverflowError):
                leg_filled = True
            if not leg_filled:
                continue
            report = next(
                (
                    item
                    for item in (
                        list(response.get("orders", []))
                        + list(response.get("orderReports", []))
                    )
                    if isinstance(item, dict)
                    and str(item.get("clientOrderId") or "")
                    == str(leg.get("client_order_id") or "")
                ),
                None,
            )
            if leg.get("order_id") is None and isinstance(report, dict):
                leg["order_id"] = report.get("orderId")
            if leg.get("order_id") is None:
                continue
            if any(
                str(existing.get("orderId")) == str(leg.get("order_id"))
                for existing in native_fill_orders
            ):
                continue
            try:
                child = client.get_order(symbol, order_id=int(leg["order_id"]))
            except BinanceAPIError:
                if isinstance(report, dict):
                    native_fill_orders.append(report)
                continue
            child_valid = (
                isinstance(child, dict)
                and str(child.get("symbol") or "").upper() == str(symbol).upper()
                and str(child.get("orderId")) == str(leg.get("order_id"))
                and str(child.get("clientOrderId") or "")
                == str(leg.get("client_order_id") or "")
                and str(child.get("side") or "").upper() == "SELL"
                and str(child.get("type") or "").upper()
                == str(leg.get("type") or "").upper()
            )
            if child_valid:
                native_fill_orders.append(child)
            elif isinstance(report, dict):
                native_fill_orders.append(report)

    if filled and status == "EXECUTING":
        _record_native_partial_fills(
            client,
            config,
            state,
            symbol,
            "NATIVE_OCO_PARTIAL_FILL",
            native_fill_orders,
        )
        return True
    if filled and status not in ("ALL_DONE", "REJECT"):
        _record_native_partial_fills(
            client,
            config,
            state,
            symbol,
            "NATIVE_OCO_STATUS_UNCERTAIN_AFTER_FILL",
            native_fill_orders,
        )
        _mark_reconciliation(state, "NATIVE_OCO_FILLED_STATUS_UNCERTAIN", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    if filled:
        base = symbol[: -len(config["QUOTE_ASSET"])]
        state["native_oco"] = None
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [base]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "OCO %s salah satu leg terminal FILLED; tidak mengirim SELL kedua. Rekonsiliasi saldo dijalankan.",
            symbol,
        )
        _settle_native_protective_fill(
            client,
            config,
            state,
            symbol,
            "NATIVE_OCO_FILLED",
            native_orders=native_fill_orders,
        )
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


def _reconcile_native_stop(client: ExchangeClient, config: dict, state: dict) -> bool:
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
    if status in ("CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"):
        state["native_stop"] = None
        state["_native_stop_exit_blocked"] = False
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    try:
        order = client.get_order(
            symbol,
            order_id=(
                int(intent["order_id"]) if intent.get("order_id") is not None else None
            ),
            orig_client_order_id=(
                None
                if intent.get("order_id") is not None
                else intent.get("client_order_id")
            ),
        )
    except BinanceAPIError as exc:
        never_confirmed = intent.get("order_id") is None
        age_ms = state_mod.now_ms() - int(intent.get("created_at") or 0)
        if (
            _is_order_not_found(exc)
            and never_confirmed
            and age_ms >= _NOT_FOUND_MIN_INTENT_AGE_MS
        ):
            state["native_stop"] = None
            state["_native_stop_exit_blocked"] = False
            state["native_protection_retry_at"] = state_mod.now_ms() + 60_000
            state["reconciliation_required"] = True
            state["reconciliation_assets"] = [symbol]
            state_mod.save_state(config["STATE_FILE"], state)
            logger.critical(
                "Proteksi native %s dipastikan TIDAK PERNAH tercipta di bursa (%s). "
                "Exit lokal diaktifkan kembali dan proteksi akan dipasang ulang.",
                symbol,
                exc,
            )
            return False
        intent["status"] = "UNKNOWN"
        intent["last_error"] = str(exc)
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "Status proteksi native %s tidak dapat diverifikasi: %s. Exit market ditahan.",
            symbol,
            exc,
        )
        return False
    try:
        order_numeric_valid = Decimal(str(order.get("origQty"))) == Decimal(
            str(intent.get("quantity"))
        ) and Decimal(str(order.get("stopPrice"))) == Decimal(
            str(intent.get("stop_price"))
        )
    except (AttributeError, ArithmeticError, TypeError, ValueError):
        order_numeric_valid = False
    order_identity_valid = (
        isinstance(order, dict)
        and str(order.get("symbol") or "").upper() == str(symbol).upper()
        and (
            intent.get("order_id") is None
            or str(order.get("orderId")) == str(intent.get("order_id"))
        )
        and str(order.get("clientOrderId") or "")
        == str(intent.get("client_order_id") or "")
        and str(order.get("side") or "").upper() == "SELL"
        and str(order.get("type") or "").upper() == "STOP_LOSS"
        and order_numeric_valid
    )
    if not order_identity_valid:
        intent["status"] = "UNKNOWN"
        state["_native_stop_exit_blocked"] = True
        _mark_reconciliation(state, "NATIVE_STOP_QUERY_IDENTITY_MISMATCH", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
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
        logger.critical(
            "Proteksi native %s FILLED; tidak mengirim SELL kedua. Rekonsiliasi saldo dijalankan.",
            symbol,
        )
        _settle_native_protective_fill(
            client,
            config,
            state,
            symbol,
            "NATIVE_STOP_FILLED",
            native_orders=[order],
        )
        return False
    if status == "PARTIALLY_FILLED" and float(order.get("executedQty", 0.0) or 0.0) > 0:
        _record_native_partial_fills(
            client,
            config,
            state,
            symbol,
            "NATIVE_STOP_PARTIAL_FILL",
            [order],
        )
        return True
    if status in ("CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"):
        try:
            canceled_executed = float(order.get("executedQty", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            canceled_executed = 0.0
        state["native_stop"] = None
        if canceled_executed > 0:
            state["_native_stop_exit_blocked"] = True
            _mark_reconciliation(state, "NATIVE_STOP_PARTIAL_FILLED_TERMINAL", [symbol])
            state_mod.save_state(config["STATE_FILE"], state)
            _settle_native_protective_fill(
                client,
                config,
                state,
                symbol,
                "NATIVE_STOP_PARTIAL_FILL_TERMINAL",
                native_orders=[order],
            )
        else:
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


def _cancel_native_oco_before_exit(
    client: ExchangeClient, config: dict, state: dict
) -> bool:
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
            order_list_id=(
                int(intent["order_list_id"])
                if intent.get("order_list_id") is not None
                else None
            ),
            list_client_order_id=(
                None
                if intent.get("order_list_id") is not None
                else intent.get("list_client_order_id")
            ),
        )
    except BinanceAPIError as exc:
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "OCO %s gagal dibatalkan: %s. SELL manual ditahan.", symbol, exc
        )
        return False
    cancel_identity_valid = (
        isinstance(response, dict)
        and str(response.get("symbol") or "").upper() == str(symbol).upper()
        and str(response.get("contingencyType") or "").upper() == "OCO"
        and str(response.get("listClientOrderId") or "")
        == str(intent.get("list_client_order_id") or "")
        and (
            intent.get("order_list_id") is None
            or str(response.get("orderListId")) == str(intent.get("order_list_id"))
        )
    )
    if not cancel_identity_valid:
        state["_native_stop_exit_blocked"] = True
        _mark_reconciliation(state, "NATIVE_OCO_CANCEL_IDENTITY_MISMATCH", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    final_status = str(response.get("listOrderStatus") or "").upper()
    if _oco_response_has_fill(response):
        fill_orders = _filled_oco_order_reports(response)
        if final_status == "ALL_DONE":
            state["native_oco"] = None
            state["_native_stop_exit_blocked"] = True
            state["reconciliation_required"] = True
            state["reconciliation_assets"] = [symbol]
            state_mod.save_state(config["STATE_FILE"], state)
            logger.critical(
                "OCO %s terisi saat cancel. SELL manual ditahan sampai settlement fill selesai.",
                symbol,
            )
            _settle_native_protective_fill(
                client,
                config,
                state,
                symbol,
                "NATIVE_OCO_FILLED_ON_CANCEL",
                native_orders=fill_orders,
            )
        else:
            intent["status"] = final_status or "UNKNOWN"
            state["_native_stop_exit_blocked"] = True
            _record_native_partial_fills(
                client,
                config,
                state,
                symbol,
                "NATIVE_OCO_CANCEL_PARTIAL_FILL",
                fill_orders,
            )
            _mark_reconciliation(state, "NATIVE_OCO_CANCEL_STATUS_UNCERTAIN", [symbol])
            state_mod.save_state(config["STATE_FILE"], state)
            logger.critical(
                "OCO %s menunjukkan fill tetapi cancel belum terminal (%s). SELL manual ditahan.",
                symbol,
                final_status or "UNKNOWN",
            )
        return False
    if final_status != "ALL_DONE":
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "Cancel OCO %s belum terverifikasi: %s. SELL manual ditahan.",
            symbol,
            final_status or "UNKNOWN",
        )
        return False
    state["native_oco"] = None
    state["native_protection_retry_at"] = 0
    state["_native_stop_exit_blocked"] = False
    state["reconciliation_required"] = False
    state["reconciliation_assets"] = []
    state_mod.save_state(config["STATE_FILE"], state)
    return True


def _cancel_native_stop_before_exit(
    client: ExchangeClient, config: dict, state: dict
) -> bool:
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
            order_id=(
                int(intent["order_id"]) if intent.get("order_id") is not None else None
            ),
            orig_client_order_id=(
                None
                if intent.get("order_id") is not None
                else intent.get("client_order_id")
            ),
        )
    except BinanceAPIError as exc:
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "Proteksi native %s gagal dibatalkan: %s. SELL manual ditahan.", symbol, exc
        )
        return False
    cancel_client_ids = (
        {
            str(response.get("clientOrderId") or ""),
            str(response.get("origClientOrderId") or ""),
        }
        if isinstance(response, dict)
        else set()
    )
    cancel_identity_valid = (
        isinstance(response, dict)
        and str(response.get("symbol") or "").upper() == str(symbol).upper()
        and str(response.get("orderId")) == str(intent.get("order_id"))
        and str(intent.get("client_order_id") or "") in cancel_client_ids
        and str(response.get("side") or "").upper() == "SELL"
        and str(response.get("type") or "").upper() == "STOP_LOSS"
    )
    if not cancel_identity_valid:
        state["_native_stop_exit_blocked"] = True
        _mark_reconciliation(state, "NATIVE_STOP_CANCEL_IDENTITY_MISMATCH", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    final_status = str(response.get("status") or "").upper()
    try:
        cancel_executed_qty = float(response.get("executedQty", 0.0) or 0.0)
    except (TypeError, ValueError, OverflowError):
        cancel_executed_qty = 0.0
    cancel_terminal = final_status in (
        "FILLED",
        "CANCELED",
        "EXPIRED",
        "EXPIRED_IN_MATCH",
        "REJECTED",
    )
    if cancel_executed_qty > 0 and not cancel_terminal:
        intent["status"] = final_status or "UNKNOWN"
        state["_native_stop_exit_blocked"] = True
        _record_native_partial_fills(
            client,
            config,
            state,
            symbol,
            "NATIVE_STOP_CANCEL_PARTIAL_FILL",
            [response],
        )
        _mark_reconciliation(state, "NATIVE_STOP_CANCEL_STATUS_UNCERTAIN", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        return False
    if final_status == "FILLED" or (
        cancel_executed_qty > 0
        and final_status in ("CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED")
    ):
        state["native_stop"] = None
        state["_native_stop_exit_blocked"] = True
        _mark_reconciliation(state, "NATIVE_STOP_FILLED_ON_CANCEL", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        _settle_native_protective_fill(
            client,
            config,
            state,
            symbol,
            "NATIVE_STOP_FILLED_ON_CANCEL",
            native_orders=[response],
        )
        return False
    if final_status not in ("CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"):
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "Cancel proteksi native %s belum terverifikasi: %s. SELL manual ditahan.",
            symbol,
            final_status or "UNKNOWN",
        )
        return False
    state["native_stop"] = None
    state["reconciliation_required"] = False
    state["reconciliation_assets"] = []
    state_mod.save_state(config["STATE_FILE"], state)
    return True


def _executable_bid(
    client: ExchangeClient, symbol: str, reference_price: float | None = None
) -> float | None:
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


_LISTING_AGE_TTL_MS = 6 * 3_600_000
_LISTING_AGE_CACHE_LIMIT = 500
_listing_age_cache: "OrderedDict[str, tuple[int, float]]" = OrderedDict()


def listing_age_days(client: ExchangeClient, symbol: str, now_ms: int) -> float:
    now = int(now_ms or 0)
    cached = _listing_age_cache.get(symbol)
    if cached is not None:
        cached_at, cached_age = cached
        if 0 <= now - cached_at < _LISTING_AGE_TTL_MS:
            _listing_age_cache.move_to_end(symbol)
            return cached_age
        _listing_age_cache.pop(symbol, None)
    raw = client.get_klines(symbol, "1d", limit=1, start_time_ms=0)
    if not raw:
        raise BinanceAPIError(
            502,
            None,
            f"riwayat harian {symbol} kosong sehingga usia listing tidak dapat "
            "diverifikasi",
        )
    try:
        first_open_ms = int(raw[0][0])
    except (IndexError, TypeError, ValueError) as exc:
        raise BinanceAPIError(
            502, None, f"candle harian pertama {symbol} tidak valid"
        ) from exc
    if first_open_ms <= 0:
        raise BinanceAPIError(
            502, None, f"waktu listing {symbol} tidak valid ({first_open_ms})"
        )
    age = max(0.0, (now - first_open_ms) / 86_400_000.0)
    _listing_age_cache[symbol] = (now, age)
    _listing_age_cache.move_to_end(symbol)
    while len(_listing_age_cache) > _LISTING_AGE_CACHE_LIMIT:
        _listing_age_cache.popitem(last=False)
    return age


def _restore_pending_buy(
    client: ExchangeClient,
    config: dict,
    state: dict,
    pending: dict,
    order: dict,
    account: dict,
) -> bool:
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
    state["entry_time"] = int(order.get("transactTime") or order.get("time") or state_mod.now_ms())
    state["trade_accounting"] = _new_trade_accounting(
        client,
        config,
        order,
        symbol,
        qty,
        quoted / executed,
        state["entry_time"],
    )
    _refresh_accounting_state(
        client, config, state, actual_remaining_qty=qty
    )
    state["be_active"] = False
    state["be_stop_price"] = 0.0
    state["trailing_active"] = False
    state["trailing_stop_price"] = 0.0
    for key in (
        "sl_pct",
        "tp_pct",
        "be_trigger_pct",
        "be_lock_pct",
        "trail_start_pct",
        "trail_step_pct",
        "exit_source",
    ):
        if key in levels:
            state[key] = levels[key]
    state["last_trade_time"] = state_mod.now_ms()
    state["sell_fail_count"] = 0
    return True


def _fresh_live_entry_filters(
    client: ExchangeClient, symbol: str, account: dict
) -> SymbolFilters:
    exchange_info = client.get_exchange_info(symbol)
    if not isinstance(exchange_info, dict) or not isinstance(
        exchange_info.get("symbols"), list
    ):
        raise BinanceAPIError(
            502, None, f"exchangeInfo {symbol} tidak memiliki daftar symbols"
        )
    matches = [
        item
        for item in exchange_info["symbols"]
        if isinstance(item, dict)
        and str(item.get("symbol") or "").upper() == str(symbol).upper()
    ]
    if len(exchange_info["symbols"]) != 1 or len(matches) != 1:
        raise BinanceAPIError(
            502,
            None,
            f"exchangeInfo {symbol} tidak menghasilkan tepat satu simbol yang cocok",
        )
    symbol_data = matches[0]
    if str(symbol_data.get("status") or "").upper() != "TRADING":
        raise BinanceAPIError(403, None, f"simbol {symbol} tidak berstatus TRADING")
    if symbol_data.get("isSpotTradingAllowed") is not True:
        raise BinanceAPIError(
            403, None, f"simbol {symbol} tidak mengonfirmasi Spot trading aktif"
        )
    try:
        fresh_filters = SymbolFilters.from_symbol_data(symbol_data)
    except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
        raise BinanceAPIError(
            502, None, f"filter/permissionSets {symbol} tidak dapat diverifikasi"
        ) from exc
    if not fresh_filters.permission_metadata_verified:
        raise BinanceAPIError(502, None, f"permissionSets {symbol} hilang atau rusak")
    if not permission_sets_allow(
        fresh_filters.permission_sets, _effective_account_permissions(account)
    ):
        raise BinanceAPIError(
            403, None, f"permissionSets {symbol} tidak cocok dengan permission akun"
        )
    return fresh_filters


def _entry_orderbook_ok(
    client: ExchangeClient, config: dict, symbol: str, planned_notional: float
) -> bool:
    if not (
        bool(config.get("DEPTH_FILTER_ENABLED", False))
        or bool(config.get("ORDERBOOK_FILTER_ENABLED", False))
    ):
        return True
    limit = scanner.normalize_depth_limit(config.get("ORDERBOOK_DEPTH_LIMIT", 500))
    try:
        depth = client.get_depth(symbol, limit)
    except Exception as exc:
        logger.warning(
            "Entry %s dibatalkan: order book gagal diambil (%s). Fail closed.",
            symbol,
            str(exc)[:160],
        )
        return False
    ok, reason, _metrics = scanner.evaluate_orderbook(depth, planned_notional, config)
    if ok:
        logger.info("Order book %s lolos: %s", symbol, reason)
        return True
    logger.warning("Entry %s dibatalkan oleh filter order book: %s", symbol, reason)
    return False


def open_position(
    client: ExchangeClient,
    config: dict,
    filters_cache: dict,
    state: dict,
    candidate: "scanner.Candidate",
    reference_price: "float | None" = None,
) -> None:
    try:
        strategy_dd.require_entry_period(config, state)
    except Exception as exc:
        logger.warning("Entry ditahan: periode DD strategi belum siap (%s).", type(exc).__name__)
        return
    if (
        str(config.get("MODE", "PAPER")).upper() == "LIVE"
        and state.get("accounting_blocked")
    ):
        logger.critical(
            "Entry LIVE %s ditahan: accounting fee/fill belum terverifikasi (%s).",
            candidate.symbol,
            str(state.get("accounting_block_reason") or "alasan tidak tersedia")[:120],
        )
        return
    block_until = _symbol_block_until_ts(state, candidate.symbol)
    now_ms = state_mod.now_ms()
    if block_until > now_ms:
        logger.info(
            "Entry %s diblokir sampai %s: koin yang sama baru ditutup dengan loss "
            "(SAME_COIN_BLOCK_HOURS=%g jam, sisa %.1f menit).",
            candidate.symbol,
            _fmt_utc_ms(block_until),
            float(config.get("SAME_COIN_BLOCK_HOURS", 0) or 0),
            (block_until - now_ms) / 60000.0,
        )
        return
    filters = filters_cache.get(candidate.symbol)
    if filters is None:
        logger.warning(
            "Tidak ada data filter untuk %s, entry dilewati.", candidate.symbol
        )
        return

    price_ref = (
        reference_price
        if (reference_price and reference_price > 0)
        else candidate.last_price
    )

    try:
        account = client.get_account()
        if str(config.get("MODE", "PAPER")).upper() == "LIVE":
            _validate_live_account_snapshot(account, config["QUOTE_ASSET"])
    except BinanceAPIError as exc:
        _mark_reconciliation(state, "BUY_ACCOUNT_UNVERIFIED", [config["QUOTE_ASSET"]])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.error(
            "Gagal verifikasi saldo/status akun sebelum BUY %s: %s. Entry dilewati.",
            candidate.symbol,
            exc,
        )
        return
    if str(config.get("MODE", "PAPER")).upper() == "LIVE":
        try:
            filters = _fresh_live_entry_filters(client, candidate.symbol, account)
        except BinanceAPIError as exc:
            logger.error(
                "Entry %s diblokir saat verifikasi ulang metadata simbol: %s",
                candidate.symbol,
                exc,
            )
            return
        filters_cache[candidate.symbol] = filters
    usdt_free = get_balance(account, config["QUOTE_ASSET"])
    sizing = strategy.resolve_position_notional(config, usdt_free)
    usdt_amount = sizing["notional"]
    if sizing["cap_active"]:
        asal = (
            f"RISK_PERCENT={float(config.get('RISK_PERCENT', 0) or 0):.2f}%"
            if sizing["mode"] == "PERCENT"
            else f"POSITION_SIZE_USDT={float(config.get('POSITION_SIZE_USDT', 0) or 0):.2f}"
        )
        logger.warning(
            "Ukuran posisi %s dipotong plafon MAX_POSITION_USDT: %.2f -> %.2f %s. "
            "Sumber nominal=%s; eksposur efektif %.2f%% dari saldo free %.2f.",
            candidate.symbol,
            sizing["requested_notional"],
            usdt_amount,
            config["QUOTE_ASSET"],
            asal,
            sizing["effective_pct_of_free"],
            usdt_free,
        )
    if usdt_amount > usdt_free:
        logger.warning(
            "Entry %s dilewati: nominal %.2f %s melebihi saldo free %.2f %s.",
            candidate.symbol,
            usdt_amount,
            config["QUOTE_ASSET"],
            usdt_free,
            config["QUOTE_ASSET"],
        )
        return

    if price_ref <= 0:
        logger.warning(
            "Entry %s dilewati: harga ASK/acuan tidak valid %.8f.",
            candidate.symbol,
            price_ref,
        )
        return

    order_quote_qty = usdt_amount
    if filters.max_notional > 0:
        order_quote_qty = min(order_quote_qty, float(filters.max_notional))
    qty = filters.round_qty(order_quote_qty / price_ref)
    if filters.max_qty > 0 and qty > float(filters.max_qty):
        qty = filters.round_qty(float(filters.max_qty))
        order_quote_qty = min(order_quote_qty, qty * price_ref)
    if bool(filters.quote_order_qty_market_allowed):
        order_quote_qty = filters.round_quote_amount(order_quote_qty)
    notional = qty * price_ref
    if (
        not filters.market_qty_valid(qty)
        or notional < float(filters.min_notional)
        or order_quote_qty < float(filters.min_notional)
        or (filters.max_notional > 0 and order_quote_qty > float(filters.max_notional))
    ):
        logger.warning(
            "Entry %s dilewati: qty/notional di bawah atau melampaui batas bursa "
            "(qty=%.8f, notional=%.2f, minQty=%.8f, maxQty=%.8f, "
            "minNotional=%.2f, maxNotional=%.2f). Nominal order %.4f %s.",
            candidate.symbol,
            qty,
            notional,
            float(filters.min_qty),
            float(filters.max_qty),
            float(filters.min_notional),
            float(filters.max_notional),
            order_quote_qty,
            config["QUOTE_ASSET"],
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
            candidate.symbol,
            float(preview.get("sl_pct") or 0.0),
            price_ref,
        )
        return
    sl_distance = float(preview.get("sl_pct") or 0.0)
    if str(preview.get("source", "")).upper() == "ATR":
        risk_quote = qty * sl_distance
        risk_basis = "jarak harga SL x qty"
    else:
        risk_quote = notional * sl_distance / 100.0
        risk_basis = "notional x SL persen"
    exposure_pct = (notional / usdt_free * 100.0) if usdt_free > 0 else 0.0
    risk_pct_free = (risk_quote / usdt_free * 100.0) if usdt_free > 0 else 0.0
    log_risk = logger.info if risk_pct_free < 10.0 else logger.warning
    log_risk(
        "Risiko SL %s: nominal %.4f %s (%.1f%% saldo bebas %.4f), perkiraan "
        "kerugian bila SL kena %.4f %s (%.2f%% saldo bebas) dihitung dari %s.",
        candidate.symbol,
        notional,
        config["QUOTE_ASSET"],
        exposure_pct,
        usdt_free,
        risk_quote,
        config["QUOTE_ASSET"],
        risk_pct_free,
        risk_basis,
    )
    if exposure_pct >= 50.0:
        logger.warning(
            "Ukuran posisi %s memakai %.1f%% saldo bebas. RISK_PERCENT dan "
            "POSITION_SIZE_USDT adalah NOMINAL posisi, bukan risiko per trade; "
            "kerugian sebenarnya ditentukan jarak stop loss di atas.",
            candidate.symbol,
            exposure_pct,
        )
    if not _entry_orderbook_ok(client, config, candidate.symbol, notional):
        return
    client_order_id = _new_client_order_id("buy")
    state["pending_order"] = {
        "side": "BUY",
        "symbol": candidate.symbol,
        "qty": qty,
        "client_order_id": client_order_id,
        "created_at": state_mod.now_ms(),
        "levels": {
            "sl_pct": preview["sl_pct"],
            "tp_pct": preview["tp_pct"],
            "be_trigger_pct": preview["be_trigger_pct"],
            "be_lock_pct": preview["be_lock_pct"],
            "trail_start_pct": preview["trail_start_pct"],
            "trail_step_pct": preview["trail_step_pct"],
            "exit_source": preview["source"],
        },
    }
    pending_intent = dict(state["pending_order"])
    state_mod.save_state(config["STATE_FILE"], state)
    try:
        resp = _submit_market_order(
            client,
            symbol=candidate.symbol,
            side="BUY",
            quantity=order_quantity,
            client_order_id=client_order_id,
            quote_order_qty=order_quote_qty,
            quote_precision=filters.quote_precision,
        )
    except BinanceAPIError as exc:
        if _is_definitive_reject(exc):
            state["pending_order"] = None
            _clear_reconciliation(state)
            state_mod.save_state(config["STATE_FILE"], state)
            logger.error(
                "Order BUY %s ditolak bursa dan dipastikan tidak terbentuk: %s. "
                "Intent dibatalkan, entry berikutnya tetap berjalan.",
                candidate.symbol,
                exc,
            )
            return
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [candidate.symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "Order BUY %s status tidak pasti: %s. Intent disimpan dan entry baru diblokir sampai rekonsiliasi.",
            candidate.symbol,
            exc,
        )
        return

    executed_qty = max(0.0, float(resp.get("executedQty", 0.0) or 0.0))
    cumm_quote = max(0.0, float(resp.get("cummulativeQuoteQty", 0.0) or 0.0))
    status = str(resp.get("status") or "").upper()
    live = str(config.get("MODE", "PAPER")).upper() == "LIVE"
    identity_valid = not live or (
        str(resp.get("symbol") or "").upper() == candidate.symbol.upper()
        and str(resp.get("clientOrderId") or "") == client_order_id
        and str(resp.get("side") or "").upper() == "BUY"
        and str(resp.get("type") or "").upper() == "MARKET"
    )
    if (
        not identity_valid
        or status in _NONTERMINAL_ORDER_STATUSES
        or status not in _TERMINAL_ORDER_STATUSES
    ):
        pending = state.get("pending_order")
        if isinstance(pending, dict):
            pending["last_status"] = status or "UNKNOWN"
            pending["executed_qty"] = executed_qty
            pending["cummulative_quote_qty"] = cumm_quote
        _mark_reconciliation(state, "BUY_RESPONSE_UNVERIFIED", [candidate.symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "Respons BUY %s belum terminal/konsisten: status=%s filled=%.8f. "
            "Intent dipertahankan untuk query berdasarkan client order ID.",
            candidate.symbol,
            status or "UNKNOWN",
            executed_qty,
        )
        return

    if executed_qty <= 0:
        state["pending_order"] = None
        _clear_reconciliation(state)
        state_mod.save_state(config["STATE_FILE"], state)
        logger.warning(
            "BUY %s terminal tanpa fill (status=%s).", candidate.symbol, status
        )
        return
    if cumm_quote <= 0:
        _mark_reconciliation(state, "BUY_FILL_VALUE_UNVERIFIED", [candidate.symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "BUY %s memiliki executedQty tetapi cummulativeQuoteQty tidak valid: %s",
            candidate.symbol,
            resp,
        )
        return

    fill_price = cumm_quote / executed_qty
    base_asset = candidate.symbol[: -len(config["QUOTE_ASSET"])]
    base_commission = 0.0
    fills = resp.get("fills", []) if isinstance(resp, dict) else []
    if isinstance(fills, list):
        for fill in fills:
            if not isinstance(fill, dict) or fill.get("commissionAsset") != base_asset:
                continue
            try:
                base_commission += max(0.0, float(fill.get("commission", 0.0) or 0.0))
            except (TypeError, ValueError):
                pass
    estimated_qty = max(0.0, executed_qty - base_commission)

    state["pending_order"] = None
    state["current_symbol"] = candidate.symbol
    state["entry_price"] = fill_price
    state["qty"] = estimated_qty or executed_qty
    state["entry_time"] = int(resp.get("transactTime") or state_mod.now_ms())
    state["trade_accounting"] = _new_trade_accounting(
        client,
        config,
        resp,
        candidate.symbol,
        state["qty"],
        fill_price,
        state["entry_time"],
    )
    _refresh_accounting_state(
        client, config, state, actual_remaining_qty=state["qty"]
    )
    state["last_trade_time"] = state_mod.now_ms()
    state["be_active"] = False
    state["be_stop_price"] = 0.0
    state["trailing_active"] = False
    state["trailing_stop_price"] = 0.0
    state["sl_pct"] = preview["sl_pct"]
    state["tp_pct"] = preview["tp_pct"]
    state["be_trigger_pct"] = preview["be_trigger_pct"]
    state["be_lock_pct"] = preview["be_lock_pct"]
    state["trail_start_pct"] = preview["trail_start_pct"]
    state["trail_step_pct"] = preview["trail_step_pct"]
    state["exit_source"] = preview["source"]
    state["sell_fail_count"] = 0

    try:
        post_account = client.get_account()
        if live:
            _validate_live_account_snapshot(post_account, config["QUOTE_ASSET"])
    except BinanceAPIError as exc:
        _mark_reconciliation(state, "FILLED_BUY_BALANCE_UNVERIFIED", [base_asset])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "BUY %s terisi qty=%.8f tetapi saldo sesudah fill tidak dapat diverifikasi: %s. "
            "Posisi dicatat secara konservatif dan proteksi baru ditunda.",
            candidate.symbol,
            executed_qty,
            exc,
        )
        return

    confirmed_free = get_balance(post_account, base_asset)
    managed_qty = min(estimated_qty or executed_qty, confirmed_free)
    if managed_qty <= 0:
        state["pending_order"] = pending_intent
        state["current_symbol"] = None
        state["entry_price"] = 0.0
        state["qty"] = 0.0
        state["trade_accounting"] = None
        _mark_reconciliation(state, "FILLED_BUY_BASE_BALANCE_ZERO", [base_asset])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "BUY %s terisi tetapi saldo base terverifikasi nol. Intent dipertahankan.",
            candidate.symbol,
        )
        return

    state["qty"] = managed_qty
    _refresh_accounting_state(
        client, config, state, actual_remaining_qty=managed_qty
    )
    _clear_reconciliation(state)
    state_mod.save_state(config["STATE_FILE"], state)
    logger.info(
        "BUY FILLED %s: qty=%.8f managed=%.8f @ avg %.6f | 24h=%.2f%% | "
        "vol24h=%.0f | alasan: %s",
        candidate.symbol,
        executed_qty,
        managed_qty,
        fill_price,
        candidate.price_change_pct,
        candidate.quote_volume,
        candidate.confirm_reason,
    )
    logger.info(
        "%s: level exit dikunci -> %s | %s",
        candidate.symbol,
        preview["source"],
        preview["note"],
    )

    if _native_protection_enabled(config):
        if not _ensure_native_protection(client, config, filters, state):
            _mark_reconciliation(
                state, "NATIVE_PROTECTION_UNVERIFIED", [candidate.symbol]
            )
            state_mod.save_state(config["STATE_FILE"], state)


def close_position(
    client: ExchangeClient,
    config: dict,
    filters_cache: dict,
    state: dict,
    reason: str,
    reference_price: float | None = None,
) -> None:
    symbol = state.get("current_symbol")
    if not symbol:
        return
    base_asset = str(symbol)[: -len(config["QUOTE_ASSET"])]
    live = str(config.get("MODE", "PAPER")).upper() == "LIVE"

    pending = state.get("pending_order")
    if isinstance(pending, dict):
        _mark_reconciliation(state, "SELL_PENDING_ORDER_UNRESOLVED", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "SELL %s (%s) tidak dikirim karena intent %s belum terverifikasi.",
            symbol,
            reason,
            pending.get("client_order_id"),
        )
        return

    if not _cancel_native_stop_before_exit(client, config, state):
        return

    filters = filters_cache.get(symbol)
    if filters is None:
        _mark_reconciliation(state, "SELL_FILTERS_UNVERIFIED", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical("SELL %s ditahan karena filter simbol tidak tersedia.", symbol)
        return
    executable_bid = _executable_bid(client, symbol, reference_price)
    if executable_bid is None or executable_bid <= 0:
        _mark_reconciliation(state, "SELL_BID_UNVERIFIED", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "SELL %s ditahan karena bid executable tidak dapat diverifikasi.", symbol
        )
        return

    try:
        account = client.get_account()
        if live:
            _validate_live_account_snapshot(account, config["QUOTE_ASSET"])
    except BinanceAPIError as exc:
        _mark_reconciliation(state, "SELL_ACCOUNT_UNVERIFIED", [base_asset])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "SELL %s ditahan karena saldo/status akun tidak terverifikasi: %s",
            symbol,
            exc,
        )
        return

    free_base = get_balance(account, base_asset)
    locked_base = next(
        (
            float(b.get("locked", 0.0) or 0.0)
            for b in account.get("balances", [])
            if b.get("asset") == base_asset
        ),
        0.0,
    )
    if locked_base > 0:
        _mark_reconciliation(state, "SELL_BASE_BALANCE_LOCKED", [base_asset])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "SELL %s ditahan karena %.12g %s masih locked oleh order lain.",
            symbol,
            locked_base,
            base_asset,
        )
        return

    qty_to_sell = min(float(state.get("qty") or 0.0), free_base)
    if filters.max_qty > 0:
        qty_to_sell = min(qty_to_sell, float(filters.max_qty))
    qty_to_sell = filters.round_qty(qty_to_sell)
    if filters.max_notional > 0 and qty_to_sell * executable_bid > float(
        filters.max_notional
    ):
        qty_to_sell = filters.round_qty(float(filters.max_notional) / executable_bid)

    def settle_as_verified_dust(detail: str) -> None:
        logger.warning(
            "%s Posisi %s direset sebagai dust terverifikasi.", detail, symbol
        )
        _ensure_trade_accounting(state, config, str(symbol))
        _finalize_trade_accounting(
            client,
            config,
            state,
            str(symbol),
            f"DUST: {detail[:100]}",
            remaining_qty=free_base,
        )
        reset_position(state)
        state["sell_fail_count"] = 0
        state["cooldown_until"] = (
            state_mod.now_ms() + config["COOLDOWN_MINUTES_AFTER_CLOSE"] * 60 * 1000
        )
        state["last_trade_time"] = state_mod.now_ms()
        _clear_reconciliation(state)
        state_mod.save_state(config["STATE_FILE"], state)
        try_dust_sweep(client, config, symbol)

    if not filters.market_qty_valid(qty_to_sell):
        if qty_to_sell < float(filters.min_qty) and locked_base <= 0:
            settle_as_verified_dust(
                f"Qty jual {qty_to_sell:.12g} di bawah minQty {float(filters.min_qty):.12g}."
            )
        else:
            _mark_reconciliation(state, "SELL_QTY_FILTER_INVALID", [base_asset])
            state_mod.save_state(config["STATE_FILE"], state)
        return

    notional = qty_to_sell * executable_bid
    if filters.min_notional > 0 and notional < float(filters.min_notional):
        settle_as_verified_dust(
            f"Notional jual {notional:.12g} di bawah minNotional "
            f"{float(filters.min_notional):.12g}."
        )
        return
    if filters.max_notional > 0 and notional > float(filters.max_notional):
        _mark_reconciliation(state, "SELL_MAX_NOTIONAL_INVALID", [base_asset])
        state_mod.save_state(config["STATE_FILE"], state)
        return

    entry_price = float(state.get("entry_price") or 0.0)
    client_order_id = _new_client_order_id("sell")
    state["pending_order"] = {
        "side": "SELL",
        "symbol": symbol,
        "qty": qty_to_sell,
        "client_order_id": client_order_id,
        "reason": reason,
        "created_at": state_mod.now_ms(),
    }
    state_mod.save_state(config["STATE_FILE"], state)

    try:
        resp = _submit_market_order(
            client, symbol, "SELL", qty_to_sell, client_order_id
        )
    except BinanceAPIError as exc:
        state["sell_fail_count"] = int(state.get("sell_fail_count", 0)) + 1
        _mark_reconciliation(state, "SELL_SUBMISSION_UNVERIFIED", [base_asset])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "SELL %s (%s) status tidak pasti (%d): %s. Intent dipertahankan.",
            symbol,
            reason,
            state["sell_fail_count"],
            exc,
        )
        return

    executed_qty = max(0.0, float(resp.get("executedQty", 0.0) or 0.0))
    cumm_quote = max(0.0, float(resp.get("cummulativeQuoteQty", 0.0) or 0.0))
    status = str(resp.get("status") or "").upper()
    identity_valid = not live or (
        str(resp.get("symbol") or "").upper() == str(symbol).upper()
        and str(resp.get("clientOrderId") or "") == client_order_id
        and str(resp.get("side") or "").upper() == "SELL"
        and str(resp.get("type") or "").upper() == "MARKET"
    )
    if (
        not identity_valid
        or status in _NONTERMINAL_ORDER_STATUSES
        or status not in _TERMINAL_ORDER_STATUSES
    ):
        pending_now = state.get("pending_order")
        if isinstance(pending_now, dict):
            pending_now["last_status"] = status or "UNKNOWN"
            pending_now["executed_qty"] = executed_qty
            pending_now["cummulative_quote_qty"] = cumm_quote
        _mark_reconciliation(state, "SELL_RESPONSE_UNVERIFIED", [base_asset])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "SELL %s (%s) belum terminal/konsisten: status=%s filled=%.8f dari %.8f.",
            symbol,
            reason,
            status or "UNKNOWN",
            executed_qty,
            qty_to_sell,
        )
        return

    if executed_qty <= 0:
        state["pending_order"] = None
        state["sell_fail_count"] = 0
        _clear_reconciliation(state)
        state_mod.save_state(config["STATE_FILE"], state)
        logger.warning(
            "SELL %s terminal tanpa fill (status=%s). Posisi dipertahankan.",
            symbol,
            status,
        )
        return

    try:
        post_account = client.get_account()
        if live:
            _validate_live_account_snapshot(post_account, config["QUOTE_ASSET"])
    except BinanceAPIError as exc:
        pending_now = state.get("pending_order")
        if isinstance(pending_now, dict):
            pending_now["last_status"] = status
            pending_now["executed_qty"] = executed_qty
            pending_now["cummulative_quote_qty"] = cumm_quote
        _mark_reconciliation(state, "FILLED_SELL_BALANCE_UNVERIFIED", [base_asset])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "SELL %s terminal tetapi saldo setelah fill tidak dapat diverifikasi: %s. "
            "Intent dipertahankan agar tidak ada SELL duplikat.",
            symbol,
            exc,
        )
        return

    post_total = get_total_balance(post_account, base_asset)
    post_locked = next(
        (
            float(b.get("locked", 0.0) or 0.0)
            for b in post_account.get("balances", [])
            if b.get("asset") == base_asset
        ),
        0.0,
    )
    remaining = min(
        max(0.0, float(state.get("qty") or 0.0) - executed_qty),
        post_total,
    )
    accounting = _ensure_trade_accounting(state, config, str(symbol))
    if isinstance(accounting, dict):
        _attach_accounting_order(
            accounting,
            _accounting_order_record(client, resp, symbol, "SELL", config["QUOTE_ASSET"]),
        )
    state["qty"] = remaining
    state["pending_order"] = None
    _refresh_accounting_state(
        client, config, state, actual_remaining_qty=remaining
    )
    sell_price = (cumm_quote / executed_qty) if cumm_quote > 0 else 0.0
    pnl = (
        (sell_price - entry_price) * executed_qty
        if entry_price > 0 and sell_price > 0
        else 0.0
    )
    min_qty = float(filters.min_qty)
    fully_closed = (
        status in _TERMINAL_ORDER_STATUSES
        and (remaining <= 0 or remaining < min_qty)
        and post_locked <= 0
    )
    if fully_closed:
        logger.info(
            "SELL FILLED %s (%s): qty=%.8f @ avg %.6f | entry=%.6f | estimasi PnL=%.2f %s",
            symbol,
            reason,
            executed_qty,
            sell_price,
            entry_price,
            pnl,
            config["QUOTE_ASSET"],
        )
        _finalize_trade_accounting(
            client,
            config,
            state,
            str(symbol),
            reason,
            remaining_qty=remaining,
        )
        reset_position(state)
        state["sell_fail_count"] = 0
        state["cooldown_until"] = (
            state_mod.now_ms() + config["COOLDOWN_MINUTES_AFTER_CLOSE"] * 60 * 1000
        )
        state["last_trade_time"] = state_mod.now_ms()
        _clear_reconciliation(state)
        state_mod.save_state(config["STATE_FILE"], state)
        try_dust_sweep(client, config, symbol)
        return

    state["qty"] = remaining
    state["sell_fail_count"] = 0
    if post_locked > 0 or remaining <= 0:
        _mark_reconciliation(state, "SELL_REMAINDER_UNVERIFIED", [base_asset])
    else:
        _clear_reconciliation(state)
    state_mod.save_state(config["STATE_FILE"], state)
    logger.critical(
        "SELL PARTIAL/TERMINAL %s (%s): status=%s filled=%.8f dari %.8f, "
        "sisa state=%.8f. Posisi tidak direset.",
        symbol,
        reason,
        status,
        executed_qty,
        qty_to_sell,
        remaining,
    )


def check_manual_control(
    client: ExchangeClient, config: dict, filters_cache: dict, state: dict
) -> None:
    control_path = config.get("CONTROL_FILE") or get_control_file(config)
    cmd = state_mod.load_control(control_path)
    if not cmd:
        return

    requested_at = int(cmd.get("requested_at", 0) or 0)
    age_sec = (state_mod.now_ms() - requested_at) / 1000.0
    MAX_AGE_SECONDS = 120
    if requested_at <= 0 or age_sec > MAX_AGE_SECONDS:
        logger.warning(
            "Perintah manual dari dashboard diabaikan (kadaluarsa, umur %.0f detik): %s",
            age_sec,
            cmd,
        )
        state_mod.clear_control(control_path)
        return

    action = cmd.get("action")
    if action != "CLOSE_POSITION":
        logger.warning("Perintah manual dari dashboard tidak dikenali: %s", cmd)
        state_mod.clear_control(control_path)
        return

    state_mod.clear_control(control_path)

    if not state["current_symbol"] or state["qty"] <= 0:
        logger.info(
            "Perintah 'Jual Sekarang' dari dashboard diabaikan: tidak ada posisi terbuka saat ini."
        )
        return

    requested_symbol = cmd.get("symbol")
    if requested_symbol and requested_symbol != state["current_symbol"]:
        logger.warning(
            "Perintah 'Jual Sekarang' dari dashboard diabaikan: diminta untuk %s, "
            "tapi posisi saat ini adalah %s (kemungkinan posisi sudah berganti "
            "sejak tombol diklik).",
            requested_symbol,
            state["current_symbol"],
        )
        return

    logger.info(
        "Perintah 'Jual Sekarang' diterima dari dashboard untuk %s. Menutup posisi...",
        state["current_symbol"],
    )
    close_position(client, config, filters_cache, state, "MANUAL_CLOSE_DASHBOARD")


_MAX_HOLD_CACHE: dict = {}
_MAX_HOLD_WARNED: set = set()


def _max_hold_cap_ms(config: dict) -> int:
    raw = config.get("MAX_HOLD_HOURS")
    interval = str(config.get("CONFIRM_INTERVAL", "5m") or "5m")
    kunci = (repr(raw), interval)
    cached = _MAX_HOLD_CACHE.get(kunci)
    if cached is not None:
        return cached
    try:
        cap_ms = strategy.max_hold_ms(config, interval)
    except ValueError as exc:
        cap_ms = 0
        if kunci not in _MAX_HOLD_WARNED:
            _MAX_HOLD_WARNED.add(kunci)
            logger.warning("Time stop nonaktif karena konfigurasi: %s", exc)
    _MAX_HOLD_CACHE[kunci] = cap_ms
    return cap_ms


def _time_stop_due(config: dict, state: dict) -> bool:
    cap_ms = _max_hold_cap_ms(config)
    if cap_ms <= 0:
        return False
    entry_ms = int(state.get("entry_time") or 0)
    if entry_ms <= 0:
        return False
    bar_ms = strategy.interval_to_ms(
        str(config.get("CONFIRM_INTERVAL", "5m") or "5m")
    )
    return strategy.time_stop_due(entry_ms, state_mod.now_ms(), cap_ms, bar_ms)


def manage_exit(
    client: ExchangeClient,
    config: dict,
    filters_cache: dict,
    state: dict,
    current_price: float,
) -> None:
    if not state["current_symbol"] or state["qty"] <= 0 or state["entry_price"] <= 0:
        return

    if _native_protection_enabled(config):
        if isinstance(state.get("native_oco"), dict):
            if not _reconcile_native_oco(client, config, state):
                if not state.get("current_symbol") or state.get(
                    "_native_stop_exit_blocked"
                ):
                    return
        if isinstance(state.get("native_stop"), dict):
            if not _reconcile_native_stop(client, config, state):
                if not state.get("current_symbol") or state.get(
                    "_native_stop_exit_blocked"
                ):
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
    be_trigger = _exit_distance(
        state, config, "be_trigger_pct", "BE_TRIGGER_PCT", atr_mode, entry
    )
    be_lock = _exit_distance(
        state, config, "be_lock_pct", "BE_LOCK_PCT", atr_mode, entry
    )
    trail_start = _exit_distance(
        state, config, "trail_start_pct", "TRAILING_START_PCT", atr_mode, entry
    )
    trail_step = _exit_distance(
        state, config, "trail_step_pct", "TRAILING_STEP_PCT", atr_mode, entry
    )
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
        close_position(
            client,
            config,
            filters_cache,
            state,
            "STOP_LOSS",
            reference_price=current_price,
        )
        return
    if config["USE_BREAKEVEN"] and not state["be_active"] and be_trigger_hit:
        state["be_active"] = True
        state["be_stop_price"] = (
            state["entry_price"] + be_lock
            if atr_mode
            else state["entry_price"] * (1 + be_lock / 100.0)
        )
    if config["USE_TRAILING"]:
        if not state["trailing_active"] and trail_start_hit:
            state["trailing_active"] = True
            state["trailing_stop_price"] = (
                current_price - trail_step
                if atr_mode
                else current_price * (1 - trail_step / 100.0)
            )
        elif state["trailing_active"]:
            candidate_stop = (
                current_price - trail_step
                if atr_mode
                else current_price * (1 - trail_step / 100.0)
            )
            if candidate_stop > state["trailing_stop_price"]:
                state["trailing_stop_price"] = candidate_stop
    reasons = []
    if config["USE_TP"] and tp_hit:
        reasons.append("TAKE_PROFIT")
    if state["be_active"] and current_price <= state["be_stop_price"]:
        reasons.append("BREAKEVEN")
    if state["trailing_active"] and current_price <= state["trailing_stop_price"]:
        reasons.append("TRAILING_STOP")
    if not reasons and _time_stop_due(config, state):
        reasons.append(strategy.TIME_STOP_REASON)
    if not reasons:
        _maybe_amend_native_protection(
            client,
            config,
            filters_cache.get(str(state.get("current_symbol") or "")),
            state,
            current_price,
        )
        if state.get("_native_stop_exit_blocked"):
            return
    if reasons:
        close_position(
            client,
            config,
            filters_cache,
            state,
            "+".join(reasons),
            reference_price=current_price,
        )


def should_log_scan_diagnostics(
    diagnostics: scanner.ScanDiagnostics,
    previous_signature: tuple | None,
    last_logged_ms: int,
    now_ms: int,
) -> bool:
    return bool(
        diagnostics.checked_candidates
        and (
            previous_signature != diagnostics.signature()
            or int(now_ms) < int(last_logged_ms)
            or int(now_ms) - int(last_logged_ms) >= 300_000
        )
    )


def run(
    config: dict,
    lifecycle=None,
    *,
    allow_missing_live_state: bool = False,
) -> int:
    global _shutdown_requested
    _shutdown_requested = False
    _shutdown_event.clear()
    setup_logging(config)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    if os.name == "nt" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_signal)

    if CONFIG_LOAD_ERRORS:
        logger.critical(
            "Konfigurasi runtime tidak aman untuk dipakai: %s",
            "; ".join(CONFIG_LOAD_ERRORS),
        )
        return 2

    from config.config import InvalidModeError

    try:
        mode = require_valid_mode(config)
    except InvalidModeError as exc:
        logger.critical(str(exc))
        return 2
    base_url = get_base_url(config)

    if mode == "LIVE" and (
        not str(config.get("API_KEY") or "").strip()
        or not str(config.get("API_SECRET") or "").strip()
    ):
        logger.error(
            "API key/secret belum di-set. Mode LIVE mengirim order dengan UANG ASLI, "
            "jadi kredensial produksi wajib ada di file .env. Bot dihentikan. "
            "(Mode PAPER tidak memerlukan API key.)"
        )
        return 1
    if (
        mode == "LIVE"
        and not allow_missing_live_state
        and not os.path.exists(config["STATE_FILE"])
    ):
        logger.critical(
            "File state LIVE tidak ditemukan. Startup dihentikan sebelum koneksi bursa. "
            "Konfirmasi eksplisit melalui dashboard diperlukan untuk inisialisasi baru."
        )
        return 2

    logger.info("=" * 70)
    logger.info(
        "Pump Scanner Bot mulai berjalan. MODE=%s | endpoint=%s", mode, base_url
    )
    logger.info(
        "File data (otomatis per mode) -> state=%s | log=%s | kontrol=%s",
        config["STATE_FILE"],
        config["LOG_FILE"],
        config.get("CONTROL_FILE", "-"),
    )
    if mode == "PAPER":
        logger.warning(
            "MODE PAPER AKTIF: eksekusi order, fee, dan saldo DISIMULASIKAN lokal. "
            "Data pasar tetap ASLI dari Binance produksi publik (REST + WebSocket), tanpa API key. "
            "Tidak ada order sungguhan yang dikirim. Hasil PAPER BUKAN jaminan hasil LIVE."
        )
    else:
        logger.warning("MODE LIVE AKTIF: order memakai UANG ASLI di Binance produksi.")

    block_hours = float(config.get("SAME_COIN_BLOCK_HOURS", 0) or 0)
    if block_hours > 0:
        aturan = (
            "hanya trade loss yang memicu blokir"
            if config.get("SAME_COIN_BLOCK_LOSS_ONLY", True)
            else "semua trade (profit dan loss) memicu blokir"
        )
        logger.info(
            "Blokir koin sama AKTIF: koin yang posisinya baru ditutup tidak boleh "
            "di-entry lagi selama %.1f jam; %s.",
            block_hours,
            aturan,
        )

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
            "gerbang entry M5. Bot tetap mengambil %d candle per konfirmasi agar deteksi "
            "tidak selalu gagal, tetapi perbaiki nilai config ini supaya backtest dan live "
            "benar-benar memakai angka yang sama.",
            have_bars,
            need_setup,
            strategy.confirm_window_bars(config),
        )
    if config.get("DIRECTION_FILTER_ENABLED", False):
        butuh_arah = strategy.direction_required_bars(config)
        jendela_arah = strategy.direction_window_bars(config)
        if int(config.get("DIRECTION_LOOKBACK_BARS", 0) or 0) < butuh_arah:
            logger.warning(
                "DIRECTION_LOOKBACK_BARS=%s lebih kecil dari %d candle yang dibutuhkan EMA "
                "dan ADX pada %s. Gerbang arah otomatis memakai %d candle, dan kandidat "
                "tetap ditolak selama riwayat belum cukup. Perbaiki nilai config ini "
                "supaya live dan backtest memakai jendela yang sama.",
                config.get("DIRECTION_LOOKBACK_BARS"),
                butuh_arah,
                strategy.direction_interval(config),
                jendela_arah,
            )
    if (
        config.get("DIRECTION_FILTER_ENABLED", False)
        or config.get("DIRECTION_ZONE_FILTER_ENABLED", False)
    ):
        try:
            strategy.gate_warmup_bars(config, config["CONFIRM_INTERVAL"])
        except ValueError as exc:
            logger.warning(
                "%s Backtest akan menolak kombinasi ini sampai diperbaiki.", exc
            )
    if config.get("DIRECTION_FILTER_ENABLED", False):
        logger.info(
            "Layer arah AKTIF (%s): entry lolos hanya kalau candle %s terakhir yang sudah "
            "tutup memenuhi close > EMA%d, EMA%d > EMA%d, dan ADX%d >= %g. Kalau candle arah "
            "gagal diambil atau riwayatnya kurang, kandidat DITOLAK (fail closed).",
            config.get("DIRECTION_INTERVAL", "15m"),
            config.get("DIRECTION_INTERVAL", "15m"),
            int(config.get("DIRECTION_EMA_FAST", 20) or 20),
            int(config.get("DIRECTION_EMA_FAST", 20) or 20),
            int(config.get("DIRECTION_EMA_SLOW", 50) or 50),
            int(config.get("DIRECTION_ADX_PERIOD", 14) or 14),
            float(config.get("DIRECTION_ADX_MIN", 0.0) or 0.0),
        )
    else:
        logger.info(
            "Layer arah NONAKTIF: entry tidak memakai arah M15 sebagai penyaring.",
        )
    if config.get("DIRECTION_ZONE_FILTER_ENABLED", False):
        logger.info(
            "Layer zona demand %s AKTIF: entry hanya boleh kalau harga %s zona demand "
            "%s (lookback %d candle, tebal zona min %.2f%%, %s, close_pos min %.2f). "
            "Memakai seri candle arah yang sama, tanpa unduhan tambahan.",
            config.get("DIRECTION_INTERVAL", "15m"),
            "wajib berada di dalam"
            if config.get("DIRECTION_ZONE_REQUIRE_INSIDE_ZONE", False)
            else "masih di dalam atau dekat",
            config.get("DIRECTION_INTERVAL", "15m"),
            strategy.direction_zone_lookback_bars(config),
            float(config.get("DIRECTION_ZONE_BUFFER_PCT", 1.2) or 0.0),
            "jarak maks %.2f%% di atas atap zona"
            % float(config.get("DIRECTION_ZONE_MAX_DISTANCE_PCT", 7.0) or 0.0),
            float(config.get("DIRECTION_ZONE_MIN_CLOSE_POSITION", 0.35) or 0.0),
        )
    if config.get("TIMING_FILTER_ENABLED", False):
        logger.info(
            "Layer timing AKTIF (%s): entry lolos hanya kalau candle %s terakhir yang sudah "
            "tutup memenuhi close > EMA%d, EMA%d > EMA%d, dan ADX%d >= %g. Memakai seri "
            "konfirmasi yang sama dengan breakout-retest %s.",
            config.get("CONFIRM_INTERVAL", "5m"),
            config.get("CONFIRM_INTERVAL", "5m"),
            int(config.get("TIMING_EMA_FAST", 20) or 20),
            int(config.get("TIMING_EMA_FAST", 20) or 20),
            int(config.get("TIMING_EMA_SLOW", 50) or 50),
            int(config.get("TIMING_ADX_PERIOD", 14) or 14),
            float(config.get("TIMING_ADX_MIN", 0.0) or 0.0),
            config.get("CONFIRM_INTERVAL", "5m"),
        )
    else:
        logger.info(
            "Layer timing %s NONAKTIF: tidak ada pemeriksaan tren mikro.",
            config.get("CONFIRM_INTERVAL", "5m"),
        )
    if config.get("BREAKOUT_RETEST_FILTER_ENABLED", False):
        logger.info(
            "Breakout-retest AKTIF (%s): swing high lookback %d, pivot %d/%d, retest "
            "maksimum %d candle, toleransi %.2f ATR, maksimum %d sentuhan, bullish=%s.",
            config.get("CONFIRM_INTERVAL", "5m"),
            int(config.get("BREAKOUT_LOOKBACK_BARS", 30) or 30),
            int(config.get("BREAKOUT_PIVOT_WING_BARS", 2) or 2),
            int(config.get("BREAKOUT_PIVOT_WING_BARS", 2) or 2),
            int(config.get("BREAKOUT_RETEST_MAX_BARS", 12) or 12),
            float(config.get("BREAKOUT_RETEST_TOLERANCE_ATR", 0.2) or 0.0),
            int(config.get("BREAKOUT_RETEST_MAX_TOUCHES", 2) or 2),
            bool(config.get("BREAKOUT_RETEST_REQUIRE_BULLISH", True)),
        )
    else:
        logger.info("Breakout-retest %s NONAKTIF.", config.get("CONFIRM_INTERVAL", "5m"))
    logger.info("=" * 70)

    client = None
    try:
        client = create_exchange_client(config)
        client.sync_time()

        logger.info("Mengambil exchangeInfo untuk semua simbol (sekali di awal)...")
        exchange_info = client.get_exchange_info()
        if not isinstance(exchange_info, dict) or not isinstance(
            exchange_info.get("symbols"), list
        ):
            raise BinanceAPIError(
                502, None, "exchangeInfo tidak memiliki daftar symbols"
            )
        filters_cache = build_filters_cache(exchange_info)
        account_permissions = None
        if mode == "LIVE":
            startup_account = client.get_account()
            _validate_live_account_snapshot(startup_account, config["QUOTE_ASSET"])
            account_permissions = _effective_account_permissions(startup_account)
        tradable_symbols = build_trading_symbols(
            exchange_info, account_permissions=account_permissions
        )
        if not filters_cache or not tradable_symbols:
            raise BinanceAPIError(
                502,
                None,
                "filter atau daftar simbol TRADING yang cocok dengan permission akun "
                "kosong; metadata exchange tidak terverifikasi",
            )
        filters_cache_time = time.time()
        logger.info(
            "Filter tervalidasi untuk %d simbol berhasil dimuat; %d simbol TRADING "
            "cocok dengan permission efektif akun.",
            len(filters_cache),
            len(tradable_symbols),
        )

        state = load_pump_state(
            config["STATE_FILE"],
            fail_closed=True,
            allow_missing_state=(mode == "LIVE" and allow_missing_live_state),
            mode=mode,
        )
        if (
            mode == "LIVE"
            and not allow_missing_live_state
            and state.get("state_integrity_error") == state_mod.STATE_FILE_MISSING
        ):
            raise RuntimeError(
                "File state LIVE hilang saat startup; tidak menulis state default "
                "tanpa konfirmasi operator."
            )
        held_symbol = state.get("current_symbol")
        if held_symbol and held_symbol not in filters_cache:
            _mark_reconciliation(state, "HELD_SYMBOL_FILTERS_UNVERIFIED", [held_symbol])
            state_mod.save_state(config["STATE_FILE"], state)
        logger.info("Mode exit efektif: %s", describe_exit_mode(config, state))
        if held_symbol:
            logger.info(
                "Melanjutkan posisi yang sudah ada: %s qty=%.8f @ %.6f",
                held_symbol,
                state["qty"],
                state["entry_price"],
            )
        reconcile_state_with_exchange(
            client,
            config,
            state,
            filters_cache,
            check_open_orders=(mode == "LIVE"),
        )
    except Exception as exc:
        logger.exception(
            "Startup gagal karena waktu, akun, state, atau metadata exchange tidak dapat "
            "diverifikasi. Bot berhenti fail-closed: %s",
            exc,
        )
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if lifecycle is not None:
            lifecycle.write("STOPPED", reason="Startup LIVE/PAPER gagal diverifikasi.")
        return 1

    if lifecycle is not None:
        lifecycle.write("RUNNING")

    consecutive_errors = 0
    last_time_sync = time.time()
    last_heartbeat = 0.0
    last_strategy_init_warning = (None, 0.0)
    last_reconciliation = time.time()
    last_scan_diagnostic_signature = None
    last_scan_diagnostic_logged_ms = 0
    TIME_SYNC_INTERVAL_SECONDS = 15 * 60
    FILTERS_REFRESH_INTERVAL_SECONDS = 6 * 3600
    RECONCILIATION_INTERVAL_SECONDS = 60

    def konfirmasi_dari_raw(raw):
        lookback = strategy.confirm_window_bars(config)
        now_ms = state_mod.now_ms()
        closed = [k for k in strategy.parse_klines(raw or []) if k.close_time < now_ms]
        return closed[-lookback:]

    def klines_fetcher(symbol: str):
        lookback = strategy.confirm_window_bars(config)
        raw = client.get_klines(symbol, config["CONFIRM_INTERVAL"], limit=lookback + 1)
        return konfirmasi_dari_raw(raw)

    def klines_fetcher_many(symbols):
        lookback = strategy.confirm_window_bars(config)
        raw_map = (
            client.get_klines_many(
                symbols, config["CONFIRM_INTERVAL"], limit=lookback + 1
            )
            or {}
        )
        return {
            simbol: (konfirmasi_dari_raw(raw) if raw is not None else None)
            for simbol, raw in raw_map.items()
        }

    direction_cache = DirectionCache(client, config)
    butuh_candle_arah = bool(
        config.get("DIRECTION_FILTER_ENABLED", False)
    ) or bool(config.get("DIRECTION_ZONE_FILTER_ENABLED", False))
    direction_provider = direction_cache.provider if butuh_candle_arah else None
    htf_cache = HtfTrendCache(client, config) if strategy.htf_enabled(config) else None
    htf_provider = htf_cache.provider if htf_cache is not None else None
    if htf_provider is not None:
        logger.info(
            "Gerbang tren HTF %s AKTIF: close > EMA%d > EMA%d dan ADX%d >= %g "
            "(jendela %d candle tertutup; candle berjalan dikecualikan; jika data "
            "gagal/kurang, entry ditolak demi keamanan; hasil per simbol di-cache).",
            strategy.htf_interval(config),
            int(config.get("HTF_EMA_FAST", 20)),
            int(config.get("HTF_EMA_SLOW", 50)),
            int(config.get("HTF_ADX_PERIOD", 14)),
            float(config.get("HTF_ADX_MIN", 30.0) or 0.0),
            strategy.htf_window_bars(config),
        )
    if direction_provider is None:
        logger.warning(
            "Layer arah NONAKTIF (DIRECTION_FILTER_ENABLED=False). "
            "Entry tidak pernah memakai arah M15 sebagai penyaring.",
        )
    else:
        logger.info(
            "Layer arah %s AKTIF: close > EMA%d > EMA%d dan ADX%d >= %g "
            "(jendela %d candle tertutup, fail closed bila data gagal diambil).",
            strategy.direction_interval(config),
            int(config.get("DIRECTION_EMA_FAST", 20)),
            int(config.get("DIRECTION_EMA_SLOW", 50)),
            int(config.get("DIRECTION_ADX_PERIOD", 14)),
            float(config.get("DIRECTION_ADX_MIN", 32.0) or 0.0),
            strategy.direction_window_bars(config),
        )
    if bool(config.get("DIRECTION_ZONE_FILTER_ENABLED", False)):
        logger.info(
            "Layer zona demand %s AKTIF: close wajib di atas dasar zona%s, candle sinyal "
            "bullish, close_pos >= %.2f, %s yang dipetakan dari %d candle %s tertutup "
            "(fail closed bila data gagal diambil).",
            strategy.direction_interval(config),
            " dan tidak boleh di atas atap zona"
            if config.get("DIRECTION_ZONE_REQUIRE_INSIDE_ZONE", False)
            else "",
            float(config.get("DIRECTION_ZONE_MIN_CLOSE_POSITION", 0.35) or 0.0),
            "tanpa toleransi jarak"
            if config.get("DIRECTION_ZONE_REQUIRE_INSIDE_ZONE", False)
            else (
                "jarak maks %.2f%% di atas atap zona"
                % float(config.get("DIRECTION_ZONE_MAX_DISTANCE_PCT", 7.0) or 0.0)
            ),
            strategy.direction_zone_lookback_bars(config),
            strategy.direction_interval(config),
        )
    if not bool(config.get("TIMING_FILTER_ENABLED", False)):
        logger.info(
            "Layer timing %s NONAKTIF: tidak ada pemeriksaan tren mikro M5.",
            strategy.timing_interval(config),
        )
    else:
        logger.info(
            "Layer timing %s AKTIF: close > EMA%d > EMA%d dan ADX%d >= %g "
            "(jendela %d candle tertutup; memakai seri konfirmasi yang sama "
            "dengan breakout-retest M5).",
            strategy.timing_interval(config),
            int(config.get("TIMING_EMA_FAST", 20)),
            int(config.get("TIMING_EMA_SLOW", 50)),
            int(config.get("TIMING_ADX_PERIOD", 14)),
            float(config.get("TIMING_ADX_MIN", 18.0) or 0.0),
            strategy.timing_window_bars(config),
        )

    exit_code = 0
    while not _shutdown_requested:
        loop_start = time.time()
        try:
            if state_mod.consume_stop_request(config["CONTROL_FILE"]):
                _shutdown_requested = True
                _shutdown_event.set()
                if lifecycle is not None:
                    lifecycle.write(
                        "STOPPING", reason="Permintaan stop dari dashboard."
                    )
                state_mod.save_state(config["STATE_FILE"], state)
                break

            if time.time() - last_time_sync > TIME_SYNC_INTERVAL_SECONDS:
                client.sync_time()
                last_time_sync = time.time()

            if time.time() - filters_cache_time > FILTERS_REFRESH_INTERVAL_SECONDS:
                refreshed_info = client.get_exchange_info()
                refreshed_filters = build_filters_cache(refreshed_info)
                refreshed_permissions = None
                if mode == "LIVE":
                    refreshed_account = client.get_account()
                    _validate_live_account_snapshot(
                        refreshed_account, config["QUOTE_ASSET"]
                    )
                    refreshed_permissions = _effective_account_permissions(
                        refreshed_account
                    )
                refreshed_tradable = build_trading_symbols(
                    refreshed_info, account_permissions=refreshed_permissions
                )
                if not refreshed_filters or not refreshed_tradable:
                    raise BinanceAPIError(
                        502,
                        None,
                        "refresh exchangeInfo menghasilkan filter/simbol TRADING yang "
                        "cocok dengan permission akun dalam keadaan kosong",
                    )
                filters_cache = refreshed_filters
                tradable_symbols = refreshed_tradable
                filters_cache_time = time.time()
                logger.info(
                    "Filter simbol disegarkan ulang (%d filter, %d cocok permission akun).",
                    len(filters_cache),
                    len(tradable_symbols),
                )
                held = state.get("current_symbol")
                if held and (held not in tradable_symbols or held not in filters_cache):
                    _mark_reconciliation(state, "HELD_SYMBOL_NOT_TRADABLE", [held])
                    state_mod.save_state(config["STATE_FILE"], state)
                    logger.critical(
                        "Simbol %s yang sedang dipegang tidak lagi TRADING atau filternya "
                        "tidak valid. Entry baru diblokir dan exit akan tetap dicoba bila bursa menerima.",
                        held,
                    )

            accounting_retry_due = (
                bool(state.get("accounting_blocked"))
                and state_mod.now_ms()
                >= int(state.get("accounting_retry_after_ms", 0) or 0)
            )
            if (
                state.get("pending_order")
                or accounting_retry_due
                or (
                    state.get("reconciliation_required")
                    and time.time() - last_reconciliation >= RECONCILIATION_INTERVAL_SECONDS
                )
            ):
                pending_state = state.get("pending_order") or {}
                logger.warning(
                    "Menjalankan rekonsiliasi LIVE/PAPER (intent=%s, alasan=%s).",
                    pending_state.get("client_order_id", "-"),
                    state.get("reconciliation_reason") or "-",
                )
                reconcile_state_with_exchange(
                    client,
                    config,
                    state,
                    filters_cache,
                    check_open_orders=(mode == "LIVE"),
                )
                last_reconciliation = time.time()

            check_manual_control(client, config, filters_cache, state)

            current_price = None
            if state["current_symbol"]:
                book = client.get_book_ticker(state["current_symbol"], max_retries=1)
                current_price = float(book.get("bidPrice", 0.0))
                if current_price <= 0:
                    raise BinanceAPIError(
                        502,
                        None,
                        f"bid executable {state['current_symbol']} tidak valid",
                    )

            if state["current_symbol"] and current_price is not None:
                manage_exit(client, config, filters_cache, state, current_price)

            equity_price = current_price if state.get("current_symbol") else None
            equity_snapshot = collect_equity(
                client, config, state, position_price=equity_price
            )
            strategy_initial_snapshot = equity_snapshot if not state.get("current_symbol") and not state.get("qty") else None
            equity = equity_snapshot["equity"]
            if equity is None:
                entries_paused = True
                reconciliation_reason = (
                    "STATE_INTEGRITY_UNVERIFIED"
                    if state.get("state_integrity_error")
                    else "EQUITY_UNVERIFIED"
                )
                _mark_reconciliation(state, reconciliation_reason)
            elif state.get("state_integrity_error"):
                entries_paused = True
                _mark_reconciliation(state, "STATE_INTEGRITY_UNVERIFIED")
            else:
                entries_paused = update_equity_controls(state, equity, config)

            equity_snapshot["risk_snapshot"] = account_risk_snapshot(equity, state)
            try:
                publish_equity(config, equity_snapshot)
            except (OSError, ValueError, TypeError):
                logger.warning(
                    "Snapshot equity dashboard gagal disimpan; kontrol risiko tetap memakai snapshot saat ini.",
                    exc_info=True,
                )

            maybe_force_close_at_risk_limit(
                client, config, filters_cache, state, entries_paused, current_price
            )

            strategy_period = strategy_dd.auto_entry_gate(client, config, state, strategy_initial_snapshot)
            strategy_period_ready = strategy_period["ready"]
            if strategy_period.get("created"):
                logger.info(
                    "DD STRATEGI AUTO: modal awal=%s %s | periode=%s | entry belum dikirim.",
                    strategy_period["initial_capital"], config["QUOTE_ASSET"], strategy_period["period_id"],
                )
            if not strategy_period_ready:
                reason = strategy_period["reason"]
                previous_reason, previous_time = last_strategy_init_warning
                if reason != previous_reason or time.time() - previous_time >= 30.0:
                    logger.warning("Entry baru menunggu periode DD strategi: %s. Pengelolaan exit tetap berjalan.", reason)
                    last_strategy_init_warning = (reason, time.time())

            do_scan = (
                time.time() * 1000 - state.get("last_scan_time", 0)
                > config["MARKET_SCAN_INTERVAL_SECONDS"] * 1000
            )
            if do_scan:
                state["last_scan_time"] = state_mod.now_ms()
                tickers = client.get_ticker_24hr_all()

                now = state_mod.now_ms()
                can_enter = (
                    strategy_period_ready
                    and not state["current_symbol"]
                    and not state.get("pending_order")
                    and not state.get("reconciliation_required")
                    and not (mode == "LIVE" and state.get("accounting_blocked"))
                    and now >= state.get("cooldown_until", 0)
                    and not entries_paused
                    and now - state.get("last_trade_time", 0)
                    >= config["MIN_SECONDS_BETWEEN_TRADES"] * 1000
                )
                if can_enter:
                    scan_config = dict(config)
                    diblokir = set()
                    blocks = state.get("symbol_block_until")
                    if isinstance(blocks, dict):
                        for sym in blocks:
                            if _symbol_block_until_ts(state, sym) > now:
                                diblokir.add(str(sym).upper())
                    if diblokir:
                        scan_config["_blocked_symbols"] = diblokir
                        logger.debug(
                            "Scan: koin diblokir karena trade sebelumnya loss: %s",
                            ", ".join(sorted(diblokir)),
                        )
                    if config.get("BTC_FILTER_ENABLED", False):
                        scan_config["_btc_filter_fail_closed"] = True
                        try:
                            btc_raw = client.get_klines(
                                "BTC" + config["QUOTE_ASSET"],
                                config["CONFIRM_INTERVAL"],
                                limit=int(config.get("BTC_LOOKBACK_BARS", 3) or 3) + 1,
                            )
                            btc_closed = [
                                k
                                for k in strategy.parse_klines(btc_raw)
                                if k.close_time < state_mod.now_ms()
                            ]
                            look = int(config.get("BTC_LOOKBACK_BARS", 3) or 3)
                            if len(btc_closed) >= look + 1:
                                scan_config["_btc_drop_pct"] = (
                                    btc_closed[-1].close / btc_closed[-look - 1].close
                                    - 1.0
                                ) * 100.0
                        except Exception as exc:
                            logger.warning(
                                "Filter BTC tidak dapat dihitung; kandidat ditolak: %s",
                                exc,
                            )
                    try:
                        batas_konfirmasi = strategy.last_closed_bar_close_ms(
                            str(scan_config.get("CONFIRM_INTERVAL", "5m")),
                            state_mod.now_ms(),
                        )
                    except (KeyError, ValueError):
                        batas_konfirmasi = None
                    scan_diagnostics = scanner.ScanDiagnostics()
                    best = scanner.find_best_candidate(
                        tickers,
                        klines_fetcher,
                        scan_config,
                        tradable_symbols,
                        klines_fetcher_many=klines_fetcher_many,
                        prewarm_fn=client.prewarm_book_ticker,
                        direction_provider=direction_provider,
                        diagnostics=scan_diagnostics,
                        confirm_close_time_ms=batas_konfirmasi,
                        htf_provider=htf_provider,
                    )
                    if best:
                        book = client.get_book_ticker(best.symbol)
                        bid, ask = float(book["bidPrice"]), float(book["askPrice"])
                        spread_pct = scanner.spread_pct_from_book(bid, ask)
                        max_chase = float(config.get("MAX_CHASE_PCT", 0) or 0)
                        signal_close = (
                            float(best.setup.signal_close or 0.0)
                            if (best.setup is not None and best.setup.signal_close)
                            else 0.0
                        )
                        chase_ok = True
                        if (
                            max_chase > 0
                            and signal_close > 0
                            and ask > signal_close * (1.0 + max_chase / 100.0)
                        ):
                            chase_ok = False
                            logger.info(
                                "Kandidat %s dilewati: ask %.8f sudah %+.2f%% di atas "
                                "close candle sinyal %.8f (batas MAX_CHASE_PCT %.2f%%).",
                                best.symbol,
                                ask,
                                (ask / signal_close - 1.0) * 100.0,
                                signal_close,
                                max_chase,
                            )
                        if spread_pct <= config["MAX_SPREAD_PCT"] and chase_ok:
                            trend_info = ""
                            if best.htf_trend is not None:
                                nilai = best.htf_trend.get("values") or {}
                                trend_info = " | tren HTF %s: %s" % (
                                    best.htf_trend.get("interval")
                                    or strategy.htf_interval(config),
                                    best.htf_trend.get("reason"),
                                )
                                if nilai.get("adx") is not None:
                                    trend_info += " (ADX %.1f)" % float(nilai["adx"])
                            if best.direction is not None:
                                nilai = best.direction.get("values") or {}
                                trend_info += " | arah %s: %s" % (
                                    best.direction.get("interval")
                                    or strategy.direction_interval(config),
                                    best.direction.get("reason"),
                                )
                                if nilai.get("adx") is not None:
                                    trend_info += " (ADX %.1f)" % float(nilai["adx"])
                            if best.direction_zone is not None:
                                trend_info += " | zona demand %s: %s" % (
                                    best.direction_zone.get("interval")
                                    or strategy.direction_interval(config),
                                    best.direction_zone.get("reason"),
                                )
                            if best.timing is not None:
                                nilai = best.timing.get("values") or {}
                                trend_info += " | timing %s: %s" % (
                                    best.timing.get("interval")
                                    or strategy.timing_interval(config),
                                    best.timing.get("reason"),
                                )
                                if nilai.get("adx") is not None:
                                    trend_info += " (ADX %.1f)" % float(nilai["adx"])
                            logger.info(
                                "Kandidat terpilih: %s (vol24h=%.0f, 24h=%.2f%%, spread=%.3f%%) | %s%s",
                                best.symbol,
                                best.quote_volume,
                                best.price_change_pct,
                                spread_pct,
                                best.confirm_reason,
                                trend_info,
                            )
                            min_age = float(config.get("MIN_LISTING_AGE_DAYS", 0) or 0)
                            if min_age > 0:
                                try:
                                    age = listing_age_days(
                                        client, best.symbol, state_mod.now_ms()
                                    )
                                except BinanceAPIError as exc:
                                    logger.warning(
                                        "Usia listing %s tidak bisa diverifikasi (%s). "
                                        "Entry dilewati demi keamanan.",
                                        best.symbol,
                                        exc,
                                    )
                                    continue_scan_entry = False
                                    age = None
                                else:
                                    continue_scan_entry = True
                                if age is not None and age < min_age:
                                    logger.info(
                                        "Kandidat %s dilewati: baru listing %.1f hari "
                                        "(batas minimal %.0f hari).",
                                        best.symbol,
                                        age,
                                        min_age,
                                    )
                                    continue_scan_entry = False
                            else:
                                continue_scan_entry = True
                            if continue_scan_entry:
                                open_position(
                                    client,
                                    config,
                                    filters_cache,
                                    state,
                                    best,
                                    reference_price=ask,
                                )
                        else:
                            logger.info(
                                "Kandidat %s dilewati: spread %.3f%% > batas %.3f%%.",
                                best.symbol,
                                spread_pct,
                                config["MAX_SPREAD_PCT"],
                            )
                    if not best:
                        logger.info(
                            "Tidak ada kandidat entry pada scan ini (%d diperiksa dari "
                            "%d yang lolos filter pasar).",
                            scan_diagnostics.checked_candidates,
                            scan_diagnostics.market_candidates,
                        )
                    htf_rejection_logged = (
                        strategy.htf_enabled(scan_config)
                        and scan_diagnostics.rejection_counts.get("HTF_TREND", 0) > 0
                    )
                    if not best or htf_rejection_logged:
                        diagnostic_now_ms = state_mod.now_ms()
                        if should_log_scan_diagnostics(
                            scan_diagnostics,
                            last_scan_diagnostic_signature,
                            last_scan_diagnostic_logged_ms,
                            diagnostic_now_ms,
                        ):
                            for line in scan_diagnostics.log_lines(scan_config):
                                logger.info("%s", line)
                            last_scan_diagnostic_signature = scan_diagnostics.signature()
                            last_scan_diagnostic_logged_ms = diagnostic_now_ms

            if time.time() - last_heartbeat >= config["HEARTBEAT_INTERVAL_SECONDS"]:
                last_heartbeat = time.time()
                if state["current_symbol"]:
                    pnl_pct = (
                        (current_price / state["entry_price"] - 1.0) * 100.0
                        if current_price
                        else 0.0
                    )
                    posisi_info = (
                        f"pegang {state['current_symbol']} (PnL={pnl_pct:+.2f}%)"
                    )
                else:
                    posisi_info = "tidak ada posisi"
                flags = []
                if state.get("dd_stopped"):
                    flags.append("DD-STOP")
                flag_str = f" | status: {', '.join(flags)}" if flags else ""
                equity_str = (
                    f"{equity:.2f}"
                    if equity is not None
                    else "n/a (API harga gangguan)"
                )
                logger.info(
                    "[HEARTBEAT] Bot masih berjalan | equity=%s %s | %s%s | snapshot=%s | captured_at_ms=%s",
                    equity_str,
                    config["QUOTE_ASSET"],
                    posisi_info,
                    flag_str,
                    equity_snapshot["snapshot_id"],
                    equity_snapshot["captured_at_ms"],
                )

            state_mod.save_state(config["STATE_FILE"], state)
            if lifecycle is not None:
                lifecycle.heartbeat("RUNNING")
            consecutive_errors = 0

        except BinanceAPIError as exc:
            consecutive_errors += 1
            logger.error(
                "BinanceAPIError (%d berturut-turut): %s", consecutive_errors, exc
            )
        except Exception as exc:
            consecutive_errors += 1
            logger.exception(
                "Error tak terduga (%d berturut-turut): %s", consecutive_errors, exc
            )

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
    if mode == "LIVE" and state.get("current_symbol"):
        protected = isinstance(state.get("native_oco"), dict) or isinstance(
            state.get("native_stop"), dict
        )
        if not protected or state.get("_native_stop_exit_blocked"):
            logger.critical(
                "Shutdown LIVE meninggalkan posisi %s tanpa proteksi native yang terverifikasi. "
                "State disimpan, tetapi operator wajib memeriksa akun Binance.",
                state.get("current_symbol"),
            )
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


def main() -> int:
    configure_utf8_console()
    parser = argparse.ArgumentParser(description="Pump Scanner Bot Binance Spot")
    parser.add_argument(
        "--acknowledge-missing-live-state",
        action="store_true",
        help="Konfirmasi operator untuk menginisialisasi state LIVE yang hilang.",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Jalankan pemeriksaan aturan entry dan exit tanpa menyentuh Binance.",
    )
    args = parser.parse_args()

    if args.selftest:
        from backtesting.entry_selftest import run as _run_entry_selftest
        from backtesting.exit_selftest import run as _run_exit_selftest

        entry_ok, _ = _run_entry_selftest(verbose=True)
        exit_ok, _ = _run_exit_selftest(verbose=True)
        return 0 if entry_ok and exit_ok else 1

    from infrastructure.network.file_descriptors import raise_fd_limit

    soft, hard, berubah = raise_fd_limit()
    if berubah:
        logger.info(
            "Batas file descriptor proses dinaikkan dari 1024 ke %s (hard %s).",
            soft,
            hard,
        )

    from config.config import InvalidModeError
    from infrastructure.process.runtime_control import (
        BotAlreadyRunningError,
        BotControlError,
        BotRuntime,
    )

    if CONFIG_LOAD_ERRORS:
        print(
            "Konfigurasi runtime rusak: " + "; ".join(CONFIG_LOAD_ERRORS),
            file=sys.stderr,
        )
        return 2
    try:
        mode = require_valid_mode(PUMP_CONFIG)
    except InvalidModeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.acknowledge_missing_live_state and mode != "LIVE":
        print(
            "--acknowledge-missing-live-state hanya berlaku pada mode LIVE.",
            file=sys.stderr,
        )
        return 2

    runtime = BotRuntime(mode)
    try:
        with runtime as lifecycle:
            code = run(
                PUMP_CONFIG,
                lifecycle=lifecycle,
                allow_missing_live_state=args.acknowledge_missing_live_state,
            )
            runtime.finish(
                code, None if code == 0 else "Bot berhenti dengan kode error."
            )
            return code
    except (BotAlreadyRunningError, BotControlError) as exc:
        print(str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
