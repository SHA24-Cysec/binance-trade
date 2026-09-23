"""
Logika sinyal, direplikasi seakurat mungkin dari fungsi GetTrendSignal() di
Gold_Grid_Martingale_Pro.mq5: SuperTrend(ATR Wilder, multiplier) + filter
EMA. Semua array di sini memakai urutan KRONOLOGIS (index 0 = candle paling
lama, index -1 = candle paling baru), berbeda dari MQ5 yang memakai array
"series" (index 0 = candle paling baru). Logikanya identik, hanya arah
iterasi yang dibalik menyesuaikan urutan data dari Binance klines.
"""

from __future__ import annotations

from typing import NamedTuple


class Kline(NamedTuple):
    open_time: int
    open: float
    high: float
    low: float
    close: float
    close_time: int


def parse_klines(raw: list) -> list[Kline]:
    """raw = hasil GET /api/v3/klines (list of list), urutan sudah kronologis
    (paling lama -> paling baru) sesuai perilaku default endpoint tsb."""
    out = []
    for row in raw:
        out.append(
            Kline(
                open_time=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                close_time=int(row[6]),
            )
        )
    return out


def compute_ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [float("nan")] * len(values)
    ema = [float("nan")] * len(values)
    k = 2.0 / (period + 1.0)
    sma = sum(values[:period]) / period
    ema[period - 1] = sma
    for i in range(period, len(values)):
        ema[i] = values[i] * k + ema[i - 1] * (1 - k)
    return ema


def compute_atr_wilder(highs: list[float], lows: list[float], closes: list[float],
                        period: int) -> list[float]:
    """ATR dengan smoothing Wilder, sama seperti indikator iATR bawaan MT5."""
    n = len(closes)
    if n == 0:
        return []
    tr = [0.0] * n
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    atr = [float("nan")] * n
    if n < period:
        return atr
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def compute_supertrend_trend(klines: list[Kline], atr: list[float],
                              multiplier: float) -> list[int]:
    """Mengembalikan array trend (+1 / -1) per index, kronologis.
    Replikasi persis logika finalUpper/finalLower/trend dari MQ5, hanya arah
    iterasi dibalik (maju, bukan mundur) karena data sudah kronologis."""
    n = len(klines)
    final_upper = [0.0] * n
    final_lower = [0.0] * n
    trend = [0] * n

    for i in range(n):
        hl2 = (klines[i].high + klines[i].low) / 2.0
        a = atr[i] if atr[i] == atr[i] else 0.0  # NaN guard
        basic_upper = hl2 + multiplier * a
        basic_lower = hl2 - multiplier * a

        if i == 0:
            final_upper[i] = basic_upper
            final_lower[i] = basic_lower
            trend[i] = 1 if klines[i].close >= hl2 else -1
        else:
            prev = i - 1
            if basic_upper < final_upper[prev] or klines[prev].close > final_upper[prev]:
                final_upper[i] = basic_upper
            else:
                final_upper[i] = final_upper[prev]

            if basic_lower > final_lower[prev] or klines[prev].close < final_lower[prev]:
                final_lower[i] = basic_lower
            else:
                final_lower[i] = final_lower[prev]

            if trend[prev] == -1 and klines[i].close > final_upper[prev]:
                trend[i] = 1
            elif trend[prev] == 1 and klines[i].close < final_lower[prev]:
                trend[i] = -1
            else:
                trend[i] = trend[prev]

    return trend


def get_trend_signal(klines: list[Kline], st_atr_period: int, st_multiplier: float,
                      ema_period: int, use_closed_bar: bool) -> int:
    """Mengembalikan 1 (bullish), -1 (bearish), atau 0 (tidak ada sinyal /
    data belum cukup). Index yang dipakai: candle terakhir yang SUDAH CLOSE
    (index -2) jika use_closed_bar=True, atau candle paling baru (index -1,
    bisa jadi belum close) jika False -- persis seperti idx di MQ5."""
    min_bars = max(300, ema_period + st_atr_period + 80)
    if len(klines) < min_bars:
        return 0

    closes = [k.close for k in klines]
    highs = [k.high for k in klines]
    lows = [k.low for k in klines]

    ema = compute_ema(closes, ema_period)
    atr = compute_atr_wilder(highs, lows, closes, st_atr_period)
    trend = compute_supertrend_trend(klines, atr, st_multiplier)

    idx = -2 if use_closed_bar else -1
    if abs(idx) > len(klines):
        return 0
    if ema[idx] != ema[idx] or atr[idx] != atr[idx]:  # NaN guard
        return 0

    if trend[idx] > 0 and closes[idx] > ema[idx]:
        return 1
    if trend[idx] < 0 and closes[idx] < ema[idx]:
        return -1
    return 0


def compute_grid_step_pct(klines: list[Kline], config: dict) -> float:
    """Jarak grid berikutnya dalam PERSEN dari harga, menggantikan konsep
    'points' di EA asli. Dihitung dari ATR/harga jika USE_ATR_GRID=True."""
    if not config["USE_ATR_GRID"]:
        return config["FIXED_GRID_PCT"]

    closes = [k.close for k in klines]
    highs = [k.high for k in klines]
    lows = [k.low for k in klines]
    atr = compute_atr_wilder(highs, lows, closes, config["GRID_ATR_PERIOD"])
    last_atr = atr[-1]
    last_close = closes[-1]
    if last_atr != last_atr or last_close <= 0:  # NaN guard
        return config["GRID_MIN_PCT"]

    raw_pct = (last_atr / last_close) * 100.0 * config["GRID_ATR_MULTIPLIER"]
    return max(config["GRID_MIN_PCT"], min(config["GRID_MAX_PCT"], raw_pct))
