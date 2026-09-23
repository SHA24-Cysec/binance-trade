"""
Struktur data candle (Kline) dan parser hasil endpoint klines Binance.

Semua array di sini memakai urutan KRONOLOGIS (index 0 = candle paling lama,
index -1 = candle paling baru), sesuai perilaku default endpoint
GET /api/v3/klines.

Catatan: modul ini dulu juga berisi indikator SuperTrend + EMA + ATR untuk bot
grid martingale. Bot grid sudah dihapus, jadi modul ini kini hanya menyisakan
struktur candle dan parser yang masih dipakai oleh pump scanner.
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
    # Dua field di bawah ditambahkan untuk kebutuhan fitur backtest (menghitung
    # volume 24 jam bergulir tanpa perlu memanggil ulang ticker/24hr per-bar).
    # Diberi nilai default 0.0 supaya SEMUA pemanggilan lama (mis. selftest di
    # pump_scanner_bot.py yang membuat Kline manual tanpa argumen ini) tetap
    # berjalan tanpa perlu diubah.
    volume: float = 0.0
    quote_volume: float = 0.0


def parse_klines(raw: list) -> list[Kline]:
    """raw = hasil GET /api/v3/klines (list of list), urutan sudah kronologis
    (paling lama -> paling baru) sesuai perilaku default endpoint tsb.

    Indeks array mentah (sesuai dokumentasi resmi Binance):
    0=openTime 1=open 2=high 3=low 4=close 5=volume 6=closeTime
    7=quoteAssetVolume ..."""
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
                volume=float(row[5]) if len(row) > 5 else 0.0,
                quote_volume=float(row[7]) if len(row) > 7 else 0.0,
            )
        )
    return out
