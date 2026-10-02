"""Scanner pasar dan filter operasional Binance Spot.

Modul ini menyediakan filter semesta, gerbang pump, likuiditas, spread, usia
listing, dan korelasi BTC, serta konfirmasi volume rolling yang dipakai bot
dan backtest untuk membuka posisi baru. Tidak ada indikator teknikal (EMA,
RSI, MACD, higher low) pada jalur entry.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, Optional


from strategy import indicators as strategy
from strategy.indicators import Kline

logger = logging.getLogger(__name__)

PUMP_GATE_DAILY_CANDLES = 7

MS_PER_DAY = 86_400_000

STABLE_BASE_ASSETS = {
    "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "EUR", "GBP", "TRY",
    "BRL", "AEUR", "USTC", "USDD", "PYUSD", "USDE",
    "USD1", "RLUSD",
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


def _looks_leveraged(base_asset: str) -> bool:
    for sfx in LEVERAGED_TOKEN_SUFFIXES:
        if base_asset.endswith(sfx) and len(base_asset) - len(sfx) >= 2:
            return True
    return False


def is_structurally_allowed_symbol(symbol: str, config: dict,
                                   tradable_symbols: "set | None" = None) -> bool:
    quote_asset = str(config.get("QUOTE_ASSET", ""))
    if not quote_asset or not symbol.endswith(quote_asset):
        return False
    if symbol in set(config.get("EXTRA_EXCLUDE_SYMBOLS", [])):
        return False
    if tradable_symbols is not None and symbol not in tradable_symbols:
        return False
    base_asset = symbol[: -len(quote_asset)]
    return bool(base_asset and base_asset not in STABLE_BASE_ASSETS
                and not _looks_leveraged(base_asset))


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


def aggregate_to_daily(klines: list[Kline]) -> list[Kline]:
    ember: dict[int, list[Kline]] = {}
    for k in klines:
        ember.setdefault(int(k.open_time) // MS_PER_DAY, []).append(k)

    harian: list[Kline] = []
    for hari in sorted(ember):
        isi = ember[hari]
        harian.append(Kline(
            open_time=hari * MS_PER_DAY,
            open=isi[0].open,
            high=max(x.high for x in isi),
            low=min(x.low for x in isi),
            close=isi[-1].close,
            close_time=hari * MS_PER_DAY + MS_PER_DAY - 1,
            volume=sum(x.volume for x in isi),
            quote_volume=sum(x.quote_volume for x in isi),
        ))
    return harian


def average_prior_daily_quote_volume(
    daily_klines: "list[Kline] | None",
    reference_ms: int,
    need: int = PUMP_GATE_DAILY_CANDLES,
) -> tuple[Optional[float], str]:
    if not daily_klines:
        return None, "tidak ada candle harian"

    tertutup = [k for k in daily_klines if int(k.close_time) <= int(reference_ms)]
    if len(tertutup) < need:
        return None, (f"riwayat harian kurang: {len(tertutup)} candle tertutup, "
                      f"minimum {need} (kemungkinan koin baru listing)")

    dipakai = tertutup[-need:]
    volumes: list[float] = []
    for k in dipakai:
        if not _angka_wajar(k.quote_volume):
            return None, ("quote_volume candle harian tidak wajar "
                          f"({k.quote_volume!r}), data bursa rusak")
        volumes.append(float(k.quote_volume))

    return sum(volumes) / float(need), f"rata-rata {need} hari penuh terakhir"


def evaluate_pump_gate(price_change_pct, quote_volume,
                       avg_daily_quote_volume: Optional[float],
                       config: dict, btc_drop_pct: float | None = None) -> tuple[bool, str]:
    min_change = float(config.get("PUMP_MIN_24H_CHANGE_PCT", 10.0) or 0.0)
    max_change = float(config.get("PUMP_MAX_24H_CHANGE_PCT", 0.0) or 0.0)
    surge_mult = float(config.get("PUMP_VOLUME_SURGE_MULT", 1.5) or 0.0)

    if btc_drop_pct is None:
        btc_drop_pct = config.get("_btc_drop_pct")
    if config.get("BTC_FILTER_ENABLED", False):
        if btc_drop_pct is None:
            if config.get("_btc_filter_fail_closed", False):
                return False, "data filter BTC tidak tersedia, simbol ditolak (fail closed)"
        else:
            max_drop = abs(float(config.get("BTC_MAX_DROP_PCT", 5.0) or 0.0))
            if float(btc_drop_pct) <= -max_drop:
                return False, (f"BTC turun {float(btc_drop_pct):.2f}% dalam "
                               f"{int(config.get("BTC_LOOKBACK_BARS", 3) or 3)} candle")

    if not _angka_wajar(quote_volume):
        return False, f"quote_volume 24 jam tidak wajar ({quote_volume!r})"
    try:
        change = float(price_change_pct)
    except (TypeError, ValueError):
        return False, f"priceChangePercent tidak bisa dibaca ({price_change_pct!r})"
    if math.isnan(change) or math.isinf(change):
        return False, f"priceChangePercent tidak wajar ({price_change_pct!r})"

    if change < min_change:
        return False, (f"kenaikan 24 jam {change:.2f}% di bawah ambang "
                       f"{min_change:g}%")
    if max_change > 0 and change > max_change:
        return False, (f"kenaikan 24 jam {change:.2f}% melewati batas atas "
                       f"{max_change:g}% (koin sudah terlalu tinggi)")

    if avg_daily_quote_volume is None:
        return False, "rata-rata volume harian tidak tersedia"
    if not _angka_wajar(avg_daily_quote_volume):
        return False, f"rata-rata volume harian tidak wajar ({avg_daily_quote_volume!r})"
    if avg_daily_quote_volume <= 0:
        return False, "rata-rata volume harian nol, perbandingan volume tidak bermakna"

    butuh = surge_mult * float(avg_daily_quote_volume)
    rasio = float(quote_volume) / float(avg_daily_quote_volume)
    if float(quote_volume) < butuh:
        return False, (f"volume 24 jam {float(quote_volume):.0f} hanya {rasio:.2f}x "
                       f"rata-rata 7 hari {float(avg_daily_quote_volume):.0f}, "
                       f"minimum {surge_mult:g}x")

    rentang = f"{min_change:g}% sampai {max_change:g}%" if max_change > 0 else f">= {min_change:g}%"
    return True, (f"pump sah: naik {change:.2f}% (rentang {rentang}), "
                  f"volume {rasio:.2f}x rata-rata 7 hari (ambang {surge_mult:g}x)")


def is_pumping_today(symbol: str, price_change_pct, quote_volume,
                     get_daily_klines_fn: "Optional[Callable[[str], list[Kline]]]",
                     config: dict,
                     reference_ms: "int | None" = None) -> tuple[bool, str]:
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
        return False, (f"kenaikan 24 jam {change:.2f}% melewati batas atas "
                       f"{max_change:g}% (koin sudah terlalu tinggi)")

    if get_daily_klines_fn is None:
        return False, "sumber candle harian tidak tersedia, simbol ditolak (fail closed)"

    ref = int(reference_ms) if reference_ms is not None else int(time.time() * 1000)

    try:
        harian = get_daily_klines_fn(symbol)
    except Exception as exc:
        return False, f"gagal mengambil candle harian: {str(exc)[:160]}"

    rata, alasan = average_prior_daily_quote_volume(harian, ref)
    if rata is None:
        return False, alasan
    return evaluate_pump_gate(change, quote_volume, rata, config)


def pump_gate_ok_at(daily_klines: "list[Kline] | None", reference_ms: int,
                    price_change_pct, quote_volume, config: dict,
                    btc_drop_pct: "float | None" = None) -> bool:
    rata, _alasan = average_prior_daily_quote_volume(daily_klines, reference_ms)
    ok, _r = evaluate_pump_gate(price_change_pct, quote_volume, rata, config,
                                btc_drop_pct=btc_drop_pct)
    return ok


def make_daily_klines_fetcher(client, *, limit: int = PUMP_GATE_DAILY_CANDLES + 1,
                              end_time_ms: "int | None" = None,
                              cache: "dict | None" = None):
    def _fetch(symbol: str) -> list[Kline]:
        if cache is not None and symbol in cache:
            return cache[symbol]
        raw = client.get_klines(symbol, interval="1d", limit=limit,
                                end_time_ms=end_time_ms)
        parsed = strategy.parse_klines(raw)
        if cache is not None:
            cache[symbol] = parsed
        return parsed

    return _fetch


def filter_and_rank_candidates(tickers: list, config: dict,
                               tradable_symbols: "set | None" = None,
                               *,
                               get_daily_klines_fn: "Optional[Callable[[str], list[Kline]]]" = None,
                               reference_ms: "int | None" = None,
                               apply_pump_gate: bool = True) -> list[Candidate]:
    quote_asset = config["QUOTE_ASSET"]
    min_vol = float(config.get("MIN_QUOTE_VOLUME_USDT_24H", 0) or 0)

    if apply_pump_gate and get_daily_klines_fn is None:
        logger.warning(
            "Gerbang pump aktif tetapi sumber candle harian tidak diberikan. "
            "Semua simbol ditolak (fail closed).")

    lolos_struktural = 0
    out = []
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
                symbol, price_change_pct, quote_volume,
                get_daily_klines_fn, config, reference_ms=reference_ms)
            if not ok_pump:
                logger.debug("Gerbang pump menolak %s: %s", symbol, alasan)
                continue

        out.append(Candidate(
            symbol=symbol, base_asset=base_asset, price_change_pct=price_change_pct,
            quote_volume=quote_volume, last_price=last_price,
        ))

    out.sort(key=lambda c: c.quote_volume, reverse=True)

    if apply_pump_gate:
        if out:
            logger.info("Gerbang pump: %d kandidat lolos dari %d simbol yang lolos "
                        "saringan likuiditas.", len(out), lolos_struktural)
        else:
            logger.info("Gerbang pump: 0 kandidat lolos gerbang pump (dari %d simbol "
                        "yang lolos saringan likuiditas). Ambang: naik %g%% sampai %s "
                        "dan volume >= %gx rata-rata 7 hari.",
                        lolos_struktural,
                        float(config.get("PUMP_MIN_24H_CHANGE_PCT", 10.0) or 0.0),
                        (f"{float(config.get('PUMP_MAX_24H_CHANGE_PCT', 0.0) or 0.0):g}%"
                         if float(config.get("PUMP_MAX_24H_CHANGE_PCT", 0.0) or 0.0) > 0
                         else "tanpa batas atas"),
                        float(config.get("PUMP_VOLUME_SURGE_MULT", 1.5) or 0.0))
    return out


# ---------------------------------------------------------------------------
# Kedalaman dan order book (hanya PAPER dan LIVE, tidak ada di backtest)
# ---------------------------------------------------------------------------

SELL_WALL_MIN_LEVELS = 3
VALID_DEPTH_LIMITS = (100, 500, 1000)


def normalize_depth_limit(value) -> int:
    """Bulatkan ke atas ke limit depth yang valid di Binance (100, 500, 1000)."""
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return 500
    for limit in VALID_DEPTH_LIMITS:
        if v <= limit:
            return limit
    return VALID_DEPTH_LIMITS[-1]


def _parse_levels(raw) -> list[tuple[float, float]]:
    """Ubah daftar [harga, qty] jadi tuple float. Level rusak dibuang."""
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


def evaluate_orderbook(depth, planned_notional: float, config: dict) -> tuple[bool, str, dict]:
    """Nilai snapshot order book sebelum entry BUY.

    Tiga pemeriksaan, semuanya fail closed bila data kosong atau rusak:
    1. Kedalaman: total nilai ask dalam DEPTH_RANGE_PCT dari ask terbaik
       harus >= DEPTH_MIN_ASK_NOTIONAL_MULT x nilai order.
    2. Ketimpangan: total nilai bid di ORDERBOOK_LEVELS level teratas harus
       >= ORDERBOOK_MIN_BID_ASK_RATIO x total nilai ask di level teratas.
    3. Sell wall: tidak ada satu level ask di dalam SELL_WALL_RANGE_PCT yang
       bernilai lebih dari SELL_WALL_MAX_SHARE_PCT dari total ask di rentang itu
       (hanya dinilai bila ada minimal SELL_WALL_MIN_LEVELS level di rentang).

    Mengembalikan (lolos, alasan, metrik).
    """
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
        return False, (f"order book tidak wajar (bid {best_bid:g} >= ask {best_ask:g})"), metrics
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
        metrics.update(ask_depth_notional=total, ask_depth_required=need,
                       ask_depth_range_pct=rng, ask_depth_truncated=truncated)
        if total < need:
            extra = " (snapshot terpotong sebelum batas rentang)" if truncated else ""
            return False, (f"kedalaman ask {rng:g}% hanya {total:,.0f} USDT, butuh "
                           f"{need:,.0f} USDT ({mult:g}x order {planned:,.0f}){extra}"), metrics

    if book_on:
        n = max(1, int(config.get("ORDERBOOK_LEVELS", 10) or 10))
        min_ratio = float(config.get("ORDERBOOK_MIN_BID_ASK_RATIO", 0.8) or 0.0)
        bid_n = sum(p * q for p, q in bids[:n])
        ask_n = sum(p * q for p, q in asks[:n])
        ratio = bid_n / ask_n if ask_n > 0 else float("inf")
        metrics.update(bid_top_notional=bid_n, ask_top_notional=ask_n,
                       bid_ask_ratio=ratio, orderbook_levels=n)
        if ratio < min_ratio:
            return False, (f"tekanan jual: bid {n} level teratas {bid_n:,.0f} USDT hanya "
                           f"{ratio:.2f}x ask {ask_n:,.0f} USDT, minimum {min_ratio:g}x"), metrics

        wall_rng = float(config.get("SELL_WALL_RANGE_PCT", 1.0) or 0.0)
        max_share = float(config.get("SELL_WALL_MAX_SHARE_PCT", 30.0) or 0.0) / 100.0
        inside, truncated = _range(wall_rng)
        total = sum(p * q for p, q in inside)
        metrics.update(wall_range_pct=wall_rng, wall_range_levels=len(inside),
                       wall_range_notional=total)
        if len(inside) >= SELL_WALL_MIN_LEVELS and total > 0 and max_share > 0:
            wall_price, wall_qty = max(inside, key=lambda x: x[0] * x[1])
            wall_val = wall_price * wall_qty
            share = wall_val / total
            metrics.update(wall_price=wall_price, wall_notional=wall_val, wall_share=share)
            if share > max_share:
                return False, (f"sell wall di {wall_price:g}: {wall_val:,.0f} USDT = "
                               f"{share * 100:.0f}% dari ask {wall_rng:g}% "
                               f"({total:,.0f} USDT), maksimum {max_share * 100:g}%"), metrics

    return True, (f"order book sehat: ask {metrics.get('ask_depth_notional', 0):,.0f} USDT, "
                  f"bid/ask {metrics.get('bid_ask_ratio', 0):.2f}x, "
                  f"wall {metrics.get('wall_share', 0) * 100:.0f}%"), metrics


@dataclass
class SetupResult:
    ok: bool
    reason: str
    signal_close: Optional[float] = None
    atr_value: Optional[float] = None

def _rolling_volume_confirmation(klines: list[Kline], config: dict) -> tuple[bool, str]:
    if not bool(config.get("ROLLING_VOLUME_FILTER_ENABLED", True)):
        return True, "rolling volume nonaktif"

    lookback = max(1, int(config.get("ROLLING_VOLUME_LOOKBACK_BARS", 20) or 20))
    confirmations = max(1, int(config.get("ROLLING_VOLUME_CONFIRMATION_BARS", 1) or 1))
    multiplier = float(config.get("ROLLING_VOLUME_SURGE_MULT", 2.0) or 0.0)
    required = lookback + confirmations
    if len(klines) < required:
        return False, f"data volume rolling kurang: {len(klines)} dari minimum {required}"
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
        prior = values[idx - lookback:idx]
        average = sum(prior) / lookback
        current = values[idx]
        if average <= 0:
            return False, "rata-rata volume rolling nol"
        ratio = current / average
        ratios.append(ratio)
        if ratio >= multiplier:
            passed += 1

    ok = passed == confirmations
    detail = (f"rolling volume {min(ratios):.2f}x, minimum {multiplier:g}x, "
              f"{passed}/{confirmations} candle konfirmasi")
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
    return SetupResult(
        True, f"entry sah: gerbang pump lolos, {volume_detail}",
        signal_close=klines[-1].close,
        atr_value=strategy.atr(klines, int(config.get("ATR_PERIOD", 14) or 14)))

def setup_quality_key(setup: SetupResult, candidate: Candidate) -> tuple:
    return (-float(candidate.quote_volume),)

def find_best_candidate(tickers: list, klines_fetcher, config: dict,
                        tradable_symbols: "set | None" = None,
                        daily_klines_fetcher=None,
                        reference_ms: "int | None" = None) -> Optional[Candidate]:
    ranked = filter_and_rank_candidates(
        tickers, config, tradable_symbols,
        get_daily_klines_fn=daily_klines_fetcher, reference_ms=reference_ms)
    top_n = ranked[: int(config.get("TOP_N_CANDIDATES_TO_CONFIRM", 10) or 10)]

    lolos: list[Candidate] = []
    for cand in top_n:
        try:
            klines = klines_fetcher(cand.symbol)
        except Exception as exc:
            cand.confirmed = False
            cand.confirm_reason = f"gagal mengambil candle: {exc}"
            continue
        hasil = detect_entry_setup(klines or [], config)
        cand.confirmed = hasil.ok
        cand.confirm_reason = hasil.reason
        cand.setup = hasil
        if hasil.ok:
            lolos.append(cand)

    if not lolos:
        return None
    lolos.sort(key=lambda c: setup_quality_key(c.setup, c))
    return lolos[0]
