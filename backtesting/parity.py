from __future__ import annotations

import math
from bisect import bisect_right
from typing import Optional, Sequence

from strategy.indicators import (
    MAX_HOLD_HOURS_LIMIT,
    TIME_STOP_REASON,
    Kline,
    max_hold_ms,
    time_stop_due,
)

MS_PER_MIN = 60_000

RISK_LIMIT_REASON = "RISK_LIMIT_TRIGGERED"


class PositionState:
    __slots__ = (
        "entry_price",
        "levels",
        "be_active",
        "be_stop",
        "trailing_active",
        "trailing_stop",
        "entry_time",
    )

    def __init__(
        self, entry_price: float, levels: dict, entry_time: int = 0
    ) -> None:
        self.entry_price = float(entry_price)
        # Waktu entry dalam ms epoch. 0 berarti tidak diketahui, sehingga time
        # stop otomatis nonaktif untuk posisi itu (perilaku lama tetap aman).
        self.entry_time = int(entry_time or 0)
        self.levels = {
            "sl": float(levels["sl_pct"]),
            "tp": float(levels["tp_pct"]),
            "be_trig": float(levels["be_trigger_pct"]),
            "be_lock": float(levels["be_lock_pct"]),
            "tr_start": float(levels["trail_start_pct"]),
            "tr_step": float(levels["trail_step_pct"]),
            "src": str(levels["source"]),
        }
        self.be_active = False
        self.be_stop = 0.0
        self.trailing_active = False
        self.trailing_stop = 0.0


def evaluate_candle_exit(
    pos: PositionState,
    candle: Kline,
    config: dict,
    cap_ms: int = 0,
    bar_ms: int = 0,
) -> Optional[tuple[str, float]]:
    entry = pos.entry_price
    lv = pos.levels
    atr_mode = lv["src"] == "ATR"
    use_sl = bool(config["USE_STOP_LOSS"])
    sl_price = (entry - lv["sl"]) if atr_mode else entry * (1 - lv["sl"] / 100.0)

    carried_level = None
    carried_reason = None
    if pos.be_active:
        carried_level, carried_reason = pos.be_stop, "BREAKEVEN"
    if pos.trailing_active and (
        carried_level is None or pos.trailing_stop > carried_level
    ):
        carried_level, carried_reason = pos.trailing_stop, "TRAILING_STOP"
    gap_below_sl = use_sl and candle.open <= sl_price
    if carried_level is not None and not gap_below_sl and candle.low <= carried_level:
        return carried_reason, min(carried_level, candle.open)

    pnl_high = (candle.high / entry - 1.0) * 100.0
    pnl_low = (candle.low / entry - 1.0) * 100.0
    pnl_high_unit = candle.high - entry if atr_mode else pnl_high

    if config["USE_BREAKEVEN"] and not pos.be_active and pnl_high_unit >= lv["be_trig"]:
        pos.be_active = True
        pos.be_stop = (
            entry + lv["be_lock"] if atr_mode else entry * (1 + lv["be_lock"] / 100.0)
        )
    if config["USE_TRAILING"]:
        cand_stop = (
            candle.high - lv["tr_step"]
            if atr_mode
            else candle.high * (1 - lv["tr_step"] / 100.0)
        )
        if not pos.trailing_active and pnl_high_unit >= lv["tr_start"]:
            pos.trailing_active = True
            pos.trailing_stop = cand_stop
        elif pos.trailing_active and cand_stop > pos.trailing_stop:
            pos.trailing_stop = cand_stop

    sl_triggered = (candle.low <= sl_price) if atr_mode else (pnl_low <= -lv["sl"])
    if use_sl and sl_triggered:
        return "STOP_LOSS", min(sl_price, candle.open)
    tp_price = entry + lv["tp"] if atr_mode else entry * (1 + lv["tp"] / 100.0)
    tp_hit = (candle.high >= tp_price) if atr_mode else (pnl_high >= lv["tp"])
    if config["USE_TP"] and tp_hit:
        return "TAKE_PROFIT", max(tp_price, candle.open)
    if pos.be_active and candle.low <= pos.be_stop:
        return "BREAKEVEN", min(pos.be_stop, candle.open)
    if pos.trailing_active and candle.low <= pos.trailing_stop:
        return "TRAILING_STOP", min(pos.trailing_stop, candle.open)
    # TIME STOP dievaluasi paling akhir: SL/BE/TRAIL tetap menang pada bar yang
    # sama, jadi hierarki exit yang lama tidak berubah sama sekali. Exit memakai
    # harga close bar ini karena alasan penutupan adalah waktu, bukan level harga.
    if time_stop_due(pos.entry_time, candle.close_time, cap_ms, bar_ms):
        return TIME_STOP_REASON, candle.close
    return None


class AccountRiskControls:

    def __init__(self, config: dict, initial_equity: float) -> None:
        self.use_equity_stop = bool(config.get("USE_EQUITY_STOP", False))
        self.max_drawdown_pct = float(config.get("MAX_DRAWDOWN_PERCENT", 0.0) or 0.0)
        self.dd_cooldown_ms = (
            float(config.get("DD_COOLDOWN_HOURS", 0.0) or 0.0) * 3_600_000
        )
        self.close_all_at_limit = bool(config.get("CLOSE_ALL_AT_LIMIT", False))

        self.peak_equity: Optional[float] = float(initial_equity)
        self.dd_stopped = False
        self.dd_stop_until = 0.0
        self._limit_close_done = False
        self.events = {
            "dd_stop": 0,
            "forced_close": 0,
        }

    def update(self, now_ms: int, equity: float) -> bool:
        equity = float(equity)

        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity

        if (
            self.use_equity_stop
            and self.max_drawdown_pct > 0
            and not self.dd_stopped
            and self.peak_equity
        ):
            dd_pct = (self.peak_equity - equity) / self.peak_equity * 100.0
            if dd_pct >= self.max_drawdown_pct:
                self.dd_stopped = True
                self.dd_stop_until = now_ms + self.dd_cooldown_ms
                self.events["dd_stop"] += 1

        if self.dd_stopped:
            if (
                not self.use_equity_stop
                or not self.dd_stop_until
                or now_ms >= self.dd_stop_until
            ):
                self.dd_stopped = False
                self.dd_stop_until = 0.0
                self.peak_equity = equity

        return bool(self.dd_stopped)

    def force_close_due(self, entries_paused: bool, in_position: bool) -> bool:
        limit_now = bool(self.dd_stopped)
        due = False
        if (
            self.close_all_at_limit
            and entries_paused
            and limit_now
            and not self._limit_close_done
            and in_position
        ):
            self._limit_close_done = True
            self.events["forced_close"] += 1
            due = True
        if not entries_paused and self._limit_close_done:
            self._limit_close_done = False
        return due


class BtcDropLookup:

    def __init__(
        self, klines: Sequence[Kline], bar_ms: int, lookback_bars: int
    ) -> None:
        self._klines = sorted(klines, key=lambda k: int(k.open_time))
        self._index = {int(k.open_time): i for i, k in enumerate(self._klines)}
        self._bar_ms = int(bar_ms)
        self._look = max(1, int(lookback_bars))

    def drop_pct_at(self, open_time: int) -> Optional[float]:
        idx = self._index.get(int(open_time))
        if idx is None or idx < self._look:
            return None
        prev = self._klines[idx - self._look]
        if int(prev.open_time) != int(open_time) - self._look * self._bar_ms:
            return None
        if prev.close <= 0:
            return None
        return (self._klines[idx].close / prev.close - 1.0) * 100.0


def make_btc_lookup(
    btc_klines: Optional[Sequence[Kline]], config: dict, bar_ms: int
) -> Optional[BtcDropLookup]:
    if not config.get("BTC_FILTER_ENABLED", False) or not btc_klines:
        return None
    return BtcDropLookup(
        btc_klines, bar_ms, int(config.get("BTC_LOOKBACK_BARS", 3) or 3)
    )


def btc_filter_warning(config: dict, lookup: Optional[BtcDropLookup]) -> Optional[str]:
    if config.get("BTC_FILTER_ENABLED", False) and lookup is None:
        return (
            "BTC_FILTER_ENABLED aktif di config, tetapi candle BTCUSDT tidak "
            "diberikan ke simulasi ini, sehingga filter BTC TIDAK diterapkan "
            "(bot live menerapkannya)."
        )
    return None


def gate_config(config: dict, lookup: Optional[BtcDropLookup]) -> dict:
    if lookup is None:
        return config
    out = dict(config)
    out["_btc_filter_fail_closed"] = True
    return out


def chase_exceeded(
    exec_price: float, signal_close: Optional[float], max_chase_pct: float
) -> bool:
    if not max_chase_pct or max_chase_pct <= 0:
        return False
    if not signal_close or signal_close <= 0:
        return False
    return float(exec_price) > float(signal_close) * (
        1.0 + float(max_chase_pct) / 100.0
    )


def next_entry_allowed(close_time_ms: int, config: dict) -> int:
    cooldown_ms = int(config.get("COOLDOWN_MINUTES_AFTER_CLOSE", 0) or 0) * MS_PER_MIN
    spacing_ms = int(float(config.get("MIN_SECONDS_BETWEEN_TRADES", 0) or 0) * 1000)
    return int(close_time_ms) + max(cooldown_ms, spacing_ms)


def same_coin_block_hours(config: dict) -> float:
    try:
        hours = float(config.get("SAME_COIN_BLOCK_HOURS", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(hours) or hours <= 0:
        return 0.0
    return hours


def same_coin_block_loss_only(config: dict) -> bool:
    return bool(config.get("SAME_COIN_BLOCK_LOSS_ONLY", True))


def same_coin_block_until_ms(close_time_ms: int, pnl_quote: float, config: dict) -> int:
    hours = same_coin_block_hours(config)
    close_time_ms = int(close_time_ms)
    if hours <= 0:
        return close_time_ms
    try:
        hasil = float(pnl_quote)
    except (TypeError, ValueError):
        hasil = float("-inf")
    if not math.isfinite(hasil):
        hasil = -1.0 if hasil < 0 else 1.0
    if same_coin_block_loss_only(config) and hasil >= 0:
        return close_time_ms
    return close_time_ms + int(hours * 3_600_000)


def same_coin_blocked(symbol: str, now_ms: int, blocks: dict) -> bool:
    if not isinstance(blocks, dict) or not symbol:
        return False
    try:
        until = float(blocks.get(str(symbol).upper(), 0) or 0)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(until):
        return False
    return until > int(now_ms)


def same_coin_block_record(
    blocks: dict, symbol: str, close_time_ms: int, pnl_quote: float, config: dict
) -> None:
    if not symbol or not isinstance(blocks, dict):
        return
    close_time_ms = int(close_time_ms)
    until = same_coin_block_until_ms(close_time_ms, pnl_quote, config)
    if until <= close_time_ms:
        return
    kedaluwarsa = []
    for sym, nilai in blocks.items():
        try:
            batas = float(nilai or 0)
        except (TypeError, ValueError):
            kedaluwarsa.append(sym)
            continue
        if not math.isfinite(batas) or batas <= close_time_ms:
            kedaluwarsa.append(sym)
    for sym in kedaluwarsa:
        blocks.pop(sym, None)
    blocks[str(symbol).upper()] = until


class TrendLookup:

    def __init__(self, trend_klines: Sequence[Kline], config: dict) -> None:
        from strategy import indicators as strategy_mod

        self.config = config
        self.interval = (
            strategy_mod.trend_interval(config) if _interval_ok(config) else None
        )
        self.window = strategy_mod.htf_window_bars(config)
        self.required = strategy_mod.trend_required_bars(config)
        self.klines = list(trend_klines)
        self.close_times = [int(k.close_time) for k in self.klines]

    def window_at(self, signal_close_time_ms: int) -> list:
        idx = bisect_right(self.close_times, int(signal_close_time_ms))
        return self.klines[max(0, idx - self.window) : idx]

    def verdict_at(self, signal_close_time_ms: int) -> dict:
        return evaluate_trend(self.window_at(signal_close_time_ms), self.config)

    def htf_demand_at(self, signal_close_time_ms: int) -> dict:
        from strategy import indicators as strategy_mod

        return strategy_mod.evaluate_htf_demand(
            self.window_at(signal_close_time_ms), self.config
        )


def _interval_ok(config: dict) -> bool:
    from strategy import indicators as strategy_mod

    raw = str(config.get("TREND_INTERVAL", "1h") or "1h").strip().lower()
    return raw in strategy_mod.INTERVAL_MINUTES


def build_trend_klines(klines: Sequence[Kline], config: dict, interval: str) -> list:
    from strategy import indicators as strategy_mod

    trend_minutes = strategy_mod.trend_interval_minutes(config)
    source_minutes = strategy_mod.INTERVAL_MINUTES.get(str(interval))
    if source_minutes is None:
        raise ValueError(
            f"Interval simulasi '{interval}' tidak dikenal sehingga candle trend "
            f"'{strategy_mod.trend_interval(config)}' tidak bisa dirangkai."
        )
    return strategy_mod.aggregate_klines(list(klines), trend_minutes, source_minutes)


def make_trend_lookup(
    klines: Sequence[Kline],
    config: dict,
    interval: str,
    *,
    sudah_dirangkai: bool = False,
) -> Optional[TrendLookup]:
    if not config.get("TREND_FILTER_ENABLED", False) and not config.get(
        "HTF_DEMAND_FILTER_ENABLED", False
    ):
        return None
    bars = (
        list(klines)
        if sudah_dirangkai
        else build_trend_klines(klines, config, interval)
    )
    return TrendLookup(bars, config)


def trend_warmup_bars(config: dict, interval: str) -> int:
    from strategy import indicators as strategy_mod

    return strategy_mod.trend_warmup_bars(config, interval)


def trend_warmup_ms(config: dict, interval: str) -> int:
    from strategy import indicators as strategy_mod

    return strategy_mod.trend_warmup_bars(
        config, interval
    ) * strategy_mod.interval_to_ms(interval)


def m15_demand_enabled(config: dict) -> bool:
    from strategy import indicators as strategy_mod

    return strategy_mod.m15_demand_enabled(config)


def m15_demand_window_bars(config: dict) -> int:
    from strategy import indicators as strategy_mod

    return strategy_mod.m15_demand_window_bars(config)


def m15_demand_lookback_bars(config: dict) -> int:
    from strategy import indicators as strategy_mod

    return strategy_mod.m15_demand_lookback_bars(config)


def m15_trend_window_bars(config: dict) -> int:
    from strategy import indicators as strategy_mod

    return strategy_mod.m15_trend_window_bars(config)


def m15_trend_warmup_ms(config: dict, interval: str) -> int:
    from strategy import indicators as strategy_mod

    return strategy_mod.m15_trend_warmup_bars(
        config, interval
    ) * strategy_mod.interval_to_ms(interval)


def htf_gate_warmup_bars(config: dict, interval: str) -> int:
    from strategy import indicators as strategy_mod

    return strategy_mod.htf_gate_warmup_bars(config, interval)


def htf_gate_warmup_ms(config: dict, interval: str) -> int:
    from strategy import indicators as strategy_mod

    return strategy_mod.htf_gate_warmup_bars(
        config, interval
    ) * strategy_mod.interval_to_ms(interval)


def evaluate_trend(klines: Sequence[Kline], config: dict) -> dict:
    from strategy import indicators as strategy_mod

    return strategy_mod.evaluate_trend_filter(list(klines), config)


class M15DemandLookup:

    def __init__(self, m15_klines: Sequence[Kline], config: dict) -> None:
        from strategy import indicators as strategy_mod

        self.config = config
        self.interval = strategy_mod.m15_demand_interval(config)
        self.window = strategy_mod.m15_demand_window_bars(config)
        self.klines = list(m15_klines)
        self.close_times = [int(k.close_time) for k in self.klines]

    def window_at(self, signal_close_time_ms: int) -> list:
        idx = bisect_right(self.close_times, int(signal_close_time_ms))
        return self.klines[max(0, idx - self.window) : idx]

    def m15_demand_at(self, signal_close_time_ms: int) -> dict:
        from strategy import indicators as strategy_mod

        return strategy_mod.evaluate_m15_demand(
            self.window_at(signal_close_time_ms), self.config
        )


def make_m15_lookup(
    klines: Sequence[Kline],
    config: dict,
    interval: str,
    *,
    sudah_dirangkai: bool = False,
) -> Optional[M15DemandLookup]:
    from strategy import indicators as strategy_mod

    if not strategy_mod.m15_demand_enabled(config):
        return None
    bars = (
        list(klines) if sudah_dirangkai else build_m15_klines(klines, config, interval)
    )
    return M15DemandLookup(bars, config)


def build_m15_klines(
    klines: Sequence[Kline], config: dict, interval: str
) -> list[Kline]:
    from strategy import indicators as strategy_mod

    source_minutes = strategy_mod.INTERVAL_MINUTES.get(str(interval))
    if source_minutes is None:
        raise ValueError(
            f"Interval simulasi '{interval}' tidak dikenal sehingga candle "
            f"'{strategy_mod.m15_demand_interval(config)}' tidak bisa dirangkai."
        )
    return strategy_mod.aggregate_klines(
        list(klines),
        strategy_mod.m15_demand_interval_minutes(config),
        source_minutes,
    )


class M15TrendLookup:

    def __init__(self, m15_klines: Sequence[Kline], config: dict) -> None:
        from strategy import indicators as strategy_mod

        self.config = config
        self.interval = strategy_mod.m15_trend_interval(config)
        self.window = strategy_mod.m15_trend_window_bars(config)
        self.klines = list(m15_klines)
        self.close_times = [int(k.close_time) for k in self.klines]

    def window_at(self, signal_close_time_ms: int) -> list:
        idx = bisect_right(self.close_times, int(signal_close_time_ms))
        return self.klines[max(0, idx - self.window) : idx]

    def verdict_at(self, signal_close_time_ms: int) -> dict:
        from strategy import indicators as strategy_mod

        return strategy_mod.evaluate_m15_trend(
            self.window_at(signal_close_time_ms), self.config
        )


def build_m15_trend_klines(
    klines: Sequence[Kline], config: dict, interval: str
) -> list[Kline]:
    from strategy import indicators as strategy_mod

    source_minutes = strategy_mod.INTERVAL_MINUTES.get(str(interval))
    if source_minutes is None:
        raise ValueError(
            f"Interval simulasi '{interval}' tidak dikenal sehingga candle "
            f"'{strategy_mod.m15_trend_interval(config)}' tidak bisa dirangkai."
        )
    return strategy_mod.aggregate_klines(
        list(klines),
        strategy_mod.m15_trend_interval_minutes(config),
        source_minutes,
    )


def make_m15_trend_lookup(
    klines: Sequence[Kline],
    config: dict,
    interval: str,
    *,
    sudah_dirangkai: bool = False,
) -> Optional[M15TrendLookup]:
    from strategy import indicators as strategy_mod

    if not strategy_mod.m15_trend_enabled(config):
        return None
    bars = (
        list(klines)
        if sudah_dirangkai
        else build_m15_trend_klines(klines, config, interval)
    )
    return M15TrendLookup(bars, config)


def per_trade_metrics(trades: Sequence) -> dict:
    pcts = [float(t.pnl_pct) for t in trades]
    if not pcts:
        return {
            "avg_trade_pct": 0.0,
            "median_trade_pct": 0.0,
            "best_trade_pct": 0.0,
            "worst_trade_pct": 0.0,
            "sum_trade_pct": 0.0,
            "payoff_ratio": None,
            "max_consecutive_losses": 0,
        }
    ordered = sorted(pcts)
    mid = len(ordered) // 2
    median = (
        ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
    )
    wins = [p for p in pcts if p > 0]
    losses = [p for p in pcts if p <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    payoff = (avg_win / abs(avg_loss)) if (wins and losses and avg_loss != 0) else None
    streak = longest = 0
    for p in pcts:
        streak = streak + 1 if p <= 0 else 0
        longest = max(longest, streak)
    return {
        "avg_trade_pct": sum(pcts) / len(pcts),
        "median_trade_pct": median,
        "best_trade_pct": max(pcts),
        "worst_trade_pct": min(pcts),
        "sum_trade_pct": sum(pcts),
        "payoff_ratio": payoff if payoff is None or math.isfinite(payoff) else None,
        "max_consecutive_losses": longest,
    }
