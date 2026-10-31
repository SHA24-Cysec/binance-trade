from __future__ import annotations

import math
from typing import NamedTuple, Optional, Sequence

from strategy import entry_rule


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

STRUCTURE_DEFAULT_INTERVAL = "4h"
TIMING_DEFAULT_INTERVAL = "4h"
DIRECTION_DEFAULT_INTERVAL = STRUCTURE_DEFAULT_INTERVAL
TREND_KLINE_LIMIT = 1000

MS_PER_HOUR = 3_600_000
TIME_STOP_REASON = "TIME_STOP"
MAX_HOLD_HOURS_LIMIT = 720.0

ENTRY_DEFAULTS = dict(entry_rule.ENTRY_RULE_DEFAULTS)


def interval_ratio(target: str, source: str) -> int:
    source_minutes = INTERVAL_MINUTES.get(str(source).strip().lower())
    target_minutes = INTERVAL_MINUTES.get(str(target).strip().lower())
    if source_minutes is None:
        raise ValueError(f"Interval sumber '{source}' tidak dikenal.")
    if target_minutes is None:
        raise ValueError(f"Interval target '{target}' tidak dikenal.")
    if target_minutes % source_minutes:
        raise ValueError(
            f"Interval target '{target}' ({target_minutes} menit) harus kelipatan "
            f"bulat dari interval sumber '{source}' ({source_minutes} menit) "
            "supaya candle bisa dirangkai dari data yang sudah diunduh."
        )
    return target_minutes // source_minutes


def interval_to_ms(interval: str, default_minutes: int = 5) -> int:
    return int(INTERVAL_MINUTES.get(str(interval), default_minutes)) * 60_000


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
    return _interval_from_config(config, "CONFIRM_INTERVAL", TIMING_DEFAULT_INTERVAL)


def confirm_interval_minutes(config: dict) -> int:
    return int(INTERVAL_MINUTES[confirm_interval(config)])


def entry_rule_enabled(config: dict) -> bool:
    return entry_rule.rule_enabled(config)


def entry_bull_candles(config: dict) -> int:
    return entry_rule.bull_candles(config)


def entry_required_bars(config: dict) -> int:
    return entry_rule.required_bars(config)


def required_lookback_bars(config: dict) -> int:
    return entry_required_bars(config)



def exit_trailing_params(config: dict) -> dict:
    return entry_rule.trailing_params(config)


def exit_trailing_start_price(
    entry_price: float, stop_price: float, start_r: float
) -> float:
    return entry_rule.trailing_start_price(entry_price, stop_price, start_r)


def exit_trailing_stop_price(highest_price: float, pct: float) -> float:
    return entry_rule.trailing_stop_price(highest_price, pct)


def exit_fixed_params() -> dict:
    return dict(entry_rule.EXIT_FIXED)




def entry_breakeven_params(config: dict) -> dict:
    return entry_rule.breakeven_params(config)


def entry_breakeven_price(
    entry_price: float,
    sl_price: float,
    r_multiple: float,
    lock_pct: float = 0.0,
) -> float:
    return entry_rule.breakeven_price(entry_price, sl_price, r_multiple, lock_pct)


def entry_breakeven_trigger(
    entry_price: float,
    sl_price: float,
    r_multiple: float,
) -> float:
    return entry_rule.breakeven_trigger(entry_price, sl_price, r_multiple)


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


def gate_warmup_ms(config: dict, interval: str = "5m") -> int:
    if not entry_rule_enabled(config):
        return 0
    return int(entry_warmup_bars(config, interval) * interval_to_ms(interval))


def native_history_ms(config: dict) -> int:
    return 0


def compute_entry_exit_levels(
    entry_price: float,
    config: dict,
    *,
    anchor_low: Optional[float] = None,
    fallback_allowed: bool = False,
) -> dict:
    return entry_rule.exit_levels(
        entry_price,
        anchor_low,
        config,
        fallback_allowed=fallback_allowed,
    )


def resolve_exit_levels(
    config: dict,
    setup: Optional[object] = None,
    entry_price: Optional[float] = None,
) -> dict:
    if setup is None:
        setup = config.get("_setup")
    ep_raw = entry_price if entry_price is not None else config.get("_entry_price")
    ep = float(ep_raw) if ep_raw is not None and float(ep_raw) > 0 else 0.0

    buffer_pct = entry_rule.sl_buffer_pct(config)
    rr_target = entry_rule.min_rr_ratio(config)

    sl_price = config.get("_sl_price")
    tp_price = config.get("_tp_price")
    sl_pct = config.get("_sl_pct")
    tp_pct = config.get("_tp_pct")

    if setup is not None:
        if sl_price is None:
            sl_price = getattr(setup, "sl_price", None)
        if tp_price is None:
            tp_price = getattr(setup, "tp_price", None)
        if sl_pct is None:
            sl_pct = getattr(setup, "sl_pct", None)
        if tp_pct is None:
            tp_pct = getattr(setup, "tp_pct", None)
        if ep <= 0.0:
            sig_c = getattr(setup, "signal_close", None)
            if sig_c is not None and float(sig_c) > 0:
                ep = float(sig_c)

    if (
        ep > 0.0
        and sl_price is not None
        and tp_price is not None
        and 0.0 < float(sl_price) < ep < float(tp_price)
    ):
        sl_p = (ep - float(sl_price)) / ep * 100.0
        tp_p = (float(tp_price) - ep) / ep * 100.0
        sl_px = float(sl_price)
        tp_px = float(tp_price)
    elif (
        sl_pct is not None
        and tp_pct is not None
        and 0.0 < float(sl_pct) < 100.0
        and float(tp_pct) > 0.0
    ):
        sl_p = float(sl_pct)
        tp_p = float(tp_pct)
        sl_px = ep * (1.0 - sl_p / 100.0) if ep > 0.0 else None
        tp_px = ep * (1.0 + tp_p / 100.0) if ep > 0.0 else None
    else:
        anchor_low = getattr(setup, "entry_low", None) if setup is not None else None
        info = entry_rule.exit_levels(
            ep if ep > 0.0 else 100.0,
            anchor_low,
            config,
            fallback_allowed=True,
        )
        sl_p = float(info["sl_pct"])
        tp_p = float(info["tp_pct"])
        sl_px = float(info["sl_price"]) if ep > 0.0 else None
        tp_px = float(info["tp_price"]) if ep > 0.0 else None

    rr_ratio = (tp_p / sl_p) if sl_p > 0.0 else 0.0
    atr_nilai = getattr(setup, "atr_value", None) if setup is not None else None
    try:
        atr_nilai = float(atr_nilai) if atr_nilai is not None else None
    except (TypeError, ValueError):
        atr_nilai = None
    if atr_nilai is not None and atr_nilai <= 0.0:
        atr_nilai = None
    return {
        "atr": atr_nilai,
        "sl_pct": sl_p,
        "tp_pct": tp_p,
        "sl_price": sl_px,
        "tp_price": tp_px,
        "rr_ratio": rr_ratio,
        "sl_buffer_pct": buffer_pct,
        "min_rr_ratio": rr_target,
        "be_trigger_pct": 0.0,
        "be_lock_pct": 0.0,
        "trail_start_pct": 0.0,
        "trail_step_pct": 0.0,
        "source": "ENTRY_RULE",
        "note": (
            f"Level exit aturan entry: SL -{sl_p:.2f}% (di bawah low candle sinyal, "
            f"buffer {buffer_pct:g}%), TP +{tp_p:.2f}% (R:R 1:{rr_ratio:.2f})"
        ),
    }


def evaluate_entry(
    klines: list[Kline],
    config: dict,
    signal_close_time_ms: Optional[int] = None,
) -> dict:
    return entry_rule.evaluate_entry_rule(klines, config, signal_close_time_ms)













def confirm_window_bars(config: dict) -> int:
    lookback = int(config.get("CONFIRM_LOOKBACK_BARS", 60) or 60)
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
    period = int(period)
    if period <= 0 or not klines:
        return None
    trs: list[float] = []
    for i, candle in enumerate(klines):
        prev_close = klines[i - 1].close if i else candle.close
        trs.append(
            max(
                candle.high - candle.low,
                abs(candle.high - prev_close),
                abs(candle.low - prev_close),
            )
        )
    if len(trs) < period:
        return None
    result = sum(trs[:period]) / period
    for tr in trs[period:]:
        result = (result * (period - 1) + tr) / period
    return result if math.isfinite(result) and result > 0 else None




def max_hold_ms(config: dict, interval: str) -> int:
    cap_h = float(entry_rule.max_hold_hours())
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


def _resolve_minutes(val: int | str) -> int:
    if isinstance(val, str):
        s = val.strip().lower()
        if s in INTERVAL_MINUTES:
            return int(INTERVAL_MINUTES[s])
        return int(s)
    return int(val)


def aggregate_klines(
    klines: list[Kline],
    target_minutes: int | str,
    source_minutes: Optional[int | str] = None,
) -> list[Kline]:
    target_m = _resolve_minutes(target_minutes)
    if source_minutes is None:
        if len(klines) >= 2:
            source_m = max(
                1, (int(klines[1].open_time) - int(klines[0].open_time)) // 60_000
            )
        else:
            source_m = target_m
    else:
        source_m = _resolve_minutes(source_minutes)
    if target_m < source_m:
        target_m, source_m = source_m, target_m
    target_ms = target_m * 60_000
    source_ms = source_m * 60_000
    if source_ms <= 0 or target_ms % source_ms:
        raise ValueError(
            f"Interval sumber {source_m} menit tidak bisa dirangkai ke "
            f"{target_m} menit (harus pembagi bulat)."
        )
    rasio = target_ms // source_ms
    if rasio <= 1:
        return list(klines)

    tersusun: dict[int, list[Kline]] = {}
    for candle in sorted(klines, key=lambda k: int(k.open_time)):
        bucket = (int(candle.open_time) // target_ms) * target_ms
        tersusun.setdefault(bucket, []).append(candle)

    hasil: list[Kline] = []
    for bucket in sorted(tersusun):
        rows = tersusun[bucket]
        if len(rows) != rasio:
            continue
        if any(int(rows[i].open_time) != bucket + i * source_ms for i in range(rasio)):
            continue
        hasil.append(
            Kline(
                open_time=bucket,
                open=float(rows[0].open),
                high=max(float(k.high) for k in rows),
                low=min(float(k.low) for k in rows),
                close=float(rows[-1].close),
                close_time=bucket + target_ms - 1,
                volume=sum(float(k.volume) for k in rows),
                quote_volume=sum(float(k.quote_volume) for k in rows),
            )
        )
    return hasil


def _valid_candle_series(klines: list[Kline]) -> bool:
    if not klines:
        return False
    for candle in klines:
        try:
            o = float(candle.open)
            h = float(candle.high)
            l = float(candle.low)
            c = float(candle.close)
        except (AttributeError, TypeError, ValueError):
            return False
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
            return False
        if h < max(o, c, l) or l > min(o, c, h):
            return False
    return True






def btc_change_pct(klines: list[Kline], lookback_bars: int = 3) -> Optional[float]:
    look = max(1, int(lookback_bars or 3))
    if not klines or len(klines) < look + 1:
        return None
    prev_close = float(klines[-look - 1].close)
    last_close = float(klines[-1].close)
    if prev_close <= 0:
        return None
    return (last_close / prev_close - 1.0) * 100.0


def compute_btc_drop_pct(klines: list[Kline], lookback_bars: int = 3) -> Optional[float]:
    return btc_change_pct(klines, lookback_bars)


