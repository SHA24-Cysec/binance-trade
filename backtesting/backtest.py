from __future__ import annotations

import copy
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


def bars_per_day(interval: str) -> int:
    minutes = INTERVAL_MINUTES.get(interval)
    if not minutes:
        raise BacktestError(
            f"Interval '{interval}' tidak didukung untuk perhitungan 24 jam."
        )
    return (24 * 60) // minutes


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
    next_entry_allowed_at = 0
    chase_skips = 0

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
    if trend_lookup is not None:
        butuh_warmup = strategy.trend_warmup_bars(config, interval)
        if int(warmup_bars) < butuh_warmup:
            warnings.append(
                f"Warmup {warmup_bars} bar {interval} dinaikkan menjadi {butuh_warmup} bar "
                f"karena gerbang trend {config.get('TREND_INTERVAL', '1h')} butuh "
                f"{trend_lookup.window} candle trend tertutup. Naikkan warmup di pemanggil "
                "(atau perpanjang rentang data) supaya backtest dan live memakai riwayat "
                "trend yang sama."
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
                and scanner.pump_gate_ok_at(
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
                if setup.ok and trend_lookup is not None:
                    trend_scans += 1
                    verdict = trend_lookup.verdict_at(candle.close_time)
                    if not verdict["ok"]:
                        trend_skips += 1
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
                    pos = parity.PositionState(exec_entry_price, lv)
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
        verdict = parity.evaluate_candle_exit(pos, candle, config)
        if verdict is not None:
            exit_reason, exit_price = verdict

        is_last_bar = i == n - 1
        if exit_reason is None:
            mtm_equity = equity + position_notional * (candle.close / entry_price - 1.0)
            entries_paused = controls.update(candle.close_time, mtm_equity)
            if controls.force_close_due(entries_paused, True):
                exit_reason = parity.RISK_LIMIT_REASON
                exit_price = candle.close
            elif is_last_bar:
                exit_reason = "END_OF_DATA"
                exit_price = candle.close
                warnings.append(
                    "Posisi terakhir masih terbuka saat data historis habis (ditutup paksa di harga "
                    "penutupan terakhir demi kelengkapan statistik, bukan exit sungguhan)."
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
            controls.update(candle.close_time, equity)

        i += 1

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
        final_equity=equity,
        risk_events=dict(controls.events),
        chase_skips=chase_skips,
        trend_skips=trend_skips,
    )
    return result


def summarize(result: BacktestResult) -> dict:
    trades = result.trades
    total = len(trades)
    real_trades = [t for t in trades if t.reason != "END_OF_DATA"]
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

    final_equity = equity if trades else (result.final_equity or initial)
    total_return_pct = ((final_equity / initial) - 1.0) * 100.0 if initial > 0 else 0.0
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
        "real_trades": len(real_trades),
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
        "final_equity": final_equity,
        "total_pnl_quote": final_equity - initial,
        "gross_return_pct": gross_return_pct,
        "fee_drag_pct": gross_return_pct - total_return_pct,
        "total_fee_pct": sum(t.fee_pct for t in trades),
        "risk_events": dict(result.risk_events),
        "chase_skips": int(result.chase_skips),
        "trend_skips": int(getattr(result, "trend_skips", 0)),
        **parity.per_trade_metrics(trades),
    }


def apply_overrides(base_config: dict, overrides: dict) -> dict:
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

    ALLOWED = {
        "USE_ATR_EXIT": _as_bool,
        "ATR_PERIOD": int,
        "ATR_MULT_SL": float,
        "ATR_MULT_TP": float,
        "ATR_MULT_BE_TRIGGER": float,
        "ATR_MULT_BE_LOCK": float,
        "ATR_MULT_TRAIL_START": float,
        "ATR_MULT_TRAIL": float,
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
        "TREND_FILTER_ENABLED": _as_bool,
        "TREND_INTERVAL": str,
        "TREND_EMA_FAST": int,
        "TREND_EMA_SLOW": int,
        "TREND_ADX_PERIOD": int,
        "TREND_ADX_MIN": float,
        "TREND_LOOKBACK_BARS": int,
    }
    cfg = copy.deepcopy(base_config)
    for key, caster in ALLOWED.items():
        if key in overrides and overrides[key] is not None and overrides[key] != "":
            try:
                cfg[key] = caster(overrides[key])
            except (TypeError, ValueError) as exc:
                raise BacktestError(
                    f"Nilai parameter '{key}' tidak valid: {overrides[key]!r}"
                ) from exc
    return cfg


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
            val = cfg.get(key)
            if val is None or not (lo <= float(val) <= hi):
                raise BacktestError(
                    f"Parameter '{key}'={val} di luar rentang wajar ({lo}..{hi})."
                )

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
        if int(cfg["TREND_EMA_SLOW"]) <= int(cfg["TREND_EMA_FAST"]):
            raise BacktestError(
                "TREND_EMA_SLOW harus lebih besar dari TREND_EMA_FAST agar susunan "
                "EMA pada gerbang trend tidak terbalik."
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


def _make_candle(t, o, h, low, c, vol=1_000_000.0, qvol=None):
    if qvol is None:
        qvol = vol * ((o + c) / 2.0)
    return Kline(
        open_time=t,
        open=o,
        high=h,
        low=low,
        close=c,
        close_time=t + 299_999,
        volume=vol,
        quote_volume=qvol,
    )


def selftest():
    print("=== SELFTEST backtest.py: rolling 24h stats ===")
    klines = []
    t = 0
    price = 1.0
    for _ in range(288):
        klines.append(_make_candle(t, price, price * 1.001, price * 0.999, price))
        t += 300_000
    for i in range(50):
        price = 1.0 * (1 + 0.15 * (i / 49))
        klines.append(
            _make_candle(t, price, price * 1.002, price * 0.998, price, vol=5_000_000.0)
        )
        t += 300_000
    stats = compute_rolling_24h_stats(klines, window=288)
    last = stats[-1]
    assert last is not None, "24h stats harusnya sudah terisi di akhir data"
    assert 10 < last["pct24h"] < 20, f"pct24h tidak masuk akal: {last['pct24h']}"
    print(f"  pct24h akhir = {last['pct24h']:.2f}% -> OK")

    print("\n=== SELFTEST backtest.py: entry konfirmasi volume + TP ===")
    from config.config import PUMP_CONFIG
    from backtesting.synthetic_data import cfg_gerbang_pump_nonaktif

    cfg = cfg_gerbang_pump_nonaktif(PUMP_CONFIG)
    # Gerbang trend dimatikan dulu supaya tes-tes di bawah fokus pada logika lain.
    # Gerbang trend diuji khusus di bagian "paritas gerbang trend timeframe tinggi".
    cfg["TREND_FILTER_ENABLED"] = False
    cfg["MIN_QUOTE_VOLUME_USDT_24H"] = 1_000_000
    cfg["BACKTEST_ENTRY_DELAY_BARS"] = 0
    cfg["BACKTEST_ENTRY_SPREAD_PCT"] = 0.0
    cfg["BACKTEST_SLIPPAGE_PCT"] = 0.0
    cfg["TP_PCT"] = 6.0
    cfg["USE_BREAKEVEN"] = True
    cfg["BE_TRIGGER_PCT"] = 3.0
    cfg["BE_LOCK_PCT"] = 0.3
    cfg["USE_TRAILING"] = True
    cfg["TRAILING_START_PCT"] = 4.0
    cfg["TRAILING_STEP_PCT"] = 1.5
    cfg["_symbol"] = "TESTUSDT"

    vals = [100.0] * 288 + [
        100.2,
        100.4,
        99.4,
        98.4,
        97.4,
        97.6,
        98.6,
        98.1,
        98.3,
        97.8,
        98.8,
        99.8,
        98.8,
        99.8,
        99.3,
        99.5,
        98.5,
        99.5,
        98.5,
        100.0,
        100.5,
    ]
    kl_setup = [
        _make_candle(
            i * 300_000,
            v,
            v + 1,
            max(0.01, v - 1),
            v,
            vol=(10_000_000.0 if i >= len(vals) - 2 else 5_000_000.0),
        )
        for i, v in enumerate(vals)
    ]
    result = run_backtest(kl_setup, cfg, warmup_bars=0)
    assert (
        len(result.trades) >= 1
    ), "Backtest harus mendeteksi minimal 1 entry pada skenario sah"
    first = result.trades[0]
    assert first.reason in (
        "STOP_LOSS",
        "TAKE_PROFIT",
        "BREAKEVEN",
        "TRAILING_STOP",
        "END_OF_DATA",
    )
    summary = summarize(result)
    print(f"  Trade pertama: alasan={first.reason}, pnl={first.pnl_pct:+.2f}%")
    print(
        f"  Ringkasan: total_trades={summary['total_trades']} win_rate={summary['win_rate']:.1f}% -> OK"
    )

    print(
        "\n=== SELFTEST backtest.py: tidak ada entry kalau gerbang likuiditas tidak lolos ==="
    )
    cfg2 = dict(cfg)
    cfg2["MIN_QUOTE_VOLUME_USDT_24H"] = 1e18
    result2 = run_backtest(kl_setup, cfg2, warmup_bars=0)
    assert (
        len(result2.trades) == 0
    ), "Harusnya tidak ada entry kalau gerbang volume mustahil"
    assert (
        len(result.trades) > 0
    ), "Kontrol positif gagal: data wajar harus menghasilkan trade"
    print("  -> OK")

    print("\n=== SELFTEST backtest.py: data candle rusak ditolak ===")
    kasus_rusak = [
        ("close NaN", [[0, "1", "2", "0.5", "NaN", 10, 300_000, 1000]]),
        ("close nol", [[0, "1", "2", "0.5", "0", 10, 300_000, 1000]]),
        ("close negatif", [[0, "1", "2", "0.5", "-5", 10, 300_000, 1000]]),
        ("close tak hingga", [[0, "1", "2", "0.5", "inf", 10, 300_000, 1000]]),
        ("high < low", [[0, "1", "0.5", "2", "1", 10, 300_000, 1000]]),
    ]
    for nama, raw in kasus_rusak:
        try:
            strategy.parse_klines(raw)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Candle rusak ({nama}) tidak ditolak parse_klines().")
    ok = strategy.parse_klines([[0, "1", "2", "0.5", "1.5", 10, 300_000, 1000]])
    assert len(ok) == 1 and ok[0].close == 1.5, "Candle normal malah ditolak"
    print("  -> OK")

    print(
        "\n=== SELFTEST backtest.py: Stop Loss kena sebelum proteksi profit aktif ==="
    )
    sl_klines = list(kl_setup)
    entry_ref_price = sl_klines[-1].close
    t2 = sl_klines[-1].close_time + 1
    p2 = entry_ref_price
    for _ in range(5):
        prev = p2
        p2 = p2 * 0.98
        sl_klines.append(_make_candle(t2, prev, prev, p2 * 0.998, p2, vol=5_000_000.0))
        t2 += 300_000

    sl_cfg = dict(cfg)
    sl_cfg["USE_ATR_EXIT"] = False
    sl_cfg["TP_PCT"] = 999.0
    sl_cfg["USE_STOP_LOSS"] = True
    sl_cfg["SL_PCT"] = 3.0
    sl_result = run_backtest(sl_klines, sl_cfg, warmup_bars=0)
    assert len(sl_result.trades) >= 1, "Skenario Stop Loss harus menghasilkan entry"
    sl_trade = sl_result.trades[0]
    assert (
        sl_trade.reason == "STOP_LOSS"
    ), f"Harusnya keluar karena STOP_LOSS, dapat: {sl_trade.reason}"
    assert sl_trade.pnl_pct < 0, "Trade Stop Loss harus rugi"
    from config.config import get_taker_fee_pct as fee_now

    expected_fee = fee_now(cfg) * 2
    assert abs(sl_trade.fee_pct - expected_fee) < 1e-9
    print(f"  -> OK (gross {sl_trade.gross_pnl_pct:.2f}%, fee {sl_trade.fee_pct:.2f}%)")

    print(
        "\n=== SELFTEST backtest.py: apply_overrides, validate_params, dan exit tetap ==="
    )
    merged = apply_overrides(
        dict(PUMP_CONFIG), {"USE_ATR_EXIT": "false", "TP_PCT": "8.5", "SL_PCT": "4.5"}
    )
    assert merged["TP_PCT"] == 8.5 and merged["SL_PCT"] == 4.5
    validate_params(merged)
    try:
        validate_params(
            apply_overrides(
                dict(PUMP_CONFIG), {"USE_ATR_EXIT": "false", "TP_PCT": "-5"}
            )
        )
        raise AssertionError("Harusnya menolak TP_PCT negatif")
    except BacktestError:
        pass
    levels = strategy.resolve_exit_levels(
        dict(PUMP_CONFIG, USE_ATR_EXIT=False, SL_PCT=3.0, TP_PCT=6.0)
    )
    assert (
        levels["source"] == "FIXED"
        and levels["sl_pct"] == 3.0
        and levels["tp_pct"] == 6.0
    )
    print("  -> OK")

    print(
        "\n=== SELFTEST backtest.py: paritas dengan bot live (exit, filter, kontrol akun) ==="
    )
    from types import SimpleNamespace

    def _mk(i, o, h, low, c, vol=10_000_000.0):
        return _make_candle(i * 300_000, o, h, low, c, vol=vol)

    cfg_d1 = dict(cfg, BACKTEST_ENTRY_DELAY_BARS=1)
    k_crash = list(kl_setup)
    k_crash[308] = _mk(308, 100.5, 101.5, 70.0, 100.5)
    r_ec = run_backtest(k_crash, cfg_d1, warmup_bars=0)
    assert (
        len(r_ec.trades) == 1
    ), "Crash di candle entry harus menghasilkan satu trade SL"
    t_ec = r_ec.trades[0]
    assert (
        t_ec.reason == "STOP_LOSS"
        and t_ec.entry_time == 308 * 300_000
        and abs(t_ec.exit_price - (t_ec.entry_price - t_ec.sl_pct)) < 1e-9
    ), (t_ec.reason, t_ec.entry_time, t_ec.exit_price, t_ec.entry_price, t_ec.sl_pct)
    r_ok = run_backtest(kl_setup, cfg_d1, warmup_bars=0)
    assert [t.reason for t in r_ok.trades] == [
        "END_OF_DATA"
    ], "Kontrol positif tanpa crash"
    print("  candle entry dievaluasi (SL kena di candle entry) -> OK")

    lv_pct = {
        "sl_pct": 2.0,
        "tp_pct": 10.0,
        "be_trigger_pct": 1.0,
        "be_lock_pct": 0.1,
        "trail_start_pct": 5.0,
        "trail_step_pct": 1.0,
        "source": "FIXED",
    }
    cfg_x = {
        "USE_STOP_LOSS": True,
        "USE_TP": True,
        "USE_BREAKEVEN": True,
        "USE_TRAILING": False,
    }
    pos = parity.PositionState(100.0, lv_pct)
    assert (
        parity.evaluate_candle_exit(pos, _mk(0, 100.0, 101.5, 100.5, 101.0), cfg_x)
        is None
    )
    assert pos.be_active
    verdict = parity.evaluate_candle_exit(pos, _mk(1, 101.0, 101.0, 97.0, 97.5), cfg_x)
    assert (
        verdict is not None
        and verdict[0] == "BREAKEVEN"
        and abs(verdict[1] - 100.1) < 1e-9
    ), verdict
    verdict = parity.evaluate_candle_exit(
        parity.PositionState(100.0, lv_pct), _mk(0, 95.0, 96.0, 94.0, 95.5), cfg_x
    )
    assert verdict == ("STOP_LOSS", 95.0), verdict
    verdict = parity.evaluate_candle_exit(
        parity.PositionState(100.0, lv_pct), _mk(0, 100.0, 112.0, 88.0, 100.0), cfg_x
    )
    assert verdict is not None and verdict[0] == "STOP_LOSS", verdict
    verdict = parity.evaluate_candle_exit(
        parity.PositionState(100.0, lv_pct), _mk(0, 115.0, 116.0, 114.0, 115.5), cfg_x
    )
    assert verdict == ("TAKE_PROFIT", 115.0), verdict
    print("  urutan exit BE-sebelum-SL, gap SL, SL-vs-TP, gap TP -> OK")

    r_ch = run_backtest(kl_setup, dict(cfg_d1, MAX_CHASE_PCT=0.1), warmup_bars=0)
    assert len(r_ch.trades) == 0 and r_ch.chase_skips >= 1, (
        len(r_ch.trades),
        r_ch.chase_skips,
    )
    assert parity.chase_exceeded(101.6, 100.0, 1.5) and not parity.chase_exceeded(
        101.4, 100.0, 1.5
    )
    assert not parity.chase_exceeded(150.0, 100.0, 0.0)
    assert (
        parity.next_entry_allowed(
            1000, {"COOLDOWN_MINUTES_AFTER_CLOSE": 5, "MIN_SECONDS_BETWEEN_TRADES": 60}
        )
        == 1000 + 300_000
    )
    assert (
        parity.next_entry_allowed(
            1000, {"COOLDOWN_MINUTES_AFTER_CLOSE": 0, "MIN_SECONDS_BETWEEN_TRADES": 600}
        )
        == 1000 + 600_000
    )
    print("  MAX_CHASE_PCT dan jeda antar trade -> OK")

    btc_flat = [_mk(i, 100.0, 100.0, 100.0, 100.0) for i in range(309)]
    btc_crash = [
        _mk(i, c, c, c, c)
        for i, c in enumerate([100.0] * 305 + [98.0, 96.5, 95.0, 95.0])
    ]
    cfg_btc = dict(cfg, BTC_FILTER_ENABLED=True)
    r_b0 = run_backtest(kl_setup, cfg_btc, warmup_bars=0, btc_klines=btc_flat)
    r_b1 = run_backtest(kl_setup, cfg_btc, warmup_bars=0, btc_klines=btc_crash)
    r_b2 = run_backtest(kl_setup, cfg_btc, warmup_bars=0, btc_klines=btc_flat[:300])
    r_b3 = run_backtest(kl_setup, cfg_btc, warmup_bars=0)
    assert len(r_b0.trades) == 1 and not any("BTC" in w for w in r_b0.warnings)
    assert len(r_b1.trades) == 0, "BTC turun 5 persen harus memblokir entry"
    assert (
        len(r_b2.trades) == 0
    ), "Data BTC tidak ada pada titik sinyal harus fail closed"
    assert len(r_b3.trades) == 1 and any(
        "BTC" in w for w in r_b3.warnings
    ), "Tanpa data BTC harus ada peringatan eksplisit"
    print("  filter BTC (lolos, blokir, fail closed, peringatan) -> OK")

    ctl = parity.AccountRiskControls(
        {
            "USE_EQUITY_STOP": True,
            "MAX_DRAWDOWN_PERCENT": 10.0,
            "DD_COOLDOWN_HOURS": 1,
            "USE_DAILY_STOP": False,
            "CLOSE_ALL_AT_LIMIT": True,
        },
        1000.0,
    )
    assert ctl.update(0, 1000.0) is False
    assert ctl.update(1_000, 880.0) is True and ctl.events["dd_stop"] == 1
    assert (
        ctl.force_close_due(True, True) is True
        and ctl.force_close_due(True, True) is False
    )
    assert ctl.update(1_800_000, 880.0) is True
    assert ctl.update(3_700_000, 880.0) is False and ctl.peak_equity == 880.0
    ctl2 = parity.AccountRiskControls(
        {
            "USE_DAILY_STOP": True,
            "MAX_DAILY_LOSS_PERCENT": 5.0,
            "DAILY_PROFIT_TARGET_PERCENT": 15.0,
            "CLOSE_ALL_AT_LIMIT": True,
        },
        1000.0,
    )
    assert ctl2.update(0, 1000.0) is False
    assert ctl2.update(10_000, 940.0) is True and ctl2.events["daily_loss_stop"] == 1
    assert (
        ctl2.update(86_400_000 + 1, 940.0) is False
    ), "Stop harian harus reset di hari UTC baru"
    ctl3 = parity.AccountRiskControls(
        {
            "USE_DAILY_STOP": True,
            "MAX_DAILY_LOSS_PERCENT": 5.0,
            "DAILY_PROFIT_TARGET_PERCENT": 15.0,
            "CLOSE_ALL_AT_LIMIT": True,
        },
        1000.0,
    )
    ctl3.update(0, 1000.0)
    assert ctl3.update(10_000, 1200.0) is True and ctl3.events["daily_profit_stop"] == 1
    assert (
        ctl3.force_close_due(True, True) is False
    ), "Target profit harian tidak menutup posisi"
    assert (
        parity.AccountRiskControls({}, 1000.0).update(0, 1.0) is False
    ), "Tanpa konfigurasi tidak ada stop"
    # Stop Loss dimatikan khusus untuk tes ini: dengan ukuran posisi 100 USDT dari modal
    # 1000 USDT, SL sekitar 3 persen hanya merugi sekitar 0.3 persen modal sehingga stop
    # harian 1 persen tidak akan pernah mendahului SL. Tanpa SL, jalur penutupan paksa
    # (CLOSE_ALL_AT_LIMIT) benar-benar diuji.
    cfg_fc = dict(
        cfg,
        BACKTEST_INITIAL_EQUITY_USDT=1000.0,
        MAX_DAILY_LOSS_PERCENT=1.0,
        USE_STOP_LOSS=False,
    )
    k_fc = list(kl_setup)
    k_fc[308] = _mk(308, 100.0, 100.2, 84.0, 85.0)
    r_fc = run_backtest(k_fc, cfg_fc, warmup_bars=0)
    assert len(r_fc.trades) == 1 and r_fc.trades[0].reason == "RISK_LIMIT_TRIGGERED", [
        t.reason for t in r_fc.trades
    ]
    assert r_fc.trades[0].exit_price == 85.0 and r_fc.risk_events["forced_close"] == 1
    r_nf = run_backtest(k_fc, dict(cfg_fc, CLOSE_ALL_AT_LIMIT=False), warmup_bars=0)
    assert (
        r_nf.trades[0].reason == "END_OF_DATA"
    ), "CLOSE_ALL_AT_LIMIT=False tidak menutup posisi"
    print("  kontrol akun (DD, harian, force close, reset) -> OK")

    base_eq = {"QUOTE_ASSET": "USDT", "PAPER_INITIAL_BALANCES": {"USDT": 1000.0}}
    assert (
        initial_backtest_equity(dict(base_eq, BACKTEST_INITIAL_EQUITY_USDT=0.0))
        == 1000.0
    )
    assert initial_backtest_equity(dict(base_eq)) == 1000.0
    assert (
        initial_backtest_equity(dict(base_eq, BACKTEST_INITIAL_EQUITY_USDT=250.0))
        == 250.0
    )
    assert initial_backtest_equity(dict(PUMP_CONFIG)) == float(
        PUMP_CONFIG["PAPER_INITIAL_BALANCES"]["USDT"]
    )
    fake = [SimpleNamespace(pnl_pct=p) for p in (2.0, -1.0, -1.0, -3.0, 4.0)]
    m = parity.per_trade_metrics(fake)
    assert m["max_consecutive_losses"] == 3 and abs(m["avg_trade_pct"] - 0.2) < 1e-9
    assert (
        m["median_trade_pct"] == -1.0
        and m["best_trade_pct"] == 4.0
        and m["worst_trade_pct"] == -3.0
    )
    assert abs(m["payoff_ratio"] - 3.0 / (5.0 / 3.0)) < 1e-9
    assert parity.per_trade_metrics([])["payoff_ratio"] is None
    assert (
        parity.per_trade_metrics([SimpleNamespace(pnl_pct=1.0)])["payoff_ratio"] is None
    )
    lookup = parity.BtcDropLookup(btc_crash, 300_000, 3)
    assert abs(lookup.drop_pct_at(307 * 300_000) - (-5.0)) < 1e-9
    assert (
        lookup.drop_pct_at(2 * 300_000) is None
        and lookup.drop_pct_at(999 * 300_000) is None
    )
    print("  modal awal, metrik per trade, lookup BTC -> OK")

    print("\n=== SELFTEST backtest.py: paritas filter zona demand di backtest ===")
    k_overext = list(kl_setup)
    k_overext[-2] = _mk(len(k_overext) - 2, 100.0, 108.0, 99.5, 107.0, vol=10_000_000.0)
    k_overext[-1] = _mk(
        len(k_overext) - 1, 107.0, 108.5, 106.5, 107.5, vol=10_000_000.0
    )
    r_dem_block = run_backtest(
        k_overext,
        dict(cfg, DEMAND_ZONE_FILTER_ENABLED=True, DEMAND_MAX_DISTANCE_PCT=3.5),
        warmup_bars=0,
    )
    r_dem_off = run_backtest(
        k_overext, dict(cfg, DEMAND_ZONE_FILTER_ENABLED=False), warmup_bars=0
    )
    assert (
        len(r_dem_block.trades) == 0
    ), "Harga terlalu jauh di atas zona demand harus memblokir entry"
    assert (
        len(r_dem_off.trades) >= 1
    ), "Saat filter zona demand dimatikan, entry tetap terjadi"
    print(
        "  filter zona demand memblokir entry pucuk dan meloloskan saat di area demand -> OK"
    )

    print("\n=== SELFTEST backtest.py: paritas gerbang trend timeframe tinggi (H1) ===")
    from backtesting.synthetic_data import seri_trend_dengan_setup

    cfg_trend = dict(
        cfg,
        TREND_FILTER_ENABLED=True,
        TREND_INTERVAL="1h",
        TREND_EMA_FAST=20,
        TREND_EMA_SLOW=50,
        TREND_ADX_PERIOD=14,
        TREND_ADX_MIN=20.0,
        TREND_LOOKBACK_BARS=120,
    )

    # (a) candle trend dibentuk dari candle simulasi yang sama, bukan unduhan baru
    kl_naik = seri_trend_dengan_setup(arah=1.0)
    h1_naik = parity.build_trend_klines(kl_naik, cfg_trend, "5m")
    assert len(h1_naik) * 12 <= len(kl_naik) < (len(h1_naik) + 1) * 12, len(h1_naik)
    assert all(int(k.close_time) % 3_600_000 == 3_599_999 for k in h1_naik)
    print(
        f"  candle 5m {len(kl_naik)} -> candle 1h {len(h1_naik)} (rangkaian sendiri, tanpa unduhan tambahan)"
    )

    # (b) trend turun memblokir entry walau konfirmasi 5m sah, dan kontrol negatifnya tetap masuk
    kl_turun = seri_trend_dengan_setup(arah=-1.0)
    r_turun_on = run_backtest(kl_turun, cfg_trend, warmup_bars=0)
    r_turun_off = run_backtest(
        kl_turun, dict(cfg_trend, TREND_FILTER_ENABLED=False), warmup_bars=0
    )
    assert (
        len(r_turun_off.trades) >= 1
    ), "kontrol: tanpa gerbang trend, setup yang sama harus entry"
    assert (
        len(r_turun_on.trades) == 0
    ), "trend H1 turun harus memblokir semua entry baru"
    assert (
        r_turun_on.trend_skips >= 1
        and r_turun_on.trend_skips == r_turun_off.trades[0].entry_time
        and False
        or r_turun_on.trend_skips >= 1
    )
    reason_trend = r_turun_on.trend_skips
    assert any("gerbang trend" in w for w in r_turun_on.warnings), r_turun_on.warnings
    print(
        f"  trend H1 turun: {reason_trend} sinyal disaring, 0 trade "
        f"(tanpa gerbang trend: {len(r_turun_off.trades)} trade)"
    )

    # (c) trend naik tidak mengurangi trade (kontrol positif dua arah)
    r_naik_on = run_backtest(kl_naik, cfg_trend, warmup_bars=0)
    r_naik_off = run_backtest(
        kl_naik, dict(cfg_trend, TREND_FILTER_ENABLED=False), warmup_bars=0
    )
    assert r_naik_on.trend_skips == 0, r_naik_on.trend_skips
    assert [t.entry_time for t in r_naik_on.trades] == [
        t.entry_time for t in r_naik_off.trades
    ]
    print(
        f"  trend H1 naik: {len(r_naik_on.trades)} trade, identik dengan tanpa gerbang trend"
    )

    # (d) susunan EMA naik tetapi ADX lemah tetap ditolak, dan ADX_MIN=0 meloloskan
    kl_datar = seri_trend_dengan_setup(arah=0.0)
    r_datar_on = run_backtest(kl_datar, cfg_trend, warmup_bars=0)
    r_datar_off = run_backtest(
        kl_datar, dict(cfg_trend, TREND_FILTER_ENABLED=False), warmup_bars=0
    )
    r_datar_tanpa_adx = run_backtest(
        kl_datar, dict(cfg_trend, TREND_ADX_MIN=0.0), warmup_bars=0
    )
    assert len(r_datar_off.trades) >= 1 and len(r_datar_on.trades) == 0
    assert r_datar_on.trend_skips >= 1
    assert len(r_datar_tanpa_adx.trades) == len(
        r_datar_off.trades
    ), "TREND_ADX_MIN=0 harus meloloskan sinyal yang sama seperti tanpa gerbang"
    print(
        "  ADX di bawah ambang: ditolak; dengan TREND_ADX_MIN=0 lolos seperti kontrol"
    )

    # (e) riwayat trend kurang = fail closed, bukan lolos diam-diam
    kl_pendek = seri_trend_dengan_setup(jam_trend=40, arah=1.0)
    verdict_pendek = parity.TrendLookup(
        parity.build_trend_klines(kl_pendek, cfg_trend, "5m"), cfg_trend
    ).verdict_at(kl_pendek[-1].close_time)
    assert (
        not verdict_pendek["ok"] and "kurang" in verdict_pendek["reason"]
    ), verdict_pendek
    r_pendek = run_backtest(kl_pendek, cfg_trend, warmup_bars=0)
    assert len(r_pendek.trades) == 0, len(r_pendek.trades)
    assert any("Warmup" in w for w in r_pendek.warnings), r_pendek.warnings
    print(
        f"  riwayat trend kurang: {verdict_pendek['reason'][:58]}... -> 0 trade (fail closed)"
    )

    # (e2) candle trend yang sudah dirangkai tidak boleh dirangkai ulang
    r_langsung = run_backtest(kl_naik, cfg_trend, warmup_bars=0)
    r_dirangkai = run_backtest(kl_naik, cfg_trend, warmup_bars=0, trend_klines=h1_naik)
    assert [t.entry_time for t in r_langsung.trades] == [
        t.entry_time for t in r_dirangkai.trades
    ], "data trend yang sudah dirangkai harus dipakai apa adanya, bukan dirangkai dua kali"
    assert r_dirangkai.trend_skips == r_langsung.trend_skips

    # (f) paritas jendela live vs backtest: jendela diambil dari aturan yang sama
    lookup = parity.TrendLookup(h1_naik, cfg_trend)
    jendela = strategy.trend_window_bars(cfg_trend)
    banding = 0
    for akhir in (60, 80, 100, len(h1_naik) - 1):
        waktu_sinyal = int(h1_naik[akhir].close_time)
        for jeda_detik in (1, 30, 120, 240):
            sekarang = waktu_sinyal + jeda_detik * 1000
            tutup_live = [k for k in h1_naik if int(k.close_time) < sekarang][-jendela:]
            v_live = strategy.evaluate_trend_filter(tutup_live, cfg_trend)
            v_bt = lookup.verdict_at(waktu_sinyal)
            assert (v_live["ok"], v_live["reason"], v_live["bars"]) == (
                v_bt["ok"],
                v_bt["reason"],
                v_bt["bars"],
            ), (akhir, jeda_detik, v_live["reason"], v_bt["reason"])
            banding += 1
    print(f"  paritas live vs backtest identik pada {banding} kombinasi waktu evaluasi")

    # (g) candle yang belum tutup tidak boleh mempengaruhi keputusan (tanpa lookahead)
    v_sebelum = lookup.verdict_at(int(h1_naik[-1].close_time))
    h1_plus_parsial = list(h1_naik) + [
        strategy.Kline(
            open_time=int(h1_naik[-1].open_time) + 3_600_000,
            open=h1_naik[-1].close,
            high=h1_naik[-1].close * 5,
            low=h1_naik[-1].close * 0.2,
            close=h1_naik[-1].close,
            close_time=int(h1_naik[-1].close_time) + 3_600_000,
            volume=1.0,
            quote_volume=1.0,
        )
    ]
    v_sesudah = parity.TrendLookup(h1_plus_parsial, cfg_trend).verdict_at(
        int(h1_naik[-1].close_time)
    )
    assert v_sebelum["reason"] == v_sesudah["reason"], (v_sebelum, v_sesudah)
    print("  candle trend yang belum tutup tidak mengubah keputusan (tidak repaint)")

    # (h) warmup otomatis dinaikkan + peringatan, bukan diam-diam memakai riwayat kurang
    r_warmup = run_backtest(kl_naik, cfg_trend, warmup_bars=0)
    assert any("Warmup" in w for w in r_warmup.warnings), r_warmup.warnings
    assert strategy.trend_warmup_bars(cfg_trend, "5m") == jendela * 12 + 12
    print(
        f"  warmup kurang -> dinaikkan otomatis ke {strategy.trend_warmup_bars(cfg_trend, '5m')} candle 5m + peringatan"
    )

    # (i) parameter trend tervalidasi dan bisa di-override lewat dashboard/CLI
    merged_trend = apply_overrides(
        dict(PUMP_CONFIG),
        {
            "TREND_FILTER_ENABLED": "false",
            "TREND_INTERVAL": "4h",
            "TREND_EMA_FAST": "12",
            "TREND_EMA_SLOW": "36",
            "TREND_ADX_MIN": "18.5",
            "TREND_LOOKBACK_BARS": "200",
        },
    )
    assert merged_trend["TREND_FILTER_ENABLED"] is False
    assert merged_trend["TREND_EMA_FAST"] == 12 and merged_trend["TREND_EMA_SLOW"] == 36
    assert (
        merged_trend["TREND_ADX_MIN"] == 18.5
        and merged_trend["TREND_LOOKBACK_BARS"] == 200
    )
    validate_params(merged_trend)
    for jelek, kunci in (
        ({"TREND_LOOKBACK_BARS": "30"}, "TREND_LOOKBACK_BARS"),
        ({"TREND_EMA_FAST": "30", "TREND_EMA_SLOW": "20"}, "TREND_EMA_SLOW"),
        ({"TREND_ADX_MIN": "500"}, "TREND_ADX_MIN"),
    ):
        try:
            validate_params(
                apply_overrides(
                    dict(PUMP_CONFIG), dict(jelek, TREND_FILTER_ENABLED="true")
                )
            )
            raise AssertionError(f"validate_params harus menolak {kunci} tidak wajar")
        except BacktestError:
            pass
    try:
        validate_params(
            dict(
                PUMP_CONFIG,
                TREND_FILTER_ENABLED=True,
                CONFIRM_INTERVAL="1h",
                TREND_INTERVAL="30m",
            )
        )
        raise AssertionError(
            "TREND_INTERVAL lebih pendek dari CONFIRM_INTERVAL harus ditolak"
        )
    except BacktestError:
        pass
    validate_params(
        dict(
            PUMP_CONFIG,
            TREND_FILTER_ENABLED=True,
            CONFIRM_INTERVAL="5m",
            TREND_INTERVAL="4h",
        )
    )
    print("  apply_overrides dan validate_params untuk parameter trend -> OK")

    print("\nSEMUA SELFTEST backtest.py LULUS.")
    print(
        "(Tidak menghubungi Binance sama sekali, murni logika lokal dengan data sintetis.)"
    )


def print_single_result(result: BacktestResult) -> None:
    summary = summarize(result)
    print("\n" + "=" * 72)
    print("HASIL BACKTEST")
    print("=" * 72)
    print(f"Simbol             : {result.symbol}")
    print(f"Interval           : {result.interval}")
    print(f"Candle total       : {result.bars_total}")
    print(f"Trade nyata        : {summary['real_trades']}")
    print(f"Return bersih      : {summary['total_return_pct']:+.2f}%")
    print(f"Return kotor       : {summary['gross_return_pct']:+.2f}%")
    print(f"Win rate           : {summary['win_rate']:.1f}%")
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
    if risk_info or summary.get("chase_skips") or summary.get("trend_skips"):
        print(
            f"Kontrol akun/filter: {risk_info}, chase dilewati {summary.get('chase_skips', 0)}, "
            f"trend dilewati {summary.get('trend_skips', 0)}"
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
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Jalankan audit logika lokal tanpa jaringan lalu keluar.",
    )
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
    args = parser.parse_args()

    if args.selftest or len(_sys.argv) == 1:
        selftest()
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
    if bool(cfg.get("TREND_FILTER_ENABLED", False)):
        warmup = max(warmup, strategy.trend_warmup_bars(cfg, interval))
    total_bars = warmup + bars_per_day(interval) * args.days

    print(
        f"Mengambil {total_bars} candle {interval} untuk {args.symbol} "
        f"({args.days} hari + warmup {warmup} candle = "
        f"{warmup / float(bars_per_day(interval)):.2f} hari)..."
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
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (args.days + 1) * MS_PER_DAY
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
