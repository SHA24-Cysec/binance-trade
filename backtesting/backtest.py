from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from strategy.indicators import Kline
from strategy import entry_rule
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
    entry_rule_scans: int = 0
    entry_rule_skips: int = 0
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
) -> BacktestResult:
    interval = config.get("CONFIRM_INTERVAL", "5m")
    window = bars_per_day(interval)
    stats = compute_rolling_24h_stats(klines, window)

    lookback = strategy.confirm_window_bars(config)
    min_vol = config["MIN_QUOTE_VOLUME_USDT_24H"]

    n = len(klines)
    trades: list[BacktestTrade] = []
    warnings: list[str] = []
    entry_rule_scans = 0
    entry_rule_skips = 0

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
    butuh_warmup = max(
        strategy.required_lookback_bars(config),
        strategy.gate_warmup_bars(config, interval),
    )
    if int(warmup_bars) < butuh_warmup:
        warnings.append(
            f"Warmup {warmup_bars} bar {interval} dinaikkan menjadi {butuh_warmup} bar "
            "karena aturan entry membutuhkan riwayat candle tertutup. Naikkan warmup di "
            "pemanggil (atau perpanjang rentang data) supaya backtest dan live memakai "
            "riwayat yang sama."
        )
        warmup_bars = butuh_warmup

    i = max(warmup_bars, window - 1, lookback)
    start_idx = i

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
                and scanner.market_gate_ok_at(st["vol24h"], config)
            ):
                window_klines = klines[max(0, i - lookback + 1) : i + 1]
                if strategy.entry_rule_enabled(config):
                    entry_rule_scans += 1
                setup = scanner.detect_entry_setup(
                    window_klines,
                    config,
                    signal_close_time_ms=candle.close_time,
                )
                if not setup.ok:
                    if strategy.entry_rule_enabled(config):
                        entry_rule_skips += 1
                    i += 1
                    continue
                if parity.same_coin_blocked(symbol, candle.open_time, symbol_blocks):
                    same_coin_skips += 1
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
                    exec_entry_price = strategy.backtest_buy_execution_price(
                        raw_entry_price, execution_spread_pct, execution_slippage_pct
                    )
                    if parity.chase_exceeded(
                        exec_entry_price, setup.signal_close, max_chase_pct
                    ):
                        chase_skips += 1
                        i += 1
                        continue
                    lv = strategy.resolve_exit_levels(
                        config, setup=setup, entry_price=exec_entry_price
                    )
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
    if entry_rule_skips:
        warnings.append(
            f"{entry_rule_skips} dari {entry_rule_scans} sinyal dilewati aturan entry "
            f"({strategy.entry_bull_candles(config)} candle bullish beruntun di {interval}, "
            "SL di bawah low candle sinyal, atau jarak SL melebihi batas)."
        )
    risk_summary = controls.summary()
    if risk_summary["kill_switch_triggers"] > 0:
        warnings.append(
            f"Kill switch drawdown aktif {risk_summary['kill_switch_triggers']} kali "
            f"(batas {risk_summary['max_drawdown_limit_pct']}%). "
            f"Force close: {risk_summary['force_closes']}, "
            f"reset cooldown: {risk_summary['cooldown_resets']}."
        )

    usable = max(0, n - start_idx)
    start_time = klines[start_idx].open_time if start_idx < n else 0
    end_time = klines[-1].close_time if n > 0 else 0

    if progress_cb:
        progress_cb(1.0)

    return BacktestResult(
        symbol=config.get("_symbol", ""),
        interval=interval,
        bars_total=n,
        bars_usable=usable,
        start_time=start_time,
        end_time=end_time,
        trades=trades,
        params=config,
        warnings=warnings,
        initial_equity=initial_equity,
        final_equity=equity,
        risk_events=risk_summary,
        chase_skips=chase_skips,
        entry_rule_scans=entry_rule_scans,
        entry_rule_skips=entry_rule_skips,
        same_coin_skips=same_coin_skips,
        mark_to_market_open=mark_to_market_open,
        mark_to_market_pnl_pct=mark_to_market_pnl_pct,
        mark_to_market_pnl_quote=mark_to_market_pnl_quote,
    )


def summarize(result: BacktestResult) -> dict:
    all_trades = list(result.trades)
    legacy_mtm_trades = [t for t in all_trades if t.reason == "END_OF_DATA"]
    trades = [t for t in all_trades if t.reason != "END_OF_DATA"]
    total = len(trades)
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    win_rate = (len(wins) / total * 100.0) if total else 0.0

    initial = float(result.initial_equity or initial_backtest_equity(result.params))
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
    by_reason: dict[str, dict] = {}
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

    trade_metrics = parity.per_trade_metrics(trades)

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
        "gross_return_pct": gross_return_pct,
        "fee_drag_pct": gross_return_pct - total_return_pct,
        "total_fee_pct": sum(t.fee_pct for t in trades),
        "max_drawdown_pct": max_dd,
        "avg_pnl_pct": trade_metrics["avg_trade_pct"],
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "profit_factor": profit_factor,
        "avg_hold_minutes": avg_hold,
        "reason_counts": reason_counts,
        "by_reason": by_reason,
        "equity_curve": equity_curve,
        "initial_equity": initial,
        "realized_equity": realized_equity,
        "final_equity": final_equity,
        "total_pnl_quote": final_equity - initial,
        "risk_events": result.risk_events or {},
        "chase_skips": result.chase_skips,
        "entry_rule_scans": result.entry_rule_scans,
        "entry_rule_skips": result.entry_rule_skips,
        "same_coin_skips": result.same_coin_skips,
        **trade_metrics,
    }


def _cast_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if v in (0, 1):
            return bool(v)
        raise ValueError(f"Nilai boolean angka harus 0 atau 1, bukan {v!r}")
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("1", "true", "yes", "ya", "on"):
            return True
        if low in ("0", "false", "no", "tidak", "off"):
            return False
    raise ValueError(f"Nilai boolean tidak dikenal: {v!r}")


OVERRIDE_CASTERS = {
    "MIN_QUOTE_VOLUME_USDT_24H": float,
    "CONFIRM_INTERVAL": str,
    "CONFIRM_LOOKBACK_BARS": int,
    "ENTRY_RULE_ENABLED": _cast_bool,
    "ENTRY_BULL_CANDLES": int,
    "ENTRY_SL_ANCHOR": str,
    "ENTRY_SL_BUFFER_PCT": float,
    "ENTRY_MIN_RR_RATIO": float,
    "ENTRY_MAX_SL_PCT": float,
    "EXIT_TRAILING_PCT": float,
    "TAKER_FEE_PCT": float,
    "USE_BNB_FEE_DISCOUNT": _cast_bool,
    "USE_RISK_PERCENT": _cast_bool,
    "RISK_PERCENT": float,
    "POSITION_SIZE_USDT": float,
    "MAX_POSITION_USDT": float,
    "BALANCE_BUFFER_PCT": float,
    "BACKTEST_INITIAL_EQUITY_USDT": float,
    "BACKTEST_ENTRY_SPREAD_PCT": float,
    "BACKTEST_SLIPPAGE_PCT": float,
    "BACKTEST_ENTRY_DELAY_BARS": int,
    "USE_EQUITY_STOP": _cast_bool,
    "MAX_DRAWDOWN_PERCENT": float,
    "CLOSE_ALL_AT_LIMIT": _cast_bool,
    "DD_COOLDOWN_HOURS": float,
    "COOLDOWN_MINUTES_AFTER_CLOSE": float,
    "MIN_SECONDS_BETWEEN_TRADES": float,
    "SAME_COIN_BLOCK_HOURS": float,
    "SAME_COIN_BLOCK_LOSS_ONLY": _cast_bool,
    "MAX_CHASE_PCT": float,
}


def apply_overrides(base_config: dict, overrides: Optional[dict]) -> dict:
    cfg = copy.deepcopy(base_config)
    if not overrides:
        return cfg
    for key, val in overrides.items():
        if key not in OVERRIDE_CASTERS or val is None or val == "":
            continue
        caster = OVERRIDE_CASTERS[key]
        try:
            cast_val = caster(val)
        except (ValueError, TypeError) as e:
            raise BacktestError(f"Nilai parameter '{key}' tidak valid: {val}") from e
        if isinstance(cast_val, float) and not math.isfinite(cast_val):
            raise BacktestError(
                f"Nilai parameter '{key}' harus angka berhingga: {val}"
            )
        if caster is int and isinstance(val, str) and "." in val.strip():
            raise BacktestError(
                f"Nilai parameter '{key}' harus bilangan bulat: {val}"
            )
        if caster is int and isinstance(val, float) and not val.is_integer():
            raise BacktestError(
                f"Nilai parameter '{key}' harus bilangan bulat: {val}"
            )
        cfg[key] = cast_val
    return cfg


def _rentang_schema(key: str, lo: float, hi: float) -> tuple:
    try:
        from config.settings_schema import PARAMETER_SCHEMA

        spec = PARAMETER_SCHEMA.get(key) or {}
        smin, smax = spec.get("min"), spec.get("max")
        if isinstance(smin, (int, float)) and isinstance(smax, (int, float)):
            return float(smin), float(smax)
    except Exception:
        pass
    return float(lo), float(hi)


def validate_params(config: dict) -> None:
    checks = [
        ("ENTRY_SL_BUFFER_PCT", 0.0, 10.0),
        ("ENTRY_MIN_RR_RATIO", 0.1, 20.0),
        ("ENTRY_MAX_SL_PCT", 0.0, 50.0),
        (
            "EXIT_TRAILING_PCT",
            entry_rule.EXIT_TRAILING_MIN_PCT,
            entry_rule.EXIT_TRAILING_MAX_PCT,
        ),
    ]
    for key, lo, hi in checks:
        lo, hi = _rentang_schema(key, lo, hi)
        val = config.get(key)
        if val is None or not (lo <= float(val) <= hi):
            raise BacktestError(
                f"Parameter '{key}'={val} di luar rentang wajar ({lo:g}..{hi:g})."
            )

    bull = config.get("ENTRY_BULL_CANDLES")
    if bull is None or not (2 <= int(bull) <= 10):
        raise BacktestError(
            f"Parameter 'ENTRY_BULL_CANDLES'={bull} harus di antara 2 dan 10."
        )
    anchor = str(config.get("ENTRY_SL_ANCHOR") or "").strip().upper()
    if anchor not in ("SIGNAL", "SEQUENCE"):
        raise BacktestError(
            f"Parameter 'ENTRY_SL_ANCHOR' hanya menerima SIGNAL atau SEQUENCE, bukan {anchor!r}."
        )

    interval = config.get("CONFIRM_INTERVAL", "5m")
    if interval not in INTERVAL_MINUTES:
        raise BacktestError(f"Interval '{interval}' tidak dikenal.")

    try:
        strategy.max_hold_ms(config, str(interval))
    except ValueError as exc:
        raise BacktestError(str(exc)) from exc

    from config.settings_schema import relation_violations

    pelanggaran = relation_violations(config, mode_aware=True)
    if pelanggaran:
        daftar = "; ".join(f"{kunci}: {pesan}" for kunci, pesan in pelanggaran.items())
        raise BacktestError(f"Kombinasi parameter tidak konsisten: {daftar}")


def execute_backtest(
    client,
    symbol: str,
    days: float,
    base_config: dict,
    overrides: Optional[dict] = None,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    sleep_between_calls: float = 0.2,
    use_cache: Optional[bool] = None,
) -> dict:
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise BacktestError("Simbol tidak boleh kosong.")
    if days <= 0 or days > 1825:
        raise BacktestError("Rentang hari harus antara 0.5 sampai 1825 (5 tahun).")

    config = apply_overrides(base_config, overrides)
    config["_symbol"] = symbol
    validate_params(config)

    interval = config.get("CONFIRM_INTERVAL", "5m")
    bar_minutes = INTERVAL_MINUTES[interval]
    window_24h = bars_per_day(interval)
    warmup_bars = max(
        window_24h,
        strategy.confirm_window_bars(config),
        strategy.gate_warmup_bars(config, interval),
    )
    warmup_ms = warmup_bars * bar_minutes * MS_PER_MIN

    end_ms = last_completed_candle_end_ms(interval)
    start_ms = end_ms - int(days * MS_PER_DAY) - warmup_ms

    cache_enabled = (
        bool(config.get("BACKTEST_CACHE_ENABLED", True))
        if use_cache is None
        else bool(use_cache)
    )
    cache_obj = None
    if cache_enabled:
        try:
            from backtesting.backtest_cache import KlineCache

            cache_path = str(config.get("BACKTEST_CACHE_FILE", "") or "").strip()
            if cache_path:
                cache_obj = KlineCache(cache_path)
        except Exception:
            cache_obj = None

    def _fetch_with_cache(sym: str, itv: str, s_ms: int, e_ms: int, cb=None) -> list[Kline]:
        if cache_obj is None:
            return fetch_full_klines(
                client,
                sym,
                itv,
                s_ms,
                e_ms,
                progress_cb=cb,
                sleep_between_calls=sleep_between_calls,
            )
        missing = cache_obj.missing_ranges(sym, itv, s_ms, e_ms)
        for m_start, m_end in missing:
            bagian = fetch_full_klines(
                client,
                sym,
                itv,
                m_start,
                m_end,
                progress_cb=cb,
                sleep_between_calls=sleep_between_calls,
            )
            cache_obj.put(sym, itv, bagian, m_start, m_end)
        return cache_obj.read(sym, itv, s_ms, e_ms)

    def fetch_cb(p: float):
        if progress_cb:
            progress_cb(round(p * 0.75, 3), f"Mengunduh data {symbol} ({int(p*100)}%)")

    if progress_cb:
        progress_cb(0.02, f"Mengunduh data historis {symbol} ({interval})...")

    cache_meta = None
    try:
        klines = _fetch_with_cache(symbol, interval, start_ms, end_ms, cb=fetch_cb)

        min_needed = warmup_bars + 10
        if len(klines) < min_needed:
            raise BacktestError(
                f"Data {symbol} tidak cukup: hanya {len(klines)} candle "
                f"(butuh minimal {min_needed}). Coba kurangi rentang hari atau pilih koin lain."
            )

        if cache_obj is not None:
            cache_meta = cache_obj.stats()
    finally:
        if cache_obj is not None:
            cache_obj.close()

    def sim_cb(p: float):
        if progress_cb:
            progress_cb(
                round(0.78 + p * 0.20, 3), f"Menjalankan simulasi ({int(p*100)}%)"
            )

    if progress_cb:
        progress_cb(0.78, "Menjalankan simulasi strategi...")

    result = run_backtest(
        klines,
        config,
        warmup_bars=warmup_bars,
        progress_cb=sim_cb,
    )
    summary = summarize(result)

    if progress_cb:
        progress_cb(1.0, "Selesai")

    trades_out = [
        {
            "n": idx,
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
            "position_notional": round(t.position_notional, 2),
            "equity_before": round(t.equity_before, 2),
            "equity_after": round(t.equity_after, 2),
            "pnl_quote": round(t.pnl_quote, 2),
        }
        for idx, t in enumerate(result.trades, start=1)
    ]

    applied_params = {k: config.get(k) for k in OVERRIDE_CASTERS.keys()}

    spread_pct, slip_pct, delay_bars = entry_execution_params(config)
    assumptions = [
        "Simulasi 1 koin mengikuti aturan entry candle bullish beruntun dan logika exit yang sama dengan bot.",
        (
            f"Eksekusi entry menunggu {delay_bars} bar setelah sinyal lalu membeli pada harga open + "
            f"setengah spread ({spread_pct / 2:.3f}%) + slippage ({slippage_label(slip_pct)}%). "
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
        "Untuk menguji pemilihan top gainer lintas seluruh pasar sekaligus, gunakan mode Backtest Portofolio.",
    ]
    if result.warnings:
        assumptions.extend(result.warnings)

    return {
        "symbol": symbol,
        "interval": interval,
        "days": days,
        "bars_total": result.bars_total,
        "bars_usable": result.bars_usable,
        "start_time": result.start_time,
        "end_time": result.end_time,
        "summary": summary,
        "trades": trades_out,
        "params": applied_params,
        "assumptions": assumptions,
        "warnings": result.warnings,
        "cache": cache_meta,
    }


def slippage_label(slip_pct: float) -> str:
    return f"{float(slip_pct):.3f}"


def _fmt_ts(ms: int) -> str:
    if not ms:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


def print_report(data: dict) -> None:
    s = data["summary"]
    p = data["params"]
    print("=" * 72)
    print(
        f"BACKTEST REPORT : {data['symbol']}  |  interval {data['interval']}  |  {data['days']} hari"
    )
    print(
        f"Periode data    : {_fmt_ts(data['start_time'])}  s/d  {_fmt_ts(data['end_time'])}  "
        f"({data['bars_usable']} candle diuji)"
    )
    print("-" * 72)
    exit_info = (
        f"SL buffer={p.get('ENTRY_SL_BUFFER_PCT', 0.3)}% "
        f"Min R:R=1:{p.get('ENTRY_MIN_RR_RATIO', 2.0)} "
        f"MaxHold={strategy.exit_fixed_params()['max_hold_hours']:g}h"
    )
    entry_info = (
        f"{p.get('ENTRY_BULL_CANDLES', 3)} candle bullish di {p.get('CONFIRM_INTERVAL', '5m')} "
        f"(SL anchor {p.get('ENTRY_SL_ANCHOR', 'SIGNAL')})"
        if p.get("ENTRY_RULE_ENABLED", True)
        else "OFF"
    )
    print(
        f"Parameter utama : Entry={entry_info}  |  Exit: {exit_info}"
    )
    print("-" * 72)
    print(f"Total trade     : {s['total_trades']}")
    if s["total_trades"] == 0:
        print("Tidak ada sinyal entry yang terpicu pada periode ini.")
        print("=" * 72)
        return
    print(
        f"Menang / Kalah  : {s['wins']} / {s['losses']}   (Win rate: {s['win_rate']}%)"
    )
    print(f"Total return    : {s['total_return_pct']:+.2f}% (kompon, sudah potong fee)")
    print(
        f"Rata-rata PnL   : {s['avg_pnl_pct']:+.3f}%   "
        f"(menang: {s['avg_win_pct']:+.3f}% | kalah: {s['avg_loss_pct']:+.3f}%)"
    )
    print(
        f"Trade terbaik   : {s['best_trade_pct']:+.3f}%   |   terburuk: {s['worst_trade_pct']:+.3f}%"
    )
    pf = s["profit_factor"] if s["profit_factor"] is not None else "inf"
    print(
        f"Profit factor   : {pf}   |   Max drawdown: {s['max_drawdown_pct']:.2f}%   "
        f"|   Rata-rata tahan: {s['avg_hold_minutes']} menit"
    )
    print("-" * 72)
    print("Rincian per alasan exit:")
    for reason, info in s["by_reason"].items():
        print(
            f"  {reason:<16} : {info['count']:>4} trade  |  "
            f"winrate {info['win_rate']:>5.1f}%  |  avg PnL {info['avg_pnl_pct']:+.3f}%"
        )
    print("-" * 72)
    last_trades = data["trades"][-10:]
    print(f"{len(last_trades)} trade terakhir:")
    for t in last_trades:
        print(
            f"  #{t['n']:<3} {_fmt_ts(t['entry_time'])} -> {_fmt_ts(t['exit_time'])}  "
            f"in={t['entry_price']:.6g} out={t['exit_price']:.6g}  "
            f"{t['pnl_pct']:+6.2f}%  [{t['reason']}] ({t['hold_minutes']}m)"
        )
    print("=" * 72)


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
    if risk_info or summary.get("chase_skips") or summary.get("entry_rule_scans"):
        print(
            f"Kontrol akun/filter: {risk_info}, chase dilewati {summary.get('chase_skips', 0)}, "
            f"aturan entry menolak {summary.get('entry_rule_skips', 0)} dari "
            f"{summary.get('entry_rule_scans', 0)} evaluasi"
        )
    if result.warnings:
        print("Peringatan:")
        for item in result.warnings:
            print(f"  - {item}")
    print("=" * 72 + "\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Backtest strategi Pump Scanner pada data historis Binance."
    )
    parser.add_argument(
        "--symbol", default="BTCUSDT", help="Simbol pasangan, contoh: SOLUSDT, PEPEUSDT"
    )
    parser.add_argument(
        "--days", type=float, default=14, help="Jumlah hari ke belakang (default: 14)"
    )
    parser.add_argument(
        "--interval",
        default=None,
        help="Override CONFIRM_INTERVAL (1m/3m/5m/15m/1h)",
    )
    parser.add_argument(
        "--sl-buffer",
        type=float,
        default=None,
        help="Override ENTRY_SL_BUFFER_PCT",
    )
    parser.add_argument(
        "--min-rr",
        type=float,
        default=None,
        help="Override ENTRY_MIN_RR_RATIO",
    )
    parser.add_argument(
        "--bull-candles",
        type=int,
        default=None,
        help="Override ENTRY_BULL_CANDLES (2-10)",
    )
    parser.add_argument(
        "--grid",
        metavar="SPEC",
        default=None,
        help="Jalankan grid search.",
    )
    parser.add_argument(
        "--grid-metric",
        default="total_return_pct",
        help="Metrik peringkat grid search.",
    )
    parser.add_argument(
        "--grid-train",
        type=float,
        default=0.7,
        help="Porsi data untuk periode latih (0.7 = 70 persen).",
    )
    parser.add_argument(
        "--grid-min-trades",
        type=int,
        default=10,
        help="Di bawah jumlah trade ini hasil ditandai tidak andal.",
    )
    parser.add_argument(
        "--grid-top",
        type=int,
        default=15,
        help="Jumlah baris teratas yang dicetak.",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Jalankan pemeriksaan aturan entry dan exit tanpa menyentuh Binance.",
    )
    args = parser.parse_args()

    if args.selftest:
        from backtesting.entry_selftest import run as _run_entry_selftest
        from backtesting.exit_selftest import run as _run_exit_selftest

        entry_ok, _ = _run_entry_selftest(verbose=True)
        exit_ok, _ = _run_exit_selftest(verbose=True)
        raise SystemExit(0 if entry_ok and exit_ok else 1)

    from trading.clients.binance_client import BinanceSpotClient
    from config.config import PUMP_CONFIG, get_base_url

    overrides = {}
    if args.interval:
        overrides["CONFIRM_INTERVAL"] = args.interval
    if args.sl_buffer is not None:
        overrides["ENTRY_SL_BUFFER_PCT"] = args.sl_buffer
    if args.min_rr is not None:
        overrides["ENTRY_MIN_RR_RATIO"] = args.min_rr
    if args.bull_candles is not None:
        overrides["ENTRY_BULL_CANDLES"] = args.bull_candles

    client = BinanceSpotClient(
        "",
        "",
        get_base_url(PUMP_CONFIG),
        allow_signed=False,
        rate_limit_state_file=PUMP_CONFIG.get("RATE_LIMIT_STATE_FILE"),
        rate_limit_limit=int(
            PUMP_CONFIG.get("RATE_LIMIT_WEIGHT_LIMIT", 6000) or 6000
        ),
        rate_limit_safety_margin=int(
            PUMP_CONFIG.get("RATE_LIMIT_SAFETY_MARGIN", 100) or 100
        ),
    )

    def cli_progress(p: float, msg: str):
        print(f"\r[{int(p*100):>3}%] {msg:<50}", end="", flush=True)

    if args.grid:
        from backtesting.grid_search import (
            GridSearchError,
            cetak_tabel,
            execute_grid_search,
            parse_spec_cli,
        )

        try:
            spec = parse_spec_cli(args.grid)
            hasil = execute_grid_search(
                client,
                mode="single",
                symbol=args.symbol,
                days=args.days,
                base_config=PUMP_CONFIG,
                spec=spec,
                base_overrides=overrides,
                metrik=args.grid_metric,
                rasio_latih=args.grid_train,
                min_trades=args.grid_min_trades,
                progress_cb=cli_progress,
            )
        except (GridSearchError, BacktestError) as exc:
            print(f"\nGrid search gagal: {exc}")
            raise SystemExit(2) from exc
        print()
        cetak_tabel(hasil, top_n=args.grid_top)
        return

    try:
        data = execute_backtest(
            client, args.symbol, args.days, PUMP_CONFIG, overrides, cli_progress
        )
        print()
        print_report(data)
    except BacktestError as e:
        print(f"\nGagal menjalankan backtest: {e}")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
