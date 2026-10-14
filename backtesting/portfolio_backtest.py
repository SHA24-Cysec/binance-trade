from __future__ import annotations

import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Callable, Optional

from strategy.indicators import Kline
from strategy import indicators as strategy
from market import market_scanner as scanner
from backtesting import backtest_storage as storage
from backtesting import backtest_cache as kcache
from backtesting import parity
from backtesting.backtest_storage import KlineStore, SymbolSeries
from backtesting.backtest_cache import KlineCache

logger = logging.getLogger(__name__)

from backtesting.backtest import (
    BacktestError,
    INTERVAL_MINUTES,
    MS_PER_MIN,
    bars_per_day,
    compute_rolling_24h_stats,
    entry_execution_params,
    fetch_full_klines,
    initial_backtest_equity,
)


@dataclass
class PortfolioTrade:
    symbol: str
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    reason: str
    hold_minutes: float
    pnl_pct: float
    gross_pnl_pct: float
    fee_pct: float
    sl_pct: float
    tp_pct: float
    exit_source: str
    rank_at_entry: int
    pct24h_at_entry: float
    candidates_at_entry: int
    position_notional: float = 0.0
    equity_before: float = 0.0
    equity_after: float = 0.0
    pnl_quote: float = 0.0


@dataclass
class SkippedSignal:
    time: int
    symbol: str
    reason: str
    holding: Optional[str] = None


@dataclass
class PortfolioResult:
    interval: str
    symbols_scanned: int
    symbols_with_data: int
    bars_total: int
    start_time: int
    end_time: int
    trades: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    symbols_failed: list = field(default_factory=list)
    initial_equity: float = 0.0
    final_equity: float = 0.0
    risk_events: dict = field(default_factory=dict)
    chase_skips: int = 0
    trend_skips: int = 0
    trend_scans: int = 0
    htf_demand_skips: int = 0
    htf_demand_scans: int = 0
    daily_demand_skips: int = 0
    daily_demand_scans: int = 0
    daily_trend_skips: int = 0
    daily_trend_scans: int = 0
    same_coin_skips: int = 0
    mark_to_market_open: bool = False
    mark_to_market_symbol: Optional[str] = None
    mark_to_market_pnl_pct: float = 0.0
    mark_to_market_pnl_quote: float = 0.0
    mark_to_market_hold_minutes: float = 0.0


def select_universe(
    tickers: list,
    config: dict,
    max_symbols: Optional[int] = None,
    tradable_symbols: Optional[set] = None,
) -> list[str]:
    cfg = dict(config)
    cfg["MIN_QUOTE_VOLUME_USDT_24H"] = float(config.get("MIN_QUOTE_VOLUME_USDT_24H", 0))

    ranked = scanner.filter_and_rank_candidates(
        tickers, cfg, tradable_symbols, apply_market_gate=False
    )
    ranked.sort(key=lambda c: c.quote_volume, reverse=True)
    symbols = [c.symbol for c in ranked]
    if max_symbols is not None and max_symbols > 0:
        symbols = symbols[:max_symbols]
    return symbols


def new_backtest_store(config: Optional[dict] = None) -> KlineStore:
    cfg = config or {}
    cache_size = int(
        cfg.get("BACKTEST_SYMBOL_CACHE_SIZE", storage.DEFAULT_SYMBOL_CACHE_SIZE)
        or storage.DEFAULT_SYMBOL_CACHE_SIZE
    )
    return KlineStore.create_temp(cache_size=max(1, cache_size))


def open_kline_cache(config: Optional[dict] = None) -> Optional[KlineCache]:
    cfg = config or {}
    if not bool(cfg.get("BACKTEST_CACHE_ENABLED", True)):
        return None
    path = str(cfg.get("BACKTEST_CACHE_FILE") or "").strip()
    if not path:
        return None
    try:
        cache = KlineCache(
            path,
            fresh_hours=float(
                cfg.get("BACKTEST_CACHE_FRESH_HOURS", kcache.DEFAULT_FRESH_HOURS) or 0
            ),
            ttl_days=float(
                cfg.get("BACKTEST_CACHE_TTL_DAYS", kcache.DEFAULT_TTL_DAYS) or 0
            ),
        )
        cache.prune()
        return cache
    except (sqlite3.Error, OSError, kcache.CacheError) as exc:
        logger.warning(
            "Cache candle backtest tidak bisa dipakai (%s). Backtest "
            "tetap berjalan dengan mengunduh penuh.",
            exc,
        )
        return None


def fetch_universe_klines(
    client,
    symbols: list[str],
    interval: str,
    start_ms: int,
    end_ms: int,
    store: KlineStore,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    sleep_between_symbols: float = 0.0,
    cancel_cb: Optional[Callable[[], bool]] = None,
    cache: Optional[KlineCache] = None,
    max_workers: int = 1,
) -> tuple[list[str], list]:
    ok_symbols: list[str] = []
    failed: list = []
    total = max(1, len(symbols))
    workers = max(1, int(max_workers or 1))

    def _download(sym: str) -> list[Kline]:
        return _klines_untuk_simbol(client, sym, interval, start_ms, end_ms, cache)

    if workers == 1 or len(symbols) <= 1:
        batches = [symbols]
        executor = None
    else:
        batches = [symbols[i : i + workers] for i in range(0, len(symbols), workers)]
        executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="backtest-klines"
        )

    done = 0
    try:
        for batch in batches:
            if cancel_cb is not None and cancel_cb():
                raise BacktestError("Backtest dibatalkan.")
            futures = (
                {sym: executor.submit(_download, sym) for sym in batch}
                if executor is not None
                else {}
            )
            for sym in batch:
                if cancel_cb is not None and cancel_cb():
                    for future in futures.values():
                        future.cancel()
                    raise BacktestError("Backtest dibatalkan.")
                try:
                    kl = (
                        futures[sym].result()
                        if executor is not None
                        else _download(sym)
                    )
                    if kl:
                        store.write_symbol(sym, kl)
                        ok_symbols.append(sym)
                    else:
                        failed.append(
                            {
                                "symbol": sym,
                                "error": "tidak ada data candle pada rentang ini",
                            }
                        )
                except Exception as exc:
                    failed.append({"symbol": sym, "error": str(exc)[:160]})

                done += 1
                if progress_cb:
                    progress_cb(done / total, sym)
                if sleep_between_symbols:
                    time.sleep(sleep_between_symbols)
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    return ok_symbols, failed


def _klines_untuk_simbol(
    client,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    cache: Optional[KlineCache],
) -> list[Kline]:
    if cache is None:
        return fetch_full_klines(
            client, symbol, interval, start_ms, end_ms, sleep_between_calls=0.0
        )

    for awal, akhir in cache.missing_ranges(symbol, interval, start_ms, end_ms):
        bagian = fetch_full_klines(
            client, symbol, interval, awal, akhir, sleep_between_calls=0.0
        )
        cache.put(symbol, interval, bagian, awal, akhir)
    return cache.read(symbol, interval, start_ms, end_ms)


def build_timeline(
    store: KlineStore,
    interval: str,
    symbols: Optional[list[str]] = None,
    *,
    include_ohlcv: bool = False,
) -> tuple[list[int], dict[str, SymbolSeries]]:
    window = bars_per_day(interval)
    daftar = list(symbols) if symbols is not None else store.symbols()
    series_of: dict[str, SymbolSeries] = {}
    all_times: set = set()

    for sym in daftar:
        klines = store.load_klines(sym)
        if not klines:
            continue
        stats = compute_rolling_24h_stats(klines, window)
        series = SymbolSeries.build(sym, klines, stats, include_ohlcv=include_ohlcv)
        series_of[sym] = series
        all_times.update(series.open_times())
        del klines, stats

    timeline = sorted(all_times)
    return timeline, series_of


EntrySignal = tuple[int, float, str, int, float, scanner.SetupResult]


class EntrySignalCache(dict):

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.trend_scans = 0
        self.trend_skips = 0
        self.trend_events: list = []
        self.htf_demand_scans = 0
        self.htf_demand_skips = 0
        self.htf_demand_events: list = []
        self.daily_demand_scans = 0
        self.daily_demand_skips = 0
        self.daily_demand_events: list = []
        self.daily_trend_scans = 0
        self.daily_trend_skips = 0
        self.daily_trend_events: list = []


def build_trend_lookups(
    store: KlineStore, symbols: list, config: dict, interval: str
) -> dict:
    from strategy import indicators as strategy_mod

    if not config.get("TREND_FILTER_ENABLED", False) and not config.get(
        "HTF_DEMAND_FILTER_ENABLED", False
    ):
        return {}
    target = strategy_mod.trend_interval_minutes(config)
    source = strategy_mod.INTERVAL_MINUTES.get(str(interval))
    if source is None:
        raise BacktestError(
            f"Interval simulasi '{interval}' tidak dikenal sehingga candle trend "
            f"'{strategy_mod.trend_interval(config)}' tidak bisa dirangkai."
        )
    lookups: dict = {}
    for sym in symbols:
        klines = store.klines(sym)
        if not klines:
            continue
        lookups[sym] = parity.TrendLookup(
            strategy_mod.aggregate_klines(klines, target, source), config
        )
        del klines
    return lookups


def build_daily_lookups(
    store: KlineStore, symbols: list, config: dict, interval: str
) -> dict:
    from strategy import indicators as strategy_mod

    if not strategy_mod.daily_demand_enabled(config):
        return {}
    lookups: dict = {}
    for sym in symbols:
        klines = store.klines(sym)
        if not klines:
            continue
        lookups[sym] = parity.DailyDemandLookup(
            parity.build_daily_klines(klines, config, interval), config
        )
        del klines
    return lookups


def build_daily_trend_lookups(
    store: KlineStore, symbols: list, config: dict, interval: str
) -> dict:
    from strategy import indicators as strategy_mod

    if not strategy_mod.daily_trend_enabled(config):
        return {}
    lookups: dict = {}
    for sym in symbols:
        klines = store.klines(sym)
        if not klines:
            continue
        lookup = parity.make_daily_trend_lookup(klines, config, interval)
        if lookup is not None:
            lookups[sym] = lookup
        del klines
    return lookups


def unpack_prebuilt(prebuilt: Optional[tuple]):
    if prebuilt is None:
        return None, None, None, None
    if len(prebuilt) >= 4:
        return prebuilt[0], prebuilt[1], prebuilt[2], prebuilt[3]
    if len(prebuilt) == 3:
        return prebuilt[0], prebuilt[1], prebuilt[2], None
    return prebuilt[0], prebuilt[1], None, None


def _siapkan_trend(prebuilt, store, series_of, config, interval):
    _, _, trend_of, _ = unpack_prebuilt(prebuilt)
    if trend_of is not None:
        return trend_of
    if not config.get("TREND_FILTER_ENABLED", False) and not config.get(
        "HTF_DEMAND_FILTER_ENABLED", False
    ):
        return {}
    return build_trend_lookups(store, list(series_of), config, interval)


def _siapkan_harian(prebuilt, store, series_of, config, interval):
    _, _, _, daily_of = unpack_prebuilt(prebuilt)
    if daily_of is not None:
        return daily_of
    if not config.get("DAILY_DEMAND_FILTER_ENABLED", False):
        return {}
    return build_daily_lookups(store, list(series_of), config, interval)


def _siapkan_tren_harian(prebuilt, store, series_of, config, interval):
    from strategy import indicators as strategy_mod

    if prebuilt is not None and len(prebuilt) >= 5 and prebuilt[4] is not None:
        return prebuilt[4]
    if not strategy_mod.daily_trend_enabled(config):
        return {}
    return build_daily_trend_lookups(store, list(series_of), config, interval)


def precompute_entry_signals(
    store: KlineStore,
    config: dict,
    interval: str,
    warmup_ms: int = 0,
    *,
    prebuilt: Optional[tuple] = None,
    progress_cb: Optional[Callable[[float], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    btc_klines: Optional[list[Kline]] = None,
    max_skipped_records: int = 400,
) -> EntrySignalCache:
    all_symbols = store.symbols()
    if not all_symbols:
        raise BacktestError("Tidak ada data candle untuk memproses sinyal.")

    tradable_meta = config.get("_historical_tradable_symbols")
    symbols = [
        sym
        for sym in all_symbols
        if scanner.is_structurally_allowed_symbol(sym, config, tradable_meta)
    ]
    if not symbols:
        raise BacktestError("Tidak ada data yang lolos policy semesta bersama.")

    if prebuilt is not None:
        timeline, series_of = prebuilt
    else:
        timeline, series_of = build_timeline(store, interval, symbols)
    if not timeline:
        raise BacktestError("Garis waktu kosong, tidak ada candle yang bisa diproses.")
    symbols = [sym for sym in symbols if sym in series_of]

    lookback = strategy.confirm_window_bars(config)
    min_vol = float(config["MIN_QUOTE_VOLUME_USDT_24H"])
    top_n = int(config.get("TOP_N_CANDIDATES_TO_CONFIRM", 10))
    bar_ms = INTERVAL_MINUTES.get(interval, 5) * MS_PER_MIN
    btc_lookup = parity.make_btc_lookup(btc_klines, config, bar_ms)
    gate_cfg = parity.gate_config(config, btc_lookup)
    trend_of = _siapkan_trend(prebuilt, store, series_of, config, interval)
    daily_of = _siapkan_harian(prebuilt, store, series_of, config, interval)
    tren_harian_of = _siapkan_tren_harian(prebuilt, store, series_of, config, interval)
    if trend_of or daily_of or tren_harian_of:
        butuh_warmup = parity.htf_gate_warmup_ms(config, interval)
        if int(warmup_ms) < butuh_warmup:
            logger.warning(
                "Warmup %d ms dinaikkan menjadi %d ms karena gerbang timeframe "
                "tinggi yang aktif (trend + demand H1 + demand harian + EMA/ADX harian) "
                "butuh paling sedikit %d candle %s sebagai pemanasan.",
                warmup_ms,
                butuh_warmup,
                strategy.htf_gate_warmup_bars(config, interval),
                interval,
            )
            warmup_ms = butuh_warmup
    first_allowed_time = timeline[0] + int(warmup_ms)

    papan_input = []
    for sym in symbols:
        ot, _ct, pct_arr, vol_arr, ready_arr = series_of[sym].board_arrays()
        papan_input.append((sym, ot, pct_arr, vol_arr, ready_arr, len(ot)))

    signals = EntrySignalCache()
    total_bars = len(timeline)
    for bi, t_now in enumerate(timeline):
        if cancel_cb is not None and bi % 200 == 0 and cancel_cb():
            raise BacktestError("Backtest dibatalkan.")
        if progress_cb and bi % 50 == 0:
            progress_cb(bi / max(1, total_bars))
        if t_now < first_allowed_time:
            continue

        btc_drop = btc_lookup.drop_pct_at(t_now) if btc_lookup is not None else None
        board = []
        for sym, ot, pct_arr, vol_arr, ready_arr, n_bar in papan_input:
            pos = bisect_left(ot, t_now)
            if pos >= n_bar or ot[pos] != t_now or not ready_arr[pos]:
                continue
            vol24 = vol_arr[pos]
            if vol24 < min_vol:
                continue
            pct24 = pct_arr[pos]
            gate_ok, _gate_reason = scanner.evaluate_market_gate(
                pct24, vol24, gate_cfg, btc_drop_pct=btc_drop
            )
            if gate_ok:
                board.append((vol24, sym, pos, pct24))
        board.sort(key=lambda x: -x[0])
        if not board:
            continue

        lolos: list[EntrySignal] = []
        for rank, (vol24, sym, index, pct24) in enumerate(board[:top_n], start=1):
            if index + 1 < lookback:
                continue
            symbol_series = series_of[sym]
            try:
                if symbol_series.has_ohlcv:
                    window_klines = symbol_series.klines_slice(
                        max(0, index - lookback + 1), index + 1
                    )
                    signal_candle = symbol_series.kline_at(index)
                else:
                    klines = store.klines(sym)
                    window_klines = klines[max(0, index - lookback + 1) : index + 1]
                    signal_candle = window_klines[-1]
                setup = scanner.detect_entry_setup(window_klines, config)
            except Exception as exc:
                raise BacktestError(
                    f"Evaluasi sinyal {sym} pada waktu {int(t_now)} "
                    f"(bar {index}) gagal: {type(exc).__name__}: {str(exc)[:160]}"
                ) from exc
            if setup.ok and trend_of:
                lock = trend_of.get(sym)
                if lock is not None and signal_candle is not None:
                    signals.trend_scans += 1
                    if not lock.verdict_at(signal_candle.close_time)["ok"]:
                        signals.trend_skips += 1
                        if len(signals.trend_events) < max_skipped_records:
                            signals.trend_events.append((int(t_now), sym))
                        continue
            if (
                setup.ok
                and trend_of
                and bool(config.get("HTF_DEMAND_FILTER_ENABLED", False))
            ):
                lock = trend_of.get(sym)
                if lock is not None and signal_candle is not None:
                    signals.htf_demand_scans += 1
                    if not lock.htf_demand_at(signal_candle.close_time)["ok"]:
                        signals.htf_demand_skips += 1
                        if len(signals.htf_demand_events) < max_skipped_records:
                            signals.htf_demand_events.append((int(t_now), sym))
                        continue
            if (
                setup.ok
                and daily_of
                and bool(config.get("DAILY_DEMAND_FILTER_ENABLED", False))
            ):
                lock = daily_of.get(sym)
                if lock is not None and signal_candle is not None:
                    signals.daily_demand_scans += 1
                    if not lock.daily_demand_at(signal_candle.close_time)["ok"]:
                        signals.daily_demand_skips += 1
                        if len(signals.daily_demand_events) < max_skipped_records:
                            signals.daily_demand_events.append((int(t_now), sym))
                        continue
            if setup.ok and bool(config.get("DAILY_TREND_FILTER_ENABLED", False)):
                lock = tren_harian_of.get(sym)
                signals.daily_trend_scans += 1
                if (
                    lock is None
                    or signal_candle is None
                    or not lock.verdict_at(signal_candle.close_time)["ok"]
                ):
                    signals.daily_trend_skips += 1
                    if len(signals.daily_trend_events) < max_skipped_records:
                        signals.daily_trend_events.append((int(t_now), sym))
                    continue
            if setup.ok:
                lolos.append((rank, pct24, sym, index, vol24, setup))

        if not lolos:
            continue
        lolos.sort(
            key=lambda row: scanner.setup_quality_key(
                row[5],
                scanner.Candidate(
                    row[2],
                    "",
                    row[1],
                    row[4],
                    (
                        series_of[row[2]].kline_at(row[3]).close
                        if series_of[row[2]].has_ohlcv
                        else store.klines(row[2])[row[3]].close
                    ),
                ),
            )
        )
        signals[int(t_now)] = (len(board), lolos)

    if progress_cb:
        progress_cb(1.0)
    return signals


def _resolve_symbol_cache_size(config: dict, top_n: int) -> int:
    diminta = int(
        config.get("BACKTEST_SYMBOL_CACHE_SIZE", storage.DEFAULT_SYMBOL_CACHE_SIZE)
        or storage.DEFAULT_SYMBOL_CACHE_SIZE
    )
    return max(1, diminta, 2 * int(top_n) + 4)


def run_portfolio_backtest(
    store: KlineStore,
    config: dict,
    interval: str,
    warmup_ms: int = 0,
    progress_cb: Optional[Callable[[float], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    max_skipped_records: int = 400,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    prebuilt: Optional[tuple] = None,
    btc_klines: Optional[list] = None,
    entry_signal_cache: Optional[EntrySignalCache] = None,
) -> PortfolioResult:
    semua_simbol = store.symbols()
    if not semua_simbol:
        raise BacktestError("Tidak ada data candle untuk disimulasikan.")

    tradable_meta = config.get("_historical_tradable_symbols")
    original_count = len(semua_simbol)
    symbols = [
        sym
        for sym in semua_simbol
        if scanner.is_structurally_allowed_symbol(sym, config, tradable_meta)
    ]
    if not symbols:
        raise BacktestError("Tidak ada data yang lolos policy semesta bersama.")
    lolos_policy = len(symbols)

    timeline, series_of, _trend_prebuilt, _daily_prebuilt = unpack_prebuilt(prebuilt)
    if timeline is None:
        timeline, series_of = build_timeline(store, interval, symbols)
    if not timeline:
        raise BacktestError("Garis waktu kosong, tidak ada candle yang bisa diproses.")
    if start_ms is not None or end_ms is not None:
        timeline = [
            t
            for t in timeline
            if (start_ms is None or t >= start_ms) and (end_ms is None or t <= end_ms)
        ]
        if not timeline:
            raise BacktestError(
                "Rentang waktu (start_ms/end_ms) tidak memuat satu bar pun dari data."
            )
    symbols = [sym for sym in symbols if sym in series_of]

    lookback = strategy.confirm_window_bars(config)
    min_vol = float(config["MIN_QUOTE_VOLUME_USDT_24H"])
    top_n = int(config.get("TOP_N_CANDIDATES_TO_CONFIRM", 10))
    if entry_signal_cache is None:
        store.set_cache_size(_resolve_symbol_cache_size(config, top_n))
    else:
        cache_size = int(
            config.get("BACKTEST_SYMBOL_CACHE_SIZE", storage.DEFAULT_SYMBOL_CACHE_SIZE)
            or storage.DEFAULT_SYMBOL_CACHE_SIZE
        )
        store.set_cache_size(max(1, cache_size))
    _pre_warnings: list = []
    if lolos_policy != original_count:
        _pre_warnings.append(
            "Sebagian data dibuang oleh policy semesta bersama (stablecoin, leveraged token, blacklist, quote, atau status metadata)."
        )
    if tradable_meta is None or config.get("_tradable_status_is_current_snapshot"):
        _pre_warnings.append(
            "Status TRADING historis tidak tersedia dari candle Binance. Policy status hanya dapat diverifikasi dari metadata saat ini bila caller menyediakannya."
        )
    trend_of = _siapkan_trend(prebuilt, store, series_of, config, interval)
    daily_of = _siapkan_harian(prebuilt, store, series_of, config, interval)
    tren_harian_of = _siapkan_tren_harian(prebuilt, store, series_of, config, interval)
    if trend_of or daily_of or tren_harian_of:
        _butuh_gerbang = parity.htf_gate_warmup_ms(config, interval)
        if int(warmup_ms) < _butuh_gerbang:
            _bagian = []
            if trend_of:
                _bagian.append(
                    f"gerbang timeframe tinggi {config.get('TREND_INTERVAL', '1h')} "
                    f"(trend + zona demand) butuh {strategy.htf_window_bars(config)} candle "
                    "tertutup"
                )
            if daily_of:
                _bagian.append(
                    f"gerbang zona demand {strategy.daily_demand_interval(config)} butuh "
                    f"{strategy.daily_demand_window_bars(config)} candle "
                    f"{strategy.daily_demand_interval(config)} tertutup"
                )
            if tren_harian_of:
                _bagian.append(
                    f"gerbang EMA + ADX {strategy.daily_trend_interval(config)} butuh "
                    f"{strategy.daily_trend_window_bars(config)} candle "
                    f"{strategy.daily_trend_interval(config)} tertutup"
                )
            _pre_warnings.append(
                f"Warmup {warmup_ms} ms dinaikkan menjadi {_butuh_gerbang} ms karena "
                f"{' dan '.join(_bagian)} sebelum bar entry pertama. Unduhan data harus "
                "mencakup rentang warmup ini."
            )
            warmup_ms = _butuh_gerbang
    _butuh = strategy.required_lookback_bars(config)
    if int(config.get("CONFIRM_LOOKBACK_BARS", 0)) < _butuh:
        _pre_warnings.append(
            f"CONFIRM_LOOKBACK_BARS={config.get('CONFIRM_LOOKBACK_BARS')} lebih kecil dari "
            f"{_butuh} candle yang dibutuhkan konfirmasi volume dan ATR. Simulasi memakai "
            f"{lookback} candle agar deteksi tetap mungkin, tetapi perbaiki config supaya "
            "backtest dan bot live benar-benar memakai angka yang sama."
        )

    try:
        from config.config import get_taker_fee_pct as _fee_fn

        fee_round_trip = _fee_fn(config) * 2.0
    except ImportError:
        fee_round_trip = float(config.get("TAKER_FEE_PCT", 0.1)) * 2.0

    execution_spread_pct, execution_slippage_pct, entry_delay_bars = (
        entry_execution_params(config)
    )

    trades: list = []
    skipped: list = []
    warnings: list = list(_pre_warnings)
    initial_equity = initial_backtest_equity(config)
    equity = initial_equity
    mark_to_market_open = False
    mark_to_market_symbol: Optional[str] = None
    mark_to_market_pnl_pct = 0.0
    mark_to_market_pnl_quote = 0.0
    mark_to_market_hold_minutes = 0.0

    holding: Optional[str] = None
    position: Optional[parity.PositionState] = None
    entry_price = 0.0
    entry_time = 0
    position_notional = 0.0
    equity_before_entry = 0.0
    rank_at_entry = 0
    pct24h_at_entry = 0.0
    cands_at_entry = 0
    next_entry_allowed_at = 0
    chase_skips = 0
    trend_skips = 0
    trend_scans = 0
    htf_demand_skips = 0
    htf_demand_scans = 0
    daily_demand_skips = 0
    daily_demand_scans = 0
    daily_trend_skips = 0
    daily_trend_scans = 0
    symbol_blocks: dict = {}
    same_coin_skips = 0
    max_chase_pct = float(config.get("MAX_CHASE_PCT", 0) or 0)

    controls = parity.AccountRiskControls(config, initial_equity)
    bar_ms = INTERVAL_MINUTES.get(interval, 5) * MS_PER_MIN
    cap_ms = parity.max_hold_ms(config, interval)
    btc_lookup = parity.make_btc_lookup(btc_klines, config, bar_ms)
    gate_cfg = parity.gate_config(config, btc_lookup)
    btc_warning = parity.btc_filter_warning(config, btc_lookup)
    if btc_warning:
        warnings.append(btc_warning)

    first_allowed_time = timeline[0] + warmup_ms
    total_bars = len(timeline)
    if tren_harian_of:
        _hari_uji_tren = (int(timeline[-1]) - int(first_allowed_time)) / float(
            MS_PER_MIN * 1440
        )
        _butuh_hari_tren = strategy.daily_trend_required_bars(config)
        if _hari_uji_tren < float(_butuh_hari_tren):
            _pre_warnings.append(
                f"Periode uji hanya sekitar {_hari_uji_tren:.1f} hari, lebih pendek dari "
                f"riwayat yang dibutuhkan EMA + ADX harian ({_butuh_hari_tren} hari). "
                "Perpanjang rentang data supaya gerbang harian benar-benar teruji."
            )
    if daily_of:
        _hari_uji = (int(timeline[-1]) - int(first_allowed_time)) / float(
            MS_PER_MIN * 1440
        )
        if _hari_uji < float(strategy.daily_demand_lookback_bars(config)):
            _pre_warnings.append(
                f"Periode uji hanya sekitar {_hari_uji:.1f} hari, lebih pendek dari "
                f"jendela zona demand harian "
                f"({strategy.daily_demand_lookback_bars(config)} hari lookback). "
                "Perpanjang rentang hari (atau kecilkan DAILY_DEMAND_LOOKBACK_BARS) "
                "supaya gerbang harian benar-benar teruji."
            )

    papan_input = []
    for sym in symbols:
        ot, ct, pct_arr, vol_arr, ready_arr = series_of[sym].board_arrays()
        papan_input.append((sym, ot, ct, pct_arr, vol_arr, ready_arr, len(ot)))

    if entry_signal_cache is not None:
        trend_scans += int(getattr(entry_signal_cache, "trend_scans", 0))
        trend_skips += int(getattr(entry_signal_cache, "trend_skips", 0))
        htf_demand_scans += int(getattr(entry_signal_cache, "htf_demand_scans", 0))
        htf_demand_skips += int(getattr(entry_signal_cache, "htf_demand_skips", 0))
        daily_demand_scans += int(getattr(entry_signal_cache, "daily_demand_scans", 0))
        daily_demand_skips += int(getattr(entry_signal_cache, "daily_demand_skips", 0))
        daily_trend_scans += int(getattr(entry_signal_cache, "daily_trend_scans", 0))
        daily_trend_skips += int(getattr(entry_signal_cache, "daily_trend_skips", 0))
        for waktu_event, simbol_event in getattr(
            entry_signal_cache, "trend_events", []
        ):
            if len(skipped) >= max_skipped_records:
                break
            skipped.append(
                SkippedSignal(
                    time=waktu_event,
                    symbol=simbol_event,
                    reason="FILTER_TREND",
                    holding=None,
                )
            )
        for waktu_event, simbol_event in getattr(
            entry_signal_cache, "htf_demand_events", []
        ):
            if len(skipped) >= max_skipped_records:
                break
            skipped.append(
                SkippedSignal(
                    time=waktu_event,
                    symbol=simbol_event,
                    reason="FILTER_HTF_DEMAND",
                    holding=None,
                )
            )
        for waktu_event, simbol_event in getattr(
            entry_signal_cache, "daily_demand_events", []
        ):
            if len(skipped) >= max_skipped_records:
                break
            skipped.append(
                SkippedSignal(
                    time=waktu_event,
                    symbol=simbol_event,
                    reason="FILTER_DAILY_DEMAND",
                    holding=None,
                )
            )
        for waktu_event, simbol_event in getattr(
            entry_signal_cache, "daily_trend_events", []
        ):
            if len(skipped) >= max_skipped_records:
                break
            skipped.append(
                SkippedSignal(
                    time=waktu_event,
                    symbol=simbol_event,
                    reason="FILTER_DAILY_TREND",
                    holding=None,
                )
            )

    signal_times = (
        sorted(int(value) for value in entry_signal_cache)
        if entry_signal_cache is not None
        else []
    )
    signal_cursor = 0
    bi = 0
    while bi < total_bars:
        if entry_signal_cache is not None and holding is None:
            while (
                signal_cursor < len(signal_times)
                and signal_times[signal_cursor] < timeline[bi]
            ):
                signal_cursor += 1
            if signal_cursor >= len(signal_times):
                break
            next_signal = signal_times[signal_cursor]
            next_index = bisect_left(timeline, next_signal)
            if next_index >= total_bars:
                break
            if timeline[next_index] != next_signal:
                signal_cursor += 1
                continue
            if next_index > bi:
                bi = next_index

        current_bi = bi
        t_now = timeline[current_bi]
        bi = current_bi + 1
        if progress_cb and current_bi % 50 == 0:
            progress_cb(current_bi / total_bars)
        if cancel_cb is not None and current_bi % 200 == 0 and cancel_cb():
            raise BacktestError("Backtest dibatalkan.")

        board = []
        if entry_signal_cache is None:
            btc_drop = btc_lookup.drop_pct_at(t_now) if btc_lookup is not None else None
            for sym, ot, _ct, pct_arr, vol_arr, ready_arr, n_bar in papan_input:
                pos = bisect_left(ot, t_now)
                if pos >= n_bar or ot[pos] != t_now:
                    continue
                if parity.same_coin_blocked(sym, t_now, symbol_blocks):
                    same_coin_skips += 1
                    if len(skipped) < max_skipped_records:
                        skipped.append(
                            SkippedSignal(
                                time=t_now,
                                symbol=sym,
                                reason="SAME_COIN_BLOCK",
                                holding=None,
                            )
                        )
                    continue
                if not ready_arr[pos]:
                    continue
                vol24 = vol_arr[pos]
                if vol24 < min_vol:
                    continue
                pct24 = pct_arr[pos]
                gate_ok, _gate_reason = scanner.evaluate_market_gate(
                    pct24, vol24, gate_cfg, btc_drop_pct=btc_drop
                )
                if not gate_ok:
                    continue
                board.append((vol24, sym, pos, pct24))
            board.sort(key=lambda x: -x[0])

        if holding is not None:
            hi = series_of[holding].index_at(t_now)
            if hi is None:
                continue

            held_series = series_of[holding]
            candle = (
                held_series.kline_at(hi)
                if entry_signal_cache is not None and held_series.has_ohlcv
                else store.klines(holding)[hi]
            )
            if candle.open_time < entry_time:
                continue
            hold_minutes = (candle.close_time - entry_time) / 60000.0

            exit_reason = None
            exit_price = None
            verdict = parity.evaluate_candle_exit(
                position, candle, config, cap_ms=cap_ms, bar_ms=bar_ms
            )
            if verdict is not None:
                exit_reason, exit_price = verdict

            is_last = current_bi == total_bars - 1
            if exit_reason is None:
                mtm_pnl_pct = (candle.close / entry_price - 1.0) * 100.0
                mtm_pnl_quote = position_notional * mtm_pnl_pct / 100.0
                mtm_equity = equity + mtm_pnl_quote
                entries_paused = controls.update(candle.close_time, mtm_equity)
                if controls.force_close_due(entries_paused, True):
                    exit_reason = parity.RISK_LIMIT_REASON
                    exit_price = candle.close
                elif is_last:
                    mark_to_market_open = True
                    mark_to_market_symbol = holding
                    mark_to_market_pnl_pct = mtm_pnl_pct
                    mark_to_market_pnl_quote = mtm_pnl_quote
                    mark_to_market_hold_minutes = max(
                        0.0, (candle.close_time - entry_time) / 60000.0
                    )
                    warnings.append(
                        "Posisi terakhir masih terbuka pada akhir data. Nilainya dicatat "
                        "sebagai mark-to-market, bukan sebagai trade tertutup."
                    )

            if exit_reason:
                exit_price = strategy.backtest_sell_execution_price(
                    exit_price, execution_spread_pct, execution_slippage_pct
                )
                gross = (exit_price / entry_price - 1.0) * 100.0
                pnl_pct = gross - fee_round_trip
                pnl_quote = position_notional * pnl_pct / 100.0
                equity_after = max(0.0, equity + pnl_quote)
                trades.append(
                    PortfolioTrade(
                        symbol=holding,
                        entry_time=entry_time,
                        entry_price=entry_price,
                        exit_time=candle.close_time,
                        exit_price=exit_price,
                        reason=exit_reason,
                        hold_minutes=hold_minutes,
                        pnl_pct=pnl_pct,
                        gross_pnl_pct=gross,
                        fee_pct=fee_round_trip,
                        sl_pct=position.levels["sl"],
                        tp_pct=position.levels["tp"],
                        exit_source=position.levels["src"],
                        rank_at_entry=rank_at_entry,
                        pct24h_at_entry=pct24h_at_entry,
                        candidates_at_entry=cands_at_entry,
                        position_notional=position_notional,
                        equity_before=equity_before_entry,
                        equity_after=equity_after,
                        pnl_quote=pnl_quote,
                    )
                )
                equity = equity_after
                position_notional = 0.0
                parity.same_coin_block_record(
                    symbol_blocks, holding, candle.close_time, pnl_quote, config
                )
                holding = None
                position = None
                next_entry_allowed_at = parity.next_entry_allowed(
                    candle.close_time, config
                )
                controls.update(candle.close_time, equity)
            continue

        entries_paused = controls.update(t_now + bar_ms - 1, equity)
        if entries_paused:
            continue
        if t_now < first_allowed_time or t_now < next_entry_allowed_at:
            continue

        if entry_signal_cache is None:
            eligible = board
            if not eligible:
                continue

            lolos = []
            for rank, (vol24, sym, i, pct) in enumerate(eligible[:top_n], start=1):
                kl = store.klines(sym)
                if i + 1 < lookback:
                    continue
                window_kl = kl[max(0, i - lookback + 1) : i + 1]
                try:
                    setup = scanner.detect_entry_setup(window_kl, config)
                except Exception as exc:
                    raise BacktestError(
                        f"Evaluasi sinyal {sym} pada waktu {int(t_now)} "
                        f"(bar {i}) gagal: {type(exc).__name__}: {str(exc)[:160]}"
                    ) from exc
                if not setup.ok:
                    continue
                if trend_of:
                    lock = trend_of.get(sym)
                    if lock is not None:
                        trend_scans += 1
                        if not lock.verdict_at(kl[i].close_time)["ok"]:
                            trend_skips += 1
                            if len(skipped) < max_skipped_records:
                                skipped.append(
                                    SkippedSignal(
                                        time=t_now,
                                        symbol=sym,
                                        reason="FILTER_TREND",
                                        holding=None,
                                    )
                                )
                            continue
                if trend_of and bool(config.get("HTF_DEMAND_FILTER_ENABLED", False)):
                    lock = trend_of.get(sym)
                    if lock is not None:
                        htf_demand_scans += 1
                        if not lock.htf_demand_at(kl[i].close_time)["ok"]:
                            htf_demand_skips += 1
                            if len(skipped) < max_skipped_records:
                                skipped.append(
                                    SkippedSignal(
                                        time=t_now,
                                        symbol=sym,
                                        reason="FILTER_HTF_DEMAND",
                                        holding=None,
                                    )
                                )
                            continue
                if daily_of and bool(config.get("DAILY_DEMAND_FILTER_ENABLED", False)):
                    lock = daily_of.get(sym)
                    if lock is not None:
                        daily_demand_scans += 1
                        if not lock.daily_demand_at(kl[i].close_time)["ok"]:
                            daily_demand_skips += 1
                            if len(skipped) < max_skipped_records:
                                skipped.append(
                                    SkippedSignal(
                                        time=t_now,
                                        symbol=sym,
                                        reason="FILTER_DAILY_DEMAND",
                                        holding=None,
                                    )
                                )
                            continue
                if tren_harian_of and bool(
                    config.get("DAILY_TREND_FILTER_ENABLED", False)
                ):
                    lock = tren_harian_of.get(sym)
                    daily_trend_scans += 1
                    if lock is None or not lock.verdict_at(kl[i].close_time)["ok"]:
                        daily_trend_skips += 1
                        if len(skipped) < max_skipped_records:
                            skipped.append(
                                SkippedSignal(
                                    time=t_now,
                                    symbol=sym,
                                    reason="FILTER_DAILY_TREND",
                                    holding=None,
                                )
                            )
                        continue
                lolos.append((rank, pct, sym, i, vol24, setup))

            if not lolos:
                continue

            lolos.sort(
                key=lambda row: scanner.setup_quality_key(
                    row[5],
                    scanner.Candidate(
                        row[2], "", row[1], row[4], store.klines(row[2])[row[3]].close
                    ),
                )
            )
            eligible_count = len(eligible)
        else:
            cached_entry = entry_signal_cache.get(int(t_now))
            if cached_entry is None:
                continue
            eligible_count, lolos = cached_entry
            if not lolos:
                continue
        lolos_tersedia = []
        for row in lolos:
            if parity.same_coin_blocked(row[2], t_now, symbol_blocks):
                same_coin_skips += 1
                if len(skipped) < max_skipped_records:
                    skipped.append(
                        SkippedSignal(
                            time=t_now,
                            symbol=row[2],
                            reason="SAME_COIN_BLOCK",
                            holding=None,
                        )
                    )
                continue
            lolos_tersedia.append(row)
        lolos = lolos_tersedia
        if not lolos:
            continue
        rank, pct, sym, i, _vol24, setup_terpilih = lolos[0]
        sizing = strategy.resolve_position_notional(config, equity)
        if sizing["notional"] <= 0 or sizing["notional"] > equity:
            continue

        if len(skipped) < max_skipped_records:
            for _r2, _p2, sym2, _i2, _v2, _s2 in lolos:
                if sym2 == sym:
                    continue
                skipped.append(
                    SkippedSignal(
                        time=t_now,
                        symbol=sym2,
                        reason="KALAH_KUALITAS_SETUP",
                        holding=sym,
                    )
                )
                if len(skipped) >= max_skipped_records:
                    break

        entry_series = series_of[sym]
        entry_idx = i + entry_delay_bars
        if entry_signal_cache is not None and entry_series.has_ohlcv:
            series_length = len(entry_series)
            if entry_idx >= series_length:
                warnings.append(
                    f"Sinyal {sym} terakhir tidak memiliki bar eksekusi setelah latency entry; trade dilewati."
                )
                continue
            signal_candle = entry_series.kline_at(i)
            entry_candle = entry_series.kline_at(entry_idx)
        else:
            kl = store.klines(sym)
            series_length = len(kl)
            if entry_idx >= series_length:
                warnings.append(
                    f"Sinyal {sym} terakhir tidak memiliki bar eksekusi setelah latency entry; trade dilewati."
                )
                continue
            signal_candle = kl[i]
            entry_candle = kl[entry_idx]
        if entry_delay_bars == 0:
            raw_entry_price = signal_candle.close
            entry_time_value = signal_candle.close_time
        else:
            raw_entry_price = entry_candle.open
            entry_time_value = entry_candle.open_time
        level_cfg = dict(config)
        if bool(config.get("USE_ATR_EXIT", False)):
            atr_val = setup_terpilih.atr_value
            if atr_val is None:
                warnings.append(
                    f"Entry {sym} bar {i} dilewati: USE_ATR_EXIT aktif tetapi "
                    "nilai ATR tidak tersedia, level exit tidak dapat dikunci "
                    "dengan aman (paritas open_position)."
                )
                continue
            level_cfg["_atr_value"] = atr_val
        lv = strategy.resolve_exit_levels(level_cfg)
        if str(lv.get("source", "")).upper() == "ATR" and not (
            0.0 < float(lv.get("sl_pct") or 0.0) < raw_entry_price
        ):
            warnings.append(
                f"Entry {sym} bar {i} dilewati: jarak SL ATR "
                f"{float(lv.get('sl_pct') or 0.0):.10g} tidak masuk akal "
                f"terhadap harga acuan {raw_entry_price:.10g} "
                "(paritas open_position). Cek ATR_MULT_SL/ATR."
            )
            continue
        exec_entry_price = strategy.backtest_buy_execution_price(
            raw_entry_price, execution_spread_pct, execution_slippage_pct
        )
        if parity.chase_exceeded(
            exec_entry_price, setup_terpilih.signal_close, max_chase_pct
        ):
            chase_skips += 1
            if len(skipped) < max_skipped_records:
                skipped.append(
                    SkippedSignal(
                        time=t_now, symbol=sym, reason="FILTER_CHASE", holding=None
                    )
                )
            continue
        holding = sym
        position = parity.PositionState(
            exec_entry_price, lv, entry_time=entry_time_value
        )
        position_notional = sizing["notional"]
        equity_before_entry = equity
        entry_price = exec_entry_price
        entry_time = entry_time_value
        rank_at_entry = rank
        pct24h_at_entry = pct
        cands_at_entry = eligible_count

    if progress_cb:
        progress_cb(1.0)

    return PortfolioResult(
        interval=interval,
        symbols_scanned=len(symbols),
        symbols_with_data=len(symbols),
        bars_total=total_bars,
        start_time=timeline[0],
        end_time=timeline[-1],
        trades=trades,
        skipped=skipped,
        warnings=list(
            dict.fromkeys(
                warnings
                + (
                    [
                        f"{trend_skips} dari {trend_scans} sinyal konfirmasi dilewati oleh "
                        f"gerbang trend {config.get('TREND_INTERVAL', '1h')}, sama seperti "
                        f"bot live."
                    ]
                    if trend_of and trend_skips
                    else []
                )
                + (
                    [
                        f"{htf_demand_skips} dari {htf_demand_scans} sinyal konfirmasi "
                        f"dilewati oleh gerbang zona demand "
                        f"{config.get('TREND_INTERVAL', '1h')}, sama seperti bot live."
                    ]
                    if trend_of and htf_demand_skips
                    else []
                )
                + (
                    [
                        f"{daily_demand_skips} dari {daily_demand_scans} sinyal konfirmasi "
                        f"dilewati oleh gerbang zona demand "
                        f"{config.get('DAILY_DEMAND_INTERVAL', '1d')}, sama seperti bot live."
                    ]
                    if daily_of and daily_demand_skips
                    else []
                )
                + (
                    [
                        f"{daily_trend_skips} dari {daily_trend_scans} sinyal konfirmasi "
                        f"dilewati oleh gerbang EMA + ADX "
                        f"{config.get('DAILY_TREND_INTERVAL', '1d')}, sama seperti bot live."
                    ]
                    if tren_harian_of and daily_trend_skips
                    else []
                )
                + (
                    [
                        f"{same_coin_skips} sinyal dilewati oleh blokir koin sama setelah "
                        f"loss (SAME_COIN_BLOCK_HOURS="
                        f"{parity.same_coin_block_hours(config):g} jam), sama seperti bot live."
                    ]
                    if same_coin_skips
                    else []
                )
            )
        ),
        initial_equity=initial_equity,
        final_equity=equity + mark_to_market_pnl_quote,
        risk_events=dict(controls.events),
        chase_skips=chase_skips,
        trend_skips=trend_skips,
        trend_scans=trend_scans,
        htf_demand_skips=htf_demand_skips,
        htf_demand_scans=htf_demand_scans,
        daily_demand_skips=daily_demand_skips,
        daily_demand_scans=daily_demand_scans,
        daily_trend_skips=daily_trend_skips,
        daily_trend_scans=daily_trend_scans,
        same_coin_skips=same_coin_skips,
        mark_to_market_open=mark_to_market_open,
        mark_to_market_symbol=mark_to_market_symbol,
        mark_to_market_pnl_pct=mark_to_market_pnl_pct,
        mark_to_market_pnl_quote=mark_to_market_pnl_quote,
        mark_to_market_hold_minutes=mark_to_market_hold_minutes,
    )


def summarize_portfolio(result: PortfolioResult) -> dict:
    all_trades = list(result.trades)
    legacy_mtm_trades = [t for t in all_trades if t.reason == "END_OF_DATA"]
    trades = [t for t in all_trades if t.reason != "END_OF_DATA"]
    total = len(trades)
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]

    initial = result.initial_equity
    equity = initial
    gross_equity = initial
    equity_curve = [0.0]
    for t in trades:
        notional = t.position_notional if t.position_notional > 0 else equity
        if t.equity_after > 0 or t.pnl_quote != 0:
            equity = t.equity_after
        else:
            equity = max(0.0, equity + notional * t.pnl_pct / 100.0)
        gross_equity = max(0.0, gross_equity + notional * t.gross_pnl_pct / 100.0)
        equity_curve.append((equity / initial - 1.0) * 100.0 if initial > 0 else 0.0)

    realized_equity = equity
    mark_to_market_open = bool(getattr(result, "mark_to_market_open", False))
    mark_to_market_symbol = getattr(result, "mark_to_market_symbol", None)
    mark_to_market_pnl_quote = float(
        getattr(result, "mark_to_market_pnl_quote", 0.0) or 0.0
    )
    mark_to_market_pnl_pct = float(
        getattr(result, "mark_to_market_pnl_pct", 0.0) or 0.0
    )
    mark_to_market_hold_minutes = float(
        getattr(result, "mark_to_market_hold_minutes", 0.0) or 0.0
    )
    if legacy_mtm_trades:
        mark_to_market_open = True
        mark_to_market_symbol = mark_to_market_symbol or legacy_mtm_trades[-1].symbol
        if not getattr(result, "mark_to_market_open", False):
            legacy_final_equity = float(getattr(result, "final_equity", 0.0) or 0.0)
            mark_to_market_pnl_quote = (
                legacy_final_equity - realized_equity
                if legacy_final_equity > 0
                else float(legacy_mtm_trades[-1].pnl_quote)
            )
            mark_to_market_pnl_pct = float(legacy_mtm_trades[-1].pnl_pct)
            mark_to_market_hold_minutes = float(legacy_mtm_trades[-1].hold_minutes)

    if mark_to_market_open:
        final_equity = max(0.0, realized_equity + mark_to_market_pnl_quote)
        gross_equity = max(0.0, gross_equity + mark_to_market_pnl_quote)
        equity_curve.append(
            (final_equity / initial - 1.0) * 100.0 if initial > 0 else 0.0
        )
    else:
        final_equity = realized_equity if trades else (result.final_equity or initial)

    total_return = ((final_equity / initial) - 1.0) * 100.0 if initial > 0 else 0.0
    realized_return = (
        ((realized_equity / initial) - 1.0) * 100.0 if initial > 0 else 0.0
    )
    mark_to_market_return = (
        (mark_to_market_pnl_quote / initial) * 100.0
        if initial > 0 and mark_to_market_open
        else 0.0
    )
    gross_return = ((gross_equity / initial) - 1.0) * 100.0 if initial > 0 else 0.0
    peak = initial
    max_dd = 0.0
    for value in [initial] + [initial * (1.0 + v / 100.0) for v in equity_curve[1:]]:
        peak = max(peak, value)
        max_dd = max(max_dd, ((peak - value) / peak * 100.0) if peak > 0 else 0.0)

    gross_win = sum(t.pnl_pct for t in wins)
    gross_loss = abs(sum(t.pnl_pct for t in losses))
    profit_factor = (
        (gross_win / gross_loss)
        if gross_loss > 0
        else (float("inf") if gross_win > 0 else 0.0)
    )

    reason_counts: dict = {}
    for t in trades:
        reason_counts[t.reason] = reason_counts.get(t.reason, 0) + 1

    symbol_counts: dict = {}
    symbol_pnl: dict = {}
    for t in trades:
        symbol_counts[t.symbol] = symbol_counts.get(t.symbol, 0) + 1
        symbol_pnl[t.symbol] = symbol_pnl.get(t.symbol, 0.0) + t.pnl_quote
    top_symbols = sorted(symbol_pnl.items(), key=lambda kv: kv[1], reverse=True)

    span_ms = max(1, result.end_time - result.start_time)
    span_days = span_ms / (24 * 60 * 60 * 1000)
    closed_hold_minutes = sum(t.hold_minutes for t in trades)
    total_hold = closed_hold_minutes + (
        mark_to_market_hold_minutes if mark_to_market_open else 0.0
    )
    exposure = (total_hold / (span_days * 24 * 60) * 100.0) if span_days > 0 else 0.0

    return {
        "total_trades": total,
        "closed_trades": total,
        "mark_to_market_open": mark_to_market_open,
        "mark_to_market_positions": int(mark_to_market_open),
        "mark_to_market_symbol": mark_to_market_symbol,
        "mark_to_market_pnl_pct": mark_to_market_pnl_pct,
        "mark_to_market_pnl_quote": mark_to_market_pnl_quote,
        "mark_to_market_return_pct": mark_to_market_return,
        "realized_return_pct": realized_return,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / total * 100.0) if total else 0.0,
        "total_return_pct": total_return,
        "gross_return_pct": gross_return,
        "fee_drag_pct": gross_return - total_return,
        "total_fee_pct": sum(t.fee_pct for t in trades),
        "max_drawdown_pct": max_dd,
        "avg_win_pct": (sum(t.pnl_pct for t in wins) / len(wins)) if wins else 0.0,
        "avg_loss_pct": (
            (sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0.0
        ),
        "profit_factor": profit_factor,
        "avg_hold_minutes": (closed_hold_minutes / total) if total else 0.0,
        "reason_counts": reason_counts,
        "equity_curve": equity_curve,
        "initial_equity": initial,
        "realized_equity": realized_equity,
        "final_equity": final_equity,
        "total_pnl_quote": final_equity - initial,
        "unique_symbols": len(symbol_counts),
        "symbols_in_universe": result.symbols_with_data,
        "top_symbols": [
            {
                "symbol": s,
                "pnl_quote": p,
                "pnl_pct": (p / initial * 100.0) if initial > 0 else 0.0,
                "trades": symbol_counts[s],
            }
            for s, p in top_symbols[:10]
        ],
        "worst_symbols": [
            {
                "symbol": s,
                "pnl_quote": p,
                "pnl_pct": (p / initial * 100.0) if initial > 0 else 0.0,
                "trades": symbol_counts[s],
            }
            for s, p in top_symbols[-5:][::-1]
            if p < 0
        ],
        "skipped_signals": len(result.skipped),
        "avg_rank_at_entry": (
            (sum(t.rank_at_entry for t in trades) / total) if total else 0.0
        ),
        "exposure_pct": exposure,
        "span_days": span_days,
        "trades_per_day": (total / span_days) if span_days > 0 else 0.0,
        "risk_events": dict(result.risk_events),
        "chase_skips": int(result.chase_skips),
        "trend_skips": int(getattr(result, "trend_skips", 0)),
        "trend_scans": int(getattr(result, "trend_scans", 0)),
        "htf_demand_skips": int(getattr(result, "htf_demand_skips", 0)),
        "htf_demand_scans": int(getattr(result, "htf_demand_scans", 0)),
        "daily_demand_skips": int(getattr(result, "daily_demand_skips", 0)),
        "daily_demand_scans": int(getattr(result, "daily_demand_scans", 0)),
        "daily_trend_skips": int(getattr(result, "daily_trend_skips", 0)),
        "daily_trend_scans": int(getattr(result, "daily_trend_scans", 0)),
        "same_coin_skips": int(getattr(result, "same_coin_skips", 0)),
        **parity.per_trade_metrics(trades),
    }


def _mk(t, o, h, low, c, qv=5_000_000.0):
    ms = t * 5 * MS_PER_MIN
    return Kline(
        open_time=ms,
        open=o,
        high=h,
        low=low,
        close=c,
        close_time=ms + 5 * MS_PER_MIN - 1,
        volume=1000.0,
        quote_volume=qv,
    )
