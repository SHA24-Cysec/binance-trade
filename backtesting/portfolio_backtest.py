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


def select_universe(tickers: list, config: dict, max_symbols: Optional[int] = None,
                    tradable_symbols: Optional[set] = None) -> list[str]:
    cfg = dict(config)
    cfg["MIN_QUOTE_VOLUME_USDT_24H"] = float(config.get("MIN_QUOTE_VOLUME_USDT_24H", 0))

    ranked = scanner.filter_and_rank_candidates(tickers, cfg, tradable_symbols,
                                                apply_pump_gate=False)
    ranked.sort(key=lambda c: c.quote_volume, reverse=True)
    symbols = [c.symbol for c in ranked]
    if max_symbols is not None and max_symbols > 0:
        symbols = symbols[:max_symbols]
    return symbols


def new_backtest_store(config: Optional[dict] = None) -> KlineStore:
    cfg = config or {}
    cache_size = int(cfg.get("BACKTEST_SYMBOL_CACHE_SIZE",
                             storage.DEFAULT_SYMBOL_CACHE_SIZE)
                     or storage.DEFAULT_SYMBOL_CACHE_SIZE)
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
            fresh_hours=float(cfg.get("BACKTEST_CACHE_FRESH_HOURS",
                                      kcache.DEFAULT_FRESH_HOURS) or 0),
            ttl_days=float(cfg.get("BACKTEST_CACHE_TTL_DAYS",
                                   kcache.DEFAULT_TTL_DAYS) or 0),
        )
        cache.prune()
        return cache
    except (sqlite3.Error, OSError, kcache.CacheError) as exc:
        logger.warning("Cache candle backtest tidak bisa dipakai (%s). Backtest "
                       "tetap berjalan dengan mengunduh penuh.", exc)
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

    # Fetch in small ordered batches: network I/O can overlap, while storage
    # insertion remains in the original universe order. The latter preserves
    # deterministic tie-breaking in portfolio simulations.
    if workers == 1 or len(symbols) <= 1:
        batches = [symbols]
        executor = None
    else:
        batches = [symbols[i:i + workers] for i in range(0, len(symbols), workers)]
        executor = ThreadPoolExecutor(max_workers=workers,
                                      thread_name_prefix="backtest-klines")

    done = 0
    try:
        for batch in batches:
            if cancel_cb is not None and cancel_cb():
                raise BacktestError("Backtest dibatalkan.")
            futures = ({sym: executor.submit(_download, sym) for sym in batch}
                       if executor is not None else {})
            for sym in batch:
                if cancel_cb is not None and cancel_cb():
                    for future in futures.values():
                        future.cancel()
                    raise BacktestError("Backtest dibatalkan.")
                try:
                    kl = futures[sym].result() if executor is not None else _download(sym)
                    if kl:
                        store.write_symbol(sym, kl)
                        ok_symbols.append(sym)
                    else:
                        failed.append({
                            "symbol": sym,
                            "error": "tidak ada data candle pada rentang ini",
                        })
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


def _klines_untuk_simbol(client, symbol: str, interval: str, start_ms: int,
                         end_ms: int,
                         cache: Optional[KlineCache]) -> list[Kline]:
    if cache is None:
        return fetch_full_klines(client, symbol, interval, start_ms, end_ms,
                                 sleep_between_calls=0.0)

    for awal, akhir in cache.missing_ranges(symbol, interval, start_ms, end_ms):
        bagian = fetch_full_klines(client, symbol, interval, awal, akhir,
                                   sleep_between_calls=0.0)
        cache.put(symbol, interval, bagian, awal, akhir)
    return cache.read(symbol, interval, start_ms, end_ms)


def build_timeline(store: KlineStore, interval: str,
                   symbols: Optional[list[str]] = None,
                   *, include_ohlcv: bool = False,
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
EntrySignalCache = dict[int, tuple[int, list[EntrySignal]]]


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
) -> EntrySignalCache:
    """Precompute entry candidates when only exit multipliers will vary.

    Entry qualification depends on market data, scanner filters, and ATR_PERIOD,
    not on ATR exit multipliers. Reusing these signals makes multi-parameter
    exit searches much faster without changing the portfolio simulator's fills.
    """
    all_symbols = store.symbols()
    if not all_symbols:
        raise BacktestError("Tidak ada data candle untuk memproses sinyal.")

    tradable_meta = config.get("_historical_tradable_symbols")
    symbols = [sym for sym in all_symbols
               if scanner.is_structurally_allowed_symbol(sym, config, tradable_meta)]
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
    first_allowed_time = timeline[0] + int(warmup_ms)

    papan_input = []
    for sym in symbols:
        ot, _ct, pct_arr, vol_arr, ready_arr = series_of[sym].board_arrays()
        papan_input.append((sym, ot, pct_arr, vol_arr, ready_arr, len(ot)))

    signals: EntrySignalCache = {}
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
            gate_ok, _gate_reason = scanner.evaluate_pump_gate(
                pct24, vol24, gate_cfg, btc_drop_pct=btc_drop)
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
                        max(0, index - lookback + 1), index + 1)
                else:
                    klines = store.klines(sym)
                    window_klines = klines[max(0, index - lookback + 1): index + 1]
                setup = scanner.detect_entry_setup(window_klines, config)
            except Exception:
                continue
            if setup.ok:
                lolos.append((rank, pct24, sym, index, vol24, setup))

        if not lolos:
            continue
        lolos.sort(key=lambda row: scanner.setup_quality_key(
            row[5], scanner.Candidate(
                row[2], "", row[1], row[4], series_of[row[2]].kline_at(row[3]).close
                if series_of[row[2]].has_ohlcv
                else store.klines(row[2])[row[3]].close)))
        signals[int(t_now)] = (len(board), lolos)

    if progress_cb:
        progress_cb(1.0)
    return signals


def _resolve_symbol_cache_size(config: dict, top_n: int) -> int:
    diminta = int(config.get("BACKTEST_SYMBOL_CACHE_SIZE",
                             storage.DEFAULT_SYMBOL_CACHE_SIZE)
                  or storage.DEFAULT_SYMBOL_CACHE_SIZE)
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
    symbols = [sym for sym in semua_simbol
               if scanner.is_structurally_allowed_symbol(sym, config, tradable_meta)]
    if not symbols:
        raise BacktestError("Tidak ada data yang lolos policy semesta bersama.")
    lolos_policy = len(symbols)

    if prebuilt is not None:
        timeline, series_of = prebuilt
    else:
        timeline, series_of = build_timeline(store, interval, symbols)
    if not timeline:
        raise BacktestError("Garis waktu kosong, tidak ada candle yang bisa diproses.")
    if start_ms is not None or end_ms is not None:
        timeline = [t for t in timeline
                    if (start_ms is None or t >= start_ms)
                    and (end_ms is None or t <= end_ms)]
        if not timeline:
            raise BacktestError(
                "Rentang waktu (start_ms/end_ms) tidak memuat satu bar pun dari data.")
    symbols = [sym for sym in symbols if sym in series_of]

    lookback = strategy.confirm_window_bars(config)
    min_vol = float(config["MIN_QUOTE_VOLUME_USDT_24H"])
    top_n = int(config.get("TOP_N_CANDIDATES_TO_CONFIRM", 10))
    if entry_signal_cache is None:
        store.set_cache_size(_resolve_symbol_cache_size(config, top_n))
    else:
        cache_size = int(config.get("BACKTEST_SYMBOL_CACHE_SIZE",
                                   storage.DEFAULT_SYMBOL_CACHE_SIZE)
                         or storage.DEFAULT_SYMBOL_CACHE_SIZE)
        store.set_cache_size(max(1, cache_size))
    _pre_warnings: list = []
    if lolos_policy != original_count:
        _pre_warnings.append("Sebagian data dibuang oleh policy semesta bersama (stablecoin, leveraged token, blacklist, quote, atau status metadata).")
    if tradable_meta is None or config.get("_tradable_status_is_current_snapshot"):
        _pre_warnings.append("Status TRADING historis tidak tersedia dari candle Binance. Policy status hanya dapat diverifikasi dari metadata saat ini bila caller menyediakannya.")
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

    execution_spread_pct, execution_slippage_pct, entry_delay_bars = entry_execution_params(config)

    trades: list = []
    skipped: list = []
    warnings: list = list(_pre_warnings)
    initial_equity = initial_backtest_equity(config)
    equity = initial_equity

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
    max_chase_pct = float(config.get("MAX_CHASE_PCT", 0) or 0)

    controls = parity.AccountRiskControls(config, initial_equity)
    bar_ms = INTERVAL_MINUTES.get(interval, 5) * MS_PER_MIN
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

    signal_times = (sorted(int(value) for value in entry_signal_cache)
                    if entry_signal_cache is not None else [])
    signal_cursor = 0
    bi = 0
    while bi < total_bars:
        if entry_signal_cache is not None and holding is None:
            while (signal_cursor < len(signal_times)
                   and signal_times[signal_cursor] < timeline[bi]):
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
            for (sym, ot, ct, pct_arr, vol_arr, ready_arr, n_bar) in papan_input:
                pos = bisect_left(ot, t_now)
                if pos >= n_bar or ot[pos] != t_now:
                    continue
                if not ready_arr[pos]:
                    continue
                vol24 = vol_arr[pos]
                if vol24 < min_vol:
                    continue
                pct24 = pct_arr[pos]
                gate_ok, _gate_reason = scanner.evaluate_pump_gate(
                    pct24, vol24, gate_cfg, btc_drop_pct=btc_drop)
                if not gate_ok:
                    continue
                board.append((vol24, sym, pos, pct24))
            board.sort(key=lambda x: -x[0])

        if holding is not None:
            hi = series_of[holding].index_at(t_now)
            if hi is None:
                continue

            held_series = series_of[holding]
            candle = (held_series.kline_at(hi)
                      if entry_signal_cache is not None and held_series.has_ohlcv
                      else store.klines(holding)[hi])
            if candle.open_time < entry_time:
                continue
            hold_minutes = (candle.close_time - entry_time) / 60000.0

            exit_reason = None
            exit_price = None
            verdict = parity.evaluate_candle_exit(position, candle, config)
            if verdict is not None:
                exit_reason, exit_price = verdict

            is_last = (current_bi == total_bars - 1)
            if exit_reason is None:
                mtm_equity = equity + position_notional * (candle.close / entry_price - 1.0)
                entries_paused = controls.update(candle.close_time, mtm_equity)
                if controls.force_close_due(entries_paused, True):
                    exit_reason = parity.RISK_LIMIT_REASON
                    exit_price = candle.close
                elif is_last:
                    exit_reason = "END_OF_DATA"
                    exit_price = candle.close
                    warnings.append(
                        "Posisi terakhir masih terbuka saat data habis (ditutup paksa di harga "
                        "penutupan terakhir demi kelengkapan statistik, bukan exit sungguhan)."
                    )

            if exit_reason:
                exit_price = strategy.backtest_sell_execution_price(
                    exit_price, execution_spread_pct, execution_slippage_pct)
                gross = (exit_price / entry_price - 1.0) * 100.0
                pnl_pct = gross - fee_round_trip
                pnl_quote = position_notional * pnl_pct / 100.0
                equity_after = max(0.0, equity + pnl_quote)
                trades.append(PortfolioTrade(
                    symbol=holding,
                    entry_time=entry_time, entry_price=entry_price,
                    exit_time=candle.close_time, exit_price=exit_price,
                    reason=exit_reason, hold_minutes=hold_minutes,
                    pnl_pct=pnl_pct, gross_pnl_pct=gross, fee_pct=fee_round_trip,
                    sl_pct=position.levels["sl"], tp_pct=position.levels["tp"],
                    exit_source=position.levels["src"],
                    rank_at_entry=rank_at_entry, pct24h_at_entry=pct24h_at_entry,
                    candidates_at_entry=cands_at_entry,
                    position_notional=position_notional, equity_before=equity_before_entry,
                    equity_after=equity_after, pnl_quote=pnl_quote,
                ))
                equity = equity_after
                position_notional = 0.0
                holding = None
                position = None
                next_entry_allowed_at = parity.next_entry_allowed(candle.close_time, config)
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
                window_kl = kl[max(0, i - lookback + 1): i + 1]
                try:
                    setup = scanner.detect_entry_setup(window_kl, config)
                except Exception:
                    continue
                if setup.ok:
                    lolos.append((rank, pct, sym, i, vol24, setup))

            if not lolos:
                continue

            lolos.sort(key=lambda row: scanner.setup_quality_key(
                row[5], scanner.Candidate(
                    row[2], "", row[1], row[4], store.klines(row[2])[row[3]].close)))
            eligible_count = len(eligible)
        else:
            cached_entry = entry_signal_cache.get(int(t_now))
            if cached_entry is None:
                continue
            eligible_count, lolos = cached_entry
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
                skipped.append(SkippedSignal(
                    time=t_now, symbol=sym2,
                    reason="KALAH_KUALITAS_SETUP", holding=sym,
                ))
                if len(skipped) >= max_skipped_records:
                    break

        entry_series = series_of[sym]
        entry_idx = i + entry_delay_bars
        if entry_signal_cache is not None and entry_series.has_ohlcv:
            series_length = len(entry_series)
            if entry_idx >= series_length:
                warnings.append(f"Sinyal {sym} terakhir tidak memiliki bar eksekusi setelah latency entry; trade dilewati.")
                continue
            signal_candle = entry_series.kline_at(i)
            entry_candle = entry_series.kline_at(entry_idx)
        else:
            kl = store.klines(sym)
            series_length = len(kl)
            if entry_idx >= series_length:
                warnings.append(f"Sinyal {sym} terakhir tidak memiliki bar eksekusi setelah latency entry; trade dilewati.")
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
                    "dengan aman (paritas open_position).")
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
                "(paritas open_position). Cek ATR_MULT_SL/ATR.")
            continue
        exec_entry_price = strategy.backtest_buy_execution_price(
            raw_entry_price, execution_spread_pct, execution_slippage_pct)
        if parity.chase_exceeded(exec_entry_price, setup_terpilih.signal_close, max_chase_pct):
            chase_skips += 1
            if len(skipped) < max_skipped_records:
                skipped.append(SkippedSignal(
                    time=t_now, symbol=sym, reason="FILTER_CHASE", holding=None))
            continue
        holding = sym
        position = parity.PositionState(exec_entry_price, lv)
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
        warnings=list(dict.fromkeys(warnings)),
        initial_equity=initial_equity,
        final_equity=equity,
        risk_events=dict(controls.events),
        chase_skips=chase_skips,
    )

def summarize_portfolio(result: PortfolioResult) -> dict:
    trades = result.trades
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

    final_equity = equity if trades else (result.final_equity or initial)
    total_return = ((final_equity / initial) - 1.0) * 100.0 if initial > 0 else 0.0
    gross_return = ((gross_equity / initial) - 1.0) * 100.0 if initial > 0 else 0.0
    peak = initial
    max_dd = 0.0
    for value in [initial] + [initial * (1.0 + v / 100.0) for v in equity_curve[1:]]:
        peak = max(peak, value)
        max_dd = max(max_dd, ((peak - value) / peak * 100.0) if peak > 0 else 0.0)

    gross_win = sum(t.pnl_pct for t in wins)
    gross_loss = abs(sum(t.pnl_pct for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0)

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
    total_hold = sum(t.hold_minutes for t in trades)
    exposure = (total_hold / (span_days * 24 * 60) * 100.0) if span_days > 0 else 0.0

    return {
        "total_trades": total, "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / total * 100.0) if total else 0.0,
        "total_return_pct": total_return, "gross_return_pct": gross_return,
        "fee_drag_pct": gross_return - total_return,
        "total_fee_pct": sum(t.fee_pct for t in trades),
        "max_drawdown_pct": max_dd,
        "avg_win_pct": (sum(t.pnl_pct for t in wins) / len(wins)) if wins else 0.0,
        "avg_loss_pct": (sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0.0,
        "profit_factor": profit_factor,
        "avg_hold_minutes": (total_hold / total) if total else 0.0,
        "reason_counts": reason_counts, "equity_curve": equity_curve,
        "initial_equity": initial, "final_equity": final_equity,
        "total_pnl_quote": final_equity - initial,
        "unique_symbols": len(symbol_counts), "symbols_in_universe": result.symbols_with_data,
        "top_symbols": [{"symbol": s, "pnl_quote": p,
                         "pnl_pct": (p / initial * 100.0) if initial > 0 else 0.0,
                         "trades": symbol_counts[s]}
                        for s, p in top_symbols[:10]],
        "worst_symbols": [{"symbol": s, "pnl_quote": p,
                            "pnl_pct": (p / initial * 100.0) if initial > 0 else 0.0,
                            "trades": symbol_counts[s]}
                          for s, p in top_symbols[-5:][::-1] if p < 0],
        "skipped_signals": len(result.skipped),
        "avg_rank_at_entry": (sum(t.rank_at_entry for t in trades) / total) if total else 0.0,
        "exposure_pct": exposure, "span_days": span_days,
        "trades_per_day": (total / span_days) if span_days > 0 else 0.0,
        "risk_events": dict(result.risk_events),
        "chase_skips": int(result.chase_skips),
        **parity.per_trade_metrics(trades),
    }


def _mk(t, o, h, l, c, qv=5_000_000.0):
    ms = t * 5 * MS_PER_MIN
    return Kline(open_time=ms, open=o, high=h, low=l, close=c,
                 close_time=ms + 5 * MS_PER_MIN - 1, volume=1000.0, quote_volume=qv)


def selftest() -> bool:
    ok_all = True

    def check(name, cond, extra=""):
        nonlocal ok_all
        if not cond:
            ok_all = False
        print(("  LULUS " if cond else "  GAGAL ") + name + (("  -> " + str(extra)) if extra else ""))

    print("Selftest portfolio_backtest")

    a = [_mk(i, 100, 101, 99, 100) for i in range(5)]
    b = [_mk(i, 50, 51, 49, 50) for i in range(3, 9)]
    with KlineStore.from_klines({"AUSDT": a, "BUSDT": b}) as _store_tl:
        tl, seri = build_timeline(_store_tl, "5m")
        check("timeline gabungan unik & terurut", tl == sorted(set(tl)) and len(tl) == 9, len(tl))
        check("SymbolSeries memetakan waktu ke posisi",
              seri["BUSDT"].index_at(b[0].open_time) == 0)
        check("SymbolSeries menolak waktu yang tidak ada",
              seri["BUSDT"].index_at(b[0].open_time - 1) is None)

    cfg = {
        "CONFIRM_LOOKBACK_BARS": 48,
        "MIN_QUOTE_VOLUME_USDT_24H": 0, "TOP_N_CANDIDATES_TO_CONFIRM": 10,
        "COOLDOWN_MINUTES_AFTER_CLOSE": 0,
        "USE_STOP_LOSS": True, "USE_TP": True, "USE_BREAKEVEN": False,
        "USE_TRAILING": False, "SL_PCT": 2.0, "TP_PCT": 3.0,
        "BE_TRIGGER_PCT": 1.0, "BE_LOCK_PCT": 0.1,
        "TRAILING_START_PCT": 1.5, "TRAILING_STEP_PCT": 0.6,
        "TAKER_FEE_PCT": 0.1,
        "QUOTE_ASSET": "USDT",
        "PUMP_MIN_24H_CHANGE_PCT": -1000.0,
        "ROLLING_VOLUME_FILTER_ENABLED": False,
    }

    from backtesting.synthetic_data import seri_banyak_setup

    def _jalankan(data_dict: dict, cfg_uji: dict) -> PortfolioResult:
        with KlineStore.from_klines(data_dict) as _st:
            return run_portfolio_backtest(_st, cfg_uji, "5m")

    up_a = seri_banyak_setup(harga=100.0, siklus=10, volume=9_000_000.0)
    up_b = seri_banyak_setup(harga=200.0, siklus=10, volume=5_000_000.0)

    res = _jalankan({"AUSDT": up_a, "BUSDT": up_b}, cfg)
    overlaps = 0
    for i in range(len(res.trades)):
        for j in range(i + 1, len(res.trades)):
            t1, t2 = res.trades[i], res.trades[j]
            if t1.entry_time < t2.exit_time and t2.entry_time < t1.exit_time:
                overlaps += 1
    check("tidak pernah dua posisi bersamaan", overlaps == 0, f"{overlaps} tumpang tindih")
    check("menghasilkan trade", len(res.trades) > 0, len(res.trades))

    if res.trades:
        t = res.trades[0]
        check("fee pulang-pergi dipotong",
              abs((t.gross_pnl_pct - t.pnl_pct) - 0.2) < 1e-9,
              f"selisih {t.gross_pnl_pct - t.pnl_pct:.4f}")

    cfg_cd = dict(cfg)
    cfg_cd["COOLDOWN_MINUTES_AFTER_CLOSE"] = 60
    res_cd = _jalankan({"AUSDT": up_a, "BUSDT": up_b}, cfg_cd)
    viol = 0
    for i in range(1, len(res_cd.trades)):
        gap_min = (res_cd.trades[i].entry_time - res_cd.trades[i - 1].exit_time) / 60000.0
        if gap_min < 59.9:
            viol += 1
    check("cooldown dihormati", viol == 0, f"{viol} pelanggaran")
    check("cooldown mengurangi jumlah trade",
          len(res_cd.trades) <= len(res.trades),
          f"{len(res_cd.trades)} vs {len(res.trades)}")

    entry_pertama = res.trades[0].entry_time if res.trades else 0
    seq_sl = []
    for k in up_a:
        if k.open_time == entry_pertama:
            seq_sl.append(Kline(open_time=k.open_time, open=k.open,
                                high=k.open * 1.12, low=k.open * 0.88, close=k.open,
                                close_time=k.close_time, volume=k.volume,
                                quote_volume=k.quote_volume))
        else:
            seq_sl.append(k)

    res_sl = _jalankan({"AUSDT": seq_sl}, cfg)
    check("skenario SL-vs-TP benar-benar menghasilkan trade (tes tidak vakum)",
          len(res_sl.trades) > 0, len(res_sl.trades))
    spanning = [t for t in res_sl.trades if t.entry_time == entry_pertama]
    if spanning:
        check("SL diprioritaskan saat SL & TP kena di satu candle",
              all(t.reason == "STOP_LOSS" for t in spanning),
              [t.reason for t in spanning][:4])
    else:
        check("SL diprioritaskan saat SL & TP kena di satu candle",
              False, "tidak ada trade yang melewati candle ekstrem")

    from backtesting import backtest as _bt
    from backtesting.synthetic_data import seri_dengan_setup
    cfg_par = dict(cfg)
    par_kl = seri_dengan_setup(harga=100.0, ekor="naik", panjang_ekor=20)
    res_p1 = _bt.run_backtest(par_kl, dict(cfg_par, _symbol="AUSDT"), warmup_bars=0)
    res_p2 = _jalankan({"AUSDT": par_kl}, cfg_par)
    check("paritas entry satu simbol vs portofolio",
          [t.entry_time for t in res_p1.trades] == [t.entry_time for t in res_p2.trades],
          f"{[t.entry_time for t in res_p1.trades]} vs {[t.entry_time for t in res_p2.trades]}")
    check("paritas alasan exit satu simbol vs portofolio",
          [t.reason for t in res_p1.trades] == [t.reason for t in res_p2.trades],
          f"{[t.reason for t in res_p1.trades]} vs {[t.reason for t in res_p2.trades]}")

    res_ec_p = _bt.run_backtest(seq_sl, dict(cfg, _symbol="AUSDT"), warmup_bars=0)
    ec_single = [(t.entry_time, t.reason, round(t.exit_price, 9)) for t in res_ec_p.trades[:3]]
    ec_port = [(t.entry_time, t.reason, round(t.exit_price, 9)) for t in res_sl.trades[:3]]
    check("paritas candle entry ekstrem satu simbol vs portofolio",
          bool(ec_single) and ec_single == ec_port, f"{ec_single} vs {ec_port}")

    btc_cfg = dict(cfg, BTC_FILTER_ENABLED=True, BTC_MAX_DROP_PCT=1.0, BTC_LOOKBACK_BARS=3)

    def _btc(closes):
        return [_mk(i, c, c, c, c) for i, c in enumerate(closes)]

    def _jalankan_btc(cfg_uji, btc_kl):
        with KlineStore.from_klines({"AUSDT": up_a}) as _st:
            return run_portfolio_backtest(_st, cfg_uji, "5m", btc_klines=btc_kl)

    turun = [100.0 * 0.99 ** i for i in range(len(up_a))]
    r_btc_turun = _jalankan_btc(btc_cfg, _btc(turun))
    r_btc_datar = _jalankan_btc(btc_cfg, _btc([100.0] * len(up_a)))
    r_btc_none = _jalankan_btc(btc_cfg, None)
    check("filter BTC turun memblokir semua entry", len(r_btc_turun.trades) == 0,
          len(r_btc_turun.trades))
    check("filter BTC datar tidak mengubah hasil",
          len(r_btc_datar.trades) == len(res.trades),
          f"{len(r_btc_datar.trades)} trade")
    check("tanpa data BTC ada peringatan eksplisit",
          any("BTC" in w for w in r_btc_none.warnings) and len(r_btc_none.trades) > 0)
    r_chase = _jalankan({"AUSDT": up_a}, dict(cfg, MAX_CHASE_PCT=0.0001))
    check("MAX_CHASE_PCT melewati sinyal yang mengejar harga",
          r_chase.chase_skips > 0 and len(r_chase.trades) < len(res.trades)
          and any(sk.reason == "FILTER_CHASE" for sk in r_chase.skipped),
          f"{r_chase.chase_skips} dilewati, {len(r_chase.trades)} trade")
    cfg_risk = dict(cfg, USE_DAILY_STOP=True, MAX_DAILY_LOSS_PERCENT=0.01,
                    DAILY_PROFIT_TARGET_PERCENT=1000.0, CLOSE_ALL_AT_LIMIT=True,
                    BACKTEST_INITIAL_EQUITY_USDT=1000.0, MAX_POSITION_USDT=100.0)
    r_risk = _jalankan({"AUSDT": up_a}, cfg_risk)
    check("stop harian menjeda entry dan tercatat",
          r_risk.risk_events.get("daily_loss_stop", 0) > 0
          and len(r_risk.trades) < len(res.trades), r_risk.risk_events)
    check("ringkasan memuat metrik per trade dan peristiwa risiko",
          {"avg_trade_pct", "max_consecutive_losses", "risk_events", "chase_skips"}
          <= set(summarize_portfolio(r_risk)))

    cfg_vol = dict(cfg)
    cfg_vol["MIN_QUOTE_VOLUME_USDT_24H"] = 1e15
    res_vol = _jalankan({"AUSDT": up_a}, cfg_vol)
    check("filter volume menyaring semua", len(res_vol.trades) == 0, len(res_vol.trades))
    check("kontrol positif: data sama tanpa ambang volume tetap menghasilkan trade",
          len(_jalankan({"AUSDT": up_a}, cfg).trades) > 0)

    s = summarize_portfolio(res)
    check("total = menang + kalah", s["total_trades"] == s["wins"] + s["losses"])
    check("kurva equity panjangnya benar", len(s["equity_curve"]) == s["total_trades"] + 1)
    check("drawdown tidak negatif", s["max_drawdown_pct"] >= 0, s["max_drawdown_pct"])
    check("return kotor >= bersih",
          s["gross_return_pct"] >= s["total_return_pct"] - 1e-9,
          f'{s["gross_return_pct"]:.3f} vs {s["total_return_pct"]:.3f}')
    check("eksposur masuk akal (0-100%)", 0 <= s["exposure_pct"] <= 100, s["exposure_pct"])

    tickers = [
        {"symbol": "BTCUSDT", "priceChangePercent": "1.0", "quoteVolume": "9e9", "lastPrice": "60000"},
        {"symbol": "USDCUSDT", "priceChangePercent": "0.0", "quoteVolume": "9e9", "lastPrice": "1"},
        {"symbol": "BTCUPUSDT", "priceChangePercent": "5.0", "quoteVolume": "9e9", "lastPrice": "10"},
        {"symbol": "ETHBTC", "priceChangePercent": "1.0", "quoteVolume": "9e9", "lastPrice": "0.05"},
        {"symbol": "SOLUSDT", "priceChangePercent": "-3.0", "quoteVolume": "5e8", "lastPrice": "200"},
    ]
    uni = select_universe(tickers, {"QUOTE_ASSET": "USDT",
                                    "MIN_QUOTE_VOLUME_USDT_24H": 0,
                                    "EXTRA_EXCLUDE_SYMBOLS": []})
    check("semesta buang stablecoin/leveraged/non-USDT",
          set(uni) == {"BTCUSDT", "SOLUSDT"}, uni)
    check("select_universe tidak memakai ticker hari ini sebagai gerbang pump "
          "(SOL yang turun hari ini tetap ikut diunduh)",
          "SOLUSDT" in uni)

    print("\nHASIL: " + ("SEMUA LULUS" if ok_all else "ADA YANG GAGAL"))
    return ok_all


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
