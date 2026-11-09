from __future__ import annotations

import math
from typing import NamedTuple, Optional

from strategy import entry_rule
from strategy import exit_atr


class Kline(NamedTuple):
    open_time: int
    open: float
    high: float
    low: float
    close: float
    close_time: int
    volume: float = 0.0
    quote_volume: float = 0.0


INTERVAL_MINUTES = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "6h": 360,
    "8h": 480,
    "12h": 720,
    "1d": 1440,
}

TREND_KLINE_LIMIT = 1000

MS_PER_HOUR = 3_600_000
TIME_STOP_REASON = "TIME_STOP"
MAX_HOLD_HOURS_LIMIT = entry_rule.MAX_HOLD_HOURS_LIMIT

ENTRY_DEFAULTS = dict(entry_rule.ENTRY_RULE_DEFAULTS)


def interval_to_ms(interval: str) -> int:
    menit = INTERVAL_MINUTES.get(str(interval).strip().lower())
    if menit is None:
        raise ValueError(
            f"Interval '{interval}' tidak dikenal; pilihan: "
            + ", ".join(sorted(INTERVAL_MINUTES, key=INTERVAL_MINUTES.get))
        )
    return int(menit) * 60_000


def last_closed_bar_close_ms(interval: str, now_ms: int) -> int:
    menit = INTERVAL_MINUTES.get(str(interval).strip().lower())
    if menit is None:
        raise ValueError(
            f"Interval '{interval}' tidak dikenal; pilihan: "
            + ", ".join(sorted(INTERVAL_MINUTES, key=INTERVAL_MINUTES.get))
        )
    batas = int(menit) * 60_000
    if batas <= 0:
        raise ValueError("Interval harus lebih besar dari nol")
    return ((int(now_ms) // batas) - 1) * batas + batas - 1


def parse_klines(raw: list) -> list[Kline]:
    out = []
    for idx, row in enumerate(raw):
        o = float(row[1])
        h = float(row[2])
        low_ = float(row[3])
        c = float(row[4])

        for nama, nilai in (("open", o), ("high", h), ("low", low_), ("close", c)):
            if nilai != nilai:
                raise ValueError(
                    f"Candle ke-{idx} punya {nama}=NaN. Data bursa rusak. "
                    "Dihentikan karena NaN membuat semua cek Stop Loss/Take "
                    "Profit diam-diam gagal."
                )
            if nilai in (float("inf"), float("-inf")):
                raise ValueError(
                    f"Candle ke-{idx} punya {nama} tak hingga. Data bursa rusak."
                )
            if nilai <= 0:
                raise ValueError(
                    f"Candle ke-{idx} punya {nama}={nilai}. Harga wajib > 0. "
                    "Data bursa rusak atau simbol sudah tidak diperdagangkan."
                )

        if h < low_:
            raise ValueError(
                f"Candle ke-{idx}: high ({h}) lebih kecil dari low ({low_})."
            )

        out.append(
            Kline(
                open_time=int(row[0]),
                open=o,
                high=h,
                low=low_,
                close=c,
                close_time=int(row[6]),
                volume=float(row[5]) if len(row) > 5 else 0.0,
                quote_volume=float(row[7]) if len(row) > 7 else 0.0,
            )
        )
    return out


def _interval_from_config(config: dict, key: str, default: str) -> str:
    raw = str(config.get(key, default) or default).strip().lower()
    if raw not in INTERVAL_MINUTES:
        raise ValueError(
            f"{key} '{raw}' tidak dikenal. Pilihan: "
            + ", ".join(sorted(INTERVAL_MINUTES, key=INTERVAL_MINUTES.get))
        )
    return raw


def confirm_interval(config: dict) -> str:
    return _interval_from_config(config, "CONFIRM_INTERVAL", "15m")


def entry_rule_enabled(config: dict) -> bool:
    return entry_rule.rule_enabled(config)


def required_lookback_bars(config: dict) -> int:
    return entry_rule.required_bars(config)


def exit_atr_params(config: dict) -> dict:
    return exit_atr.atr_params(config)


def exit_atr_breakeven_trigger(entry_price: float, atr: float, config: dict) -> float:
    params = exit_atr.atr_params(config)
    return exit_atr.breakeven_trigger_price(entry_price, atr, params["breakeven_mult"])


def exit_advance(
    state: dict, price: float, config: dict, *, atr: Optional[float] = None
) -> dict:
    return exit_atr.advance(state, price, config, atr=atr)


def exit_fixed_params(config: Optional[dict] = None) -> dict:
    return entry_rule.exit_fixed_params(config)


def entry_signal_params(config: dict) -> dict:
    return entry_rule.signal_params(config)


def momentum_interval(config: dict) -> str:
    return entry_rule.momentum_interval(config)


def atr_period(config: dict) -> int:
    return entry_rule.atr_period(config)


def volume_rise_params(config: dict) -> dict:
    return entry_rule.volume_rise_params(config)


def volume_rise_check(klines: list[Kline], config: dict) -> dict:
    return entry_rule.volume_rise_check(klines, config)


def volume_rise_bars(config: dict) -> int:
    return entry_rule.volume_rise_bars(config)


def entry_warmup_bars(config: dict, interval: str) -> int:
    if not entry_rule_enabled(config):
        return 0
    raw = str(interval).strip().lower()
    if raw not in INTERVAL_MINUTES:
        raise ValueError(
            f"Interval '{interval}' tidak dikenal; pilihan: "
            + ", ".join(sorted(INTERVAL_MINUTES, key=INTERVAL_MINUTES.get))
        )
    return int(required_lookback_bars(config))


def gate_warmup_bars(config: dict, interval: str) -> int:
    return entry_warmup_bars(config, interval)


def gate_warmup_ms(config: dict, interval: str = "15m") -> int:
    if not entry_rule_enabled(config):
        return 0
    return int(entry_warmup_bars(config, interval) * interval_to_ms(interval))


def compute_entry_exit_levels(
    entry_price: float,
    config: dict,
    *,
    anchor_low: Optional[float] = None,
    atr: Optional[float] = None,
    fallback_allowed: bool = False,
) -> dict:
    return entry_rule.exit_levels(
        entry_price,
        anchor_low,
        config,
        atr=atr,
        fallback_allowed=fallback_allowed,
    )


def resolve_exit_levels(
    config: dict,
    setup: Optional[object] = None,
    entry_price: Optional[float] = None,
) -> dict:
    if setup is None:
        setup = config.get("_setup")
    raw_entry = entry_price if entry_price is not None else config.get("_entry_price")
    if raw_entry is None and setup is not None:
        raw_entry = getattr(setup, "signal_close", None)
    try:
        entry = float(raw_entry or 0.0)
    except (TypeError, ValueError):
        entry = 0.0
    anchor = getattr(setup, "entry_low", None) if setup is not None else None
    atr_value = getattr(setup, "atr_value", None) if setup is not None else config.get("_atr")
    plan = entry_rule.exit_levels(entry, anchor, config, atr=atr_value, fallback_allowed=True)
    params = exit_atr.atr_params(config)
    valid = bool(plan["ok"] and plan["sl_price"] and plan["tp_price"] and 0 < plan["sl_price"] < entry < plan["tp_price"])
    return {
        "atr": plan["atr"],
        "atr_period": params["period"],
        "sl_mult": plan.get("sl_atr_buffer_mult", 0.0),
        "tp_mult": 0.0,
        "trail_mult": params["trail_mult"],
        "breakeven_mult": params["breakeven_mult"],
        "sl_pct": plan["sl_pct"] if valid else 0.0,
        "tp_pct": plan["tp_pct"] if valid else 0.0,
        "sl_price": plan["sl_price"] if valid else None,
        "tp_price": plan["tp_price"] if valid else None,
        "rr_ratio": plan["rr_ratio"] if valid else 0.0,
        "min_rr_ratio": plan["min_rr_ratio"],
        "be_trigger_pct": 0.0,
        "be_lock_pct": 0.0,
        "trail_start_pct": 0.0,
        "trail_step_pct": 0.0,
        "source": entry_rule.SL_ANCHOR_ATR if valid else "TIDAK_TERSEDIA",
        "note": plan["reason"],
    }


def evaluate_entry(
    klines: list[Kline],
    config: dict,
    signal_close_time_ms: Optional[int] = None,
) -> dict:
    return entry_rule.evaluate_entry_rule(klines, config, signal_close_time_ms)


def confirm_window_bars(config: dict) -> int:
    lookback = int(config.get("CONFIRM_LOOKBACK_BARS", 192) or 192)
    return max(1, min(1000, max(lookback, required_lookback_bars(config))))


def resolve_position_notional(config: dict, quote_free: float) -> dict:
    free = max(0.0, float(quote_free or 0.0))
    use_percent = bool(config.get("USE_RISK_PERCENT"))
    buffer_pct = max(0.0, float(config.get("BALANCE_BUFFER_PCT", 0.5) or 0.0))
    buffer_pct = min(buffer_pct, 100.0)

    if use_percent:
        spendable = free * (1.0 - buffer_pct / 100.0)
        requested = spendable * float(config.get("RISK_PERCENT", 25.0) or 0.0) / 100.0
        mode = "PERCENT"
    else:
        spendable = free
        requested = float(config.get("POSITION_SIZE_USDT", 5.0) or 0.0)
        mode = "FIXED"

    requested = max(0.0, requested)
    cap = float(config.get("MAX_POSITION_USDT", 0.0) or 0.0)
    cap_active = cap > 0.0 and requested > cap
    notional = min(requested, cap) if cap > 0.0 else requested
    effective_pct = (notional / free * 100.0) if free > 0 else 0.0

    return {
        "notional": notional,
        "requested_notional": requested,
        "mode": mode,
        "quote_free": free,
        "spendable_quote": spendable,
        "buffer_pct": buffer_pct if use_percent else 0.0,
        "cap": cap if cap > 0.0 else None,
        "cap_active": cap_active,
        "effective_pct_of_free": effective_pct,
    }


def backtest_buy_execution_price(
    open_price: float, spread_pct: float = 0.0, slippage_pct: float = 0.0
) -> float:
    price = float(open_price)
    adverse = (
        max(0.0, float(spread_pct)) / 200.0 + max(0.0, float(slippage_pct)) / 100.0
    )
    return price * (1.0 + adverse)


def backtest_sell_execution_price(
    price: float, spread_pct: float = 0.0, slippage_pct: float = 0.0
) -> float:
    raw = max(0.0, float(price))
    adverse = (
        max(0.0, float(spread_pct)) / 200.0 + max(0.0, float(slippage_pct)) / 100.0
    )
    return raw * max(0.0, 1.0 - adverse)


def atr(klines: list[Kline], period: int = 14) -> float | None:
    return entry_rule.atr_value(klines, period)


def max_hold_ms(config: dict, interval: str) -> int:
    cap_h = float(entry_rule.max_hold_hours(config))
    if cap_h <= 0.0:
        return 0
    if cap_h > MAX_HOLD_HOURS_LIMIT:
        raise ValueError(
            f"Batas tahan {cap_h:g} jam melampaui batas mesin "
            f"{MAX_HOLD_HOURS_LIMIT:g} jam."
        )
    bar_ms = interval_to_ms(interval)
    cap_ms = int(round(cap_h * MS_PER_HOUR))
    if cap_ms < 2 * bar_ms:
        raise ValueError(
            f"Batas tahan {cap_h:g} jam terlalu sempit untuk interval {interval}: "
            f"minimal 2 bar = {2 * bar_ms / MS_PER_HOUR:g} jam."
        )
    return cap_ms


def time_stop_due(entry_time: int, now_ms: int, cap_ms: int, bar_ms: int) -> bool:
    if int(cap_ms) <= 0 or int(bar_ms) <= 0:
        return False
    entry = int(entry_time or 0)
    now = int(now_ms or 0)
    if entry <= 0 or now <= 0:
        return False
    return (now - entry) + int(bar_ms) > int(cap_ms)
