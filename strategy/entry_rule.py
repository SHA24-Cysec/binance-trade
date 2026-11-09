from __future__ import annotations

import math
from typing import Optional, Sequence

from strategy import exit_atr

ENTRY_RULE_DEFAULTS = {
    "ENTRY_RULE_ENABLED": True,
    "CONFIRM_INTERVAL": "30m",
    "EMA_FAST_PERIOD": 20,
    "EMA_SLOW_PERIOD": 50,
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

SIGNAL_PARAM_KEYS = ("EMA_FAST_PERIOD", "EMA_SLOW_PERIOD", "ENTRY_SL_ATR_BUFFER_MULT")
MOMENTUM_PARAM_KEYS = SIGNAL_PARAM_KEYS
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
SL_ANCHOR_ATR = "BULLISH_CANDLE_LOW_ATR_BUFFER"
STAGE_HISTORY = "HISTORY_ENTRY"
STAGE_CANDLE = "CANDLE_ENTRY"
STAGE_BULLISH_CANDLE = "BULLISH_CANDLE"
STAGE_ENGULFING = STAGE_BULLISH_CANDLE
STAGE_EMA_PRICE = "EMA_PRICE"
STAGE_EMA_TREND = "EMA_TREND"
STAGE_VOLUME_24H = "VOLUME_24H"
STAGE_RISK = "RISK_SL"
STAGE_OK = "OK"
STAGE_DISABLED = "DISABLED"
STAGE_SEQUENCE = STAGE_BULLISH_CANDLE
STAGE_BODY = STAGE_BULLISH_CANDLE
STAGE_BREAKOUT = STAGE_BULLISH_CANDLE
STAGE_VOLUME = STAGE_BULLISH_CANDLE


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
        "ema_fast": _cfg_int(config, "EMA_FAST_PERIOD", 20, 2, 500),
        "ema_slow": _cfg_int(config, "EMA_SLOW_PERIOD", 50, 3, 1000),
        "sl_atr_buffer_mult": _cfg_float(config, "ENTRY_SL_ATR_BUFFER_MULT", 0.1, 0.0, 10.0),
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


def required_bars(config: dict) -> int:
    params = signal_params(config)
    return max(2, params["ema_slow"] + 1, atr_period(config) + 1)


def atr_window_bars(config: dict) -> int:
    return max(required_bars(config), _cfg_int(config, "CONFIRM_LOOKBACK_BARS", 192, 3, 1000))


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


def is_bullish_engulfing(previous, current, strict: bool = True) -> bool:
    try:
        po, pc = float(previous.open), float(previous.close)
        co, cc = float(current.open), float(current.close)
    except (AttributeError, TypeError, ValueError):
        return False
    if not all(math.isfinite(value) for value in (po, pc, co, cc)) or pc >= po or cc <= co:
        return False
    covers = co <= pc and cc >= po
    exceeds = co < pc or cc > po
    return covers and (exceeds if strict else True)


def exit_levels(entry_price: float, anchor_low: float, config: dict, *, atr: Optional[float] = None, fallback_allowed: bool = False) -> dict:
    entry = _finite(entry_price) or 0.0
    low = _finite(anchor_low) or 0.0
    distance = _finite(atr)
    rr = min_rr_ratio(config)
    buffer_mult = signal_params(config)["sl_atr_buffer_mult"]
    result = {"ok": False, "stage": STAGE_RISK, "reason": "", "sl_price": None, "tp_price": None, "sl_pct": None, "tp_pct": None, "rr_ratio": None, "min_rr_ratio": rr, "max_sl_pct": max_sl_pct(config), "anchor_low": low or None, "atr": distance if distance and distance > 0 else None, "sl_atr_buffer_mult": buffer_mult}
    if entry <= 0.0 or low <= 0.0:
        result["reason"] = "harga entry atau low candle sinyal tidak valid"
        return result
    if distance is None or distance <= 0.0:
        if not fallback_allowed:
            result["reason"] = "ATR tidak tersedia untuk menghitung buffer stop"
            return result
        distance = 0.0
    sl = low - distance * buffer_mult
    if sl <= 0.0 or sl >= entry:
        result["reason"] = f"SL {sl:.6g} dari low candle tidak berada di bawah entry {entry:.6g}"
        return result
    sl_pct = (entry - sl) / entry * 100.0
    limit = max_sl_pct(config)
    if limit > 0.0 and sl_pct > limit + 1e-9:
        result.update(sl_price=sl, sl_pct=sl_pct)
        result["reason"] = f"jarak SL candle {sl_pct:.2f}% melampaui batas {limit:.2f}%"
        return result
    tp = entry + (entry - sl) * rr
    tp_pct = (tp - entry) / entry * 100.0
    result.update(ok=True, stage=STAGE_OK, sl_price=sl, tp_price=tp, sl_pct=sl_pct, tp_pct=tp_pct, rr_ratio=rr, atr=distance, reason=f"SL {sl:.6g} di bawah low candle {low:.6g} dengan buffer {buffer_mult:g} x ATR, TP {tp:.6g}, R:R 1:{rr:.2f}")
    return result


def _empty_result(interval: str, bars: int, required: int) -> dict:
    return {"ok": False, "stage": STAGE_HISTORY, "reason": "", "interval": interval, "bars": bars, "required": required, "window": required, "bull_count": 0, "bull_required": 1, "signal_close": None, "signal_close_time": None, "entry_low": None, "atr": None, "sl_price": None, "tp_price": None, "sl_pct": None, "tp_pct": None, "rr_ratio": None, "ema_fast": None, "ema_slow": None, "candle_strength": None, "engulfing_strength": None, "price_ema_atr": None, "ema_trend_atr": None, "checks": {"history": False, "candles": False, "bullish_candle": False, "bullish_engulfing": False, "price_above_ema": False, "ema_trend": False, "risk": False}, "values": {}}


def evaluate_entry_rule(klines: Sequence, config: dict, signal_close_time_ms: Optional[int] = None) -> dict:
    interval = momentum_interval(config)
    params = signal_params(config)
    need = required_bars(config)
    rows = [bar for bar in (klines or []) if signal_close_time_ms is None or int(getattr(bar, "close_time", 0)) <= int(signal_close_time_ms)]
    result = _empty_result(interval, len(rows), need)
    if len(rows) < need:
        result["reason"] = f"data candle {interval} kurang: {len(rows)} dari minimum {need}"
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
    if not rule_enabled(config):
        levels = exit_levels(entry, candle_low, config, atr=atr, fallback_allowed=True)
        result.update(ok=True, stage=STAGE_DISABLED, reason="filter candle bullish dan EMA nonaktif", sl_price=levels["sl_price"], tp_price=levels["tp_price"], sl_pct=levels["sl_pct"], tp_pct=levels["tp_pct"], rr_ratio=levels["rr_ratio"])
        for key in result["checks"]:
            result["checks"][key] = True
        return result
    if not is_bullish_candle(current):
        result.update(stage=STAGE_BULLISH_CANDLE, reason=f"candle terakhir {interval} bukan candle bullish (close {entry:.6g} <= open {float(current.open):.6g})")
        return result
    current_body = abs(float(current.close) - float(current.open))
    candle_range = max(1e-12, float(current.high) - float(current.low))
    candle_strength = (current_body / atr) if (atr is not None and atr > 0.0) else (current_body / candle_range)
    result["candle_strength"] = candle_strength
    result["engulfing_strength"] = candle_strength
    result["bull_count"] = 1
    result["checks"]["bullish_candle"] = True
    result["checks"]["bullish_engulfing"] = True
    fast = ema_value(window, params["ema_fast"])
    slow = ema_value(window, params["ema_slow"])
    price_ema_atr = (entry - slow) / atr if slow is not None and atr and atr > 0.0 else None
    ema_trend_atr = (fast - slow) / atr if fast is not None and slow is not None and atr and atr > 0.0 else None
    result.update(ema_fast=fast, ema_slow=slow, price_ema_atr=price_ema_atr, ema_trend_atr=ema_trend_atr)
    if fast is None or slow is None:
        result.update(stage=STAGE_HISTORY, reason="EMA belum tersedia dari riwayat candle")
        return result
    if entry <= fast or entry <= slow:
        result.update(stage=STAGE_EMA_PRICE, reason=f"close {entry:.6g} belum di atas EMA {params['ema_fast']} ({fast:.6g}) dan EMA {params['ema_slow']} ({slow:.6g})")
        return result
    result["checks"]["price_above_ema"] = True
    if fast <= slow:
        result.update(stage=STAGE_EMA_TREND, reason=f"EMA {params['ema_fast']} {fast:.6g} belum di atas EMA {params['ema_slow']} {slow:.6g}")
        return result
    result["checks"]["ema_trend"] = True
    levels = exit_levels(entry, candle_low, config, atr=atr)
    result.update(sl_price=levels["sl_price"], tp_price=levels["tp_price"], sl_pct=levels["sl_pct"], tp_pct=levels["tp_pct"], rr_ratio=levels["rr_ratio"])
    if not levels["ok"]:
        result.update(stage=STAGE_RISK, reason=levels["reason"])
        return result
    result["checks"]["risk"] = True
    result.update(ok=True, stage=STAGE_OK, reason=f"candle bullish {interval} valid, close di atas EMA {params['ema_fast']}/{params['ema_slow']}, EMA cepat di atas EMA lambat; {levels['reason']}")
    result["values"] = {"entry_low": candle_low, "sl_price": levels["sl_price"], "tp_price": levels["tp_price"], "rr_ratio": levels["rr_ratio"], "atr": atr, "ema_fast": fast, "ema_slow": slow, "candle_strength": candle_strength, "engulfing_strength": candle_strength, "price_ema_atr": price_ema_atr, "ema_trend_atr": ema_trend_atr}
    return result


def take_profit_due(entry_price, target_price, current_price) -> bool:
    entry, target, current = _finite(entry_price), _finite(target_price), _finite(current_price)
    return bool(EXIT_TAKE_PROFIT_ENABLED and entry is not None and target is not None and current is not None and 0.0 < entry < target <= current)
