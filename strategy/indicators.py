from __future__ import annotations

import math
from typing import NamedTuple, Optional


class Kline(NamedTuple):
    open_time: int
    open: float
    high: float
    low: float
    close: float
    close_time: int
    volume: float = 0.0
    quote_volume: float = 0.0


INTERVAL_MINUTES = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "6h": 360,
    "8h": 480,
    "12h": 720,
    "1d": 1440,
}

STRUCTURE_DEFAULT_INTERVAL = "15m"
TIMING_DEFAULT_INTERVAL = "5m"
DIRECTION_DEFAULT_INTERVAL = STRUCTURE_DEFAULT_INTERVAL
TREND_KLINE_LIMIT = 1000

MS_PER_HOUR = 3_600_000
TIME_STOP_REASON = "TIME_STOP"
MAX_HOLD_HOURS_LIMIT = 720.0

CONFLUENCE_DEFAULTS = {
    "CONFLUENCE_FILTER_ENABLED": True,
    "CONFLUENCE_STRUCTURE_INTERVAL": STRUCTURE_DEFAULT_INTERVAL,
    "CONFLUENCE_LOOKBACK_BARS": 60,
    "CONFLUENCE_MIN_IMPULSE_PCT": 1.2,
    "CONFLUENCE_ZONE_TOLERANCE_RATIO": 0.06,
    "VP_FILTER_ENABLED": True,
    "VP_BINS": 20,
    "VP_HVN_TOP_RATIO": 0.40,
    "FIB_FILTER_ENABLED": True,
    "FIB_RETRACEMENT_MIN": 0.50,
    "FIB_RETRACEMENT_MAX": 0.786,
    "LIQUIDITY_FILTER_ENABLED": True,
    "LIQUIDITY_SWEEP_LOOKBACK_BARS": 4,
    "FVG_FILTER_ENABLED": True,
    "OB_FILTER_ENABLED": True,
    "CONFLUENCE_REQUIRE_BULLISH_REACTION": True,
    "CONFLUENCE_MIN_CLOSE_POSITION": 0.55,
    "CONFLUENCE_SL_BUFFER_PCT": 0.20,
    "CONFLUENCE_MIN_RR_RATIO": 1.2,
}


def interval_ratio(target: str, source: str) -> int:
    source_minutes = INTERVAL_MINUTES.get(str(source).strip().lower())
    target_minutes = INTERVAL_MINUTES.get(str(target).strip().lower())
    if source_minutes is None:
        raise ValueError(f"Interval sumber '{source}' tidak dikenal.")
    if target_minutes is None:
        raise ValueError(f"Interval target '{target}' tidak dikenal.")
    if target_minutes % source_minutes:
        raise ValueError(
            f"Interval target '{target}' ({target_minutes} menit) harus kelipatan "
            f"bulat dari interval sumber '{source}' ({source_minutes} menit) "
            "supaya candle bisa dirangkai dari data yang sudah diunduh."
        )
    return target_minutes // source_minutes


def interval_to_ms(interval: str, default_minutes: int = 5) -> int:
    return int(INTERVAL_MINUTES.get(str(interval), default_minutes)) * 60_000


def last_closed_bar_close_ms(interval: str, now_ms: int) -> int:
    menit = INTERVAL_MINUTES.get(str(interval).strip().lower())
    if menit is None:
        raise ValueError(
            f"Interval '{interval}' tidak dikenal; pilihan: "
            + ", ".join(sorted(INTERVAL_MINUTES, key=INTERVAL_MINUTES.get))
        )
    batas = int(menit) * 60_000
    if batas <= 0:
        raise ValueError("Interval harus lebih besar dari nol")
    return ((int(now_ms) // batas) - 1) * batas + batas - 1


def parse_klines(raw: list) -> list[Kline]:
    out = []
    for idx, row in enumerate(raw):
        o = float(row[1])
        h = float(row[2])
        low_ = float(row[3])
        c = float(row[4])

        for nama, nilai in (("open", o), ("high", h), ("low", low_), ("close", c)):
            if nilai != nilai:
                raise ValueError(
                    f"Candle ke-{idx} punya {nama}=NaN. Data bursa rusak. "
                    "Dihentikan karena NaN membuat semua cek Stop Loss/Take "
                    "Profit diam-diam gagal."
                )
            if nilai in (float("inf"), float("-inf")):
                raise ValueError(
                    f"Candle ke-{idx} punya {nama} tak hingga. Data bursa rusak."
                )
            if nilai <= 0:
                raise ValueError(
                    f"Candle ke-{idx} punya {nama}={nilai}. Harga wajib > 0. "
                    "Data bursa rusak atau simbol sudah tidak diperdagangkan."
                )

        if h < low_:
            raise ValueError(
                f"Candle ke-{idx}: high ({h}) lebih kecil dari low ({low_})."
            )

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


def _interval_from_config(config: dict, key: str, default: str) -> str:
    raw = str(config.get(key, default) or default).strip().lower()
    if raw not in INTERVAL_MINUTES:
        raise ValueError(
            f"{key} '{raw}' tidak dikenal. Pilihan: "
            + ", ".join(sorted(INTERVAL_MINUTES, key=INTERVAL_MINUTES.get))
        )
    return raw


def confirm_interval(config: dict) -> str:
    return _interval_from_config(config, "CONFIRM_INTERVAL", TIMING_DEFAULT_INTERVAL)


def confirm_interval_minutes(config: dict) -> int:
    return int(INTERVAL_MINUTES[confirm_interval(config)])


def confluence_enabled(config: dict) -> bool:
    return bool(config.get("CONFLUENCE_FILTER_ENABLED", True))


def confluence_structure_interval(config: dict) -> str:
    return _interval_from_config(
        config, "CONFLUENCE_STRUCTURE_INTERVAL", STRUCTURE_DEFAULT_INTERVAL
    )


def confluence_structure_interval_minutes(config: dict) -> int:
    return int(INTERVAL_MINUTES[confluence_structure_interval(config)])


def confluence_uses_mtf(config: dict) -> bool:
    if not confluence_enabled(config):
        return False
    return confluence_structure_interval(config) != confirm_interval(config)


def confluence_structure_required_bars(config: dict) -> int:
    if not confluence_enabled(config):
        return 0
    lookback = int(config.get("CONFLUENCE_LOOKBACK_BARS", 60) or 60)
    return max(20, min(TREND_KLINE_LIMIT - 1, lookback))


def confluence_structure_window_bars(config: dict) -> int:
    if not confluence_enabled(config):
        return 0
    return confluence_structure_required_bars(config)


def confluence_trigger_required_bars(config: dict) -> int:
    if not confluence_enabled(config):
        return 0
    sweep_bars = max(2, int(config.get("LIQUIDITY_SWEEP_LOOKBACK_BARS", 4) or 4))
    if confluence_uses_mtf(config):
        return max(30, sweep_bars + 12)
    return max(confluence_structure_required_bars(config), sweep_bars + 15)


def required_lookback_bars(config: dict) -> int:
    trigger_req = confluence_trigger_required_bars(config)
    return max(14, trigger_req)


def confirm_window_bars(config: dict) -> int:
    lookback = int(config.get("CONFIRM_LOOKBACK_BARS", 60) or 60)
    return max(1, min(1000, max(lookback, required_lookback_bars(config))))


def confluence_warmup_bars(config: dict, interval: str) -> int:
    if not confluence_enabled(config):
        return 0
    source_interval = str(interval).strip().lower()
    source_minutes = INTERVAL_MINUTES.get(source_interval)
    if source_minutes is None:
        raise ValueError(
            f"Interval simulasi '{interval}' tidak dikenal sehingga candle struktur "
            "konfluensi tidak bisa dirangkai."
        )
    target_interval = confluence_structure_interval(config)
    target_minutes = int(INTERVAL_MINUTES[target_interval])
    if target_minutes % source_minutes:
        raise ValueError(
            f"CONFLUENCE_STRUCTURE_INTERVAL '{target_interval}' ({target_minutes} menit) "
            f"harus kelipatan bulat dari interval simulasi '{interval}' ({source_minutes} menit) "
            "supaya backtest bisa merangkai candle struktur dari data yang sudah diunduh."
        )
    rasio = target_minutes // source_minutes
    return (confluence_structure_window_bars(config) + 1) * rasio + rasio


def gate_warmup_bars(config: dict, interval: str) -> int:
    return confluence_warmup_bars(config, interval)


def gate_warmup_ms(config: dict, interval: str = "5m") -> int:
    menit = int(INTERVAL_MINUTES.get(str(interval), 5))
    return confluence_warmup_bars(config, str(interval)) * menit * 60_000


def native_history_ms(config: dict) -> int:
    if not confluence_enabled(config):
        return 0
    return int(
        confluence_structure_window_bars(config)
        * confluence_structure_interval_minutes(config)
        * 60_000
    )


def resolve_position_notional(config: dict, quote_free: float) -> dict:
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


def backtest_buy_execution_price(
    open_price: float, spread_pct: float = 0.0, slippage_pct: float = 0.0
) -> float:
    price = float(open_price)
    adverse = (
        max(0.0, float(spread_pct)) / 200.0 + max(0.0, float(slippage_pct)) / 100.0
    )
    return price * (1.0 + adverse)


def backtest_sell_execution_price(
    price: float, spread_pct: float = 0.0, slippage_pct: float = 0.0
) -> float:
    raw = max(0.0, float(price))
    adverse = (
        max(0.0, float(spread_pct)) / 200.0 + max(0.0, float(slippage_pct)) / 100.0
    )
    return raw * max(0.0, 1.0 - adverse)


def atr(klines: list[Kline], period: int = 14) -> float | None:
    period = int(period)
    if period <= 0 or not klines:
        return None
    trs: list[float] = []
    for i, candle in enumerate(klines):
        prev_close = klines[i - 1].close if i else candle.close
        trs.append(
            max(
                candle.high - candle.low,
                abs(candle.high - prev_close),
                abs(candle.low - prev_close),
            )
        )
    if len(trs) < period:
        return None
    result = sum(trs[:period]) / period
    for tr in trs[period:]:
        result = (result * (period - 1) + tr) / period
    return result if math.isfinite(result) and result > 0 else None


def compute_confluence_exit_levels(
    entry_price: float,
    config: dict,
    *,
    poi_low: Optional[float] = None,
    ob_low: Optional[float] = None,
    swing_low: Optional[float] = None,
    sweep_low: Optional[float] = None,
    bsl_level: Optional[float] = None,
    swing_high: Optional[float] = None,
    fallback_allowed: bool = False,
) -> dict:
    ep = float(entry_price or 0.0)
    buffer_pct = max(
        0.0, float(config.get("CONFLUENCE_SL_BUFFER_PCT", 0.20) or 0.0)
    )
    min_rr = max(
        0.1, float(config.get("CONFLUENCE_MIN_RR_RATIO", 1.2) or 1.2)
    )
    if not (math.isfinite(ep) and ep > 0.0):
        ep = 100.0

    sw_lo = (
        float(swing_low)
        if swing_low is not None and math.isfinite(float(swing_low)) and float(swing_low) > 0.0
        else 0.0
    )
    poi_valid = (
        float(poi_low)
        if poi_low is not None
        and math.isfinite(float(poi_low))
        and 0.0 < float(poi_low) < ep
        and (sw_lo <= 0.0 or float(poi_low) > sw_lo + 1e-9)
        else None
    )
    primary_floors = [
        float(x)
        for x in (poi_valid, sweep_low)
        if x is not None and math.isfinite(float(x)) and 0.0 < float(x) < ep
    ]
    secondary_floors = [
        float(x)
        for x in (ob_low, swing_low)
        if x is not None and math.isfinite(float(x)) and 0.0 < float(x) < ep
    ]
    sl_anchor = (
        min(primary_floors)
        if primary_floors
        else (min(secondary_floors) if secondary_floors else None)
    )

    ceil_candidates = [
        float(x)
        for x in (bsl_level, swing_high)
        if x is not None and math.isfinite(float(x)) and float(x) > ep
    ]
    tp_anchor = max(ceil_candidates) if ceil_candidates else None

    if sl_anchor is not None and tp_anchor is not None:
        sl_price = sl_anchor * max(0.0001, 1.0 - buffer_pct / 100.0)
        tp_price = tp_anchor
        if not (0.0 < sl_price < ep < tp_price):
            return {
                "ok": False,
                "stage": "EXIT_STRUCTURE",
                "reason": (
                    f"level SL/TP struktural tidak valid terhadap harga {ep:.6g} "
                    f"(SL {sl_price:.6g}, TP {tp_price:.6g})"
                ),
                "sl_price": None,
                "tp_price": None,
                "sl_pct": None,
                "tp_pct": None,
                "rr_ratio": None,
                "sl_buffer_pct": buffer_pct,
                "min_rr_ratio": min_rr,
                "source": "CONFLUENCE",
            }
        sl_pct = (ep - sl_price) / ep * 100.0
        tp_pct = (tp_price - ep) / ep * 100.0
        rr_ratio = (tp_pct / sl_pct) if sl_pct > 0.0 else 0.0
        if rr_ratio + 1e-9 < min_rr:
            return {
                "ok": False,
                "stage": "RISK_REWARD",
                "reason": (
                    f"rasio R:R struktural {rr_ratio:.2f} di bawah minimum {min_rr:.2f} "
                    f"(SL -{sl_pct:.2f}% @ {sl_price:.6g}, TP +{tp_pct:.2f}% @ BSL {tp_price:.6g})"
                ),
                "sl_price": sl_price,
                "tp_price": tp_price,
                "sl_pct": sl_pct,
                "tp_pct": tp_pct,
                "rr_ratio": rr_ratio,
                "sl_buffer_pct": buffer_pct,
                "min_rr_ratio": min_rr,
                "source": "CONFLUENCE",
            }
        return {
            "ok": True,
            "stage": "OK",
            "reason": (
                f"SL struktural {sl_price:.6g} (-{sl_pct:.2f}%), "
                f"TP BSL {tp_price:.6g} (+{tp_pct:.2f}%), R:R 1:{rr_ratio:.2f}"
            ),
            "sl_price": sl_price,
            "tp_price": tp_price,
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
            "rr_ratio": rr_ratio,
            "sl_buffer_pct": buffer_pct,
            "min_rr_ratio": min_rr,
            "source": "CONFLUENCE",
        }

    if not fallback_allowed:
        if sl_anchor is None:
            reason = f"level dasar SL struktural (POI/OB) tidak berada di bawah harga {ep:.6g}"
        else:
            reason = f"harga close {ep:.6g} sudah melewati target BSL struktural"
        return {
            "ok": False,
            "stage": "EXIT_STRUCTURE",
            "reason": reason,
            "sl_price": None,
            "tp_price": None,
            "sl_pct": None,
            "tp_pct": None,
            "rr_ratio": None,
            "sl_buffer_pct": buffer_pct,
            "min_rr_ratio": min_rr,
            "source": "CONFLUENCE",
        }

    sl_pct = max(2.0, buffer_pct * 10.0)
    tp_pct = max(3.5, sl_pct * max(1.75, min_rr))
    sl_price = ep * (1.0 - sl_pct / 100.0)
    tp_price = ep * (1.0 + tp_pct / 100.0)
    rr_ratio = tp_pct / sl_pct
    return {
        "ok": True,
        "stage": "FALLBACK",
        "reason": (
            f"level exit struktural cadangan: SL -{sl_pct:.2f}%, "
            f"TP +{tp_pct:.2f}%, R:R 1:{rr_ratio:.2f}"
        ),
        "sl_price": sl_price,
        "tp_price": tp_price,
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "rr_ratio": rr_ratio,
        "sl_buffer_pct": buffer_pct,
        "min_rr_ratio": min_rr,
        "source": "CONFLUENCE",
    }


def resolve_exit_levels(
    config: dict,
    setup: Optional[object] = None,
    entry_price: Optional[float] = None,
) -> dict:
    if setup is None:
        setup = config.get("_setup")
    ep_raw = (
        entry_price
        if entry_price is not None
        else config.get("_entry_price")
    )
    ep = float(ep_raw) if ep_raw is not None and float(ep_raw) > 0 else 0.0

    buffer_pct = max(
        0.0, float(config.get("CONFLUENCE_SL_BUFFER_PCT", 0.20) or 0.0)
    )
    min_rr = max(
        0.1, float(config.get("CONFLUENCE_MIN_RR_RATIO", 1.2) or 1.2)
    )

    sl_price = config.get("_sl_price")
    tp_price = config.get("_tp_price")
    sl_pct = config.get("_sl_pct")
    tp_pct = config.get("_tp_pct")

    if setup is not None:
        if sl_price is None:
            sl_price = getattr(setup, "sl_price", None)
        if tp_price is None:
            tp_price = getattr(setup, "tp_price", None)
        if sl_pct is None:
            sl_pct = getattr(setup, "sl_pct", None)
        if tp_pct is None:
            tp_pct = getattr(setup, "tp_pct", None)
        if ep <= 0.0:
            sig_c = getattr(setup, "signal_close", None)
            if sig_c is not None and float(sig_c) > 0:
                ep = float(sig_c)

    if (
        ep > 0.0
        and sl_price is not None
        and tp_price is not None
        and 0.0 < float(sl_price) < ep < float(tp_price)
    ):
        sl_p = (ep - float(sl_price)) / ep * 100.0
        tp_p = (float(tp_price) - ep) / ep * 100.0
        sl_px = float(sl_price)
        tp_px = float(tp_price)
    elif (
        sl_pct is not None
        and tp_pct is not None
        and 0.0 < float(sl_pct) < 100.0
        and float(tp_pct) > 0.0
    ):
        sl_p = float(sl_pct)
        tp_p = float(tp_pct)
        sl_px = ep * (1.0 - sl_p / 100.0) if ep > 0.0 else None
        tp_px = ep * (1.0 + tp_p / 100.0) if ep > 0.0 else None
    else:
        info = compute_confluence_exit_levels(
            ep if ep > 0.0 else 100.0,
            config,
            poi_low=getattr(setup, "poi_low", None) if setup is not None else None,
            ob_low=getattr(setup, "ob_low", None) if setup is not None else None,
            swing_low=getattr(setup, "swing_low", None) if setup is not None else None,
            sweep_low=getattr(setup, "sweep_low", None) if setup is not None else None,
            bsl_level=getattr(setup, "bsl_level", None) if setup is not None else None,
            swing_high=getattr(setup, "swing_high", None) if setup is not None else None,
            fallback_allowed=True,
        )
        sl_p = float(info["sl_pct"])
        tp_p = float(info["tp_pct"])
        sl_px = float(info["sl_price"]) if ep > 0.0 else None
        tp_px = float(info["tp_price"]) if ep > 0.0 else None

    rr_ratio = (tp_p / sl_p) if sl_p > 0.0 else 0.0
    return {
        "sl_pct": sl_p,
        "tp_pct": tp_p,
        "sl_price": sl_px,
        "tp_price": tp_px,
        "rr_ratio": rr_ratio,
        "sl_buffer_pct": buffer_pct,
        "min_rr_ratio": min_rr,
        "be_trigger_pct": 0.0,
        "be_lock_pct": 0.0,
        "trail_start_pct": 0.0,
        "trail_step_pct": 0.0,
        "source": "CONFLUENCE",
        "note": (
            f"Level exit struktural Confluence Code: SL -{sl_p:.2f}% "
            f"(di bawah OB/POI, buffer {buffer_pct:g}%), "
            f"TP +{tp_p:.2f}% (target BSL $$$), R:R 1:{rr_ratio:.2f}"
        ),
    }


def max_hold_ms(config: dict, interval: str) -> int:
    raw = config.get("MAX_HOLD_HOURS")
    if raw is None or raw == "":
        return 0
    try:
        cap_h = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"MAX_HOLD_HOURS harus berupa angka, diterima: {raw!r}")
    if not math.isfinite(cap_h):
        raise ValueError("MAX_HOLD_HOURS harus angka berhingga.")
    if cap_h < 0.0:
        raise ValueError("MAX_HOLD_HOURS tidak boleh negatif (0 = nonaktif).")
    if cap_h == 0.0:
        return 0
    if cap_h > MAX_HOLD_HOURS_LIMIT:
        raise ValueError(
            f"MAX_HOLD_HOURS maksimal {MAX_HOLD_HOURS_LIMIT:g} jam "
            f"({MAX_HOLD_HOURS_LIMIT / 24:g} hari), diminta {cap_h:g} jam."
        )
    bar_ms = interval_to_ms(interval)
    cap_ms = int(round(cap_h * MS_PER_HOUR))
    if cap_ms < 2 * bar_ms:
        raise ValueError(
            f"MAX_HOLD_HOURS {cap_h:g} jam terlalu sempit untuk interval {interval}: "
            f"minimal 2 bar = {2 * bar_ms / MS_PER_HOUR:g} jam."
        )
    return cap_ms


def time_stop_due(entry_time: int, now_ms: int, cap_ms: int, bar_ms: int) -> bool:
    if int(cap_ms) <= 0 or int(bar_ms) <= 0:
        return False
    entry = int(entry_time or 0)
    now = int(now_ms or 0)
    if entry <= 0 or now <= 0:
        return False
    return (now - entry) + int(bar_ms) > int(cap_ms)


def _resolve_minutes(val: int | str) -> int:
    if isinstance(val, str):
        s = val.strip().lower()
        if s in INTERVAL_MINUTES:
            return int(INTERVAL_MINUTES[s])
        return int(s)
    return int(val)


def aggregate_klines(
    klines: list[Kline],
    target_minutes: int | str,
    source_minutes: Optional[int | str] = None,
) -> list[Kline]:
    target_m = _resolve_minutes(target_minutes)
    if source_minutes is None:
        if len(klines) >= 2:
            source_m = max(
                1, (int(klines[1].open_time) - int(klines[0].open_time)) // 60_000
            )
        else:
            source_m = target_m
    else:
        source_m = _resolve_minutes(source_minutes)
    if isinstance(target_minutes, str) and isinstance(source_minutes, str) and target_m < source_m:
        target_m, source_m = source_m, target_m
    target_ms = target_m * 60_000
    source_ms = source_m * 60_000
    if source_ms <= 0 or target_ms % source_ms:
        raise ValueError(
            f"Interval sumber {source_m} menit tidak bisa dirangkai ke "
            f"{target_m} menit (harus pembagi bulat)."
        )
    rasio = target_ms // source_ms
    if rasio <= 1:
        return list(klines)

    tersusun: dict[int, list[Kline]] = {}
    for candle in sorted(klines, key=lambda k: int(k.open_time)):
        bucket = (int(candle.open_time) // target_ms) * target_ms
        tersusun.setdefault(bucket, []).append(candle)

    hasil: list[Kline] = []
    for bucket in sorted(tersusun):
        rows = tersusun[bucket]
        if len(rows) != rasio:
            continue
        if any(int(rows[i].open_time) != bucket + i * source_ms for i in range(rasio)):
            continue
        hasil.append(
            Kline(
                open_time=bucket,
                open=float(rows[0].open),
                high=max(float(k.high) for k in rows),
                low=min(float(k.low) for k in rows),
                close=float(rows[-1].close),
                close_time=bucket + target_ms - 1,
                volume=sum(float(k.volume) for k in rows),
                quote_volume=sum(float(k.quote_volume) for k in rows),
            )
        )
    return hasil


def _valid_candle_series(klines: list[Kline]) -> bool:
    if not klines:
        return False
    for candle in klines:
        try:
            o = float(candle.open)
            h = float(candle.high)
            l = float(candle.low)
            c = float(candle.close)
        except (AttributeError, TypeError, ValueError):
            return False
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
            return False
        if h < max(o, c, l) or l > min(o, c, h):
            return False
    return True


def _empty_structure_result(interval: str, bars: int, required: int, window: int) -> dict:
    return {
        "ok": False,
        "reason": "",
        "stage": "CONFLUENCE_STRUCTURE",
        "interval": interval,
        "bars": bars,
        "required": required,
        "window": window,
        "swing_low": None,
        "swing_high": None,
        "swing_low_time": None,
        "swing_high_time": None,
        "bsl_level": None,
        "impulse_pct": None,
        "fib_zone_low": None,
        "fib_zone_high": None,
        "vp_poc_price": None,
        "vp_zone_low": None,
        "vp_zone_high": None,
        "fvg_zone_low": None,
        "fvg_zone_high": None,
        "ob_zone_low": None,
        "ob_zone_high": None,
        "poi_zone_low": None,
        "poi_zone_high": None,
        "checks": {
            "bos_impulse": False,
            "fibonacci": False,
            "volume_profile": False,
            "fvg": False,
            "order_block": False,
        },
        "values": {},
    }


def evaluate_confluence_structure(
    klines: list[Kline],
    config: dict,
    signal_close_time_ms: Optional[int] = None,
    *,
    is_single_timeframe: bool = False,
) -> dict:
    interval = (
        confirm_interval(config)
        if is_single_timeframe
        else confluence_structure_interval(config)
    )
    window = confluence_structure_window_bars(config)
    required = confluence_structure_required_bars(config)
    if not confluence_enabled(config):
        res = _empty_structure_result(interval, 0, 0, 0)
        res["ok"] = True
        res["reason"] = f"filter Confluence Code {interval} nonaktif"
        res["stage"] = "DISABLED"
        return res

    siap = [
        k
        for k in (klines or [])
        if signal_close_time_ms is None
        or int(k.close_time) <= int(signal_close_time_ms)
    ]
    jendela = siap[-window:] if window > 0 else siap
    dasar = _empty_structure_result(interval, len(jendela), required, window)

    if len(jendela) < required:
        dasar["reason"] = (
            f"data candle struktur {interval} kurang: {len(jendela)} dari minimum {required}"
        )
        dasar["stage"] = "HISTORY_CONFIRM"
        return dasar

    if not _valid_candle_series(jendela):
        dasar["reason"] = f"data OHLC candle struktur {interval} tidak valid"
        dasar["stage"] = "CONFLUENCE_STRUCTURE"
        return dasar

    hist = jendela[:-1] if is_single_timeframe else jendela
    n = len(hist)
    lo_end = n - 4 if is_single_timeframe else n - 3
    if lo_end < 6:
        dasar["reason"] = f"rentang candle {interval} belum cukup untuk struktur swing"
        return dasar

    lo_start = max(2, int(n * 0.08))
    if lo_start >= lo_end:
        lo_start = 1
    lo_idx = min(range(lo_start, lo_end), key=lambda idx: float(hist[idx].low))
    swing_low = float(hist[lo_idx].low)
    swing_low_time = int(hist[lo_idx].open_time)

    hi_end = n - 1 if not is_single_timeframe else n - 2
    if lo_idx + 2 >= hi_end:
        dasar["reason"] = f"belum terbentuk swing high setelah swing low pada {interval}"
        return dasar

    hi_idx = max(range(lo_idx + 2, hi_end), key=lambda idx: float(hist[idx].high))
    swing_high = float(hist[hi_idx].high)
    swing_high_time = int(hist[hi_idx].close_time)
    impulse = swing_high - swing_low
    impulse_pct = (impulse / swing_low * 100.0) if swing_low > 0 else 0.0

    min_impulse_pct = max(
        0.1, float(config.get("CONFLUENCE_MIN_IMPULSE_PCT", 1.2) or 1.2)
    )
    prior_high = max(float(c.high) for c in hist[: lo_idx + 1])
    bos_ok = swing_high > prior_high and impulse_pct >= min_impulse_pct

    dasar.update(
        swing_low=swing_low,
        swing_high=swing_high,
        swing_low_time=swing_low_time,
        swing_high_time=swing_high_time,
        bsl_level=swing_high,
        impulse_pct=impulse_pct,
    )
    if not bos_ok:
        if swing_high <= prior_high:
            dasar["reason"] = (
                f"struktur {interval} belum Break of Structure (BoS): swing high "
                f"{swing_high:.6g} belum menembus high sebelumnya {prior_high:.6g}"
            )
        else:
            dasar["reason"] = (
                f"impuls BoS {interval} terlalu lemah: +{impulse_pct:.2f}% "
                f"di bawah minimum +{min_impulse_pct:g}%"
            )
        return dasar

    dasar["checks"]["bos_impulse"] = True

    fib_on = bool(config.get("FIB_FILTER_ENABLED", True))
    fib_min = float(config.get("FIB_RETRACEMENT_MIN", 0.50) or 0.50)
    fib_max = float(config.get("FIB_RETRACEMENT_MAX", 0.786) or 0.786)
    if fib_min > fib_max:
        fib_min, fib_max = fib_max, fib_min
    fib_min = max(0.1, min(0.95, fib_min))
    fib_max = max(fib_min + 0.05, min(0.99, fib_max))

    fib_zone_high = swing_high - impulse * fib_min if fib_on else swing_high
    fib_zone_low = swing_high - impulse * fib_max if fib_on else swing_low
    tol_ratio = max(
        0.0, float(config.get("CONFLUENCE_ZONE_TOLERANCE_RATIO", 0.06) or 0.06)
    )
    tol = impulse * tol_ratio

    pullback = hist[hi_idx + 1 :]
    if not pullback:
        dasar["reason"] = f"belum ada fase pullback setelah BoS pada {interval}"
        return dasar

    if any(float(c.close) < fib_zone_low - tol for c in pullback):
        dasar["fib_zone_low"] = fib_zone_low
        dasar["fib_zone_high"] = fib_zone_high
        dasar["stage"] = "FIBONACCI"
        dasar["reason"] = (
            f"zona Fibonacci {interval} [{fib_zone_low:.6g}..{fib_zone_high:.6g}] "
            "ditembus ke bawah oleh penutupan candle (struktur batal)"
        )
        return dasar

    dasar["fib_zone_low"] = fib_zone_low
    dasar["fib_zone_high"] = fib_zone_high
    dasar["checks"]["fibonacci"] = True

    vp_on = bool(config.get("VP_FILTER_ENABLED", True))
    vp_bins = max(5, min(100, int(config.get("VP_BINS", 20) or 20)))
    vp_top_ratio = max(
        0.05, min(1.0, float(config.get("VP_HVN_TOP_RATIO", 0.40) or 0.40))
    )
    leg = hist[lo_idx : hi_idx + 1]
    bin_size = impulse / vp_bins
    bins = [0.0] * vp_bins
    for candle in leg:
        vol = (
            float(candle.quote_volume)
            if float(candle.quote_volume) > 0
            else float(candle.volume)
        )
        if vol <= 0:
            vol = 1.0
        b0 = max(0, min(vp_bins - 1, int((float(candle.low) - swing_low) / bin_size)))
        b1 = max(0, min(vp_bins - 1, int((float(candle.high) - swing_low) / bin_size)))
        span = max(1, b1 - b0 + 1)
        for b in range(b0, b1 + 1):
            bins[b] += vol / span

    ranked_bins = sorted(range(vp_bins), key=lambda b: bins[b], reverse=True)
    poc_bin = ranked_bins[0]
    vp_poc_price = swing_low + (poc_bin + 0.5) * bin_size
    top_count = max(1, int(math.ceil(vp_bins * vp_top_ratio)))
    hvn_bins = ranked_bins[:top_count]

    vp_match = None
    for b in hvn_bins:
        b_lo = swing_low + b * bin_size
        b_hi = b_lo + bin_size
        if min(b_hi, fib_zone_high + tol) >= max(b_lo, fib_zone_low - tol):
            vp_match = (b_lo, b_hi)
            break

    dasar["vp_poc_price"] = vp_poc_price
    if vp_on:
        if vp_match is None:
            dasar["stage"] = "VOLUME_PROFILE"
            dasar["reason"] = (
                f"Volume Profile {interval}: tidak ada High Volume Node pada zona "
                f"diskon Fibonacci [{fib_zone_low:.6g}..{fib_zone_high:.6g}] "
                f"(POC berada di {vp_poc_price:.6g})"
            )
            return dasar
        dasar["vp_zone_low"] = vp_match[0]
        dasar["vp_zone_high"] = vp_match[1]
    else:
        dasar["vp_zone_low"] = fib_zone_low
        dasar["vp_zone_high"] = fib_zone_high
    dasar["checks"]["volume_profile"] = True

    fvg_on = bool(config.get("FVG_FILTER_ENABLED", True))
    fvg_match = None
    for k in range(lo_idx + 1, hi_idx):
        low_next = float(hist[k + 1].low)
        high_prev = float(hist[k - 1].high)
        if low_next > high_prev:
            f_lo, f_hi = high_prev, low_next
            if min(f_hi, fib_zone_high + tol) >= max(f_lo, fib_zone_low - tol):
                fvg_match = (f_lo, f_hi)

    if fvg_on:
        if fvg_match is None:
            dasar["stage"] = "FVG"
            dasar["reason"] = (
                f"Fair Value Gap (FVG) bullish {interval} tidak ditemukan pada "
                f"zona diskon [{fib_zone_low:.6g}..{fib_zone_high:.6g}]"
            )
            return dasar
        dasar["fvg_zone_low"] = fvg_match[0]
        dasar["fvg_zone_high"] = fvg_match[1]
    else:
        dasar["fvg_zone_low"] = fib_zone_low
        dasar["fvg_zone_high"] = fib_zone_high
    dasar["checks"]["fvg"] = True

    ob_on = bool(config.get("OB_FILTER_ENABLED", True))
    ob_match = None
    for k in range(lo_idx, hi_idx):
        c = hist[k]
        if float(c.close) <= float(c.open) or k == lo_idx:
            horizon = hist[k + 1 : min(hi_idx + 1, k + 4)]
            if horizon and max(float(x.close) for x in horizon) > float(c.high):
                o_lo = float(c.low)
                o_hi = max(float(c.open), float(c.close))
                if min(o_hi, fib_zone_high + tol) >= max(o_lo, fib_zone_low - tol):
                    ob_match = (o_lo, o_hi)

    if ob_on:
        if ob_match is None:
            dasar["stage"] = "ORDER_BLOCK"
            dasar["reason"] = (
                f"Order Block (OB) bullish {interval} tidak ditemukan pada "
                f"zona diskon [{fib_zone_low:.6g}..{fib_zone_high:.6g}]"
            )
            return dasar
        dasar["ob_zone_low"] = ob_match[0]
        dasar["ob_zone_high"] = ob_match[1]
    else:
        dasar["ob_zone_low"] = fib_zone_low
        dasar["ob_zone_high"] = fib_zone_high
    dasar["checks"]["order_block"] = True

    poi_low = fib_zone_low - tol
    poi_high = fib_zone_high + tol
    dasar["poi_zone_low"] = poi_low
    dasar["poi_zone_high"] = poi_high
    dasar["poi_low"] = poi_low
    dasar["poi_high"] = poi_high
    dasar["fib_low"] = fib_zone_low
    dasar["fib_high"] = fib_zone_high
    dasar["vp_poc"] = vp_poc_price
    dasar["vp_hvn_low"] = dasar["vp_zone_low"]
    dasar["vp_hvn_high"] = dasar["vp_zone_high"]
    dasar["fvg_low"] = dasar["fvg_zone_low"]
    dasar["fvg_high"] = dasar["fvg_zone_high"]
    dasar["ob_low"] = dasar["ob_zone_low"]
    dasar["ob_high"] = dasar["ob_zone_high"]
    dasar["values"] = {
        "swing_low": swing_low,
        "swing_high": swing_high,
        "bsl_level": swing_high,
        "fib_zone_low": fib_zone_low,
        "fib_zone_high": fib_zone_high,
        "vp_poc_price": vp_poc_price,
        "vp_zone_low": dasar["vp_zone_low"],
        "vp_zone_high": dasar["vp_zone_high"],
        "fvg_zone_low": dasar["fvg_zone_low"],
        "fvg_zone_high": dasar["fvg_zone_high"],
        "ob_zone_low": dasar["ob_zone_low"],
        "ob_zone_high": dasar["ob_zone_high"],
        "poi_zone_low": poi_low,
        "poi_zone_high": poi_high,
    }
    dasar["ok"] = True
    dasar["stage"] = "OK"
    dasar["reason"] = (
        f"struktur konfluensi {interval} valid: BoS +{impulse_pct:.2f}%, "
        f"POI [{poi_low:.6g}..{poi_high:.6g}], VP POC {vp_poc_price:.6g}, "
        f"FVG [{dasar['fvg_zone_low']:.6g}..{dasar['fvg_zone_high']:.6g}], "
        f"OB [{dasar['ob_zone_low']:.6g}..{dasar['ob_zone_high']:.6g}]"
    )
    return dasar


def evaluate_confluence_trigger(
    klines: list[Kline],
    config: dict,
    structure: Optional[dict] = None,
    signal_close_time_ms: Optional[int] = None,
    *,
    structure_verdict: Optional[dict] = None,
) -> dict:
    if structure is None and structure_verdict is not None:
        structure = structure_verdict
    interval = confirm_interval(config)
    required = confluence_trigger_required_bars(config)
    siap = [
        k
        for k in (klines or [])
        if signal_close_time_ms is None
        or int(k.close_time) <= int(signal_close_time_ms)
    ]
    window = confirm_window_bars(config)
    jendela = siap[-window:] if window > 0 else siap

    kosong = {
        "ok": False,
        "reason": "",
        "stage": "SETUP_CONFIRM",
        "interval": interval,
        "bars": len(jendela),
        "required": required,
        "window": window,
        "ssl_level": None,
        "sweep_low": None,
        "bsl_level": None,
        "close_position": None,
        "signal_close": None,
        "signal_close_time": None,
        "poi_zone_low": None,
        "poi_zone_high": None,
        "sl_price": None,
        "tp_price": None,
        "sl_pct": None,
        "tp_pct": None,
        "rr_ratio": None,
        "checks": {
            "liquidity_sweep": False,
            "bullish_reaction": False,
            "risk_reward": False,
        },
        "structure": structure,
    }

    if not confluence_enabled(config):
        kosong["ok"] = True
        kosong["stage"] = "DISABLED"
        kosong["reason"] = f"trigger Confluence Code {interval} nonaktif"
        if jendela:
            sig_c = float(jendela[-1].close)
            kosong["signal_close"] = sig_c
            kosong["signal_close_time"] = int(jendela[-1].close_time)
            fb_exit = compute_confluence_exit_levels(
                sig_c, config, fallback_allowed=True
            )
            kosong["sl_price"] = fb_exit["sl_price"]
            kosong["tp_price"] = fb_exit["tp_price"]
            kosong["sl_pct"] = fb_exit["sl_pct"]
            kosong["tp_pct"] = fb_exit["tp_pct"]
            kosong["rr_ratio"] = fb_exit["rr_ratio"]
        return kosong

    if len(jendela) < required:
        kosong["stage"] = "HISTORY_CONFIRM"
        kosong["reason"] = (
            f"data candle {interval} kurang: {len(jendela)} dari minimum {required}"
        )
        return kosong

    if not _valid_candle_series(jendela):
        kosong["stage"] = "CANDLE_CONFIRM"
        kosong["reason"] = f"data OHLC candle {interval} tidak valid"
        return kosong

    if structure is None:
        structure = evaluate_confluence_structure(
            jendela,
            config,
            signal_close_time_ms=signal_close_time_ms,
            is_single_timeframe=True,
        )
        kosong["structure"] = structure

    if not structure.get("ok"):
        kosong["stage"] = str(structure.get("stage") or "CONFLUENCE_STRUCTURE")
        kosong["reason"] = str(structure.get("reason") or "struktur konfluensi ditolak")
        return kosong

    raw_poi_low = structure.get("poi_zone_low", structure.get("poi_low"))
    raw_poi_high = structure.get("poi_zone_high", structure.get("poi_high"))
    if raw_poi_low is None or raw_poi_high is None:
        poi_low = min(float(c.low) for c in jendela[:-1])
        poi_high = max(float(c.high) for c in jendela[:-1])
    else:
        poi_low = float(raw_poi_low)
        poi_high = float(raw_poi_high)
    raw_fib_low = structure.get("fib_zone_low", structure.get("fib_low"))
    fib_low = float(raw_fib_low) if raw_fib_low is not None else poi_low
    swing_high_time = int(structure.get("swing_high_time") or 0)
    raw_bsl = structure.get("bsl_level", structure.get("swing_high"))
    bsl_level = float(raw_bsl) if raw_bsl is not None else max(float(c.high) for c in jendela)

    sig = jendela[-1]
    prev = jendela[-2]
    sig_open = float(sig.open)
    sig_high = float(sig.high)
    sig_low = float(sig.low)
    sig_close = float(sig.close)
    rng = sig_high - sig_low
    close_pos = ((sig_close - sig_low) / rng) if rng > 0 else 0.0

    kosong.update(
        bsl_level=bsl_level,
        close_position=close_pos,
        signal_close=sig_close,
        signal_close_time=int(sig.close_time),
        poi_zone_low=poi_low,
        poi_zone_high=poi_high,
        poi_low=poi_low,
        poi_high=poi_high,
        fib_low=fib_low,
        fib_high=structure.get("fib_zone_high", structure.get("fib_high")),
        vp_poc=structure.get("vp_poc_price", structure.get("vp_poc")),
        vp_hvn_low=structure.get("vp_zone_low", structure.get("vp_hvn_low")),
        vp_hvn_high=structure.get("vp_zone_high", structure.get("vp_hvn_high")),
        fvg_low=structure.get("fvg_zone_low", structure.get("fvg_low")),
        fvg_high=structure.get("fvg_zone_high", structure.get("fvg_high")),
        ob_low=structure.get("ob_zone_low", structure.get("ob_low")),
        ob_high=structure.get("ob_zone_high", structure.get("ob_high")),
        swing_low=structure.get("swing_low"),
        swing_high=structure.get("swing_high"),
        impulse_pct=structure.get("impulse_pct"),
    )

    liq_on = bool(config.get("LIQUIDITY_FILTER_ENABLED", True))
    sweep_bars = max(2, int(config.get("LIQUIDITY_SWEEP_LOOKBACK_BARS", 4) or 4))
    n_jendela = len(jendela)
    sweep_start_idx = max(1, n_jendela - sweep_bars)
    sweep_idx = min(
        range(sweep_start_idx, n_jendela),
        key=lambda idx: float(jendela[idx].low),
    )
    sweep_low = float(jendela[sweep_idx].low)
    early_pb = [
        c for c in jendela[:sweep_idx] if int(c.open_time) > swing_high_time
    ]
    if not early_pb:
        fallback_start = max(0, sweep_idx - max(4, sweep_bars + 2))
        early_pb = jendela[fallback_start:sweep_idx]
    if not early_pb:
        kosong["stage"] = "LIQUIDITY_SWEEP"
        kosong["reason"] = f"belum terbentuk struktur likuiditas SSL pada {interval}"
        return kosong

    ssl_level = min(float(c.low) for c in early_pb)
    kosong["ssl_level"] = ssl_level
    kosong["sweep_low"] = sweep_low

    if liq_on:
        if sweep_low >= ssl_level:
            kosong["stage"] = "LIQUIDITY_SWEEP"
            kosong["reason"] = (
                f"likuiditas SSL {interval} di {ssl_level:.6g} belum disapu "
                f"(low terbaru {sweep_low:.6g})"
            )
            return kosong
        if not (poi_low <= sweep_low <= poi_high):
            kosong["stage"] = "LIQUIDITY_SWEEP"
            kosong["reason"] = (
                f"sapuan SSL {interval} ({sweep_low:.6g}) tidak menyentuh zona "
                f"konfluensi POI [{poi_low:.6g}..{poi_high:.6g}]"
            )
            return kosong
        if sig_close < fib_low:
            kosong["stage"] = "LIQUIDITY_SWEEP"
            kosong["reason"] = (
                f"harga {interval} belum reclaim di atas dasar Fibonacci {fib_low:.6g} "
                f"setelah sapuan SSL (close {sig_close:.6g})"
            )
            return kosong
    kosong["checks"]["liquidity_sweep"] = True

    react_on = bool(config.get("CONFLUENCE_REQUIRE_BULLISH_REACTION", True))
    min_close_pos = max(
        0.1, min(0.95, float(config.get("CONFLUENCE_MIN_CLOSE_POSITION", 0.55) or 0.55))
    )
    if react_on:
        if sig_close <= sig_open:
            kosong["stage"] = "REACTION_CONFIRM"
            kosong["reason"] = (
                f"candle reaksi {interval} belum bullish "
                f"(open {sig_open:.6g}, close {sig_close:.6g})"
            )
            return kosong
        if sig_close <= float(prev.high):
            kosong["stage"] = "REACTION_CONFIRM"
            kosong["reason"] = (
                f"candle reaksi {interval} belum menembus high candle sebelumnya "
                f"({sig_close:.6g} <= {float(prev.high):.6g})"
            )
            return kosong
        if close_pos + 1e-9 < min_close_pos:
            kosong["stage"] = "REACTION_CONFIRM"
            kosong["reason"] = (
                f"dorongan candle reaksi {interval} lemah: close_pos {close_pos:.2f} "
                f"di bawah minimum {min_close_pos:.2f}"
            )
            return kosong
    kosong["checks"]["bullish_reaction"] = True

    exit_info = compute_confluence_exit_levels(
        sig_close,
        config,
        poi_low=poi_low,
        ob_low=kosong.get("ob_low"),
        swing_low=kosong.get("swing_low"),
        sweep_low=sweep_low,
        bsl_level=bsl_level,
        swing_high=kosong.get("swing_high"),
        fallback_allowed=False,
    )
    kosong["sl_price"] = exit_info["sl_price"]
    kosong["tp_price"] = exit_info["tp_price"]
    kosong["sl_pct"] = exit_info["sl_pct"]
    kosong["tp_pct"] = exit_info["tp_pct"]
    kosong["rr_ratio"] = exit_info["rr_ratio"]
    if not exit_info["ok"]:
        kosong["stage"] = str(exit_info["stage"])
        kosong["reason"] = str(exit_info["reason"])
        return kosong
    kosong["checks"]["risk_reward"] = True

    kosong["ok"] = True
    kosong["stage"] = "OK"
    kosong["reason"] = (
        f"SSL sweep {interval} valid ({sweep_low:.6g} < SSL {ssl_level:.6g}) "
        f"di zona POI [{poi_low:.6g}..{poi_high:.6g}], "
        f"reaksi bullish close {sig_close:.6g} (pos {close_pos:.2f}), "
        f"SL {exit_info['sl_price']:.6g} (-{exit_info['sl_pct']:.2f}%), "
        f"TP BSL {bsl_level:.6g} (+{exit_info['tp_pct']:.2f}%, R:R 1:{exit_info['rr_ratio']:.2f})"
    )
    return kosong


def evaluate_confluence_code(
    trigger_klines: list[Kline],
    config: dict,
    structure_klines: Optional[list[Kline]] = None,
    signal_close_time_ms: Optional[int] = None,
) -> dict:
    if (
        signal_close_time_ms is None
        and trigger_klines
    ):
        signal_close_time_ms = int(trigger_klines[-1].close_time)

    if confluence_uses_mtf(config):
        if structure_klines is None:
            src_min = confirm_interval_minutes(config)
            tgt_min = confluence_structure_interval_minutes(config)
            if tgt_min >= src_min and tgt_min % src_min == 0:
                structure_klines = aggregate_klines(
                    trigger_klines, tgt_min, src_min
                )
            else:
                structure_klines = []
        structure = evaluate_confluence_structure(
            structure_klines,
            config,
            signal_close_time_ms=signal_close_time_ms,
            is_single_timeframe=False,
        )
    else:
        structure = evaluate_confluence_structure(
            trigger_klines,
            config,
            signal_close_time_ms=signal_close_time_ms,
            is_single_timeframe=True,
        )

    return evaluate_confluence_trigger(
        trigger_klines,
        config,
        structure=structure,
        signal_close_time_ms=signal_close_time_ms,
    )


def btc_change_pct(klines: list[Kline], lookback_bars: int = 3) -> Optional[float]:
    look = max(1, int(lookback_bars or 3))
    if not klines or len(klines) < look + 1:
        return None
    prev_close = float(klines[-look - 1].close)
    last_close = float(klines[-1].close)
    if prev_close <= 0:
        return None
    return (last_close / prev_close - 1.0) * 100.0


def compute_btc_drop_pct(klines: list[Kline], lookback_bars: int = 3) -> Optional[float]:
    return btc_change_pct(klines, lookback_bars)


