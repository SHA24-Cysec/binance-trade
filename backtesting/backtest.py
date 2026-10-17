from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from strategy.indicators import Kline
from strategy import indicators as strategy
from market import market_scanner as scanner
from backtesting import parity

MS_PER_MIN = 60_000
MS_PER_DAY = 24 * 60 * MS_PER_MIN

INTERVAL_MINUTES = strategy.INTERVAL_MINUTES


@dataclass
class BacktestTrade:
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    reason: str
    hold_minutes: float
    pnl_pct: float
    sl_pct: float = 0.0
    tp_pct: float = 0.0
    exit_source: str = "FIXED"
    gross_pnl_pct: float = 0.0
    fee_pct: float = 0.0
    position_notional: float = 0.0
    equity_before: float = 0.0
    equity_after: float = 0.0
    pnl_quote: float = 0.0


class BacktestError(Exception):
    pass


@dataclass
class BacktestResult:
    symbol: str
    interval: str
    bars_total: int
    bars_usable: int
    start_time: int
    end_time: int
    trades: list = field(default_factory=list)
    params: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    initial_equity: float = 0.0
    final_equity: float = 0.0
    risk_events: dict = field(default_factory=dict)
    chase_skips: int = 0
    trend_skips: int = 0
    htf_demand_skips: int = 0
    m15_demand_skips: int = 0
    m15_demand_scans: int = 0
    m15_trend_skips: int = 0
    m15_trend_scans: int = 0
    same_coin_skips: int = 0
    mark_to_market_open: bool = False
    mark_to_market_pnl_pct: float = 0.0
    mark_to_market_pnl_quote: float = 0.0


def bars_per_day(interval: str) -> int:
    minutes = INTERVAL_MINUTES.get(interval)
    if not minutes:
        raise BacktestError(
            f"Interval '{interval}' tidak didukung untuk perhitungan 24 jam."
        )
    return (24 * 60) // minutes


def last_completed_candle_end_ms(
    interval: str, now_ms: Optional[int] = None
) -> int:
    minutes = INTERVAL_MINUTES.get(str(interval))
    if minutes is None:
        raise BacktestError(f"Interval candle '{interval}' tidak didukung.")
    interval_ms = int(minutes) * MS_PER_MIN
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return (current_ms // interval_ms) * interval_ms - 1


def initial_backtest_equity(config: dict) -> float:
    paper = (config.get("PAPER_INITIAL_BALANCES", {}) or {}).get(
        config.get("QUOTE_ASSET", "USDT"), 10_000.0
    )
    value = float(config.get("BACKTEST_INITIAL_EQUITY_USDT", 0.0) or 0.0)
    if value <= 0:
        value = float(paper or 0.0)
    return max(0.0, value)


def entry_execution_params(config: dict) -> tuple:
    try:
        from config.config import PUMP_DEFAULTS

        d_spread = PUMP_DEFAULTS.get("BACKTEST_ENTRY_SPREAD_PCT", 0.10)
        d_slip = PUMP_DEFAULTS.get("BACKTEST_SLIPPAGE_PCT", 0.05)
        d_delay = PUMP_DEFAULTS.get("BACKTEST_ENTRY_DELAY_BARS", 1)
    except ImportError:
        d_spread, d_slip, d_delay = 0.10, 0.05, 1

    spread = max(0.0, float(config.get("BACKTEST_ENTRY_SPREAD_PCT", d_spread) or 0.0))
    slippage = max(0.0, float(config.get("BACKTEST_SLIPPAGE_PCT", d_slip) or 0.0))
    delay = max(0, int(config.get("BACKTEST_ENTRY_DELAY_BARS", d_delay) or 0))
    return spread, slippage, delay


def compute_rolling_24h_stats(klines: list[Kline], window: int) -> list[Optional[dict]]:
    n = len(klines)
    out: list[Optional[dict]] = [None] * n
    if n == 0:
        return out
    vol_sum = 0.0
    for i in range(n):
        vol_sum += klines[i].quote_volume
        if i >= window:
            vol_sum -= klines[i - window].quote_volume
        if i >= window - 1:
            ref_close = klines[i - window + 1].open
            if ref_close > 0:
                pct = (klines[i].close / ref_close - 1.0) * 100.0
                out[i] = {"pct24h": pct, "vol24h": vol_sum}
    return out


def fetch_full_klines(
    client,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    progress_cb: Optional[Callable[[float], None]] = None,
    sleep_between_calls: float = 0.25,
) -> list[Kline]:
    from strategy.indicators import parse_klines

    all_rows: list = []
    cursor = start_ms
    minutes = INTERVAL_MINUTES.get(interval, 5)
    step_ms = minutes * MS_PER_MIN
    total_span = max(1, end_ms - start_ms)
    guard = 0
    guard_limit = 5000

    while cursor < end_ms:
        guard += 1
        if guard > guard_limit:
            raise BacktestError(
                "Terlalu banyak halaman data, dihentikan demi keamanan."
            )
        raw = client.get_klines(
            symbol, interval, limit=1000, start_time_ms=cursor, end_time_ms=end_ms
        )
        if not raw:
            break
        all_rows.extend(raw)
        last_open_time = int(raw[-1][0])
        next_cursor = last_open_time + step_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if progress_cb:
            done = min(1.0, (cursor - start_ms) / total_span)
            progress_cb(done)
        if len(raw) < 1000:
            break
        if sleep_between_calls:
            time.sleep(sleep_between_calls)

    klines = parse_klines(all_rows)
    seen = set()
    unique = []
    for k in klines:
        if k.open_time in seen:
            continue
        seen.add(k.open_time)
        unique.append(k)
    unique.sort(key=lambda k: k.open_time)
    return unique


def run_backtest(
    klines: list[Kline],
    config: dict,
    warmup_bars: int,
    progress_cb: Optional[Callable[[float], None]] = None,
    btc_klines: Optional[list[Kline]] = None,
    trend_klines: Optional[list[Kline]] = None,
    m15_klines: Optional[list[Kline]] = None,
) -> BacktestResult:
    interval = config.get("CONFIRM_INTERVAL", "5m")
    window = bars_per_day(interval)
    stats = compute_rolling_24h_stats(klines, window)

    lookback = strategy.confirm_window_bars(config)
    min_vol = config["MIN_QUOTE_VOLUME_USDT_24H"]

    n = len(klines)
    trades: list[BacktestTrade] = []
    warnings: list[str] = []
    trend_scans = 0
    trend_skips = 0
    htf_demand_scans = 0
    htf_demand_skips = 0
    m15_demand_scans = 0
    m15_demand_skips = 0
    m15_trend_scans = 0
    m15_trend_skips = 0

    initial_equity = initial_backtest_equity(config)
    symbol = str(config.get("_symbol", "") or "")
    historical_tradable = config.get("_historical_tradable_symbols")
    if symbol and not scanner.is_structurally_allowed_symbol(
        symbol, config, historical_tradable
    ):
        warnings.append(
            "Simbol ditolak oleh policy semesta bersama (quote, stablecoin, leveraged token, blacklist, atau status historis)."
        )
        return BacktestResult(
            symbol=symbol,
            interval=interval,
            bars_total=n,
            bars_usable=0,
            start_time=klines[0].open_time if klines else 0,
            end_time=klines[-1].close_time if klines else 0,
            trades=[],
            params=config,
            warnings=warnings,
            initial_equity=initial_equity,
            final_equity=initial_equity,
        )
    equity = initial_equity
    pos: Optional[parity.PositionState] = None
    entry_price = 0.0
    entry_time = 0
    position_notional = 0.0
    equity_before_entry = 0.0
    mark_to_market_open = False
    mark_to_market_pnl_pct = 0.0
    mark_to_market_pnl_quote = 0.0
    next_entry_allowed_at = 0
    chase_skips = 0
    symbol_blocks: dict = {}
    same_coin_skips = 0

    try:
        from config.config import get_taker_fee_pct as _fee_fn

        fee_round_trip_pct = _fee_fn(config) * 2.0
    except ImportError:
        fee_round_trip_pct = float(config.get("TAKER_FEE_PCT", 0.1)) * 2.0

    execution_spread_pct, execution_slippage_pct, entry_delay_bars = (
        entry_execution_params(config)
    )
    max_chase_pct = float(config.get("MAX_CHASE_PCT", 0) or 0)

    controls = parity.AccountRiskControls(config, initial_equity)
    bar_ms = strategy.interval_to_ms(interval)
    cap_ms = parity.max_hold_ms(config, interval)
    btc_lookup = parity.make_btc_lookup(btc_klines, config, bar_ms)
    gate_cfg = parity.gate_config(config, btc_lookup)
    btc_warning = parity.btc_filter_warning(config, btc_lookup)
    if btc_warning:
        warnings.append(btc_warning)

    trend_lookup = parity.make_trend_lookup(
        trend_klines if trend_klines is not None else klines,
        config,
        interval,
        sudah_dirangkai=trend_klines is not None,
    )
    m15_lookup = parity.make_m15_lookup(
        m15_klines if m15_klines is not None else klines,
        config,
        interval,
        sudah_dirangkai=m15_klines is not None,
    )
    m15_trend_lookup = parity.make_m15_trend_lookup(klines, config, interval)
    if trend_lookup is not None or m15_lookup is not None or m15_trend_lookup is not None:
        butuh_warmup = strategy.htf_gate_warmup_bars(config, interval)
        if int(warmup_bars) < butuh_warmup:
            bagian = []
            if trend_lookup is not None:
                bagian.append(
                    f"gerbang timeframe tinggi {config.get('TREND_INTERVAL', '1h')} "
                    f"(trend + zona demand) butuh {trend_lookup.window} candle tertutup"
                )
            if m15_lookup is not None:
                bagian.append(
                    f"gerbang zona demand {m15_lookup.interval} butuh "
                    f"{m15_lookup.window} candle {m15_lookup.interval} tertutup"
                )
            if m15_trend_lookup is not None:
                bagian.append(
                    f"gerbang EMA + ADX {m15_trend_lookup.interval} butuh "
                    f"{m15_trend_lookup.window} candle {m15_trend_lookup.interval} tertutup"
                )
            warnings.append(
                f"Warmup {warmup_bars} bar {interval} dinaikkan menjadi {butuh_warmup} bar "
                f"karena {' dan '.join(bagian)}. "
                "Naikkan warmup di pemanggil (atau perpanjang rentang data) supaya "
                "backtest dan live memakai riwayat yang sama."
            )
            warmup_bars = butuh_warmup

    i = max(warmup_bars, window - 1, lookback)
    start_idx = i
    if m15_trend_lookup is not None or m15_lookup is not None:
        bar_uji_m15 = max(0.0, (n - start_idx) / float(strategy.m15_ratio(interval)))
        if m15_trend_lookup is not None:
            butuh_tren = strategy.m15_trend_required_bars(config)
            if bar_uji_m15 < butuh_tren:
                warnings.append(
                    f"Periode uji sekitar {bar_uji_m15:.1f} candle M15, lebih pendek "
                    f"dari {butuh_tren} candle yang dibutuhkan EMA + ADX M15. "
                    "Perpanjang rentang uji agar filter ini teruji di beberapa siklus."
                )
        if m15_lookup is not None:
            lookback_m15 = strategy.m15_demand_lookback_bars(config)
            if bar_uji_m15 < lookback_m15:
                warnings.append(
                    f"Periode uji sekitar {bar_uji_m15:.1f} candle M15, lebih pendek "
                    f"dari {lookback_m15} candle lookback zona demand M15. "
                    "Perpanjang rentang uji atau kecilkan M15_DEMAND_LOOKBACK_BARS."
                )

    while i < n:
        if progress_cb and i % 200 == 0:
            progress_cb(min(1.0, (i - start_idx) / max(1, n - start_idx)))

        candle = klines[i]

        if pos is None:
            entries_paused = controls.update(candle.close_time, equity)
            st = stats[i]
            if (
                not entries_paused
                and st is not None
                and st["vol24h"] >= min_vol
                and candle.open_time >= next_entry_allowed_at
                and scanner.market_gate_ok_at(
                    st["pct24h"],
                    st["vol24h"],
                    gate_cfg,
                    btc_drop_pct=(
                        btc_lookup.drop_pct_at(candle.open_time)
                        if btc_lookup is not None
                        else None
                    ),
                )
            ):
                window_klines = klines[max(0, i - lookback + 1) : i + 1]
                setup = scanner.detect_entry_setup(window_klines, config)
                if setup.ok and parity.same_coin_blocked(
                    symbol, candle.open_time, symbol_blocks
                ):
                    same_coin_skips += 1
                    i += 1
                    continue
                if setup.ok and trend_lookup is not None:
                    trend_scans += 1
                    verdict = trend_lookup.verdict_at(candle.close_time)
                    if not verdict["ok"]:
                        trend_skips += 1
                        i += 1
                        continue
                if (
                    setup.ok
                    and trend_lookup is not None
                    and bool(config.get("HTF_DEMAND_FILTER_ENABLED", False))
                ):
                    htf_demand_scans += 1
                    verdict_htf = trend_lookup.htf_demand_at(candle.close_time)
                    if not verdict_htf["ok"]:
                        htf_demand_skips += 1
                        i += 1
                        continue
                if (
                    setup.ok
                    and m15_lookup is not None
                    and bool(config.get("M15_DEMAND_FILTER_ENABLED", False))
                ):
                    m15_demand_scans += 1
                    verdict_m15 = m15_lookup.m15_demand_at(candle.close_time)
                    if not verdict_m15["ok"]:
                        m15_demand_skips += 1
                        i += 1
                        continue
                if setup.ok and m15_trend_lookup is not None:
                    m15_trend_scans += 1
                    if not m15_trend_lookup.verdict_at(candle.close_time)["ok"]:
                        m15_trend_skips += 1
                        i += 1
                        continue
                if setup.ok:
                    entry_idx = i + entry_delay_bars
                    if entry_idx >= n:
                        warnings.append(
                            "Sinyal terakhir tidak memiliki bar eksekusi setelah latency entry; trade dilewati."
                        )
                        i += 1
                        continue
                    sizing = strategy.resolve_position_notional(config, equity)
                    if sizing["notional"] <= 0 or sizing["notional"] > equity:
                        i += 1
                        continue
                    if entry_delay_bars == 0:
                        raw_entry_price = candle.close
                        entry_time_value = candle.close_time
                        first_exit_idx = i + 1
                    else:
                        entry_candle = klines[entry_idx]
                        raw_entry_price = entry_candle.open
                        entry_time_value = entry_candle.open_time
                        first_exit_idx = entry_idx
                    level_cfg = dict(config)
                    if bool(config.get("USE_ATR_EXIT", False)):
                        atr_val = setup.atr_value
                        if atr_val is None:
                            warnings.append(
                                f"Entry bar {i} dilewati: USE_ATR_EXIT aktif tetapi "
                                "nilai ATR tidak tersedia, level exit tidak dapat "
                                "dikunci dengan aman (paritas open_position)."
                            )
                            i += 1
                            continue
                        level_cfg["_atr_value"] = atr_val
                    lv = strategy.resolve_exit_levels(level_cfg)
                    if str(lv.get("source", "")).upper() == "ATR" and not (
                        0.0 < float(lv.get("sl_pct") or 0.0) < raw_entry_price
                    ):
                        warnings.append(
                            f"Entry bar {i} dilewati: jarak SL ATR "
                            f"{float(lv.get('sl_pct') or 0.0):.10g} tidak masuk akal "
                            f"terhadap harga acuan {raw_entry_price:.10g} "
                            "(paritas open_position). Cek ATR_MULT_SL/ATR."
                        )
                        i += 1
                        continue
                    exec_entry_price = strategy.backtest_buy_execution_price(
                        raw_entry_price, execution_spread_pct, execution_slippage_pct
                    )
                    if parity.chase_exceeded(
                        exec_entry_price, setup.signal_close, max_chase_pct
                    ):
                        chase_skips += 1
                        i += 1
                        continue
                    pos = parity.PositionState(
                        exec_entry_price, lv, entry_time=entry_time_value
                    )
                    position_notional = sizing["notional"]
                    equity_before_entry = equity
                    entry_price = exec_entry_price
                    entry_time = entry_time_value
                    i = first_exit_idx
                    continue
            i += 1
            continue

        hold_minutes = (candle.close_time - entry_time) / 60000.0
        exit_reason = None
        exit_price = None
        verdict = parity.evaluate_candle_exit(
            pos, candle, config, cap_ms=cap_ms, bar_ms=bar_ms
        )
        if verdict is not None:
            exit_reason, exit_price = verdict

        is_last_bar = i == n - 1
        if exit_reason is None:
            mtm_pnl_pct = (candle.close / entry_price - 1.0) * 100.0
            mtm_pnl_quote = position_notional * mtm_pnl_pct / 100.0
            mtm_equity = equity + mtm_pnl_quote
            entries_paused = controls.update(candle.close_time, mtm_equity)
            if controls.force_close_due(entries_paused, True):
                exit_reason = parity.RISK_LIMIT_REASON
                exit_price = candle.close
            elif is_last_bar:
                mark_to_market_open = True
                mark_to_market_pnl_pct = mtm_pnl_pct
                mark_to_market_pnl_quote = mtm_pnl_quote
                warnings.append(
                    "Posisi terakhir masih terbuka pada akhir data. Nilainya dicatat "
                    "sebagai mark-to-market, bukan sebagai trade tertutup."
                )

        if exit_reason:
            raw_exit_price = exit_price
            exit_price = strategy.backtest_sell_execution_price(
                raw_exit_price, execution_spread_pct, execution_slippage_pct
            )
            gross_pct = (exit_price / entry_price - 1.0) * 100.0
            pnl_pct = gross_pct - fee_round_trip_pct
            pnl_quote = position_notional * pnl_pct / 100.0
            equity_after = max(0.0, equity + pnl_quote)
            trades.append(
                BacktestTrade(
                    entry_time=entry_time,
                    entry_price=entry_price,
                    exit_time=candle.close_time,
                    exit_price=exit_price,
                    reason=exit_reason,
                    hold_minutes=hold_minutes,
                    pnl_pct=pnl_pct,
                    sl_pct=pos.levels["sl"],
                    tp_pct=pos.levels["tp"],
                    exit_source=pos.levels["src"],
                    gross_pnl_pct=gross_pct,
                    fee_pct=fee_round_trip_pct,
                    position_notional=position_notional,
                    equity_before=equity_before_entry,
                    equity_after=equity_after,
                    pnl_quote=pnl_quote,
                )
            )
            equity = equity_after
            pos = None
            position_notional = 0.0
            next_entry_allowed_at = parity.next_entry_allowed(candle.close_time, config)
            parity.same_coin_block_record(
                symbol_blocks, symbol, candle.close_time, pnl_quote, config
            )
            controls.update(candle.close_time, equity)

        i += 1

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
    if trend_lookup is not None and trend_skips:
        warnings.append(
            f"{trend_skips} dari {trend_scans} sinyal konfirmasi dilewati oleh gerbang "
            f"trend {config.get('TREND_INTERVAL', '1h')} "
            f"(EMA{int(config.get('TREND_EMA_FAST', 20))}/"
            f"EMA{int(config.get('TREND_EMA_SLOW', 50))} dan ADX"
            f"{int(config.get('TREND_ADX_PERIOD', 14))} >= "
            f"{float(config.get('TREND_ADX_MIN', 20.0) or 0.0):g}), sama seperti bot live."
        )
    if trend_lookup is not None and htf_demand_skips:
        warnings.append(
            f"{htf_demand_skips} dari {htf_demand_scans} sinyal konfirmasi dilewati oleh "
            f"gerbang zona demand {config.get('TREND_INTERVAL', '1h')} "
            f"(lookback {int(config.get('HTF_DEMAND_LOOKBACK_BARS', 72) or 72)} candle, "
            f"jarak maks "
            f"{float(config.get('HTF_DEMAND_MAX_DISTANCE_PCT', 10.0) or 0.0):g}% dari atap "
            "zona), sama seperti bot live."
        )
    if m15_lookup is not None and m15_demand_skips:
        warnings.append(
            f"{m15_demand_skips} dari {m15_demand_scans} sinyal konfirmasi dilewati "
            f"oleh gerbang zona demand {m15_lookup.interval} "
            f"(lookback {strategy.m15_demand_lookback_bars(config)} candle, "
            f"jarak maks "
            f"{float(config.get('M15_DEMAND_MAX_DISTANCE_PCT', 5.0) or 0.0):g}% dari "
            "atap zona), sama seperti bot live."
        )
    if m15_trend_lookup is not None and m15_trend_skips:
        warnings.append(
            f"{m15_trend_skips} dari {m15_trend_scans} sinyal konfirmasi dilewati oleh "
            f"gerbang EMA{int(config.get('M15_TREND_EMA_FAST', 20) or 20)}/"
            f"EMA{int(config.get('M15_TREND_EMA_SLOW', 50) or 50)} dan ADX"
            f"{int(config.get('M15_TREND_ADX_PERIOD', 14) or 14)} >= "
            f"{float(config.get('M15_TREND_ADX_MIN', 20.0) or 0.0):g} pada "
            f"{m15_trend_lookup.interval}, sama seperti bot live."
        )
    result = BacktestResult(
        symbol=config.get("_symbol", "?"),
        interval=interval,
        bars_total=n,
        bars_usable=max(0, n - start_idx),
        start_time=(
            klines[start_idx].open_time
            if n > start_idx
            else (klines[0].open_time if n else 0)
        ),
        end_time=klines[-1].close_time if n else 0,
        trades=trades,
        params=config,
        warnings=warnings,
        initial_equity=initial_equity,
        final_equity=equity + mark_to_market_pnl_quote,
        risk_events=dict(controls.events),
        chase_skips=chase_skips,
        trend_skips=trend_skips,
        htf_demand_skips=htf_demand_skips,
        m15_demand_skips=m15_demand_skips,
        m15_demand_scans=m15_demand_scans,
        m15_trend_skips=m15_trend_skips,
        m15_trend_scans=m15_trend_scans,
        same_coin_skips=same_coin_skips,
        mark_to_market_open=mark_to_market_open,
        mark_to_market_pnl_pct=mark_to_market_pnl_pct,
        mark_to_market_pnl_quote=mark_to_market_pnl_quote,
    )
    return result


def summarize(result: BacktestResult) -> dict:
    all_trades = list(result.trades)
    legacy_mtm_trades = [t for t in all_trades if t.reason == "END_OF_DATA"]
    trades = [t for t in all_trades if t.reason != "END_OF_DATA"]
    total = len(trades)
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    win_rate = (len(wins) / total * 100.0) if total else 0.0

    initial = result.initial_equity or initial_backtest_equity(result.params)
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
    mark_to_market_pnl_quote = float(
        getattr(result, "mark_to_market_pnl_quote", 0.0) or 0.0
    )
    mark_to_market_pnl_pct = float(
        getattr(result, "mark_to_market_pnl_pct", 0.0) or 0.0
    )
    if legacy_mtm_trades:
        mark_to_market_open = True
        if not getattr(result, "mark_to_market_open", False):
            legacy_final_equity = float(getattr(result, "final_equity", 0.0) or 0.0)
            mark_to_market_pnl_quote = (
                legacy_final_equity - realized_equity
                if legacy_final_equity > 0
                else float(legacy_mtm_trades[-1].pnl_quote)
            )
            mark_to_market_pnl_pct = float(legacy_mtm_trades[-1].pnl_pct)

    if mark_to_market_open:
        final_equity = max(0.0, realized_equity + mark_to_market_pnl_quote)
        gross_equity = max(0.0, gross_equity + mark_to_market_pnl_quote)
        equity_curve.append(
            (final_equity / initial - 1.0) * 100.0 if initial > 0 else 0.0
        )
    else:
        final_equity = realized_equity if trades else (result.final_equity or initial)

    total_return_pct = ((final_equity / initial) - 1.0) * 100.0 if initial > 0 else 0.0
    realized_return_pct = (
        ((realized_equity / initial) - 1.0) * 100.0 if initial > 0 else 0.0
    )
    mark_to_market_return_pct = (
        (mark_to_market_pnl_quote / initial) * 100.0
        if initial > 0 and mark_to_market_open
        else 0.0
    )
    gross_return_pct = ((gross_equity / initial) - 1.0) * 100.0 if initial > 0 else 0.0

    peak = initial
    max_dd = 0.0
    for value in [initial] + [initial * (1.0 + v / 100.0) for v in equity_curve[1:]]:
        peak = max(peak, value)
        dd = (peak - value) / peak * 100.0 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    avg_win = (sum(t.pnl_pct for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0.0
    gross_win = sum(t.pnl_pct for t in wins)
    gross_loss = abs(sum(t.pnl_pct for t in losses))
    profit_factor = (
        (gross_win / gross_loss)
        if gross_loss > 0
        else (float("inf") if gross_win > 0 else 0.0)
    )
    avg_hold = (sum(t.hold_minutes for t in trades) / total) if total else 0.0

    reason_counts: dict = {}
    for t in trades:
        reason_counts[t.reason] = reason_counts.get(t.reason, 0) + 1

    return {
        "total_trades": total,
        "closed_trades": total,
        "real_trades": total,
        "mark_to_market_open": mark_to_market_open,
        "mark_to_market_positions": int(mark_to_market_open),
        "mark_to_market_pnl_pct": mark_to_market_pnl_pct,
        "mark_to_market_pnl_quote": mark_to_market_pnl_quote,
        "mark_to_market_return_pct": mark_to_market_return_pct,
        "realized_return_pct": realized_return_pct,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_return_pct": total_return_pct,
        "max_drawdown_pct": max_dd,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "profit_factor": profit_factor,
        "avg_hold_minutes": avg_hold,
        "reason_counts": reason_counts,
        "equity_curve": equity_curve,
        "initial_equity": initial,
        "realized_equity": realized_equity,
        "final_equity": final_equity,
        "total_pnl_quote": final_equity - initial,
        "gross_return_pct": gross_return_pct,
        "fee_drag_pct": gross_return_pct - total_return_pct,
        "total_fee_pct": sum(t.fee_pct for t in trades),
        "risk_events": dict(result.risk_events),
        "chase_skips": int(result.chase_skips),
        "trend_skips": int(getattr(result, "trend_skips", 0)),
        "htf_demand_skips": int(getattr(result, "htf_demand_skips", 0)),
        "m15_demand_skips": int(getattr(result, "m15_demand_skips", 0)),
        "m15_demand_scans": int(getattr(result, "m15_demand_scans", 0)),
        "m15_trend_skips": int(getattr(result, "m15_trend_skips", 0)),
        "m15_trend_scans": int(getattr(result, "m15_trend_scans", 0)),
        "same_coin_skips": int(getattr(result, "same_coin_skips", 0)),
        **parity.per_trade_metrics(trades),
    }


def _as_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("true", "1", "on", "yes", "ya"):
        return True
    if s in ("false", "0", "off", "no", "tidak"):
        return False
    raise ValueError(f"bukan boolean: {v!r}")


OVERRIDE_CASTERS = {
        "USE_ATR_EXIT": _as_bool,
        "ATR_PERIOD": int,
        "ATR_MULT_SL": float,
        "ATR_MULT_TP": float,
        "ATR_MULT_BE_TRIGGER": float,
        "ATR_MULT_BE_LOCK": float,
        "ATR_MULT_TRAIL_START": float,
        "ATR_MULT_TRAIL": float,
        "MAX_HOLD_HOURS": float,
        "SL_PCT": float,
        "TP_PCT": float,
        "BE_TRIGGER_PCT": float,
        "BE_LOCK_PCT": float,
        "TRAILING_START_PCT": float,
        "TRAILING_STEP_PCT": float,
        "DEMAND_ZONE_FILTER_ENABLED": _as_bool,
        "DEMAND_LOOKBACK_BARS": int,
        "DEMAND_ZONE_BUFFER_PCT": float,
        "DEMAND_MAX_DISTANCE_PCT": float,
        "DEMAND_MIN_CLOSE_POSITION": float,
        "HTF_DEMAND_FILTER_ENABLED": _as_bool,
        "HTF_DEMAND_LOOKBACK_BARS": int,
        "HTF_DEMAND_ZONE_BUFFER_PCT": float,
        "HTF_DEMAND_MAX_DISTANCE_PCT": float,
        "HTF_DEMAND_MIN_CLOSE_POSITION": float,
        "M15_DEMAND_FILTER_ENABLED": _as_bool,
        "M15_DEMAND_LOOKBACK_BARS": int,
        "M15_DEMAND_ZONE_BUFFER_PCT": float,
        "M15_DEMAND_MAX_DISTANCE_PCT": float,
        "M15_DEMAND_MIN_CLOSE_POSITION": float,
        "M15_TREND_FILTER_ENABLED": _as_bool,
        "M15_TREND_EMA_FAST": int,
        "M15_TREND_EMA_SLOW": int,
        "M15_TREND_ADX_PERIOD": int,
        "M15_TREND_ADX_MIN": float,
        "M15_TREND_LOOKBACK_BARS": int,
        "TREND_FILTER_ENABLED": _as_bool,
        "TREND_INTERVAL": str,
        "TREND_EMA_FAST": int,
        "TREND_EMA_SLOW": int,
        "TREND_ADX_PERIOD": int,
        "TREND_ADX_MIN": float,
        "TREND_LOOKBACK_BARS": int,
        "PUMP_MIN_24H_CHANGE_PCT": float,
        "MIN_QUOTE_VOLUME_USDT_24H": float,
        "ROLLING_VOLUME_FILTER_ENABLED": _as_bool,
        "ROLLING_VOLUME_LOOKBACK_BARS": int,
        "ROLLING_VOLUME_SURGE_MULT": float,
        "ROLLING_VOLUME_CONFIRMATION_BARS": int,
        "SAME_COIN_BLOCK_HOURS": float,
        "SAME_COIN_BLOCK_LOSS_ONLY": _as_bool,
}


def apply_overrides(base_config: dict, overrides: dict) -> dict:
    cfg = copy.deepcopy(base_config)
    for key, caster in OVERRIDE_CASTERS.items():
        if key in overrides and overrides[key] is not None and overrides[key] != "":
            try:
                cfg[key] = caster(overrides[key])
            except (TypeError, ValueError) as exc:
                raise BacktestError(
                    f"Nilai parameter '{key}' tidak valid: {overrides[key]!r}"
                ) from exc
    return cfg


def _rentang_demand(key: str, lo: float, hi: float) -> tuple:
    try:
        from config.settings_schema import PARAMETER_SCHEMA

        spec = PARAMETER_SCHEMA.get(key) or {}
        smin, smax = spec.get("min"), spec.get("max")
        if isinstance(smin, (int, float)) and isinstance(smax, (int, float)):
            return float(smin), float(smax)
    except Exception:
        pass
    return float(lo), float(hi)


def validate_params(cfg: dict) -> None:
    if bool(cfg.get("USE_ATR_EXIT", False)):
        checks = [
            ("ATR_PERIOD", 1, 1000),
            ("ATR_MULT_SL", 0.0001, 1000),
            ("ATR_MULT_TP", 0.0001, 1000),
            ("ATR_MULT_BE_TRIGGER", 0.0001, 1000),
            ("ATR_MULT_BE_LOCK", 0.0, 1000),
            ("ATR_MULT_TRAIL_START", 0.0001, 1000),
            ("ATR_MULT_TRAIL", 0.0001, 1000),
        ]
    else:
        checks = [
            ("SL_PCT", 0.01, 1000),
            ("TP_PCT", 0.01, 1000),
            ("BE_TRIGGER_PCT", 0.01, 1000),
            ("BE_LOCK_PCT", -100, 1000),
            ("TRAILING_START_PCT", 0.01, 1000),
            ("TRAILING_STEP_PCT", 0.01, 1000),
        ]
    for key, lo, hi in checks:
        val = cfg.get(key)
        if val is None or not (lo <= val <= hi):
            raise BacktestError(
                f"Parameter '{key}'={val} di luar rentang wajar ({lo}..{hi})."
            )

    if bool(cfg.get("DEMAND_ZONE_FILTER_ENABLED", False)):
        demand_checks = [
            ("DEMAND_LOOKBACK_BARS", 3, 500),
            ("DEMAND_ZONE_BUFFER_PCT", 0.05, 20.0),
            ("DEMAND_MAX_DISTANCE_PCT", 0.1, 50.0),
            ("DEMAND_MIN_CLOSE_POSITION", 0.0, 1.0),
        ]
        for key, lo, hi in demand_checks:
            lo, hi = _rentang_demand(key, lo, hi)
            val = cfg.get(key)
            if val is None or not (lo <= float(val) <= hi):
                raise BacktestError(
                    f"Parameter '{key}'={val} di luar rentang wajar ({lo:g}..{hi:g})."
                )

    if bool(cfg.get("HTF_DEMAND_FILTER_ENABLED", False)):
        htf_demand_checks = [
            ("HTF_DEMAND_LOOKBACK_BARS", 3, 500),
            ("HTF_DEMAND_ZONE_BUFFER_PCT", 0.05, 20.0),
            ("HTF_DEMAND_MAX_DISTANCE_PCT", 0.1, 50.0),
            ("HTF_DEMAND_MIN_CLOSE_POSITION", 0.0, 1.0),
        ]
        for key, lo, hi in htf_demand_checks:
            lo, hi = _rentang_demand(key, lo, hi)
            val = cfg.get(key)
            if val is None or not (lo <= float(val) <= hi):
                raise BacktestError(
                    f"Parameter '{key}'={val} di luar rentang wajar ({lo:g}..{hi:g})."
                )

    if bool(cfg.get("M15_DEMAND_FILTER_ENABLED", False)):
        m15_demand_checks = [
            ("M15_DEMAND_LOOKBACK_BARS", 3, 500),
            ("M15_DEMAND_ZONE_BUFFER_PCT", 0.05, 20.0),
            ("M15_DEMAND_MAX_DISTANCE_PCT", 0.1, 50.0),
            ("M15_DEMAND_MIN_CLOSE_POSITION", 0.0, 1.0),
        ]
        for key, lo, hi in m15_demand_checks:
            lo, hi = _rentang_demand(key, lo, hi)
            val = cfg.get(key)
            if val is None or not (lo <= float(val) <= hi):
                raise BacktestError(
                    f"Parameter '{key}'={val} di luar rentang wajar ({lo:g}..{hi:g})."
                )
        try:
            strategy.m15_demand_warmup_bars(cfg, cfg.get("CONFIRM_INTERVAL", "5m"))
        except ValueError as exc:
            raise BacktestError(str(exc)) from exc

    if bool(cfg.get("M15_TREND_FILTER_ENABLED", False)):
        m15_trend_checks = [
            ("M15_TREND_EMA_FAST", 2, 500),
            ("M15_TREND_EMA_SLOW", 3, strategy.TREND_KLINE_LIMIT - 1),
            ("M15_TREND_ADX_PERIOD", 2, 200),
            ("M15_TREND_ADX_MIN", 0.0, 100.0),
            ("M15_TREND_LOOKBACK_BARS", 20, strategy.TREND_KLINE_LIMIT - 1),
        ]
        for key, lo, hi in m15_trend_checks:
            val = cfg.get(key)
            if val is None or not (lo <= float(val) <= hi):
                raise BacktestError(
                    f"Parameter '{key}'={val} di luar rentang wajar ({lo:g}..{hi:g})."
                )
        try:
            strategy.m15_trend_warmup_bars(cfg, cfg.get("CONFIRM_INTERVAL", "5m"))
        except ValueError as exc:
            raise BacktestError(str(exc)) from exc

    from config.settings_schema import relation_violations

    pelanggaran = relation_violations(cfg, mode_aware=True)
    if pelanggaran:
        daftar = "; ".join(f"{kunci}: {pesan}" for kunci, pesan in pelanggaran.items())
        raise BacktestError(f"Kombinasi parameter tidak konsisten: {daftar}")

    if bool(cfg.get("TREND_FILTER_ENABLED", False)):
        trend_checks = [
            ("TREND_EMA_FAST", 2, 500),
            ("TREND_EMA_SLOW", 3, 1000),
            ("TREND_ADX_PERIOD", 2, 200),
            ("TREND_ADX_MIN", 0.0, 100.0),
            ("TREND_LOOKBACK_BARS", 20, strategy.TREND_KLINE_LIMIT - 1),
        ]
        for key, lo, hi in trend_checks:
            val = cfg.get(key)
            if val is None or not (lo <= float(val) <= hi):
                raise BacktestError(
                    f"Parameter '{key}'={val} di luar rentang wajar ({lo}..{hi})."
                )
        interval = cfg.get("CONFIRM_INTERVAL", "5m")
        try:
            butuh_trend = strategy.trend_required_bars(cfg)
            strategy.trend_warmup_bars(cfg, interval)
        except ValueError as exc:
            raise BacktestError(str(exc)) from exc
        if int(cfg.get("TREND_LOOKBACK_BARS", 0)) < butuh_trend:
            raise BacktestError(
                f"TREND_LOOKBACK_BARS={cfg.get('TREND_LOOKBACK_BARS')} lebih kecil dari "
                f"{butuh_trend} candle yang dibutuhkan EMA dan ADX pada "
                f"{cfg.get('TREND_INTERVAL', '1h')}. Naikkan nilainya supaya backtest "
                "dan bot live memakai jendela trend yang sama."
            )

    butuh = strategy.required_lookback_bars(cfg)
    if int(cfg.get("CONFIRM_LOOKBACK_BARS", 0)) < butuh:
        raise BacktestError(
            f"CONFIRM_LOOKBACK_BARS={cfg.get('CONFIRM_LOOKBACK_BARS')} lebih kecil dari {butuh} "
            "candle yang dibutuhkan konfirmasi volume dan ATR. Naikkan nilainya supaya backtest "
            "dan bot live memakai jendela yang sama."
        )

    pump_min = cfg.get("PUMP_MIN_24H_CHANGE_PCT", 10.0)
    try:
        minimum = float(pump_min)
    except (TypeError, ValueError):
        raise BacktestError(
            f"Parameter 'PUMP_MIN_24H_CHANGE_PCT'={pump_min!r} bukan angka."
        ) from None
    if not math.isfinite(minimum) or not (-1000.0 <= minimum <= 1000.0):
        raise BacktestError(
            f"Parameter 'PUMP_MIN_24H_CHANGE_PCT'={pump_min!r} "
            "di luar rentang wajar (-1000..1000)."
        )

    block_hours = cfg.get("SAME_COIN_BLOCK_HOURS")
    if block_hours is not None:
        lo, hi = _rentang_demand("SAME_COIN_BLOCK_HOURS", 0.0, 8760.0)
        try:
            nilai_block = float(block_hours)
        except (TypeError, ValueError):
            raise BacktestError(
                f"Parameter 'SAME_COIN_BLOCK_HOURS'={block_hours} bukan angka."
            ) from None
        if not math.isfinite(nilai_block) or not (lo <= nilai_block <= hi):
            raise BacktestError(
                f"Parameter 'SAME_COIN_BLOCK_HOURS'={block_hours} di luar rentang "
                f"wajar ({lo:g}..{hi:g})."
            )

    max_hold = cfg.get("MAX_HOLD_HOURS")
    if max_hold is not None and max_hold != "":
        lo, hi = _rentang_demand("MAX_HOLD_HOURS", 0.0, parity.MAX_HOLD_HOURS_LIMIT)
        try:
            nilai_hold = float(max_hold)
        except (TypeError, ValueError):
            raise BacktestError(
                f"Parameter 'MAX_HOLD_HOURS'={max_hold} bukan angka."
            ) from None
        if not math.isfinite(nilai_hold) or not (lo <= nilai_hold <= hi):
            raise BacktestError(
                f"Parameter 'MAX_HOLD_HOURS'={max_hold} di luar rentang wajar "
                f"({lo:g}..{hi:g}). 0 berarti time stop nonaktif."
            )
        # Cek juga terhadap interval simulasi: batas di bawah 2 bar ditolak supaya
        # time stop tidak menutup posisi pada bar entry-nya sendiri.
        try:
            parity.max_hold_ms(cfg, cfg.get("CONFIRM_INTERVAL", "5m"))
        except ValueError as exc:
            raise BacktestError(str(exc)) from exc


def print_single_result(result: BacktestResult) -> None:
    summary = summarize(result)
    print("\n" + "=" * 72)
    print("HASIL BACKTEST")
    print("=" * 72)
    print(f"Simbol             : {result.symbol}")
    print(f"Interval           : {result.interval}")
    print(f"Candle total       : {result.bars_total}")
    print(f"Trade tertutup     : {summary['closed_trades']}")
    print(f"Return total + MTM : {summary['total_return_pct']:+.2f}%")
    print(f"Return terealisasi : {summary['realized_return_pct']:+.2f}%")
    print(f"Return kotor       : {summary['gross_return_pct']:+.2f}%")
    print(f"Win rate tertutup  : {summary['win_rate']:.1f}%")
    if summary["mark_to_market_open"]:
        quote = result.params.get("QUOTE_ASSET", "USDT")
        print(
            f"Posisi akhir MTM   : {summary['mark_to_market_pnl_quote']:+.2f} "
            f"{quote} ({summary['mark_to_market_pnl_pct']:+.2f}%)"
        )
    print(f"Max drawdown       : {summary['max_drawdown_pct']:.2f}%")
    print(f"Profit factor      : {summary['profit_factor']:.2f}")
    print(
        f"Rata2 per trade    : {summary['avg_trade_pct']:+.3f}% (median {summary['median_trade_pct']:+.3f}%, "
        f"terbaik {summary['best_trade_pct']:+.2f}%, terburuk {summary['worst_trade_pct']:+.2f}%)"
    )
    print(f"Rugi beruntun maks : {summary['max_consecutive_losses']}")
    print(f"Modal awal         : {summary['initial_equity']:.2f}")
    print(f"Alasan exit        : {dict(sorted(summary['reason_counts'].items()))}")
    risk_info = {k: v for k, v in summary.get("risk_events", {}).items() if v}
    if (
        risk_info
        or summary.get("chase_skips")
        or summary.get("trend_skips")
        or summary.get("htf_demand_skips")
        or summary.get("m15_demand_skips")
        or summary.get("m15_trend_skips")
    ):
        print(
            f"Kontrol akun/filter: {risk_info}, chase dilewati {summary.get('chase_skips', 0)}, "
            f"trend dilewati {summary.get('trend_skips', 0)}, "
            f"demand H1 dilewati {summary.get('htf_demand_skips', 0)}, "
            f"demand M15 dilewati {summary.get('m15_demand_skips', 0)}, "
            f"EMA+ADX M15 dilewati {summary.get('m15_trend_skips', 0)}"
        )
    if result.warnings:
        print("Peringatan:")
        for item in result.warnings:
            print(f"  - {item}")
    print("=" * 72 + "\n")


def main():
    import argparse as _argparse
    import sys as _sys

    parser = _argparse.ArgumentParser(description="Backtest pump scanner")
    parser.add_argument("--symbol", default="BTCUSDT", help="Simbol, mis. SOLUSDT")
    parser.add_argument(
        "--days", type=int, default=30, help="Jumlah hari data historis"
    )
    parser.add_argument(
        "--interval", default=None, help="Interval candle, default dari config"
    )
    parser.add_argument(
        "--grid",
        metavar="SPEC",
        default=None,
        help="Jalankan grid search. Format: "
        '"SL_PCT=1:4:0.5,TP_PCT=2:8:1" untuk rentang, atau '
        '"TP_PCT=2|4|6" untuk daftar nilai. Data diunduh '
        "SEKALI lalu dipakai ulang untuk semua kombinasi.",
    )
    parser.add_argument(
        "--grid-metric",
        default="total_return_pct",
        help="Metrik peringkat: total_return_pct, profit_factor, "
        "return_per_drawdown, win_rate",
    )
    parser.add_argument(
        "--grid-train",
        type=float,
        default=0.7,
        help="Porsi data untuk periode latih (0.7 = 70 persen). "
        "Sisanya dipakai memvalidasi hasil pada data baru.",
    )
    parser.add_argument(
        "--grid-min-trades",
        type=int,
        default=10,
        help="Di bawah jumlah trade ini hasil ditandai tidak andal.",
    )
    parser.add_argument(
        "--grid-top", type=int, default=15, help="Jumlah baris teratas yang dicetak."
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Jalankan pemeriksaan aturan exit (termasuk time stop) tanpa menyentuh Binance.",
    )
    args = parser.parse_args()

    if args.selftest:
        from backtesting.exit_selftest import run as _run_exit_selftest

        sukses, _ = _run_exit_selftest(verbose=True)
        raise SystemExit(0 if sukses else 1)

    if len(_sys.argv) == 1:
        parser.print_help()
        return

    from config.config import PUMP_CONFIG
    from trading.clients.binance_client import BinanceSpotClient

    cfg = copy.deepcopy(PUMP_CONFIG)
    cfg["_symbol"] = args.symbol
    if args.interval:
        cfg["CONFIRM_INTERVAL"] = args.interval
    validate_params(cfg)

    interval = cfg["CONFIRM_INTERVAL"]
    warmup = bars_per_day(interval)
    try:
        butuh_gerbang = strategy.htf_gate_warmup_bars(cfg, interval)
    except ValueError as exc:
        raise BacktestError(str(exc)) from exc
    warmup = max(warmup, butuh_gerbang)
    total_bars = warmup + bars_per_day(interval) * args.days
    bar_per_hari = float(bars_per_day(interval))

    print(
        f"Mengambil {total_bars} candle {interval} untuk {args.symbol} "
        f"({args.days} hari + warmup {warmup} candle = "
        f"{warmup / bar_per_hari:.2f} hari)..."
    )
    uji_bars_m15 = args.days * (1440 // strategy.INTERVAL_MINUTES[strategy.M15_INTERVAL])
    if strategy.m15_trend_enabled(cfg):
        print(
            f"  Gerbang EMA + ADX M15 aktif: jendela "
            f"{strategy.m15_trend_window_bars(cfg)} candle M15 (minimum "
            f"{strategy.m15_trend_required_bars(cfg)} candle untuk EMA dan ADX). "
            "Koin dengan riwayat lebih pendek ditolak."
        )
        if uji_bars_m15 < strategy.m15_trend_required_bars(cfg):
            print(
                f"PERINGATAN: --days {args.days} mencakup sekitar {uji_bars_m15} "
                f"candle M15, lebih pendek dari kebutuhan EMA + ADX "
                f"({strategy.m15_trend_required_bars(cfg)} candle)."
            )
    if strategy.m15_demand_enabled(cfg):
        pemanasan_m15 = strategy.m15_demand_warmup_bars(cfg, interval)
        print(
            f"  Gerbang demand M15 aktif: {pemanasan_m15} candle {interval} "
            f"(sekitar {pemanasan_m15 / bar_per_hari:.2f} hari) diperlukan "
            "untuk warmup. Rentang unduhan diperpanjang otomatis."
        )
        lookback_m15 = strategy.m15_demand_lookback_bars(cfg)
        if uji_bars_m15 < lookback_m15:
            print(
                f"PERINGATAN: --days {args.days} mencakup sekitar {uji_bars_m15} "
                f"candle M15, lebih pendek dari lookback demand {lookback_m15} candle. "
                "Perpanjang periode uji atau kecilkan M15_DEMAND_LOOKBACK_BARS."
            )
    client = BinanceSpotClient(
        "",
        "",
        cfg["LIVE_BASE_URL"],
        allow_signed=False,
        rate_limit_state_file=cfg.get("RATE_LIMIT_STATE_FILE"),
        rate_limit_limit=int(cfg.get("RATE_LIMIT_WEIGHT_LIMIT", 6000) or 6000),
        rate_limit_safety_margin=int(cfg.get("RATE_LIMIT_SAFETY_MARGIN", 100) or 100),
    )
    end_ms = last_completed_candle_end_ms(interval)
    start_ms = end_ms - (total_bars + 1) * strategy.interval_to_ms(interval)
    klines = fetch_full_klines(client, args.symbol, interval, start_ms, end_ms)
    print(f"Dapat {len(klines)} candle.")

    if len(klines) < warmup + 50:
        raise BacktestError(
            f"Data terlalu sedikit ({len(klines)} candle) untuk backtest yang berarti."
        )

    btc_klines = None
    if cfg.get("BTC_FILTER_ENABLED", False):
        btc_symbol = "BTC" + str(cfg.get("QUOTE_ASSET", "USDT"))
        try:
            btc_klines = fetch_full_klines(
                client, btc_symbol, interval, start_ms, end_ms
            )
            print(f"Dapat {len(btc_klines)} candle {btc_symbol} untuk filter BTC.")
        except Exception as exc:
            btc_klines = None
            print(
                f"PERINGATAN: candle {btc_symbol} gagal diambil ({exc}); filter BTC tidak diterapkan."
            )

    if args.grid:
        from backtesting.grid_search import (
            GridSearchError,
            cetak_tabel,
            parse_spec_cli,
            run_grid_search,
        )

        try:
            spec = parse_spec_cli(args.grid)
        except GridSearchError as exc:
            print(f"Spesifikasi grid tidak valid: {exc}")
            raise SystemExit(2) from exc

        jumlah = 1
        for nilai in spec.values():
            jumlah *= len(nilai) if isinstance(nilai, list) else 1
        print(
            f"\nGrid search: {len(spec)} parameter, sampai {jumlah:,} kombinasi mentah."
        )
        print("Data candle dipakai ulang, tidak ada request tambahan ke Binance.\n")

        def _progress(p):
            print(f"\r  kemajuan {p*100:5.1f}%", end="", flush=True)

        try:
            hasil = run_grid_search(
                klines,
                cfg,
                spec,
                warmup,
                metrik=args.grid_metric,
                rasio_latih=args.grid_train,
                min_trades=args.grid_min_trades,
                progress_cb=_progress,
                btc_klines=btc_klines,
            )
        except GridSearchError as exc:
            print(f"\nGrid search gagal: {exc}")
            raise SystemExit(2) from exc
        print()
        cetak_tabel(hasil, top_n=args.grid_top)
        return

    result = run_backtest(klines, cfg, warmup, btc_klines=btc_klines)
    print_single_result(result)


if __name__ == "__main__":
    main()
