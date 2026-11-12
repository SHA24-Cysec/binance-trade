from __future__ import annotations

from strategy.indicators import Kline

MS_PER_BAR = 300_000
VOLUME_SPIKE_MULT = 3.0


def make_candle(
    index: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 1_000.0,
    bar_ms: int = MS_PER_BAR,
) -> Kline:
    harga_rata = (high + low + close) / 3.0
    return Kline(
        open_time=index * bar_ms,
        open=float(open_),
        high=float(high),
        low=float(low),
        close=float(close),
        close_time=index * bar_ms + (bar_ms - 1),
        volume=float(volume),
        quote_volume=float(volume) * harga_rata,
    )


MS_PER_BAR_15M = 900_000
BAR_PER_H4_15M = 16


def _tambah_15m(out: list[Kline], i: int, open_: float, close: float, low: float, high: float, volume: float) -> int:
    out.append(make_candle(i, open_, high, low, close, volume, bar_ms=MS_PER_BAR_15M))
    return i + 1


def blok_zona_demand(harga: float, mulai_index: int, volume: float = 5_000_000.0) -> tuple[list[Kline], float, int]:
    if mulai_index % BAR_PER_H4_15M != 0:
        raise ValueError("blok zona demand harus mulai tepat di batas candle 4h")
    out: list[Kline] = []
    i = mulai_index
    p = float(harga)
    for _ in range(4):
        i = _tambah_15m(out, i, p, p, p * 0.9997, p * 1.0003, volume)
    harga_kini = p
    for _ in range(4):
        tutup = harga_kini * 1.005
        i = _tambah_15m(out, i, harga_kini, tutup, harga_kini * 0.9995, tutup * 1.0005, volume * VOLUME_SPIKE_MULT)
        harga_kini = tutup
    for _ in range(8):
        i = _tambah_15m(out, i, harga_kini, harga_kini, harga_kini * 0.9995, harga_kini * 1.0005, volume)
    target = p * 1.006
    langkah = (harga_kini - target) / 3.0
    for _ in range(3):
        tutup = harga_kini - langkah
        i = _tambah_15m(out, i, harga_kini, tutup, tutup * 0.9995, harga_kini * 1.0005, volume)
        harga_kini = tutup
    tutup = p * 1.016
    i = _tambah_15m(out, i, harga_kini, tutup, p * 1.0002, tutup * 1.0005, volume * VOLUME_SPIKE_MULT)
    harga_kini = tutup
    for _ in range(12):
        tutup = harga_kini * 1.005
        i = _tambah_15m(out, i, harga_kini, tutup, harga_kini * 0.9995, tutup * 1.0005, volume)
        harga_kini = tutup
    return out, harga_kini, i


def seri_zona_demand(
    harga: float = 100.0,
    siklus: int = 4,
    bar_awal: int = 960,
    volume: float = 5_000_000.0,
    jeda: int = 64,
    mulai_index: int = 0,
) -> list[Kline]:
    if mulai_index % BAR_PER_H4_15M != 0 or bar_awal % BAR_PER_H4_15M != 0 or jeda % BAR_PER_H4_15M != 0:
        raise ValueError("indeks awal, bar awal, dan jeda harus kelipatan 16 candle 15m")
    out: list[Kline] = []
    i = mulai_index
    p = float(harga)

    def naik_pelan(jumlah: int) -> None:
        nonlocal i, p
        for _ in range(jumlah):
            tutup = p * 1.0002
            i = _tambah_15m(out, i, p, tutup, p * 0.9997, tutup * 1.0003, volume)
            p = tutup

    naik_pelan(bar_awal)
    for _ in range(max(1, siklus)):
        blok, p, i = blok_zona_demand(p, i, volume)
        out.extend(blok)
        naik_pelan(jeda)
    return out
