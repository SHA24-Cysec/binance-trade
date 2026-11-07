from __future__ import annotations

import math
from typing import Optional, Sequence

from strategy import exit_atr

ENTRY_RULE_DEFAULTS = {
    "ENTRY_RULE_ENABLED": True,
    "CONFIRM_INTERVAL": "1h",
    "MOMENTUM_BREAKOUT_LOOKBACK": 1,
    "MOMENTUM_BODY_ATR_MULT": 0.5,
    "MOMENTUM_VOLUME_MULT": 1.5,
    "MOMENTUM_VOLUME_LOOKBACK": 20,
    "ENTRY_ATR_PERIOD": exit_atr.DEFAULT_ATR_PERIOD,
    "EXIT_ATR_SL_MULT": exit_atr.DEFAULT_SL_MULT,
    "EXIT_ATR_TP_MULT": exit_atr.DEFAULT_TP_MULT,
    "EXIT_ATR_TRAIL_MULT": exit_atr.DEFAULT_TRAIL_MULT,
    "EXIT_ATR_BREAKEVEN_MULT": exit_atr.DEFAULT_BREAKEVEN_MULT,
    "EXIT_ATR_MAX_SL_PCT": exit_atr.DEFAULT_MAX_SL_PCT,
    "ENTRY_MIN_RR_RATIO": exit_atr.DEFAULT_MIN_RR,
    "EXIT_MAX_HOLD_HOURS": 24.0,
    "GAINER_VOLUME_FILTER_ENABLED": True,
    "GAINER_VOLUME_RISE_MULT": 1.2,
    "GAINER_VOLUME_AVG_DAYS": 3,
}

MOMENTUM_PARAM_KEYS = (
    "MOMENTUM_BREAKOUT_LOOKBACK",
    "MOMENTUM_BODY_ATR_MULT",
    "MOMENTUM_VOLUME_MULT",
    "MOMENTUM_VOLUME_LOOKBACK",
)

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


def exit_fixed_params(config: Optional[dict] = None) -> dict:
    hasil = dict(EXIT_FIXED)
    params = exit_atr.atr_params(config or {})
    hasil["breakeven_r_multiple"] = params["breakeven_mult"]
    hasil["trailing_atr_mult"] = params["trail_mult"]
    hasil["max_hold_hours"] = max_hold_hours(config)
    return hasil

SL_ANCHOR_ATR = "ATR"

STAGE_HISTORY = "HISTORY_ENTRY"
STAGE_CANDLE = "CANDLE_ENTRY"
STAGE_SEQUENCE = "MOMENTUM_BULL"
STAGE_BODY = "MOMENTUM_BODY"
STAGE_BREAKOUT = "MOMENTUM_BREAKOUT"
STAGE_VOLUME = "MOMENTUM_VOLUME"
STAGE_VOLUME_24H = "VOLUME_24H"
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


def _cfg_float(config: dict, key: str, default: float, low: float, high: float) -> float:
    sumber = config if isinstance(config, dict) else {}
    nilai = _finite(sumber.get(key, default))
    if nilai is None:
        nilai = default
    return max(low, min(high, nilai))


def _cfg_int(config: dict, key: str, default: int, low: int, high: int) -> int:
    return int(round(_cfg_float(config, key, float(default), float(low), float(high))))


def _cfg_bool(config: dict, key: str, default: bool) -> bool:
    sumber = config if isinstance(config, dict) else {}
    raw = sumber.get(key, default)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on", "aktif"}
    return bool(raw)


def rule_enabled(config: dict) -> bool:
    return _cfg_bool(config, "ENTRY_RULE_ENABLED", True)


def momentum_params(config: dict) -> dict:
    return {
        "lookback": _cfg_int(config, "MOMENTUM_BREAKOUT_LOOKBACK", 1, 1, 20),
        "body_atr_mult": _cfg_float(config, "MOMENTUM_BODY_ATR_MULT", 0.5, 0.0, 5.0),
        "volume_mult": _cfg_float(config, "MOMENTUM_VOLUME_MULT", 1.5, 0.0, 20.0),
        "volume_lookback": _cfg_int(config, "MOMENTUM_VOLUME_LOOKBACK", 20, 3, 120),
    }


def volume_rise_params(config: dict) -> dict:
    return {
        "enabled": _cfg_bool(config, "GAINER_VOLUME_FILTER_ENABLED", True),
        "mult": _cfg_float(config, "GAINER_VOLUME_RISE_MULT", 1.2, 0.0, 20.0),
        "days": _cfg_int(config, "GAINER_VOLUME_AVG_DAYS", 3, 1, 14),
    }


def _interval_minutes(interval: str) -> int:
    from strategy.indicators import INTERVAL_MINUTES

    menit = INTERVAL_MINUTES.get(str(interval).strip().lower())
    if menit is None:
        raise ValueError(
            f"Interval '{interval}' tidak dikenal; pilihan: "
            + ", ".join(sorted(INTERVAL_MINUTES, key=INTERVAL_MINUTES.get))
        )
    return int(menit)


def bars_per_day(config: dict) -> int:
    menit = _interval_minutes(momentum_interval(config))
    per_hari = 1440 // menit
    return max(1, int(per_hari))


def volume_rise_bars(config: dict) -> int:
    params = volume_rise_params(config)
    return bars_per_day(config) * (params["days"] + 1)


def _turnover(chunk: Sequence) -> Optional[float]:
    total = 0.0
    for bar in chunk:
        try:
            nilai = float(getattr(bar, "quote_volume", 0.0) or 0.0)
            if nilai <= 0.0:
                nilai = float(bar.close) * float(bar.volume)
        except (AttributeError, TypeError, ValueError):
            return None
        if not math.isfinite(nilai) or nilai < 0.0:
            return None
        total += nilai
    return total


def turnover_windows_available(bars: int, per_day: int) -> int:
    if per_day <= 0:
        return 0
    return max(0, int(bars) // int(per_day) - 1)


def turnover_rise_ratio(bars: Sequence, per_day: int, days: int) -> Optional[float]:
    if per_day <= 0 or days <= 0:
        return None
    tersedia = turnover_windows_available(len(bars), per_day)
    if tersedia < 1:
        return None
    hari = min(int(days), tersedia)
    butuh = per_day * (hari + 1)
    ekor = list(bars[-butuh:])
    sekarang = _turnover(ekor[-per_day:])
    if sekarang is None:
        return None
    lampau = [_turnover(ekor[i * per_day : (i + 1) * per_day]) for i in range(hari)]
    if any(nilai is None for nilai in lampau):
        return None
    rata = sum(lampau) / float(hari)
    if rata <= 0.0:
        return None
    return sekarang / rata


def volume_rise_check(klines: Sequence, config: dict) -> dict:
    params = volume_rise_params(config)
    hasil = {
        "ok": True,
        "skipped": False,
        "ratio": None,
        "stage": STAGE_VOLUME_24H,
        "reason": "",
    }
    if not params["enabled"] or params["mult"] <= 0.0:
        hasil["reason"] = "syarat kenaikan volume 24 jam nonaktif"
        return hasil
    per_hari = bars_per_day(config)
    hari = min(int(params["days"]), turnover_windows_available(len(klines), per_hari))
    rasio = turnover_rise_ratio(klines, per_hari, params["days"])
    if rasio is None:
        hasil["skipped"] = True
        hasil["reason"] = (
            f"turnover 24 jam tidak bisa dinilai, candle "
            f"{momentum_interval(config)} kurang dari {2 * per_hari} bar"
        )
        return hasil
    hasil["ratio"] = rasio
    hasil["days"] = hari
    if rasio + 1e-12 < params["mult"]:
        hasil["ok"] = False
        hasil["reason"] = (
            f"turnover 24 jam hanya {rasio:.2f} x rata-rata {hari} hari, "
            f"di bawah syarat {params['mult']:g} x"
        )
        return hasil
    hasil["reason"] = f"turnover 24 jam {rasio:.2f} x rata-rata {hari} hari"
    return hasil


def momentum_interval(config: dict) -> str:
    sumber = config if isinstance(config, dict) else {}
    raw = str(sumber.get("CONFIRM_INTERVAL", "1h") or "1h").strip().lower()
    return raw or "1h"


def min_rr_ratio(config: dict) -> float:
    return max(0.1, min(20.0, _cfg_float(config, "ENTRY_MIN_RR_RATIO", 2.0, 0.1, 20.0)))


def max_sl_pct(config: dict) -> float:
    return max(0.0, min(50.0, _cfg_float(config, "EXIT_ATR_MAX_SL_PCT", 5.0, 0.0, 50.0)))


def atr_period(config: dict) -> int:
    return exit_atr.atr_period(config)


def required_bars(config: dict) -> int:
    momentum = momentum_params(config)
    minimal = max(
        15,
        atr_period(config) + 2,
        momentum["volume_lookback"] + 2,
        momentum["lookback"] + 2,
    )
    return int(minimal)


def atr_window_bars(config: dict) -> int:
    return max(required_bars(config), _cfg_int(config, "CONFIRM_LOOKBACK_BARS", 60, 15, 1000))


def max_hold_hours(config: Optional[dict] = None) -> float:
    if config is None:
        return EXIT_MAX_HOLD_HOURS
    return _cfg_float(
        config, "EXIT_MAX_HOLD_HOURS", EXIT_MAX_HOLD_HOURS, 0.0, MAX_HOLD_HOURS_LIMIT
    )


def trailing_stop_price(highest_price: float, atr: float, mult: float) -> float:
    return exit_atr.trailing_stop_price(highest_price, atr, mult)


def breakeven_price(
    entry_price: float,
    sl_price: float = 0.0,
    r_multiple: float = 0.0,
    lock_pct: float = 0.0,
) -> float:
    return exit_atr.breakeven_price(entry_price, lock_pct)


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


def atr_value(klines: Sequence, period: int = 14) -> Optional[float]:
    period = int(period)
    if period <= 0 or len(klines) < period:
        return None
    trs: list[float] = []
    for index, candle in enumerate(klines):
        try:
            prev_close_raw = klines[index - 1].close if index else candle.close
            prev_close = float(prev_close_raw)
            high = float(candle.high)
            low = float(candle.low)
            close = float(candle.close)
        except (AttributeError, TypeError, ValueError):
            return None
        if not all(math.isfinite(x) and x > 0.0 for x in (high, low, close)):
            return None
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    seed = sum(trs[:period]) / period
    nilai = seed
    for tr in trs[period:]:
        nilai = (nilai * (period - 1) + tr) / period
    return nilai if math.isfinite(nilai) and nilai > 0.0 else None


def candle_body(bar) -> float:
    try:
        return abs(float(bar.close) - float(bar.open))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def breakout_level(bars: Sequence, lookback: int) -> Optional[float]:
    jumlah = int(lookback)
    if jumlah <= 0 or len(bars) < jumlah + 1:
        return None
    sebelumnya = bars[-(jumlah + 1) : -1]
    tertinggi = 0.0
    for bar in sebelumnya:
        try:
            high = float(bar.high)
        except (AttributeError, TypeError, ValueError):
            return None
        if not math.isfinite(high) or high <= 0.0:
            return None
        tertinggi = max(tertinggi, high)
    return tertinggi if tertinggi > 0.0 else None


def volume_ratio(bars: Sequence, lookback: int) -> Optional[float]:
    jumlah = int(lookback)
    if jumlah <= 0 or len(bars) < jumlah + 1:
        return None
    sebelumnya = bars[-(jumlah + 1) : -1]
    volume: list[float] = []
    for bar in sebelumnya:
        try:
            nilai = float(bar.volume)
        except (AttributeError, TypeError, ValueError):
            return None
        if not math.isfinite(nilai) or nilai < 0.0:
            return None
        volume.append(nilai)
    rata = sum(volume) / len(volume)
    if rata <= 0.0:
        return None
    try:
        terakhir = float(bars[-1].volume)
    except (AttributeError, TypeError, ValueError):
        return None
    if not math.isfinite(terakhir) or terakhir < 0.0:
        return None
    return terakhir / rata


def exit_levels(
    entry_price: float,
    anchor_low: float,
    config: dict,
    *,
    atr: Optional[float] = None,
    fallback_allowed: bool = False,
) -> dict:
    entry = _finite(entry_price) or 0.0
    low = _finite(anchor_low) or 0.0
    jarak = _finite(atr)
    dasar = {
        "ok": False,
        "stage": STAGE_RISK,
        "reason": "",
        "sl_price": None,
        "tp_price": None,
        "sl_pct": None,
        "tp_pct": None,
        "rr_ratio": None,
        "min_rr_ratio": min_rr_ratio(config),
        "max_sl_pct": max_sl_pct(config),
        "anchor_low": low if low > 0.0 else None,
        "atr": jarak if jarak and jarak > 0.0 else None,
    }

    if entry <= 0.0:
        dasar["reason"] = "harga entry tidak valid untuk menghitung level ATR"
        return dasar

    if jarak is not None and jarak > 0.0:
        rencana = exit_atr.plan_levels(entry, jarak, config)
        dasar.update(
            {
                "ok": bool(rencana["ok"]),
                "reason": str(rencana["reason"]),
                "sl_price": rencana["sl_price"],
                "tp_price": rencana["tp_price"],
                "sl_pct": rencana["sl_pct"],
                "tp_pct": rencana["tp_pct"],
                "rr_ratio": rencana["rr_ratio"],
                "atr": rencana["atr"],
                "sl_mult": rencana["sl_mult"],
                "tp_mult": rencana["tp_mult"],
                "trail_mult": rencana["trail_mult"],
                "breakeven_mult": rencana["breakeven_mult"],
            }
        )
        if dasar["ok"]:
            dasar["stage"] = STAGE_OK
        return dasar

    if not fallback_allowed:
        dasar["reason"] = (
            "ATR tidak tersedia sehingga stop ATR tidak bisa dihitung; entry ditolak "
            "daripada memakai stop tebakan"
        )
        return dasar

    sl_pct = EXIT_FALLBACK_SL_PCT
    rr = min_rr_ratio(config)
    sl = entry * (1.0 - sl_pct / 100.0)
    tp = entry + (entry - sl) * rr
    dasar.update(
        {
            "ok": True,
            "stage": STAGE_OK,
            "sl_price": sl,
            "tp_price": tp,
            "sl_pct": sl_pct,
            "tp_pct": (tp - entry) / entry * 100.0,
            "rr_ratio": rr,
            "reason": (
                f"ATR tidak tersedia, dipakai stop cadangan {sl_pct:g}% dan TP "
                f"R:R 1:{rr:.2f} sebagai level sementara"
            ),
        }
    )
    return dasar


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
        "body_atr": None,
        "volume_ratio": None,
        "breakout_level": None,
        "checks": {
            "history": False,
            "candles": False,
            "bull_sequence": False,
            "momentum_body": False,
            "momentum_breakout": False,
            "momentum_volume": False,
            "risk": False,
        },
        "values": {},
    }


def evaluate_entry_rule(
    klines: Sequence,
    config: dict,
    signal_close_time_ms: Optional[int] = None,
) -> dict:
    interval = momentum_interval(config)
    momentum = momentum_params(config)
    period = atr_period(config)
    need = required_bars(config)
    rows = [
        k
        for k in (klines or [])
        if signal_close_time_ms is None
        or int(getattr(k, "close_time", 0)) <= int(signal_close_time_ms)
    ]
    window = rows[-need:] if need > 0 else list(rows)
    result = _empty_result(interval, len(window), need, MOMENTUM_CONFIRM_CANDLES)

    if not rule_enabled(config):
        result["ok"] = True
        result["stage"] = STAGE_DISABLED
        result["reason"] = "filter momentum nonaktif di konfigurasi"
        for kunci in result["checks"]:
            result["checks"][kunci] = True
        if window:
            last = window[-1]
            entry = float(last.close)
            jarak = atr_value(rows[-atr_window_bars(config) :], period)
            info = exit_levels(
                entry, float(last.low), config, atr=jarak, fallback_allowed=True
            )
            result["signal_close"] = entry
            result["signal_close_time"] = int(last.close_time)
            result["entry_low"] = float(last.low)
            result["atr"] = jarak
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

    sinyal = window[-1]
    sebelumnya = window[-2] if len(window) >= 2 else None
    entry = float(sinyal.close)
    jarak = atr_value(rows[-atr_window_bars(config) :], period)
    result["signal_close"] = entry
    result["signal_close_time"] = int(sinyal.close_time)
    result["entry_low"] = float(sinyal.low)
    result["atr"] = jarak
    result["bull_count"] = count_bull_run(window)

    if jarak is None or jarak <= 0.0:
        result["stage"] = STAGE_RISK
        result["reason"] = (
            f"ATR periode {period} pada {interval} tidak dapat dihitung dari "
            f"{len(window)} candle, momentum tidak bisa dinilai"
        )
        return result

    if not is_bullish(sinyal, sebelumnya):
        result["stage"] = STAGE_SEQUENCE
        result["reason"] = (
            f"candle {interval} terakhir tidak bullish (close {entry:.6g} tidak di atas "
            f"open {float(sinyal.open):.6g} dan close sebelumnya)"
        )
        return result
    result["checks"]["bull_sequence"] = True

    body = candle_body(sinyal)
    rasio_body = body / jarak if jarak > 0.0 else 0.0
    result["body_atr"] = rasio_body
    if momentum["body_atr_mult"] > 0.0 and rasio_body + 1e-12 < momentum["body_atr_mult"]:
        result["stage"] = STAGE_BODY
        result["reason"] = (
            f"body candle {interval} hanya {rasio_body:.2f} x ATR, "
            f"di bawah syarat {momentum['body_atr_mult']:g} x ATR"
        )
        return result
    result["checks"]["momentum_body"] = True

    batas = breakout_level(window, momentum["lookback"])
    result["breakout_level"] = batas
    if batas is None:
        result["stage"] = STAGE_BREAKOUT
        result["reason"] = f"high acuan breakout {interval} tidak tersedia"
        return result
    if entry <= batas + 1e-12:
        result["stage"] = STAGE_BREAKOUT
        result["reason"] = (
            f"close {entry:.6g} belum melewati high {momentum['lookback']} candle "
            f"{interval} sebelumnya di {batas:.6g}"
        )
        return result
    result["checks"]["momentum_breakout"] = True

    rasio_volume = volume_ratio(window, momentum["volume_lookback"])
    result["volume_ratio"] = rasio_volume
    if momentum["volume_mult"] > 0.0:
        if rasio_volume is None:
            result["stage"] = STAGE_VOLUME
            result["reason"] = (
                f"volume {momentum['volume_lookback']} candle {interval} tidak bisa "
                "dihitung sehingga lonjakan volume tidak terverifikasi"
            )
            return result
        if rasio_volume + 1e-12 < momentum["volume_mult"]:
            result["stage"] = STAGE_VOLUME
            result["reason"] = (
                f"volume candle {interval} hanya {rasio_volume:.2f} x rata-rata "
                f"{momentum['volume_lookback']} candle, di bawah syarat "
                f"{momentum['volume_mult']:g} x"
            )
            return result
    result["checks"]["momentum_volume"] = True

    info = exit_levels(entry, float(sinyal.low), config, atr=jarak)
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
        f"momentum {interval} terpenuhi: close {entry:.6g} menembus high "
        f"{batas:.6g}, body {rasio_body:.2f} x ATR, volume {rasio_volume:.2f} x "
        f"rata-rata; " + info["reason"]
    )
    result["values"] = {
        "entry_low": float(sinyal.low),
        "sl_price": info["sl_price"],
        "tp_price": info["tp_price"],
        "rr_ratio": info["rr_ratio"],
        "bull_count": result["bull_count"],
        "atr": jarak,
        "body_atr": rasio_body,
        "volume_ratio": rasio_volume,
        "breakout_level": batas,
    }
    return result


def take_profit_due(entry_price, target_price, current_price) -> bool:
    entry = _finite(entry_price)
    target = _finite(target_price)
    current = _finite(current_price)
    return bool(
        EXIT_TAKE_PROFIT_ENABLED
        and entry is not None
        and target is not None
        and current is not None
        and 0.0 < entry < target <= current
    )
