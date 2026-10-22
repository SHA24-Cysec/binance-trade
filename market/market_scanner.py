from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    htf_trend: "Optional[dict]" = None
    htf_resistance: "Optional[dict]" = None
    direction: "Optional[dict]" = None
    direction_zone: "Optional[dict]" = None
    timing: "Optional[dict]" = None


@dataclass
class ScanDiagnostics:
    market_candidates: int = 0
    checked_candidates: int = 0
    latest_signal_close_ms: int = 0
    rejection_counts: dict[str, int] = field(default_factory=dict)
    examples: dict[str, tuple[str, str]] = field(default_factory=dict)

    def observe_signal(self, klines: list) -> None:
        if not klines:
            return
        try:
            close_ms = int(klines[-1].close_time)
        except (AttributeError, TypeError, ValueError):
            return
        self.latest_signal_close_ms = max(self.latest_signal_close_ms, close_ms)

    def reject(self, stage: str, symbol: str, reason: str) -> None:
        self.rejection_counts[stage] = self.rejection_counts.get(stage, 0) + 1
        detail = "".join(ch if ch.isprintable() else " " for ch in str(reason))
        detail = " ".join(detail.split()) or "alasan tidak tersedia"
        sample_symbol = "".join(ch if ch.isprintable() else " " for ch in str(symbol))
        sample_symbol = " ".join(sample_symbol.split())[:32]
        if len(detail) > 140:
            detail = detail[:137] + "..."
        self.examples.setdefault(stage, (sample_symbol, detail))

    def signature(self) -> tuple:
        return (
            self.market_candidates,
            self.checked_candidates,
            self.latest_signal_close_ms,
            tuple(
                (stage, count, self.examples.get(stage))
                for stage, count in sorted(self.rejection_counts.items())
            ),
        )

    def log_lines(self, config: dict) -> list[str]:
        if not self.checked_candidates:
            return []
        interval = str(config.get("CONFIRM_INTERVAL", "5m"))
        arah_interval = str(config.get("DIRECTION_INTERVAL", "15m"))
        htf_interval = str(config.get("HTF_INTERVAL", "1h"))
        labels = {
            "HTF_TREND": f"tren {htf_interval}",
            "HTF_RESISTANCE": f"resistance {htf_interval}",
            "DIRECTION": f"arah {arah_interval}",
            "DIRECTION_ZONE": f"zona demand {arah_interval}",
            "CANDLE_CONFIRM": f"candle {interval} gagal diambil",
            "TIMING": f"timing {interval}",
            "HISTORY_CONFIRM": f"riwayat {interval} kurang",
            "BREAKOUT_RETEST": f"breakout-retest {interval}",
            "VOLUME_CONFIRM": f"volume rolling {interval}",
            "DEMAND_CONFIRM": f"demand {interval}",
            "SETUP_CONFIRM": f"konfirmasi {interval}",
        }
        candle = "belum tersedia"
        if self.latest_signal_close_ms > 0:
            try:
                candle = datetime.fromtimestamp(
                    self.latest_signal_close_ms / 1000, tz=timezone.utc
                ).strftime("%H:%M:%S UTC")
            except (OverflowError, OSError, ValueError):
                candle = "waktu candle tidak valid"
        lines = [
            f"Diagnostik entry: {self.checked_candidates} dari "
            f"{self.market_candidates} kandidat pasar diperiksa; "
            f"candle konfirmasi terbaru tutup {candle}.",
            "Diagnostik: setiap simbol dicatat pada gerbang pertama yang gagal.",
        ]
        if bool(config.get("HTF_FILTER_ENABLED", False)):
            lines.insert(
                0,
                f"Status gerbang tren HTF {htf_interval}: AKTIF; hanya candle tertutup "
                "yang dipakai dan kegagalan data menolak entry.",
            )
        if strategy.htf_resistance_enabled(config):
            lines.insert(
                1 if lines else 0,
                f"Status resistance HTF {htf_interval}: AKTIF; close sinyal M5 "
                "dibandingkan dengan pivot high H1 tertutup.",
            )
        for stage, label in labels.items():
            count = self.rejection_counts.get(stage, 0)
            if not count:
                continue
            symbol, reason = self.examples.get(stage, ("-", "alasan tidak tersedia"))
            lines.append(
                f"Penolakan {label}: {count} kandidat; contoh {symbol}: {reason}"
            )
        return lines


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
    if symbol in set(config.get("_blocked_symbols") or ()):
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


def _validate_price_change(price_change_pct) -> tuple[bool, str]:
    try:
        change = float(price_change_pct)
    except (TypeError, ValueError):
        return False, f"priceChangePercent tidak bisa dibaca ({price_change_pct!r})"
    if not math.isfinite(change):
        return False, f"priceChangePercent tidak wajar ({price_change_pct!r})"
    return True, ""


def evaluate_market_gate(
    price_change_pct, quote_volume, config: dict, btc_drop_pct: float | None = None
) -> tuple[bool, str]:
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

    data_ok, data_reason = _validate_price_change(price_change_pct)
    if not data_ok:
        return False, data_reason

    try:
        minimum = float(config.get("PUMP_MIN_24H_CHANGE_PCT", 10.0))
    except (TypeError, ValueError):
        return False, "minimum pump 24 jam bukan angka yang valid"
    if not math.isfinite(minimum):
        return False, "minimum pump 24 jam bukan angka yang valid"
    change = float(price_change_pct)
    if change < minimum:
        return False, f"kenaikan 24 jam {change:.2f}% di bawah ambang {minimum:g}%"

    return True, f"filter pasar lolos: kenaikan 24 jam {change:.2f}% >= {minimum:g}%"


def market_gate_ok_at(
    price_change_pct, quote_volume, config: dict, btc_drop_pct: "float | None" = None
) -> bool:
    ok, _r = evaluate_market_gate(
        price_change_pct, quote_volume, config, btc_drop_pct=btc_drop_pct
    )
    return ok


def filter_and_rank_candidates(
    tickers: list,
    config: dict,
    tradable_symbols: "set | None" = None,
    *,
    apply_market_gate: bool = True,
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

        if not math.isfinite(last_price) or last_price <= 0:
            continue
        if not math.isfinite(price_change_pct) or not _angka_wajar(quote_volume):
            continue
        if quote_volume < min_vol:
            continue

        lolos_struktural += 1

        if apply_market_gate:
            ok_market, alasan = evaluate_market_gate(
                price_change_pct, quote_volume, config
            )
            if not ok_market:
                logger.debug("Filter pasar menolak %s: %s", symbol, alasan)
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

    if apply_market_gate:
        logger.info(
            "Filter pasar: %d kandidat lolos dari %d simbol yang lolos "
            "saringan likuiditas.",
            len(out),
            lolos_struktural,
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


@dataclass
class SetupResult:
    ok: bool
    reason: str
    signal_close: Optional[float] = None
    signal_close_time: Optional[int] = None
    atr_value: Optional[float] = None
    demand_zone_low: Optional[float] = None
    demand_zone_high: Optional[float] = None
    demand_distance_pct: Optional[float] = None
    demand_close_position: Optional[float] = None
    breakout_level: Optional[float] = None
    breakout_time: Optional[int] = None
    retest_zone_low: Optional[float] = None
    retest_zone_high: Optional[float] = None
    retest_touches: int = 0


def _demand_zone_confirmation(klines: list[Kline], config: dict) -> dict:
    return strategy.detect_demand_zone(klines, config)


def _rolling_volume_confirmation(klines: list[Kline], config: dict) -> tuple[bool, str]:
    if not bool(config.get("ROLLING_VOLUME_FILTER_ENABLED", False)):
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

    breakout = strategy.evaluate_breakout_retest(klines, config)
    breakout_fields = {
        "breakout_level": breakout.get("breakout_level"),
        "breakout_time": breakout.get("breakout_time"),
        "retest_zone_low": breakout.get("retest_zone_low"),
        "retest_zone_high": breakout.get("retest_zone_high"),
        "retest_touches": int(breakout.get("retest_touches", 0) or 0),
    }
    if not breakout["ok"]:
        return SetupResult(
            False,
            f"breakout-retest ditolak: {breakout['reason']}",
            **breakout_fields,
        )

    volume_ok, volume_detail = _rolling_volume_confirmation(klines, config)
    if not volume_ok:
        return SetupResult(
            False,
            f"rolling volume ditolak: {volume_detail}",
            **breakout_fields,
        )
    demand = _demand_zone_confirmation(klines, config)
    if not demand["ok"]:
        return SetupResult(
            False,
            f"zona demand ditolak: {demand['reason']}",
            demand_zone_low=demand.get("zone_low"),
            demand_zone_high=demand.get("zone_high"),
            demand_distance_pct=demand.get("distance_pct"),
            demand_close_position=demand.get("close_position"),
            **breakout_fields,
        )

    details = []
    if strategy.breakout_retest_enabled(config):
        details.append(breakout["reason"])
    if bool(config.get("ROLLING_VOLUME_FILTER_ENABLED", False)):
        details.append(volume_detail)
    if bool(config.get("DEMAND_ZONE_FILTER_ENABLED", False)):
        details.append(demand["reason"])
    return SetupResult(
        True,
        "entry sah: " + (", ".join(details) if details else "seluruh filter aktif lolos"),
        signal_close=klines[-1].close,
        signal_close_time=int(klines[-1].close_time),
        atr_value=strategy.atr(klines, int(config.get("ATR_PERIOD", 14) or 14)),
        demand_zone_low=demand.get("zone_low"),
        demand_zone_high=demand.get("zone_high"),
        demand_distance_pct=demand.get("distance_pct"),
        demand_close_position=demand.get("close_position"),
        **breakout_fields,
    )


def setup_quality_key(setup: SetupResult, candidate: Candidate) -> tuple:
    return (-float(candidate.quote_volume),)


def _htf_trend_context(
    symbol: str,
    config: dict,
    htf_provider,
    signal_close_time_ms: int | None = None,
) -> tuple[dict, list[Kline] | None]:
    from strategy import indicators as strategy_mod

    interval = strategy_mod.htf_interval(config)
    if not strategy_mod.htf_enabled(config):
        return strategy_mod.evaluate_htf([], config, signal_close_time_ms), None

    kosong = {
        "interval": interval,
        "bars": 0,
        "required": strategy_mod.htf_required_bars(config),
        "window": strategy_mod.htf_window_bars(config),
        "close": None,
        "ema_fast": None,
        "ema_slow": None,
        "adx": None,
        "checks": {},
        "values": {},
    }
    if htf_provider is None:
        return (
            {
                "ok": False,
                "reason": (
                    f"filter tren HTF {interval} aktif tetapi penyedia candle "
                    "tidak tersedia (ditolak demi keamanan)"
                ),
                **kosong,
            },
            None,
        )
    try:
        klines = htf_provider(symbol)
    except Exception as exc:
        return (
            {
                "ok": False,
                "reason": f"candle tren HTF {interval} {symbol} gagal diambil: {exc} (ditolak demi keamanan)",
                **kosong,
            },
            None,
        )
    if not klines:
        return (
            {
                "ok": False,
                "reason": f"candle tren HTF {interval} {symbol} kosong (ditolak demi keamanan)",
                **kosong,
            },
            None,
        )
    rows = list(klines)
    return (
        strategy_mod.evaluate_htf(rows, config, signal_close_time_ms),
        rows,
    )


def htf_trend_verdict(
    symbol: str,
    config: dict,
    htf_provider,
    signal_close_time_ms: int | None = None,
) -> dict:
    return _htf_trend_context(
        symbol, config, htf_provider, signal_close_time_ms
    )[0]


def direction_verdict(
    symbol: str,
    config: dict,
    direction_provider,
    signal_close_time_ms: int | None = None,
) -> dict:
    from strategy import indicators as strategy_mod

    interval = str(config.get("DIRECTION_INTERVAL", "15m") or "15m")
    kosong = {
        "interval": interval,
        "bars": 0,
        "required": strategy_mod.direction_required_bars(config),
        "window": strategy_mod.direction_window_bars(config),
        "close": None,
        "ema_fast": None,
        "ema_slow": None,
        "adx": None,
        "checks": {},
        "values": {},
    }
    if not bool(config.get("DIRECTION_FILTER_ENABLED", False)):
        return {"ok": True, "reason": "filter arah nonaktif", **kosong}
    if direction_provider is None:
        return {
            "ok": False,
            "reason": (
                f"filter arah {interval} aktif tetapi penyedia candle arah "
                "tidak tersedia (fail closed)"
            ),
            **kosong,
        }
    try:
        klines = direction_provider(symbol)
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"candle arah {interval} {symbol} gagal diambil: {exc}",
            **kosong,
        }
    if not klines:
        return {
            "ok": False,
            "reason": f"candle arah {interval} {symbol} kosong",
            **kosong,
        }
    return strategy_mod.evaluate_direction(
        list(klines), config, signal_close_time_ms
    )


def direction_zone_verdict(
    symbol: str,
    config: dict,
    direction_provider,
    signal_close_time_ms: int | None = None,
) -> dict:
    from strategy import indicators as strategy_mod

    interval = strategy_mod.direction_interval(config)
    kosong = {
        "interval": interval,
        "bars": 0,
        "required": strategy_mod.direction_zone_window_bars(config),
        "zone_low": None,
        "zone_high": None,
        "distance_pct": None,
        "close_position": None,
    }
    if not bool(config.get("DIRECTION_ZONE_FILTER_ENABLED", False)):
        return {
            "ok": True,
            "reason": f"filter zona demand {interval} nonaktif",
            **kosong,
        }
    if direction_provider is None:
        return {
            "ok": False,
            "reason": (
                f"filter zona demand {interval} aktif tetapi penyedia candle "
                "arah tidak tersedia (fail closed)"
            ),
            **kosong,
        }
    try:
        klines = direction_provider(symbol)
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"candle arah {interval} {symbol} gagal diambil: {exc}",
            **kosong,
        }
    if not klines:
        return {
            "ok": False,
            "reason": f"candle arah {interval} {symbol} kosong",
            **kosong,
        }
    return strategy_mod.evaluate_direction_zone(
        list(klines), config, signal_close_time_ms
    )


def timing_verdict(
    config: dict, klines: list, signal_close_time_ms: int | None = None
) -> dict:
    from strategy import indicators as strategy_mod

    interval = strategy_mod.timing_interval(config)
    kosong = {
        "interval": interval,
        "bars": 0,
        "required": strategy_mod.timing_required_bars(config),
        "window": strategy_mod.timing_window_bars(config),
        "close": None,
        "ema_fast": None,
        "ema_slow": None,
        "adx": None,
        "checks": {},
        "values": {},
    }
    if not strategy_mod.timing_enabled(config):
        return {"ok": True, "reason": f"filter timing {interval} nonaktif", **kosong}
    if not klines:
        return {
            "ok": False,
            "reason": f"candle timing {interval} kosong (fail closed)",
            **kosong,
        }
    return strategy_mod.evaluate_timing(list(klines), config, signal_close_time_ms)


def find_best_candidate(
    tickers: list,
    klines_fetcher,
    config: dict,
    tradable_symbols: "set | None" = None,
    klines_fetcher_many: "Optional[Callable[[list], dict]]" = None,
    prewarm_fn: "Optional[Callable[[list], None]]" = None,
    direction_provider: "Optional[Callable[[str], list]]" = None,
    diagnostics: Optional[ScanDiagnostics] = None,
    confirm_close_time_ms: "int | None" = None,
    htf_provider: "Optional[Callable[[str], list]]" = None,
) -> Optional[Candidate]:
    ranked = filter_and_rank_candidates(tickers, config, tradable_symbols)
    top_n = ranked[: int(config.get("TOP_N_CANDIDATES_TO_CONFIRM", 10) or 10)]
    if diagnostics is not None:
        diagnostics.market_candidates = len(ranked)
        diagnostics.checked_candidates = len(top_n)
        diagnostics.latest_signal_close_ms = 0
        diagnostics.rejection_counts.clear()
        diagnostics.examples.clear()
    if not top_n:
        return None

    if prewarm_fn is not None:
        try:
            prewarm_fn([cand.symbol for cand in top_n])
        except Exception as exc:
            logger.debug("Pemanasan harga kandidat dilewati: %s", exc)

    lolos_htf: list[Candidate] = []
    htf_klines_by_symbol: dict[str, list[Kline]] = {}
    resistance_active = strategy.htf_resistance_enabled(config)
    for cand in top_n:
        if strategy.htf_enabled(config):
            if resistance_active:
                tren_htf, htf_klines = _htf_trend_context(
                    cand.symbol, config, htf_provider, confirm_close_time_ms
                )
                if htf_klines is not None:
                    htf_klines_by_symbol[cand.symbol] = htf_klines
            else:
                tren_htf = htf_trend_verdict(
                    cand.symbol, config, htf_provider, confirm_close_time_ms
                )
            cand.htf_trend = tren_htf
            if not tren_htf["ok"]:
                cand.confirmed = False
                cand.confirm_reason = (
                    f"tren HTF {tren_htf.get('interval') or '1h'} ditolak: "
                    f"{tren_htf['reason']}"
                )
                if diagnostics is not None:
                    diagnostics.reject("HTF_TREND", cand.symbol, tren_htf["reason"])
                continue
            cand.confirm_reason = tren_htf["reason"]
        if bool(config.get("DIRECTION_FILTER_ENABLED", False)):
            arah = direction_verdict(
                cand.symbol, config, direction_provider, confirm_close_time_ms
            )
            cand.direction = arah
            if not arah["ok"]:
                cand.confirmed = False
                cand.confirm_reason = (
                    f"arah {arah.get('interval') or '15m'} ditolak: {arah['reason']}"
                )
                if diagnostics is not None:
                    diagnostics.reject("DIRECTION", cand.symbol, arah["reason"])
                continue
            cand.confirm_reason = (
                f"{cand.confirm_reason} | {arah['reason']}"
                if cand.confirm_reason
                else arah["reason"]
            )
        if bool(config.get("DIRECTION_ZONE_FILTER_ENABLED", False)):
            zona_arah = direction_zone_verdict(
                cand.symbol, config, direction_provider, confirm_close_time_ms
            )
            cand.direction_zone = zona_arah
            if not zona_arah["ok"]:
                cand.confirmed = False
                cand.confirm_reason = (
                    f"zona demand {zona_arah.get('interval') or '15m'} ditolak: "
                    f"{zona_arah['reason']}"
                )
                if diagnostics is not None:
                    diagnostics.reject(
                        "DIRECTION_ZONE", cand.symbol, zona_arah["reason"]
                    )
                continue
            cand.confirm_reason = (
                f"{cand.confirm_reason} | {zona_arah['reason']}"
                if cand.confirm_reason
                else zona_arah["reason"]
            )
        lolos_htf.append(cand)

    if not lolos_htf:
        return None

    diprakira: dict = {}
    if klines_fetcher_many is not None and len(lolos_htf) > 1:
        try:
            diprakira = klines_fetcher_many([cand.symbol for cand in lolos_htf]) or {}
        except Exception as exc:
            logger.debug("Pengambilan candle paralel dilewati: %s", exc)
            diprakira = {}

    lolos: list[Candidate] = []
    for cand in lolos_htf:
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
            if diagnostics is not None:
                diagnostics.reject("CANDLE_CONFIRM", cand.symbol, cand.confirm_reason)
            continue
        if diagnostics is not None:
            diagnostics.observe_signal(klines)
        signal_close_time_ms = int(klines[-1].close_time) if klines else None
        if resistance_active:
            try:
                signal_price = float(klines[-1].close) if klines else None
                resistance = strategy.evaluate_htf_resistance(
                    htf_klines_by_symbol.get(cand.symbol, []),
                    config,
                    signal_price=signal_price,
                    signal_close_time_ms=signal_close_time_ms,
                )
            except Exception as exc:
                resistance = {
                    "ok": False,
                    "interval": strategy.htf_interval(config),
                    "resistance_price": None,
                    "distance_pct": None,
                    "reason": (
                        f"evaluasi resistance HTF gagal: {exc} "
                        "(ditolak demi keamanan)"
                    ),
                }
            cand.htf_resistance = resistance
            if not resistance["ok"]:
                cand.confirmed = False
                resistance_reason = (
                    f"resistance HTF {resistance.get('interval') or '1h'} "
                    f"ditolak: {resistance['reason']}"
                )
                cand.confirm_reason = (
                    f"{cand.confirm_reason} | {resistance_reason}"
                    if cand.confirm_reason
                    else resistance_reason
                )
                if diagnostics is not None:
                    diagnostics.reject(
                        "HTF_RESISTANCE", cand.symbol, resistance["reason"]
                    )
                continue
            cand.confirm_reason = (
                f"{cand.confirm_reason} | {resistance['reason']}"
                if cand.confirm_reason
                else resistance["reason"]
            )
        if bool(config.get("TIMING_FILTER_ENABLED", False)):
            timing = timing_verdict(config, klines or [], signal_close_time_ms)
            cand.timing = timing
            if not timing["ok"]:
                cand.confirmed = False
                timing_reason = (
                    f"timing {timing.get('interval') or '5m'} ditolak: {timing['reason']}"
                )
                cand.confirm_reason = (
                    f"{cand.confirm_reason} | {timing_reason}"
                    if cand.confirm_reason
                    else timing_reason
                )
                if diagnostics is not None:
                    diagnostics.reject("TIMING", cand.symbol, timing["reason"])
                continue
            cand.confirm_reason = (
                f"{cand.confirm_reason} | {timing['reason']}"
                if cand.confirm_reason
                else timing["reason"]
            )
        hasil = detect_entry_setup(klines or [], config)
        cand.confirmed = hasil.ok
        cand.confirm_reason = (
            f"{cand.confirm_reason} | {hasil.reason}"
            if cand.confirm_reason
            else hasil.reason
        )
        cand.setup = hasil
        if not hasil.ok:
            if diagnostics is not None:
                if hasil.reason.startswith("breakout-retest ditolak"):
                    stage = "BREAKOUT_RETEST"
                elif hasil.reason.startswith("rolling volume ditolak"):
                    stage = "VOLUME_CONFIRM"
                elif hasil.reason.startswith("zona demand ditolak"):
                    stage = "DEMAND_CONFIRM"
                elif hasil.reason.startswith("data candle kurang"):
                    stage = "HISTORY_CONFIRM"
                else:
                    stage = "SETUP_CONFIRM"
                diagnostics.reject(stage, cand.symbol, hasil.reason)
            continue
        lolos.append(cand)

    if not lolos:
        return None
    lolos.sort(key=lambda c: setup_quality_key(c.setup, c))
    return lolos[0]
