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


@dataclass
class Candidate:
    symbol: str
    base_asset: str
    quote_volume: float
    last_price: float
    confirmed: bool = False
    confirm_reason: str = ""
    setup: Optional[SetupResult] = None


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
        bull = strategy.entry_bull_candles(config)
        labels = {
            "ENTRY_RULE": f"aturan {bull} candle bullish {interval}",
            "CANDLE_CONFIRM": f"candle {interval} gagal diambil",
            "HISTORY_CONFIRM": f"riwayat {interval} kurang",
            "BULL_SEQUENCE": f"rangkaian candle bullish {interval}",
            "RISK_SL": "jarak Stop Loss",
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
        if strategy.entry_rule_enabled(config):
            lines.insert(
                0,
                f"Status aturan entry ({interval}): AKTIF, butuh {bull} candle bullish "
                "beruntun tertutup; kegagalan data menolak entry.",
            )
        for stage in sorted(self.rejection_counts):
            count = self.rejection_counts.get(stage, 0)
            if not count:
                continue
            label = labels.get(stage, f"tahap {stage}")
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


def evaluate_market_gate(*args) -> tuple[bool, str]:
    if len(args) == 2:
        quote_volume, config = args
    elif len(args) == 3 and isinstance(args[1], dict):
        quote_volume, config, _unused = args
    elif len(args) == 3:
        _unused_change, quote_volume, config = args
    else:
        raise TypeError("Argumen evaluate_market_gate tidak sesuai.")

    if not _angka_wajar(quote_volume):
        return False, f"quote_volume 24 jam tidak wajar ({quote_volume!r})"

    return True, "filter pasar lolos"


def market_gate_ok_at(*args) -> bool:
    ok, _r = evaluate_market_gate(*args)
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
            quote_volume = float(t["quoteVolume"])
            last_price = float(t["lastPrice"])
        except (KeyError, ValueError, TypeError):
            continue

        if not math.isfinite(last_price) or last_price <= 0:
            continue
        if not _angka_wajar(quote_volume):
            continue
        if quote_volume < min_vol:
            continue

        lolos_struktural += 1

        if apply_market_gate:
            ok_market, alasan = evaluate_market_gate(quote_volume, config)
            if not ok_market:
                logger.debug("Filter pasar menolak %s: %s", symbol, alasan)
                continue

        out.append(
            Candidate(
                symbol=symbol,
                base_asset=base_asset,
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
    verdict = strategy.evaluate_entry(klines, config, signal_close_time_ms)
    atr_value = strategy.atr(klines, 14)
    return SetupResult(
        ok=bool(verdict.get("ok", False)),
        stage=str(verdict.get("stage") or "BULL_SEQUENCE"),
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
    )


STAGE_GROUPS = {
    "HISTORY_ENTRY": "HISTORY_ENTRY",
    "CANDLE_ENTRY": "CANDLE_ENTRY",
    "HISTORY_CONFIRM": "HISTORY_ENTRY",
    "CANDLE_CONFIRM": "CANDLE_ENTRY",
    "BULL_SEQUENCE": "ENTRY_RULE",
    "RISK_SL": "ENTRY_RULE",
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
    return "ENTRY_RULE"


def setup_quality_key(setup: Optional[SetupResult], candidate: Candidate) -> tuple:
    return (-float(candidate.quote_volume),)


def find_best_candidate(
    tickers: list,
    klines_fetcher,
    config: dict,
    tradable_symbols: "set | None" = None,
    klines_fetcher_many: "Optional[Callable[[list], dict]]" = None,
    prewarm_fn: "Optional[Callable[[list], None]]" = None,
    diagnostics: Optional[ScanDiagnostics] = None,
    confirm_close_time_ms: "int | None" = None,
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

    lolos_struktur: list[Candidate] = list(top_n)

    if not lolos_struktur:
        return None

    diprakira: dict = {}
    if klines_fetcher_many is not None and len(lolos_struktur) > 1:
        try:
            diprakira = (
                klines_fetcher_many([cand.symbol for cand in lolos_struktur]) or {}
            )
        except Exception as exc:
            logger.debug("Pengambilan candle paralel dilewati: %s", exc)
            diprakira = {}

    lolos: list[Candidate] = []
    for cand in lolos_struktur:
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

        hasil = detect_entry_setup(
            klines or [],
            config,
            signal_close_time_ms=confirm_close_time_ms,
        )
        cand.confirmed = hasil.ok
        cand.confirm_reason = hasil.reason
        cand.setup = hasil
        if not hasil.ok:
            if diagnostics is not None:
                diagnostics.reject(
                    diagnostic_stage(hasil.stage, hasil.reason),
                    cand.symbol,
                    hasil.reason,
                )
            continue
        lolos.append(cand)

    if not lolos:
        return None
    lolos.sort(key=lambda c: setup_quality_key(c.setup, c))
    return lolos[0]
