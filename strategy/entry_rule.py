from __future__ import annotations

import math
from bisect import bisect_right
from typing import NamedTuple, Optional, Sequence

from strategy import exit_atr

ENTRY_RULE_DEFAULTS = {
    "ENTRY_RULE_ENABLED": True,
    "CONFIRM_INTERVAL": "15m",
    "DEMAND_EMA_PERIOD": 50,
    "DEMAND_IMPULSE_ATR_MULT": 1.0,
    "ENTRY_SL_ATR_BUFFER_MULT": 2.0,
    "ENTRY_ATR_PERIOD": exit_atr.DEFAULT_ATR_PERIOD,
    "EXIT_ATR_TRAIL_MULT": 3.0,
    "EXIT_ATR_BREAKEVEN_MULT": exit_atr.DEFAULT_BREAKEVEN_MULT,
    "EXIT_ATR_MAX_SL_PCT": 8.0,
    "ENTRY_MIN_RR_RATIO": 2.5,
    "EXIT_MAX_HOLD_HOURS": 24.0,
    "GAINER_VOLUME_FILTER_ENABLED": True,
    "GAINER_VOLUME_RISE_MULT": 2.0,
    "GAINER_VOLUME_AVG_DAYS": 3,
}

SIGNAL_PARAM_KEYS = ("DEMAND_EMA_PERIOD", "DEMAND_IMPULSE_ATR_MULT", "ENTRY_SL_ATR_BUFFER_MULT")
MOMENTUM_PARAM_KEYS = SIGNAL_PARAM_KEYS
ZONE_TIMEFRAMES = (("1h", 60), ("4h", 240))
TRIGGER_INTERVALS = ("15m", "30m")
EMA_PERIOD_MIN = 5
EMA_PERIOD_MAX = 60
KLINE_FETCH_LIMIT = 1000
MAX_CLOSED_BARS = 997
DEFAULT_LOOKBACK_BARS = 997
EXIT_PARAM_KEYS = exit_atr.ATR_PARAM_KEYS
EXIT_TAKE_PROFIT_ENABLED = True
EXIT_BREAKEVEN_ENABLED = True
EXIT_BREAKEVEN_R_MULTIPLE = exit_atr.DEFAULT_BREAKEVEN_MULT
EXIT_BREAKEVEN_LOCK_PCT = 0.0
EXIT_TRAILING_START_R = exit_atr.DEFAULT_SL_MULT
EXIT_TRAILING_ATR_MULT = exit_atr.DEFAULT_TRAIL_MULT
EXIT_TRAILING_MIN_PCT = 0.1
EXIT_TRAILING_MAX_PCT = 20.0
EXIT_MAX_HOLD_HOURS = 24.0
MAX_HOLD_HOURS_LIMIT = 720.0
EXIT_FALLBACK_SL_PCT = 1.0
MOMENTUM_CONFIRM_CANDLES = 1
EXIT_FIXED = {
    "take_profit_enabled": EXIT_TAKE_PROFIT_ENABLED,
    "breakeven_enabled": EXIT_BREAKEVEN_ENABLED,
    "breakeven_r_multiple": EXIT_BREAKEVEN_R_MULTIPLE,
    "breakeven_lock_pct": EXIT_BREAKEVEN_LOCK_PCT,
    "trailing_start_r": EXIT_TRAILING_START_R,
    "trailing_atr_mult": EXIT_TRAILING_ATR_MULT,
    "max_hold_hours": EXIT_MAX_HOLD_HOURS,
}
SL_ANCHOR_ATR = "DEMAND_ZONE_LOW_ATR_BUFFER"
STAGE_HISTORY = "HISTORY_ENTRY"
STAGE_CANDLE = "CANDLE_ENTRY"
STAGE_BULLISH_CANDLE = "BULLISH_CANDLE"
STAGE_EMA_TREND = "EMA_TREND"
STAGE_DEMAND_ZONE = "DEMAND_ZONE"
STAGE_VOLUME_24H = "VOLUME_24H"
STAGE_RISK = "RISK_SL"
STAGE_OK = "OK"
STAGE_DISABLED = "DISABLED"


class HtfBar(NamedTuple):
    open_time: int
    open: float
    high: float
    low: float
    close: float
    close_time: int


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _cfg_float(config: dict, key: str, default: float, low: float, high: float) -> float:
    value = _finite((config or {}).get(key, default))
    return max(low, min(high, default if value is None else value))


def _cfg_int(config: dict, key: str, default: int, low: int, high: int) -> int:
    return int(round(_cfg_float(config, key, float(default), float(low), float(high))))


def _cfg_bool(config: dict, key: str, default: bool) -> bool:
    raw = (config or {}).get(key, default)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on", "aktif"}
    return bool(raw)


def rule_enabled(config: dict) -> bool:
    return _cfg_bool(config, "ENTRY_RULE_ENABLED", True)


def signal_params(config: dict) -> dict:
    return {
        "ema_period": _cfg_int(config, "DEMAND_EMA_PERIOD", 50, EMA_PERIOD_MIN, EMA_PERIOD_MAX),
        "impulse_atr_mult": _cfg_float(config, "DEMAND_IMPULSE_ATR_MULT", 1.0, 0.5, 5.0),
        "sl_atr_buffer_mult": _cfg_float(config, "ENTRY_SL_ATR_BUFFER_MULT", 2.0, 0.0, 10.0),
        "zone_timeframes": [name for name, _ in ZONE_TIMEFRAMES],
    }


def momentum_params(config: dict) -> dict:
    return signal_params(config)


def volume_rise_params(config: dict) -> dict:
    return {
        "enabled": _cfg_bool(config, "GAINER_VOLUME_FILTER_ENABLED", True),
        "mult": _cfg_float(config, "GAINER_VOLUME_RISE_MULT", 1.2, 0.0, 20.0),
        "days": _cfg_int(config, "GAINER_VOLUME_AVG_DAYS", 3, 1, 14),
    }


def momentum_interval(config: dict) -> str:
    return str((config or {}).get("CONFIRM_INTERVAL", "15m") or "15m").strip().lower()


def _interval_minutes(interval: str) -> int:
    from strategy.indicators import INTERVAL_MINUTES
    value = INTERVAL_MINUTES.get(str(interval).strip().lower())
    if value is None:
        raise ValueError(f"Interval '{interval}' tidak dikenal; pilihan: " + ", ".join(sorted(INTERVAL_MINUTES, key=INTERVAL_MINUTES.get)))
    return int(value)


def bars_per_day(config: dict) -> int:
    return max(1, 1440 // _interval_minutes(momentum_interval(config)))


def volume_rise_bars(config: dict) -> int:
    return bars_per_day(config) * (volume_rise_params(config)["days"] + 1)


def _turnover(chunk: Sequence) -> Optional[float]:
    total = 0.0
    for bar in chunk:
        try:
            value = float(getattr(bar, "quote_volume", 0.0) or 0.0)
            if value <= 0.0:
                value = float(bar.close) * float(bar.volume)
        except (AttributeError, TypeError, ValueError):
            return None
        if not math.isfinite(value) or value < 0.0:
            return None
        total += value
    return total


def turnover_windows_available(bars: int, per_day: int) -> int:
    return 0 if per_day <= 0 else max(0, int(bars) // int(per_day) - 1)


def turnover_rise_ratio(bars: Sequence, per_day: int, days: int) -> Optional[float]:
    available = turnover_windows_available(len(bars), per_day)
    if per_day <= 0 or days <= 0 or available < 1:
        return None
    count = min(int(days), available)
    tail = list(bars[-per_day * (count + 1):])
    current = _turnover(tail[-per_day:])
    past = [_turnover(tail[i * per_day:(i + 1) * per_day]) for i in range(count)]
    if current is None or any(value is None for value in past):
        return None
    average = sum(past) / count
    return None if average <= 0.0 else current / average


def volume_rise_check(klines: Sequence, config: dict) -> dict:
    params = volume_rise_params(config)
    result = {"ok": True, "skipped": False, "ratio": None, "stage": STAGE_VOLUME_24H, "reason": ""}
    if not params["enabled"] or params["mult"] <= 0.0:
        result["reason"] = "syarat kenaikan volume 24 jam nonaktif"
        return result
    per_day = bars_per_day(config)
    days = min(params["days"], turnover_windows_available(len(klines), per_day))
    ratio = turnover_rise_ratio(klines, per_day, params["days"])
    if ratio is None:
        result["skipped"] = True
        result["reason"] = f"turnover 24 jam tidak bisa dinilai, candle {momentum_interval(config)} kurang dari {2 * per_day} bar"
        return result
    result.update(ratio=ratio, days=days)
    if ratio + 1e-12 < params["mult"]:
        result["ok"] = False
        result["reason"] = f"turnover 24 jam hanya {ratio:.2f} x rata-rata {days} hari, di bawah syarat {params['mult']:g} x"
    else:
        result["reason"] = f"turnover 24 jam {ratio:.2f} x rata-rata {days} hari"
    return result


def min_rr_ratio(config: dict) -> float:
    return _cfg_float(config, "ENTRY_MIN_RR_RATIO", 3.0, 0.1, 20.0)


def max_sl_pct(config: dict) -> float:
    return _cfg_float(config, "EXIT_ATR_MAX_SL_PCT", 5.0, 0.0, 50.0)


def atr_period(config: dict) -> int:
    return exit_atr.atr_period(config)


def trigger_interval_ok(interval: str) -> bool:
    return str(interval).strip().lower() in TRIGGER_INTERVALS


def required_bars_for(interval: str, ema_period: int, atr_bars: int) -> int:
    try:
        minutes = _interval_minutes(interval)
    except ValueError:
        return max(2, atr_bars + 1)
    largest = ZONE_TIMEFRAMES[-1][1]
    if minutes <= 0 or largest % minutes != 0:
        return max(2, atr_bars + 1)
    factor = largest // minutes
    return max(2, atr_bars + 1, (max(int(ema_period), int(atr_bars) + 2) + 2) * factor)


def required_bars(config: dict) -> int:
    params = signal_params(config)
    return required_bars_for(momentum_interval(config), params["ema_period"], atr_period(config))


def atr_window_bars(config: dict) -> int:
    return max(required_bars(config), _cfg_int(config, "CONFIRM_LOOKBACK_BARS", DEFAULT_LOOKBACK_BARS, 3, MAX_CLOSED_BARS))


def max_hold_hours(config: Optional[dict] = None) -> float:
    return EXIT_MAX_HOLD_HOURS if config is None else _cfg_float(config, "EXIT_MAX_HOLD_HOURS", EXIT_MAX_HOLD_HOURS, 0.0, MAX_HOLD_HOURS_LIMIT)


def exit_fixed_params(config: Optional[dict] = None) -> dict:
    result = dict(EXIT_FIXED)
    params = exit_atr.atr_params(config or {})
    result.update(breakeven_r_multiple=params["breakeven_trigger_r"], trailing_start_r=params["trailing_start_r"], trailing_atr_mult=params["trail_mult"], max_hold_hours=max_hold_hours(config))
    return result


def trailing_stop_price(highest_price: float, atr: float, mult: float) -> float:
    return exit_atr.trailing_stop_price(highest_price, atr, mult)


def breakeven_price(entry_price: float, sl_price: float = 0.0, r_multiple: float = 0.0, lock_pct: float = 0.0) -> float:
    return exit_atr.breakeven_price(entry_price, lock_pct)


def candles_valid(bars: Sequence) -> bool:
    for bar in bars or []:
        try:
            values = tuple(float(value) for value in (bar.open, bar.high, bar.low, bar.close))
        except (AttributeError, TypeError, ValueError):
            return False
        o, h, low, c = values
        if not all(math.isfinite(value) and value > 0.0 for value in values) or h < max(o, c, low) or low > min(o, c, h):
            return False
    return bool(bars)


def atr_value(klines: Sequence, period: int = 14) -> Optional[float]:
    if period <= 0 or len(klines) < period:
        return None
    values = []
    for index, candle in enumerate(klines):
        try:
            previous = float(klines[index - 1].close if index else candle.close)
            high, low, close = float(candle.high), float(candle.low), float(candle.close)
        except (AttributeError, TypeError, ValueError):
            return None
        if not all(math.isfinite(value) and value > 0.0 for value in (high, low, close)):
            return None
        values.append(max(high - low, abs(high - previous), abs(low - previous)))
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = (result * (period - 1) + value) / period
    return result if math.isfinite(result) and result > 0.0 else None


def ema_values(bars: Sequence, period: int) -> list[Optional[float]]:
    if period <= 0:
        return [None] * len(bars)
    closes = []
    for bar in bars:
        value = _finite(getattr(bar, "close", None))
        if value is None or value <= 0.0:
            return [None] * len(bars)
        closes.append(value)
    output: list[Optional[float]] = [None] * len(closes)
    if len(closes) < period:
        return output
    value = sum(closes[:period]) / period
    output[period - 1] = value
    multiplier = 2.0 / (period + 1.0)
    for index in range(period, len(closes)):
        value = (closes[index] - value) * multiplier + value
        output[index] = value
    return output


def ema_value(bars: Sequence, period: int) -> Optional[float]:
    values = ema_values(bars, period)
    return values[-1] if values else None


def is_bullish_candle(candle) -> bool:
    try:
        co, cc = float(candle.open), float(candle.close)
    except (AttributeError, TypeError, ValueError):
        return False
    return math.isfinite(co) and math.isfinite(cc) and co > 0.0 and cc > co


def resample_closed(bars: Sequence, base_minutes: int, target_minutes: int) -> list[HtfBar]:
    if base_minutes <= 0 or target_minutes <= base_minutes or target_minutes % base_minutes != 0:
        return []
    factor = target_minutes // base_minutes
    base_ms = base_minutes * 60_000
    span = target_minutes * 60_000
    output: list[HtfBar] = []
    bucket = None
    count = 0
    o = h = low = c = 0.0
    start = end = 0
    for bar in bars:
        open_time = int(bar.open_time)
        key = open_time // span
        if key != bucket:
            if bucket is not None and count == factor:
                output.append(HtfBar(start, o, h, low, c, end))
            bucket = key
            count = 0
            if open_time % span != 0:
                bucket = None
                continue
            o, h, low, c = float(bar.open), float(bar.high), float(bar.low), float(bar.close)
            start, end, count = open_time, int(bar.close_time), 1
            continue
        if bucket is None:
            continue
        if open_time != start + count * base_ms:
            bucket = None
            count = 0
            continue
        h = max(h, float(bar.high))
        low = min(low, float(bar.low))
        c = float(bar.close)
        end = int(bar.close_time)
        count += 1
    if bucket is not None and count == factor:
        output.append(HtfBar(start, o, h, low, c, end))
    return output


def demand_zones(bars: Sequence, period: int, impulse_mult: float, timeframe: str = "") -> list[dict]:
    zones: list[dict] = []
    if period <= 0 or len(bars) < period + 2:
        return zones
    ranges = []
    for index, bar in enumerate(bars):
        previous = float(bars[index - 1].close) if index else float(bar.close)
        ranges.append(max(float(bar.high) - float(bar.low), abs(float(bar.high) - previous), abs(float(bar.low) - previous)))
    atr = sum(ranges[:period]) / period
    previous_impulse = False
    for index in range(period, len(bars)):
        bar = bars[index]
        base = bars[index - 1]
        body = float(bar.close) - float(bar.open)
        impulse = atr > 0.0 and body >= impulse_mult * atr and float(bar.close) > float(base.high)
        if impulse and not previous_impulse:
            zone_low = min(float(base.low), float(bar.low))
            zone_high = float(base.high)
            if 0.0 < zone_low < zone_high:
                zones.append({"timeframe": timeframe, "low": zone_low, "high": zone_high, "created_time": int(bar.close_time), "base_time": int(base.open_time), "impulse_atr": body / atr})
        previous_impulse = impulse
        atr = (atr * (period - 1) + ranges[index]) / period
    return zones


def active_demand_zones(zones: Sequence[dict], rows: Sequence) -> list[dict]:
    if not zones or len(rows) < 2:
        return []
    close_times = [int(bar.close_time) for bar in rows[:-1]]
    closes = [float(bar.close) for bar in rows[:-1]]
    signal_open = int(rows[-1].open_time)
    active = []
    for zone in zones:
        if int(zone["created_time"]) >= signal_open:
            continue
        start = bisect_right(close_times, int(zone["created_time"]))
        if start < len(closes) and min(closes[start:]) < float(zone["low"]):
            continue
        active.append(zone)
    return active


def touched_demand_zone(zones: Sequence[dict], candle) -> Optional[dict]:
    low, close = float(candle.low), float(candle.close)
    hits = [zone for zone in zones if low <= float(zone["high"]) and close > float(zone["low"])]
    if not hits:
        return None
    return max(hits, key=lambda zone: (float(zone["low"]), float(zone["high"])))


def exit_levels(entry_price: float, anchor_low: float, config: dict, *, atr: Optional[float] = None, fallback_allowed: bool = False) -> dict:
    entry = _finite(entry_price) or 0.0
    low = _finite(anchor_low) or 0.0
    distance = _finite(atr)
    rr = min_rr_ratio(config)
    buffer_mult = signal_params(config)["sl_atr_buffer_mult"]
    result = {"ok": False, "stage": STAGE_RISK, "reason": "", "sl_price": None, "tp_price": None, "sl_pct": None, "tp_pct": None, "rr_ratio": None, "min_rr_ratio": rr, "max_sl_pct": max_sl_pct(config), "anchor_low": low or None, "atr": distance if distance and distance > 0 else None, "sl_atr_buffer_mult": buffer_mult}
    if entry <= 0.0 or low <= 0.0:
        result["reason"] = "harga entry atau batas bawah zona demand tidak valid"
        return result
    if distance is None or distance <= 0.0:
        if not fallback_allowed:
            result["reason"] = "ATR tidak tersedia untuk menghitung buffer stop"
            return result
        distance = 0.0
    sl = low - distance * buffer_mult
    if sl <= 0.0 or sl >= entry:
        result["reason"] = f"SL {sl:.6g} dari batas bawah zona demand tidak berada di bawah entry {entry:.6g}"
        return result
    sl_pct = (entry - sl) / entry * 100.0
    limit = max_sl_pct(config)
    if limit > 0.0 and sl_pct > limit + 1e-9:
        result.update(sl_price=sl, sl_pct=sl_pct)
        result["reason"] = f"jarak SL zona demand {sl_pct:.2f}% melampaui batas {limit:.2f}%"
        return result
    tp = entry + (entry - sl) * rr
    tp_pct = (tp - entry) / entry * 100.0
    result.update(ok=True, stage=STAGE_OK, sl_price=sl, tp_price=tp, sl_pct=sl_pct, tp_pct=tp_pct, rr_ratio=rr, atr=distance, reason=f"SL {sl:.6g} di bawah batas zona demand {low:.6g} dengan buffer {buffer_mult:g} x ATR, TP {tp:.6g}, R:R 1:{rr:.2f}")
    return result


def _empty_result(interval: str, bars: int, required: int) -> dict:
    return {"ok": False, "stage": STAGE_HISTORY, "reason": "", "interval": interval, "bars": bars, "bars_required": required, "bull_count": 0, "bull_required": 1, "signal_close": None, "signal_close_time": None, "entry_low": None, "atr": None, "sl_price": None, "tp_price": None, "sl_pct": None, "tp_pct": None, "rr_ratio": None, "ema_fast": None, "ema_slow": None, "ema_m15": None, "ema_h1": None, "ema_h4": None, "zone_low": None, "zone_high": None, "zone_timeframe": None, "zones_active": 0, "candle_strength": None, "engulfing_strength": None, "price_ema_atr": None, "ema_trend_atr": None, "checks": {"history": False, "candles": False, "bullish_candle": False, "ema_m15": False, "ema_h1": False, "ema_h4": False, "demand_zone": False, "risk": False}, "values": {}}


def evaluate_entry_rule(klines: Sequence, config: dict, signal_close_time_ms: Optional[int] = None) -> dict:
    interval = momentum_interval(config)
    params = signal_params(config)
    need = required_bars(config)
    rows = [bar for bar in (klines or []) if signal_close_time_ms is None or int(getattr(bar, "close_time", 0)) <= int(signal_close_time_ms)]
    result = _empty_result(interval, len(rows), need)
    enabled = rule_enabled(config)
    if enabled and not trigger_interval_ok(interval):
        result["reason"] = f"interval sinyal {interval} tidak didukung, zona demand H1 dan H4 memerlukan interval {' atau '.join(TRIGGER_INTERVALS)}"
        return result
    minimum = need if enabled else max(2, atr_period(config) + 1)
    if len(rows) < minimum:
        result["reason"] = f"data candle {interval} kurang: {len(rows)} dari minimum {minimum}"
        return result
    result["checks"]["history"] = True
    window = rows[-atr_window_bars(config):]
    if not candles_valid(window):
        result.update(stage=STAGE_CANDLE, reason=f"data OHLC candle {interval} tidak valid")
        return result
    result["checks"]["candles"] = True
    current = window[-1]
    entry = float(current.close)
    atr = atr_value(window, atr_period(config))
    candle_low = float(current.low)
    result.update(signal_close=entry, signal_close_time=int(current.close_time), entry_low=candle_low, atr=atr)
    if not enabled:
        levels = exit_levels(entry, candle_low, config, atr=atr, fallback_allowed=True)
        result.update(ok=True, stage=STAGE_DISABLED, reason="filter zona demand dan EMA nonaktif", sl_price=levels["sl_price"], tp_price=levels["tp_price"], sl_pct=levels["sl_pct"], tp_pct=levels["tp_pct"], rr_ratio=levels["rr_ratio"])
        for key in result["checks"]:
            result["checks"][key] = True
        return result
    if not is_bullish_candle(current):
        result.update(stage=STAGE_BULLISH_CANDLE, reason=f"candle terakhir {interval} bukan candle bullish (close {entry:.6g} <= open {float(current.open):.6g})")
        return result
    current_body = abs(float(current.close) - float(current.open))
    candle_range = max(1e-12, float(current.high) - float(current.low))
    candle_strength = (current_body / atr) if (atr is not None and atr > 0.0) else (current_body / candle_range)
    result.update(candle_strength=candle_strength, engulfing_strength=candle_strength, bull_count=1)
    result["checks"]["bullish_candle"] = True
    period = params["ema_period"]
    base_minutes = _interval_minutes(interval)
    ema_base = ema_value(window, period)
    result["ema_m15"] = ema_base
    if ema_base is None:
        result.update(stage=STAGE_HISTORY, reason=f"EMA {period} {interval} belum tersedia dari riwayat candle")
        return result
    if entry <= ema_base:
        result.update(stage=STAGE_EMA_TREND, reason=f"close {entry:.6g} belum di atas EMA {period} {interval} ({ema_base:.6g})")
        return result
    result["checks"]["ema_m15"] = True
    frames = {name: resample_closed(window, base_minutes, minutes) for name, minutes in ZONE_TIMEFRAMES}
    emas = {}
    for name, _ in ZONE_TIMEFRAMES:
        value = ema_value(frames[name], period)
        emas[name] = value
        result["ema_h1" if name == "1h" else "ema_h4"] = value
        if value is None:
            result.update(stage=STAGE_HISTORY, reason=f"EMA {period} {name} belum tersedia, candle {name} tertutup baru {len(frames[name])}")
            return result
        if entry <= value:
            result.update(stage=STAGE_EMA_TREND, reason=f"close {entry:.6g} belum di atas EMA {period} {name} ({value:.6g})")
            return result
        result["checks"]["ema_h1" if name == "1h" else "ema_h4"] = True
    ema_h1, ema_h4 = emas["1h"], emas["4h"]
    price_ema_atr = (entry - ema_h4) / atr if atr and atr > 0.0 else None
    ema_trend_atr = (ema_h1 - ema_h4) / atr if atr and atr > 0.0 else None
    result.update(ema_fast=ema_h1, ema_slow=ema_h4, price_ema_atr=price_ema_atr, ema_trend_atr=ema_trend_atr)
    zones: list[dict] = []
    for name, _ in ZONE_TIMEFRAMES:
        zones.extend(demand_zones(frames[name], atr_period(config), params["impulse_atr_mult"], name))
    active = active_demand_zones(zones, window)
    result["zones_active"] = len(active)
    zone = touched_demand_zone(active, current)
    if zone is None:
        if not active:
            reason = "belum ada zona demand H1 atau H4 yang masih utuh di riwayat candle"
        else:
            nearest = max(active, key=lambda item: float(item["high"]))
            reason = f"candle {interval} belum menyentuh zona demand, zona terdekat {nearest['timeframe']} {float(nearest['low']):.6g} sampai {float(nearest['high']):.6g}, low candle {candle_low:.6g}"
        result.update(stage=STAGE_DEMAND_ZONE, reason=reason)
        return result
    zone_low, zone_high = float(zone["low"]), float(zone["high"])
    anchor = min(zone_low, candle_low)
    result.update(zone_low=zone_low, zone_high=zone_high, zone_timeframe=zone["timeframe"], entry_low=anchor)
    result["checks"]["demand_zone"] = True
    levels = exit_levels(entry, anchor, config, atr=atr)
    result.update(sl_price=levels["sl_price"], tp_price=levels["tp_price"], sl_pct=levels["sl_pct"], tp_pct=levels["tp_pct"], rr_ratio=levels["rr_ratio"])
    if not levels["ok"]:
        result.update(stage=STAGE_RISK, reason=levels["reason"])
        return result
    result["checks"]["risk"] = True
    result.update(ok=True, stage=STAGE_OK, reason=f"candle bullish {interval} di zona demand {zone['timeframe']} {zone_low:.6g} sampai {zone_high:.6g}, close di atas EMA {period} pada {interval}, 1h, dan 4h; {levels['reason']}")
    result["values"] = {"entry_low": anchor, "sl_price": levels["sl_price"], "tp_price": levels["tp_price"], "rr_ratio": levels["rr_ratio"], "atr": atr, "ema_m15": ema_base, "ema_h1": ema_h1, "ema_h4": ema_h4, "ema_fast": ema_h1, "ema_slow": ema_h4, "zone_low": zone_low, "zone_high": zone_high, "zone_timeframe": zone["timeframe"], "candle_strength": candle_strength, "engulfing_strength": candle_strength, "price_ema_atr": price_ema_atr, "ema_trend_atr": ema_trend_atr}
    return result


def demand_zone_snapshot(klines: Sequence, config: dict, signal_close_time_ms: Optional[int] = None) -> dict:
    interval = momentum_interval(config)
    params = signal_params(config)
    rows = [bar for bar in (klines or []) if signal_close_time_ms is None or int(getattr(bar, "close_time", 0)) <= int(signal_close_time_ms)]
    snapshot = {"ok": False, "reason": "", "interval": interval, "ema_period": params["ema_period"], "ema_base": [], "ema_h1": None, "ema_h4": None, "zones": [], "bars": len(rows)}
    if not trigger_interval_ok(interval):
        snapshot["reason"] = f"interval sinyal {interval} tidak didukung"
        return snapshot
    if len(rows) < 2:
        snapshot["reason"] = f"data candle {interval} kurang"
        return snapshot
    window = rows[-atr_window_bars(config):]
    if not candles_valid(window):
        snapshot["reason"] = f"data OHLC candle {interval} tidak valid"
        return snapshot
    base_minutes = _interval_minutes(interval)
    period = params["ema_period"]
    current = window[-1]
    snapshot["ema_base"] = ema_values(window, period)
    zones: list[dict] = []
    for name, minutes in ZONE_TIMEFRAMES:
        frame = resample_closed(window, base_minutes, minutes)
        snapshot["ema_h1" if name == "1h" else "ema_h4"] = ema_value(frame, period)
        zones.extend(demand_zones(frame, atr_period(config), params["impulse_atr_mult"], name))
    active = active_demand_zones(zones, window)
    chosen = touched_demand_zone(active, current)
    low, close = float(current.low), float(current.close)
    for zone in sorted(active, key=lambda item: float(item["high"]), reverse=True):
        zone_low, zone_high = float(zone["low"]), float(zone["high"])
        snapshot["zones"].append({
            "timeframe": zone["timeframe"],
            "low": zone_low,
            "high": zone_high,
            "created_time": int(zone["created_time"]),
            "impulse_atr": float(zone["impulse_atr"]),
            "touched": bool(low <= zone_high and close > zone_low),
            "selected": zone is chosen,
            "distance_pct": (close - zone_high) / close * 100.0 if close > 0.0 else None,
        })
    snapshot["ok"] = True
    return snapshot


def take_profit_due(entry_price, target_price, current_price) -> bool:
    entry, target, current = _finite(entry_price), _finite(target_price), _finite(current_price)
    return bool(EXIT_TAKE_PROFIT_ENABLED and entry is not None and target is not None and current is not None and 0.0 < entry < target <= current)
