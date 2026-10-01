"""Scanner pasar dan filter operasional Binance Spot.

Modul ini menyediakan filter semesta, gerbang pump, likuiditas, spread, usia
listing, dan korelasi BTC, serta deteksi setup entry (pullback-retest) dan
skor kualitas setup yang dipakai bot dan backtest untuk membuka posisi baru.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional


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

    return True, (f"pump sah: naik {change:.2f}% (ambang {min_change:g}%), "
                  f"volume {rasio:.2f}x rata-rata 7 hari (ambang {surge_mult:g}x)")


def is_pumping_today(symbol: str, price_change_pct, quote_volume,
                     get_daily_klines_fn: "Optional[Callable[[str], list[Kline]]]",
                     config: dict,
                     reference_ms: "int | None" = None) -> tuple[bool, str]:
    min_change = float(config.get("PUMP_MIN_24H_CHANGE_PCT", 10.0) or 0.0)
    try:
        change = float(price_change_pct)
    except (TypeError, ValueError):
        return False, f"priceChangePercent tidak bisa dibaca ({price_change_pct!r})"
    if math.isnan(change) or math.isinf(change):
        return False, f"priceChangePercent tidak wajar ({price_change_pct!r})"
    if change < min_change:
        return False, f"kenaikan 24 jam {change:.2f}% di bawah ambang {min_change:g}%"

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
                    price_change_pct, quote_volume, config: dict) -> bool:
    rata, _alasan = average_prior_daily_quote_volume(daily_klines, reference_ms)
    ok, _r = evaluate_pump_gate(price_change_pct, quote_volume, rata, config)
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
                        "yang lolos saringan likuiditas). Ambang: naik >= %g%% "
                        "dan volume >= %gx rata-rata 7 hari.",
                        lolos_struktural,
                        float(config.get("PUMP_MIN_24H_CHANGE_PCT", 10.0) or 0.0),
                        float(config.get("PUMP_VOLUME_SURGE_MULT", 1.5) or 0.0))
    return out


class EntrySignalScore(NamedTuple):
    score: float
    status: str
    disqualified: Optional[str]
    components: dict
    reason: str

def score_entry_signal(klines: list[Kline], config: dict, meta=None) -> EntrySignalScore:
    zero = {"ema": 0.0, "rsi": 0.0, "macd": 0.0, "higher_low": 0.0}
    passed, detail = _rolling_volume_confirmation(klines, config)
    if not passed:
        return EntrySignalScore(0.0, "TIDAK LOLOS", detail, zero, detail)
    closes = [float(k.close) for k in klines]
    if len(closes) < max(30, strategy.required_lookback_bars(config)):
        return EntrySignalScore(0.0, "TIDAK LOLOS", "data candle kurang", zero, "data candle kurang")
    w = {"ema": float(config.get("WATCH" + "LIST_ENTRY_WEIGHT_EMA", 25)),
         "rsi": float(config.get("WATCH" + "LIST_ENTRY_WEIGHT_RSI", 25)),
         "macd": float(config.get("WATCH" + "LIST_ENTRY_WEIGHT_MACD", 25)),
         "higher_low": float(config.get("WATCH" + "LIST_ENTRY_WEIGHT_HL", 25))}
    ema9, ema21 = strategy.ema(closes, 9), strategy.ema(closes, 21)
    cross = ema9[-2] <= ema21[-2] and ema9[-1] > ema21[-1]
    if cross: ema_credit = w["ema"]
    elif ema9[-1] > ema21[-1]: ema_credit = w["ema"] * 0.6
    else:
        gap = float(config.get("WATCH" + "LIST_ENTRY_EMA_GAP_PCT", 1.0))
        rel = (ema21[-1] - ema9[-1]) / ema21[-1] * 100
        ema_credit = w["ema"] * max(0.0, 1.0 - rel / gap) if gap > 0 else 0.0
    rsi = strategy.rsi(closes, 14)[-1]
    decay = float(config.get("WATCH" + "LIST_ENTRY_RSI_DECAY_PTS", 15))
    rsi_credit = w["rsi"] if 50 <= rsi <= 75 else w["rsi"] * max(0.0, 1 - (50-rsi if rsi < 50 else rsi-75) / decay)
    _, _, hist = strategy.macd(closes)
    improving = len(hist) >= 2 and (hist[-1] > hist[-2] or (hist[-2] <= 0 < hist[-1]))
    slowing = len(hist) >= 3 and hist[-1] < hist[-2] and (hist[-1]-hist[-2]) > (hist[-2]-hist[-3])
    macd_credit = w["macd"] if improving else (w["macd"] * 0.4 if slowing else 0.0)
    hl = _higher_low_confirmed(klines, max(1, int(config.get("SWING_PIVOT_WING_BARS", 2) or 2)))
    components = {"ema": round(ema_credit, 1), "rsi": round(rsi_credit, 1), "macd": round(macd_credit, 1), "higher_low": w["higher_low"] if hl else 0.0}
    raw = round(sum(components.values()), 1)
    dq = None
    if meta:
        spread = meta.get("spread_pct")
        if spread is not None and spread > float(config.get("MAX_SPREAD_PCT", .25)): dq = "spread melewati batas"
        if meta.get("weekend_pct") is not None and str(meta.get("symbol", "")).endswith("B") and meta["weekend_pct"] < 16: dq = "bStocks tidak berjalan 24/7"
    status = "SIAP" if raw >= 75 else "MENDEKAT" if raw >= 50 else "AWAL" if raw >= 25 else "JAUH"
    return EntrySignalScore(0.0 if dq else raw, "TIDAK LOLOS" if dq else status, dq, components, f"EMA={'ya' if cross else 'tidak'}, RSI={rsi:.2f}, MACD={'naik' if improving else 'tidak'}, HL={'ya' if hl else 'tidak'}; {detail}")

@dataclass
class SetupResult:
    ok: bool
    reason: str
    breakout_level: Optional[float] = None
    zone_low: Optional[float] = None
    zone_high: Optional[float] = None
    anchor_index: Optional[int] = None
    retest_touches: int = 0
    atr_value: Optional[float] = None

def _pivot_low_indexes(klines: list[Kline], wing: int) -> list[int]:
    if wing < 1 or len(klines) < 2 * wing + 1:
        return []
    return [p for p in range(wing, len(klines) - wing)
            if all(klines[p].low < klines[j].low for j in range(p-wing, p))
            and all(klines[p].low < klines[j].low for j in range(p+1, p+wing+1))]

def _higher_low_confirmed(klines: list[Kline], wing: int) -> bool:
    pivots = _pivot_low_indexes(klines, wing)
    return len(pivots) >= 2 and klines[pivots[-1]].low > klines[pivots[-2]].low

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

def detect_pullback_retest(klines: list[Kline], config: dict) -> SetupResult:
    n = len(klines)
    period = 14
    wing = max(1, int(config.get("SWING_PIVOT_WING_BARS", 2) or 2))
    minimum = max(30, strategy.required_lookback_bars(config))
    if n < minimum:
        return SetupResult(False, f"data candle kurang: {n} dari minimum {minimum}")
    closes = [float(k.close) for k in klines]
    if any(x <= 0 or not math.isfinite(x) for x in closes):
        return SetupResult(False, "close candle tidak valid")
    volume_ok, volume_detail = _rolling_volume_confirmation(klines, config)
    if not volume_ok:
        return SetupResult(False, f"rolling volume ditolak: {volume_detail}")
    ema9, ema21 = strategy.ema(closes, 9), strategy.ema(closes, 21)
    if len(ema9) < 2:
        return SetupResult(False, "data EMA kurang")
    ema_cross = ema9[-2] <= ema21[-2] and ema9[-1] > ema21[-1]
    rsi_values = strategy.rsi(closes, period)
    rsi_ok = 50.0 <= rsi_values[-1] <= 75.0
    _macd, _signal, histogram = strategy.macd(closes)
    macd_ok = len(histogram) >= 2 and (histogram[-1] > histogram[-2] or
                                       (histogram[-2] <= 0 < histogram[-1]))
    higher_low = _higher_low_confirmed(klines, wing)
    confirmations = sum((ema_cross, rsi_ok, macd_ok, higher_low))
    details = (f"EMA={'ya' if ema_cross else 'tidak'}, RSI={rsi_values[-1]:.2f}, "
               f"MACD={'naik' if macd_ok else 'tidak'}, HL={'ya' if higher_low else 'tidak'} "
               f"({confirmations}/4), {volume_detail}")
    if confirmations < 3:
        return SetupResult(False, f"konfirmasi entry kurang dari 3/4: {details}")
    return SetupResult(True, f"momentum pump sah: {details}",
                       breakout_level=klines[-1].close,
                       zone_low=klines[-1].low, zone_high=klines[-1].high,
                       anchor_index=max(0, n - 1), retest_touches=0,
                       atr_value=strategy.atr(klines, int(config.get("ATR_PERIOD", 14) or 14)))

def confirm_entry(klines: list[Kline], config: dict) -> tuple[bool, str]:
    hasil = detect_pullback_retest(klines, config)
    return hasil.ok, hasil.reason

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
        hasil = detect_pullback_retest(klines or [], config)
        cand.confirmed = hasil.ok
        cand.confirm_reason = hasil.reason
        cand.setup = hasil
        if hasil.ok:
            lolos.append(cand)

    if not lolos:
        return None
    lolos.sort(key=lambda c: setup_quality_key(c.setup, c))
    return lolos[0]
