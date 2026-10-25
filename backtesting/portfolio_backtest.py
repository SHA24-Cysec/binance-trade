from __future__ import annotations

import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from bisect import bisect_left
from dataclasses import dataclass, field, replace
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


DATA_END_REASON = "DATA_END"


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
    confluence_structure_skips: int = 0
    confluence_structure_scans: int = 0
    confluence_trigger_skips: int = 0
    confluence_trigger_scans: int = 0
    same_coin_skips: int = 0
    mark_to_market_open: bool = False
    mark_to_market_symbol: Optional[str] = None
    mark_to_market_pnl_pct: float = 0.0
    mark_to_market_pnl_quote: float = 0.0
    mark_to_market_hold_minutes: float = 0.0
    intra_drawdown_pct: float = 0.0


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


def fetch_native_klines(
    client,
    symbols: list[str],
    interval: str,
    start_ms: int,
    end_ms: int,
    cache: Optional[KlineCache] = None,
    max_workers: int = 1,
    progress_cb: Optional[Callable[[float, str], None]] = None,
) -> dict[str, list[Kline]]:
    unique = [str(sym) for sym in dict.fromkeys(symbols or ())]
    if not unique:
        return {}
    out: dict[str, list[Kline]] = {}
    workers = max(1, int(max_workers or 1))
    done = 0
    total = max(1, len(unique))

    def _one(sym: str) -> list[Kline]:
        return _klines_untuk_simbol(client, sym, interval, start_ms, end_ms, cache)

    if workers == 1 or len(unique) == 1:
        for sym in unique:
            try:
                out[sym] = _one(sym)
            except Exception:
                out[sym] = []
            done += 1
            if progress_cb is not None:
                progress_cb(done / total, sym)
        return out

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="backtest-htf") as pool:
        futures = {pool.submit(_one, sym): sym for sym in unique}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                out[sym] = future.result()
            except Exception:
                out[sym] = []
            done += 1
            if progress_cb is not None:
                progress_cb(done / total, sym)
    return out


def build_native_lookups(
    series_by_symbol: dict[str, list],
    config: dict,
    factory,
) -> dict:
    lookups: dict = {}
    for sym, klines in (series_by_symbol or {}).items():
        if not klines:
            continue
        lookup = factory(klines, config)
        if lookup is not None:
            lookups[sym] = lookup
    return lookups


def fetch_native_lookups(
    client,
    symbols: list[str],
    config: dict,
    start_ms: int,
    end_ms: int,
    cache: Optional[KlineCache] = None,
    max_workers: int = 1,
    progress_cb: Optional[Callable[[float, str], None]] = None,
) -> dict:
    struktur_of: dict = {}
    unique = [str(sym) for sym in dict.fromkeys(symbols or ())]
    if not unique:
        return struktur_of

    if strategy.confluence_enabled(config) and strategy.confluence_uses_mtf(config):
        itv = strategy.confluence_structure_interval(config)
        need_ms = (
            strategy.confluence_structure_window_bars(config)
            * strategy.INTERVAL_MINUTES.get(itv, 15)
            * 60_000
        )
        series = fetch_native_klines(
            client,
            unique,
            itv,
            int(start_ms) - int(need_ms),
            int(end_ms),
            cache,
            max_workers,
            progress_cb,
        )
        confirm_itv = str(config.get("CONFIRM_INTERVAL", "5m") or "5m")
        struktur_of = build_native_lookups(
            series,
            config,
            lambda klines, cfg: parity.make_structure_lookup(
                klines, cfg, confirm_itv, sudah_dirangkai=True
            ),
        )

    return struktur_of


def build_timeline(
    store: KlineStore,
    interval: str,
    symbols: Optional[list[str]] = None,
    config: Optional[dict] = None,
    include_ohlcv: bool = False,
    structure_store: Optional[KlineStore] = None,
) -> tuple:
    window = bars_per_day(interval)
    series_of: dict[str, SymbolSeries] = {}
    struktur_of: dict = {}
    all_times: set = set()
    pakai_struktur = bool(config and strategy.confluence_enabled(config))
    daftar = symbols if symbols is not None else store.symbols()
    struct_symbols = (
        set(structure_store.symbols()) if structure_store is not None else set()
    )

    for sym in daftar:
        kl = store.klines(sym)
        if not kl:
            continue
        stats = compute_rolling_24h_stats(kl, window)
        series = SymbolSeries.build(sym, kl, stats, include_ohlcv=include_ohlcv)
        series_of[sym] = series
        all_times.update(series.open_times())
        if pakai_struktur:
            if structure_store is not None and sym in struct_symbols:
                struktur_of[sym] = parity.make_structure_lookup(
                    structure_store.klines(sym),
                    config,
                    interval,
                    sudah_dirangkai=True,
                )
            else:
                struktur_of[sym] = parity.make_structure_lookup(
                    kl, config, interval
                )

    timeline = sorted(all_times)
    if config is None:
        return timeline, series_of
    return timeline, series_of, struktur_of


def unpack_prebuilt(prebuilt: Optional[tuple]) -> tuple:
    if prebuilt is None:
        return None, None, None
    if len(prebuilt) == 2:
        return prebuilt[0], prebuilt[1], None
    if len(prebuilt) >= 3:
        return prebuilt[0], prebuilt[1], prebuilt[2]
    raise ValueError(
        f"prebuilt harus tuple 2 atau 3 elemen, bukan {len(prebuilt)}"
    )


def _siapkan_struktur(
    prebuilt: Optional[tuple],
    store: KlineStore,
    series_of: dict,
    config: dict,
    interval: str,
    structure_store: Optional[KlineStore] = None,
) -> dict:
    if not strategy.confluence_enabled(config):
        return {}
    _, _, struktur_bawaan = unpack_prebuilt(prebuilt)
    if struktur_bawaan is not None:
        return struktur_bawaan
    if structure_store is not None:
        tersedia = set(structure_store.symbols())
        return {
            sym: parity.make_structure_lookup(
                (
                    structure_store.klines(sym)
                    if sym in tersedia
                    else store.klines(sym)
                ),
                config,
                interval,
                sudah_dirangkai=(sym in tersedia),
            )
            for sym in series_of
        }
    return {
        sym: parity.make_structure_lookup(store.klines(sym), config, interval)
        for sym in series_of
    }


@dataclass
class EntrySignalCache:
    signals: dict = field(default_factory=dict)
    confluence_structure_scans: int = 0
    confluence_structure_skips: int = 0
    confluence_trigger_scans: int = 0
    confluence_trigger_skips: int = 0
    confluence_structure_events: list = field(default_factory=list)
    confluence_trigger_events: list = field(default_factory=list)
    confluence_structure_scans_by_time: dict = field(default_factory=dict)
    confluence_structure_skips_by_time: dict = field(default_factory=dict)
    confluence_trigger_scans_by_time: dict = field(default_factory=dict)
    confluence_trigger_skips_by_time: dict = field(default_factory=dict)
    sl_buffer_pct: float = 0.20
    min_rr_ratio: float = 1.2

    def get(self, t_now: int, default=None):
        return self.signals.get(t_now, default)

    def __len__(self) -> int:
        return len(self.signals)

    def __iter__(self):
        return iter(self.signals)

    def __contains__(self, item) -> bool:
        return item in self.signals

    def __bool__(self) -> bool:
        return True

    def keys(self):
        return self.signals.keys()

    def items(self):
        return self.signals.items()

    def values(self):
        return self.signals.values()


def precompute_entry_signals(
    store: KlineStore,
    config: dict,
    interval: str,
    warmup_ms: int | tuple = 0,
    prebuilt: Optional[tuple] = None,
    btc_klines: Optional[list] = None,
    progress_cb: Optional[Callable[[float], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    structure_store: Optional[KlineStore] = None,
    max_skipped_records: int = 400,
) -> EntrySignalCache:
    if isinstance(warmup_ms, tuple):
        prebuilt = warmup_ms
        warmup_ms = 0
    warmup_ms_int = int(warmup_ms or 0)
    tradable_meta = config.get("_historical_tradable_symbols")
    symbols = [
        sym
        for sym in store.symbols()
        if scanner.is_structurally_allowed_symbol(sym, config, tradable_meta)
    ]
    if not symbols:
        return EntrySignalCache({})

    timeline, series_of, struktur_of = unpack_prebuilt(prebuilt)
    if timeline is None:
        timeline, series_of, struktur_of = build_timeline(
            store, interval, symbols, config, structure_store=structure_store
        )
    else:
        symbols = [sym for sym in symbols if sym in series_of]
        if not struktur_of:
            struktur_of = _siapkan_struktur(
                prebuilt,
                store,
                series_of,
                config,
                interval,
                structure_store=structure_store,
            )

    if not timeline or not symbols:
        return EntrySignalCache({})

    lookback = strategy.confirm_window_bars(config)
    min_vol = float(config["MIN_QUOTE_VOLUME_USDT_24H"])
    top_n = int(config.get("TOP_N_CANDIDATES_TO_CONFIRM", 10))
    store.set_cache_size(_resolve_symbol_cache_size(config, top_n))

    bar_ms = INTERVAL_MINUTES.get(interval, 5) * MS_PER_MIN
    btc_lookup = parity.make_btc_lookup(btc_klines, config, bar_ms)
    gate_cfg = parity.gate_config(config, btc_lookup)
    first_allowed_time = timeline[0] + warmup_ms_int

    papan_input = []
    for sym in symbols:
        ot, ct, pct_arr, vol_arr, ready_arr = series_of[sym].board_arrays()
        papan_input.append((sym, ot, ct, pct_arr, vol_arr, ready_arr, len(ot)))

    signals = EntrySignalCache(
        {},
        sl_buffer_pct=float(config.get("CONFLUENCE_SL_BUFFER_PCT", 0.20) or 0.0),
        min_rr_ratio=float(config.get("CONFLUENCE_MIN_RR_RATIO", 1.2) or 1.2),
    )
    total_bars = len(timeline)

    for bi, t_now in enumerate(timeline):
        if progress_cb and bi % 100 == 0:
            progress_cb(bi / max(1, total_bars))
        if cancel_cb is not None and bi % 200 == 0 and cancel_cb():
            raise BacktestError("Backtest dibatalkan.")
        if t_now < first_allowed_time:
            continue

        btc_drop = btc_lookup.drop_pct_at(t_now) if btc_lookup is not None else None
        board = []
        for sym, ot, ct, pct_arr, vol_arr, ready_arr, n_bar in papan_input:
            pos = bisect_left(ot, t_now)
            if pos >= n_bar or ot[pos] != t_now:
                continue
            if not ready_arr[pos]:
                continue
            vol24 = vol_arr[pos]
            if vol24 < min_vol:
                continue
            gate_ok, _ = scanner.evaluate_market_gate(
                vol24, gate_cfg, btc_drop_pct=btc_drop
            )
            if not gate_ok:
                continue
            board.append((vol24, sym, pos, ct[pos]))

        if not board:
            continue
        board.sort(key=lambda x: -x[0])

        lolos = []
        for rank, (vol24, sym, i, close_time) in enumerate(
            board[:top_n], start=1
        ):
            if i + 1 < lookback:
                continue
            waktu_bar = int(t_now)
            verdict_struktur = None
            if struktur_of:
                signals.confluence_structure_scans += 1
                signals.confluence_structure_scans_by_time[waktu_bar] = (
                    signals.confluence_structure_scans_by_time.get(waktu_bar, 0) + 1
                )
                lock = struktur_of.get(sym)
                verdict_struktur = (
                    lock.verdict_at(close_time)
                    if lock is not None
                    else {"ok": False}
                )
                if not verdict_struktur["ok"]:
                    signals.confluence_structure_skips += 1
                    signals.confluence_structure_skips_by_time[waktu_bar] = (
                        signals.confluence_structure_skips_by_time.get(
                            waktu_bar, 0
                        )
                        + 1
                    )
                    if len(signals.confluence_structure_events) < max_skipped_records:
                        signals.confluence_structure_events.append((waktu_bar, sym))
                    continue

            sym_series = series_of[sym]
            start_w = max(0, i - lookback + 1)
            if sym_series.has_ohlcv:
                window_kl = sym_series.klines_slice(start_w, i + 1)
            else:
                kl = store.klines(sym)
                window_kl = kl[start_w : i + 1]
            if strategy.confluence_enabled(config):
                signals.confluence_trigger_scans += 1
                signals.confluence_trigger_scans_by_time[waktu_bar] = (
                    signals.confluence_trigger_scans_by_time.get(waktu_bar, 0) + 1
                )
            try:
                setup = scanner.detect_entry_setup(
                    window_kl,
                    config,
                    structure_verdict=verdict_struktur,
                )
            except Exception as exc:
                raise BacktestError(
                    f"Evaluasi sinyal {sym} pada waktu {int(t_now)} "
                    f"(bar {i}) gagal: {type(exc).__name__}: {str(exc)[:160]}"
                ) from exc
            if not setup.ok:
                if strategy.confluence_enabled(config):
                    signals.confluence_trigger_skips += 1
                    signals.confluence_trigger_skips_by_time[waktu_bar] = (
                        signals.confluence_trigger_skips_by_time.get(waktu_bar, 0) + 1
                    )
                    if len(signals.confluence_trigger_events) < max_skipped_records:
                        signals.confluence_trigger_events.append((waktu_bar, sym))
                continue
            lolos.append((rank, sym, i, vol24, setup))

        if not lolos:
            continue

        lolos.sort(
            key=lambda row: scanner.setup_quality_key(
                row[4],
                scanner.Candidate(
                    row[1],
                    "",
                    row[3],
                    (
                        series_of[row[1]].kline_at(row[2]).close
                        if series_of[row[1]].has_ohlcv
                        else store.klines(row[1])[row[2]].close
                    ),
                ),
            )
        )
        signals.signals[int(t_now)] = (len(board), lolos)

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

    timeline, series_of, struktur_of = unpack_prebuilt(prebuilt)
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
    native_struct = prebuilt is not None and struktur_of is not None
    if not native_struct:
        struktur_of = _siapkan_struktur(prebuilt, store, series_of, config, interval)
    if struktur_of and not native_struct:
        _butuh_gerbang = strategy.gate_warmup_ms(config, interval)
        if int(warmup_ms) < _butuh_gerbang:
            _pre_warnings.append(
                f"Warmup {warmup_ms} ms dinaikkan menjadi {_butuh_gerbang} ms karena "
                f"struktur Confluence {strategy.confluence_structure_interval(config)} "
                f"membutuhkan {strategy.confluence_structure_window_bars(config)} candle "
                "tertutup sebelum bar entry pertama. Unduhan data harus mencakup rentang warmup ini."
            )
            warmup_ms = _butuh_gerbang
    _butuh = strategy.required_lookback_bars(config)
    if int(config.get("CONFIRM_LOOKBACK_BARS", 0)) < _butuh:
        _pre_warnings.append(
            f"CONFIRM_LOOKBACK_BARS={config.get('CONFIRM_LOOKBACK_BARS')} lebih kecil dari "
            f"{_butuh} candle yang dibutuhkan gerbang entry. Simulasi memakai "
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
    peak_equity = initial_equity
    intra_drawdown_pct = 0.0

    holding: Optional[str] = None
    position: Optional[parity.PositionState] = None
    entry_price = 0.0
    entry_time = 0
    position_notional = 0.0
    equity_before_entry = 0.0
    rank_at_entry = 0
    cands_at_entry = 0
    next_entry_allowed_at = 0
    chase_skips = 0
    confluence_structure_skips = 0
    confluence_structure_scans = 0
    confluence_trigger_skips = 0
    confluence_trigger_scans = 0
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

    papan_input = []
    for sym in symbols:
        ot, ct, pct_arr, vol_arr, ready_arr = series_of[sym].board_arrays()
        papan_input.append((sym, ot, ct, pct_arr, vol_arr, ready_arr, len(ot)))

    def _event_dalam_jendela(daftar: list):
        for waktu_event, simbol_event in daftar:
            if start_ms is not None and waktu_event < start_ms:
                continue
            if end_ms is not None and waktu_event > end_ms:
                continue
            yield waktu_event, simbol_event

    def _jumlah_dalam_jendela(daftar: dict) -> int:
        return sum(
            int(jumlah)
            for waktu, jumlah in (daftar or {}).items()
            if int(waktu) >= int(first_allowed_time)
            and int(waktu) <= int(timeline[-1])
        )

    if entry_signal_cache is not None:
        if start_ms is not None or end_ms is not None:
            warnings.append(
                "Cache sinyal entry dipakai bersama jendela waktu. Daftar sinyal "
                "yang dilewati filter disaring ke dalam jendela waktu uji."
            )
        struct_scan_map = getattr(
            entry_signal_cache, "confluence_structure_scans_by_time", None
        )
        struct_skip_map = getattr(
            entry_signal_cache, "confluence_structure_skips_by_time", None
        )
        if isinstance(struct_scan_map, dict) and struct_scan_map:
            confluence_structure_scans += _jumlah_dalam_jendela(struct_scan_map)
        else:
            confluence_structure_scans += int(
                getattr(entry_signal_cache, "confluence_structure_scans", 0)
            )
        if isinstance(struct_skip_map, dict) and struct_skip_map:
            confluence_structure_skips += _jumlah_dalam_jendela(struct_skip_map)
        else:
            confluence_structure_skips += int(
                getattr(entry_signal_cache, "confluence_structure_skips", 0)
            )
        trig_scan_map = getattr(
            entry_signal_cache, "confluence_trigger_scans_by_time", None
        )
        trig_skip_map = getattr(
            entry_signal_cache, "confluence_trigger_skips_by_time", None
        )
        if isinstance(trig_scan_map, dict) and trig_scan_map:
            confluence_trigger_scans += _jumlah_dalam_jendela(trig_scan_map)
        else:
            confluence_trigger_scans += int(
                getattr(entry_signal_cache, "confluence_trigger_scans", 0)
            )
        if isinstance(trig_skip_map, dict) and trig_skip_map:
            confluence_trigger_skips += _jumlah_dalam_jendela(trig_skip_map)
        else:
            confluence_trigger_skips += int(
                getattr(entry_signal_cache, "confluence_trigger_skips", 0)
            )
        for waktu_event, simbol_event in _event_dalam_jendela(
            getattr(entry_signal_cache, "confluence_structure_events", [])
        ):
            if len(skipped) >= max_skipped_records:
                break
            skipped.append(
                SkippedSignal(
                    time=waktu_event,
                    symbol=simbol_event,
                    reason="FILTER_CONFLUENCE_STRUCTURE",
                    holding=None,
                )
            )
        for waktu_event, simbol_event in _event_dalam_jendela(
            getattr(entry_signal_cache, "confluence_trigger_events", [])
        ):
            if len(skipped) >= max_skipped_records:
                break
            skipped.append(
                SkippedSignal(
                    time=waktu_event,
                    symbol=simbol_event,
                    reason="FILTER_CONFLUENCE_TRIGGER",
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
                gate_ok, _gate_reason = scanner.evaluate_market_gate(
                    vol24, gate_cfg, btc_drop_pct=btc_drop
                )
                if not gate_ok:
                    continue
                board.append((vol24, sym, pos))
            board.sort(key=lambda x: -x[0])

        if holding is not None:
            held_series = series_of[holding]
            hi = held_series.index_at(t_now)
            data_end_close = False
            if hi is None:
                hi = held_series.index_at_or_before(t_now)
                if hi is None or hi + 1 < len(held_series):
                    continue
                data_end_close = True
            candle = (
                held_series.kline_at(hi)
                if entry_signal_cache is not None and held_series.has_ohlcv
                else store.klines(holding)[hi]
            )
            if candle.open_time < entry_time and not data_end_close:
                continue
            hold_minutes = max(0.0, (candle.close_time - entry_time) / 60000.0)

            exit_reason = None
            exit_price = None
            mtm_equity = equity
            if data_end_close:
                exit_reason = DATA_END_REASON
                exit_price = candle.close
                warnings.append(
                    f"{holding} tidak lagi menerbitkan candle setelah "
                    f"{int(candle.close_time)}. Posisi yang masih terbuka ditutup "
                    "pada close terakhir yang tersedia supaya ekuitas dan slot "
                    "trading tidak menggantung sampai akhir simulasi."
                )
            else:
                verdict = parity.evaluate_candle_exit(
                    position, candle, config, cap_ms=cap_ms, bar_ms=bar_ms
                )
                if verdict is not None:
                    exit_reason, exit_price = verdict

                if exit_reason is None:
                    mtm_pnl_pct = (candle.close / entry_price - 1.0) * 100.0
                    mtm_pnl_quote = position_notional * mtm_pnl_pct / 100.0
                    mtm_equity = equity + mtm_pnl_quote
                    entries_paused = controls.update(candle.close_time, mtm_equity)
                    if controls.force_close_due(entries_paused, True):
                        exit_reason = parity.RISK_LIMIT_REASON
                        exit_price = candle.close

            if exit_reason is None:
                if mtm_equity > peak_equity:
                    peak_equity = mtm_equity
                elif peak_equity > 0:
                    dd_now = (peak_equity - mtm_equity) / peak_equity * 100.0
                    if dd_now > intra_drawdown_pct:
                        intra_drawdown_pct = dd_now

            if exit_reason:
                raw_exit_price = exit_price
                exit_price = strategy.backtest_sell_execution_price(
                    raw_exit_price, execution_spread_pct, execution_slippage_pct
                )
                gross = (exit_price / entry_price - 1.0) * 100.0
                net = gross - fee_round_trip
                pnl_quote = position_notional * net / 100.0
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
                        pnl_pct=net,
                        gross_pnl_pct=gross,
                        fee_pct=fee_round_trip,
                        sl_pct=position.levels["sl"],
                        tp_pct=position.levels["tp"],
                        exit_source=position.levels["src"],
                        rank_at_entry=rank_at_entry,
                        candidates_at_entry=cands_at_entry,
                        position_notional=position_notional,
                        equity_before=equity_before_entry,
                        equity_after=equity_after,
                        pnl_quote=pnl_quote,
                    )
                )
                equity = equity_after
                if equity > peak_equity:
                    peak_equity = equity
                elif peak_equity > 0:
                    dd_now = (peak_equity - equity) / peak_equity * 100.0
                    if dd_now > intra_drawdown_pct:
                        intra_drawdown_pct = dd_now
                parity.same_coin_block_record(
                    symbol_blocks, holding, candle.close_time, pnl_quote, config
                )
                holding = None
                position = None
                position_notional = 0.0
                next_entry_allowed_at = parity.next_entry_allowed(
                    candle.close_time, config
                )
                controls.update(candle.close_time, equity)
                continue

            if len(skipped) < max_skipped_records:
                if entry_signal_cache is None:
                    for _rank, (_vol24, sym, i) in enumerate(
                        board[:top_n], start=1
                    ):
                        if sym == holding:
                            continue
                        kl = store.klines(sym)
                        window_kl = kl[max(0, i - lookback + 1) : i + 1]
                        verdict_struktur = None
                        if struktur_of:
                            lock = struktur_of.get(sym)
                            verdict_struktur = (
                                lock.verdict_at(kl[i].close_time)
                                if lock is not None
                                else {"ok": False}
                            )
                            if not verdict_struktur["ok"]:
                                continue
                        setup_skip = scanner.detect_entry_setup(
                            window_kl,
                            config,
                            structure_verdict=verdict_struktur,
                        )
                        if setup_skip.ok:
                            skipped.append(
                                SkippedSignal(
                                    time=t_now,
                                    symbol=sym,
                                    reason="POSISI_AKTIF",
                                    holding=holding,
                                )
                            )
                            break
                else:
                    cached = entry_signal_cache.get(int(t_now))
                    if cached:
                        for _rank, sym, _i, _vol24, _setup in cached[1]:
                            if sym == holding:
                                continue
                            if parity.same_coin_blocked(sym, t_now, symbol_blocks):
                                continue
                            skipped.append(
                                SkippedSignal(
                                    time=t_now,
                                    symbol=sym,
                                    reason="POSISI_AKTIF",
                                    holding=holding,
                                )
                            )
                            break
            continue

        entries_paused = controls.update(t_now, equity)
        if entries_paused:
            continue
        if t_now < first_allowed_time:
            continue
        if t_now < next_entry_allowed_at:
            if len(skipped) < max_skipped_records:
                if entry_signal_cache is None and board:
                    for _vol24, sym, i in board[:top_n]:
                        kl = store.klines(sym)
                        window_kl = kl[max(0, i - lookback + 1) : i + 1]
                        verdict_struktur = None
                        if struktur_of:
                            lock = struktur_of.get(sym)
                            verdict_struktur = (
                                lock.verdict_at(kl[i].close_time)
                                if lock is not None
                                else {"ok": False}
                            )
                            if not verdict_struktur["ok"]:
                                continue
                        if scanner.detect_entry_setup(
                            window_kl, config, structure_verdict=verdict_struktur
                        ).ok:
                            skipped.append(
                                SkippedSignal(
                                    time=t_now,
                                    symbol=sym,
                                    reason="COOLDOWN",
                                    holding=None,
                                )
                            )
                            break
                elif entry_signal_cache is not None:
                    cached = entry_signal_cache.get(int(t_now))
                    if cached:
                        for _rank, sym, _i, _vol24, _setup in cached[1]:
                            if parity.same_coin_blocked(sym, t_now, symbol_blocks):
                                continue
                            skipped.append(
                                SkippedSignal(
                                    time=t_now,
                                    symbol=sym,
                                    reason="COOLDOWN",
                                    holding=None,
                                )
                            )
                            break
            continue

        if entry_signal_cache is None:
            if not board:
                continue

            eligible = board[:top_n]
            lolos = []
            for rank, (vol24, sym, i) in enumerate(eligible, start=1):
                if i + 1 < lookback:
                    continue
                kl = store.klines(sym)
                verdict_struktur = None
                if struktur_of:
                    confluence_structure_scans += 1
                    lock = struktur_of.get(sym)
                    verdict_struktur = (
                        lock.verdict_at(kl[i].close_time)
                        if lock is not None
                        else {"ok": False}
                    )
                    if not verdict_struktur["ok"]:
                        confluence_structure_skips += 1
                        if len(skipped) < max_skipped_records:
                            skipped.append(
                                SkippedSignal(
                                    time=t_now,
                                    symbol=sym,
                                    reason="FILTER_CONFLUENCE_STRUCTURE",
                                    holding=None,
                                )
                            )
                        continue
                window_kl = kl[max(0, i - lookback + 1) : i + 1]
                if strategy.confluence_enabled(config):
                    confluence_trigger_scans += 1
                try:
                    setup = scanner.detect_entry_setup(
                        window_kl,
                        config,
                        structure_verdict=verdict_struktur,
                    )
                except Exception as exc:
                    raise BacktestError(
                        f"Evaluasi sinyal {sym} pada waktu {int(t_now)} "
                        f"(bar {i}) gagal: {type(exc).__name__}: {str(exc)[:160]}"
                    ) from exc
                if not setup.ok:
                    if strategy.confluence_enabled(config):
                        confluence_trigger_skips += 1
                        if len(skipped) < max_skipped_records:
                            skipped.append(
                                SkippedSignal(
                                    time=t_now,
                                    symbol=sym,
                                    reason="FILTER_CONFLUENCE_TRIGGER",
                                    holding=None,
                                )
                            )
                    continue
                lolos.append((rank, sym, i, vol24, setup))

            if not lolos:
                continue

            lolos.sort(
                key=lambda row: scanner.setup_quality_key(
                    row[4],
                    scanner.Candidate(
                        row[1], "", row[3], store.klines(row[1])[row[2]].close
                    ),
                )
            )
            eligible_count = len(eligible)
        else:
            cached_entry = entry_signal_cache.get(int(t_now))
            if cached_entry is None:
                continue
            eligible_count, raw_lolos = cached_entry
            if not raw_lolos:
                continue
            cfg_buf = float(config.get("CONFLUENCE_SL_BUFFER_PCT", 0.20) or 0.0)
            cfg_rr = float(config.get("CONFLUENCE_MIN_RR_RATIO", 1.2) or 1.2)
            same_exit_cfg = (
                abs(cfg_buf - getattr(entry_signal_cache, "sl_buffer_pct", 0.20)) < 1e-9
                and abs(cfg_rr - getattr(entry_signal_cache, "min_rr_ratio", 1.2)) < 1e-9
            )
            if same_exit_cfg:
                lolos = list(raw_lolos)
            else:
                lolos = []
                for r_rank, r_sym, r_i, r_vol24, r_setup in raw_lolos:
                    if not strategy.confluence_enabled(config):
                        fb = strategy.compute_confluence_exit_levels(
                            float(r_setup.signal_close or 100.0),
                            config,
                            fallback_allowed=True,
                        )
                        lolos.append(
                            (
                                r_rank,
                                r_sym,
                                r_i,
                                r_vol24,
                                replace(
                                    r_setup,
                                    sl_price=fb["sl_price"],
                                    tp_price=fb["tp_price"],
                                    sl_pct=fb["sl_pct"],
                                    tp_pct=fb["tp_pct"],
                                    rr_ratio=fb["rr_ratio"],
                                ),
                            )
                        )
                        continue
                    info = strategy.compute_confluence_exit_levels(
                        float(r_setup.signal_close or 0.0),
                        config,
                        poi_low=r_setup.poi_low,
                        ob_low=r_setup.ob_low,
                        swing_low=r_setup.swing_low,
                        sweep_low=r_setup.sweep_low,
                        bsl_level=r_setup.bsl_level,
                        swing_high=r_setup.swing_high,
                        fallback_allowed=False,
                    )
                    if not info["ok"]:
                        confluence_trigger_skips += 1
                        if len(skipped) < max_skipped_records:
                            skipped.append(
                                SkippedSignal(
                                    time=t_now,
                                    symbol=r_sym,
                                    reason="FILTER_CONFLUENCE_TRIGGER",
                                    holding=None,
                                )
                            )
                        continue
                    lolos.append(
                        (
                            r_rank,
                            r_sym,
                            r_i,
                            r_vol24,
                            replace(
                                r_setup,
                                sl_price=info["sl_price"],
                                tp_price=info["tp_price"],
                                sl_pct=info["sl_pct"],
                                tp_pct=info["tp_pct"],
                                rr_ratio=info["rr_ratio"],
                            ),
                        )
                    )
                if not lolos:
                    continue
        lolos_tersedia = []
        for row in lolos:
            if parity.same_coin_blocked(row[1], t_now, symbol_blocks):
                same_coin_skips += 1
                if len(skipped) < max_skipped_records:
                    skipped.append(
                        SkippedSignal(
                            time=t_now,
                            symbol=row[1],
                            reason="SAME_COIN_BLOCK",
                            holding=None,
                        )
                    )
                continue
            lolos_tersedia.append(row)
        lolos = lolos_tersedia
        if not lolos:
            continue
        rank, sym, i, _vol24, setup_terpilih = lolos[0]
        sizing = strategy.resolve_position_notional(config, equity)
        if sizing["notional"] <= 0 or sizing["notional"] > equity:
            continue

        if len(skipped) < max_skipped_records:
            for _r2, sym2, _i2, _v2, _s2 in lolos:
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
        lv = strategy.resolve_exit_levels(
            config, setup=setup_terpilih, entry_price=exec_entry_price
        )
        holding = sym
        position = parity.PositionState(
            exec_entry_price, lv, entry_time=entry_time_value
        )
        position_notional = sizing["notional"]
        equity_before_entry = equity
        entry_price = exec_entry_price
        entry_time = entry_time_value
        rank_at_entry = rank
        cands_at_entry = eligible_count
        if entry_delay_bars > 0:
            next_bi = bisect_left(timeline, entry_time_value)
            if next_bi > bi:
                bi = next_bi

    if holding is not None and entry_price > 0:
        held_series = series_of[holding]
        last_idx = held_series.index_at_or_before(timeline[-1])
        if last_idx is None:
            last_idx = len(held_series) - 1
        last_candle = (
            held_series.kline_at(last_idx)
            if entry_signal_cache is not None and held_series.has_ohlcv
            else store.klines(holding)[last_idx]
        )
        mark_to_market_open = True
        mark_to_market_symbol = holding
        mark_to_market_pnl_pct = (last_candle.close / entry_price - 1.0) * 100.0
        mark_to_market_pnl_quote = position_notional * mark_to_market_pnl_pct / 100.0
        mark_to_market_hold_minutes = max(
            0.0, (last_candle.close_time - entry_time) / 60000.0
        )
        warnings.append(
            f"Posisi terakhir ({holding}) masih terbuka pada akhir data. "
            f"Nilainya dicatat sebagai mark-to-market ({mark_to_market_pnl_pct:+.2f}%), "
            f"bukan sebagai trade tertutup."
        )

    if same_coin_skips:
        warnings.append(
            f"{same_coin_skips} sinyal dilewati oleh blokir koin sama setelah loss "
            f"(SAME_COIN_BLOCK_HOURS={parity.same_coin_block_hours(config):g} jam), "
            "sama seperti bot live."
        )
    if chase_skips:
        warnings.append(
            f"{chase_skips} sinyal dilewati oleh filter MAX_CHASE_PCT "
            f"({max_chase_pct:g}%), sama seperti bot live."
        )
    if confluence_structure_skips:
        warnings.append(
            f"{confluence_structure_skips} dari {confluence_structure_scans} kandidat top-N "
            f"dilewati oleh struktur POI The Confluence Code "
            f"({strategy.confluence_structure_interval(config)})."
        )
    if confluence_trigger_skips:
        warnings.append(
            f"{confluence_trigger_skips} dari {confluence_trigger_scans} struktur POI "
            f"dilewati oleh pemicu eksekusi The Confluence Code ({interval})."
        )
    risk_summary = controls.summary()
    if risk_summary["kill_switch_triggers"] > 0:
        warnings.append(
            f"Kill switch drawdown aktif {risk_summary['kill_switch_triggers']} kali "
            f"(batas {risk_summary['max_drawdown_limit_pct']}%). "
            f"Force close: {risk_summary['force_closes']}, "
            f"reset cooldown: {risk_summary['cooldown_resets']}."
        )

    if progress_cb:
        progress_cb(1.0)

    return PortfolioResult(
        interval=interval,
        symbols_scanned=len(symbols),
        symbols_with_data=len(series_of),
        bars_total=total_bars,
        start_time=first_allowed_time,
        end_time=timeline[-1],
        trades=trades,
        skipped=skipped,
        warnings=warnings,
        initial_equity=initial_equity,
        final_equity=equity,
        risk_events=risk_summary,
        chase_skips=chase_skips,
        confluence_structure_skips=confluence_structure_skips,
        confluence_structure_scans=confluence_structure_scans,
        confluence_trigger_skips=confluence_trigger_skips,
        confluence_trigger_scans=confluence_trigger_scans,
        same_coin_skips=same_coin_skips,
        mark_to_market_open=mark_to_market_open,
        mark_to_market_symbol=mark_to_market_symbol,
        mark_to_market_pnl_pct=mark_to_market_pnl_pct,
        mark_to_market_pnl_quote=mark_to_market_pnl_quote,
        mark_to_market_hold_minutes=mark_to_market_hold_minutes,
        intra_drawdown_pct=intra_drawdown_pct,
    )


def summarize_portfolio(result: PortfolioResult) -> dict:
    all_trades = list(result.trades)
    legacy_mtm_trades = [t for t in all_trades if t.reason == "END_OF_DATA"]
    trades = [t for t in all_trades if t.reason != "END_OF_DATA"]
    total = len(trades)
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]

    initial = float(result.initial_equity or 0.0)
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
    max_dd = max(max_dd, abs(float(getattr(result, "intra_drawdown_pct", 0.0) or 0.0)))

    gross_win = sum(t.pnl_pct for t in wins)
    gross_loss = abs(sum(t.pnl_pct for t in losses))
    profit_factor = (
        (gross_win / gross_loss)
        if gross_loss > 0
        else (float("inf") if gross_win > 0 else 0.0)
    )

    reason_counts: dict = {}
    by_reason: dict = {}
    for t in trades:
        reason_counts[t.reason] = reason_counts.get(t.reason, 0) + 1
        slot = by_reason.setdefault(t.reason, {"count": 0, "sum_pnl": 0.0, "wins": 0})
        slot["count"] += 1
        slot["sum_pnl"] += t.pnl_pct
        if t.pnl_pct > 0:
            slot["wins"] += 1
    for _r, slot in by_reason.items():
        c = slot["count"]
        slot["avg_pnl_pct"] = round(slot["sum_pnl"] / c, 3) if c else 0.0
        slot["win_rate"] = round(slot["wins"] / c * 100.0, 1) if c else 0.0
        del slot["sum_pnl"]

    symbol_counts: dict = {}
    symbol_pnl: dict = {}
    coin_map: dict = {}
    for t in trades:
        symbol_counts[t.symbol] = symbol_counts.get(t.symbol, 0) + 1
        symbol_pnl[t.symbol] = symbol_pnl.get(t.symbol, 0.0) + t.pnl_quote
        slot = coin_map.setdefault(
            t.symbol,
            {"symbol": t.symbol, "trades": 0, "wins": 0, "sum_pnl": 0.0},
        )
        slot["trades"] += 1
        slot["sum_pnl"] += t.pnl_pct
        if t.pnl_pct > 0:
            slot["wins"] += 1
    top_symbols = sorted(symbol_pnl.items(), key=lambda kv: kv[1], reverse=True)
    by_coin = []
    for sym, slot in coin_map.items():
        c = slot["trades"]
        by_coin.append(
            {
                "symbol": sym,
                "trades": c,
                "wins": slot["wins"],
                "win_rate": round(slot["wins"] / c * 100.0, 1) if c else 0.0,
                "total_pnl_pct": round(slot["sum_pnl"], 3),
                "avg_pnl_pct": round(slot["sum_pnl"] / c, 3) if c else 0.0,
            }
        )
    by_coin.sort(key=lambda x: x["total_pnl_pct"], reverse=True)

    span_ms = max(1, result.end_time - result.start_time)
    span_days = span_ms / (24 * 60 * 60 * 1000)
    closed_hold_minutes = sum(t.hold_minutes for t in trades)
    total_hold = closed_hold_minutes + (
        mark_to_market_hold_minutes if mark_to_market_open else 0.0
    )
    exposure = (total_hold / (span_days * 24 * 60) * 100.0) if span_days > 0 else 0.0
    trade_metrics = parity.per_trade_metrics(trades)

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
        "avg_pnl_pct": trade_metrics["avg_trade_pct"],
        "avg_win_pct": (sum(t.pnl_pct for t in wins) / len(wins)) if wins else 0.0,
        "avg_loss_pct": (
            (sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0.0
        ),
        "profit_factor": profit_factor,
        "avg_hold_minutes": (closed_hold_minutes / total) if total else 0.0,
        "reason_counts": reason_counts,
        "by_reason": by_reason,
        "by_coin": by_coin,
        "equity_curve": equity_curve,
        "initial_equity": initial,
        "realized_equity": realized_equity,
        "final_equity": final_equity,
        "total_pnl_quote": final_equity - initial,
        "unique_symbols": len(symbol_counts),
        "unique_coins_traded": len(symbol_counts),
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
        "risk_events": dict(result.risk_events or {}),
        "chase_skips": int(result.chase_skips),
        "confluence_structure_skips": int(result.confluence_structure_skips),
        "confluence_structure_scans": int(result.confluence_structure_scans),
        "confluence_trigger_skips": int(result.confluence_trigger_skips),
        "confluence_trigger_scans": int(result.confluence_trigger_scans),
        "structure_skips": int(result.confluence_structure_skips),
        "structure_scans": int(result.confluence_structure_scans),
        "trigger_skips": int(result.confluence_trigger_skips),
        "trigger_scans": int(result.confluence_trigger_scans),
        "same_coin_skips": int(result.same_coin_skips),
        **trade_metrics,
    }


def execute_portfolio_backtest(
    client,
    days: float,
    base_config: dict,
    max_symbols: int = 30,
    overrides: Optional[dict] = None,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> dict:
    from backtesting.backtest import (
        MS_PER_DAY,
        apply_overrides,
        last_completed_candle_end_ms,
        validate_params,
    )

    if days <= 0 or days > 1825:
        raise BacktestError("Rentang hari harus antara 0.5 sampai 1825 (5 tahun).")
    max_symbols = max(3, min(int(max_symbols or 30), 200))

    config = apply_overrides(base_config, overrides)
    validate_params(config)

    if progress_cb:
        progress_cb(0.02, "Mengambil daftar seluruh pasangan USDT dari Binance...")

    tickers = client.get_ticker_24hr_all()
    try:
        tradable = client.get_tradable_symbols(config.get("QUOTE_ASSET", "USDT"))
    except Exception:
        tradable = None
    if tradable:
        config["_historical_tradable_symbols"] = set(tradable)
        config["_tradable_status_is_current_snapshot"] = True

    universe = select_universe(
        tickers, config, max_symbols=max_symbols, tradable_symbols=tradable
    )
    if not universe:
        raise BacktestError(
            "Tidak ada pasangan yang lolos filter volume 24 jam minimum. "
            "Coba turunkan MIN_QUOTE_VOLUME_USDT_24H."
        )

    interval = config.get("CONFIRM_INTERVAL", "5m")
    bar_minutes = INTERVAL_MINUTES[interval]
    window_24h = bars_per_day(interval)
    warmup_bars = max(window_24h, strategy.required_lookback_bars(config))
    butuh_gerbang_ms = strategy.gate_warmup_ms(config, interval)
    warmup_ms = max(warmup_bars * bar_minutes * MS_PER_MIN, butuh_gerbang_ms)

    end_ms = last_completed_candle_end_ms(interval)
    start_ms = end_ms - int(days * MS_PER_DAY) - warmup_ms

    def dl_cb(p: float, sym: str):
        if progress_cb:
            progress_cb(
                round(0.05 + p * 0.70, 3),
                f"Mengunduh {sym} ({int(p * len(universe))}/{len(universe)} koin)",
            )

    store = new_backtest_store(config)
    cache = open_kline_cache(config)
    try:
        ok_symbols, failed = fetch_universe_klines(
            client,
            universe,
            interval,
            start_ms,
            end_ms,
            store,
            progress_cb=dl_cb,
            cancel_cb=cancel_cb,
            cache=cache,
        )
        if not ok_symbols:
            raise BacktestError("Gagal mengunduh data untuk semua kandidat.")

        btc_klines = None
        if config.get("BTC_FILTER_ENABLED", False):
            btc_symbol = "BTC" + str(config.get("QUOTE_ASSET", "USDT"))
            if btc_symbol in ok_symbols:
                btc_klines = store.klines(btc_symbol)
            else:
                try:
                    btc_klines = _klines_untuk_simbol(
                        client, btc_symbol, interval, start_ms, end_ms, cache
                    )
                except Exception:
                    btc_klines = None

        def sim_cb(p: float):
            if progress_cb:
                progress_cb(
                    round(0.76 + p * 0.22, 3),
                    f"Simulasi pasar bar demi bar ({int(p*100)}%)",
                )

        if progress_cb:
            progress_cb(0.76, "Menyusun garis waktu dan menjalankan simulasi...")

        result = run_portfolio_backtest(
            store,
            config,
            interval,
            warmup_ms=warmup_ms,
            progress_cb=sim_cb,
            cancel_cb=cancel_cb,
            btc_klines=btc_klines,
        )
    finally:
        if cache is not None:
            cache.close()
        store.close()
    result.symbols_scanned = len(universe)
    result.symbols_failed = failed

    summary = summarize_portfolio(result)

    if progress_cb:
        progress_cb(1.0, "Selesai")

    trades_out = [
        {
            "n": idx,
            "symbol": t.symbol,
            "entry_time": t.entry_time,
            "entry_price": t.entry_price,
            "exit_time": t.exit_time,
            "exit_price": t.exit_price,
            "reason": t.reason,
            "hold_minutes": round(t.hold_minutes, 1),
            "pnl_pct": round(t.pnl_pct, 3),
            "gross_pnl_pct": round(t.gross_pnl_pct, 3),
            "fee_pct": round(t.fee_pct, 3),
            "sl_pct": round(t.sl_pct, 6),
            "tp_pct": round(t.tp_pct, 6),
            "exit_source": t.exit_source,
            "rank_at_entry": t.rank_at_entry,
            "candidates_at_entry": t.candidates_at_entry,
            "position_notional": round(t.position_notional, 2),
            "equity_before": round(t.equity_before, 2),
            "equity_after": round(t.equity_after, 2),
            "pnl_quote": round(t.pnl_quote, 2),
        }
        for idx, t in enumerate(result.trades, start=1)
    ]

    skipped_out = [
        {
            "time": s.time,
            "symbol": s.symbol,
            "reason": s.reason,
            "holding": s.holding,
        }
        for s in result.skipped
    ]

    from backtesting.backtest import OVERRIDE_CASTERS

    applied_params = {k: config.get(k) for k in OVERRIDE_CASTERS.keys()}
    applied_params["MAX_SYMBOLS"] = max_symbols

    spread_pct, slip_pct, delay_bars = entry_execution_params(config)
    assumptions = [
        f"Universe kandidat: {len(ok_symbols)} pasangan {config.get('QUOTE_ASSET', 'USDT')} ber-volume tertinggi (dari {len(universe)} yang diminta). "
        "Diurutkan dari snapshot 24 jam saat ini (survivorship bias tetap ada karena Binance tidak menyediakan daftar listing historis).",
        "Pada setiap bar, volume 24 jam dihitung bergulir dari 288 candle 5m sebelumnya, lalu diurutkan persis seperti scanner bot.",
        "Hanya 1 posisi aktif pada satu waktu. Sinyal baru saat posisi masih terbuka atau saat cooldown dicatat sebagai 'dilewati'.",
        (
            f"Eksekusi entry menunggu {delay_bars} bar setelah sinyal lalu membeli pada harga open + "
            f"setengah spread ({spread_pct / 2:.3f}%) + slippage ({slip_pct:.3f}%). "
            f"Exit menjual dengan pengurangan spread/slippage yang sama."
        ),
        (
            f"Ukuran posisi mengikuti pengaturan bot (modal awal simulasi "
            f"{summary['initial_equity']:,.2f} {config.get('QUOTE_ASSET', 'USDT')}), "
            f"cooldown selesai trade {config.get('COOLDOWN_MINUTES_AFTER_CLOSE', 0)} menit, "
            f"jeda antar trade {config.get('MIN_SECONDS_BETWEEN_TRADES', 0)} detik, "
            f"dan batas kejar harga {config.get('MAX_CHASE_PCT', 0)}%."
        ),
        "Evaluasi di dalam 1 candle memakai urutan konservatif: Stop Loss diperiksa lebih dulu daripada Take Profit.",
        "Filter orderbook live tidak dapat disimulasikan karena Binance tidak menyediakan riwayat buku order; dampak likuiditas diwakili oleh parameter spread dan slippage di atas.",
    ]
    if failed:
        assumptions.append(
            f"{len(failed)} simbol dilewati karena data tidak tersedia/gagal diunduh."
        )
    if result.warnings:
        assumptions.extend(result.warnings)

    return {
        "mode": "portfolio",
        "symbol": f"PORTOFOLIO ({len(ok_symbols)} koin)",
        "interval": interval,
        "days": days,
        "symbols_scanned": len(universe),
        "symbols_with_data": len(ok_symbols),
        "symbols_failed": failed,
        "universe": ok_symbols,
        "bars_total": result.bars_total,
        "bars_usable": result.bars_total,
        "start_time": result.start_time,
        "end_time": result.end_time,
        "summary": summary,
        "trades": trades_out,
        "skipped": skipped_out,
        "params": applied_params,
        "assumptions": assumptions,
        "warnings": result.warnings,
    }


def selftest() -> bool:
    from copy import deepcopy

    from config.config import PUMP_CONFIG
    from backtesting.synthetic_data import seri_banyak_setup, seri_dengan_setup

    ok_all = True

    def check(nama: str, kondisi: bool, info: str = "") -> None:
        nonlocal ok_all
        status = "LULUS" if kondisi else "GAGAL"
        ekor = f" ({info})" if info else ""
        print(f"  [{status}] {nama}{ekor}")
        if not kondisi:
            ok_all = False

    def _base_cfg() -> dict:
        cfg = deepcopy(PUMP_CONFIG)
        cfg.update(
            {
                "MODE": "PAPER",
                "CONFLUENCE_FILTER_ENABLED": False,
                "BTC_FILTER_ENABLED": False,
                "MIN_QUOTE_VOLUME_USDT_24H": 0.0,
                "CONFLUENCE_SL_BUFFER_PCT": 0.20,
                "CONFLUENCE_MIN_RR_RATIO": 1.2,
                "TAKER_FEE_PCT": 0.1,
                "USE_BNB_FEE_DISCOUNT": False,
                "USE_RISK_PERCENT": False,
                "POSITION_SIZE_USDT": 100.0,
                "MAX_POSITION_USDT": 0.0,
                "BACKTEST_INITIAL_EQUITY_USDT": 1000.0,
                "MAX_HOLD_HOURS": 0.0,
                "COOLDOWN_MINUTES_AFTER_CLOSE": 0,
                "MIN_SECONDS_BETWEEN_TRADES": 0,
                "MAX_CHASE_PCT": 0.0,
                "TOP_N_CANDIDATES_TO_CONFIRM": 10,
            }
        )
        return cfg

    print("Menjalankan selftest portfolio_backtest...")

    cfg = _base_cfg()
    up_a = seri_banyak_setup(harga=100.0, siklus=10, volume=9_000_000.0)
    up_b = seri_banyak_setup(harga=200.0, siklus=10, volume=5_000_000.0)
    store = new_backtest_store(cfg)
    try:
        store.write_symbol("AAAUSDT", up_a)
        store.write_symbol("BBBUSDT", up_b)

        tl_2 = build_timeline(store, "5m")
        check("build_timeline 2-tuple tanpa config", len(tl_2) == 2 and len(tl_2[0]) == len(up_a))

        tl_3 = build_timeline(store, "5m", config=cfg)
        check("build_timeline 3-tuple dengan config", len(tl_3) == 3 and tl_3[2] == {})

        cfg_conf = _base_cfg()
        cfg_conf["CONFLUENCE_FILTER_ENABLED"] = True
        tl_conf = build_timeline(store, "5m", config=cfg_conf)
        check(
            "build_timeline mengisi struktur saat CONFLUENCE_FILTER_ENABLED=True",
            len(tl_conf) == 3 and set(tl_conf[2].keys()) == {"AAAUSDT", "BBBUSDT"},
        )

        u2 = unpack_prebuilt((tl_2[0], tl_2[1]))
        u3 = unpack_prebuilt(tl_conf)
        check("unpack_prebuilt menerima 2-tuple dan 3-tuple", u2[2] is None and u3[2] is not None)

        warmup_ms = 300 * 5 * MS_PER_MIN
        res_plain = run_portfolio_backtest(
            store, cfg, "5m", warmup_ms=warmup_ms, prebuilt=tl_2
        )
        sig_cache = precompute_entry_signals(
            store, cfg, "5m", prebuilt=tl_2
        )
        res_cached = run_portfolio_backtest(
            store,
            cfg,
            "5m",
            warmup_ms=warmup_ms,
            prebuilt=tl_2,
            entry_signal_cache=sig_cache,
        )
        jejak_plain = [
            (t.symbol, t.entry_time, t.exit_time, round(t.pnl_pct, 8), t.reason)
            for t in res_plain.trades
        ]
        jejak_cached = [
            (t.symbol, t.entry_time, t.exit_time, round(t.pnl_pct, 8), t.reason)
            for t in res_cached.trades
        ]
        check(
            "run_portfolio_backtest menghasilkan trade",
            len(res_plain.trades) > 0,
            f"{len(res_plain.trades)} trade",
        )
        check(
            "EntrySignalCache identik dengan jalur tanpa cache",
            jejak_plain == jejak_cached,
            f"plain={len(jejak_plain)}, cached={len(jejak_cached)}",
        )
    finally:
        store.close()

    from backtesting.backtest import run_backtest

    par_cfg = _base_cfg()
    par_kl = seri_dengan_setup(harga=100.0, ekor="naik", panjang_ekor=20)
    par_store = new_backtest_store(par_cfg)
    try:
        par_store.write_symbol("SOLOUSDT", par_kl)
        warmup_bars = 300
        bt_res = run_backtest(par_kl, par_cfg, warmup_bars=warmup_bars)
        pf_res = run_portfolio_backtest(
            par_store,
            par_cfg,
            "5m",
            warmup_ms=warmup_bars * 5 * MS_PER_MIN,
        )
        bt_jejak = [
            (t.entry_time, t.exit_time, round(t.pnl_pct, 8), t.reason)
            for t in bt_res.trades
        ]
        pf_jejak = [
            (t.entry_time, t.exit_time, round(t.pnl_pct, 8), t.reason)
            for t in pf_res.trades
        ]
        check(
            "Paritas 1 simbol terhadap run_backtest",
            len(bt_jejak) > 0 and bt_jejak == pf_jejak,
            f"backtest={len(bt_jejak)}, portfolio={len(pf_jejak)}",
        )
    finally:
        par_store.close()

    print("HASIL: " + ("SEMUA LULUS" if ok_all else "ADA YANG GAGAL"))
    return ok_all


if __name__ == "__main__":
    import sys

    sys.exit(0 if selftest() else 1)
