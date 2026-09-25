"""
Struktur data candle (Kline), parser klines Binance, anchored VWAP, ukuran
jendela bersama, sizing posisi, dan resolusi level exit tetap.

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
# Anchored VWAP
# ---------------------------------------------------------------------
def anchored_vwap(klines: "list[Kline]", anchor_index: int) -> "float | None":
    """VWAP yang dijangkar pada satu candle tertentu.

    Rumusnya:
        anchored_vwap = sum(quote_volume[anchor..terakhir]) / sum(volume[anchor..terakhir])

    ``quote_volume`` diambil dari indeks 7 respons klines Binance dan
    ``volume`` dari indeks 5. Pemanggil wajib memperlakukan None sebagai
    setup ditolak, bukan sebagai nol.
    """
    if not klines:
        return None
    n = len(klines)
    if anchor_index < 0 or anchor_index >= n:
        return None

    vol_sum = 0.0
    quote_sum = 0.0
    for k in klines[anchor_index:]:
        vol_sum += float(k.volume)
        quote_sum += float(k.quote_volume)

    if not math.isfinite(vol_sum) or not math.isfinite(quote_sum):
        return None
    if vol_sum <= 0 or quote_sum <= 0:
        return None
    value = quote_sum / vol_sum
    if not math.isfinite(value) or value <= 0:
        return None
    return value


# ---------------------------------------------------------------------
# Kebutuhan jumlah candle untuk satu keputusan entry
# ---------------------------------------------------------------------
def required_lookback_bars(config: dict) -> int:
    """Jumlah candle minimum yang dibutuhkan satu evaluasi pullback retest.

    Struktur setup butuh ruang untuk swing lookback, sayap pivot kiri dan
    kanan, lalu jarak dari breakout sampai retest:
    SWING_LOOKBACK_BARS + 2 x SWING_PIVOT_WING_BARS + MAX_BARS_BREAKOUT_TO_RETEST.
    """
    swing_lookback = int(config.get("SWING_LOOKBACK_BARS", 12) or 12)
    wing = int(config.get("SWING_PIVOT_WING_BARS", 2) or 2)
    max_bars_to_retest = int(config.get("MAX_BARS_BREAKOUT_TO_RETEST", 12) or 12)

    return max(3, swing_lookback + 2 * wing + max_bars_to_retest)


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


def resolve_exit_levels(config: dict) -> dict:
    """Tentukan level exit tetap untuk satu posisi.

    Return dict:
        sl_pct           : float, jarak Stop Loss dalam persen
        tp_pct           : float, jarak Take Profit dalam persen
        be_trigger_pct   : float, profit yang memicu Breakeven
        be_lock_pct      : float, profit yang dikunci saat Breakeven aktif
        trail_start_pct  : float, profit yang mengaktifkan Trailing
        trail_step_pct   : float, jarak Trailing di bawah harga tertinggi
        source           : selalu "FIXED"
        note             : penjelasan singkat untuk log
    """
    fixed_sl = abs(float(config.get("SL_PCT", 1.8)))
    fixed_tp = abs(float(config.get("TP_PCT", 4.0)))
    fixed_be_trigger = abs(float(config.get("BE_TRIGGER_PCT", 1.0)))
    fixed_be_lock = abs(float(config.get("BE_LOCK_PCT", 0.15)))
    fixed_trail_start = abs(float(config.get("TRAILING_START_PCT", 1.5)))
    fixed_trail_step = abs(float(config.get("TRAILING_STEP_PCT", 0.6)))

    # Invariant level exit. Tanpanya BE dapat mengunci harga yang belum pernah
    # disentuh, sedangkan backtest candle bisa mencatat fill profit mustahil.
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
        "note": "Level exit tetap dari config; invariant BE/trailing diterapkan",
    }
