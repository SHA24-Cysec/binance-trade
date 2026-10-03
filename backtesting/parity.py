from __future__ import annotations

import math
from typing import Optional, Sequence

from strategy.indicators import Kline

MS_PER_MIN = 60_000
MS_PER_DAY = 86_400_000

RISK_LIMIT_REASON = "RISK_LIMIT_TRIGGERED"


class PositionState:
    __slots__ = ("entry_price", "levels", "be_active", "be_stop",
                 "trailing_active", "trailing_stop")

    def __init__(self, entry_price: float, levels: dict) -> None:
        self.entry_price = float(entry_price)
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


def evaluate_candle_exit(pos: PositionState, candle: Kline,
                         config: dict) -> Optional[tuple[str, float]]:
    entry = pos.entry_price
    lv = pos.levels
    atr_mode = lv["src"] == "ATR"
    use_sl = bool(config["USE_STOP_LOSS"])
    sl_price = (entry - lv["sl"]) if atr_mode else entry * (1 - lv["sl"] / 100.0)

    carried_level = None
    carried_reason = None
    if pos.be_active:
        carried_level, carried_reason = pos.be_stop, "BREAKEVEN"
    if pos.trailing_active and (carried_level is None or pos.trailing_stop > carried_level):
        carried_level, carried_reason = pos.trailing_stop, "TRAILING_STOP"
    gap_below_sl = use_sl and candle.open <= sl_price
    if carried_level is not None and not gap_below_sl and candle.low <= carried_level:
        return carried_reason, min(carried_level, candle.open)

    pnl_high = (candle.high / entry - 1.0) * 100.0
    pnl_low = (candle.low / entry - 1.0) * 100.0
    pnl_high_unit = candle.high - entry if atr_mode else pnl_high

    if config["USE_BREAKEVEN"] and not pos.be_active and pnl_high_unit >= lv["be_trig"]:
        pos.be_active = True
        pos.be_stop = entry + lv["be_lock"] if atr_mode else entry * (1 + lv["be_lock"] / 100.0)
    if config["USE_TRAILING"]:
        cand_stop = (candle.high - lv["tr_step"] if atr_mode
                     else candle.high * (1 - lv["tr_step"] / 100.0))
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
    return None


class AccountRiskControls:

    def __init__(self, config: dict, initial_equity: float) -> None:
        self.use_equity_stop = bool(config.get("USE_EQUITY_STOP", False))
        self.max_drawdown_pct = float(config.get("MAX_DRAWDOWN_PERCENT", 0.0) or 0.0)
        self.dd_cooldown_ms = float(config.get("DD_COOLDOWN_HOURS", 0.0) or 0.0) * 3_600_000
        self.use_daily_stop = bool(config.get("USE_DAILY_STOP", False))
        self.max_daily_loss_pct = float(config.get("MAX_DAILY_LOSS_PERCENT", 0.0) or 0.0)
        self.daily_profit_target_pct = float(
            config.get("DAILY_PROFIT_TARGET_PERCENT", 0.0) or 0.0)
        self.close_all_at_limit = bool(config.get("CLOSE_ALL_AT_LIMIT", False))

        self.peak_equity: Optional[float] = float(initial_equity)
        self.day_index: Optional[int] = None
        self.day_start_equity: Optional[float] = None
        self.dd_stopped = False
        self.dd_stop_until = 0.0
        self.daily_stopped = False
        self.daily_stop_source: Optional[str] = None
        self._limit_close_done = False
        self.events = {"dd_stop": 0, "daily_loss_stop": 0,
                       "daily_profit_stop": 0, "forced_close": 0}

    def update(self, now_ms: int, equity: float) -> bool:
        equity = float(equity)
        day = int(now_ms) // MS_PER_DAY
        if self.day_index != day:
            self.day_index = day
            self.day_start_equity = equity
            self.daily_stopped = False
            self.daily_stop_source = None

        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity

        if (self.use_equity_stop and self.max_drawdown_pct > 0
                and not self.dd_stopped and self.peak_equity):
            dd_pct = (self.peak_equity - equity) / self.peak_equity * 100.0
            if dd_pct >= self.max_drawdown_pct:
                self.dd_stopped = True
                self.dd_stop_until = now_ms + self.dd_cooldown_ms
                self.events["dd_stop"] += 1

        if self.dd_stopped:
            if not self.use_equity_stop or not self.dd_stop_until or now_ms >= self.dd_stop_until:
                self.dd_stopped = False
                self.dd_stop_until = 0.0
                self.peak_equity = equity

        if self.use_daily_stop and not self.daily_stopped and self.day_start_equity:
            change_pct = (equity - self.day_start_equity) / self.day_start_equity * 100.0
            if self.max_daily_loss_pct > 0 and change_pct <= -self.max_daily_loss_pct:
                self.daily_stopped = True
                self.daily_stop_source = "LOSS"
                self.events["daily_loss_stop"] += 1
            elif self.daily_profit_target_pct > 0 and change_pct >= self.daily_profit_target_pct:
                self.daily_stopped = True
                self.daily_stop_source = "PROFIT"
                self.events["daily_profit_stop"] += 1

        return bool(self.dd_stopped or self.daily_stopped)

    def force_close_due(self, entries_paused: bool, in_position: bool) -> bool:
        profit_stop_only = (
            self.daily_stopped and not self.dd_stopped
            and str(self.daily_stop_source or "LOSS").upper() == "PROFIT"
        )
        limit_now = bool(self.dd_stopped or self.daily_stopped) and not profit_stop_only
        due = False
        if (self.close_all_at_limit and entries_paused and limit_now
                and not self._limit_close_done and in_position):
            self._limit_close_done = True
            self.events["forced_close"] += 1
            due = True
        if not entries_paused and self._limit_close_done:
            self._limit_close_done = False
        return due


class BtcDropLookup:

    def __init__(self, klines: Sequence[Kline], bar_ms: int, lookback_bars: int) -> None:
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


def make_btc_lookup(btc_klines: Optional[Sequence[Kline]], config: dict,
                    bar_ms: int) -> Optional[BtcDropLookup]:
    if not config.get("BTC_FILTER_ENABLED", False) or not btc_klines:
        return None
    return BtcDropLookup(btc_klines, bar_ms, int(config.get("BTC_LOOKBACK_BARS", 3) or 3))


def btc_filter_warning(config: dict, lookup: Optional[BtcDropLookup]) -> Optional[str]:
    if config.get("BTC_FILTER_ENABLED", False) and lookup is None:
        return ("BTC_FILTER_ENABLED aktif di config, tetapi candle BTCUSDT tidak "
                "diberikan ke simulasi ini, sehingga filter BTC TIDAK diterapkan "
                "(bot live menerapkannya).")
    return None


def gate_config(config: dict, lookup: Optional[BtcDropLookup]) -> dict:
    if lookup is None:
        return config
    out = dict(config)
    out["_btc_filter_fail_closed"] = True
    return out


def chase_exceeded(exec_price: float, signal_close: Optional[float],
                   max_chase_pct: float) -> bool:
    if not max_chase_pct or max_chase_pct <= 0:
        return False
    if not signal_close or signal_close <= 0:
        return False
    return float(exec_price) > float(signal_close) * (1.0 + float(max_chase_pct) / 100.0)


def next_entry_allowed(close_time_ms: int, config: dict) -> int:
    cooldown_ms = int(config.get("COOLDOWN_MINUTES_AFTER_CLOSE", 0) or 0) * MS_PER_MIN
    spacing_ms = int(float(config.get("MIN_SECONDS_BETWEEN_TRADES", 0) or 0) * 1000)
    return int(close_time_ms) + max(cooldown_ms, spacing_ms)


def per_trade_metrics(trades: Sequence) -> dict:
    pcts = [float(t.pnl_pct) for t in trades]
    if not pcts:
        return {"avg_trade_pct": 0.0, "median_trade_pct": 0.0, "best_trade_pct": 0.0,
                "worst_trade_pct": 0.0, "sum_trade_pct": 0.0, "payoff_ratio": None,
                "max_consecutive_losses": 0}
    ordered = sorted(pcts)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
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
