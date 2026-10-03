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
        close_time=index * bar_ms + (bar_ms - 1),
        volume=float(volume),
        quote_volume=float(volume) * harga_rata,
    )


def seri_dengan_setup(harga: float = 1.0, bar_datar: int = 288, volume: float = 5_000_000.0,
                      ekor: str = "naik", panjang_ekor: int = 20,
                      mulai_index: int = 0) -> list[Kline]:
    out: list[Kline] = []
    i = mulai_index
    for _ in range(bar_datar):
        out.append(make_candle(i, harga, harga * 1.002, harga * 0.998, harga * 1.0005, volume))
        i += 1

    blok, harga_akhir, i = blok_setup(harga, i, volume)
    out.extend(blok)

    harga_kini = harga_akhir
    for _ in range(panjang_ekor):
        if ekor == "naik":
            harga_kini *= 1.01
            out.append(make_candle(i, harga_kini / 1.01, harga_kini * 1.002, harga_kini / 1.011,
                                   harga_kini, volume))
        elif ekor == "invalidasi":
            harga_kini *= 0.99
            out.append(make_candle(i, harga_kini / 0.99, harga_kini * 1.001, harga_kini * 0.998,
                                   harga_kini, volume))
        else:
            out.append(make_candle(i, harga_kini, harga_kini * 1.001, harga_kini * 0.999,
                                   harga_kini, volume))
        i += 1
    return out


def blok_setup(harga: float, mulai_index: int, volume: float = 5_000_000.0):
    out: list[Kline] = []
    i = mulai_index
    for j in range(12):
        o = harga
        h = harga * 1.004
        low = harga * 0.996
        c = harga * 1.001
        if j == 6:
            h = harga * 1.012
            c = harga * 1.006
        out.append(make_candle(i, o, h, low, c, volume))
        i += 1

    level = harga * 1.012
    out.append(make_candle(i, harga * 1.002, level * 1.013, harga * 0.999, level * 1.012, volume))
    i += 1
    out.append(make_candle(i, level * 1.012, level * 1.014, level * 1.006, level * 1.008, volume))
    i += 1
    out.append(make_candle(i, level * 1.008, level * 1.010, level * 1.004, level * 1.006, volume))
    i += 1
    out.append(make_candle(i, level * 1.006, level * 1.008, level * 1.002, level * 1.004, volume))
    i += 1
    out.append(make_candle(i, level * 1.001, level * 1.009, level * 0.997, level * 1.008, volume))
    i += 1
    return out, level * 1.008, i


def seri_banyak_setup(harga: float = 100.0, siklus: int = 10, bar_datar: int = 288,
                      volume: float = 5_000_000.0, naik: int = 6, turun: int = 20,
                      mulai_index: int = 0) -> list[Kline]:
    out: list[Kline] = []
    i = mulai_index
    for _ in range(bar_datar):
        out.append(make_candle(i, harga, harga * 1.002, harga * 0.998, harga * 1.0005, volume))
        i += 1

    for _ in range(max(1, siklus)):
        blok, harga, i = blok_setup(harga, i, volume)
        out.extend(blok)
        for _ in range(naik):
            harga *= 1.01
            out.append(make_candle(i, harga / 1.01, harga * 1.002, harga / 1.011, harga, volume))
            i += 1
        for _ in range(turun):
            harga *= 0.995
            out.append(make_candle(i, harga / 0.995, harga * 1.001, harga * 0.997, harga, volume))
            i += 1
    return out


def cfg_gerbang_pump_nonaktif(config: dict) -> dict:
    out = dict(config)
    out["PUMP_MIN_24H_CHANGE_PCT"] = -1000.0
    out["PUMP_MAX_24H_CHANGE_PCT"] = 0.0
    return out
