from __future__ import annotations

import math
from typing import NamedTuple, Optional


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
DIRECTION_DEFAULT_INTERVAL = "1d"
ZONE_DEFAULT_INTERVAL = "1h"
TIMING_DEFAULT_INTERVAL = "5m"


def interval_ratio(target: str, source: str) -> int:
    """Jumlah candle sumber yang dirangkai menjadi satu candle target.

    Dipakai backtest untuk menyamakan candle timeframe tinggi (Daily, H1)
    dengan cara merangkai candle sumber. Live memakai candle asli dari bursa.
    """
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


def required_lookback_bars(config: dict) -> int:
    rolling_enabled = bool(config.get("ROLLING_VOLUME_FILTER_ENABLED", False))
    rolling_lookback = (
        int(config.get("ROLLING_VOLUME_LOOKBACK_BARS", 20) or 20)
        if rolling_enabled
        else 0
    )
    confirmation_bars = (
        int(config.get("ROLLING_VOLUME_CONFIRMATION_BARS", 1) or 1)
        if rolling_enabled
        else 0
    )
    demand_lookback = (
        int(config.get("DEMAND_LOOKBACK_BARS", 20) or 20) + 1
        if bool(config.get("DEMAND_ZONE_FILTER_ENABLED", False))
        else 0
    )
    atr_period = max(1, int(config.get("ATR_PERIOD", 14) or 14))
    timing_lookback = (
        timing_window_bars(config)
        if bool(config.get("TIMING_FILTER_ENABLED", False))
        else 0
    )
    breakout_lookback = (
        breakout_retest_required_bars(config)
        if bool(config.get("BREAKOUT_RETEST_FILTER_ENABLED", False))
        else 0
    )
    return max(
        atr_period,
        rolling_lookback + max(1, confirmation_bars) if rolling_enabled else 0,
        demand_lookback,
        timing_lookback,
        breakout_lookback,
    )


def confirm_window_bars(config: dict) -> int:
    lookback = int(config.get("CONFIRM_LOOKBACK_BARS", 48) or 48)
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


def breakout_retest_enabled(config: dict) -> bool:
    return bool(config.get("BREAKOUT_RETEST_FILTER_ENABLED", False))


def breakout_retest_required_bars(config: dict) -> int:
    if not breakout_retest_enabled(config):
        return 0
    lookback = max(5, int(config.get("BREAKOUT_LOOKBACK_BARS", 30) or 30))
    wing = max(1, int(config.get("BREAKOUT_PIVOT_WING_BARS", 2) or 2))
    max_bars = max(1, int(config.get("BREAKOUT_RETEST_MAX_BARS", 12) or 12))
    atr_period = max(2, int(config.get("BREAKOUT_ATR_PERIOD", 14) or 14))
    return max(
        lookback + max_bars + 2 * wing + 2,
        atr_period + max_bars + 2,
        2 * wing + 4,
    )


def _is_pivot_high(klines: list[Kline], index: int, wing: int) -> bool:
    if index < wing or index + wing >= len(klines):
        return False
    level = float(klines[index].high)
    return all(
        level > float(klines[pos].high)
        for pos in range(index - wing, index + wing + 1)
        if pos != index
    )


def evaluate_breakout_retest(klines: list[Kline], config: dict) -> dict:
    interval = str(config.get("CONFIRM_INTERVAL", TIMING_DEFAULT_INTERVAL) or TIMING_DEFAULT_INTERVAL)
    required = breakout_retest_required_bars(config)
    kosong = {
        "interval": interval,
        "bars": len(klines or []),
        "required": required,
        "breakout_level": None,
        "breakout_index": None,
        "breakout_time": None,
        "pivot_index": None,
        "retest_touches": 0,
        "retest_zone_low": None,
        "retest_zone_high": None,
        "breakout_atr": None,
    }
    if not breakout_retest_enabled(config):
        return {
            "ok": True,
            "reason": f"filter breakout-retest {interval} nonaktif",
            **kosong,
        }
    jendela = list(klines or [])[-required:]
    kosong["bars"] = len(jendela)
    if len(jendela) < required:
        return {
            "ok": False,
            "reason": f"data breakout-retest {interval} kurang: {len(jendela)} dari minimum {required}",
            **kosong,
        }
    for candle in jendela:
        values = (
            float(candle.open),
            float(candle.high),
            float(candle.low),
            float(candle.close),
        )
        if (
            not all(math.isfinite(value) and value > 0 for value in values)
            or values[1] < values[2]
        ):
            return {
                "ok": False,
                "reason": f"data OHLC breakout-retest {interval} tidak valid",
                **kosong,
            }

    lookback = max(5, int(config.get("BREAKOUT_LOOKBACK_BARS", 30) or 30))
    wing = max(1, int(config.get("BREAKOUT_PIVOT_WING_BARS", 2) or 2))
    max_bars = max(1, int(config.get("BREAKOUT_RETEST_MAX_BARS", 12) or 12))
    atr_period = max(2, int(config.get("BREAKOUT_ATR_PERIOD", 14) or 14))
    tolerance_atr = max(
        0.0, float(config.get("BREAKOUT_RETEST_TOLERANCE_ATR", 0.2) or 0.0)
    )
    max_touches = max(
        1, int(config.get("BREAKOUT_RETEST_MAX_TOUCHES", 2) or 2)
    )
    require_bullish = bool(config.get("BREAKOUT_RETEST_REQUIRE_BULLISH", True))
    last_index = len(jendela) - 1
    earliest_breakout = max(2 * wing + 1, last_index - max_bars)
    breakout = None

    for breakout_index in range(last_index - 1, earliest_breakout - 1, -1):
        pivot_start = max(wing, breakout_index - lookback)
        pivot_stop = breakout_index - wing
        pivots = [
            index
            for index in range(pivot_start, pivot_stop)
            if _is_pivot_high(jendela, index, wing)
        ]
        if not pivots:
            continue
        pivot_index = pivots[-1]
        level = float(jendela[pivot_index].high)
        breakout_close = float(jendela[breakout_index].close)
        previous_close = float(jendela[breakout_index - 1].close)
        if breakout_close > level and previous_close <= level:
            breakout = (breakout_index, pivot_index, level)
            break

    if breakout is None:
        return {
            "ok": False,
            "reason": f"belum ada breakout {interval} tertutup di atas swing high terkonfirmasi",
            **kosong,
        }

    breakout_index, pivot_index, level = breakout
    breakout_atr = atr(jendela[: breakout_index + 1], atr_period)
    if breakout_atr is None:
        return {
            "ok": False,
            "reason": f"ATR{atr_period} saat breakout {interval} belum terdefinisi",
            **{
                **kosong,
                "breakout_level": level,
                "breakout_index": breakout_index,
                "breakout_time": int(jendela[breakout_index].close_time),
            },
        }

    tolerance = float(breakout_atr) * tolerance_atr
    zone_low = level - tolerance
    zone_high = level + tolerance
    post_breakout = jendela[breakout_index + 1 :]
    latest = post_breakout[-1]
    touches = sum(
        1
        for candle in post_breakout
        if float(candle.low) <= zone_high and float(candle.high) >= zone_low
    )
    terisi = {
        **kosong,
        "breakout_level": level,
        "breakout_index": breakout_index,
        "breakout_time": int(jendela[breakout_index].close_time),
        "pivot_index": pivot_index,
        "retest_touches": touches,
        "retest_zone_low": zone_low,
        "retest_zone_high": zone_high,
        "breakout_atr": float(breakout_atr),
    }

    if any(float(candle.close) < zone_low for candle in post_breakout[:-1]):
        return {
            "ok": False,
            "reason": f"struktur breakout {interval} gagal sebelum retest: close turun di bawah {zone_low:.6g}",
            **terisi,
        }
    if not zone_low <= float(latest.low) <= zone_high:
        return {
            "ok": False,
            "reason": (
                f"candle {interval} terakhir belum retest area {zone_low:.6g} sampai "
                f"{zone_high:.6g} di sekitar breakout {level:.6g}"
            ),
            **terisi,
        }
    if float(latest.close) <= level:
        return {
            "ok": False,
            "reason": f"retest {interval} belum bertahan: close {latest.close:.6g} tidak di atas level {level:.6g}",
            **terisi,
        }
    if require_bullish and float(latest.close) <= float(latest.open):
        return {
            "ok": False,
            "reason": f"candle retest {interval} belum bullish",
            **terisi,
        }
    if touches > max_touches:
        return {
            "ok": False,
            "reason": f"area breakout {interval} sudah disentuh {touches} kali, maksimum {max_touches}",
            **terisi,
        }

    age = last_index - breakout_index
    return {
        "ok": True,
        "reason": (
            f"breakout-retest {interval} valid di {level:.6g}: breakout {age} candle lalu, "
            f"retest {touches}/{max_touches}, toleransi {tolerance_atr:g} ATR"
        ),
        **terisi,
    }


def detect_demand_zone(klines: list[Kline], config: dict) -> dict:
    if not bool(config.get("DEMAND_ZONE_FILTER_ENABLED", False)):
        return {
            "ok": True,
            "reason": "filter zona demand nonaktif",
            "zone_low": None,
            "zone_high": None,
            "distance_pct": None,
            "close_position": None,
        }

    lookback = max(3, int(config.get("DEMAND_LOOKBACK_BARS", 20) or 20))
    buffer_pct = max(0.0, float(config.get("DEMAND_ZONE_BUFFER_PCT", 0.8) or 0.0))
    max_dist_pct = max(0.0, float(config.get("DEMAND_MAX_DISTANCE_PCT", 3.5) or 0.0))
    inside = bool(config.get("DEMAND_REQUIRE_INSIDE_ZONE", False))
    toleransi_pct = 0.0 if inside else max_dist_pct
    min_close_pos = max(
        0.0, min(1.0, float(config.get("DEMAND_MIN_CLOSE_POSITION", 0.45) or 0.0))
    )

    required = lookback + 1
    if not klines or len(klines) < required:
        return {
            "ok": False,
            "reason": f"data zona demand kurang: {len(klines) if klines else 0} dari minimum {required}",
            "zone_low": None,
            "zone_high": None,
            "distance_pct": None,
            "close_position": None,
        }

    prior = klines[-1 - lookback : -1]
    signal = klines[-1]

    for candle in prior + [signal]:
        if (
            not math.isfinite(candle.open)
            or not math.isfinite(candle.high)
            or not math.isfinite(candle.low)
            or not math.isfinite(candle.close)
            or candle.low <= 0
            or candle.high < candle.low
        ):
            return {
                "ok": False,
                "reason": "data OHLC candle untuk zona demand tidak valid",
                "zone_low": None,
                "zone_high": None,
                "distance_pct": None,
                "close_position": None,
            }

    window = prior
    anchor = min(float(k.close) for k in window)
    band = anchor * (1.0 + buffer_pct / 100.0)
    shelf = [k for k in window if float(k.close) <= band]
    if len(shelf) >= 2:
        zone_low = max(
            min(float(k.low) for k in shelf), anchor * (1.0 - buffer_pct / 100.0)
        )
        base_candles = shelf
    else:
        base_candles = window[-min(len(window), 6) :]
        zone_low = min(float(k.low) for k in base_candles)
    base_body = min(max(float(k.open), float(k.close)) for k in base_candles)
    zone_high = max(base_body, zone_low * (1.0 + buffer_pct / 100.0))

    sig_open = float(signal.open)
    sig_high = float(signal.high)
    sig_low = float(signal.low)
    sig_close = float(signal.close)
    prev_close = float(prior[-1].close)

    candle_range = sig_high - sig_low
    if candle_range > 0:
        close_pos = (sig_close - sig_low) / candle_range
    else:
        close_pos = 1.0 if sig_close >= prev_close else 0.0

    distance_pct = (
        max(0.0, (sig_close / zone_high - 1.0) * 100.0) if zone_high > 0 else 0.0
    )

    if sig_close < zone_low:
        return {
            "ok": False,
            "reason": f"zona demand ditembus: close {sig_close:.6g} di bawah dasar demand {zone_low:.6g}",
            "zone_low": zone_low,
            "zone_high": zone_high,
            "distance_pct": distance_pct,
            "close_position": close_pos,
        }

    if sig_close < sig_open or sig_close < prev_close:
        return {
            "ok": False,
            "reason": (
                f"reaksi demand lemah: candle sinyal bearish/turun "
                f"(open={sig_open:.6g}, close={sig_close:.6g}, prev={prev_close:.6g})"
            ),
            "zone_low": zone_low,
            "zone_high": zone_high,
            "distance_pct": distance_pct,
            "close_position": close_pos,
        }

    if close_pos + 1e-9 < min_close_pos:
        return {
            "ok": False,
            "reason": (
                f"dorongan demand kurang: posisi close candle {close_pos:.2f} "
                f"di bawah minimum {min_close_pos:.2f}"
            ),
            "zone_low": zone_low,
            "zone_high": zone_high,
            "distance_pct": distance_pct,
            "close_position": close_pos,
        }

    if distance_pct > toleransi_pct:
        return {
            "ok": False,
            "reason": (
                f"harga terlalu jauh di atas zona demand: +{distance_pct:.2f}% "
                f"di atas batas atas zona {zone_high:.6g}"
                + (
                    " (wajib di dalam zona)"
                    if inside
                    else f" (batas {max_dist_pct:g}%)"
                )
            ),
            "zone_low": zone_low,
            "zone_high": zone_high,
            "distance_pct": distance_pct,
            "close_position": close_pos,
        }

    return {
        "ok": True,
        "reason": (
            f"demand zone valid [{zone_low:.6g}..{zone_high:.6g}], "
            f"jarak +{distance_pct:.2f}%, close_pos {close_pos:.2f}"
            + (", di dalam zona" if inside else "")
        ),
        "zone_low": zone_low,
        "zone_high": zone_high,
        "distance_pct": distance_pct,
        "close_position": close_pos,
    }


def resolve_exit_levels(config: dict) -> dict:
    use_atr = bool(config.get("USE_ATR_EXIT", False))
    if use_atr:
        period = max(1, int(config.get("ATR_PERIOD", 14) or 14))
        sl = abs(float(config.get("ATR_MULT_SL", 1.5) or 0.0))
        tp = abs(float(config.get("ATR_MULT_TP", 3.0) or 0.0))
        trail = abs(float(config.get("ATR_MULT_TRAIL", 1.0) or 0.0))
        be_trigger = abs(float(config.get("ATR_MULT_BE_TRIGGER", 1.0) or 0.0))
        be_lock = abs(float(config.get("ATR_MULT_BE_LOCK", 0.1) or 0.0))
        trail_start = abs(float(config.get("ATR_MULT_TRAIL_START", 1.5) or 0.0))
        trail = min(trail, sl)
        be_trigger = min(be_trigger, trail_start)
        be_lock = min(be_lock, be_trigger)
        atr_value = config.get("_atr_value")
        if atr_value is not None:
            atr_value = abs(float(atr_value))
        scale = atr_value if atr_value is not None else 1.0
        return {
            "sl_pct": sl * scale,
            "tp_pct": tp * scale,
            "be_trigger_pct": be_trigger * scale,
            "be_lock_pct": be_lock * scale,
            "trail_start_pct": trail_start * scale,
            "trail_step_pct": trail * scale,
            "atr_period": period,
            "atr_value": atr_value,
            "atr_mult_sl": sl,
            "atr_mult_tp": tp,
            "atr_mult_trail": trail,
            "source": "ATR",
            "note": "Level exit berbasis ATR; invariant diterapkan",
        }

    fixed_sl = abs(float(config.get("SL_PCT", 1.8)))
    fixed_tp = abs(float(config.get("TP_PCT", 4.0)))
    fixed_be_trigger = abs(float(config.get("BE_TRIGGER_PCT", 1.0)))
    fixed_be_lock = abs(float(config.get("BE_LOCK_PCT", 0.15)))
    fixed_trail_start = abs(float(config.get("TRAILING_START_PCT", 1.5)))
    fixed_trail_step = abs(float(config.get("TRAILING_STEP_PCT", 0.6)))
    fixed_trail_step = min(fixed_trail_step, fixed_sl)
    fixed_be_trigger = min(fixed_be_trigger, fixed_trail_start)
    fixed_be_lock = min(fixed_be_lock, fixed_be_trigger)
    return {
        "sl_pct": fixed_sl,
        "tp_pct": fixed_tp,
        "be_trigger_pct": fixed_be_trigger,
        "be_lock_pct": fixed_be_lock,
        "trail_start_pct": fixed_trail_start,
        "trail_step_pct": fixed_trail_step,
        "source": "FIXED",
        "note": "Level exit tetap dari config; invariant diterapkan",
    }


MS_PER_HOUR = 3_600_000
TIME_STOP_REASON = "TIME_STOP"
# Batas atas MAX_HOLD_HOURS yang diterima validator dan schema (30 hari).
MAX_HOLD_HOURS_LIMIT = 720.0


def max_hold_ms(config: dict, interval: str) -> int:
    """Batas waktu tahan posisi (time stop) dalam ms; 0 berarti NONAKTIF.

    Aturan ini dipakai bersama oleh backtest dan bot live supaya keduanya
    menutup posisi yang mengendap pada umur yang sama. Nilainya hanya berubah
    bersama config, jadi panggil sekali di luar loop simulasi (atau cache di
    sisi live) supaya tidak menambah beban per bar maupun per poll.

    Syarat minimal 2 bar mencegah time stop menutup posisi pada bar entry-nya
    sendiri: exit semacam itu pasti rugi karena biaya eksekusi belum sempat
    tertutup oleh pergerakan harga.
    """
    raw = config.get("MAX_HOLD_HOURS")
    if raw is None or raw == "":
        return 0
    try:
        cap_h = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"MAX_HOLD_HOURS harus berupa angka, diterima: {raw!r}")
    if not math.isfinite(cap_h):
        raise ValueError("MAX_HOLD_HOURS harus angka berhingga.")
    if cap_h < 0.0:
        raise ValueError("MAX_HOLD_HOURS tidak boleh negatif (0 = nonaktif).")
    if cap_h == 0.0:
        return 0
    if cap_h > MAX_HOLD_HOURS_LIMIT:
        raise ValueError(
            f"MAX_HOLD_HOURS maksimal {MAX_HOLD_HOURS_LIMIT:g} jam "
            f"({MAX_HOLD_HOURS_LIMIT / 24:g} hari), diminta {cap_h:g} jam."
        )
    bar_ms = interval_to_ms(interval)
    cap_ms = int(round(cap_h * MS_PER_HOUR))
    if cap_ms < 2 * bar_ms:
        raise ValueError(
            f"MAX_HOLD_HOURS {cap_h:g} jam terlalu sempit untuk interval {interval}: "
            f"minimal 2 bar = {2 * bar_ms / MS_PER_HOUR:g} jam."
        )
    return cap_ms


def time_stop_due(entry_time: int, now_ms: int, cap_ms: int, bar_ms: int) -> bool:
    """Keputusan time stop: tutup pada evaluasi terakhir yang masih di dalam batas.

    Menunggu satu bar lagi (backtest) atau satu poll lagi (live) sudah pasti
    membuat umur posisi melewati cap, jadi exit dilakukan sekarang. Dengan
    aturan ini umur posisi saat exit tidak pernah lebih dari MAX_HOLD_HOURS.

    entry_time 0 berarti waktu entry tidak diketahui (misalnya state lama yang
    belum mencatatnya), sehingga time stop tidak dijalankan untuk posisi itu.
    """
    if int(cap_ms) <= 0 or int(bar_ms) <= 0:
        return False
    entry = int(entry_time or 0)
    now = int(now_ms or 0)
    if entry <= 0 or now <= 0:
        return False
    return (now - entry) + int(bar_ms) > int(cap_ms)


TREND_KLINE_LIMIT = 1000

DIRECTION_DEFAULTS = {
    "DIRECTION_FILTER_ENABLED": True,
    "DIRECTION_INTERVAL": DIRECTION_DEFAULT_INTERVAL,
    "DIRECTION_EMA_FAST": 20,
    "DIRECTION_EMA_SLOW": 50,
    "DIRECTION_ADX_PERIOD": 14,
    "DIRECTION_ADX_MIN": 25.0,
    "DIRECTION_LOOKBACK_BARS": 60,
    "DIRECTION_ZONE_FILTER_ENABLED": False,
    "DIRECTION_ZONE_LOOKBACK_BARS": 20,
    "DIRECTION_ZONE_BUFFER_PCT": 2.0,
    "DIRECTION_ZONE_MAX_DISTANCE_PCT": 5.0,
    "DIRECTION_ZONE_MIN_CLOSE_POSITION": 0.35,
    "DIRECTION_ZONE_REQUIRE_INSIDE_ZONE": False,
}

ZONE_DEFAULTS = {
    "ZONE_FILTER_ENABLED": True,
    "ZONE_INTERVAL": ZONE_DEFAULT_INTERVAL,
    "ZONE_LOOKBACK_BARS": 72,
    "ZONE_BUFFER_PCT": 1.5,
    "ZONE_MAX_DISTANCE_PCT": 4.0,
    "ZONE_REQUIRE_INSIDE_ZONE": False,
    "ZONE_MIN_CLOSE_POSITION": 0.35,
    "ZONE_EMA_FAST": 20,
    "ZONE_EMA_SLOW": 50,
    "ZONE_ADX_PERIOD": 14,
    "ZONE_ADX_MIN": 25.0,
    "ZONE_TREND_LOOKBACK_BARS": 60,
}

TIMING_DEFAULTS = {
    "TIMING_FILTER_ENABLED": True,
    "TIMING_EMA_FAST": 20,
    "TIMING_EMA_SLOW": 50,
    "TIMING_ADX_PERIOD": 14,
    "TIMING_ADX_MIN": 25.0,
    "TIMING_LOOKBACK_BARS": 60,
}

BREAKOUT_RETEST_DEFAULTS = {
    "BREAKOUT_RETEST_FILTER_ENABLED": True,
    "BREAKOUT_LOOKBACK_BARS": 30,
    "BREAKOUT_PIVOT_WING_BARS": 2,
    "BREAKOUT_ATR_PERIOD": 14,
    "BREAKOUT_RETEST_TOLERANCE_ATR": 0.2,
    "BREAKOUT_RETEST_MAX_BARS": 12,
    "BREAKOUT_RETEST_MAX_TOUCHES": 2,
    "BREAKOUT_RETEST_REQUIRE_BULLISH": True,
}


def _interval_from_config(config: dict, key: str, default: str) -> str:
    raw = str(config.get(key, default) or default).strip().lower()
    if raw not in INTERVAL_MINUTES:
        raise ValueError(
            f"{key} '{raw}' tidak dikenal. Pilihan: "
            + ", ".join(sorted(INTERVAL_MINUTES, key=INTERVAL_MINUTES.get))
        )
    return raw


def _ema_adx_params(config: dict, prefix: str, default_fast: int, default_slow: int) -> tuple:
    fast = max(2, int(config.get(f"{prefix}_EMA_FAST", default_fast) or default_fast))
    slow = max(3, int(config.get(f"{prefix}_EMA_SLOW", default_slow) or default_slow))
    adx_period = max(2, int(config.get(f"{prefix}_ADX_PERIOD", 14) or 14))
    adx_min = float(config.get(f"{prefix}_ADX_MIN", 25.0) or 0.0)
    if adx_min < 0:
        adx_min = 0.0
    return fast, slow, adx_period, adx_min


def direction_interval(config: dict) -> str:
    return _interval_from_config(
        config, "DIRECTION_INTERVAL", DIRECTION_DEFAULT_INTERVAL
    )


def direction_interval_minutes(config: dict) -> int:
    return int(INTERVAL_MINUTES[direction_interval(config)])


def direction_enabled(config: dict) -> bool:
    return bool(config.get("DIRECTION_FILTER_ENABLED", False))


def direction_required_bars(config: dict) -> int:
    fast, slow, adx_period, _ = _ema_adx_params(config, "DIRECTION", 20, 50)
    return max(fast, slow, 2 * adx_period + 1)


def direction_window_bars(config: dict) -> int:
    zona = direction_zone_window_bars(config)
    if not direction_enabled(config):
        return zona
    diminta = int(config.get("DIRECTION_LOOKBACK_BARS", 60) or 60)
    diminta = max(1, min(TREND_KLINE_LIMIT - 1, diminta))
    return max(direction_required_bars(config), diminta, zona)


def direction_zone_enabled(config: dict) -> bool:
    return bool(config.get("DIRECTION_ZONE_FILTER_ENABLED", False))


def direction_zone_lookback_bars(config: dict) -> int:
    lookback = int(config.get("DIRECTION_ZONE_LOOKBACK_BARS", 20) or 20)
    return max(3, min(TREND_KLINE_LIMIT - 2, lookback))


def direction_zone_window_bars(config: dict) -> int:
    if not direction_zone_enabled(config):
        return 0
    return direction_zone_lookback_bars(config) + 1


def direction_zone_gate_config(config: dict) -> dict:
    return {
        "DEMAND_ZONE_FILTER_ENABLED": direction_zone_enabled(config),
        "DEMAND_LOOKBACK_BARS": direction_zone_lookback_bars(config),
        "DEMAND_ZONE_BUFFER_PCT": max(
            0.0, float(config.get("DIRECTION_ZONE_BUFFER_PCT", 2.0) or 0.0)
        ),
        "DEMAND_MAX_DISTANCE_PCT": max(
            0.0, float(config.get("DIRECTION_ZONE_MAX_DISTANCE_PCT", 5.0) or 0.0)
        ),
        "DEMAND_REQUIRE_INSIDE_ZONE": bool(
            config.get("DIRECTION_ZONE_REQUIRE_INSIDE_ZONE", False)
        ),
        "DEMAND_MIN_CLOSE_POSITION": float(
            config.get("DIRECTION_ZONE_MIN_CLOSE_POSITION", 0.35) or 0.0
        ),
    }


def evaluate_direction_zone(
    klines: list[Kline],
    config: dict,
    signal_close_time_ms: Optional[int] = None,
) -> dict:
    interval = direction_interval(config)
    wajib = direction_zone_window_bars(config)
    if not direction_zone_enabled(config):
        return {
            "ok": True,
            "reason": f"filter zona demand {interval} nonaktif",
            "interval": interval,
            "bars": 0,
            "required": 0,
            "zone_low": None,
            "zone_high": None,
            "distance_pct": None,
            "close_position": None,
        }
    siap = [
        k
        for k in (klines or [])
        if signal_close_time_ms is None
        or int(k.close_time) <= int(signal_close_time_ms)
    ]
    jendela = siap[-wajib:] if wajib > 0 else siap
    hasil = detect_demand_zone(jendela, direction_zone_gate_config(config))
    hasil["interval"] = interval
    hasil["bars"] = len(jendela)
    hasil["required"] = wajib
    return hasil


def zone_interval(config: dict) -> str:
    return _interval_from_config(config, "ZONE_INTERVAL", ZONE_DEFAULT_INTERVAL)


def zone_interval_minutes(config: dict) -> int:
    return int(INTERVAL_MINUTES[zone_interval(config)])


def zone_enabled(config: dict) -> bool:
    return bool(config.get("ZONE_FILTER_ENABLED", False))


def zone_lookback_bars(config: dict) -> int:
    lookback = int(config.get("ZONE_LOOKBACK_BARS", 72) or 72)
    return max(3, min(TREND_KLINE_LIMIT - 2, lookback))


def zone_demand_window_bars(config: dict) -> int:
    if not zone_enabled(config):
        return 0
    return zone_lookback_bars(config) + 1


def zone_trend_required_bars(config: dict) -> int:
    if not zone_enabled(config):
        return 0
    fast, slow, adx_period, _ = _ema_adx_params(config, "ZONE", 20, 50)
    return max(fast, slow, 2 * adx_period + 1)


def zone_trend_window_bars(config: dict) -> int:
    if not zone_enabled(config):
        return 0
    requested = int(config.get("ZONE_TREND_LOOKBACK_BARS", 60) or 60)
    requested = max(1, min(TREND_KLINE_LIMIT - 1, requested))
    return max(zone_trend_required_bars(config), requested)


def zone_window_bars(config: dict) -> int:
    if not zone_enabled(config):
        return 0
    return max(zone_demand_window_bars(config), zone_trend_window_bars(config))


def zone_gate_config(config: dict) -> dict:
    return {
        "DEMAND_ZONE_FILTER_ENABLED": zone_enabled(config),
        "DEMAND_LOOKBACK_BARS": zone_lookback_bars(config),
        "DEMAND_ZONE_BUFFER_PCT": max(
            0.0, float(config.get("ZONE_BUFFER_PCT", 1.5) or 0.0)
        ),
        "DEMAND_MAX_DISTANCE_PCT": max(
            0.0, float(config.get("ZONE_MAX_DISTANCE_PCT", 4.0) or 0.0)
        ),
        "DEMAND_REQUIRE_INSIDE_ZONE": bool(
            config.get("ZONE_REQUIRE_INSIDE_ZONE", False)
        ),
        "DEMAND_MIN_CLOSE_POSITION": float(
            config.get("ZONE_MIN_CLOSE_POSITION", 0.35) or 0.0
        ),
    }


def timing_interval(config: dict) -> str:
    return _interval_from_config(config, "CONFIRM_INTERVAL", TIMING_DEFAULT_INTERVAL)


def timing_interval_minutes(config: dict) -> int:
    return int(INTERVAL_MINUTES[timing_interval(config)])


def timing_enabled(config: dict) -> bool:
    return bool(config.get("TIMING_FILTER_ENABLED", False))


def timing_required_bars(config: dict) -> int:
    if not timing_enabled(config):
        return 0
    fast, slow, adx_period, _ = _ema_adx_params(config, "TIMING", 20, 50)
    return max(fast, slow, 2 * adx_period + 1)


def timing_window_bars(config: dict) -> int:
    if not timing_enabled(config):
        return 0
    diminta = int(config.get("TIMING_LOOKBACK_BARS", 60) or 60)
    diminta = max(1, min(TREND_KLINE_LIMIT - 1, diminta))
    return max(timing_required_bars(config), diminta)


def aggregate_klines(
    klines: list[Kline], target_minutes: int, source_minutes: Optional[int] = None
) -> list[Kline]:
    target_ms = int(target_minutes) * 60_000
    if source_minutes is None:
        if len(klines) >= 2:
            source_minutes = max(
                1, (int(klines[1].open_time) - int(klines[0].open_time)) // 60_000
            )
        else:
            source_minutes = int(target_minutes)
    source_ms = int(source_minutes) * 60_000
    if source_ms <= 0 or target_ms % source_ms:
        raise ValueError(
            f"Interval sumber {source_minutes} menit tidak bisa dirangkai ke "
            f"{target_minutes} menit (harus pembagi bulat)."
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


def ema_series(values: list[float], period: int) -> list[Optional[float]]:
    period = int(period)
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    try:
        seed = sum(float(v) for v in values[:period]) / period
    except (TypeError, ValueError):
        return out
    if not math.isfinite(seed):
        return out
    out[period - 1] = seed
    faktor = 2.0 / (period + 1.0)
    prev = seed
    for i in range(period, n):
        nilai = float(values[i])
        if not math.isfinite(nilai):
            return out
        prev = nilai * faktor + prev * (1.0 - faktor)
        if not math.isfinite(prev):
            return out
        out[i] = prev
    return out


def adx_series(klines: list[Kline], period: int = 14) -> list[Optional[float]]:
    period = int(period)
    n = len(klines)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < 2 * period:
        return out

    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr[0] = float(klines[0].high) - float(klines[0].low)
    for i in range(1, n):
        high = float(klines[i].high)
        low = float(klines[i].low)
        prev_high = float(klines[i - 1].high)
        prev_low = float(klines[i - 1].low)
        prev_close = float(klines[i - 1].close)
        if not all(
            math.isfinite(v) for v in (high, low, prev_high, prev_low, prev_close)
        ):
            return out
        naik = high - prev_high
        turun = prev_low - low
        if naik > turun and naik > 0:
            plus_dm[i] = naik
        if turun > naik and turun > 0:
            minus_dm[i] = turun
        tr[i] = max(high - low, abs(high - prev_close), abs(low - prev_close))

    def _dx(
        tr_smooth: float, plus_smooth: float, minus_smooth: float
    ) -> Optional[float]:
        if tr_smooth <= 0 or not math.isfinite(tr_smooth):
            return None
        di_plus = 100.0 * plus_smooth / tr_smooth
        di_minus = 100.0 * minus_smooth / tr_smooth
        jumlah = di_plus + di_minus
        if jumlah <= 0:
            return 0.0
        return 100.0 * abs(di_plus - di_minus) / jumlah

    tr_smooth = sum(tr[1 : period + 1])
    plus_smooth = sum(plus_dm[1 : period + 1])
    minus_smooth = sum(minus_dm[1 : period + 1])

    dx: list[Optional[float]] = [None] * n
    dx[period] = _dx(tr_smooth, plus_smooth, minus_smooth)
    for i in range(period + 1, n):
        tr_smooth = tr_smooth - tr_smooth / period + tr[i]
        plus_smooth = plus_smooth - plus_smooth / period + plus_dm[i]
        minus_smooth = minus_smooth - minus_smooth / period + minus_dm[i]
        dx[i] = _dx(tr_smooth, plus_smooth, minus_smooth)

    seed_start = 2 * period - 1
    if n <= seed_start:
        return out
    jendela = [dx[i] for i in range(period, seed_start + 1)]
    if any(v is None for v in jendela):
        return out
    adx = sum(float(v) for v in jendela) / period
    out[seed_start] = adx
    for i in range(seed_start + 1, n):
        nilai = dx[i]
        if nilai is None:
            return out
        adx = (adx * (period - 1) + float(nilai)) / period
        out[i] = adx
    return out


def evaluate_ema_adx(
    klines: list[Kline],
    *,
    interval: str,
    fast: int,
    slow: int,
    adx_period: int,
    adx_min: float,
    window: int,
    required: int,
    label: str,
    nama_kunci_jendela: str,
    signal_close_time_ms: Optional[int] = None,
) -> dict:
    if interval not in INTERVAL_MINUTES:
        return {
            "ok": False,
            "reason": (
                f"Interval '{interval}' tidak dikenal; pilihan: "
                + ", ".join(sorted(INTERVAL_MINUTES, key=INTERVAL_MINUTES.get))
            ),
            "interval": interval,
            "bars": 0,
            "required": required,
            "window": window,
            "close": None,
            "ema_fast": None,
            "ema_slow": None,
            "adx": None,
            "checks": {},
            "values": {},
        }

    jendela_penuh = max(1, int(window))
    minimum = max(1, int(required))

    siap = [
        k
        for k in (klines or [])
        if signal_close_time_ms is None
        or int(k.close_time) <= int(signal_close_time_ms)
    ]
    jendela = siap[-jendela_penuh:]

    kosong = {
        "interval": interval,
        "bars": len(jendela),
        "required": minimum,
        "window": jendela_penuh,
        "close": None,
        "ema_fast": None,
        "ema_slow": None,
        "adx": None,
        "checks": {},
        "values": {},
    }
    if len(jendela) < minimum:
        return {
            "ok": False,
            "reason": (
                f"data candle {interval} kurang: {len(jendela)} dari minimum "
                f"{minimum} (jendela {jendela_penuh}; naikkan {nama_kunci_jendela} "
                "atau tunggu riwayat bertambah)"
            ),
            **kosong,
        }

    for candle in jendela:
        if (
            not math.isfinite(float(candle.close))
            or float(candle.close) <= 0
            or float(candle.high) < float(candle.low)
        ):
            return {"ok": False, "reason": f"candle {interval} tidak valid", **kosong}

    closes = [float(k.close) for k in jendela]
    ema_fast_series = ema_series(closes, fast)
    ema_slow_series = ema_series(closes, slow)
    adx_values = (
        adx_series(jendela, adx_period) if adx_min > 0 else [None] * len(jendela)
    )
    ema_fast = ema_fast_series[-1]
    ema_slow = ema_slow_series[-1]
    adx_now = adx_values[-1]
    close_now = closes[-1]

    terisi = {
        "interval": interval,
        "bars": len(jendela),
        "required": minimum,
        "window": jendela_penuh,
        "close": close_now,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "adx": adx_now,
        "checks": {},
        "values": {
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "adx": adx_now,
            "close": close_now,
        },
    }

    if ema_fast is None or ema_slow is None:
        return {
            "ok": False,
            "reason": (
                f"EMA {interval} belum terdefinisi pada jendela "
                f"{len(jendela)} candle (butuh EMA cepat {fast} dan "
                f"EMA lambat {slow})"
            ),
            **terisi,
        }

    if adx_min > 0 and adx_now is None:
        return {
            "ok": False,
            "reason": (
                f"ADX {adx_period} {interval} tidak bisa dihitung dari "
                "jendela ini (rentang harga nol atau data rusak)"
            ),
            **terisi,
        }

    harga_di_atas = close_now > float(ema_fast)
    susunan_naik = float(ema_fast) > float(ema_slow)
    trend_kuat = True if adx_min <= 0 else bool(float(adx_now) >= adx_min)

    checks = {
        "harga_di_atas_ema_cepat": harga_di_atas,
        "ema_cepat_di_atas_ema_lambat": susunan_naik,
        "adx_di_atas_ambang": trend_kuat,
    }
    ringkas = (
        f"close {close_now:.6g} vs EMA{fast} {float(ema_fast):.6g} vs "
        f"EMA{slow} {float(ema_slow):.6g}"
        + (f", ADX{adx_period} {float(adx_now):.1f}" if adx_now is not None else "")
    )

    if not harga_di_atas:
        return {
            "ok": False,
            "reason": (
                f"{label} {interval} turun: close {close_now:.6g} di bawah "
                f"EMA{fast} {float(ema_fast):.6g} ({ringkas})"
            ),
            **{**terisi, "checks": checks},
        }
    if not susunan_naik:
        return {
            "ok": False,
            "reason": (
                f"struktur {label} {interval} belum naik: EMA{fast} "
                f"{float(ema_fast):.6g} di bawah EMA{slow} "
                f"{float(ema_slow):.6g} ({ringkas})"
            ),
            **{**terisi, "checks": checks},
        }
    if not trend_kuat:
        return {
            "ok": False,
            "reason": (
                f"{label} {interval} lemah: ADX{adx_period} "
                f"{float(adx_now):.1f} di bawah ambang {adx_min:g} ({ringkas})"
            ),
            **{**terisi, "checks": checks},
        }

    return {
        "ok": True,
        "reason": f"{label} {interval} naik dan kuat ({ringkas})",
        **{**terisi, "checks": checks},
    }


def evaluate_direction(
    klines: list[Kline],
    config: dict,
    signal_close_time_ms: Optional[int] = None,
) -> dict:
    """Layer arah: tren utama dibaca dari candle Daily."""
    interval = direction_interval(config)
    if not direction_enabled(config):
        return {
            "ok": True,
            "reason": f"filter arah {interval} nonaktif",
            "interval": interval,
            "bars": 0,
            "required": 0,
            "window": 0,
            "close": None,
            "ema_fast": None,
            "ema_slow": None,
            "adx": None,
            "checks": {},
            "values": {},
        }
    fast, slow, adx_period, adx_min = _ema_adx_params(config, "DIRECTION", 20, 50)
    return evaluate_ema_adx(
        klines,
        interval=interval,
        fast=fast,
        slow=slow,
        adx_period=adx_period,
        adx_min=adx_min,
        window=direction_window_bars(config),
        required=direction_required_bars(config),
        label="arah",
        nama_kunci_jendela="DIRECTION_LOOKBACK_BARS",
        signal_close_time_ms=signal_close_time_ms,
    )


def evaluate_zone(
    klines: list[Kline],
    config: dict,
    signal_close_time_ms: Optional[int] = None,
) -> dict:
    interval = zone_interval(config)
    required = zone_window_bars(config)
    if not zone_enabled(config):
        return {
            "ok": True,
            "reason": f"filter zona dan tren {interval} nonaktif",
            "interval": interval,
            "bars": 0,
            "required": 0,
            "zone_low": None,
            "zone_high": None,
            "distance_pct": None,
            "close_position": None,
            "close": None,
            "ema_fast": None,
            "ema_slow": None,
            "adx": None,
            "checks": {},
            "values": {},
            "trend": None,
        }
    available = [
        candle
        for candle in (klines or [])
        if signal_close_time_ms is None
        or int(candle.close_time) <= int(signal_close_time_ms)
    ]
    window = available[-required:] if required > 0 else available
    demand_required = zone_demand_window_bars(config)
    demand_window = window[-demand_required:] if demand_required > 0 else window
    demand = detect_demand_zone(demand_window, zone_gate_config(config))
    fast, slow, adx_period, adx_min = _ema_adx_params(config, "ZONE", 20, 50)
    trend = evaluate_ema_adx(
        window,
        interval=interval,
        fast=fast,
        slow=slow,
        adx_period=adx_period,
        adx_min=adx_min,
        window=zone_trend_window_bars(config),
        required=zone_trend_required_bars(config),
        label="tren zona",
        nama_kunci_jendela="ZONE_TREND_LOOKBACK_BARS",
    )
    reasons = []
    if not demand["ok"]:
        reasons.append(f"demand ditolak: {demand['reason']}")
    if not trend["ok"]:
        reasons.append(f"tren ditolak: {trend['reason']}")
    if not reasons:
        reasons.append(f"{demand['reason']} | {trend['reason']}")
    return {
        **demand,
        "ok": bool(demand["ok"] and trend["ok"]),
        "reason": " | ".join(reasons),
        "interval": interval,
        "bars": len(window),
        "required": required,
        "demand_bars": len(demand_window),
        "demand_required": demand_required,
        "close": trend.get("close"),
        "ema_fast": trend.get("ema_fast"),
        "ema_slow": trend.get("ema_slow"),
        "adx": trend.get("adx"),
        "checks": trend.get("checks") or {},
        "values": trend.get("values") or {},
        "trend": trend,
    }


def evaluate_timing(
    klines: list[Kline],
    config: dict,
    signal_close_time_ms: Optional[int] = None,
) -> dict:
    """Layer timing: tren mikro dibaca dari candle konfirmasi (M5)."""
    interval = timing_interval(config)
    if not timing_enabled(config):
        return {
            "ok": True,
            "reason": f"filter timing {interval} nonaktif",
            "interval": interval,
            "bars": 0,
            "required": 0,
            "window": 0,
            "close": None,
            "ema_fast": None,
            "ema_slow": None,
            "adx": None,
            "checks": {},
            "values": {},
        }
    fast, slow, adx_period, adx_min = _ema_adx_params(config, "TIMING", 20, 50)
    return evaluate_ema_adx(
        klines,
        interval=interval,
        fast=fast,
        slow=slow,
        adx_period=adx_period,
        adx_min=adx_min,
        window=timing_window_bars(config),
        required=timing_required_bars(config),
        label="timing",
        nama_kunci_jendela="TIMING_LOOKBACK_BARS",
        signal_close_time_ms=signal_close_time_ms,
    )


def direction_warmup_bars(config: dict, interval: str) -> int:
    """Bar sumber yang perlu dirangkai jadi candle Daily saat agregasi.

    Jalur native (candle harian diambil langsung dari bursa) tidak memakai
    angka ini karena candle harian tidak bergantung pada bar simulasi.
    """
    if not direction_enabled(config) and not direction_zone_enabled(config):
        return 0
    source_minutes = int(INTERVAL_MINUTES.get(str(interval), 5))
    target_minutes = direction_interval_minutes(config)
    if target_minutes % source_minutes:
        raise ValueError(
            f"DIRECTION_INTERVAL '{direction_interval(config)}' ({target_minutes} "
            f"menit) harus kelipatan bulat dari interval simulasi '{interval}' "
            f"({source_minutes} menit) supaya backtest bisa merangkai candle "
            "harian dari data yang sudah diunduh."
        )
    rasio = target_minutes // source_minutes
    return (direction_window_bars(config) + 1) * rasio + rasio


def zone_warmup_bars(config: dict, interval: str) -> int:
    """Bar sumber yang perlu dirangkai jadi candle H1 saat agregasi."""
    if not zone_enabled(config):
        return 0
    source_minutes = int(INTERVAL_MINUTES.get(str(interval), 5))
    target_minutes = zone_interval_minutes(config)
    if target_minutes % source_minutes:
        raise ValueError(
            f"ZONE_INTERVAL '{zone_interval(config)}' ({target_minutes} menit) "
            f"harus kelipatan bulat dari interval simulasi '{interval}' "
            f"({source_minutes} menit) supaya backtest bisa merangkai candle "
            "H1 dari data yang sudah diunduh."
        )
    rasio = target_minutes // source_minutes
    return (zone_window_bars(config) + 1) * rasio + rasio


def gate_warmup_bars(config: dict, interval: str) -> int:
    """Total warmup gerbang timeframe tinggi pada jalur agregasi."""
    return max(direction_warmup_bars(config, interval), zone_warmup_bars(config, interval))


def native_history_ms(config: dict) -> int:
    """Riwayat tambahan yang perlu diambil untuk candle timeframe tinggi native.

    Backtest yang mengambil candle Daily dan H1 langsung dari bursa memakai
    angka ini untuk menarik cukup candle historis sebelum rentang uji.
    """
    besoin = 0
    if direction_enabled(config) or direction_zone_enabled(config):
        besoin = max(
            besoin,
            direction_window_bars(config)
            * INTERVAL_MINUTES[direction_interval(config)]
            * 60_000,
        )
    if zone_enabled(config):
        besoin = max(
            besoin,
            zone_window_bars(config) * INTERVAL_MINUTES[zone_interval(config)] * 60_000,
        )
    return int(besoin)
