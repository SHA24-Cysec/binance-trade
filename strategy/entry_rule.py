from __future__ import annotations

import math
from typing import Optional, Sequence

ENTRY_RULE_DEFAULTS = {
    "ENTRY_RULE_ENABLED": True,
    "ENTRY_BULL_CANDLES": 3,
    "ENTRY_SL_ANCHOR": "SIGNAL",
    "ENTRY_SL_BUFFER_PCT": 0.30,
    "ENTRY_MIN_RR_RATIO": 2.0,
    "ENTRY_MAX_SL_PCT": 3.0,
    "EXIT_TRAILING_PCT": 2.0,
}

EXIT_PARAM_KEYS = ("EXIT_TRAILING_PCT",)
EXIT_TAKE_PROFIT_ENABLED = False
EXIT_BREAKEVEN_ENABLED = True
EXIT_BREAKEVEN_R_MULTIPLE = 1.0
EXIT_BREAKEVEN_LOCK_PCT = 0.0
EXIT_TRAILING_START_R = 1.0
EXIT_TRAILING_MIN_PCT = 0.1
EXIT_TRAILING_MAX_PCT = 20.0
EXIT_MAX_HOLD_HOURS = 24.0
EXIT_FIXED = {
    "take_profit_enabled": EXIT_TAKE_PROFIT_ENABLED,
    "breakeven_enabled": EXIT_BREAKEVEN_ENABLED,
    "breakeven_r_multiple": EXIT_BREAKEVEN_R_MULTIPLE,
    "breakeven_lock_pct": EXIT_BREAKEVEN_LOCK_PCT,
    "trailing_start_r": EXIT_TRAILING_START_R,
    "max_hold_hours": EXIT_MAX_HOLD_HOURS,
}

SL_ANCHORS = ("SIGNAL", "SEQUENCE")
STAGE_HISTORY = "HISTORY_ENTRY"
STAGE_CANDLE = "CANDLE_ENTRY"
STAGE_SEQUENCE = "BULL_SEQUENCE"
STAGE_RISK = "RISK_SL"
STAGE_OK = "OK"
STAGE_DISABLED = "DISABLED"


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _cfg_float(config: dict, key: str, default: float) -> float:
    value = _finite(config.get(key, default))
    return default if value is None else value


def _cfg_int(config: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    value = _cfg_float(config, key, float(default))
    return max(minimum, min(maximum, int(value)))


def _cfg_bool(config: dict, key: str, default: bool) -> bool:
    raw = config.get(key, default)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on", "aktif"}
    return bool(raw)


def rule_enabled(config: dict) -> bool:
    return _cfg_bool(config, "ENTRY_RULE_ENABLED", True)


def bull_candles(config: dict) -> int:
    return _cfg_int(config, "ENTRY_BULL_CANDLES", 3, 2, 10)


def sl_anchor(config: dict) -> str:
    raw = str(config.get("ENTRY_SL_ANCHOR", "SIGNAL") or "SIGNAL").strip().upper()
    return raw if raw in SL_ANCHORS else "SIGNAL"


def sl_buffer_pct(config: dict) -> float:
    return max(0.0, min(5.0, _cfg_float(config, "ENTRY_SL_BUFFER_PCT", 0.30)))


def min_rr_ratio(config: dict) -> float:
    return max(0.1, min(20.0, _cfg_float(config, "ENTRY_MIN_RR_RATIO", 2.0)))


def max_sl_pct(config: dict) -> float:
    return max(0.0, min(50.0, _cfg_float(config, "ENTRY_MAX_SL_PCT", 3.0)))


def required_bars(config: dict) -> int:
    return max(15, bull_candles(config) + 2)


def breakeven_params(config: dict) -> dict:
    if not rule_enabled(config):
        return {"enabled": False, "r_multiple": 0.0, "lock_pct": 0.0}
    return {
        "enabled": EXIT_BREAKEVEN_ENABLED,
        "r_multiple": EXIT_BREAKEVEN_R_MULTIPLE,
        "lock_pct": EXIT_BREAKEVEN_LOCK_PCT,
    }


def max_hold_hours() -> float:
    return EXIT_MAX_HOLD_HOURS


def trailing_params(config: dict) -> dict:
    if not rule_enabled(config):
        return {"enabled": False, "start_r": 0.0, "pct": 0.0}
    return {
        "enabled": True,
        "start_r": EXIT_TRAILING_START_R,
        "pct": max(
            EXIT_TRAILING_MIN_PCT,
            min(
                EXIT_TRAILING_MAX_PCT,
                _cfg_float(config, "EXIT_TRAILING_PCT", 2.0),
            ),
        ),
    }


def trailing_start_price(
    entry_price: float,
    stop_price: float,
    start_r: float,
) -> float:
    entry = float(entry_price or 0.0)
    stop = float(stop_price or 0.0)
    kelipatan = float(start_r or 0.0)
    if entry <= 0.0 or stop <= 0.0 or stop >= entry or kelipatan <= 0.0:
        return 0.0
    return entry + kelipatan * (entry - stop)


def trailing_stop_price(highest_price: float, pct: float) -> float:
    puncak = float(highest_price or 0.0)
    jarak = float(pct or 0.0)
    if puncak <= 0.0 or jarak <= 0.0:
        return 0.0
    return puncak * (1.0 - jarak / 100.0)


def breakeven_price(
    entry_price: float,
    sl_price: float,
    r_multiple: float,
    lock_pct: float = 0.0,
) -> float:
    entry = float(entry_price or 0.0)
    stop = float(sl_price or 0.0)
    kelipatan = float(r_multiple or 0.0)
    if entry <= 0.0 or stop <= 0.0 or stop >= entry or kelipatan <= 0.0:
        return 0.0
    return entry * (1.0 + max(0.0, float(lock_pct or 0.0)) / 100.0)


def breakeven_trigger(
    entry_price: float,
    sl_price: float,
    r_multiple: float,
) -> float:
    entry = float(entry_price or 0.0)
    stop = float(sl_price or 0.0)
    kelipatan = float(r_multiple or 0.0)
    if entry <= 0.0 or stop <= 0.0 or stop >= entry or kelipatan <= 0.0:
        return 0.0
    return entry + kelipatan * (entry - stop)


def candles_valid(bars: Sequence) -> bool:
    if not bars:
        return False
    for bar in bars:
        try:
            o = float(bar.open)
            h = float(bar.high)
            low = float(bar.low)
            c = float(bar.close)
        except (AttributeError, TypeError, ValueError):
            return False
        if not all(math.isfinite(x) and x > 0.0 for x in (o, h, low, c)):
            return False
        if h < max(o, c, low) or low > min(o, c, h):
            return False
    return True


def is_bullish(bar, previous) -> bool:
    try:
        open_ = float(bar.open)
        close = float(bar.close)
    except (AttributeError, TypeError, ValueError):
        return False
    if not (math.isfinite(open_) and math.isfinite(close)):
        return False
    if close <= open_:
        return False
    if previous is None:
        return True
    try:
        prev_close = float(previous.close)
    except (AttributeError, TypeError, ValueError):
        return False
    if not math.isfinite(prev_close):
        return False
    return close > prev_close


def count_bull_run(bars: Sequence) -> int:
    total = 0
    for index in range(len(bars) - 1, -1, -1):
        previous = bars[index - 1] if index > 0 else None
        if not is_bullish(bars[index], previous):
            break
        total += 1
    return total


def exit_levels(
    entry_price: float,
    anchor_low: float,
    config: dict,
    *,
    fallback_allowed: bool = False,
) -> dict:
    entry = _finite(entry_price) or 0.0
    low = _finite(anchor_low) or 0.0
    buffer_pct = sl_buffer_pct(config)
    rr_target = min_rr_ratio(config)
    cap_pct = max_sl_pct(config)

    dasar = {
        "ok": False,
        "stage": STAGE_RISK,
        "reason": "",
        "sl_price": None,
        "tp_price": None,
        "sl_pct": None,
        "tp_pct": None,
        "rr_ratio": None,
        "sl_buffer_pct": buffer_pct,
        "min_rr_ratio": rr_target,
        "max_sl_pct": cap_pct,
        "anchor_low": low if low > 0.0 else None,
    }

    if entry <= 0.0:
        if not fallback_allowed:
            dasar["reason"] = "harga entry tidak valid untuk menghitung SL"
            return dasar
        entry = 100.0

    if low <= 0.0 or low >= entry:
        if not fallback_allowed:
            dasar["reason"] = (
                f"low acuan SL {low if low > 0.0 else 'n/a'} tidak berada di bawah "
                f"harga entry {entry:.6g}"
            )
            return dasar
        sl_price = entry * (1.0 - max(0.5, buffer_pct * 10.0) / 100.0)
    else:
        sl_price = low * max(0.0001, 1.0 - buffer_pct / 100.0)

    if not (0.0 < sl_price < entry):
        dasar["reason"] = (
            f"level SL {sl_price:.6g} tidak valid terhadap harga entry {entry:.6g}"
        )
        return dasar

    sl_pct = (entry - sl_price) / entry * 100.0
    if cap_pct > 0.0 and sl_pct > cap_pct + 1e-9:
        dasar["sl_price"] = sl_price
        dasar["sl_pct"] = sl_pct
        dasar["reason"] = (
            f"jarak SL {sl_pct:.2f}% melebihi batas {cap_pct:.2f}% "
            f"(SL {sl_price:.6g}, entry {entry:.6g})"
        )
        return dasar

    tp_price = entry + (entry - sl_price) * rr_target
    tp_pct = (tp_price - entry) / entry * 100.0
    hasil = {
        "ok": True,
        "stage": STAGE_OK,
        "reason": (
            f"SL {sl_price:.6g} (-{sl_pct:.2f}%) di bawah {low:.6g} buffer "
            f"{buffer_pct:g}%, TP {tp_price:.6g} (+{tp_pct:.2f}%), "
            f"R:R 1:{rr_target:.2f}"
        ),
        "sl_price": sl_price,
        "tp_price": tp_price,
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "rr_ratio": rr_target,
        "sl_buffer_pct": buffer_pct,
        "min_rr_ratio": rr_target,
        "max_sl_pct": cap_pct,
        "anchor_low": low,
    }
    return hasil


def _empty_result(interval: str, bars: int, required: int, bull: int) -> dict:
    return {
        "ok": False,
        "stage": STAGE_HISTORY,
        "reason": "",
        "interval": interval,
        "bars": bars,
        "required": required,
        "window": required,
        "bull_count": 0,
        "bull_required": bull,
        "signal_close": None,
        "signal_close_time": None,
        "entry_low": None,
        "atr": None,
        "sl_price": None,
        "tp_price": None,
        "sl_pct": None,
        "tp_pct": None,
        "rr_ratio": None,
        "checks": {
            "history": False,
            "candles": False,
            "bull_sequence": False,
            "risk": False,
        },
        "values": {},
    }


def evaluate_entry_rule(
    klines: Sequence,
    config: dict,
    signal_close_time_ms: Optional[int] = None,
) -> dict:
    interval = str(config.get("CONFIRM_INTERVAL", "5m") or "5m").strip().lower()
    bull = bull_candles(config)
    need = required_bars(config)
    rows = [
        k
        for k in (klines or [])
        if signal_close_time_ms is None
        or int(getattr(k, "close_time", 0)) <= int(signal_close_time_ms)
    ]
    window = rows[-need:] if need > 0 else list(rows)
    result = _empty_result(interval, len(window), need, bull)

    if not rule_enabled(config):
        result["ok"] = True
        result["stage"] = STAGE_DISABLED
        result["reason"] = "aturan entry nonaktif di konfigurasi"
        result["checks"]["history"] = True
        result["checks"]["candles"] = True
        result["checks"]["bull_sequence"] = True
        result["checks"]["risk"] = True
        if window:
            last = window[-1]
            entry = float(last.close)
            anchor_low = float(last.low)
            info = exit_levels(entry, anchor_low, config, fallback_allowed=True)
            result["signal_close"] = entry
            result["signal_close_time"] = int(last.close_time)
            result["entry_low"] = anchor_low
            result["atr"] = atr_value(window, 14)
            result["sl_price"] = info["sl_price"]
            result["tp_price"] = info["tp_price"]
            result["sl_pct"] = info["sl_pct"]
            result["tp_pct"] = info["tp_pct"]
            result["rr_ratio"] = info["rr_ratio"]
        return result

    if len(window) < need:
        result["reason"] = (
            f"data candle {interval} kurang: {len(window)} dari minimum {need}"
        )
        return result
    result["checks"]["history"] = True

    if not candles_valid(window):
        result["stage"] = STAGE_CANDLE
        result["reason"] = f"data OHLC candle {interval} tidak valid"
        return result
    result["checks"]["candles"] = True

    urut = window[-(bull + 1) :]
    jumlah = count_bull_run(urut)
    result["bull_count"] = jumlah
    if jumlah < bull:
        result["stage"] = STAGE_SEQUENCE
        result["reason"] = (
            f"baru {jumlah} candle bullish beruntun di {interval}, "
            f"butuh {bull} berturut-turut"
        )
        return result
    result["checks"]["bull_sequence"] = True

    sinyal = window[-1]
    entry = float(sinyal.close)
    urutan_bull = window[-bull:]
    if sl_anchor(config) == "SEQUENCE":
        anchor_low = min(float(k.low) for k in urutan_bull)
    else:
        anchor_low = float(sinyal.low)
    result["signal_close"] = entry
    result["signal_close_time"] = int(sinyal.close_time)
    result["entry_low"] = anchor_low
    result["atr"] = atr_value(window, 14)

    info = exit_levels(entry, anchor_low, config)
    result["sl_price"] = info["sl_price"]
    result["tp_price"] = info["tp_price"]
    result["sl_pct"] = info["sl_pct"]
    result["tp_pct"] = info["tp_pct"]
    result["rr_ratio"] = info["rr_ratio"]
    if not info["ok"]:
        result["stage"] = STAGE_RISK
        result["reason"] = info["reason"]
        return result
    result["checks"]["risk"] = True
    result["ok"] = True
    result["stage"] = STAGE_OK
    result["reason"] = (
        f"{jumlah} candle bullish beruntun di {interval} (close {entry:.6g}), "
        + info["reason"]
    )
    result["values"] = {
        **dict(result.get("values") or {}),
        "entry_low": anchor_low,
        "sl_price": info["sl_price"],
        "tp_price": info["tp_price"],
        "rr_ratio": info["rr_ratio"],
        "bull_count": jumlah,
    }
    return result


def atr_value(klines: Sequence, period: int = 14) -> Optional[float]:
    period = int(period)
    if period <= 0 or len(klines) < period:
        return None
    trs: list[float] = []
    for index, candle in enumerate(klines):
        prev_close = float(klines[index - 1].close) if index else float(candle.close)
        try:
            trs.append(
                max(
                    float(candle.high) - float(candle.low),
                    abs(float(candle.high) - prev_close),
                    abs(float(candle.low) - prev_close),
                )
            )
        except (AttributeError, TypeError, ValueError):
            return None
    seed = sum(trs[:period]) / period
    value = seed
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value if math.isfinite(value) and value > 0.0 else None
