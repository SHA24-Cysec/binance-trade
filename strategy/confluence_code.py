from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

CONFLUENCE_CODE_DEFAULTS = {
    "CONFLUENCE_FILTER_ENABLED": True,
    "CONFLUENCE_STRUCTURE_INTERVAL": "15m",
    "CONFLUENCE_LOOKBACK_BARS": 120,
    "CONFLUENCE_MIN_IMPULSE_PCT": 2.0,
    "CONFLUENCE_IMPULSE_SELECTION": "STRONGEST",
    "CONFLUENCE_ZONE_TOLERANCE_RATIO": 0.05,
    "CONFLUENCE_REQUIRE_ALL_FIVE": True,
    "CONFLUENCE_MIN_SCORE": 70,
    "VP_FILTER_ENABLED": True,
    "VP_BINS": 24,
    "VP_HVN_TOP_RATIO": 0.30,
    "FIB_FILTER_ENABLED": True,
    "FIB_RETRACEMENT_MIN": 0.50,
    "FIB_RETRACEMENT_MAX": 0.79,
    "LIQUIDITY_FILTER_ENABLED": True,
    "LIQUIDITY_EQUAL_LEVEL_TOL_RATIO": 0.12,
    "LIQUIDITY_SWEEP_LOOKBACK_BARS": 4,
    "FVG_FILTER_ENABLED": True,
    "OB_FILTER_ENABLED": True,
    "CONFLUENCE_REQUIRE_BULLISH_REACTION": True,
    "CONFLUENCE_MIN_CLOSE_POSITION": 0.60,
    "CONFLUENCE_SL_BUFFER_PCT": 0.50,
    "CONFLUENCE_MIN_RR_RATIO": 1.5,
    "CONFLUENCE_BREAKEVEN_ENABLED": True,
    "CONFLUENCE_BREAKEVEN_R_MULTIPLE": 1.0,
    "CONFLUENCE_BREAKEVEN_LOCK_PCT": 0.0,
}

DEFAULT_MIN_SCORE = 70
COMPONENT_LABELS = {
    "volume_profile": "Volume Profile",
    "fibonacci": "Fibonacci",
    "liquidity": "Liquidity",
    "fvg": "Fair Value Gap",
    "order_block": "Order Block",
}
STAGE_LABELS = {
    "volume_profile": "VOLUME_PROFILE",
    "fibonacci": "FIBONACCI",
    "liquidity": "LIQUIDITY",
    "fvg": "FVG",
    "order_block": "ORDER_BLOCK",
    "one_zone": "ONE_ZONE",
}


@dataclass(frozen=True)
class Zone:
    low: float
    high: float
    label: str = ""

    @property
    def width(self) -> float:
        return max(0.0, self.high - self.low)

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


@dataclass
class ImpulseLeg:
    swing_low: float = 0.0
    swing_low_index: int = -1
    swing_low_time: int = 0
    swing_high: float = 0.0
    swing_high_index: int = -1
    swing_high_time: int = 0
    prior_high: float = 0.0
    impulse: float = 0.0
    impulse_pct: float = 0.0
    bos: bool = False
    bars: int = 0
    reason: str = ""


@dataclass
class ConfluenceResult:
    ok: bool = False
    stage: str = "CONFLUENCE_STRUCTURE"
    reason: str = ""
    score: float = 0.0
    checks: dict = field(default_factory=dict)
    zones: dict = field(default_factory=dict)
    values: dict = field(default_factory=dict)
    leg: Optional[ImpulseLeg] = None

    def as_dict(self) -> dict:
        return {
            "ok": bool(self.ok),
            "stage": self.stage,
            "reason": self.reason,
            "score": float(self.score),
            "checks": dict(self.checks),
            "zones": dict(self.zones),
            "values": dict(self.values),
        }


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _cfg_float(config: dict, key: str, default: float) -> float:
    value = _finite(config.get(key, default))
    return default if value is None else value


def _cfg_int(config: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    value = _cfg_float(config, key, float(default))
    return max(minimum, min(maximum, int(value)))


def _cfg_bool(config: dict, key: str, default: bool) -> bool:
    raw = config.get(key, default)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on", "aktif"}
    return bool(raw)


def _bars_ok(bars: Sequence) -> bool:
    for bar in bars:
        for name in ("open", "high", "low", "close"):
            value = _finite(getattr(bar, name, None))
            if value is None or value <= 0.0:
                return False
        if float(getattr(bar, "high")) < float(getattr(bar, "low")):
            return False
    return True


def _bar_volume(bar) -> float:
    quote = _finite(getattr(bar, "quote_volume", 0.0)) or 0.0
    if quote > 0.0:
        return quote
    volume = _finite(getattr(bar, "volume", 0.0)) or 0.0
    if volume > 0.0:
        return volume * float(getattr(bar, "close"))
    return 0.0


def zone_overlap(a: Zone, b: Zone) -> float:
    return min(a.high, b.high) - max(a.low, b.low)


def zone_touches(a: Zone, b: Zone, tolerance: float = 0.0) -> bool:
    return zone_overlap(a, b) >= -max(0.0, tolerance)


def intersect_zones(zones: Sequence[Zone]) -> Optional[Zone]:
    if not zones:
        return None
    low = max(z.low for z in zones)
    high = min(z.high for z in zones)
    if high > low:
        return Zone(low, high, "ONE_ZONE")
    return None


def confluence_gap(zones: Sequence[Zone]) -> float:
    if not zones:
        return math.inf
    low = max(z.low for z in zones)
    high = min(z.high for z in zones)
    return max(0.0, low - high)


def price_in_zone(price: float, zone: Zone, tolerance: float = 0.0) -> bool:
    return zone.low - tolerance <= price <= zone.high + tolerance


def cluster_prices(prices: Sequence[float], tolerance: float) -> list[tuple[float, float, int]]:
    values = sorted(_finite(p) for p in prices)
    values = [v for v in values if v is not None and v > 0.0]
    if not values:
        return []
    clusters: list[list[float]] = [[values[0]]]
    for value in values[1:]:
        if value - clusters[-1][-1] <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [(c[0], c[-1], len(c)) for c in clusters]


def detect_impulse_leg(
    bars: Sequence,
    min_impulse_pct: float,
    tolerance_ratio: float,
    selection: str = CONFLUENCE_CODE_DEFAULTS["CONFLUENCE_IMPULSE_SELECTION"],
) -> Optional[ImpulseLeg]:
    total = len(bars)
    if total < 12:
        return None
    lo_end = total - 1
    lo_start = max(2, int(total * 0.10))
    if lo_start >= lo_end:
        return None

    kandidat: list[ImpulseLeg] = []
    for lo_idx in range(lo_end - 1, lo_start - 1, -1):
        low = float(bars[lo_idx].low)
        if low <= 0.0:
            continue
        left = [float(bars[i].low) for i in range(max(0, lo_idx - 3), lo_idx)]
        if left and min(left) < low:
            continue
        hi_start = lo_idx + 2
        if hi_start >= lo_end:
            continue
        hi_idx = max(range(hi_start, lo_end), key=lambda i: float(bars[i].high))
        high = float(bars[hi_idx].high)
        impulse = high - low
        if impulse <= 0.0 or low <= 0.0:
            continue
        impulse_pct = impulse / low * 100.0
        if impulse_pct + 1e-9 < min_impulse_pct:
            continue
        prior_high = max(float(bars[i].high) for i in range(0, lo_idx + 1))
        if high <= prior_high:
            continue
        pullback = bars[hi_idx + 1 : lo_end]
        if not pullback:
            continue
        floor_limit = low + impulse * tolerance_ratio
        if min(float(b.low) for b in pullback) < floor_limit:
            continue
        leg = ImpulseLeg(
            swing_low=low,
            swing_low_index=lo_idx,
            swing_low_time=int(getattr(bars[lo_idx], "open_time", 0)),
            swing_high=high,
            swing_high_index=hi_idx,
            swing_high_time=int(getattr(bars[hi_idx], "close_time", 0)),
            prior_high=prior_high,
            impulse=impulse,
            impulse_pct=impulse_pct,
            bos=True,
            bars=hi_idx - lo_idx + 1,
        )
        kandidat.append(leg)
    if not kandidat:
        return None
    mode = str(
        selection or CONFLUENCE_CODE_DEFAULTS["CONFLUENCE_IMPULSE_SELECTION"]
    ).strip().upper()
    if mode == "STRONGEST":
        kandidat.sort(key=lambda item: item.impulse_pct, reverse=True)
    else:
        kandidat.sort(
            key=lambda item: (item.swing_low_index, item.impulse_pct),
            reverse=True,
        )
    return kandidat[0]


def build_volume_profile(
    bars: Sequence,
    leg: ImpulseLeg,
    bins: int,
) -> dict:
    bin_size = leg.impulse / bins if bins > 0 else 0.0
    if bin_size <= 0.0:
        return {"bins": [], "poc_price": 0.0, "poc_index": -1, "hvn_zones": [], "bin_size": 0.0}
    weights = [0.0] * bins
    for bar in bars:
        volume = _bar_volume(bar)
        if volume <= 0.0:
            volume = 1.0
        low_index = int((float(bar.low) - leg.swing_low) / bin_size)
        high_index = int((float(bar.high) - leg.swing_low) / bin_size)
        low_index = max(0, min(bins - 1, low_index))
        high_index = max(0, min(bins - 1, high_index))
        span = max(1, high_index - low_index + 1)
        for index in range(low_index, high_index + 1):
            weights[index] += volume / span

    poc_index = max(range(bins), key=lambda i: weights[i])
    poc_price = leg.swing_low + (poc_index + 0.5) * bin_size
    ranked = sorted(range(bins), key=lambda i: weights[i], reverse=True)
    top_count = max(1, int(math.ceil(bins * 0.30)))
    top = sorted(ranked[:top_count])

    hvn_zones: list[Zone] = []
    run_start = top[0]
    previous = top[0]
    for index in top[1:]:
        if index == previous + 1:
            previous = index
            continue
        hvn_zones.append(
            Zone(
                leg.swing_low + run_start * bin_size,
                leg.swing_low + (previous + 1) * bin_size,
                "HVN",
            )
        )
        run_start = index
        previous = index
    hvn_zones.append(
        Zone(
            leg.swing_low + run_start * bin_size,
            leg.swing_low + (previous + 1) * bin_size,
            "HVN",
        )
    )
    return {
        "bins": weights,
        "poc_price": poc_price,
        "poc_index": poc_index,
        "hvn_zones": hvn_zones,
        "bin_size": bin_size,
    }


def fibonacci_discount_zone(
    leg: ImpulseLeg,
    ratio_min: float,
    ratio_max: float,
) -> Zone:
    low_ratio = max(0.10, min(0.95, min(ratio_min, ratio_max)))
    high_ratio = max(low_ratio + 0.02, min(0.99, max(ratio_min, ratio_max)))
    high = leg.swing_high - leg.impulse * low_ratio
    low = leg.swing_high - leg.impulse * high_ratio
    return Zone(low, high, "FIB")


def find_fvg_zone(
    bars: Sequence,
    start_index: int,
    end_index: int,
    discount: Zone,
    tolerance: float,
) -> Optional[Zone]:
    found: Optional[Zone] = None
    best_overlap = -math.inf
    upper = min(end_index, len(bars) - 2)
    for index in range(max(1, start_index + 1), upper):
        previous_high = float(bars[index - 1].high)
        next_low = float(bars[index + 1].low)
        if next_low <= previous_high:
            continue
        candidate = Zone(previous_high, next_low, "FVG")
        if not zone_touches(candidate, discount, tolerance):
            continue
        overlap = zone_overlap(candidate, discount)
        if overlap >= best_overlap:
            best_overlap = overlap
            found = candidate
    return found


def find_order_block_zone(
    bars: Sequence,
    start_index: int,
    end_index: int,
    discount: Zone,
    tolerance: float,
) -> Optional[Zone]:
    found: Optional[Zone] = None
    best_overlap = -math.inf
    for index in range(max(1, start_index), min(end_index, len(bars) - 1)):
        bar = bars[index]
        if float(bar.close) > float(bar.open):
            continue
        horizon = bars[index + 1 : min(len(bars), index + 4)]
        if not horizon:
            continue
        if max(float(b.close) for b in horizon) <= float(bar.high):
            continue
        candidate = Zone(min(float(bar.low), float(bar.open)), max(float(bar.open), float(bar.close)), "OB")
        if candidate.width <= 0.0:
            continue
        if not zone_touches(candidate, discount, tolerance):
            continue
        overlap = zone_overlap(candidate, discount)
        if overlap >= best_overlap:
            best_overlap = overlap
            found = candidate
    return found


def find_liquidity_zone(
    bars: Sequence,
    leg: ImpulseLeg,
    tolerance: float,
) -> dict:
    pullback = bars[leg.swing_high_index + 1 :]
    if not pullback:
        return {"zone": None, "ssl_level": None, "sweep_target": None}

    lows: list[float] = []
    offset = leg.swing_high_index + 1
    for index in range(max(1, offset), len(bars) - 1):
        low = float(bars[index].low)
        left_low = float(bars[index - 1].low)
        right_low = float(bars[index + 1].low) if index + 1 < len(bars) else low
        if low <= left_low and low <= right_low:
            lows.append(low)
    if not lows:
        lows = [min(float(b.low) for b in pullback)]

    clusters = [c for c in cluster_prices(lows, tolerance) if c[2] >= 2]
    if clusters:
        low = min(c[0] for c in clusters)
        high = max(c[1] for c in clusters)
    else:
        floor = min(lows)
        low = floor - tolerance
        high = floor + tolerance

    ssl_level = low
    return {
        "zone": Zone(low, high, "SSL"),
        "ssl_level": ssl_level,
        "sweep_target": low,
    }


def find_bsl_level(bars: Sequence, leg: ImpulseLeg, tolerance: float) -> dict:
    highs = [float(bars[i].high) for i in range(leg.swing_low_index, leg.swing_high_index + 1)]
    clusters = cluster_prices(highs, tolerance)
    target = leg.swing_high
    for low, high, count in clusters:
        if count >= 2 and high >= leg.swing_high * 0.995:
            target = max(target, high)
    above = [float(bars[i].high) for i in range(leg.swing_high_index + 1, len(bars))]
    if above:
        target = max(target, leg.swing_high)
    return {"bsl_level": target, "clusters": clusters}


def evaluate_confluence_zone(bars: Sequence, config: dict) -> dict:
    interval = str(config.get("CONFLUENCE_STRUCTURE_INTERVAL", "4h"))
    result = ConfluenceResult()
    base_values = {
        "interval": interval,
        "bars": len(bars),
        "swing_low": None,
        "swing_high": None,
        "impulse_pct": None,
        "fib_zone_low": None,
        "fib_zone_high": None,
        "vp_poc_price": None,
        "vp_zone_low": None,
        "vp_zone_high": None,
        "liquidity_zone_low": None,
        "liquidity_zone_high": None,
        "ssl_level": None,
        "sweep_target": None,
        "bsl_level": None,
        "fvg_zone_low": None,
        "fvg_zone_high": None,
        "ob_zone_low": None,
        "ob_zone_high": None,
        "poi_zone_low": None,
        "poi_zone_high": None,
    }

    if not _cfg_bool(config, "CONFLUENCE_FILTER_ENABLED", True):
        result.ok = True
        result.stage = "DISABLED"
        result.reason = f"filter The Confluence Code {interval} nonaktif"
        result.values = base_values
        return {**result.as_dict(), **base_values}

    required = _cfg_int(config, "CONFLUENCE_LOOKBACK_BARS", 120, 20, 999)
    bars = list(bars)
    if len(bars) > required:
        bars = bars[-required:]
    if len(bars) < required:
        result.stage = "HISTORY_CONFIRM"
        result.reason = f"data candle struktur {interval} kurang: {len(bars)} dari minimum {required}"
        result.values = base_values
        return {**result.as_dict(), **base_values}

    if not _bars_ok(bars):
        result.reason = f"data OHLC candle struktur {interval} tidak valid"
        result.values = base_values
        return {**result.as_dict(), **base_values}

    tolerance_ratio = max(0.0, _cfg_float(config, "CONFLUENCE_ZONE_TOLERANCE_RATIO", 0.05))
    min_impulse_pct = max(0.1, _cfg_float(config, "CONFLUENCE_MIN_IMPULSE_PCT", 2.0))
    leg = detect_impulse_leg(
        bars,
        min_impulse_pct,
        tolerance_ratio,
        str(
            config.get(
                "CONFLUENCE_IMPULSE_SELECTION",
                CONFLUENCE_CODE_DEFAULTS["CONFLUENCE_IMPULSE_SELECTION"],
            )
            or CONFLUENCE_CODE_DEFAULTS["CONFLUENCE_IMPULSE_SELECTION"]
        ),
    )
    if leg is None:
        result.stage = "STRUCTURE"
        result.reason = (
            f"struktur {interval} belum membentuk impuls BoS dengan kenaikan minimal "
            f"+{min_impulse_pct:g}% dari swing low"
        )
        result.values = base_values
        return {**result.as_dict(), **base_values}

    tolerance = leg.impulse * tolerance_ratio
    base_values.update(
        swing_low=leg.swing_low,
        swing_high=leg.swing_high,
        swing_low_time=leg.swing_low_time,
        swing_high_time=leg.swing_high_time,
        impulse_pct=leg.impulse_pct,
        bos_level=leg.prior_high,
    )

    zones: list[Zone] = []
    checks: dict = {name: False for name in COMPONENT_LABELS}

    fib_on = _cfg_bool(config, "FIB_FILTER_ENABLED", True)
    discount = fibonacci_discount_zone(
        leg,
        _cfg_float(config, "FIB_RETRACEMENT_MIN", 0.50),
        _cfg_float(config, "FIB_RETRACEMENT_MAX", 0.79),
    )
    base_values["fib_zone_low"] = discount.low
    base_values["fib_zone_high"] = discount.high
    if fib_on:
        zones.append(discount)
        checks["fibonacci"] = True

    vp_on = _cfg_bool(config, "VP_FILTER_ENABLED", True)
    vp_bins = _cfg_int(config, "VP_BINS", 24, 5, 100)
    profile = build_volume_profile(
        bars[leg.swing_low_index : leg.swing_high_index + 1],
        leg,
        vp_bins,
    )
    hvn_ratio = max(0.05, min(1.0, _cfg_float(config, "VP_HVN_TOP_RATIO", 0.30)))
    hvn_zones = profile["hvn_zones"]
    if hvn_zones and hvn_ratio != 0.30:
        top_count = max(1, int(math.ceil(vp_bins * hvn_ratio)))
        weights = profile["bins"]
        ranked = sorted(range(vp_bins), key=lambda i: weights[i], reverse=True)[:top_count]
        selected = sorted(ranked)
        rebuilt: list[Zone] = []
        start = selected[0]
        previous = selected[0]
        for index in selected[1:]:
            if index == previous + 1:
                previous = index
                continue
            rebuilt.append(
                Zone(
                    leg.swing_low + start * profile["bin_size"],
                    leg.swing_low + (previous + 1) * profile["bin_size"],
                    "HVN",
                )
            )
            start = index
            previous = index
        rebuilt.append(
            Zone(
                leg.swing_low + start * profile["bin_size"],
                leg.swing_low + (previous + 1) * profile["bin_size"],
                "HVN",
            )
        )
        hvn_zones = rebuilt

    base_values["vp_poc_price"] = profile["poc_price"] if profile["bins"] else None
    vp_match: Optional[Zone] = None
    best_overlap = -math.inf
    for candidate in hvn_zones:
        overlap = zone_overlap(candidate, discount)
        if overlap <= 0.0:
            continue
        if overlap > best_overlap:
            best_overlap = overlap
            vp_match = candidate
    if vp_on:
        if vp_match is None:
            result.stage = STAGE_LABELS["volume_profile"]
            result.reason = (
                f"Volume Profile {interval} tidak punya HVN/POC di zona diskon Fibonacci "
                f"[{discount.low:.6g}..{discount.high:.6g}] "
                f"(POC {profile['poc_price']:.6g})"
                if profile["bins"]
                else f"Volume Profile {interval} tidak bisa dibentuk dari rentang impuls"
            )
            result.values = base_values
            return {**result.as_dict(), **base_values}
        zones.append(vp_match)
        checks["volume_profile"] = True
        base_values["vp_zone_low"] = vp_match.low
        base_values["vp_zone_high"] = vp_match.high

    liq_on = _cfg_bool(config, "LIQUIDITY_FILTER_ENABLED", True)
    liquidity = find_liquidity_zone(bars, leg, tolerance)
    base_values["ssl_level"] = liquidity["ssl_level"]
    base_values["sweep_target"] = liquidity["sweep_target"]
    bsl = find_bsl_level(bars, leg, tolerance)
    base_values["bsl_level"] = bsl["bsl_level"]
    if liq_on:
        liq_zone = liquidity["zone"]
        if liq_zone is None or not zone_touches(liq_zone, discount, tolerance * 2.0):
            result.stage = STAGE_LABELS["liquidity"]
            level = liquidity["ssl_level"]
            result.reason = (
                f"Liquidity {interval}: kumpulan sell-side liquidity "
                f"({level:.6g}) tidak berada di zona diskon "
                f"[{discount.low:.6g}..{discount.high:.6g}]"
                if level is not None
                else f"Liquidity {interval}: belum ada pool SSL pada fase pullback"
            )
            result.values = base_values
            return {**result.as_dict(), **base_values}
        zones.append(liq_zone)
        checks["liquidity"] = True
        base_values["liquidity_zone_low"] = liq_zone.low
        base_values["liquidity_zone_high"] = liq_zone.high

    fvg_on = _cfg_bool(config, "FVG_FILTER_ENABLED", True)
    fvg_zone = find_fvg_zone(bars, leg.swing_low_index, len(bars), discount, tolerance)
    if fvg_on:
        if fvg_zone is None:
            result.stage = STAGE_LABELS["fvg"]
            result.reason = (
                f"Fair Value Gap bullish {interval} tidak ditemukan di zona diskon "
                f"[{discount.low:.6g}..{discount.high:.6g}]"
            )
            result.values = base_values
            return {**result.as_dict(), **base_values}
        zones.append(fvg_zone)
        checks["fvg"] = True
        base_values["fvg_zone_low"] = fvg_zone.low
        base_values["fvg_zone_high"] = fvg_zone.high

    ob_on = _cfg_bool(config, "OB_FILTER_ENABLED", True)
    ob_zone = find_order_block_zone(bars, leg.swing_low_index, len(bars), discount, tolerance)
    if ob_on:
        if ob_zone is None:
            result.stage = STAGE_LABELS["order_block"]
            result.reason = (
                f"Order Block bullish {interval} tidak ditemukan di zona diskon "
                f"[{discount.low:.6g}..{discount.high:.6g}]"
            )
            result.values = base_values
            return {**result.as_dict(), **base_values}
        zones.append(ob_zone)
        checks["order_block"] = True
        base_values["ob_zone_low"] = ob_zone.low
        base_values["ob_zone_high"] = ob_zone.high

    require_all = _cfg_bool(config, "CONFLUENCE_REQUIRE_ALL_FIVE", True)
    missing = [COMPONENT_LABELS[name] for name, present in checks.items() if not present]
    if require_all and missing:
        result.stage = "ONE_ZONE"
        result.reason = (
            "konfluensi belum lengkap, komponen nonaktif atau tidak terbentuk: "
            + ", ".join(missing)
        )
        result.checks = checks
        result.values = base_values
        return {**result.as_dict(), **base_values}

    if not zones:
        result.stage = "ONE_ZONE"
        result.reason = "tidak ada komponen konfluensi aktif untuk dipertemukan"
        result.checks = checks
        result.values = base_values
        return {**result.as_dict(), **base_values}

    gap = confluence_gap(zones)
    if gap > tolerance:
        culprit = max(zones, key=lambda z: z.low)
        result.stage = "ONE_ZONE"
        result.reason = (
            f"kelima konfluensi belum bertemu di satu zona: jarak zona terjauh "
            f"{gap:.6g} melebihi toleransi {tolerance:.6g} pada {interval}, penyebab "
            f"utama zona {culprit.label} [{culprit.low:.6g}..{culprit.high:.6g}]"
        )
        result.checks = checks
        result.values = base_values
        return {**result.as_dict(), **base_values}

    intersection = intersect_zones(zones)
    if intersection is not None:
        poi = Zone(intersection.low, intersection.high, "POI")
        strict = True
    else:
        centre = (max(z.low for z in zones) + min(z.high for z in zones)) / 2.0
        pad = max(tolerance * 0.5, leg.swing_high * 0.0005)
        poi = Zone(centre - pad, centre + pad, "POI")
        strict = False

    penalty = 0.0
    if not strict:
        penalty += min(20.0, (gap / tolerance) * 20.0) if tolerance > 0 else 20.0
    if profile["bins"] and price_in_zone(profile["poc_price"], poi, tolerance * 0.5):
        penalty = max(0.0, penalty - 5.0)
    score = max(0.0, min(100.0, 100.0 - penalty))

    base_values["poi_zone_low"] = poi.low
    base_values["poi_zone_high"] = poi.high
    base_values["one_zone_strict"] = strict
    base_values["zone_gap"] = gap
    base_values["zone_tolerance"] = tolerance
    result.ok = True
    result.stage = "OK"
    result.score = score
    result.checks = checks
    result.zones = {
        "fibonacci": [discount.low, discount.high],
        "volume_profile": [base_values["vp_zone_low"], base_values["vp_zone_high"]],
        "liquidity": [base_values["liquidity_zone_low"], base_values["liquidity_zone_high"]],
        "fvg": [base_values["fvg_zone_low"], base_values["fvg_zone_high"]],
        "order_block": [base_values["ob_zone_low"], base_values["ob_zone_high"]],
        "poi": [poi.low, poi.high],
    }
    result.values = base_values
    result.reason = (
        f"one zone {interval} terbentuk: POI [{poi.low:.6g}..{poi.high:.6g}], "
        f"impuls BoS +{leg.impulse_pct:.2f}%, skor konfluensi {score:.0f}/100, "
        f"POC {profile['poc_price']:.6g}, SSL {base_values['ssl_level']:.6g}, "
        f"BSL {base_values['bsl_level']:.6g}"
    ) if profile["bins"] and base_values["ssl_level"] is not None else (
        f"one zone {interval} terbentuk di POI [{poi.low:.6g}..{poi.high:.6g}]"
    )
    return {**result.as_dict(), **base_values}


def evaluate_confluence_reaction(bars: Sequence, config: dict, zone: dict) -> dict:
    interval = str(config.get("CONFLUENCE_STRUCTURE_INTERVAL", "4h"))
    out = {
        "ok": False,
        "stage": "REACTION_CONFIRM",
        "reason": "",
        "interval": interval,
        "touched": False,
        "swept": False,
        "close_position": None,
        "signal_close": None,
        "signal_close_time": None,
        "sweep_low": None,
        "checks": {"liquidity_sweep": False, "bullish_reaction": False},
    }
    if not bars:
        out["reason"] = "candle sinyal tidak tersedia"
        return out
    if not _bars_ok(bars):
        out["reason"] = f"data OHLC candle {interval} tidak valid"
        out["stage"] = "CANDLE_CONFIRM"
        return out

    poi_low = _finite(zone.get("poi_zone_low", zone.get("poi_low")))
    poi_high = _finite(zone.get("poi_zone_high", zone.get("poi_high")))
    if poi_low is None or poi_high is None or poi_high <= poi_low:
        out["stage"] = "ONE_ZONE"
        out["reason"] = "zona POI konfluensi tidak terbentuk sehingga reaksi tidak bisa dinilai"
        return out

    tolerance = _finite(zone.get("zone_tolerance")) or 0.0
    signal = bars[-1]
    signal_open = float(signal.open)
    signal_high = float(signal.high)
    signal_low = float(signal.low)
    signal_close = float(signal.close)
    span = signal_high - signal_low
    close_position = ((signal_close - signal_low) / span) if span > 0.0 else 0.0
    poi = Zone(poi_low, poi_high, "POI")

    out.update(
        close_position=close_position,
        signal_close=signal_close,
        signal_close_time=int(getattr(signal, "close_time", 0)),
    )

    touched = signal_low <= poi_high + tolerance and signal_high >= poi_low - tolerance
    out["touched"] = touched
    if not touched:
        out["stage"] = "ZONE_TOUCH"
        out["reason"] = (
            f"harga {interval} belum menyentuh zona konfluensi "
            f"[{poi_low:.6g}..{poi_high:.6g}] (low {signal_low:.6g}, high {signal_high:.6g})"
        )
        return out

    sweep_bars = _cfg_int(config, "LIQUIDITY_SWEEP_LOOKBACK_BARS", 4, 1, 30)
    window = list(bars[-sweep_bars:])
    sweep_low = min(float(b.low) for b in window)
    out["sweep_low"] = sweep_low
    ssl_level = _finite(zone.get("ssl_level"))
    liq_on = _cfg_bool(config, "LIQUIDITY_FILTER_ENABLED", True)

    if liq_on:
        if ssl_level is not None and sweep_low >= ssl_level:
            out["stage"] = "LIQUIDITY_SWEEP"
            out["reason"] = (
                f"sell-side liquidity {interval} di {ssl_level:.6g} belum tersapu "
                f"(low terendah {sweep_low:.6g})"
            )
            return out
        if not price_in_zone(sweep_low, poi, tolerance):
            out["stage"] = "LIQUIDITY_SWEEP"
            out["reason"] = (
                f"sapuan {interval} di {sweep_low:.6g} tidak jatuh di dalam zona "
                f"konfluensi [{poi_low:.6g}..{poi_high:.6g}]"
            )
            return out
    out["swept"] = True
    out["checks"]["liquidity_sweep"] = True

    react_on = _cfg_bool(config, "CONFLUENCE_REQUIRE_BULLISH_REACTION", True)
    min_close_position = max(
        0.1, min(0.95, _cfg_float(config, "CONFLUENCE_MIN_CLOSE_POSITION", 0.60))
    )
    if react_on:
        if signal_close <= signal_open:
            out["stage"] = "REACTION_CONFIRM"
            out["reason"] = (
                f"candle {interval} belum bullish (open {signal_open:.6g}, close {signal_close:.6g})"
            )
            return out
        if signal_close < poi_low:
            out["stage"] = "REACTION_CONFIRM"
            out["reason"] = (
                f"close {interval} {signal_close:.6g} belum reclaim di atas dasar zona "
                f"{poi_low:.6g}"
            )
            return out
        if close_position + 1e-9 < min_close_position:
            out["stage"] = "REACTION_CONFIRM"
            out["reason"] = (
                f"dorongan candle {interval} lemah: close position {close_position:.2f} "
                f"di bawah minimum {min_close_position:.2f}"
            )
            return out
    out["checks"]["bullish_reaction"] = True
    out["ok"] = True
    out["stage"] = "OK"
    out["reason"] = (
        f"reaksi bullish {interval} valid: sapuan {sweep_low:.6g} di zona "
        f"[{poi_low:.6g}..{poi_high:.6g}], close {signal_close:.6g} "
        f"(posisi {close_position:.2f})"
    )
    return out
