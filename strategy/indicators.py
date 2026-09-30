"""
Struktur candle, parser klines, interval pasar, ATR, dan level exit.
Modul ini tidak menghitung metrik pembukaan posisi.

Array candle memakai urutan kronologis, index 0 adalah candle paling lama.
"""

from __future__ import annotations

import math
from typing import NamedTuple


class Kline(NamedTuple):
    open_time: int
    open: float
    high: float
    low: float
    close: float
    close_time: int
    # Dua field di bawah ditambahkan untuk kebutuhan fitur backtest, yaitu
    # menghitung volume 24 jam bergulir tanpa perlu memanggil ulang ticker/24hr
    # per bar. Nilai default 0.0 menjaga pemanggilan lama tetap berjalan.
    volume: float = 0.0
    quote_volume: float = 0.0


# Panjang interval candle dalam menit. Dipakai bersama oleh bot live,
# backtest satu simbol, dan backtest portofolio supaya tidak ada dua tabel
# yang bisa berbeda diam-diam. Nilai enum interval mengikuti dokumentasi
# endpoint klines Binance.
INTERVAL_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480, "12h": 720,
    "1d": 1440,
}


def interval_to_ms(interval: str, default_minutes: int = 5) -> int:
    """Panjang satu candle dalam milidetik, jatuh ke default bila tidak dikenal."""
    return int(INTERVAL_MINUTES.get(str(interval), default_minutes)) * 60_000


def parse_klines(raw: list) -> list[Kline]:
    """raw = hasil GET /api/v3/klines, urutan kronologis.

    Indeks array mentah sesuai dokumentasi resmi Binance:
    0=openTime 1=open 2=high 3=low 4=close 5=volume 6=closeTime
    7=quoteAssetVolume ...
    """
    out = []
    for idx, row in enumerate(raw):
        o = float(row[1])
        h = float(row[2])
        low_ = float(row[3])
        c = float(row[4])

        # Penjagaan data rusak. NaN membuat setiap perbandingan harga bernilai
        # False, termasuk cek Stop Loss, jadi data seperti itu wajib ditolak.
        for nama, nilai in (("open", o), ("high", h), ("low", low_), ("close", c)):
            if nilai != nilai:
                raise ValueError(
                    f"Candle ke-{idx} punya {nama}=NaN. Data bursa rusak. "
                    "Dihentikan karena NaN membuat semua cek Stop Loss/Take "
                    "Profit diam-diam gagal."
                )
            if nilai in (float("inf"), float("-inf")):
                raise ValueError(f"Candle ke-{idx} punya {nama} tak hingga. Data bursa rusak.")
            if nilai <= 0:
                raise ValueError(
                    f"Candle ke-{idx} punya {nama}={nilai}. Harga wajib > 0. "
                    "Data bursa rusak atau simbol sudah tidak diperdagangkan."
                )

        if h < low_:
            raise ValueError(f"Candle ke-{idx}: high ({h}) lebih kecil dari low ({low_}).")

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


# ---------------------------------------------------------------------
# Exit calculations
# ---------------------------------------------------------------------
def atr(klines: list[Kline], period: int = 14) -> float | None:
    """Kembalikan ATR Wilder terakhir dari candle yang sudah tertutup."""
    period = int(period)
    if period <= 0 or not klines:
        return None
    trs: list[float] = []
    for i, candle in enumerate(klines):
        prev_close = klines[i - 1].close if i else candle.close
        trs.append(max(candle.high - candle.low,
                       abs(candle.high - prev_close),
                       abs(candle.low - prev_close)))
    if len(trs) < period:
        return None
    result = sum(trs[:period]) / period
    for tr in trs[period:]:
        result = (result * (period - 1) + tr) / period
    return result if math.isfinite(result) and result > 0 else None


def resolve_exit_levels(config: dict) -> dict:
    """Tentukan level exit ATR atau fallback persen lama.

    Pada mode ATR, nilai jarak dikembalikan sebagai jarak harga absolut.
    Field level dipertahankan agar state, backtest, dan dashboard tetap
    kompatibel. Nilai ATR opsional untuk posisi yang sudah ada.
    """
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
        return {"sl_pct": sl * scale, "tp_pct": tp * scale,
                "be_trigger_pct": be_trigger * scale, "be_lock_pct": be_lock * scale,
                "trail_start_pct": trail_start * scale, "trail_step_pct": trail * scale,
                "atr_period": period, "atr_value": atr_value,
                "atr_mult_sl": sl, "atr_mult_tp": tp, "atr_mult_trail": trail,
                "source": "ATR", "note": "Level exit berbasis ATR; invariant diterapkan"}

    fixed_sl = abs(float(config.get("SL_PCT", 1.8)))
    fixed_tp = abs(float(config.get("TP_PCT", 4.0)))
    fixed_be_trigger = abs(float(config.get("BE_TRIGGER_PCT", 1.0)))
    fixed_be_lock = abs(float(config.get("BE_LOCK_PCT", 0.15)))
    fixed_trail_start = abs(float(config.get("TRAILING_START_PCT", 1.5)))
    fixed_trail_step = abs(float(config.get("TRAILING_STEP_PCT", 0.6)))
    fixed_trail_step = min(fixed_trail_step, fixed_sl)
    fixed_be_trigger = min(fixed_be_trigger, fixed_trail_start)
    fixed_be_lock = min(fixed_be_lock, fixed_be_trigger)
    return {"sl_pct": fixed_sl, "tp_pct": fixed_tp,
            "be_trigger_pct": fixed_be_trigger, "be_lock_pct": fixed_be_lock,
            "trail_start_pct": fixed_trail_start, "trail_step_pct": fixed_trail_step,
            "source": "FIXED", "note": "Level exit tetap dari config; invariant diterapkan"}
