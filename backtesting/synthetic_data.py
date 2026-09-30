"""Generator candle sintetis deterministik untuk tes data dan pipeline."""

from __future__ import annotations

from strategy.indicators import Kline

MS_PER_BAR = 300_000


def make_candle(index: int, open_: float, high: float, low: float, close: float,
                volume: float = 1_000.0, bar_ms: int = MS_PER_BAR) -> Kline:
    harga_rata = (high + low + close) / 3.0
    return Kline(
        open_time=index * bar_ms,
        open=float(open_),
        high=float(high),
        low=float(low),
        close=float(close),
        close_time=index * bar_ms + bar_ms - 1,
        volume=float(volume),
        quote_volume=float(volume) * harga_rata,
    )


def seri_data(harga: float = 100.0, bars: int = 400,
              volume: float = 5_000_000.0, mulai_index: int = 0) -> list[Kline]:
    """Deret candle stabil dengan variasi kecil untuk pengujian data."""
    out = []
    for offset in range(max(1, bars)):
        price = harga * (1.0 + 0.001 * ((offset % 7) - 3))
        out.append(make_candle(mulai_index + offset, price, price * 1.002,
                               price * 0.998, price, volume))
    return out


def riwayat_harian(klines: list[Kline], hari: int = 7,
                   quote_volume_harian: float = 1_000_000.0,
                   harga: float = 1.0) -> list[Kline]:
    """Candle harian tertutup sebelum candle pertama deret utama."""
    ms_per_day = 86_400_000
    mulai = int(klines[0].open_time) if klines else 0
    out: list[Kline] = []
    for n in range(hari, 0, -1):
        open_time = mulai - n * ms_per_day
        out.append(Kline(
            open_time=open_time,
            open=harga, high=harga, low=harga, close=harga,
            close_time=open_time + ms_per_day - 1,
            volume=quote_volume_harian / max(harga, 1e-9),
            quote_volume=float(quote_volume_harian),
        ))
    return out
