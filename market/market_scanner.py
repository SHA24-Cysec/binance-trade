from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import math
from typing import Callable, Optional

from strategy import indicators as strategy
from strategy.indicators import Kline

logger = logging.getLogger("pump_bot")

STABLE_BASE_ASSETS = {
    "USDT",
    "USDC",
    "BUSD",
    "FDUSD",
    "TUSD",
    "USDP",
    "DAI",
    "AEUR",
    "EUR",
    "EURI",
    "GBP",
    "TRY",
    "BRL",
    "JPY",
    "ARS",
    "BIDR",
    "IDRT",
    "UAH",
    "PLN",
    "RON",
    "ZAR",
    "VAI",
    "UST",
    "USTC",
    "PAXG",
    "WBTC",
    "USD1",
    "USDE",
    "RLUSD",
    "BFUSD",
    "XUSD",
}

LEVERAGED_TOKEN_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
SELL_WALL_MIN_LEVELS = 3
VALID_DEPTH_LIMITS = (100, 500, 1000)
SCAN_CONFIRM_BATCH = 20


@dataclass
class SetupResult:
    ok: bool
    reason: str
    stage: str = ""
    signal_close: Optional[float] = None
    signal_close_time: Optional[int] = None
    atr_value: Optional[float] = None
    entry_interval: str = ""
    bull_count: int = 0
    bull_required: int = 0
    entry_low: Optional[float] = None
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    sl_pct: Optional[float] = None
    tp_pct: Optional[float] = None
    rr_ratio: Optional[float] = None
    turnover_ratio: Optional[float] = None
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    candle_strength: Optional[float] = None
    engulfing_strength: Optional[float] = None
    price_ema_atr: Optional[float] = None
    ema_trend_atr: Optional[float] = None


@dataclass
class Candidate:
    symbol: str
    base_asset: str
    quote_volume: float
    last_price: float
    change_pct: float = 0.0
    confirmed: bool = False
    confirm_reason: str = ""
    setup: Optional[SetupResult] = None


@dataclass
class ScanDiagnostics:
    market_symbols: int = 0
    checked_symbols: int = 0
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
            self.market_symbols,
            self.checked_symbols,
            self.latest_signal_close_ms,
            tuple(
                (stage, count, self.examples.get(stage))
                for stage, count in sorted(self.rejection_counts.items())
            ),
        )

    def log_lines(self, config: dict) -> list[str]:
        if not self.checked_symbols:
            return []
        interval = str(config.get("CONFIRM_INTERVAL", "15m"))
        signal = strategy.entry_signal_params(config)
        labels = {
            "ENTRY_RULE": f"filter zona demand + EMA {interval}",
            "CANDLE_CONFIRM": f"candle {interval} gagal diambil",
            "HISTORY_CONFIRM": f"riwayat {interval} kurang",
            "BULLISH_CANDLE": f"candle {interval} bukan bullish",
            "EMA_TREND": f"close belum di atas EMA {signal['ema_period']} pada {interval}, 1h, atau 4h",
            "DEMAND_ZONE": f"candle {interval} tidak menyentuh zona demand 1h atau 4h",
            "RISK_SL": "jarak Stop Loss zona demand",
            "VOLUME_24H": "turnover 24 jam tidak naik dibanding rata-rata",
        }
        candle = "belum tersedia"
        if self.latest_signal_close_ms > 0:
            try:
                candle = datetime.fromtimestamp(self.latest_signal_close_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S UTC")
            except (OverflowError, OSError, ValueError):
                candle = "waktu candle tidak valid"
        lines = [
            f"Diagnostik entry: {self.checked_symbols} dari {self.market_symbols} simbol pasar diperiksa; candle konfirmasi terbaru tutup {candle}.",
            "Diagnostik: setiap simbol dicatat pada gerbang pertama yang gagal.",
        ]
        if strategy.entry_rule_enabled(config):
            lines.insert(0, f"Filter zona demand + EMA AKTIF di {interval}: candle terakhir harus bullish (close > open), menyentuh zona demand 1h atau 4h yang belum ditembus, dan close di atas EMA {signal['ema_period']} pada {interval}, 1h, dan 4h.")
        for stage in sorted(self.rejection_counts):
            count = self.rejection_counts.get(stage, 0)
            if not count:
                continue
            label = labels.get(stage, f"tahap {stage}")
            symbol, reason = self.examples.get(stage, ("-", "alasan tidak tersedia"))
            lines.append(
                f"Penolakan {label}: {count} simbol; contoh {symbol}: {reason}"
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


def _angka_perubahan(value) -> Optional[float]:
    try:
        angka = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(angka):
        return None
    return angka


def _cfg_number(config: dict, key: str, default: float) -> float:
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * max(0.0, min(100.0, percentile)) / 100.0
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def dynamic_market_thresholds(changes: list[float], config: dict) -> dict:
    valid = [float(value) for value in changes if math.isfinite(float(value))]
    minimum_sample = max(5, int(_cfg_number(config, "PUMP_DYNAMIC_MIN_SAMPLE", 30)))
    if len(valid) < minimum_sample:
        return {"minimum": _cfg_number(config, "PUMP_FALLBACK_MIN_CHANGE_PCT", 0.0), "maximum": _cfg_number(config, "PUMP_FALLBACK_MAX_CHANGE_PCT", 20.0), "sample": len(valid), "fallback": True}
    raw_min = _percentile(valid, _cfg_number(config, "PUMP_DYNAMIC_MIN_PERCENTILE", 55.0))
    raw_max = _percentile(valid, _cfg_number(config, "PUMP_DYNAMIC_MAX_PERCENTILE", 95.0))
    min_floor = _cfg_number(config, "PUMP_DYNAMIC_MIN_FLOOR_PCT", -2.0)
    min_cap = _cfg_number(config, "PUMP_DYNAMIC_MIN_CAP_PCT", 5.0)
    max_floor = _cfg_number(config, "PUMP_DYNAMIC_MAX_FLOOR_PCT", 10.0)
    max_cap = _cfg_number(config, "PUMP_DYNAMIC_MAX_CAP_PCT", 35.0)
    minimum = max(min_floor, min(min_cap, raw_min))
    maximum = max(max_floor, min(max_cap, raw_max))
    return {"minimum": minimum, "maximum": max(minimum, maximum), "raw_minimum": raw_min, "raw_maximum": raw_max, "sample": len(valid), "fallback": False}


def min_change_pct(config: dict) -> float:
    try:
        nilai = float(config.get("GAINER_MIN_CHANGE_PCT", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(nilai):
        return 0.0
    return max(-100.0, nilai)


def max_change_pct(config: dict) -> float:
    try:
        nilai = float(config.get("GAINER_MAX_CHANGE_PCT", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(nilai):
        return 0.0
    return max(0.0, nilai)


def evaluate_market_gate(
    change_pct, quote_volume, config: dict
) -> tuple[bool, str]:
    if not _angka_wajar(quote_volume):
        return False, f"quote_volume 24 jam tidak wajar ({quote_volume!r})"

    perubahan = _angka_perubahan(change_pct)
    if perubahan is None:
        return False, f"perubahan harga 24 jam tidak wajar ({change_pct!r})"

    dynamic = config.get("_dynamic_market_thresholds") if isinstance(config, dict) else None
    if not isinstance(dynamic, dict) and bool(config.get("PUMP_DYNAMIC_FILTER_ENABLED", True)):
        dynamic = {"minimum": _cfg_number(config, "PUMP_FALLBACK_MIN_CHANGE_PCT", 0.0), "maximum": _cfg_number(config, "PUMP_FALLBACK_MAX_CHANGE_PCT", 20.0), "fallback": True, "sample": 0}
    batas_bawah = float(dynamic["minimum"]) if isinstance(dynamic, dict) else min_change_pct(config)
    if perubahan < batas_bawah:
        return False, (
            f"kenaikan 24 jam {perubahan:.2f}% di bawah ambang top gainer "
            f"{batas_bawah:.2f}%"
        )

    batas_atas = float(dynamic["maximum"]) if isinstance(dynamic, dict) else max_change_pct(config)
    if batas_atas > 0.0 and perubahan > batas_atas:
        return False, (
            f"kenaikan 24 jam {perubahan:.2f}% melampaui batas {batas_atas:.2f}%, "
            "koin sudah terlalu jauh untuk dikejar"
        )

    return True, (
        f"lolos filter pasar (naik {perubahan:.2f}%, turnover "
        f"{float(quote_volume):.0f} USDT)"
    )


def market_gate_ok_at(change_pct, quote_volume, config: dict) -> bool:
    ok, _r = evaluate_market_gate(change_pct, quote_volume, config)
    return ok


def filter_eligible_candidates(
    tickers: list,
    config: dict,
    tradable_symbols: "set | None" = None,
    *,
    apply_market_gate: bool = True,
) -> list[Candidate]:
    quote_asset = config["QUOTE_ASSET"]
    min_vol = float(config.get("MIN_QUOTE_VOLUME_USDT_24H", 0) or 0)

    lolos_likuid = 0
    ditolak_gain = 0
    out: list[Candidate] = []
    gate_config = config
    if bool(config.get("PUMP_DYNAMIC_FILTER_ENABLED", True)):
        liquid_changes = []
        for ticker in tickers:
            try:
                ticker_symbol = str(ticker.get("symbol", ""))
                ticker_volume = float(ticker.get("quoteVolume", 0.0))
                ticker_change = float(ticker.get("priceChangePercent"))
            except (TypeError, ValueError):
                continue
            if is_structurally_allowed_symbol(ticker_symbol, config, tradable_symbols) and math.isfinite(ticker_volume) and ticker_volume >= min_vol and math.isfinite(ticker_change):
                liquid_changes.append(ticker_change)
        gate_config = dict(config)
        gate_config["_dynamic_market_thresholds"] = dynamic_market_thresholds(liquid_changes, config)

    for t in tickers:
        symbol = t.get("symbol", "")
        if not is_structurally_allowed_symbol(symbol, config, tradable_symbols):
            continue

        base_asset = symbol[: -len(quote_asset)]

        try:
            quote_volume = float(t["quoteVolume"])
            last_price = float(t["lastPrice"])
        except (KeyError, ValueError, TypeError):
            continue

        perubahan = _angka_perubahan(t.get("priceChangePercent"))

        if not math.isfinite(last_price) or last_price <= 0:
            continue
        if not _angka_wajar(quote_volume):
            continue
        if quote_volume < min_vol:
            continue
        if perubahan is None:
            continue

        lolos_likuid += 1

        if apply_market_gate:
            ok_market, alasan = evaluate_market_gate(perubahan, quote_volume, gate_config)
            if not ok_market:
                ditolak_gain += 1
                logger.debug("Filter pasar menolak %s: %s", symbol, alasan)
                continue

        out.append(
            Candidate(
                symbol=symbol,
                base_asset=base_asset,
                quote_volume=quote_volume,
                last_price=last_price,
                change_pct=perubahan,
            )
        )

    out.sort(key=lambda c: (-c.change_pct, -c.quote_volume, c.symbol))

    if apply_market_gate:
        logger.info(
            "Saringan pasar: %d simbol lolos dari %d simbol likuid "
            "(%d ditolak karena ambang kenaikan).",
            len(out),
            lolos_likuid,
            ditolak_gain,
        )
        logger.debug("Simbol lolos saringan likuiditas 24 jam: %d", lolos_likuid)
    return out


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


def detect_entry_setup(
    klines: list[Kline],
    config: dict,
    signal_close_time_ms: Optional[int] = None,
) -> SetupResult:
    required = strategy.required_lookback_bars(config)
    if len(klines) < required:
        return SetupResult(
            ok=False,
            stage="HISTORY_ENTRY",
            reason=f"data candle kurang: {len(klines)} dari minimum {required}",
        )
    closes = [float(k.close) for k in klines]
    if any(x <= 0 or not math.isfinite(x) for x in closes):
        return SetupResult(
            ok=False, stage="CANDLE_ENTRY", reason="close candle tidak valid"
        )

    last_bar = klines[-1]
    syarat_volume = strategy.volume_rise_check(klines, config)
    if not syarat_volume["ok"]:
        return SetupResult(
            ok=False,
            stage=str(syarat_volume["stage"]),
            reason=str(syarat_volume["reason"]),
            entry_interval=str(strategy.momentum_interval(config)),
            turnover_ratio=syarat_volume["ratio"],
        )
    verdict = strategy.evaluate_entry(klines, config, signal_close_time_ms)
    atr_value = verdict.get("atr")
    if atr_value is None:
        atr_value = strategy.atr(klines, strategy.atr_period(config))
    strength_val = verdict.get("candle_strength")
    if strength_val is None:
        strength_val = verdict.get("engulfing_strength")
    return SetupResult(
        ok=bool(verdict.get("ok", False)),
        stage=str(verdict.get("stage") or "BULLISH_CANDLE"),
        reason=str(verdict.get("reason") or ""),
        signal_close=float(verdict.get("signal_close") or last_bar.close),
        signal_close_time=int(
            verdict.get("signal_close_time") or last_bar.close_time
        ),
        atr_value=atr_value,
        entry_interval=str(verdict.get("interval") or ""),
        bull_count=int(verdict.get("bull_count") or 0),
        bull_required=int(verdict.get("bull_required") or 0),
        entry_low=verdict.get("entry_low"),
        sl_price=verdict.get("sl_price"),
        tp_price=verdict.get("tp_price"),
        sl_pct=verdict.get("sl_pct"),
        tp_pct=verdict.get("tp_pct"),
        rr_ratio=verdict.get("rr_ratio"),
        turnover_ratio=syarat_volume.get("ratio"),
        ema_fast=verdict.get("ema_fast"),
        ema_slow=verdict.get("ema_slow"),
        candle_strength=strength_val,
        engulfing_strength=strength_val,
        price_ema_atr=verdict.get("price_ema_atr"),
        ema_trend_atr=verdict.get("ema_trend_atr"),
    )


STAGE_GROUPS = {
    "HISTORY_ENTRY": "HISTORY_ENTRY",
    "CANDLE_ENTRY": "CANDLE_ENTRY",
    "HISTORY_CONFIRM": "HISTORY_ENTRY",
    "CANDLE_CONFIRM": "CANDLE_ENTRY",
    "BULLISH_CANDLE": "BULLISH_CANDLE",
    "EMA_TREND": "EMA_TREND",
    "DEMAND_ZONE": "DEMAND_ZONE",
    "VOLUME_24H": "VOLUME_24H",
    "RISK_SL": "RISK_SL",
    "ENTRY_RULE": "ENTRY_RULE",
    "DISABLED": "ENTRY_RULE",
    "OK": "ENTRY_RULE",
}


def diagnostic_stage(stage: str, reason: str) -> str:
    kunci = str(stage or "").strip().upper()
    if kunci in STAGE_GROUPS:
        return STAGE_GROUPS[kunci]
    if str(reason or "").startswith("data candle kurang"):
        return "HISTORY_ENTRY"
    return "BULLISH_CANDLE"


def scan_entry_candidate(
    tickers: list,
    klines_fetcher,
    config: dict,
    tradable_symbols: "set | None" = None,
    klines_fetcher_many: "Optional[Callable[[list], dict]]" = None,
    prewarm_fn: "Optional[Callable[[list], None]]" = None,
    diagnostics: Optional[ScanDiagnostics] = None,
    confirm_close_time_ms: "int | None" = None,
) -> Optional[Candidate]:
    eligible = filter_eligible_candidates(tickers, config, tradable_symbols)
    if diagnostics is not None:
        diagnostics.market_symbols = len(eligible)
        diagnostics.checked_symbols = 0
        diagnostics.latest_signal_close_ms = 0
        diagnostics.rejection_counts.clear()
        diagnostics.examples.clear()
    if not eligible:
        return None

    for start in range(0, len(eligible), SCAN_CONFIRM_BATCH):
        batch = eligible[start : start + SCAN_CONFIRM_BATCH]
        if prewarm_fn is not None:
            try:
                prewarm_fn([cand.symbol for cand in batch])
            except Exception as exc:
                logger.debug("Pemanasan harga simbol dilewati: %s", exc)

        diprakira: dict = {}
        if klines_fetcher_many is not None and len(batch) > 1:
            try:
                diprakira = klines_fetcher_many([cand.symbol for cand in batch]) or {}
            except Exception as exc:
                logger.debug("Pengambilan candle paralel dilewati: %s", exc)
                diprakira = {}

        for cand in batch:
            if diagnostics is not None:
                diagnostics.checked_symbols += 1
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
                    diagnostics.reject(
                        "CANDLE_CONFIRM", cand.symbol, cand.confirm_reason
                    )
                continue
            if diagnostics is not None:
                diagnostics.observe_signal(klines)

            hasil = detect_entry_setup(
                klines or [],
                config,
                signal_close_time_ms=confirm_close_time_ms,
            )
            cand.confirmed = hasil.ok
            cand.confirm_reason = hasil.reason
            cand.setup = hasil
            if hasil.ok:
                return cand
            if diagnostics is not None:
                diagnostics.reject(
                    diagnostic_stage(hasil.stage, hasil.reason),
                    cand.symbol,
                    hasil.reason,
                )

    return None
