from __future__ import annotations
from config.config import PUMP_CONFIG

from strategy.indicators import Kline

MS_PER_BAR = 300_000
VOLUME_LEMBUT = 0.05
VOLUME_DORONG = 6.0


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


def seri_vidya_flip(
    harga: float = 100.0,
    siklus: int = 4,
    bar_dasar: int = 1000,
    bar_turun: int = 300,
    bar_lanjut: int = 80,
    volume: float = 5_000_000.0,
    volume_dorong: float | None = None,
    mulai_index: int = 0,
    config: dict | None = None,
) -> list[Kline]:
    """Seri sintetis yang memicu flip tren naik VIDYA berulang kali.

    Fase dasar mengisi candle sampai melewati jendela konfirmasi bot supaya
    sinyal pertama bisa dievaluasi. Tiap siklus lalu menurunkan harga pelan
    dengan volume tipis supaya garis VIDYA berbalik turun, kemudian mendorong
    harga naik dengan volume beli dominan sampai indikator mencetak flip naik.
    Jumlah candle dorongan dicari otomatis supaya seri tetap valid bila
    parameter VIDYA diubah.
    """
    from strategy import vidya as vidya_mod

    cfg = dict(config) if config else dict(PUMP_CONFIG)
    out: list[Kline] = []
    i = mulai_index
    p = float(harga)

    def tambah(open_: float, close: float, volume_bar: float) -> None:
        nonlocal i, p
        out.append(make_candle(
            i, open_, max(open_, close) * 1.0008, min(open_, close) * 0.9992,
            close, volume_bar, bar_ms=MS_PER_BAR_15M,
        ))
        p = close
        i += 1

    def flip_terakhir() -> bool:
        if len(out) < 2:
            return False
        seri = vidya_mod.compute_series(out, cfg)
        return bool(seri["cross_up"][-1])

    for k in range(bar_dasar):
        tambah(p, p * (1.0003 if k % 2 == 0 else 0.9997), volume)
    for _ in range(max(1, siklus)):
        for _ in range(bar_turun):
            tambah(p, p * 0.9995, volume * VOLUME_LEMBUT)
        dorong = volume * VOLUME_DORONG if volume_dorong is None else volume_dorong
        for _ in range(80):
            tambah(p, p * 1.004, dorong)
            if flip_terakhir():
                break
        for _ in range(bar_lanjut):
            tambah(p, p * 1.0002, volume)
    return out
