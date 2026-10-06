from __future__ import annotations

import argparse
import logging
import logging.handlers
import math
import os
import signal
import sys
import threading
import time
import uuid
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
    ACCOUNT_SPOT_PERMISSION_SOURCE,
    ACCOUNT_SPOT_PERMISSION_VERIFIED,
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
from infrastructure.storage import state as state_mod
from strategy import indicators as strategy

logger = logging.getLogger("pump_bot")
_shutdown_requested = False
_shutdown_event = threading.Event()


class TrendCache:
    """Penyedia candle trend timeframe tinggi (default H1) untuk gerbang entry.

    Perilaku yang dijaga:
      * hanya candle yang SUDAH TUTUP yang dikembalikan (tanpa repaint),
      * jendela yang dikembalikan persis TREND_LOOKBACK_BARS candle, sama
        seperti potongan jendela di backtest,
      * hasil disimpan selama candle trend terakhir belum berganti, jadi satu
        simbol tidak diunduh berulang tiap scan,
      * kegagalan pengambilan tidak pernah menghilangkan riwayat lama lalu
        diam-diam meloloskan entry: pemanggil menerima None dan scanner
        menolak kandidat (fail closed).
    """

    def __init__(self, client, config: dict) -> None:
        self._client = client
        self._config = config
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[int, list]] = {}

    def _interval_ms(self) -> int:
        return strategy.trend_interval_minutes(self._config) * 60_000

    def _last_closed_open_time(self, now_ms: int) -> int:
        step = self._interval_ms()
        return ((int(now_ms) // step) - 1) * step

    def window_klines(self, symbol: str, now_ms: int) -> list:
        jendela = strategy.trend_window_bars(self._config)
        terakhir_tutup = self._last_closed_open_time(now_ms)
        with self._lock:
            tersimpan = self._cache.get(symbol)
            if tersimpan is not None and tersimpan[0] >= terakhir_tutup:
                return list(tersimpan[1])

        raw = self._client.get_klines(
            symbol,
            strategy.trend_interval(self._config),
            limit=min(strategy.TREND_KLINE_LIMIT, jendela + 1),
        )
        closed = [
            k
            for k in strategy.parse_klines(raw or [])
            if int(k.close_time) < int(now_ms)
        ]
        siap = closed[-jendela:]

        if siap:
            with self._lock:
                self._cache[symbol] = (int(siap[-1].open_time), siap)
        return list(siap)

    def verdict(self, symbol: str, now_ms: int) -> dict:
        jendela = self.window_klines(symbol, now_ms)
        return strategy.evaluate_trend_filter(jendela, self._config)

    def provider(self, symbol: str) -> list:
        """Callable yang dipakai scanner.find_best_candidate()."""
        return self.window_klines(symbol, state_mod.now_ms())


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
    file_handler = logging.handlers.RotatingFileHandler(
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


def load_pump_state(path: str, *, fail_closed: bool = False) -> dict:
    raw, valid, load_error = state_mod.load_state_checked(path)
    merged = dict(DEFAULT_STATE)
    merged.update(raw)

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
        "daily_stopped",
        "_limit_close_done",
        "_native_stop_exit_blocked",
        "reconciliation_required",
    ):
        if not isinstance(merged.get(key), bool):
            validation_errors.append(f"{key} harus boolean")
    for key in ("pending_order", "native_stop", "native_oco"):
        value = merged.get(key)
        if value is not None and not isinstance(value, dict):
            validation_errors.append(f"{key} harus object atau null")
    if not isinstance(merged.get("reconciliation_assets"), list):
        validation_errors.append("reconciliation_assets harus list")
    for key in ("day_start_equity", "peak_equity"):
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

    if not valid:
        validation_errors.append(load_error or "state tidak dapat dibaca")
    if fail_closed and validation_errors:
        reason = "; ".join(dict.fromkeys(validation_errors))
        merged["state_integrity_error"] = reason
        merged["reconciliation_required"] = True
        merged["reconciliation_reason"] = "STATE_INTEGRITY_UNVERIFIED"
        logger.critical(
            "Integritas state LIVE tidak dapat diverifikasi: %s. Entry baru diblokir ",
            reason,
        )
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


def _spot_api_key_permission_verified(account: dict) -> bool:
    return (
        account.get(ACCOUNT_SPOT_PERMISSION_VERIFIED) is True
        and str(account.get(ACCOUNT_SPOT_PERMISSION_SOURCE) or "")
        == "GET /sapi/v1/account/apiRestrictions"
    )


def _effective_account_permissions(account: dict) -> set[str]:
    permissions: set[str] = set()
    raw = account.get("permissions")
    if isinstance(raw, list):
        permissions.update(
            item.strip().upper()
            for item in raw
            if isinstance(item, str) and item.strip()
        )
    if (
        account.get("canTrade") is True
        and str(account.get("accountType") or "").upper() == "SPOT"
        and ("SPOT" in permissions or _spot_api_key_permission_verified(account))
    ):
        permissions.add("SPOT")
    return permissions


def _validate_live_account_snapshot(account: dict, quote_asset: str) -> None:
    if not isinstance(account, dict):
        raise BinanceAPIError(502, None, "respons account bukan object")
    if account.get("canTrade") is not True:
        raise BinanceAPIError(403, None, "akun Binance melaporkan canTrade bukan true")
    account_type = str(account.get("accountType") or "").upper()
    if account_type != "SPOT":
        raise BinanceAPIError(
            403, None, f"accountType bukan SPOT: {account_type or 'kosong'}"
        )
    permissions = account.get("permissions")
    api_key_spot_verified = _spot_api_key_permission_verified(account)
    if permissions is None:
        if not api_key_spot_verified:
            raise BinanceAPIError(
                403,
                None,
                "permission SPOT tidak ada pada account dan izin API key belum terverifikasi",
            )
    elif not isinstance(permissions, list):
        raise BinanceAPIError(502, None, "field permissions account bukan list")
    else:
        if any(not isinstance(item, str) or not item.strip() for item in permissions):
            raise BinanceAPIError(502, None, "field permissions account tidak valid")
        normalized_permissions = {item.strip().upper() for item in permissions}
        if "SPOT" not in normalized_permissions and not api_key_spot_verified:
            raise BinanceAPIError(
                403,
                None,
                "permission SPOT account/API key tidak dapat diverifikasi",
            )
    if "SPOT" not in _effective_account_permissions(account):
        raise BinanceAPIError(403, None, "izin efektif SPOT tidak dapat dibentuk")
    balances = account.get("balances")
    if not isinstance(balances, list):
        raise BinanceAPIError(502, None, "balances account tidak tersedia")
    seen: set[str] = set()
    for item in balances:
        if not isinstance(item, dict):
            raise BinanceAPIError(502, None, "entri balance bukan object")
        asset = str(item.get("asset") or "")
        if not asset or asset in seen:
            raise BinanceAPIError(
                502, None, f"asset balance kosong/duplikat: {asset!r}"
            )
        seen.add(asset)
        try:
            free = float(item.get("free"))
            locked = float(item.get("locked"))
        except (TypeError, ValueError):
            raise BinanceAPIError(502, None, f"balance {asset} bukan angka") from None
        if (
            not math.isfinite(free)
            or not math.isfinite(locked)
            or free < 0
            or locked < 0
        ):
            raise BinanceAPIError(502, None, f"balance {asset} tidak valid")
    if quote_asset not in seen:
        raise BinanceAPIError(
            502, None, f"balance aset kuotasi {quote_asset} tidak tersedia"
        )


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
    account = client.get_account()
    if str(config.get("MODE", "PAPER")).upper() == "LIVE":
        _validate_live_account_snapshot(account, config["QUOTE_ASSET"])
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
                    state["current_symbol"],
                    exc,
                )
                return None
        usdt_free += state["qty"] * price
    return usdt_free


def account_risk_gate(config: dict) -> "tuple[bool, str]":
    if str(config.get("MODE", "")).strip().upper() != "LIVE":
        return True, ""
    if config.get("USE_EQUITY_STOP") or config.get("USE_DAILY_STOP"):
        return True, ""
    override = (
        str(os.environ.get("ALLOW_LIVE_WITHOUT_ACCOUNT_STOP", "")).strip().lower()
    )
    if override in ("1", "true", "yes", "on"):
        logger.critical(
            "ALLOW_LIVE_WITHOUT_ACCOUNT_STOP aktif: bot LIVE dijalankan TANPA rem "
            "drawdown maupun rem kerugian harian atas permintaan eksplisit operator."
        )
        return True, ""
    return False, (
        "MODE=LIVE ditolak: USE_EQUITY_STOP dan USE_DAILY_STOP dua-duanya nonaktif. "
        "Tidak ada rem drawdown maupun rem kerugian harian, dan CLOSE_ALL_AT_LIMIT "
        "tidak akan pernah terpicu. Aktifkan minimal salah satu melalui override "
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
    today = state_mod.today_str()
    if state.get("day_start_date") != today:
        state["day_start_date"] = today
        state["day_start_equity"] = equity
        state["daily_stopped"] = False
        state["daily_stop_source"] = None
        logger.info(
            "Hari baru (UTC): %s. Equity awal hari = %.2f %s",
            today,
            equity,
            config["QUOTE_ASSET"],
        )

    if state.get("peak_equity") is None or equity > state["peak_equity"]:
        state["peak_equity"] = equity

    if (
        config["USE_EQUITY_STOP"]
        and not state.get("dd_stopped")
        and state["peak_equity"]
    ):
        dd_pct = (state["peak_equity"] - equity) / state["peak_equity"] * 100.0
        if dd_pct >= config["MAX_DRAWDOWN_PERCENT"]:
            state["dd_stopped"] = True
            state["dd_stop_until"] = (
                state_mod.now_ms() + config["DD_COOLDOWN_HOURS"] * 3600 * 1000
            )
            logger.critical(
                "STOP DRAWDOWN: turun %.2f%% dari puncak equity. Entry baru dijeda %d jam.",
                dd_pct,
                config["DD_COOLDOWN_HOURS"],
            )

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

    if (
        config.get("USE_DAILY_STOP", True)
        and not state.get("daily_stopped")
        and state.get("day_start_equity")
    ):
        change_pct = (
            (equity - state["day_start_equity"]) / state["day_start_equity"] * 100.0
        )
        if change_pct <= -config["MAX_DAILY_LOSS_PERCENT"]:
            state["daily_stopped"] = True
            state["daily_stop_source"] = "LOSS"
            logger.warning(
                "STOP HARIAN: rugi harian %.2f%%. Tidak ada entry baru sampai hari berikutnya (UTC).",
                change_pct,
            )
        elif change_pct >= config["DAILY_PROFIT_TARGET_PERCENT"]:
            state["daily_stopped"] = True
            state["daily_stop_source"] = "PROFIT"
            logger.info(
                "TARGET HARIAN TERCAPAI: profit harian %.2f%%. Tidak ada entry baru sampai hari berikutnya (UTC).",
                change_pct,
            )

    return bool(state.get("dd_stopped") or state.get("daily_stopped"))


def maybe_force_close_at_risk_limit(
    client: ExchangeClient,
    config: dict,
    filters_cache: dict,
    state: dict,
    entries_paused: bool,
    current_price,
) -> None:
    profit_stop_only = (
        bool(state.get("daily_stopped"))
        and not state.get("dd_stopped")
        and str(state.get("daily_stop_source") or "LOSS").upper() == "PROFIT"
    )
    limit_now = (
        bool(state.get("dd_stopped") or state.get("daily_stopped"))
        and not profit_stop_only
    )
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
        state["_limit_close_done"] = True

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
                        if _restore_pending_buy(config, state, pending, order, account):
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
                            qty_state_sell = float(state.get("qty") or 0.0)
                            if (
                                qty_state_sell > 0
                                and executed_qty >= qty_state_sell - 1e-12
                            ):
                                minutes = int(
                                    config.get("COOLDOWN_MINUTES_AFTER_CLOSE", 0) or 0
                                )
                                state["cooldown_until"] = (
                                    state_mod.now_ms() + minutes * 60 * 1000
                                )
                                state["last_trade_time"] = state_mod.now_ms()
                            state["pending_order"] = None
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
        if total_base <= 0 and not pending_unresolved:
            logger.warning(
                "REKONSILIASI: state memegang %s qty=%.8f tetapi saldo total %s nol. "
                "Posisi hantu direset berdasarkan saldo terverifikasi.",
                symbol,
                qty_state,
                base_asset,
            )
            reset_position(state)
        elif total_base <= 0 and pending_unresolved:
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


def _native_oco_levels(
    state: dict, filters: SymbolFilters | None, config: dict
) -> dict:
    entry = float(state.get("entry_price") or 0.0)
    atr_mode = str(state.get("exit_source", "")).upper() == "ATR"
    sl = _exit_distance(state, config, "sl_pct", "SL_PCT", atr_mode, entry)
    tp = _exit_distance(state, config, "tp_pct", "TP_PCT", atr_mode, entry)
    raw_sl = entry - sl if atr_mode else entry * (1.0 - sl / 100.0)
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
        return False
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
    valid = (
        bid is not None
        and all(filters.price_valid(value) for value in prices)
        and below_price < below_stop < bid < above_price <= above_stop
        and below_stop < entry < above_stop
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
    valid = (
        bid is not None
        and "STOP_LOSS" in filters.order_types
        and filters.market_qty_valid(qty)
        and filters.price_valid(stop_price)
        and 0 < stop_price < entry
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


def _settle_native_protective_fill(
    client: ExchangeClient, config: dict, state: dict, symbol: str, reason: str
) -> None:
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
    state["cooldown_until"] = (
        state_mod.now_ms() + config["COOLDOWN_MINUTES_AFTER_CLOSE"] * 60 * 1000
    )
    state["last_trade_time"] = state_mod.now_ms()
    state_mod.save_state(config["STATE_FILE"], state)
    logger.info(
        "EXIT NATIVE %s (%s): posisi ditutup oleh order proteksi exchange-side "
        "(entry=%.6f). Rekonsiliasi bersih; bot lanjut scan setelah cooldown.",
        symbol,
        reason,
        entry_price,
    )
    try_dust_sweep(client, config, symbol)


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
            filled = filled or leg["status"] == "FILLED" or leg["executed_qty"] > 0

    if filled:
        base = symbol[: -len(config["QUOTE_ASSET"])]
        state["native_oco"] = None
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [base]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "OCO %s salah satu leg FILLED; tidak mengirim SELL kedua. Rekonsiliasi saldo dijalankan.",
            symbol,
        )
        _settle_native_protective_fill(
            client, config, state, symbol, "NATIVE_OCO_FILLED"
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
            client, config, state, symbol, "NATIVE_STOP_FILLED"
        )
        return False
    if status in ("CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"):
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
    if _oco_response_has_fill(response):
        state["native_oco"] = None
        state["_native_stop_exit_blocked"] = True
        state["reconciliation_required"] = True
        state["reconciliation_assets"] = [symbol]
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "OCO %s terisi saat cancel. SELL manual ditahan untuk rekonsiliasi.", symbol
        )
        _settle_native_protective_fill(
            client, config, state, symbol, "NATIVE_OCO_FILLED_ON_CANCEL"
        )
        return False
    final_status = str(response.get("listOrderStatus") or "").upper()
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
    if final_status == "FILLED" or float(response.get("executedQty", 0.0) or 0.0) > 0:
        state["native_stop"] = None
        state["_native_stop_exit_blocked"] = True
        _mark_reconciliation(state, "NATIVE_STOP_FILLED_ON_CANCEL", [symbol])
        state_mod.save_state(config["STATE_FILE"], state)
        _settle_native_protective_fill(
            client, config, state, symbol, "NATIVE_STOP_FILLED_ON_CANCEL"
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


_listing_age_cache: dict = {}


def listing_age_days(client: ExchangeClient, symbol: str, now_ms: int) -> float:
    if symbol in _listing_age_cache:
        return _listing_age_cache[symbol]
    raw = client.get_klines(symbol, "1d", limit=1, start_time_ms=0)
    age = 0.0 if not raw else max(0.0, (now_ms - int(raw[0][0])) / 86_400_000.0)
    _listing_age_cache[symbol] = age
    return age


def _restore_pending_buy(
    config: dict, state: dict, pending: dict, order: dict, account: dict
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
    state["entry_time"] = int(order.get("transactTime") or state_mod.now_ms())
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
    # quoteOrderQty wajib mengikuti presisi quote asset bursa (quoteAssetPrecision).
    # Nilai hasil pembagian/multiplikasi float bisa berdesimal panjang, misalnya
    # 7.58 * 0.995 = 7.5421000000000005, dan ditolak dengan kode -1111
    # "Parameter 'quoteOrderQty' has too much precision". Pembulatan dilakukan
    # ke bawah supaya nominal tidak pernah melebihi saldo yang tersedia.
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
            # Penolakan definitif (mis. -1111 presisi, -1013 filter, -1100
            # parameter) terjadi SEBELUM order masuk ke matching engine, jadi
            # tidak ada order yang terbentuk. Intent dibatalkan tanpa
            # rekonsiliasi supaya bot tidak memblokir entry berikutnya.
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
        _mark_reconciliation(state, "FILLED_BUY_BASE_BALANCE_ZERO", [base_asset])
        state_mod.save_state(config["STATE_FILE"], state)
        logger.critical(
            "BUY %s terisi tetapi saldo base terverifikasi nol. Intent dipertahankan.",
            candidate.symbol,
        )
        return

    state["qty"] = managed_qty
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
    state["pending_order"] = None
    sell_price = (cumm_quote / executed_qty) if cumm_quote > 0 else 0.0
    pnl = (
        (sell_price - entry_price) * executed_qty
        if entry_price > 0 and sell_price > 0
        else 0.0
    )
    min_qty = float(filters.min_qty)
    fully_closed = (
        status == "FILLED"
        and executed_qty >= qty_to_sell - 1e-12
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
    if reasons:
        close_position(
            client,
            config,
            filters_cache,
            state,
            "+".join(reasons),
            reference_price=current_price,
        )


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
            "konfirmasi volume dan ATR. Bot tetap mengambil %d candle per konfirmasi agar deteksi "
            "tidak selalu gagal, tetapi perbaiki nilai config ini supaya backtest dan live "
            "benar-benar memakai angka yang sama.",
            have_bars,
            need_setup,
            strategy.confirm_window_bars(config),
        )
    if config.get("TREND_FILTER_ENABLED", False):
        butuh_trend = strategy.trend_required_bars(config)
        jendela_trend = strategy.trend_window_bars(config)
        if int(config.get("TREND_LOOKBACK_BARS", 0) or 0) < butuh_trend:
            logger.warning(
                "TREND_LOOKBACK_BARS=%s lebih kecil dari %d candle yang dibutuhkan EMA "
                "dan ADX pada %s. Gerbang trend otomatis memakai %d candle, dan kandidat "
                "tetap ditolak selama riwayat belum cukup. Perbaiki nilai config ini "
                "supaya live dan backtest memakai jendela yang sama.",
                config.get("TREND_LOOKBACK_BARS"),
                butuh_trend,
                strategy.trend_interval(config),
                jendela_trend,
            )
        try:
            strategy.trend_warmup_bars(config, config["CONFIRM_INTERVAL"])
        except ValueError as exc:
            logger.warning(
                "%s Backtest akan menolak kombinasi ini sampai diperbaiki.", exc
            )
        logger.info(
            "Gerbang trend AKTIF (%s): entry lolos hanya kalau candle %s terakhir yang sudah "
            "tutup memenuhi close > EMA%d, EMA%d > EMA%d, dan ADX%d >= %g. Kalau candle trend "
            "gagal diambil atau riwayatnya kurang, kandidat DITOLAK (fail closed).",
            config.get("TREND_INTERVAL", "1h"),
            config.get("TREND_INTERVAL", "1h"),
            int(config.get("TREND_EMA_FAST", 20) or 20),
            int(config.get("TREND_EMA_FAST", 20) or 20),
            int(config.get("TREND_EMA_SLOW", 50) or 50),
            int(config.get("TREND_ADX_PERIOD", 14) or 14),
            float(config.get("TREND_ADX_MIN", 0.0) or 0.0),
        )
    else:
        logger.info(
            "Gerbang trend NONAKTIF: entry hanya memakai konfirmasi %s.",
            config.get("CONFIRM_INTERVAL", "5m"),
        )
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

        state = load_pump_state(config["STATE_FILE"], fail_closed=(mode == "LIVE"))
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
    last_reconciliation = time.time()
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

    trend_cache = TrendCache(client, config)
    trend_provider = (
        trend_cache.provider if config.get("TREND_FILTER_ENABLED", False) else None
    )
    if trend_provider is None:
        logger.warning(
            "Gerbang trend timeframe tinggi NONAKTIF (TREND_FILTER_ENABLED=False). "
            "Entry hanya memakai konfirmasi %s.",
            config["CONFIRM_INTERVAL"],
        )
    else:
        logger.info(
            "Gerbang trend %s AKTIF: close > EMA%d > EMA%d dan ADX%d >= %g "
            "(jendela %d candle tertutup, fail closed bila data trend gagal diambil).",
            strategy.trend_interval(config),
            int(config.get("TREND_EMA_FAST", 20)),
            int(config.get("TREND_EMA_SLOW", 50)),
            int(config.get("TREND_ADX_PERIOD", 14)),
            float(config.get("TREND_ADX_MIN", 20.0) or 0.0),
            strategy.trend_window_bars(config),
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

            if state.get("pending_order") or (
                state.get("reconciliation_required")
                and time.time() - last_reconciliation >= RECONCILIATION_INTERVAL_SECONDS
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
            equity = get_equity(client, config, state, position_price=equity_price)
            if equity is None:
                entries_paused = True
                _mark_reconciliation(state, "EQUITY_UNVERIFIED")
            else:
                entries_paused = update_equity_controls(state, equity, config)

            maybe_force_close_at_risk_limit(
                client, config, filters_cache, state, entries_paused, current_price
            )

            do_scan = (
                time.time() * 1000 - state.get("last_scan_time", 0)
                > config["MARKET_SCAN_INTERVAL_SECONDS"] * 1000
            )
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
                    and now - state.get("last_trade_time", 0)
                    >= config["MIN_SECONDS_BETWEEN_TRADES"] * 1000
                )
                if can_enter:
                    scan_config = dict(config)
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
                    best = scanner.find_best_candidate(
                        tickers,
                        klines_fetcher,
                        scan_config,
                        tradable_symbols,
                        klines_fetcher_many=klines_fetcher_many,
                        prewarm_fn=client.prewarm_book_ticker,
                        trend_provider=trend_provider,
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
                            if best.trend is not None:
                                nilai = best.trend.get("values") or {}
                                trend_info = " | trend %s: %s" % (
                                    best.trend.get("interval")
                                    or strategy.trend_interval(config),
                                    best.trend.get("reason"),
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
                    else:
                        if trend_provider is not None:
                            logger.info(
                                "Tidak ada kandidat yang lolos konfirmasi %s dan gerbang "
                                "trend %s pada scan ini.",
                                config["CONFIRM_INTERVAL"],
                                strategy.trend_interval(config),
                            )
                        else:
                            logger.info(
                                "Tidak ada kandidat yang lolos konfirmasi volume rolling "
                                "pada scan ini."
                            )

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
                if state.get("daily_stopped"):
                    flags.append("DAILY-STOP")
                flag_str = f" | status: {', '.join(flags)}" if flags else ""
                equity_str = (
                    f"{equity:.2f}"
                    if equity is not None
                    else "n/a (API harga gangguan)"
                )
                logger.info(
                    "[HEARTBEAT] Bot masih berjalan | equity=%s %s | %s%s",
                    equity_str,
                    config["QUOTE_ASSET"],
                    posisi_info,
                    flag_str,
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


def selftest() -> None:
    import tempfile

    assert PUMP_CONFIG.get("PUMP_MIN_24H_CHANGE_PCT") == 5.0
    assert PUMP_CONFIG.get("PUMP_MAX_24H_CHANGE_PCT") == 10.0
    assert PUMP_CONFIG.get("BTC_FILTER_ENABLED") is False
    assert PUMP_CONFIG.get("TOP_N_CANDIDATES_TO_CONFIRM") == 30
    assert PUMP_CONFIG.get("ROLLING_VOLUME_SURGE_MULT") == 1.3
    assert PUMP_CONFIG.get("MAX_CHASE_PCT") == 3.0
    assert PUMP_CONFIG.get("DEPTH_FILTER_ENABLED") is True
    assert PUMP_CONFIG.get("ORDERBOOK_FILTER_ENABLED") is True
    assert PUMP_CONFIG.get("DEMAND_ZONE_FILTER_ENABLED") is True
    assert PUMP_CONFIG.get("TREND_FILTER_ENABLED") is True
    assert PUMP_CONFIG.get("TREND_INTERVAL") == "1h"
    assert (
        PUMP_CONFIG.get("TREND_EMA_FAST") == 20
        and PUMP_CONFIG.get("TREND_EMA_SLOW") == 50
    )
    assert (
        PUMP_CONFIG.get("TREND_ADX_PERIOD") == 14
        and PUMP_CONFIG.get("TREND_ADX_MIN") == 20.0
    )
    assert PUMP_CONFIG.get("TREND_LOOKBACK_BARS") == 120

    cfg = dict(PUMP_CONFIG)
    cfg["STATE_FILE"] = os.path.join(
        tempfile.gettempdir(), "pump_bot_selftest_state.json"
    )
    cfg["MIN_QUOTE_VOLUME_USDT_24H"] = 1_000_000
    cfg["PUMP_MIN_24H_CHANGE_PCT"] = 5.0
    cfg["PUMP_MAX_24H_CHANGE_PCT"] = 10.0
    cfg["ROLLING_VOLUME_SURGE_MULT"] = 2.0
    cfg["DEPTH_FILTER_ENABLED"] = True
    cfg["ORDERBOOK_FILTER_ENABLED"] = True

    print("=== SELFTEST: saringan semesta, gerbang pump, dan urutan volume ===")
    tickers = [
        {
            "symbol": "AUSDT",
            "priceChangePercent": "8.0",
            "quoteVolume": "6000000",
            "lastPrice": "1.0",
        },
        {
            "symbol": "BUSDT",
            "priceChangePercent": "10.0",
            "quoteVolume": "4000000",
            "lastPrice": "2.0",
        },
        {
            "symbol": "CUSDT",
            "priceChangePercent": "-3.0",
            "quoteVolume": "9000000",
            "lastPrice": "0.5",
        },
        {
            "symbol": "DUSDT",
            "priceChangePercent": "8.0",
            "quoteVolume": "10000",
            "lastPrice": "0.1",
        },
        {
            "symbol": "EUSDT",
            "priceChangePercent": "3.0",
            "quoteVolume": "8000000",
            "lastPrice": "1.0",
        },
        {
            "symbol": "BTCUPUSDT",
            "priceChangePercent": "8.0",
            "quoteVolume": "9000000",
            "lastPrice": "3.0",
        },
        {
            "symbol": "USDCUSDT",
            "priceChangePercent": "8.0",
            "quoteVolume": "9000000",
            "lastPrice": "1.0",
        },
        {
            "symbol": "HALTUSDT",
            "priceChangePercent": "10.0",
            "quoteVolume": "8000000",
            "lastPrice": "1.0",
        },
        {
            "symbol": "HIGHUSDT",
            "priceChangePercent": "10.01",
            "quoteVolume": "7000000",
            "lastPrice": "1.0",
        },
    ]
    tradable = {
        "AUSDT",
        "BUSDT",
        "CUSDT",
        "DUSDT",
        "EUSDT",
        "BTCUPUSDT",
        "USDCUSDT",
        "HIGHUSDT",
    }
    ranked = scanner.filter_and_rank_candidates(tickers, cfg, tradable)
    symbols = [c.symbol for c in ranked]
    print("  Lolos saringan + gerbang pump, urut volume kuotasi:", symbols)
    assert symbols == ["AUSDT", "BUSDT"], f"Hasil saringan/urutan salah: {symbols}"
    print("  -> OK (leveraged token, stablecoin, volume rendah, simbol non-TRADING,")
    print(
        "      koin yang TURUN 24 jam, dan koin di luar rentang kenaikan ter-exclude)"
    )
    print(
        "  -> OK (batas atas 24 jam: 10.00% lolos, 10.01% ditolak walau volumenya terbesar)"
    )
    assert cfg.get("PUMP_MAX_24H_CHANGE_PCT") == 10.0
    ok_hi, why_hi = scanner.evaluate_pump_gate(12.0, 6_000_000.0, cfg)
    assert not ok_hi and "batas atas" in why_hi, why_hi
    ok_lo, _ = scanner.evaluate_pump_gate(6.0, 6_000_000.0, cfg)
    assert ok_lo, "batas bawah 6.0% harus lolos (inklusif)"
    cfg_nocap = dict(cfg, PUMP_MAX_24H_CHANGE_PCT=0.0)
    assert scanner.evaluate_pump_gate(40.0, 6_000_000.0, cfg_nocap)[0]
    print("  -> OK (rentang 6.0% sampai 10.0% inklusif, 0 = batas atas nonaktif)")

    print("=== SELFTEST: kedalaman dan order book sebelum entry ===")

    def _book(ask_levels, bid_levels):
        return {
            "asks": [[str(p), str(q)] for p, q in ask_levels],
            "bids": [[str(p), str(q)] for p, q in bid_levels],
        }

    flat_asks = [(1.0 + i * 0.0001, 400.0 / (1.0 + i * 0.0001)) for i in range(1, 60)]
    flat_bids = [(1.0 - i * 0.0001, 400.0 / (1.0 - i * 0.0001)) for i in range(1, 60)]
    book_ok = _book(flat_asks, flat_bids)
    ok, why, m = scanner.evaluate_orderbook(book_ok, 1000.0, cfg)
    assert ok, why
    ok, why, m = scanner.evaluate_orderbook(book_ok, 3000.0, cfg)
    assert not ok and "kedalaman" in why, why
    thin_bids = [(1.0 - i * 0.0001, 100.0 / (1.0 - i * 0.0001)) for i in range(1, 60)]
    ok, why, m = scanner.evaluate_orderbook(_book(flat_asks, thin_bids), 1000.0, cfg)
    assert not ok and "tekanan jual" in why, why
    wall_asks = list(flat_asks)
    wall_asks[20] = (wall_asks[20][0], 12_000.0 / wall_asks[20][0])
    ok, why, m = scanner.evaluate_orderbook(_book(wall_asks, flat_bids), 1000.0, cfg)
    assert not ok and "sell wall" in why, why
    for bad in (
        None,
        {},
        {"asks": [], "bids": []},
        {"asks": [["x", "y"]], "bids": [["1", "1"]]},
        _book([(1.0, 5.0)], [(1.1, 5.0)]),
    ):
        assert not scanner.evaluate_orderbook(bad, 1000.0, cfg)[0], bad
    off = dict(cfg, DEPTH_FILTER_ENABLED=False, ORDERBOOK_FILTER_ENABLED=False)
    assert scanner.evaluate_orderbook(None, 1000.0, off)[0]
    only_depth = dict(cfg, ORDERBOOK_FILTER_ENABLED=False)
    assert scanner.evaluate_orderbook(_book(flat_asks, thin_bids), 1000.0, only_depth)[
        0
    ]
    assert (
        scanner.normalize_depth_limit(50) == 100
        and scanner.normalize_depth_limit(101) == 500
        and scanner.normalize_depth_limit(2000) == 1000
        and scanner.normalize_depth_limit("abc") == 500
    )
    print("  -> OK (depth, ketimpangan bid/ask, sell wall, data rusak = fail closed)")

    print("\n=== SELFTEST: konfirmasi entry (volume rolling, tanpa indikator) ===")

    def _seri_volume(volume_akhir: float):
        out = []
        for i in range(30):
            v = 100.0 + (i % 3) * 0.1
            vol = volume_akhir if i == 29 else 1000.0
            out.append(
                strategy.Kline(
                    i * 300_000, v, v + 1, v - 1, v, i * 300_000 + 299_999, vol, v * vol
                )
            )
        return out

    hasil = scanner.detect_entry_setup(_seri_volume(3000.0), cfg)
    print(f"  Lonjakan volume 3x -> ok={hasil.ok} ({hasil.reason})")
    assert hasil.ok, "Lonjakan volume 3x harusnya lolos konfirmasi"
    assert hasil.atr_value is not None, "ATR harus ikut tersedia pada setup"
    assert (
        hasil.signal_close is not None and hasil.signal_close > 0
    ), "close candle sinyal harus tersedia untuk filter MAX_CHASE_PCT"
    hasil_tolak = scanner.detect_entry_setup(_seri_volume(1500.0), cfg)
    assert not hasil_tolak.ok, "Volume hanya 1,5x harus ditolak (minimum 2x)"
    hasil_pendek = scanner.detect_entry_setup(_seri_volume(3000.0)[:10], cfg)
    assert not hasil_pendek.ok, "Data candle kurang harus ditolak"
    assert (
        hasil.demand_zone_low is not None and hasil.demand_zone_high is not None
    ), "batas zona demand harus terisi saat DEMAND_ZONE_FILTER_ENABLED aktif"
    seri_bearish = _seri_volume(3000.0)
    seri_bearish[-1] = strategy.Kline(
        29 * 300_000,
        100.5,
        101.0,
        99.0,
        99.5,
        29 * 300_000 + 299_999,
        3000.0,
        3000.0 * 99.5,
    )
    assert not scanner.detect_entry_setup(
        seri_bearish, cfg
    ).ok, "Candle sinyal bearish harus ditolak oleh filter zona demand"
    seri_ekor_atas = _seri_volume(3000.0)
    seri_ekor_atas[-1] = strategy.Kline(
        29 * 300_000,
        100.1,
        104.0,
        100.0,
        100.3,
        29 * 300_000 + 299_999,
        3000.0,
        3000.0 * 100.3,
    )
    assert not scanner.detect_entry_setup(
        seri_ekor_atas, cfg
    ).ok, (
        "Candle dengan ekor atas panjang (close_pos rendah) harus ditolak filter demand"
    )
    seri_pucuk = _seri_volume(3000.0)
    seri_pucuk[-1] = strategy.Kline(
        29 * 300_000,
        105.0,
        106.5,
        104.8,
        106.2,
        29 * 300_000 + 299_999,
        3000.0,
        3000.0 * 106.2,
    )
    assert not scanner.detect_entry_setup(
        seri_pucuk, cfg
    ).ok, "Harga yang sudah terbang terlalu jauh di atas zona demand harus ditolak"
    assert scanner.detect_entry_setup(
        seri_pucuk, dict(cfg, DEMAND_ZONE_FILTER_ENABLED=False)
    ).ok, "Jika DEMAND_ZONE_FILTER_ENABLED=False, hanya volume rolling yang dicek"
    print(
        "  -> OK (volume 3x + zona demand lolos, bearish/ekor atas/pucuk ditolak, data kurang ditolak)"
    )

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
            return {
                "balances": [
                    {"asset": self.base_asset, "free": str(self.free), "locked": "0"},
                    {"asset": "USDT", "free": "1000", "locked": "0"},
                ]
            }

        def get_book_ticker(self, symbol, max_retries=3):
            return {
                "symbol": symbol,
                "bidPrice": str(self.price),
                "askPrice": str(self.price),
            }

        depth_mode = "deep"

        def get_depth(self, symbol, limit=100):
            if self.depth_mode == "error":
                raise BinanceAPIError(500, None, "depth simulasi gagal")
            px = float(self.price)
            asks, bids = [], []
            for i in range(1, 41):
                ask_px = px * (1.0 + i * 0.0002)
                bid_px = px * (1.0 - i * 0.0002)
                ask_val = 1_000_000.0
                if self.depth_mode == "thin_ask":
                    ask_val = 100.0
                elif self.depth_mode == "wall" and i == 10:
                    ask_val = 30_000_000.0
                asks.append([str(ask_px), str(ask_val / ask_px)])
                bids.append([str(bid_px), str(1_000_000.0 / bid_px)])
            return {"asks": asks, "bids": bids}

        def new_market_order(
            self,
            symbol,
            side,
            quantity=None,
            quote_order_qty=None,
            new_client_order_id=None,
            quote_precision=None,
        ):
            qty = float(quantity or 0.0)
            if qty <= 0 and quote_order_qty is not None and self.price > 0:
                qty = float(quote_order_qty) / self.price
            self.orders.append((symbol, side, qty))
            return {
                "status": "FILLED",
                "executedQty": str(qty),
                "cummulativeQuoteQty": str(qty * self.price),
            }

        def get_dust_convertible(self, account_type="SPOT"):
            return {"details": []}

        def convert_dust(self, assets, account_type="SPOT"):
            return {"totalTransfered": "0"}

    filters_cache = {
        "TESTUSDT": SymbolFilters(
            step_size=D("0.01"),
            min_qty=D("0.01"),
            min_notional=D("5"),
            tick_size=D("0.0001"),
        )
    }
    cfg_exit = dict(cfg)
    cfg_exit.update(
        {
            "USE_TP": True,
            "TP_PCT": 6.0,
            "USE_STOP_LOSS": True,
            "SL_PCT": 3.0,
            "USE_BREAKEVEN": True,
            "BE_TRIGGER_PCT": 3.0,
            "BE_LOCK_PCT": 0.15,
            "USE_TRAILING": True,
            "TRAILING_START_PCT": 4.0,
            "TRAILING_STEP_PCT": 1.0,
        }
    )

    state = dict(DEFAULT_STATE)
    state["current_symbol"] = "TESTUSDT"
    state["entry_price"] = 100.0
    state["qty"] = 1.0
    state["entry_time"] = state_mod.now_ms()

    manage_exit(FakeTradeClient(), cfg_exit, filters_cache, state, 103.5)
    assert state["be_active"], "Breakeven harusnya sudah aktif di profit 3.5%"
    assert (
        state["current_symbol"] == "TESTUSDT"
    ), "Belum boleh close, baru breakeven aktif"
    print(
        f"  Setelah profit +3.5%: be_active={state['be_active']}, be_stop={state['be_stop_price']:.4f} -> OK"
    )

    manage_exit(FakeTradeClient(), cfg_exit, filters_cache, state, 106.5)
    assert (
        state["current_symbol"] is None
    ), "Posisi harusnya sudah tertutup kena TAKE_PROFIT"
    print("  Setelah profit +6.5%: posisi tertutup (TAKE_PROFIT) -> OK")

    print(
        "\n=== SELFTEST: Stop Loss (harga langsung turun sejak entry, TIDAK sempat untung) ==="
    )
    assert cfg_exit["USE_STOP_LOSS"], "USE_STOP_LOSS harus aktif di skenario ini"
    state2 = dict(DEFAULT_STATE)
    state2["current_symbol"] = "TESTUSDT"
    state2["entry_price"] = 100.0
    state2["qty"] = 1.0
    state2["entry_time"] = state_mod.now_ms()

    manage_exit(FakeTradeClient(), cfg_exit, filters_cache, state2, 98.0)
    assert (
        state2["current_symbol"] == "TESTUSDT"
    ), "Rugi -2% belum boleh kena Stop Loss (ambang 3.0%)"
    assert not state2["be_active"], "Breakeven tidak boleh aktif kalau posisi rugi"
    print("  Rugi -2%: posisi masih terbuka, BE/Trailing tidak aktif -> OK")

    manage_exit(FakeTradeClient(), cfg_exit, filters_cache, state2, 96.5)
    assert (
        state2["current_symbol"] is None
    ), "Posisi harusnya sudah tertutup kena STOP_LOSS di rugi -3.5%"
    print("  Rugi -3.5%: posisi tertutup (STOP_LOSS) -> OK")

    print("\n=== SELFTEST: ukuran posisi (RISK_PERCENT, plafon, bantalan saldo) ===")

    class SizingClient(FakeTradeClient):

        def __init__(self, usdt_free):
            super().__init__(base_asset="TESTB", free=0.0, price=1.0)
            self.usdt_free = usdt_free

        def get_account(self):
            return {
                "balances": [
                    {"asset": "USDT", "free": str(self.usdt_free), "locked": "0"},
                    {"asset": "TESTB", "free": str(self.free), "locked": "0"},
                ]
            }

        def new_market_order(
            self,
            symbol,
            side,
            quantity=None,
            quote_order_qty=None,
            new_client_order_id=None,
            quote_precision=None,
        ):
            resp = super().new_market_order(
                symbol,
                side,
                quantity,
                quote_order_qty,
                new_client_order_id,
                quote_precision,
            )
            if side == "BUY":
                bought = float(quantity or 0.0)
                if bought <= 0 and quote_order_qty is not None and self.price > 0:
                    bought = float(quote_order_qty) / self.price
                self.free += bought
            return resp

    from decimal import Decimal as D2
    from trading.clients.binance_client import SymbolFilters as SF2

    size_filters = {
        "TESTBUSDT": SF2(
            step_size=D2("0.00000001"),
            min_qty=D2("0.00000001"),
            min_notional=D2("1"),
            tick_size=D2("0.0001"),
        )
    }
    cand = scanner.Candidate(
        symbol="TESTBUSDT",
        base_asset="TESTB",
        price_change_pct=20.0,
        quote_volume=9e6,
        last_price=1.0,
        confirmed=True,
        confirm_reason="selftest",
    )

    def nominal_dipakai(cfg_size, saldo):
        cl = SizingClient(saldo)
        st = dict(DEFAULT_STATE)
        open_position(cl, cfg_size, size_filters, st, cand)
        assert cl.orders, "Order BUY seharusnya terkirim"
        return float(cl.orders[-1][2])

    cfg_size = dict(cfg)
    cfg_size.update(
        {
            "USE_RISK_PERCENT": True,
            "RISK_PERCENT": 95.0,
            "BALANCE_BUFFER_PCT": 0.5,
            "MAX_POSITION_USDT": 0,
            "USE_ATR_EXIT": False,
        }
    )

    for saldo, harap in (
        (100.0, 100 * 0.995 * 0.95),
        (1000.0, 1000 * 0.995 * 0.95),
        (5000.0, 5000 * 0.995 * 0.95),
    ):
        got = nominal_dipakai(cfg_size, saldo)
        assert (
            abs(got - harap) < 0.01
        ), f"saldo {saldo}: harap {harap:.2f}, dapat {got:.2f}"
        print(
            f"  Saldo {saldo:>7.0f} -> pakai {got:>8.2f} USDT ({got / saldo * 100:.2f}% saldo) -> OK"
        )

    print("=== SELFTEST: open_position menolak entry bila order book buruk ===")
    for mode_depth, harus_order in (
        ("deep", True),
        ("thin_ask", False),
        ("wall", False),
        ("error", False),
    ):
        cl = SizingClient(1000.0)
        cl.depth_mode = mode_depth
        st = dict(DEFAULT_STATE)
        open_position(cl, cfg_size, size_filters, st, cand)
        assert (
            bool(cl.orders) == harus_order
        ), f"depth_mode={mode_depth}: orders={cl.orders}"
        if not harus_order:
            assert not st.get(
                "pending_order"
            ), "penolakan tidak boleh meninggalkan pending_order"
        print(f"  depth_mode={mode_depth:<9} -> order terkirim={bool(cl.orders)} -> OK")
    cfg_off = dict(cfg_size, DEPTH_FILTER_ENABLED=False, ORDERBOOK_FILTER_ENABLED=False)
    cl = SizingClient(1000.0)
    cl.depth_mode = "error"
    open_position(cl, cfg_off, size_filters, dict(DEFAULT_STATE), cand)
    assert cl.orders, "filter nonaktif tidak boleh menyentuh depth"
    print("  filter nonaktif -> depth tidak dipanggil, order terkirim -> OK")

    cfg_cap = dict(cfg_size)
    cfg_cap["MAX_POSITION_USDT"] = 10.0
    got_cap = nominal_dipakai(cfg_cap, 1000.0)
    assert abs(got_cap - 10.0) < 1e-6, f"Plafon 10 USDT harus mengikat, dapat {got_cap}"
    print(
        f"  Plafon 10 USDT aktif, saldo 1000 -> pakai {got_cap:.2f} USDT "
        f"({got_cap / 1000 * 100:.2f}% saldo) -> OK (inilah bug lama)"
    )

    cfg_allin = dict(cfg_size)
    cfg_allin["RISK_PERCENT"] = 100.0
    got_allin = nominal_dipakai(cfg_allin, 1000.0)
    assert (
        got_allin < 1000.0
    ), "All-in tidak boleh membelanjakan 100% saldo persis (butuh ruang fee)"
    assert got_allin >= 1000.0 * 0.98, f"Bantalan terlalu besar: {got_allin}"
    print(
        f"  RISK_PERCENT=100, saldo 1000 -> pakai {got_allin:.2f} USDT "
        f"(sisa {1000 - got_allin:.2f} untuk fee) -> OK"
    )

    cfg_fixed_size = dict(cfg_size)
    cfg_fixed_size.update({"USE_RISK_PERCENT": False, "POSITION_SIZE_USDT": 25.0})
    got_fixed = nominal_dipakai(cfg_fixed_size, 1000.0)
    assert (
        abs(got_fixed - 25.0) < 1e-6
    ), f"Mode nominal tetap harus pakai 25 USDT, dapat {got_fixed}"
    print(
        f"  Mode nominal tetap (USE_RISK_PERCENT=False) -> {got_fixed:.2f} USDT -> OK"
    )

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
    assert (
        state_locked["current_symbol"] is None
    ), "manage_exit harus memakai sl_pct dari state (1%), bukan SL_PCT config (3%)"
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
    assert (
        state_tp["current_symbol"] is None
    ), "manage_exit harus memakai tp_pct dari state (2%), bukan TP_PCT config (6%)"
    print("  TP terkunci di state (2%) dipakai, bukan TP_PCT config (6%) -> OK")

    state_old = dict(DEFAULT_STATE)
    del state_old["sl_pct"]
    del state_old["tp_pct"]
    state_old["current_symbol"] = "TESTUSDT"
    state_old["entry_price"] = 100.0
    state_old["qty"] = 1.0
    state_old["entry_time"] = state_mod.now_ms()
    manage_exit(FakeTradeClient(), cfg_locked, filters_cache, state_old, 96.0)
    assert (
        state_old["current_symbol"] is None
    ), "State versi lama tanpa sl_pct harus tetap terlindungi oleh SL_PCT config"
    print("  State versi lama tanpa sl_pct tetap terlindungi SL_PCT config -> OK")

    print("\n=== SELFTEST: invariant Breakeven & Trailing tetap ===")
    cfg_full = dict(cfg)
    cfg_full.update(
        {
            "SL_PCT": 2.5,
            "TP_PCT": 5.0,
            "BE_TRIGGER_PCT": 9.0,
            "BE_LOCK_PCT": 12.0,
            "TRAILING_START_PCT": 4.0,
            "TRAILING_STEP_PCT": 9.0,
        }
    )
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
    assert not st_be[
        "be_active"
    ], "Breakeven memakai BE_TRIGGER_PCT config, seharusnya memakai be_trigger_pct dari state"
    manage_exit(FakeTradeClient(), cfg_be_cfg, filters_cache, st_be, 106.0)
    assert st_be[
        "be_active"
    ], "Breakeven harus aktif setelah melewati trigger dari state"
    assert abs(st_be["be_stop_price"] - 101.0) < 1e-6
    print("  BE/Trailing memakai level state saat tersedia -> OK")

    print(
        "\n=== SELFTEST: perintah manual 'Jual Sekarang' dari dashboard (control file) ==="
    )
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
        state_mod.save_control(
            control_path,
            {
                "action": "CLOSE_POSITION",
                "symbol": "TESTUSDT",
                "requested_at": state_mod.now_ms(),
            },
        )
        check_manual_control(FakeTradeClient(), cfg_ctrl, filters_cache, state3)
        assert (
            state3["current_symbol"] is None
        ), "Posisi harusnya tertutup oleh perintah manual yang valid"
        assert not state_mod.load_control(
            control_path
        ), "Control file harus terhapus setelah diproses"
        print(
            "  Perintah valid untuk simbol yang sesuai -> posisi ditutup, control file dibersihkan -> OK"
        )

        state4 = dict(DEFAULT_STATE)
        state4["current_symbol"] = "TESTUSDT"
        state4["entry_price"] = 100.0
        state4["qty"] = 1.0
        state4["entry_time"] = state_mod.now_ms()
        state_mod.save_control(
            control_path,
            {
                "action": "CLOSE_POSITION",
                "symbol": "TESTUSDT",
                "requested_at": state_mod.now_ms() - 10 * 60 * 1000,
            },
        )
        check_manual_control(FakeTradeClient(), cfg_ctrl, filters_cache, state4)
        assert (
            state4["current_symbol"] == "TESTUSDT"
        ), "Perintah kadaluarsa (>2 menit) harus DIABAIKAN"
        print(
            "  Perintah kadaluarsa (10 menit lalu) -> diabaikan, posisi tetap terbuka -> OK"
        )

        state5 = dict(DEFAULT_STATE)
        state5["current_symbol"] = "LAINUSDT"
        state5["entry_price"] = 50.0
        state5["qty"] = 2.0
        state5["entry_time"] = state_mod.now_ms()
        state_mod.save_control(
            control_path,
            {
                "action": "CLOSE_POSITION",
                "symbol": "TESTUSDT",
                "requested_at": state_mod.now_ms(),
            },
        )
        check_manual_control(FakeTradeClient(), cfg_ctrl, filters_cache, state5)
        assert (
            state5["current_symbol"] == "LAINUSDT"
        ), "Perintah untuk simbol berbeda dari posisi aktif harus DIABAIKAN"
        print(
            "  Perintah untuk simbol yang sudah tidak dipegang -> diabaikan, posisi lain tetap aman -> OK"
        )

        state6 = dict(DEFAULT_STATE)
        state_mod.save_control(
            control_path,
            {
                "action": "CLOSE_POSITION",
                "symbol": "TESTUSDT",
                "requested_at": state_mod.now_ms(),
            },
        )
        check_manual_control(FakeTradeClient(), cfg_ctrl, filters_cache, state6)
        assert (
            state6["current_symbol"] is None
        ), "Tanpa posisi terbuka, perintah manual harus diabaikan dengan aman"
        print(
            "  Tidak ada posisi terbuka saat perintah diproses -> diabaikan dengan aman, tidak error -> OK"
        )

    print("\n=== SELFTEST: dust sweep ke BNB setelah posisi ditutup ===")

    class FakeDustClient:

        def __init__(self, convertible_assets):
            self.convertible_assets = convertible_assets
            self.convert_calls = []
            self.fail_convert = False

        def get_dust_convertible(self, account_type="SPOT"):
            return {
                "details": [
                    {"asset": a, "amountFree": "1.0", "toBNB": "0.0001"}
                    for a in self.convertible_assets
                ]
            }

        def convert_dust(self, assets, account_type="SPOT"):
            self.convert_calls.append(list(assets))
            if self.fail_convert:
                raise BinanceAPIError(400, -5001, "Asset not supported (simulasi)")
            return {
                "totalTransfered": "0.0001",
                "totalServiceCharge": "0.000002",
                "transferResult": [],
            }

    assert cfg["USE_DUST_SWEEP"], "USE_DUST_SWEEP harusnya True di config default"
    cfg = dict(cfg)
    cfg["MODE"] = "LIVE"

    fake_a = FakeDustClient(convertible_assets=["PEPE"])
    try_dust_sweep(fake_a, cfg, "PEPEUSDT")
    assert fake_a.convert_calls == [
        ["PEPE"]
    ], f"Harusnya convert PEPE saja, dapat: {fake_a.convert_calls}"
    print(
        "  Sisa PEPE terdaftar dust convertible -> convert_dust(['PEPE']) dipanggil -> OK"
    )

    fake_b = FakeDustClient(convertible_assets=[])
    try_dust_sweep(fake_b, cfg, "PEPEUSDT")
    assert (
        fake_b.convert_calls == []
    ), "Tidak boleh convert kalau asset tidak terdaftar sebagai dust"
    print(
        "  Sisa PEPE TIDAK terdaftar dust convertible -> convert_dust tidak dipanggil -> OK"
    )

    fake_c = FakeDustClient(convertible_assets=["USDT", "BNB"])
    try_dust_sweep(fake_c, cfg, "BNBUSDT")
    assert (
        fake_c.convert_calls == []
    ), "BNB tidak boleh pernah dikonversi (proteksi keras)"
    print(
        "  Simbol dengan base asset BNB -> TIDAK PERNAH dikonversi (proteksi modal) -> OK"
    )

    cfg_paper = dict(cfg)
    cfg_paper["MODE"] = "PAPER"
    fake_d = FakeDustClient(convertible_assets=["PEPE"])
    try_dust_sweep(fake_d, cfg_paper, "PEPEUSDT")
    assert (
        fake_d.convert_calls == []
    ), "Mode PAPER tidak boleh memanggil convert_dust (endpoint /sapi bertanda tangan)"
    print("  Mode PAPER -> dust sweep dilewati, tidak ada panggilan /sapi -> OK")

    fake_e = FakeDustClient(convertible_assets=["PEPE"])
    fake_e.fail_convert = True
    try:
        try_dust_sweep(fake_e, cfg, "PEPEUSDT")
        gagal_ditangani = True
    except BinanceAPIError:
        gagal_ditangani = False
    assert (
        gagal_ditangani
    ), "Kegagalan convert_dust (mis. rate limit) harus ditangani, bukan dilempar ke pemanggil"
    print(
        "  convert_dust gagal (simulasi rate limit Binance) -> ditangani dengan aman, tidak crash -> OK"
    )

    cfg_no_dust = dict(cfg)
    cfg_no_dust["USE_DUST_SWEEP"] = False
    fake_f = FakeDustClient(convertible_assets=["PEPE"])
    try_dust_sweep(fake_f, cfg_no_dust, "PEPEUSDT")
    assert (
        fake_f.convert_calls == []
    ), "USE_DUST_SWEEP=False harusnya menonaktifkan fitur ini sepenuhnya"
    print("  USE_DUST_SWEEP=False -> fitur nonaktif total -> OK")

    print("\n=== SELFTEST: get_equity None-safe saat API harga gangguan (T-05) ===")

    class EquityClient:
        def __init__(self, fail_price=False):
            self.fail_price = fail_price

        def get_account(self):
            return {
                "balances": [
                    {"asset": "USDT", "free": "500", "locked": "0"},
                    {"asset": "TEST", "free": "1.0", "locked": "0"},
                ]
            }

        def get_price(self, symbol):
            if self.fail_price:
                raise BinanceAPIError(500, None, "simulasi gangguan API")
            return 100.0

    st_eq = dict(DEFAULT_STATE)
    st_eq["current_symbol"] = "TESTUSDT"
    st_eq["qty"] = 1.0
    cfg_equity = dict(cfg)
    cfg_equity["MODE"] = "PAPER"
    eq_ok = get_equity(EquityClient(), cfg_equity, st_eq)
    assert abs(eq_ok - 600.0) < 1e-9, f"equity harus 500 + 1x100 = 600, dapat {eq_ok}"
    eq_none = get_equity(EquityClient(fail_price=True), cfg_equity, st_eq)
    assert (
        eq_none is None
    ), "API harga gagal -> equity harus None, BUKAN 500 (posisi hilang semu)"
    print(
        "  Equity normal = 600; API gagal -> None (bukan angka keliru pemicu stop semu) -> OK"
    )

    print(
        "\n=== SELFTEST: CLOSE_ALL_AT_LIMIT benar-benar menutup posisi (K-01/T-06) ==="
    )
    cfg_limit = dict(cfg_exit)
    cfg_limit["CLOSE_ALL_AT_LIMIT"] = True

    st_lim = dict(DEFAULT_STATE)
    st_lim["current_symbol"] = "TESTUSDT"
    st_lim["entry_price"] = 100.0
    st_lim["qty"] = 1.0
    st_lim["entry_time"] = state_mod.now_ms()
    st_lim["dd_stopped"] = True
    st_lim["dd_stop_until"] = state_mod.now_ms() + 3600 * 1000
    maybe_force_close_at_risk_limit(
        FakeTradeClient(), cfg_limit, filters_cache, st_lim, True, 100.0
    )
    assert (
        st_lim["current_symbol"] is None
    ), "Posisi harus ditutup paksa saat DD stop aktif"
    assert (
        st_lim["_limit_close_done"] is True
    ), "Penanda episode harus di-set setelah penutupan paksa"
    print(
        "  DD stop aktif + posisi terbuka -> SELL paksa, _limit_close_done=True -> OK"
    )

    st_lim2 = dict(DEFAULT_STATE)
    st_lim2["current_symbol"] = "TESTUSDT"
    st_lim2["qty"] = 1.0
    st_lim2["dd_stopped"] = True
    st_lim2["_limit_close_done"] = True
    klien2 = FakeTradeClient()
    maybe_force_close_at_risk_limit(
        klien2, cfg_limit, filters_cache, st_lim2, True, 100.0
    )
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
    maybe_force_close_at_risk_limit(
        klien3, cfg_limit_off, filters_cache, st_lim3, True, 100.0
    )
    assert klien3.orders == [], "CLOSE_ALL_AT_LIMIT=False tidak boleh menutup posisi"
    print("  CLOSE_ALL_AT_LIMIT=False -> posisi dibiarkan, hanya entry dijeda -> OK")

    st_lim4 = dict(DEFAULT_STATE)
    st_lim4["_limit_close_done"] = True
    maybe_force_close_at_risk_limit(
        FakeTradeClient(), cfg_limit, filters_cache, st_lim4, False, 100.0
    )
    assert (
        st_lim4["_limit_close_done"] is False
    ), "Penanda harus direset saat episode stop berakhir"
    print("  Episode stop berakhir -> penanda direset otomatis -> OK")

    print(
        "\n=== SELFTEST: rekonsiliasi state vs saldo exchange saat startup (S-02) ==="
    )

    class ReconClient:
        def __init__(self, balances, fail=False):
            self.balances = balances
            self.fail = fail
            self.calls = 0

        def get_account(self):
            self.calls += 1
            if self.fail:
                raise BinanceAPIError(500, None, "simulasi gangguan")
            return {
                "balances": [
                    {"asset": a, "free": str(v), "locked": "0"}
                    for a, v in self.balances.items()
                ]
            }

    cfg_rec = dict(cfg)
    cfg_rec["MODE"] = "PAPER"
    cfg_rec["QUOTE_ASSET"] = "USDT"
    with tempfile.TemporaryDirectory() as tmprec:
        cfg_rec["STATE_FILE"] = f"{tmprec}/state.json"

        st_r = dict(DEFAULT_STATE)
        st_r["current_symbol"] = "PEPEUSDT"
        st_r["qty"] = 1000.0
        st_r["entry_price"] = 0.01
        reconcile_state_with_exchange(ReconClient({}), cfg_rec, st_r)
        assert (
            st_r["current_symbol"] is None and st_r["qty"] == 0.0
        ), "Saldo 0 -> posisi hantu harus direset"
        print("  Saldo 0 di exchange -> posisi hantu direset -> OK")

        st_r2 = dict(DEFAULT_STATE)
        st_r2["current_symbol"] = "SOLUSDT"
        st_r2["qty"] = 10.0
        st_r2["entry_price"] = 100.0
        reconcile_state_with_exchange(ReconClient({"SOL": 9.5}), cfg_rec, st_r2)
        assert (
            abs(st_r2["qty"] - 9.5) < 1e-12 and st_r2["entry_price"] == 100.0
        ), "Qty harus disesuaikan ke saldo nyata"
        print("  Qty state > saldo nyata -> qty disesuaikan -> OK")

        cl_idle = ReconClient({})
        st_idle = dict(DEFAULT_STATE)
        reconcile_state_with_exchange(cl_idle, cfg_rec, st_idle)
        assert (
            cl_idle.calls == 1 and not st_idle["reconciliation_required"]
        ), "State kosong harus cek saldo sekali tetapi tidak boleh memblokir akun benar-benar kosong"
        st_r4 = dict(DEFAULT_STATE)
        st_r4["current_symbol"] = "PEPEUSDT"
        st_r4["qty"] = 10.0
        reconcile_state_with_exchange(ReconClient({}, fail=True), cfg_rec, st_r4)
        assert (
            st_r4["current_symbol"] == "PEPEUSDT"
        ), "API gagal -> state lama dipertahankan"
        print("  State kosong -> cek saldo sekali; API gagal -> aman tanpa crash -> OK")

    print("\n=== SELFTEST: filter usia listing (S-08) ===")
    _listing_age_cache.clear()

    class AgeClient:
        def __init__(self, first_open):
            self.first_open = first_open
            self.calls = 0

        def get_klines(
            self, symbol, interval, limit=500, start_time_ms=None, end_time_ms=None
        ):
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
    assert (
        listing_age_days(AgeClient(None), "KOSONGUSDT", NOW10) == 0.0
    ), "Tanpa riwayat -> usia 0 (akan ditolak ambang minimum)"
    print(
        "  Usia 10 hari / 2 hari dihitung benar, cache hemat API, tanpa riwayat -> 0 -> OK"
    )

    print("\n=== SELFTEST: konfirmasi paralel identik dengan serial (L-03) ===")
    MS_HARI = 86_400_000
    NOW_T = 100 * MS_HARI

    def _candle_5m(indeks: int, price: float, volume: float) -> list:
        open_time = NOW_T + indeks * 5 * 60_000
        return [
            open_time,
            str(price),
            str(price),
            str(price),
            str(price),
            str(volume),
            open_time + 5 * 60_000 - 1,
            str(volume * price),
            0,
            "0",
            "0",
            "0",
        ]

    def _konfirmasi_generator(lonjakan: bool = False):
        rows = [_candle_5m(i, 100.0 + i * 0.01, 1_000.0) for i in range(20)]
        if lonjakan:
            rows.append(_candle_5m(20, 100.2, 5_000.0))
        else:
            rows.append(_candle_5m(20, 100.2, 1_000.0))
        return rows

    cfg_konfirmasi = {
        "QUOTE_ASSET": "USDT",
        "MIN_QUOTE_VOLUME_USDT_24H": 1_000_000,
        "PUMP_MIN_24H_CHANGE_PCT": 6.0,
        "PUMP_MAX_24H_CHANGE_PCT": 10.0,
        "BTC_FILTER_ENABLED": False,
        "EXTRA_EXCLUDE_SYMBOLS": [],
        "ROLLING_VOLUME_FILTER_ENABLED": True,
        "ROLLING_VOLUME_LOOKBACK_BARS": 20,
        "ROLLING_VOLUME_SURGE_MULT": 2.0,
        "ROLLING_VOLUME_CONFIRMATION_BARS": 1,
        "CONFIRM_LOOKBACK_BARS": 60,
        "ATR_PERIOD": 14,
    }

    ticker_konfirmasi = [
        {
            "symbol": "LONJAKUSDT",
            "priceChangePercent": "8.0",
            "quoteVolume": "5000000",
            "lastPrice": "100.0",
        },
        {
            "symbol": "DATARUSDT",
            "priceChangePercent": "8.0",
            "quoteVolume": "4000000",
            "lastPrice": "100.0",
        },
    ]
    sumber_konfirmasi = {
        "LONJAKUSDT": _konfirmasi_generator(True),
        "DATARUSDT": _konfirmasi_generator(False),
    }
    waktu_tutup = {"ms": NOW_T + 21 * 5 * 60_000}
    asli_now_ms = state_mod.now_ms
    state_mod.now_ms = lambda: waktu_tutup["ms"]
    try:

        def _serial(simbol: str):
            raw = sumber_konfirmasi[simbol]
            closed = [
                k
                for k in strategy.parse_klines(raw)
                if k.close_time < state_mod.now_ms()
            ]
            lookback = strategy.confirm_window_bars(cfg_konfirmasi)
            return closed[-lookback:]

        def _paralel(simbol2):
            lookback = strategy.confirm_window_bars(cfg_konfirmasi)
            hasil = {}
            for s in simbol2:
                closed = [
                    k
                    for k in strategy.parse_klines(sumber_konfirmasi[s])
                    if k.close_time < state_mod.now_ms()
                ]
                hasil[s] = closed[-lookback:]
            return hasil

        pilih_serial = scanner.find_best_candidate(
            ticker_konfirmasi, _serial, cfg_konfirmasi, None
        )

        pilih_paralel = scanner.find_best_candidate(
            ticker_konfirmasi,
            _serial,
            cfg_konfirmasi,
            None,
            klines_fetcher_many=_paralel,
            prewarm_fn=lambda simbol2: None,
        )

        assert pilih_serial is not None, "kandidat dengan lonjakan harus ditemukan"
        assert pilih_paralel is not None, "jalur paralel harus menemukan kandidat"
        assert (
            pilih_serial.symbol == "LONJAKUSDT"
        ), f"yang dipilih harus LONJAKUSDT, dapat {pilih_serial.symbol}"
        assert (
            pilih_paralel.symbol == pilih_serial.symbol
        ), "jalur paralel harus memilih simbol yang sama dengan jalur serial"
        assert (
            pilih_paralel.confirm_reason == pilih_serial.confirm_reason
        ), "alasan konfirmasi harus sama persis antara serial dan paralel"

        def _paralel_rusak(simbol2):
            raise RuntimeError("jaringan putus")

        pilih_gagal = scanner.find_best_candidate(
            ticker_konfirmasi,
            _serial,
            cfg_konfirmasi,
            None,
            klines_fetcher_many=_paralel_rusak,
        )
        assert (
            pilih_gagal is not None and pilih_gagal.symbol == "LONJAKUSDT"
        ), "kegagalan pengambilan paralel harus jatuh ke mode serial"
    finally:
        state_mod.now_ms = asli_now_ms
    print("  pilihan, alasan, dan fallback saat paralel gagal -> OK")

    print("\n=== SELFTEST: snapshot ticker 24 jam dan timpaan harga WS (L-04) ===")
    from market.market_data import MarketDataProvider
    from market.market_ws import MarketWebSocket

    ws_uji = MarketWebSocket.__new__(MarketWebSocket)
    from market.market_ws import _StreamCache

    ws_uji._prices = _StreamCache()
    ws_uji._mini = _StreamCache()
    ws_uji._mini_lock = __import__("threading").RLock()
    ws_uji._mini_arr_ts = 0.0
    ws_uji._handle_payload(
        [
            {
                "e": "24hrMiniTicker",
                "s": "AAAUSDT",
                "c": "110.0",
                "o": "100.0",
                "v": "1000",
                "q": "105000",
            },
            {
                "e": "24hrMiniTicker",
                "s": "BBBUSDT",
                "c": "90.0",
                "o": "100.0",
                "v": "2000",
                "q": "190000",
            },
            {
                "e": "24hrMiniTicker",
                "s": "RUSAKUSDT",
                "c": "0",
                "o": "100.0",
                "v": "0",
                "q": "0",
            },
        ]
    )
    data_ws, usia_ws = ws_uji.all_mini_tickers()
    assert len(data_ws) == 3, f"snapshot harus 3 simbol, dapat {len(data_ws)}"
    assert usia_ws < 1.0, "usia snapshot harus segar"

    class _WsStub:
        def __init__(self, data, usia):
            self._data, self._usia = data, usia

        def is_connected(self):
            return True

        def all_mini_tickers(self):
            return self._data, self._usia

    class _RestStub:
        def __init__(self):
            self.panggil = 0

        def get_ticker_24hr_all(self):
            self.panggil += 1
            return [
                {
                    "symbol": "AAAUSDT",
                    "lastPrice": "110.0",
                    "priceChangePercent": "10.0",
                    "quoteVolume": "105000",
                }
            ]

        def close(self):
            return None

    def _daftar_ticker(jumlah: int = 3) -> list:
        return [
            {
                "symbol": f"SIMBOL{i}USDT",
                "lastPrice": "100.0",
                "priceChangePercent": "8.0",
                "quoteVolume": "2000000",
            }
            for i in range(jumlah)
        ] + [
            {
                "symbol": "AAAUSDT",
                "lastPrice": "100.0",
                "priceChangePercent": "8.0",
                "quoteVolume": "2000000",
            }
        ]

    cfg_ws = {
        "USE_WEBSOCKET": False,
        "MAX_MARKET_DATA_AGE_SECONDS": 10.0,
        "WS_LAST_PRICE_OVERLAY_ENABLED": True,
        "TICKER_SNAPSHOT_TTL_SECONDS": 0,
        "MARKET_DATA_WORKERS": 1,
        "PAPER_DEPTH_LIMIT": 100,
        "RATE_LIMIT_STATE_FILE": None,
        "RATE_LIMIT_WEIGHT_LIMIT": 6000,
        "RATE_LIMIT_SAFETY_MARGIN": 100,
    }

    provider = MarketDataProvider(cfg_ws)
    provider.rest = _RestStub()
    provider.rest.get_ticker_24hr_all = lambda: _daftar_ticker()
    provider.get_ticker_24hr_all()
    provider.get_ticker_24hr_all()
    assert provider.rest.panggil == 0, "stub dipakai, penghitung tidak relevan"
    provider.close()

    class _RestHitung:
        def __init__(self, daftar):
            self.panggil = 0
            self.daftar = daftar

        def get_ticker_24hr_all(self):
            self.panggil += 1
            return self.daftar

        def close(self):
            return None

    rest_hitung = _RestHitung(_daftar_ticker())
    provider = MarketDataProvider(dict(cfg_ws, TICKER_SNAPSHOT_TTL_SECONDS=30))
    provider.rest = rest_hitung
    pertama = provider.get_ticker_24hr_all()
    kedua = provider.get_ticker_24hr_all()
    ketiga = provider.get_ticker_24hr_all()
    assert (
        rest_hitung.panggil == 1
    ), f"TTL aktif hanya boleh mengunduh sekali, dapat {rest_hitung.panggil}"
    assert len(pertama) == len(kedua) == len(ketiga) == 4, "isi snapshot harus sama"
    assert provider._ticker_refresher is not None, "penyegar latar harus hidup"

    with provider._ticker_lock:
        provider._ticker_snapshot_ts = time.monotonic() - 10_000.0
    provider.get_ticker_24hr_all()
    assert rest_hitung.panggil == 2, "snapshot terlalu tua harus disegarkan sinkron"
    provider.close()

    data_overlay = {
        "AAAUSDT": {"c": "123.5", "o": "100.0", "v": "1", "q": "1"},
        "SIMBOL0USDT": {"c": "0", "o": "100.0", "v": "1", "q": "1"},
    }
    provider = MarketDataProvider(dict(cfg_ws, TICKER_SNAPSHOT_TTL_SECONDS=30))
    provider.rest = _RestHitung(_daftar_ticker())
    provider._use_ws = True
    provider._ensure_ws = lambda: _WsStub(data_overlay, 0.5)
    snap = provider.get_ticker_24hr_all()
    peta_snap = {t["symbol"]: t for t in snap}
    assert (
        float(peta_snap["AAAUSDT"]["lastPrice"]) == 123.5
    ), "harga terakhir harus ditimpa dengan harga WebSocket terbaru"
    assert (
        float(peta_snap["AAAUSDT"]["priceChangePercent"]) == 8.0
    ), "persentase perubahan tidak boleh berubah oleh timpaan"
    assert (
        float(peta_snap["AAAUSDT"]["quoteVolume"]) == 2000000.0
    ), "volume kuotasi tidak boleh berubah oleh timpaan"
    assert (
        float(peta_snap["SIMBOL0USDT"]["lastPrice"]) == 100.0
    ), "harga tidak valid dari WebSocket harus diabaikan"
    assert (
        float(peta_snap["SIMBOL1USDT"]["lastPrice"]) == 100.0
    ), "simbol tanpa data WebSocket harus tetap memakai harga snapshot"

    cfg_timpa = {
        "QUOTE_ASSET": "USDT",
        "MIN_QUOTE_VOLUME_USDT_24H": 1_000_000,
        "PUMP_MIN_24H_CHANGE_PCT": 6.0,
        "PUMP_MAX_24H_CHANGE_PCT": 10.0,
        "BTC_FILTER_ENABLED": False,
        "EXTRA_EXCLUDE_SYMBOLS": [],
    }
    pilih_timpa = scanner.filter_and_rank_candidates(snap, cfg_timpa, None)
    pilih_asli = scanner.filter_and_rank_candidates(
        provider.rest.daftar, cfg_timpa, None
    )
    assert [c.symbol for c in pilih_timpa] == [
        c.symbol for c in pilih_asli
    ], "timpaan harga tidak boleh mengubah kandidat yang lolos"

    provider._ensure_ws = lambda: _WsStub(data_overlay, 999.0)
    snap_basi = provider.get_ticker_24hr_all()
    peta_basi = {t["symbol"]: t for t in snap_basi}
    assert (
        float(peta_basi["AAAUSDT"]["lastPrice"]) == 100.0
    ), "data WebSocket basi tidak boleh dipakai untuk menimpa"

    provider._ws_overlay_enabled = False
    provider._ensure_ws = lambda: _WsStub(data_overlay, 0.5)
    snap_mati = provider.get_ticker_24hr_all()
    peta_mati = {t["symbol"]: t for t in snap_mati}
    assert (
        float(peta_mati["AAAUSDT"]["lastPrice"]) == 100.0
    ), "timpaan nonaktif harus menyisakan harga snapshot apa adanya"
    provider.close()
    print("  TTL, penyegar sinkron, timpaan harga, dan paritas kandidat -> OK")

    print("\n=== SELFTEST: pengambilan klines paralel dan sesi per thread (L-05) ===")
    from trading.clients.binance_client import BinanceSpotClient

    class _RestParalel:
        def __init__(self, delay=0.0):
            self.delay = delay
            self.jejak = []
            self._lock = __import__("threading").Lock()

        def get_klines(
            self, symbol, interval, limit=500, start_time_ms=None, end_time_ms=None
        ):
            with self._lock:
                self.jejak.append(symbol)
            if self.delay:
                __import__("time").sleep(self.delay)
            if symbol == "GAGALUSDT":
                raise RuntimeError("simbol tidak dikenal")
            return [[0, "1", "1", "1", "1", "1", 0, "1"]]

    rest_paralel = _RestParalel()
    provider2 = MarketDataProvider(dict(cfg_ws, MARKET_DATA_WORKERS=8))
    provider2.rest = rest_paralel
    peta = provider2.get_klines_many(
        ["AAAUSDT", "BBBUSDT", "CCCUSDT", "AAAUSDT", "GAGALUSDT"], "5m", 61
    )
    assert set(peta) == {
        "AAAUSDT",
        "BBBUSDT",
        "CCCUSDT",
        "GAGALUSDT",
    }, f"simbol duplikat harus digabung, dapat {sorted(peta)}"
    assert peta["GAGALUSDT"] is None, "simbol gagal harus None, bukan exception"
    assert len(peta["AAAUSDT"]) == 1, "simbol valid harus tetap berisi candle"
    assert (
        len(rest_paralel.jejak) == 4
    ), f"harus 4 panggilan (duplikat digabung), dapat {len(rest_paralel.jejak)}"

    rest_serial = _RestParalel(delay=0.05)
    provider3 = MarketDataProvider(dict(cfg_ws, MARKET_DATA_WORKERS=1))
    provider3.rest = rest_serial
    import time as _t

    mulai = _t.monotonic()
    provider3.get_klines_many(["A", "B", "C", "D"], "5m", 61)
    lama_serial = _t.monotonic() - mulai
    rest_paralel2 = _RestParalel(delay=0.05)
    provider4 = MarketDataProvider(dict(cfg_ws, MARKET_DATA_WORKERS=8))
    provider4.rest = rest_paralel2
    mulai = _t.monotonic()
    provider4.get_klines_many(["A", "B", "C", "D"], "5m", 61)
    lama_paralel = _t.monotonic() - mulai
    assert (
        lama_serial >= 0.18
    ), f"mode serial harus menumpuk delay, dapat {lama_serial:.3f}s"
    assert (
        lama_paralel < lama_serial * 0.75
    ), f"mode paralel harus jauh lebih cepat ({lama_paralel:.3f}s vs {lama_serial:.3f}s)"

    klien = BinanceSpotClient(
        "", "", "http://127.0.0.1:1", allow_signed=False, rate_limit_state_file=None
    )
    try:
        id_utama = id(klien.session)
        assert id(klien.session) == id_utama, "sesi harus stabil dalam satu thread"
        terkumpul = {}

        def _baca():
            terkumpul[id(__import__("threading").current_thread())] = [
                id(klien.session),
                id(klien.session),
            ]

        utas = [__import__("threading").Thread(target=_baca) for _ in range(3)]
        for u in utas:
            u.start()
        for u in utas:
            u.join()
        semua_id = [i for pasang in terkumpul.values() for i in pasang]
        assert len(set(semua_id)) == len(
            terkumpul
        ), "tiap thread harus punya sesi sendiri, dan stabil di thread itu"
        assert id_utama not in set(
            semua_id
        ), "thread lain tidak boleh memakai sesi thread utama"
    finally:
        klien.close()
    print(
        "  dedup simbol, isolasi kegagalan, paralel lebih cepat, sesi per thread -> OK"
    )

    print(
        "\n=== SELFTEST: gerbang trend timeframe tinggi (cache H1 + integrasi scanner) ==="
    )
    MS_JAM = 3_600_000
    # Memakai ulang fixture konfirmasi 5m dari selftest paritas (L-03) supaya yang
    # benar-benar diuji di sini hanya gerbang trend-nya.
    cfg_trend = dict(
        cfg_konfirmasi,
        CONFIRM_INTERVAL="5m",
        TREND_FILTER_ENABLED=True,
        TREND_INTERVAL="1h",
        TREND_EMA_FAST=20,
        TREND_EMA_SLOW=50,
        TREND_ADX_PERIOD=14,
        TREND_ADX_MIN=20.0,
        TREND_LOOKBACK_BARS=120,
    )

    def _baris_h1(arah: float, jumlah: int = 130, sekarang_ms: int = 0) -> list:
        """Baris klines H1 format Binance, berakhir di bucket terakhir yang tutup."""
        batas = ((int(sekarang_ms) // MS_JAM) - 1) * MS_JAM
        rows = []
        p = 100.0
        for i in range(jumlah):
            p *= 1.0 + arah * 0.002
            open_time = batas - (jumlah - 1 - i) * MS_JAM
            rows.append(
                [
                    open_time,
                    str(p * 0.999),
                    str(p * 1.003),
                    str(p * 0.997),
                    str(p),
                    "1000",
                    open_time + MS_JAM - 1,
                    str(p * 1000),
                    10,
                    "500",
                    "500000",
                    "0",
                ]
            )
        return rows

    class KlienH1:
        def __init__(self, rows, gagal: bool = False):
            self.rows = rows
            self.calls = 0
            self.gagal = gagal
            self.limit_terakhir = None

        def get_klines(
            self, symbol, interval, limit=500, start_time_ms=None, end_time_ms=None
        ):
            self.calls += 1
            self.limit_terakhir = limit
            if self.gagal:
                raise RuntimeError("rate limit Binance")
            assert interval == "1h", interval
            return [list(r) for r in self.rows][-limit:]

    NOW_H1 = NOW_T + 21 * 5 * 60_000  # jam pindai yang sama dengan fixture konfirmasi
    klien_naik = KlienH1(_baris_h1(1.0, 130, NOW_H1))
    cache_trend = TrendCache(klien_naik, cfg_trend)
    jendela = cache_trend.window_klines("NAIKUSDT", NOW_H1)
    assert len(jendela) == 120, len(jendela)
    assert all(
        k.close_time < NOW_H1 for k in jendela
    ), "hanya candle tertutup yang boleh dipakai"
    assert jendela[-1].open_time == (NOW_H1 // MS_JAM - 1) * MS_JAM, jendela[
        -1
    ].open_time
    assert (
        jendela[-1].close_time < NOW_H1
    ), "candle terakhir harus benar-benar sudah tutup"
    assert (
        klien_naik.limit_terakhir == 121
    ), f"unduhan dibatasi jendela + 1 candle, dapat {klien_naik.limit_terakhir}"
    assert klien_naik.calls == 1, f"panggilan pertama harus 1, dapat {klien_naik.calls}"
    cache_trend.window_klines("NAIKUSDT", NOW_H1 + 60_000)
    assert klien_naik.calls == 1, "masih dalam jam yang sama tidak perlu unduh ulang"
    cache_trend.window_klines("NAIKUSDT", NOW_H1 + MS_JAM)
    assert (
        klien_naik.calls == 2
    ), f"jam berikutnya harus unduh ulang, dapat {klien_naik.calls}"
    verdict_naik = cache_trend.verdict("NAIKUSDT", NOW_H1)
    assert verdict_naik["ok"] and verdict_naik["bars"] == 120, verdict_naik["reason"]

    cache_turun = TrendCache(KlienH1(_baris_h1(-1.0, 130, NOW_H1)), cfg_trend)
    verdict_turun = cache_turun.verdict("TURUNUSDT", NOW_H1)
    assert (
        not verdict_turun["ok"] and "di bawah" in verdict_turun["reason"]
    ), verdict_turun["reason"]

    cache_pendek = TrendCache(KlienH1(_baris_h1(1.0, 40, NOW_H1)), cfg_trend)
    verdict_pendek = cache_pendek.verdict("BARUUSDT", NOW_H1)
    assert (
        not verdict_pendek["ok"] and "kurang" in verdict_pendek["reason"]
    ), verdict_pendek["reason"]

    cache_rusak = TrendCache(KlienH1([], gagal=True), cfg_trend)
    try:
        cache_rusak.provider("GAGALUSDT")
        raise AssertionError(
            "gagal ambil candle trend harus melempar error ke pemanggil"
        )
    except RuntimeError:
        pass
    print(
        "  cache: hanya candle tutup, jendela 120, hemat API per jam, gagal -> error terlihat"
    )

    asli_now = state_mod.now_ms
    state_mod.now_ms = lambda: NOW_H1
    try:
        hasil_naik = scanner.trend_verdict(
            "NAIKUSDT", cfg_trend, lambda s: cache_trend.window_klines(s, NOW_H1)
        )
        hasil_turun_scanner = scanner.trend_verdict(
            "TURUNUSDT", cfg_trend, lambda s: cache_turun.window_klines(s, NOW_H1)
        )

        def _rusak(_simbol):
            raise RuntimeError("jaringan putus")

        hasil_rusak = scanner.trend_verdict("XUSDT", cfg_trend, _rusak)
        hasil_nonaktif = scanner.trend_verdict(
            "XUSDT", dict(cfg_trend, TREND_FILTER_ENABLED=False), _rusak
        )
        assert hasil_naik["ok"] and not hasil_turun_scanner["ok"]
        assert (
            not hasil_rusak["ok"] and "gagal diambil" in hasil_rusak["reason"]
        ), hasil_rusak["reason"]
        assert hasil_nonaktif[
            "ok"
        ], "filter nonaktif tidak boleh memanggil penyedia trend"

        # integrasi di jalur pemilihan kandidat: konfirmasi 5m lolos, trend yang menentukan
        panggilan = {"n": 0}

        def provider_naik(_simbol):
            panggilan["n"] += 1
            return cache_trend.window_klines(_simbol, NOW_H1)

        pilih_ok = scanner.find_best_candidate(
            ticker_konfirmasi, _serial, cfg_trend, None, trend_provider=provider_naik
        )
        assert pilih_ok is not None and pilih_ok.symbol == "LONJAKUSDT", pilih_ok
        assert "trend 1h" in pilih_ok.confirm_reason, pilih_ok.confirm_reason
        assert pilih_ok.trend is not None and pilih_ok.trend["ok"]

        pilih_tolak = scanner.find_best_candidate(
            ticker_konfirmasi,
            _serial,
            cfg_trend,
            None,
            trend_provider=lambda s: cache_turun.window_klines(s, NOW_H1),
        )
        assert (
            pilih_tolak is None
        ), "trend H1 turun harus membuat semua kandidat ditolak"

        pilih_gagal = scanner.find_best_candidate(
            ticker_konfirmasi, _serial, cfg_trend, None, trend_provider=_rusak
        )
        assert pilih_gagal is None, "penyedia trend error harus fail closed"

        panggilan["n"] = 0
        pilih_mati = scanner.find_best_candidate(
            ticker_konfirmasi,
            _serial,
            dict(cfg_trend, TREND_FILTER_ENABLED=False),
            None,
            trend_provider=provider_naik,
        )
        assert (
            pilih_mati is not None and panggilan["n"] == 0
        ), "filter nonaktif tidak boleh menambah panggilan API trend"

        pilih_tanpa_provider = scanner.find_best_candidate(
            ticker_konfirmasi, _serial, cfg_trend, None
        )
        assert (
            pilih_tanpa_provider is None
        ), "filter trend aktif tanpa penyedia candle harus fail closed, bukan lolos"
        pilih_lama = scanner.find_best_candidate(
            ticker_konfirmasi,
            _serial,
            dict(cfg_trend, TREND_FILTER_ENABLED=False),
            None,
        )
        assert (
            pilih_lama is not None
        ), "pemanggil lama tanpa penyedia trend tetap jalan selama filter nonaktif"
    finally:
        state_mod.now_ms = asli_now
    print(
        "  scanner: lolos saat trend naik, ditolak saat turun, fail closed saat error,"
    )
    print("           tanpa panggilan API tambahan saat filter nonaktif -> OK")

    print("\nSEMUA SELFTEST LULUS.")
    print("(Selftest ini TIDAK menghubungi Binance sama sekali -- murni logika lokal.)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Pump Scanner Bot Binance Spot")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Jalankan audit logika murni (tanpa jaringan) lalu keluar.",
    )
    args = parser.parse_args()
    if args.selftest:
        selftest()
        return 0

    from infrastructure.network.file_descriptors import raise_fd_limit

    # Batas fd bawaan 1024 terlalu sempit untuk proses yang memegang lock file,
    # soket Binance, dan worker klines sekaligus (lihat OSError(24) 07-10-2026).
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

    runtime = BotRuntime(mode)
    try:
        with runtime as lifecycle:
            code = run(PUMP_CONFIG, lifecycle=lifecycle)
            runtime.finish(
                code, None if code == 0 else "Bot berhenti dengan kode error."
            )
            return code
    except (BotAlreadyRunningError, BotControlError) as exc:
        print(str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
