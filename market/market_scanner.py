from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Callable, Optional


from strategy import indicators as strategy
from strategy.indicators import Kline

logger = logging.getLogger(__name__)

STABLE_BASE_ASSETS = {
    "USDC",
    "BUSD",
    "TUSD",
    "FDUSD",
    "DAI",
    "USDP",
    "EUR",
    "GBP",
    "TRY",
    "BRL",
    "AEUR",
    "USTC",
    "USDD",
    "PYUSD",
    "USDE",
    "USD1",
    "RLUSD",
}

LEVERAGED_TOKEN_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


@dataclass
class Candidate:
    symbol: str
    base_asset: str
    price_change_pct: float
    quote_volume: float
    last_price: float
    confirmed: bool = False
    confirm_reason: str = ""
    setup: "Optional[SetupResult]" = None
    trend: "Optional[dict]" = None


def _looks_leveraged(base_asset: str) -> bool:
    for sfx in LEVERAGED_TOKEN_SUFFIXES:
        if base_asset.endswith(sfx) and len(base_asset) - len(sfx) >= 2:
            return True
    return False


def is_structurally_allowed_symbol(
    symbol: str, config: dict, tradable_symbols: "set | None" = None
) -> bool:
    quote_asset = str(config.get("QUOTE_ASSET", ""))
    if not quote_asset or not symbol.endswith(quote_asset):
        return False
    if symbol in set(config.get("EXTRA_EXCLUDE_SYMBOLS", [])):
        return False
    if tradable_symbols is not None and symbol not in tradable_symbols:
        return False
    base_asset = symbol[: -len(quote_asset)]
    return bool(
        base_asset
        and base_asset not in STABLE_BASE_ASSETS
        and not _looks_leveraged(base_asset)
    )


def spread_pct_from_book(bid: float, ask: float) -> float:
    mid = (float(bid) + float(ask)) / 2.0
    return ((float(ask) - float(bid)) / mid * 100.0) if mid > 0 else float("inf")


def _angka_wajar(nilai) -> bool:
    try:
        angka = float(nilai)
    except (TypeError, ValueError):
        return False
    if math.isnan(angka) or math.isinf(angka):
        return False
    return angka >= 0.0


def _change_window_ok(price_change_pct, config: dict) -> tuple[bool, str]:
    min_change = float(config.get("PUMP_MIN_24H_CHANGE_PCT", 10.0) or 0.0)
    max_change = float(config.get("PUMP_MAX_24H_CHANGE_PCT", 0.0) or 0.0)
    try:
        change = float(price_change_pct)
    except (TypeError, ValueError):
        return False, f"priceChangePercent tidak bisa dibaca ({price_change_pct!r})"
    if math.isnan(change) or math.isinf(change):
        return False, f"priceChangePercent tidak wajar ({price_change_pct!r})"
    if change < min_change:
        return False, f"kenaikan 24 jam {change:.2f}% di bawah ambang {min_change:g}%"
    if max_change > 0 and change > max_change:
        return False, (
            f"kenaikan 24 jam {change:.2f}% melewati batas atas "
            f"{max_change:g}% (koin sudah terlalu tinggi)"
        )
    return True, ""


def evaluate_pump_gate(
    price_change_pct, quote_volume, config: dict, btc_drop_pct: float | None = None
) -> tuple[bool, str]:
    min_change = float(config.get("PUMP_MIN_24H_CHANGE_PCT", 10.0) or 0.0)
    max_change = float(config.get("PUMP_MAX_24H_CHANGE_PCT", 0.0) or 0.0)

    if btc_drop_pct is None:
        btc_drop_pct = config.get("_btc_drop_pct")
    if config.get("BTC_FILTER_ENABLED", False):
        if btc_drop_pct is None:
            if config.get("_btc_filter_fail_closed", False):
                return (
                    False,
                    "data filter BTC tidak tersedia, simbol ditolak (fail closed)",
                )
        else:
            max_drop = abs(float(config.get("BTC_MAX_DROP_PCT", 5.0) or 0.0))
            if float(btc_drop_pct) <= -max_drop:
                return False, (
                    f"BTC turun {float(btc_drop_pct):.2f}% dalam "
                    f"{int(config.get('BTC_LOOKBACK_BARS', 3) or 3)} candle"
                )

    if not _angka_wajar(quote_volume):
        return False, f"quote_volume 24 jam tidak wajar ({quote_volume!r})"

    window_ok, window_reason = _change_window_ok(price_change_pct, config)
    if not window_ok:
        return False, window_reason

    change = float(price_change_pct)
    rentang = (
        f"{min_change:g}% sampai {max_change:g}%"
        if max_change > 0
        else f">= {min_change:g}%"
    )
    return True, f"pump sah: naik {change:.2f}% (rentang {rentang})"


def is_pumping_today(
    symbol: str, price_change_pct, quote_volume, config: dict
) -> tuple[bool, str]:
    return evaluate_pump_gate(price_change_pct, quote_volume, config)


def pump_gate_ok_at(
    price_change_pct, quote_volume, config: dict, btc_drop_pct: "float | None" = None
) -> bool:
    ok, _r = evaluate_pump_gate(
        price_change_pct, quote_volume, config, btc_drop_pct=btc_drop_pct
    )
    return ok


def filter_and_rank_candidates(
    tickers: list,
    config: dict,
    tradable_symbols: "set | None" = None,
    *,
    apply_pump_gate: bool = True,
) -> list[Candidate]:
    quote_asset = config["QUOTE_ASSET"]
    min_vol = float(config.get("MIN_QUOTE_VOLUME_USDT_24H", 0) or 0)

    lolos_struktural = 0
    out: list[Candidate] = []

    for t in tickers:
        symbol = t.get("symbol", "")
        if not is_structurally_allowed_symbol(symbol, config, tradable_symbols):
            continue

        base_asset = symbol[: -len(quote_asset)]

        try:
            price_change_pct = float(t["priceChangePercent"])
            quote_volume = float(t["quoteVolume"])
            last_price = float(t["lastPrice"])
        except (KeyError, ValueError, TypeError):
            continue

        if last_price <= 0:
            continue
        if quote_volume < min_vol:
            continue

        lolos_struktural += 1

        if apply_pump_gate:
            ok_pump, alasan = is_pumping_today(
                symbol, price_change_pct, quote_volume, config
            )
            if not ok_pump:
                logger.debug("Gerbang pump menolak %s: %s", symbol, alasan)
                continue

        out.append(
            Candidate(
                symbol=symbol,
                base_asset=base_asset,
                price_change_pct=price_change_pct,
                quote_volume=quote_volume,
                last_price=last_price,
            )
        )

    out.sort(key=lambda c: c.quote_volume, reverse=True)

    if apply_pump_gate:
        if out:
            logger.info(
                "Gerbang pump: %d kandidat lolos dari %d simbol yang lolos "
                "saringan likuiditas.",
                len(out),
                lolos_struktural,
            )
        else:
            logger.info(
                "Gerbang pump: 0 kandidat lolos gerbang pump (dari %d simbol "
                "yang lolos saringan likuiditas). Ambang: naik %g%% sampai %s.",
                lolos_struktural,
                float(config.get("PUMP_MIN_24H_CHANGE_PCT", 10.0) or 0.0),
                (
                    f"{float(config.get('PUMP_MAX_24H_CHANGE_PCT', 0.0) or 0.0):g}%"
                    if float(config.get("PUMP_MAX_24H_CHANGE_PCT", 0.0) or 0.0) > 0
                    else "tanpa batas atas"
                ),
            )
    return out


SELL_WALL_MIN_LEVELS = 3
VALID_DEPTH_LIMITS = (100, 500, 1000)


def normalize_depth_limit(value) -> int:
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return 500
    for limit in VALID_DEPTH_LIMITS:
        if v <= limit:
            return limit
    return VALID_DEPTH_LIMITS[-1]


def _parse_levels(raw) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for lvl in raw or []:
        try:
            price = float(lvl[0])
            qty = float(lvl[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if not (math.isfinite(price) and math.isfinite(qty)) or price <= 0 or qty <= 0:
            continue
        out.append((price, qty))
    return out


def evaluate_orderbook(
    depth, planned_notional: float, config: dict
) -> tuple[bool, str, dict]:
    depth_on = bool(config.get("DEPTH_FILTER_ENABLED", False))
    book_on = bool(config.get("ORDERBOOK_FILTER_ENABLED", False))
    metrics: dict = {}
    if not depth_on and not book_on:
        return True, "filter kedalaman dan order book nonaktif", metrics

    if not isinstance(depth, dict):
        return False, "snapshot order book tidak tersedia (fail closed)", metrics
    asks = sorted(_parse_levels(depth.get("asks")), key=lambda x: x[0])
    bids = sorted(_parse_levels(depth.get("bids")), key=lambda x: x[0], reverse=True)
    if not asks or not bids:
        return False, "order book kosong di salah satu sisi (fail closed)", metrics
    try:
        planned = float(planned_notional)
    except (TypeError, ValueError):
        return False, f"nilai order tidak valid ({planned_notional!r})", metrics
    if not math.isfinite(planned) or planned <= 0:
        return False, f"nilai order tidak valid ({planned_notional!r})", metrics

    best_ask = asks[0][0]
    best_bid = bids[0][0]
    if best_bid >= best_ask:
        return (
            False,
            (f"order book tidak wajar (bid {best_bid:g} >= ask {best_ask:g})"),
            metrics,
        )
    metrics.update(best_bid=best_bid, best_ask=best_ask, planned_notional=planned)

    def _range(pct: float) -> tuple[list[tuple[float, float]], bool]:
        limit_price = best_ask * (1.0 + pct / 100.0)
        inside = [lv for lv in asks if lv[0] <= limit_price]
        truncated = asks[-1][0] < limit_price
        return inside, truncated

    if depth_on:
        rng = float(config.get("DEPTH_RANGE_PCT", 0.5) or 0.0)
        mult = float(config.get("DEPTH_MIN_ASK_NOTIONAL_MULT", 10.0) or 0.0)
        inside, truncated = _range(rng)
        total = sum(p * q for p, q in inside)
        need = mult * planned
        metrics.update(
            ask_depth_notional=total,
            ask_depth_required=need,
            ask_depth_range_pct=rng,
            ask_depth_truncated=truncated,
        )
        if total < need:
            extra = " (snapshot terpotong sebelum batas rentang)" if truncated else ""
            return (
                False,
                (
                    f"kedalaman ask {rng:g}% hanya {total:,.0f} USDT, butuh "
                    f"{need:,.0f} USDT ({mult:g}x order {planned:,.0f}){extra}"
                ),
                metrics,
            )

    if book_on:
        n = max(1, int(config.get("ORDERBOOK_LEVELS", 10) or 10))
        min_ratio = float(config.get("ORDERBOOK_MIN_BID_ASK_RATIO", 0.8) or 0.0)
        bid_n = sum(p * q for p, q in bids[:n])
        ask_n = sum(p * q for p, q in asks[:n])
        ratio = bid_n / ask_n if ask_n > 0 else float("inf")
        metrics.update(
            bid_top_notional=bid_n,
            ask_top_notional=ask_n,
            bid_ask_ratio=ratio,
            orderbook_levels=n,
        )
        if ratio < min_ratio:
            return (
                False,
                (
                    f"tekanan jual: bid {n} level teratas {bid_n:,.0f} USDT hanya "
                    f"{ratio:.2f}x ask {ask_n:,.0f} USDT, minimum {min_ratio:g}x"
                ),
                metrics,
            )

        wall_rng = float(config.get("SELL_WALL_RANGE_PCT", 1.0) or 0.0)
        max_share = float(config.get("SELL_WALL_MAX_SHARE_PCT", 30.0) or 0.0) / 100.0
        inside, truncated = _range(wall_rng)
        total = sum(p * q for p, q in inside)
        metrics.update(
            wall_range_pct=wall_rng,
            wall_range_levels=len(inside),
            wall_range_notional=total,
        )
        if len(inside) >= SELL_WALL_MIN_LEVELS and total > 0 and max_share > 0:
            wall_price, wall_qty = max(inside, key=lambda x: x[0] * x[1])
            wall_val = wall_price * wall_qty
            share = wall_val / total
            metrics.update(
                wall_price=wall_price, wall_notional=wall_val, wall_share=share
            )
            if share > max_share:
                return (
                    False,
                    (
                        f"sell wall di {wall_price:g}: {wall_val:,.0f} USDT = "
                        f"{share * 100:.0f}% dari ask {wall_rng:g}% "
                        f"({total:,.0f} USDT), maksimum {max_share * 100:g}%"
                    ),
                    metrics,
                )

    return (
        True,
        (
            f"order book sehat: ask {metrics.get('ask_depth_notional', 0):,.0f} USDT, "
            f"bid/ask {metrics.get('bid_ask_ratio', 0):.2f}x, "
            f"wall {metrics.get('wall_share', 0) * 100:.0f}%"
        ),
        metrics,
    )


DETECTOR_COMPONENTS = (
    ("change", "DETECTOR_WEIGHT_CHANGE", "Kenaikan 24j"),
    ("volume5m", "DETECTOR_WEIGHT_VOLUME5M", "Volume 5m"),
    ("orderbook", "DETECTOR_WEIGHT_ORDERBOOK", "Order book"),
    ("atr", "DETECTOR_WEIGHT_ATR", "ATR"),
)


def _clamp01(x: float) -> float:
    if x is None or not math.isfinite(x):
        return 0.0
    return max(0.0, min(1.0, float(x)))


def _closed_volume_ratio(klines: "list[Kline] | None", config: dict) -> Optional[float]:
    lookback = max(1, int(config.get("ROLLING_VOLUME_LOOKBACK_BARS", 20) or 20))
    if not klines or len(klines) < lookback + 1:
        return None
    values = []
    for k in klines:
        quote = float(getattr(k, "quote_volume", 0.0) or 0.0)
        base = float(getattr(k, "volume", 0.0) or 0.0)
        v = quote if quote > 0 else base
        if not math.isfinite(v) or v < 0:
            return None
        values.append(v)
    prior = values[-1 - lookback : -1]
    avg = sum(prior) / lookback
    if avg <= 0:
        return None
    return values[-1] / avg


def orderbook_metrics(depth, planned_notional: float, config: dict) -> Optional[dict]:
    if not isinstance(depth, dict):
        return None
    asks = sorted(_parse_levels(depth.get("asks")), key=lambda x: x[0])
    bids = sorted(_parse_levels(depth.get("bids")), key=lambda x: x[0], reverse=True)
    if not asks or not bids or planned_notional <= 0:
        return None
    best_ask, best_bid = asks[0][0], bids[0][0]
    if best_bid >= best_ask:
        return None

    def _inside(pct: float) -> list:
        lim = best_ask * (1.0 + pct / 100.0)
        return [lv for lv in asks if lv[0] <= lim]

    depth_rng = float(config.get("DEPTH_RANGE_PCT", 0.5) or 0.5)
    n = max(1, int(config.get("ORDERBOOK_LEVELS", 10) or 10))
    wall_rng = float(config.get("SELL_WALL_RANGE_PCT", 1.0) or 1.0)
    ask_depth = sum(p * q for p, q in _inside(depth_rng))
    bid_n = sum(p * q for p, q in bids[:n])
    ask_n = sum(p * q for p, q in asks[:n])
    wall_lv = _inside(wall_rng)
    wall_total = sum(p * q for p, q in wall_lv)
    wall_share = None
    if len(wall_lv) >= SELL_WALL_MIN_LEVELS and wall_total > 0:
        wall_share = max(p * q for p, q in wall_lv) / wall_total
    return {
        "ask_depth_notional": ask_depth,
        "bid_ask_ratio": (bid_n / ask_n) if ask_n > 0 else None,
        "wall_share": wall_share,
        "planned_notional": planned_notional,
    }


def compute_detector_score(
    change_pct,
    klines_5m: "list[Kline] | None",
    last_price,
    book: Optional[dict],
    config: dict,
) -> dict:
    comps: dict = {}
    missing: list = []

    lo = float(config.get("PUMP_MIN_24H_CHANGE_PCT", 6.0) or 0.0)
    hi = float(config.get("PUMP_MAX_24H_CHANGE_PCT", 0.0) or 0.0)
    try:
        chg = float(change_pct)
        if not math.isfinite(chg):
            raise ValueError
        if chg < lo:
            sub = chg / lo if lo > 0 else 1.0
        elif hi > 0 and chg > hi:
            sub = 1.0 - (chg - hi) / hi
        else:
            sub = 1.0
        comps["change"] = {"sub": _clamp01(sub), "value": chg, "unit": "%"}
    except (TypeError, ValueError):
        comps["change"] = {"sub": 0.0, "value": None, "unit": "%"}
        missing.append("change")

    mult5 = float(config.get("ROLLING_VOLUME_SURGE_MULT", 2.0) or 0.0)
    r5 = _closed_volume_ratio(klines_5m, config)
    if r5 is None:
        comps["volume5m"] = {"sub": 0.0, "value": None, "unit": "x"}
        missing.append("volume5m")
    else:
        comps["volume5m"] = {
            "sub": _clamp01(r5 / mult5) if mult5 > 0 else 1.0,
            "value": r5,
            "unit": "x",
        }

    if book is None:
        comps["orderbook"] = {"sub": 0.0, "value": None, "unit": ""}
        missing.append("orderbook")
    else:
        planned = float(book["planned_notional"])
        need = float(config.get("DEPTH_MIN_ASK_NOTIONAL_MULT", 10.0) or 0.0) * planned
        depth_sub = _clamp01(book["ask_depth_notional"] / need) if need > 0 else 1.0
        min_ratio = float(config.get("ORDERBOOK_MIN_BID_ASK_RATIO", 0.8) or 0.0)
        ratio = book.get("bid_ask_ratio")
        imb_sub = (
            0.0
            if ratio is None
            else (_clamp01(ratio / min_ratio) if min_ratio > 0 else 1.0)
        )
        max_share = float(config.get("SELL_WALL_MAX_SHARE_PCT", 30.0) or 0.0) / 100.0
        share = book.get("wall_share")
        if share is None or max_share <= 0 or share <= max_share:
            wall_sub = 1.0
        else:
            wall_sub = _clamp01(1.0 - (share - max_share) / max(1e-9, 1.0 - max_share))
        comps["orderbook"] = {
            "sub": 0.4 * depth_sub + 0.3 * imb_sub + 0.3 * wall_sub,
            "value": None,
            "unit": "",
            "depth_sub": depth_sub,
            "imbalance_sub": imb_sub,
            "wall_sub": wall_sub,
            "ask_depth_notional": book["ask_depth_notional"],
            "bid_ask_ratio": ratio,
            "wall_share": share,
        }

    band_lo = float(config.get("DETECTOR_ATR_MIN_PCT", 0.3) or 0.0)
    band_hi = float(config.get("DETECTOR_ATR_MAX_PCT", 1.2) or 0.0)
    atr_val = None
    try:
        if klines_5m:
            atr_val = strategy.atr(klines_5m, int(config.get("ATR_PERIOD", 14) or 14))
        price = float(last_price)
    except (TypeError, ValueError):
        atr_val, price = None, 0.0
    if atr_val is None or not math.isfinite(atr_val) or price <= 0:
        comps["atr"] = {"sub": 0.0, "value": None, "unit": "%"}
        missing.append("atr")
    else:
        pct = atr_val / price * 100.0
        if pct < band_lo:
            sub = pct / band_lo if band_lo > 0 else 1.0
        elif band_hi > 0 and pct > band_hi:
            sub = 1.0 - (pct - band_hi) / band_hi
        else:
            sub = 1.0
        comps["atr"] = {"sub": _clamp01(sub), "value": pct, "unit": "%"}

    total_w = 0.0
    acc = 0.0
    for key, wkey, label in DETECTOR_COMPONENTS:
        w = max(0.0, float(config.get(wkey, 0.0) or 0.0))
        comps[key]["weight"] = w
        comps[key]["label"] = label
        comps[key]["points"] = None
        total_w += w
        acc += w * comps[key]["sub"]
    score = (acc / total_w * 100.0) if total_w > 0 else 0.0
    for key, _wkey, _label in DETECTOR_COMPONENTS:
        w = comps[key]["weight"]
        comps[key]["points"] = (
            (w * comps[key]["sub"] / total_w * 100.0) if total_w > 0 else 0.0
        )
        comps[key]["max_points"] = (w / total_w * 100.0) if total_w > 0 else 0.0
    return {
        "score": round(score, 1),
        "components": comps,
        "missing": missing,
        "partial": bool(missing),
    }


@dataclass
class SetupResult:
    ok: bool
    reason: str
    signal_close: Optional[float] = None
    atr_value: Optional[float] = None
    demand_zone_low: Optional[float] = None
    demand_zone_high: Optional[float] = None
    demand_distance_pct: Optional[float] = None
    demand_close_position: Optional[float] = None


def _demand_zone_confirmation(klines: list[Kline], config: dict) -> dict:
    return strategy.detect_demand_zone(klines, config)


def _rolling_volume_confirmation(klines: list[Kline], config: dict) -> tuple[bool, str]:
    if not bool(config.get("ROLLING_VOLUME_FILTER_ENABLED", True)):
        return True, "rolling volume nonaktif"

    lookback = max(1, int(config.get("ROLLING_VOLUME_LOOKBACK_BARS", 20) or 20))
    confirmations = max(1, int(config.get("ROLLING_VOLUME_CONFIRMATION_BARS", 1) or 1))
    multiplier = float(config.get("ROLLING_VOLUME_SURGE_MULT", 2.0) or 0.0)
    required = lookback + confirmations
    if len(klines) < required:
        return (
            False,
            f"data volume rolling kurang: {len(klines)} dari minimum {required}",
        )
    if not math.isfinite(multiplier) or multiplier <= 0:
        return False, "ROLLING_VOLUME_SURGE_MULT tidak valid"

    values = []
    for k in klines:
        quote = float(getattr(k, "quote_volume", 0.0) or 0.0)
        base = float(getattr(k, "volume", 0.0) or 0.0)
        value = quote if quote > 0 else base
        if not math.isfinite(value) or value < 0:
            return False, "volume candle tidak valid"
        values.append(value)

    passed = 0
    ratios = []
    for offset in range(confirmations):
        idx = len(values) - confirmations + offset
        prior = values[idx - lookback : idx]
        average = sum(prior) / lookback
        current = values[idx]
        if average <= 0:
            return False, "rata-rata volume rolling nol"
        ratio = current / average
        ratios.append(ratio)
        if ratio >= multiplier:
            passed += 1

    ok = passed == confirmations
    detail = (
        f"rolling volume {min(ratios):.2f}x, minimum {multiplier:g}x, "
        f"{passed}/{confirmations} candle konfirmasi"
    )
    return ok, detail


def detect_entry_setup(klines: list[Kline], config: dict) -> SetupResult:
    n = len(klines)
    minimum = strategy.required_lookback_bars(config)
    if n < minimum:
        return SetupResult(False, f"data candle kurang: {n} dari minimum {minimum}")
    closes = [float(k.close) for k in klines]
    if any(x <= 0 or not math.isfinite(x) for x in closes):
        return SetupResult(False, "close candle tidak valid")
    volume_ok, volume_detail = _rolling_volume_confirmation(klines, config)
    if not volume_ok:
        return SetupResult(False, f"rolling volume ditolak: {volume_detail}")
    demand = _demand_zone_confirmation(klines, config)
    if not demand["ok"]:
        return SetupResult(
            False,
            f"zona demand ditolak: {demand['reason']}",
            demand_zone_low=demand.get("zone_low"),
            demand_zone_high=demand.get("zone_high"),
            demand_distance_pct=demand.get("distance_pct"),
            demand_close_position=demand.get("close_position"),
        )
    return SetupResult(
        True,
        f"entry sah: gerbang pump lolos, {volume_detail}, {demand['reason']}",
        signal_close=klines[-1].close,
        atr_value=strategy.atr(klines, int(config.get("ATR_PERIOD", 14) or 14)),
        demand_zone_low=demand.get("zone_low"),
        demand_zone_high=demand.get("zone_high"),
        demand_distance_pct=demand.get("distance_pct"),
        demand_close_position=demand.get("close_position"),
    )


def setup_quality_key(setup: SetupResult, candidate: Candidate) -> tuple:
    return (-float(candidate.quote_volume),)


def trend_verdict(symbol: str, config: dict, trend_provider) -> dict:
    """Gerbang trend timeframe tinggi untuk satu kandidat.

    Aturan fail closed yang sama seperti filter BTC: kalau candle trend tidak
    bisa diambil atau riwayatnya kurang, kandidat DITOLAK, bukan diloloskan.
    """
    from strategy import indicators as strategy_mod

    interval = str(config.get("TREND_INTERVAL", "1h") or "1h")
    kosong = {
        "interval": interval,
        "bars": 0,
        "required": strategy_mod.trend_required_bars(config),
        "window": strategy_mod.trend_window_bars(config),
        "close": None,
        "ema_fast": None,
        "ema_slow": None,
        "adx": None,
        "checks": {},
        "values": {},
    }
    if not bool(config.get("TREND_FILTER_ENABLED", False)):
        return {"ok": True, "reason": "filter trend nonaktif", **kosong}
    if trend_provider is None:
        return {
            "ok": False,
            "reason": (
                f"filter trend {interval} aktif tetapi penyedia candle trend "
                "tidak tersedia (fail closed)"
            ),
            **kosong,
        }
    try:
        klines = trend_provider(symbol)
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"candle trend {interval} {symbol} gagal diambil: {exc}",
            **kosong,
        }
    if not klines:
        return {
            "ok": False,
            "reason": f"candle trend {interval} {symbol} kosong",
            **kosong,
        }
    return strategy_mod.evaluate_trend_filter(list(klines), config)


def find_best_candidate(
    tickers: list,
    klines_fetcher,
    config: dict,
    tradable_symbols: "set | None" = None,
    klines_fetcher_many: "Optional[Callable[[list], dict]]" = None,
    prewarm_fn: "Optional[Callable[[list], None]]" = None,
    trend_provider: "Optional[Callable[[str], list]]" = None,
) -> Optional[Candidate]:
    ranked = filter_and_rank_candidates(tickers, config, tradable_symbols)
    top_n = ranked[: int(config.get("TOP_N_CANDIDATES_TO_CONFIRM", 10) or 10)]
    if not top_n:
        return None

    if prewarm_fn is not None:
        try:
            prewarm_fn([cand.symbol for cand in top_n])
        except Exception as exc:
            logger.debug("Pemanasan harga kandidat dilewati: %s", exc)

    diprakira: dict = {}
    if klines_fetcher_many is not None and len(top_n) > 1:
        try:
            diprakira = klines_fetcher_many([cand.symbol for cand in top_n]) or {}
        except Exception as exc:
            logger.debug("Pengambilan candle paralel dilewati: %s", exc)
            diprakira = {}

    lolos: list[Candidate] = []
    for cand in top_n:
        try:
            if cand.symbol in diprakira:
                klines = diprakira.get(cand.symbol)
                if klines is None:
                    raise ValueError("candle konfirmasi gagal diunduh")
            else:
                klines = klines_fetcher(cand.symbol)
        except Exception as exc:
            cand.confirmed = False
            cand.confirm_reason = f"gagal mengambil candle: {exc}"
            continue
        hasil = detect_entry_setup(klines or [], config)
        cand.confirmed = hasil.ok
        cand.confirm_reason = hasil.reason
        cand.setup = hasil
        if not hasil.ok:
            continue
        if bool(config.get("TREND_FILTER_ENABLED", False)):
            trend = trend_verdict(cand.symbol, config, trend_provider)
            cand.trend = trend
            if not trend["ok"]:
                cand.confirmed = False
                cand.confirm_reason = (
                    f"trend {trend.get('interval') or 'HTF'} ditolak: {trend['reason']}"
                )
                continue
            cand.confirm_reason = f"{hasil.reason} | {trend['reason']}"
        lolos.append(cand)

    if not lolos:
        return None
    lolos.sort(key=lambda c: setup_quality_key(c.setup, c))
    return lolos[0]
