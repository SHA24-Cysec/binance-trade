from __future__ import annotations

import math
from typing import Optional, Sequence

from strategy import indicators as strategy
from strategy.indicators import Kline

TIME_STOP_REASON = strategy.TIME_STOP_REASON
RISK_LIMIT_REASON = "RISK_LIMIT_TRIGGERED"
MAX_HOLD_HOURS_LIMIT = strategy.MAX_HOLD_HOURS_LIMIT
MS_PER_MIN = 60_000


def max_hold_ms(config: dict, interval: str) -> int:
    return strategy.max_hold_ms(config, interval)


def chase_exceeded(
    exec_entry_price: float,
    signal_close: float,
    max_chase_pct: float,
) -> bool:
    if max_chase_pct <= 0 or signal_close <= 0:
        return False
    chase_pct = (exec_entry_price / signal_close - 1.0) * 100.0
    return chase_pct > max_chase_pct


def next_entry_allowed(exit_close_time_ms: int, config: dict) -> int:
    cooldown_ms = int(
        float(config.get("COOLDOWN_MINUTES_AFTER_CLOSE", 0) or 0) * 60_000
    )
    min_between_ms = int(
        float(config.get("MIN_SECONDS_BETWEEN_TRADES", 0) or 0) * 1000
    )
    wait_ms = max(cooldown_ms, min_between_ms)
    return int(exit_close_time_ms) + max(0, wait_ms)


def same_coin_block_hours(config: dict) -> float:
    raw = config.get("SAME_COIN_BLOCK_HOURS", 0)
    if raw is None or raw == "" or isinstance(raw, bool):
        return 0.0
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(hours) or hours <= 0:
        return 0.0
    return hours


def same_coin_block_loss_only(config: dict) -> bool:
    raw = config.get("SAME_COIN_BLOCK_LOSS_ONLY", True)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() not in ("false", "0", "no", "tidak", "off")
    return bool(raw)


def same_coin_blocked(
    symbol: str,
    now_ms: int,
    blocked_until: dict[str, int],
) -> bool:
    if not symbol:
        return False
    until = int(blocked_until.get(symbol, 0) or 0)
    return until > 0 and int(now_ms) < until


def same_coin_block_record(
    blocked_until: dict[str, int],
    symbol: str,
    exit_close_time_ms: int,
    pnl_quote: float,
    config: dict,
) -> None:
    if not symbol:
        return
    hours = same_coin_block_hours(config)
    if hours <= 0:
        blocked_until.pop(symbol, None)
        return
    if same_coin_block_loss_only(config) and float(pnl_quote) >= 0.0:
        blocked_until.pop(symbol, None)
        return
    blocked_until[symbol] = int(exit_close_time_ms) + int(hours * 3_600_000)


class AccountRiskControls:

    def __init__(self, config: dict, initial_equity: float) -> None:
        self.use_equity_stop = bool(config.get("USE_EQUITY_STOP", False))
        self.max_dd_pct = max(
            0.0, float(config.get("MAX_DRAWDOWN_PERCENT", 0.0) or 0.0)
        )
        self.close_all_at_limit = bool(config.get("CLOSE_ALL_AT_LIMIT", False))
        self.dd_cooldown_ms = int(
            max(0.0, float(config.get("DD_COOLDOWN_HOURS", 0) or 0)) * 3_600_000
        )
        self.peak_equity = max(0.0, float(initial_equity))
        self.max_drawdown_seen_pct = 0.0
        self.paused_until_ms = 0
        self.kill_switch_triggers = 0
        self.cooldown_R_resets = 0
        self.force_closes = 0
        self._in_pause = False
        self._limit_close_done = False
        self.events = {
            "dd_stop": 0,
            "forced_close": 0,
        }

    def drawdown_pct(self, equity: float) -> float:
        if self.peak_equity <= 0:
            return 0.0
        dd = (self.peak_equity - float(equity)) / self.peak_equity * 100.0
        return max(0.0, dd)

    def update(self, now_ms: int, equity: float) -> bool:
        now_ms = int(now_ms)
        eq = max(0.0, float(equity))

        if self._in_pause:
            if (
                not self.use_equity_stop
                or self.dd_cooldown_ms <= 0
                or now_ms >= self.paused_until_ms
            ):
                self._in_pause = False
                self.paused_until_ms = 0
                self.peak_equity = eq
                self.cooldown_R_resets += 1
            else:
                return True

        if eq > self.peak_equity:
            self.peak_equity = eq

        dd = self.drawdown_pct(eq)
        if dd > self.max_drawdown_seen_pct:
            self.max_drawdown_seen_pct = dd

        if (
            self.use_equity_stop
            and self.max_dd_pct > 0
            and self.peak_equity > 0
            and dd >= self.max_dd_pct
        ):
            self._in_pause = True
            self.kill_switch_triggers += 1
            self.events["dd_stop"] = self.kill_switch_triggers
            if self.dd_cooldown_ms > 0:
                self.paused_until_ms = now_ms + self.dd_cooldown_ms
            else:
                self.paused_until_ms = 0
            return True

        return False

    def force_close_due(self, entries_paused: bool, has_open_position: bool) -> bool:
        due = False
        if (
            self.close_all_at_limit
            and entries_paused
            and self._in_pause
            and not self._limit_close_done
            and has_open_position
        ):
            self._limit_close_done = True
            self.force_closes += 1
            self.events["forced_close"] = self.force_closes
            due = True
        if not entries_paused and self._limit_close_done:
            self._limit_close_done = False
        return due

    def summary(self) -> dict:
        return {
            "use_equity_stop": self.use_equity_stop,
            "max_drawdown_limit_pct": self.max_dd_pct,
            "close_all_at_limit": self.close_all_at_limit,
            "dd_cooldown_hours": self.dd_cooldown_ms / 3_600_000.0,
            "max_drawdown_seen_pct": round(self.max_drawdown_seen_pct, 4),
            "kill_switch_triggers": self.kill_switch_triggers,
            "cooldown_resets": self.cooldown_R_resets,
            "force_closes": self.force_closes,
            "dd_stop": self.kill_switch_triggers,
            "forced_close": self.force_closes,
        }


FIXED_BREAKEVEN_REASON = "BREAKEVEN"
TRAILING_STOP_REASON = "TRAILING_STOP"


class PositionState:

    def __init__(
        self,
        entry_price: float,
        levels_dict: dict,
        entry_time: int = 0,
    ) -> None:
        ep = float(entry_price)
        if not math.isfinite(ep) or ep <= 0:
            raise ValueError(
                f"PositionState membutuhkan entry_price > 0 dan berhingga, dapat {entry_price!r}"
            )
        src = str(levels_dict.get("source", "ENTRY_RULE") or "ENTRY_RULE").upper()
        sl_p = float(levels_dict.get("sl_pct") or 0.0)
        tp_p = float(levels_dict.get("tp_pct") or 0.0)
        if not (0.0 < sl_p < 100.0):
            raise ValueError(
                f"Persen SL struktural ({sl_p}%) tidak valid; wajib di antara 0 dan 100."
            )
        if not (tp_p > 0.0 and math.isfinite(tp_p)):
            raise ValueError(
                f"Persen TP struktural ({tp_p}%) tidak valid; wajib > 0."
            )
        raw_sl_price = levels_dict.get("sl_price")
        raw_tp_price = levels_dict.get("tp_price")
        sl_price = (
            float(raw_sl_price)
            if raw_sl_price is not None and 0.0 < float(raw_sl_price) < ep
            else ep * (1.0 - sl_p / 100.0)
        )
        tp_price = (
            float(raw_tp_price)
            if raw_tp_price is not None and float(raw_tp_price) > ep
            else ep * (1.0 + tp_p / 100.0)
        )
        self.entry_price = ep
        self.entry_time = int(entry_time or 0)
        self.highest_price = self.entry_price
        self.sl_price = sl_price
        self.tp_price = tp_price
        self.be_active = False
        self.be_stop = 0.0
        self.be_activated = False
        self.trailing_active = False
        self.trailing_stop_price = 0.0
        self.initial_sl_price = sl_price
        raw_atr = levels_dict.get("atr")
        try:
            atr_val = float(raw_atr) if raw_atr is not None else None
        except (TypeError, ValueError):
            atr_val = None
        if atr_val is not None and atr_val <= 0.0:
            atr_val = None
        self.levels = {
            "src": src,
            "atr": atr_val,
            "use_atr": False,
            "sl": sl_p,
            "tp": tp_p,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "rr_ratio": float(
                levels_dict.get("rr_ratio") or (tp_p / sl_p if sl_p > 0 else 0.0)
            ),
            "be_trig": 0.0,
            "be_lock": 0.0,
            "tr_start": 0.0,
            "tr_step": 0.0,
        }


def _alasan_stop(pos: PositionState, sl_price: float) -> str:
    trail = float(getattr(pos, "trailing_stop_price", 0.0) or 0.0)
    if bool(getattr(pos, "trailing_active", False)) and trail > 0.0:
        if sl_price >= trail - 1e-12:
            return TRAILING_STOP_REASON
    if bool(getattr(pos, "be_active", False)) and sl_price >= float(pos.entry_price):
        return FIXED_BREAKEVEN_REASON
    return "STOP_LOSS"


def evaluate_candle_exit(
    pos: PositionState,
    candle: Kline,
    config: dict,
    cap_ms: int = 0,
    bar_ms: int = 0,
) -> Optional[tuple[str, float]]:
    entry = pos.entry_price
    lv = pos.levels
    sl_price = float(lv.get("sl_price") or (entry * (1.0 - lv["sl"] / 100.0)))

    puncak_sebelum = pos.highest_price
    if candle.high > pos.highest_price:
        pos.highest_price = candle.high

    if candle.low <= sl_price:
        return _alasan_stop(pos, sl_price), min(sl_price, candle.open)
    be_params = strategy.entry_breakeven_params(config)
    if be_params["enabled"] and not pos.be_active:
        pemicu = strategy.entry_breakeven_trigger(
            entry, pos.initial_sl_price, be_params["r_multiple"]
        )
        if pemicu > entry and candle.high >= pemicu:
            level_be = strategy.entry_breakeven_price(
                entry, pos.initial_sl_price, be_params["r_multiple"], be_params["lock_pct"]
            )
            if level_be > sl_price:
                pos.be_stop = level_be
                pos.be_active = True
                pos.be_activated = True
                pos.sl_price = level_be
                pos.levels["sl_price"] = level_be
                sl_price = level_be
    trail_params = strategy.exit_trailing_params(config)
    if trail_params["enabled"]:
        pemicu_trail = strategy.exit_trailing_start_price(
            entry, pos.initial_sl_price, trail_params["start_r"]
        )
        if pemicu_trail > 0.0 and puncak_sebelum >= pemicu_trail:
            kandidat = strategy.exit_trailing_stop_price(
                puncak_sebelum, trail_params["pct"]
            )
            if kandidat > 0.0 and kandidat > sl_price:
                pos.trailing_active = True
                pos.trailing_stop_price = kandidat
                pos.sl_price = kandidat
                pos.levels["sl_price"] = kandidat
                sl_price = kandidat
    if candle.low <= sl_price:
        return _alasan_stop(pos, sl_price), min(sl_price, candle.open)
    if strategy.time_stop_due(pos.entry_time, candle.close_time, cap_ms, bar_ms):
        return TIME_STOP_REASON, candle.close

    return None


def entry_warmup_bars(config: dict, interval: str = "5m") -> int:
    return strategy.entry_warmup_bars(config, str(interval))


def entry_warmup_ms(config: dict, interval: str = "5m") -> int:
    return strategy.gate_warmup_ms(config, str(interval))


def gate_warmup_bars(config: dict, interval: str = "5m") -> int:
    return strategy.gate_warmup_bars(config, str(interval))


def gate_warmup_ms(config: dict, interval: str = "5m") -> int:
    return strategy.gate_warmup_ms(config, str(interval))


def same_coin_block_until_ms(
    close_time_ms: int, pnl_quote: float, config: dict
) -> int:
    hours = same_coin_block_hours(config)
    if hours <= 0:
        return 0
    if bool(config.get("SAME_COIN_BLOCK_LOSS_ONLY", True)) and float(pnl_quote) >= 0:
        return 0
    return int(close_time_ms) + int(round(hours * 3_600_000))


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


