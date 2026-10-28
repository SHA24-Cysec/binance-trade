from __future__ import annotations

from strategy.indicators import Kline

STEP_4H_MS = 14_400_000
STEP_5M_MS = 300_000
STEP_15M_MS = 900_000

BASE_PRICE = 100.0


def make_bar(
    index: int,
    open_price: float,
    high: float,
    low: float,
    close: float,
    *,
    step_ms: int = STEP_4H_MS,
    volume: float = 1_000.0,
    quote_volume: float | None = None,
) -> Kline:
    start = index * step_ms
    qv = float(quote_volume) if quote_volume is not None else float(volume) * float(close)
    return Kline(
        start,
        float(open_price),
        float(high),
        float(low),
        float(close),
        start + step_ms - 1,
        float(volume),
        qv,
    )


def _kill_bullish_gaps(bars: list[Kline]) -> list[Kline]:
    hasil: list[Kline] = []
    for index, bar in enumerate(bars):
        if index >= 2:
            previous_high = float(bars[index - 2].high)
            if float(bar.low) > previous_high:
                bar = Kline(
                    bar.open_time,
                    bar.open,
                    bar.high,
                    previous_high,
                    bar.close,
                    bar.close_time,
                    bar.volume,
                    bar.quote_volume,
                )
        hasil.append(bar)
    return hasil


def build_one_zone_series(
    start_index: int = 0,
    *,
    step_ms: int = STEP_4H_MS,
    with_fvg: bool = True,
    hvn_in_discount: bool = True,
    sweep_ssl: bool = True,
    bullish_reaction: bool = True,
    impulse_scale: float = 1.0,
) -> list[Kline]:
    base = BASE_PRICE
    bars: list[Kline] = []
    index = start_index

    price = base
    for _ in range(12):
        bars.append(
            make_bar(index, price, price + 0.6, price - 0.4, price + 0.4, step_ms=step_ms, volume=500.0)
        )
        price += 0.4
        index += 1

    base_volume = 5_000.0 if hvn_in_discount else 120.0
    for step in range(20):
        open_price = base + (0.3 if step % 2 else -0.3)
        close_price = base + (0.35 if step % 2 else -0.35)
        bars.append(
            make_bar(
                index,
                open_price,
                base + 0.6,
                base - 0.5,
                close_price,
                step_ms=step_ms,
                volume=base_volume,
            )
        )
        index += 1

    rally_volume = 900.0 if hvn_in_discount else 9_000.0
    disc_volume = 5_000.0 if hvn_in_discount else 60.0
    bars.append(
        make_bar(index, base + 0.6, base + 1.0, base + 0.5, base + 0.9, step_ms=step_ms, volume=disc_volume)
    )
    index += 1
    bars.append(
        make_bar(index, base + 0.9, base + 2.8, base + 0.8, base + 2.6, step_ms=step_ms, volume=disc_volume)
    )
    index += 1
    bars.append(
        make_bar(index, base + 2.85, base + 2.9, base + 2.6, base + 2.65, step_ms=step_ms, volume=disc_volume)
    )
    index += 1
    fourth_low = base + 3.0 if with_fvg else base + 2.5
    bars.append(
        make_bar(index, base + 3.1, base + 3.5, fourth_low, base + 3.4, step_ms=step_ms, volume=disc_volume)
    )
    index += 1

    top = base + 6.0 * impulse_scale
    for step in range(8):
        point = base + 3.6 + (top - base - 3.6) * step / 7.0
        bars.append(
            make_bar(index, point, point + 0.5, point - 0.4, point + 0.35, step_ms=step_ms, volume=rally_volume)
        )
        index += 1

    swing_high = top + 0.5
    pullback_end = base + 3.15
    for step in range(10):
        point = swing_high - (swing_high - pullback_end) * step / 9.0
        bars.append(
            make_bar(index, point, point + 0.45, point - 0.5, point - 0.3, step_ms=step_ms, volume=800.0)
        )
        index += 1

    low_base = base + 2.75
    for _ in range(3):
        bars.append(
            make_bar(index, low_base + 0.35, low_base + 0.45, low_base, low_base + 0.3, step_ms=step_ms, volume=700.0)
        )
        index += 1

    if sweep_ssl:
        bars.append(
            make_bar(
                index,
                low_base + 0.3,
                low_base + 1.1,
                low_base - 0.25,
                low_base + 1.0 if bullish_reaction else low_base - 0.2,
                step_ms=step_ms,
                volume=1_600.0,
            )
        )
    else:
        bars.append(
            make_bar(
                index,
                low_base + 0.3,
                low_base + 1.1,
                low_base + 0.1,
                low_base + 1.0 if bullish_reaction else low_base + 0.05,
                step_ms=step_ms,
                volume=1_600.0,
            )
        )
    index += 1

    return bars if with_fvg else _kill_bullish_gaps(bars)


def build_flat_series(
    count: int,
    *,
    start_index: int = 0,
    step_ms: int = STEP_4H_MS,
    price: float = 200.0,
    volume: float = 1_000.0,
    drift: float = 0.0,
) -> list[Kline]:
    bars: list[Kline] = []
    for step in range(count):
        point = price + drift * step
        bars.append(
            make_bar(
                start_index + step,
                point,
                point + 0.5,
                point - 0.5,
                point,
                step_ms=step_ms,
                volume=volume,
            )
        )
    return bars
