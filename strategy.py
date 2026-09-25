"""
Struktur data candle (Kline), parser klines Binance, indikator momentum,
ukuran jendela bersama, sizing posisi, dan resolusi level exit ATR.

Semua array di sini memakai urutan KRONOLOGIS (index 0 = candle paling lama,
index -1 = candle paling baru), sesuai perilaku default endpoint
GET /api/v3/klines.

Referensi endpoint yang dipakai modul ini:
  https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market#klines
  (dicek 2026-09-25). Respons klines berupa tuple 12 field dengan indeks
  0=openTime, 1=open, 2=high, 3=low, 4=close, 5=volume, 6=closeTime,
  7=quoteAssetVolume. Bobot request 2 per panggilan, limit maksimum 1000
  candle per panggilan.
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
# Kebutuhan jumlah candle untuk satu keputusan entry
# ---------------------------------------------------------------------
def required_lookback_bars(config: dict) -> int:
    """Jumlah candle minimum untuk indikator momentum dan volume rolling.

    EMA, RSI, MACD, ATR, pivot low, serta rata-rata volume rolling hanya
    boleh memakai candle yang sudah close. Angka ini dipakai bersama oleh
    bot live, backtest, dan watchlist agar jendelanya konsisten.
    """
    rolling_lookback = int(config.get("ROLLING_VOLUME_LOOKBACK_BARS", 20) or 20)
    confirmation_bars = int(config.get("ROLLING_VOLUME_CONFIRMATION_BARS", 1) or 1)
    return max(30, rolling_lookback + max(1, confirmation_bars))


def confirm_window_bars(config: dict) -> int:
    """Jumlah candle tertutup yang harus diambil untuk satu keputusan entry.

    Sama dengan CONFIRM_LOOKBACK_BARS, tetapi tidak pernah lebih kecil dari
    required_lookback_bars(). Ini yang dipakai pengambil klines di bot live,
    dashboard, dan watchlist supaya ketiganya melihat jendela yang identik.
    """
    lookback = int(config.get("CONFIRM_LOOKBACK_BARS", 48) or 48)
    return max(1, min(1000, max(lookback, required_lookback_bars(config))))


def resolve_position_notional(config: dict, quote_free: float) -> dict:
    """Tentukan nominal entry dari saldo quote dengan policy yang dipakai live."""
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


def ema(closes: list[float], period: int) -> list[float]:
    """Hitung EMA kronologis dan mengembalikan seluruh deret EMA.

    Candle pada indeks 0 adalah candle paling lama. Nilai awal memakai close
    pertama, sehingga tidak ada data masa depan yang masuk ke perhitungan.
    """
    period = int(period)
    values = [float(x) for x in closes]
    if period <= 0:
        raise ValueError("period EMA harus lebih besar dari nol")
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def rsi(closes: list[float], period: int = 14) -> list[float]:
    """Hitung RSI Wilder kronologis; nilai yang belum matang bernilai 50.0."""
    period = int(period)
    values = [float(x) for x in closes]
    if period <= 0:
        raise ValueError("period RSI harus lebih besar dari nol")
    if not values:
        return []
    out = [50.0] * len(values)
    if len(values) <= period:
        return out
    gains = [max(0.0, values[i] - values[i - 1]) for i in range(1, len(values))]
    losses = [max(0.0, values[i - 1] - values[i]) for i in range(1, len(values))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    def value():
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    out[period] = value()
    for i in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = value()
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[list[float], list[float], list[float]]:
    """Hitung MACD line, signal line, dan histogram secara kronologis."""
    fast_line = ema(closes, fast)
    slow_line = ema(closes, slow)
    line = [a - b for a, b in zip(fast_line, slow_line)]
    signal_line = ema(line, signal)
    histogram = [a - b for a, b in zip(line, signal_line)]
    return line, signal_line, histogram


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
    Nama field lama dipertahankan agar state, backtest, dan dashboard tidak
    perlu mengubah kontrak penyimpanan. ``atr_value`` dan ``entry_price``
    opsional: tanpa keduanya fungsi mengembalikan multiplier ATR sebagai
    jarak unit, yang kemudian dikalikan ATR saat entry.
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
