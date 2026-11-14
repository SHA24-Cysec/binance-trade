from __future__ import annotations

import math
from typing import Optional, Sequence

DEFAULT_LENGTH = 10
DEFAULT_MOMENTUM = 20
DEFAULT_BAND_MULT = 2.0
DEFAULT_ATR_PERIOD = 200
DEFAULT_SMOOTH = 15
DEFAULT_DELTA_LOOKBACK = 20
DEFAULT_ENTRY_TRIGGER = "FLIP"
DEFAULT_PULLBACK_MAX_BARS = 20
DEFAULT_PULLBACK_TOUCH_ATR = 0.25
DEFAULT_PULLBACK_TARGET = "VIDYA_LINE"


def _f(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _cfg_float(config: dict, key: str, default: float, low: float, high: float) -> float:
    value = _f((config or {}).get(key, default))
    return max(low, min(high, default if value is None else value))


def _cfg_int(config: dict, key: str, default: int, low: int, high: int) -> int:
    return int(round(_cfg_float(config, key, float(default), float(low), float(high))))


def _cfg_choice(config: dict, key: str, default: str, choices: tuple) -> str:
    value = str((config or {}).get(key, default) or default).strip().upper()
    return value if value in choices else default


def _cfg_bool(config: dict, key: str, default: bool) -> bool:
    value = (config or {}).get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def vidya_params(config: dict) -> dict:
    return {
        "length": _cfg_int(config, "VIDYA_LENGTH", DEFAULT_LENGTH, 2, 200),
        "momentum": _cfg_int(config, "VIDYA_MOMENTUM", DEFAULT_MOMENTUM, 2, 200),
        "band_mult": _cfg_float(config, "VIDYA_BAND_MULT", DEFAULT_BAND_MULT, 0.1, 20.0),
        "atr_period": _cfg_int(config, "VIDYA_ATR_PERIOD", DEFAULT_ATR_PERIOD, 2, 400),
        "smooth": DEFAULT_SMOOTH,
        "delta_lookback": _cfg_int(config, "VIDYA_DELTA_LOOKBACK", DEFAULT_DELTA_LOOKBACK, 2, 400),
        "delta_min_ratio": _cfg_float(config, "VIDYA_DELTA_MIN_RATIO", 0.0, -100.0, 100.0),
        "entry_trigger": _cfg_choice(config, "VIDYA_ENTRY_TRIGGER", DEFAULT_ENTRY_TRIGGER, ("FLIP", "PULLBACK")),
        "pullback_max_bars": _cfg_int(config, "VIDYA_PULLBACK_MAX_BARS", DEFAULT_PULLBACK_MAX_BARS, 1, 400),
        "pullback_touch_atr": _cfg_float(config, "VIDYA_PULLBACK_TOUCH_ATR", DEFAULT_PULLBACK_TOUCH_ATR, 0.0, 5.0),
        "pullback_target": _cfg_choice(config, "VIDYA_PULLBACK_TARGET", DEFAULT_PULLBACK_TARGET, ("VIDYA_LINE", "LOWER_BAND")),
        "pullback_require_bullish": _cfg_bool(config, "VIDYA_PULLBACK_REQUIRE_BULLISH", True),
    }


def required_bars(config: dict) -> int:
    p = vidya_params(config)
    return max(
        p["atr_period"] + p["momentum"] + p["smooth"] + 5,
        p["delta_lookback"] + 2,
        60,
    )


def _wilder_atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int) -> list[Optional[float]]:
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    tr = [highs[0] - lows[0]]
    for i in range(1, n):
        prev = closes[i - 1]
        tr.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    value = sum(tr[:period]) / period
    out[period - 1] = value if value > 0.0 else None
    for i in range(period, n):
        value = (value * (period - 1) + tr[i]) / period
        out[i] = value if value > 0.0 else None
    return out


def _pullback_entries(closes, opens, lows, vidya, atr, lower, trend_up, cross_up, cross_down, p) -> list[bool]:
    n = len(closes)
    out = [False] * n
    if p["entry_trigger"] != "PULLBACK":
        return out
    active = False
    flip_i = -1
    touched = False
    max_bars = p["pullback_max_bars"]
    touch_tol = p["pullback_touch_atr"]
    target = p["pullback_target"]
    need_bull = p["pullback_require_bullish"]
    for i in range(n):
        if cross_down[i]:
            active = False
            touched = False
            continue
        if cross_up[i]:
            active = True
            flip_i = i
            touched = False
            continue
        if not active or not trend_up[i]:
            continue
        age = i - flip_i
        if age > max_bars:
            active = False
            touched = False
            continue
        if age < 1 or vidya[i] is None or atr[i] is None:
            continue
        line = vidya[i] if target == "VIDYA_LINE" else lower[i]
        if line is None:
            continue
        if lows[i] <= line + touch_tol * atr[i]:
            touched = True
        if touched and closes[i] > line and (closes[i] >= opens[i] or not need_bull):
            out[i] = True
            active = False
            touched = False
    return out


def compute_series(bars: Sequence, config: dict) -> dict:
    p = vidya_params(config)
    n = len(bars)
    closes = [_f(getattr(b, "close", None)) for b in bars]
    highs = [_f(getattr(b, "high", None)) for b in bars]
    lows = [_f(getattr(b, "low", None)) for b in bars]
    opens = [_f(getattr(b, "open", None)) for b in bars]
    vols = [_f(getattr(b, "volume", 0.0)) or 0.0 for b in bars]
    empty = {
        "vidya": [None] * n, "atr": [None] * n, "upper": [None] * n, "lower": [None] * n,
        "trend_up": [False] * n, "cross_up": [False] * n, "cross_down": [False] * n,
        "pullback_entry": [False] * n,
        "delta": [None] * n, "delta_ratio": [None] * n, "ready": [False] * n, "params": p,
    }
    if n == 0 or any(c is None or c <= 0.0 for c in closes):
        return empty
    if any(h is None or low is None or o is None for h, low, o in zip(highs, lows, opens)):
        return empty

    length, momentum, smooth = p["length"], p["momentum"], p["smooth"]
    alpha = 2.0 / (length + 1.0)
    changes = [0.0] + [closes[i] - closes[i - 1] for i in range(1, n)]
    raw: list[Optional[float]] = [None] * n
    prev_raw = 0.0
    pos_sum = 0.0
    neg_sum = 0.0
    for i in range(n):
        ch = changes[i]
        pos = ch if ch >= 0.0 else 0.0
        neg = -ch if ch < 0.0 else 0.0
        pos_sum += pos
        neg_sum += neg
        if i >= momentum:
            old = changes[i - momentum]
            if old >= 0.0:
                pos_sum -= old
            else:
                neg_sum += old
        if i < momentum:
            prev_raw = 0.0
            continue
        denom = pos_sum + neg_sum
        abs_cmo = abs(pos_sum - neg_sum) / denom if denom > 0.0 else 0.0
        k = alpha * abs_cmo
        value = k * closes[i] + (1.0 - k) * prev_raw
        raw[i] = value
        prev_raw = value

    vidya: list[Optional[float]] = [None] * n
    run = 0.0
    for i in range(n):
        if raw[i] is not None:
            run += raw[i]
        if i >= smooth and raw[i - smooth] is not None:
            run -= raw[i - smooth]
        if i >= momentum + smooth - 1 and all(raw[j] is not None for j in range(i - smooth + 1, i + 1)):
            vidya[i] = run / smooth

    atr = _wilder_atr(highs, lows, closes, p["atr_period"])
    upper: list[Optional[float]] = [None] * n
    lower: list[Optional[float]] = [None] * n
    for i in range(n):
        if vidya[i] is not None and atr[i] is not None:
            upper[i] = vidya[i] + atr[i] * p["band_mult"]
            lower[i] = vidya[i] - atr[i] * p["band_mult"]

    trend_up = [False] * n
    cross_up = [False] * n
    cross_down = [False] * n
    prev_trend = False
    for i in range(n):
        up, low = upper[i], lower[i]
        if up is not None and low is not None and i > 0 and upper[i - 1] is not None and lower[i - 1] is not None:
            if closes[i] > up and closes[i - 1] <= upper[i - 1]:
                prev_trend = True
            elif closes[i] < low and closes[i - 1] >= lower[i - 1]:
                prev_trend = False
        trend_up[i] = prev_trend
        cross_up[i] = prev_trend and not trend_up[i - 1]
        cross_down[i] = (not prev_trend) and trend_up[i - 1]

    delta: list[Optional[float]] = [None] * n
    delta_ratio: list[Optional[float]] = [None] * n
    look = p["delta_lookback"]
    up_vol = 0.0
    down_vol = 0.0
    for i in range(n):
        if closes[i] > opens[i]:
            up_vol += vols[i]
        elif closes[i] < opens[i]:
            down_vol += vols[i]
        if i >= look:
            j = i - look
            if closes[j] > opens[j]:
                up_vol -= vols[j]
            elif closes[j] < opens[j]:
                down_vol -= vols[j]
        if i >= look - 1:
            delta[i] = up_vol - down_vol
            avg = (up_vol + down_vol) / 2.0
            delta_ratio[i] = (up_vol - down_vol) / avg * 100.0 if avg > 0.0 else 0.0

    pullback_entry = _pullback_entries(closes, opens, lows, vidya, atr, lower, trend_up, cross_up, cross_down, p)
    ready = [vidya[i] is not None and upper[i] is not None and delta[i] is not None for i in range(n)]
    return {
        "vidya": vidya, "atr": atr, "upper": upper, "lower": lower,
        "trend_up": trend_up, "cross_up": cross_up, "cross_down": cross_down,
        "pullback_entry": pullback_entry,
        "delta": delta, "delta_ratio": delta_ratio, "ready": ready, "params": p,
    }


def signal_at(series: dict, index: int, config: dict) -> dict:
    min_ratio = vidya_params(config)["delta_min_ratio"]
    out = {"ok": False, "stage": "", "reason": "", "trend_up": False, "cross_up": False, "delta": None, "delta_ratio": None, "vidya": None, "upper": None, "lower": None, "atr_band": None}
    if index < 0 or index >= len(series["ready"]):
        out.update(stage="HISTORY_ENTRY", reason="index di luar riwayat")
        return out
    if not series["ready"][index]:
        out.update(stage="HISTORY_ENTRY", reason="VIDYA, band, atau delta volume belum siap dari riwayat candle")
        return out
    out.update(
        trend_up=bool(series["trend_up"][index]),
        cross_up=bool(series["cross_up"][index]),
        delta=series["delta"][index],
        delta_ratio=series["delta_ratio"][index],
        vidya=series["vidya"][index],
        upper=series["upper"][index],
        lower=series["lower"][index],
        atr_band=series["atr"][index],
    )
    trigger = vidya_params(config)["entry_trigger"]
    out["entry_trigger"] = trigger
    if trigger == "PULLBACK":
        fired = bool(series.get("pullback_entry", [False] * len(series["ready"]))[index])
        fail_reason = "belum ada konfirmasi pullback ke garis VIDYA setelah flip naik"
        ok_label = "konfirmasi pullback VIDYA"
    else:
        fired = bool(series["cross_up"][index])
        fail_reason = "belum ada flip trend naik (close menembus band atas VIDYA)"
        ok_label = "flip trend naik VIDYA"
    if not fired:
        out.update(stage="VIDYA_TREND", reason=fail_reason)
        return out
    ratio = series["delta_ratio"][index]
    if ratio is None or ratio < min_ratio:
        out.update(stage="DELTA_VOLUME", reason=f"delta volume beli {ratio:.1f}% belum dominan (syarat minimal {min_ratio:g}%)")
        return out
    out.update(ok=True, stage="OK", reason=f"{ok_label} dengan delta volume beli {ratio:.1f}%")
    return out
