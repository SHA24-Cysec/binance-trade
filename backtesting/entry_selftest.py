from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from config.config import PUMP_CONFIG
from config.settings_schema import (
    _migrate_legacy_keys,
    _validate_override_payload,
    validate_candidate,
)
from backtesting import backtest as bt
from backtesting import grid_search as gs
from backtesting import parity
from backtesting import portfolio_backtest as pbt
from backtesting.backtest_storage import KlineStore
from market.market_scanner import ScanDiagnostics, detect_entry_setup, find_best_candidate
from strategy import indicators as strategy
from strategy.indicators import Kline
from trading.pump_scanner_bot import HtfTrendCache


def _m5(index: int, open_price: float, high: float, low: float, close: float) -> Kline:
    start = index * 300_000
    return Kline(start, open_price, high, low, close, start + 299_999, 1_000.0, 100_000.0)


def _m15(index: int, open_price: float, high: float, low: float, close: float) -> Kline:
    start = index * 900_000
    return Kline(start, open_price, high, low, close, start + 899_999, 1_000.0, 100_000.0)


def _h1(index: int, close: float) -> Kline:
    start = index * 3_600_000
    return Kline(start, close - 0.1, close + 0.5, close - 0.5, close, start + 3_599_999, 1_000.0, 100_000.0)


def _raw_h1(index: int, close: float) -> list[float | int]:
    candle = _h1(index, close)
    return [
        candle.open_time,
        candle.open,
        candle.high,
        candle.low,
        candle.close,
        candle.volume,
        candle.close_time,
        candle.quote_volume,
    ]


def _m5_from_hourly_closes(hourly_closes: list[float]) -> list[Kline]:
    rows = []
    previous_close = float(hourly_closes[0]) - 1.0
    for hour_index, target_close in enumerate(hourly_closes):
        start_price = previous_close
        target_close = float(target_close)
        for part in range(12):
            open_price = start_price + (target_close - start_price) * part / 12.0
            close_price = start_price + (target_close - start_price) * (part + 1) / 12.0
            rows.append(
                _m5(
                    hour_index * 12 + part,
                    open_price,
                    max(open_price, close_price) + 0.02,
                    min(open_price, close_price) - 0.02,
                    close_price,
                )
            )
        previous_close = target_close
    return rows


def breakout_series() -> list[Kline]:
    rows = []
    for index in range(44):
        close = 100.0 + index * 0.12
        rows.append(_m5(index, close - 0.05, close + 0.25, close - 0.25, close))
    rows.extend(
        [
            _m5(44, 105.0, 110.0, 104.8, 105.3),
            _m5(45, 105.3, 107.2, 105.0, 106.7),
            _m5(46, 106.7, 107.0, 105.8, 106.2),
            _m5(47, 106.2, 107.3, 106.0, 107.0),
            _m5(48, 107.0, 107.5, 106.5, 107.2),
            _m5(49, 107.2, 108.0, 106.8, 107.7),
            _m5(50, 107.7, 109.0, 107.5, 108.5),
            _m5(51, 108.5, 111.8, 108.3, 111.3),
        ]
    )
    for index, close in enumerate(
        [111.5, 111.6, 111.7, 111.8, 111.7, 111.6, 111.5], start=52
    ):
        rows.append(_m5(index, close - 0.1, close + 0.15, close - 0.2, close))
    rows.append(_m5(59, 110.05, 111.2, 109.95, 110.8))
    return rows


def falling_m5() -> list[Kline]:
    rows = []
    for index in range(60):
        close = 110.0 - index * 0.1
        rows.append(_m5(index, close + 0.05, close + 0.2, close - 0.2, close))
    return rows


def rising_m15() -> list[Kline]:
    rows = []
    for index in range(65):
        close = 100.0 + index * 0.12
        rows.append(_m15(index, close - 0.05, close + 0.15, close - 0.15, close))
    return rows


def falling_m15() -> list[Kline]:
    rows = []
    for index in range(64):
        close = 108.0 - index * 0.10
        rows.append(_m15(index, close + 0.05, close + 0.15, close - 0.15, close))
    rows.append(_m15(64, 101.65, 101.85, 101.55, 101.72))
    return rows


def run(verbose: bool = True) -> tuple[bool, list[str]]:
    failures = []
    total = 0

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal total
        total += 1
        if not condition:
            failures.append(name + (f": {detail}" if detail else ""))
        if verbose:
            print(f"[{'OK' if condition else 'GAGAL'}] {name}" + (f": {detail}" if detail else ""))

    cfg = deepcopy(PUMP_CONFIG)
    rows = breakout_series()
    breakout = strategy.evaluate_breakout_retest(rows, cfg)
    check("breakout_retest_valid", breakout["ok"], breakout["reason"])
    check("breakout_level", abs(float(breakout.get("breakout_level") or 0.0) - 110.0) < 1e-9)
    check("breakout_closed_only", not strategy.evaluate_breakout_retest(rows[:-1], cfg)["ok"])

    bearish = list(rows)
    bearish[-1] = _m5(59, 111.0, 111.2, 109.95, 110.8)
    bearish_result = strategy.evaluate_breakout_retest(bearish, cfg)
    check("retest_bullish_required", not bearish_result["ok"] and "bullish" in bearish_result["reason"])

    deep = list(rows)
    deep[-1] = _m5(59, 109.8, 111.2, 109.4, 110.8)
    deep_result = strategy.evaluate_breakout_retest(deep, cfg)
    check("retest_tolerance_enforced", not deep_result["ok"] and "belum retest" in deep_result["reason"])

    stale_cfg = dict(cfg)
    stale_cfg["BREAKOUT_RETEST_MAX_BARS"] = 7
    stale_cfg["CONFIRM_LOOKBACK_BARS"] = 60
    stale_result = strategy.evaluate_breakout_retest(rows, stale_cfg)
    check("retest_expiry_enforced", not stale_result["ok"])

    touched = list(rows)
    touched[-2] = _m5(58, 110.1, 111.1, 110.0, 110.7)
    touch_cfg = dict(cfg)
    touch_cfg["BREAKOUT_RETEST_MAX_TOUCHES"] = 1
    touch_result = strategy.evaluate_breakout_retest(touched, touch_cfg)
    check("retest_touch_limit", not touch_result["ok"] and "disentuh" in touch_result["reason"])

    setup = detect_entry_setup(rows, cfg)
    timing = strategy.evaluate_timing(rows, cfg)
    check("m5_setup_and_trend", setup.ok and timing["ok"], f"{setup.reason} | {timing['reason']}")
    check("m5_signal_uses_last_closed", setup.signal_close_time == rows[-1].close_time)
    m5_future = rows + [_m5(60, 110.0, 111.0, 49.0, 50.0)]
    closed_timing = strategy.evaluate_timing(
        m5_future, cfg, signal_close_time_ms=rows[-1].close_time
    )
    check(
        "m5_closed_candle_cutoff",
        closed_timing["ok"] and not strategy.evaluate_timing(m5_future, cfg)["ok"],
    )

    order_cfg = dict(cfg)
    order_cfg["DIRECTION_FILTER_ENABLED"] = False
    order_cfg["DIRECTION_ZONE_FILTER_ENABLED"] = False
    diagnostics = ScanDiagnostics()
    selected = find_best_candidate(
        [
            {
                "symbol": "TESTUSDT",
                "priceChangePercent": "20",
                "quoteVolume": "1000000000",
                "lastPrice": "104.1",
            }
        ],
        lambda _symbol: falling_m5(),
        order_cfg,
        tradable_symbols={"TESTUSDT"},
        diagnostics=diagnostics,
    )
    check(
        "m5_trend_precedes_breakout",
        selected is None
        and diagnostics.rejection_counts.get("TIMING") == 1
        and not diagnostics.rejection_counts.get("BREAKOUT_RETEST"),
    )

    m15 = rising_m15()
    direction_ok = strategy.evaluate_direction(m15, cfg)
    check("m15_ema_adx", direction_ok["ok"], direction_ok["reason"])
    check(
        "m15_strict_ema_order",
        float(direction_ok.get("close") or 0.0)
        > float(direction_ok.get("ema_fast") or 0.0)
        > float(direction_ok.get("ema_slow") or 0.0),
    )
    check("m15_adx_25", float(direction_ok.get("adx") or 0.0) >= 25.0)
    future_m15 = m15 + [_m15(65, 108.0, 109.0, 79.0, 80.0)]
    closed_direction = strategy.evaluate_direction(
        future_m15, cfg, signal_close_time_ms=m15[-1].close_time
    )
    check(
        "m15_closed_candle_cutoff",
        closed_direction["ok"] and not strategy.evaluate_direction(future_m15, cfg)["ok"],
    )

    direction_down = strategy.evaluate_direction(falling_m15(), cfg)
    check("m15_rejects_wrong_trend", not direction_down["ok"], direction_down["reason"])

    strong_cfg = dict(cfg)
    strong_cfg["DIRECTION_ADX_MIN"] = 101.0
    direction_weak = strategy.evaluate_direction(m15, strong_cfg)
    check(
        "m15_adx_threshold_enforced",
        not direction_weak["ok"] and "ADX14" in direction_weak["reason"],
    )

    htf_cfg = {
        "HTF_FILTER_ENABLED": True,
        "HTF_INTERVAL": "1h",
        "HTF_EMA_FAST": 20,
        "HTF_EMA_SLOW": 50,
        "HTF_ADX_PERIOD": 14,
        "HTF_ADX_MIN": 25.0,
        "HTF_LOOKBACK_BARS": 60,
    }
    h1 = [_h1(index, 100.0 + index) for index in range(100)]
    htf_up = strategy.evaluate_htf(h1, htf_cfg)
    check("h1_ema_adx", htf_up["ok"], htf_up["reason"])
    check(
        "h1_strict_ema_order",
        float(htf_up.get("close") or 0.0)
        > float(htf_up.get("ema_fast") or 0.0)
        > float(htf_up.get("ema_slow") or 0.0),
    )
    check("h1_adx_min", float(htf_up.get("adx") or 0.0) >= 25.0)

    htf_resistance_cfg = {
        "HTF_FILTER_ENABLED": True,
        "HTF_INTERVAL": "1h",
        "HTF_RESISTANCE_FILTER_ENABLED": True,
        "HTF_RESISTANCE_LOOKBACK_BARS": 10,
        "HTF_RESISTANCE_PIVOT_WING_BARS": 2,
        "HTF_RESISTANCE_MIN_ROOM_PCT": 1.5,
    }

    def _resistance_candle(index: int, high: float, start_ms: int = 0) -> Kline:
        open_time = start_ms + index * 3_600_000
        close = 100.0 if high >= 100.0 else 99.0
        return Kline(
            open_time,
            close,
            high,
            close - 0.5,
            close,
            open_time + 3_599_999,
            1_000.0,
            100_000.0,
        )

    def _resistance_fixture(level: float, index: int = 4) -> list[Kline]:
        highs = [100.5] * 10
        highs[index] = level
        return [_resistance_candle(i, high) for i, high in enumerate(highs)]

    resistance_disabled = strategy.evaluate_htf_resistance(
        [],
        dict(htf_resistance_cfg, HTF_FILTER_ENABLED=False),
        signal_price=None,
    )
    check(
        "htf_resistance_disabled_when_trend_htf_off",
        resistance_disabled["ok"]
        and "nonaktif" in resistance_disabled["reason"]
        and strategy.htf_resistance_window_bars(
            dict(htf_resistance_cfg, HTF_FILTER_ENABLED=False)
        )
        == 0,
    )

    resistance_far_bars = _resistance_fixture(101.5)
    resistance_far = strategy.evaluate_htf_resistance(
        resistance_far_bars,
        htf_resistance_cfg,
        signal_price=100.0,
        signal_close_time_ms=resistance_far_bars[-1].close_time,
    )
    resistance_near_bars = _resistance_fixture(101.49)
    resistance_near = strategy.evaluate_htf_resistance(
        resistance_near_bars,
        htf_resistance_cfg,
        signal_price=100.0,
        signal_close_time_ms=resistance_near_bars[-1].close_time,
    )
    check(
        "htf_resistance_pivot_room_and_threshold_boundary",
        resistance_far["ok"]
        and abs(resistance_far.get("distance_pct", 0.0) - 1.5) < 1e-9
        and not resistance_near["ok"]
        and resistance_near.get("distance_pct", 2.0) < 1.5,
        f"batas={resistance_far.get('distance_pct')}, "
        f"dekat={resistance_near.get('distance_pct')}",
    )

    resistance_below_bars = [
        _resistance_candle(index, 99.5) for index in range(10)
    ]
    resistance_below = strategy.evaluate_htf_resistance(
        resistance_below_bars,
        htf_resistance_cfg,
        signal_price=100.0,
        signal_close_time_ms=resistance_below_bars[-1].close_time,
    )
    check(
        "htf_resistance_no_overhead_level_passes",
        resistance_below["ok"]
        and resistance_below.get("resistance_price") is None,
    )

    future_resistance_bars = [
        _resistance_candle(index, 101.0 if index == 9 else 99.5)
        for index in range(12)
    ]
    future_resistance = strategy.evaluate_htf_resistance(
        future_resistance_bars,
        htf_resistance_cfg,
        signal_price=100.0,
        signal_close_time_ms=future_resistance_bars[9].close_time,
    )
    check(
        "htf_resistance_does_not_use_unconfirmed_future_pivot",
        future_resistance["ok"]
        and future_resistance.get("resistance_price") is None
        and future_resistance.get("bars") == 10,
    )
    resistance_lookup = parity.HtfLookup(
        resistance_near_bars, htf_resistance_cfg
    )
    resistance_lookup_result = resistance_lookup.resistance_at(
        resistance_near_bars[-1].close_time, 100.0
    )
    resistance_lookup_future = parity.HtfLookup(
        future_resistance_bars, htf_resistance_cfg
    ).resistance_at(future_resistance_bars[9].close_time, 100.0)
    check(
        "backtest_resistance_lookup_matches_live_and_cuts_future_pivots",
        not resistance_lookup_result["ok"]
        and all(
            resistance_lookup_result.get(key) == resistance_near.get(key)
            for key in (
                "ok",
                "reason",
                "bars",
                "resistance_price",
                "signal_price",
                "distance_pct",
            )
        )
        and resistance_lookup_future["ok"]
        and resistance_lookup_future.get("resistance_price") is None
        and len(resistance_lookup.window_at(future_resistance_bars[9].close_time)) == 10,
        f"langsung={resistance_near['reason']}; lookup={resistance_lookup_result['reason']}",
    )

    stale_resistance_bars = [
        _resistance_candle(index, 100.5) for index in range(10)
    ]
    stale_resistance = strategy.evaluate_htf_resistance(
        stale_resistance_bars,
        htf_resistance_cfg,
        signal_price=100.0,
        signal_close_time_ms=11 * 3_600_000 - 1,
    )
    gap_resistance_bars = [
        _resistance_candle(index, 100.5)
        for index in range(11)
        if index != 5
    ]
    gap_resistance = strategy.evaluate_htf_resistance(
        gap_resistance_bars,
        htf_resistance_cfg,
        signal_price=100.0,
        signal_close_time_ms=gap_resistance_bars[-1].close_time,
    )
    check(
        "htf_resistance_rejects_stale_or_gapped_data",
        not stale_resistance["ok"]
        and "terbaru tidak tersedia" in stale_resistance["reason"]
        and not gap_resistance["ok"]
        and "celah" in gap_resistance["reason"],
        f"stale={stale_resistance['reason']}; gap={gap_resistance['reason']}",
    )

    future_h1 = h1 + [_h1(100, 2.0)]
    closed_htf = strategy.evaluate_htf(
        future_h1, htf_cfg, signal_close_time_ms=h1[-1].close_time
    )
    check(
        "h1_closed_candle_cutoff",
        closed_htf["ok"]
        and closed_htf["close"] == htf_up["close"]
        and not strategy.evaluate_htf(future_h1, htf_cfg)["ok"],
    )

    h1_down = [_h1(index, 200.0 - index) for index in range(100)]
    check("h1_rejects_wrong_trend", not strategy.evaluate_htf(h1_down, htf_cfg)["ok"])
    h1_weak = [
        _h1(index, 100.0 + index * 0.02 + (0.6 if index % 2 else -0.6))
        for index in range(100)
    ]
    weak_htf = strategy.evaluate_htf(h1_weak, htf_cfg)
    check(
        "h1_adx_threshold_enforced",
        not weak_htf["ok"] and "ADX14" in weak_htf["reason"],
        weak_htf["reason"],
    )
    short_htf = strategy.evaluate_htf(h1[:20], htf_cfg)
    check(
        "h1_insufficient_history_fails_closed",
        not short_htf["ok"] and "kurang" in short_htf["reason"],
        short_htf["reason"],
    )
    inactive_htf_cfg = dict(htf_cfg, HTF_FILTER_ENABLED=False)
    check(
        "h1_disabled_does_not_block",
        strategy.evaluate_htf([], inactive_htf_cfg)["ok"],
    )

    warmup_cfg = dict(cfg, HTF_FILTER_ENABLED=False, HTF_LOOKBACK_BARS=999)
    expected_old_warmup = strategy.direction_warmup_bars(warmup_cfg, "5m")
    expected_old_history = (
        strategy.direction_window_bars(warmup_cfg)
        * strategy.INTERVAL_MINUTES[strategy.direction_interval(warmup_cfg)]
        * 60_000
    )
    check(
        "h1_disabled_warmup_matches_previous",
        strategy.gate_warmup_bars(warmup_cfg, "5m") == expected_old_warmup
        and strategy.native_history_ms(warmup_cfg) == expected_old_history,
    )
    enabled_warmup_cfg = dict(warmup_cfg, **htf_cfg)
    expected_max_warmup = max(
        strategy.direction_warmup_bars(enabled_warmup_cfg, "5m"),
        strategy.htf_warmup_bars(enabled_warmup_cfg, "5m"),
    )
    expected_max_history = max(
        expected_old_history,
        strategy.htf_window_bars(enabled_warmup_cfg)
        * strategy.INTERVAL_MINUTES[strategy.htf_interval(enabled_warmup_cfg)]
        * 60_000,
    )
    check(
        "h1_warmup_and_native_history_take_max",
        strategy.gate_warmup_bars(enabled_warmup_cfg, "5m") == expected_max_warmup
        and strategy.native_history_ms(enabled_warmup_cfg) == expected_max_history,
    )
    resistance_warmup_cfg = {
        "HTF_FILTER_ENABLED": True,
        "HTF_INTERVAL": "1h",
        "HTF_EMA_FAST": 2,
        "HTF_EMA_SLOW": 3,
        "HTF_ADX_PERIOD": 2,
        "HTF_ADX_MIN": 0.0,
        "HTF_LOOKBACK_BARS": 20,
        "HTF_RESISTANCE_FILTER_ENABLED": True,
        "HTF_RESISTANCE_LOOKBACK_BARS": 20,
        "HTF_RESISTANCE_PIVOT_WING_BARS": 2,
        "HTF_RESISTANCE_MIN_ROOM_PCT": 1.5,
    }
    check(
        "resistance_lookback_is_included_in_htf_warmup_without_extra_request",
        strategy.htf_window_bars(resistance_warmup_cfg) >= 20
        and strategy.htf_warmup_bars(resistance_warmup_cfg, "5m")
        == (max(strategy.htf_window_bars(resistance_warmup_cfg), 20) + 1) * 12 + 12
        and strategy.htf_window_bars(
            dict(resistance_warmup_cfg, HTF_RESISTANCE_FILTER_ENABLED=False)
        )
        == 20,
    )

    htf_scan_cfg = dict(cfg, **htf_cfg)
    htf_scan_cfg["DIRECTION_FILTER_ENABLED"] = False
    htf_scan_cfg["DIRECTION_ZONE_FILTER_ENABLED"] = False
    signal_ticker = [
        {
            "symbol": "HTFUSDT",
            "priceChangePercent": "18",
            "quoteVolume": "1000000000",
            "lastPrice": "110.8",
        }
    ]
    m15_after_htf: list[str] = []
    m5_after_htf: list[str] = []
    rejected_htf_diag = ScanDiagnostics()
    htf_order_cfg = dict(htf_scan_cfg, DIRECTION_FILTER_ENABLED=True)
    rejected_htf = find_best_candidate(
        signal_ticker,
        lambda symbol: m5_after_htf.append(symbol) or rows,
        htf_order_cfg,
        tradable_symbols={"HTFUSDT"},
        direction_provider=lambda symbol: m15_after_htf.append(symbol) or m15,
        diagnostics=rejected_htf_diag,
        confirm_close_time_ms=h1_down[-1].close_time,
        htf_provider=lambda _symbol: h1_down,
    )
    check(
        "scanner_htf_rejects_before_m15_m5",
        rejected_htf is None
        and not m15_after_htf
        and not m5_after_htf
        and rejected_htf_diag.rejection_counts.get("HTF_TREND") == 1,
        str(rejected_htf_diag.rejection_counts),
    )

    future_up_h1 = [_h1(index, 101.0 + index - 100) for index in range(100, 200)]
    future_series_h1 = h1_down + future_up_h1
    check(
        "h1_future_fixture_would_flip_trend",
        strategy.evaluate_htf(future_series_h1, htf_cfg)["ok"],
    )
    cutoff_htf_diag = ScanDiagnostics()
    cutoff_htf = find_best_candidate(
        signal_ticker,
        lambda _symbol: rows,
        htf_scan_cfg,
        tradable_symbols={"HTFUSDT"},
        diagnostics=cutoff_htf_diag,
        confirm_close_time_ms=h1_down[-1].close_time,
        htf_provider=lambda _symbol: future_series_h1,
    )
    check(
        "scanner_htf_never_uses_candle_after_signal",
        cutoff_htf is None
        and cutoff_htf_diag.rejection_counts.get("HTF_TREND") == 1,
        str(cutoff_htf_diag.examples.get("HTF_TREND")),
    )

    missing_htf_diag = ScanDiagnostics()
    missing_htf = find_best_candidate(
        signal_ticker,
        lambda _symbol: rows,
        htf_scan_cfg,
        tradable_symbols={"HTFUSDT"},
        diagnostics=missing_htf_diag,
        htf_provider=None,
    )
    htf_log_lines = missing_htf_diag.log_lines(htf_scan_cfg)
    check(
        "scanner_htf_missing_provider_fails_closed_and_logs",
        missing_htf is None
        and missing_htf_diag.rejection_counts.get("HTF_TREND") == 1
        and any("Status gerbang tren HTF 1h: AKTIF" in line for line in htf_log_lines)
        and any("Penolakan tren 1h" in line for line in htf_log_lines),
        " | ".join(htf_log_lines),
    )

    passing_htf_diag = ScanDiagnostics()
    passing_htf = find_best_candidate(
        signal_ticker,
        lambda _symbol: rows,
        htf_scan_cfg,
        tradable_symbols={"HTFUSDT"},
        diagnostics=passing_htf_diag,
        confirm_close_time_ms=h1[-1].close_time,
        htf_provider=lambda _symbol: h1,
    )
    check(
        "scanner_htf_passes_before_lower_timeframes",
        passing_htf is not None
        and passing_htf.confirmed
        and passing_htf.htf_trend is not None
        and passing_htf.htf_trend.get("ok")
        and "tren HTF 1h" in passing_htf.confirm_reason,
        passing_htf.confirm_reason if passing_htf else "tidak ada kandidat",
    )
    three_timeframe_cfg = dict(cfg, **htf_cfg)
    three_timeframe = find_best_candidate(
        signal_ticker,
        lambda _symbol: rows,
        three_timeframe_cfg,
        tradable_symbols={"HTFUSDT"},
        direction_provider=lambda _symbol: m15,
        htf_provider=lambda _symbol: h1,
        confirm_close_time_ms=None,
    )
    check(
        "scanner_preserves_htf_m15_and_m5_reason_chain",
        three_timeframe is not None
        and three_timeframe.confirmed
        and three_timeframe.htf_trend is not None
        and three_timeframe.direction is not None
        and three_timeframe.direction_zone is not None
        and "tren HTF 1h" in three_timeframe.confirm_reason
        and "arah 15m" in three_timeframe.confirm_reason
        and "demand zone" in three_timeframe.confirm_reason
        and "timing 5m" in three_timeframe.confirm_reason,
        three_timeframe.confirm_reason if three_timeframe else "tidak ada kandidat",
    )

    class _FakeKlineClient:
        def __init__(self, raw=None, error=None):
            self.raw = list(raw or [])
            self.error = error
            self.calls = 0

        def get_klines(self, _symbol, _interval, *, limit):
            self.calls += 1
            if self.error is not None:
                raise self.error
            return self.raw[-limit:]

    from decimal import Decimal
    import os
    import tempfile
    import time
    from unittest.mock import patch

    from config.config import require_valid_mode
    from trading.clients.binance_client import SymbolFilters
    from trading.paper.paper_engine import PaperMatchingEngine
    from trading.paper.paper_store import PaperStore

    paper_cfg = deepcopy(PUMP_CONFIG)
    paper_cfg.update(
        {
            "MODE": "PAPER",
            "HTF_FILTER_ENABLED": True,
            "HTF_INTERVAL": "1h",
            "HTF_EMA_FAST": 20,
            "HTF_EMA_SLOW": 50,
            "HTF_ADX_PERIOD": 14,
            "HTF_ADX_MIN": 30.0,
            "HTF_LOOKBACK_BARS": 60,
            "DIRECTION_FILTER_ENABLED": False,
            "DIRECTION_ZONE_FILTER_ENABLED": False,
            "TIMING_FILTER_ENABLED": False,
            "DEMAND_ZONE_FILTER_ENABLED": False,
            "ROLLING_VOLUME_FILTER_ENABLED": False,
            "PUMP_MIN_24H_CHANGE_PCT": 0.0,
            "MIN_QUOTE_VOLUME_USDT_24H": 1.0,
            "TOP_N_CANDIDATES_TO_CONFIRM": 1,
        }
    )
    paper_mode = require_valid_mode(paper_cfg)
    paper_now_ms = int(time.time() * 1000)
    paper_m5_open = (paper_now_ms // 300_000) * 300_000
    paper_signal_close_ms = paper_m5_open - 1
    m5_offset_ms = paper_signal_close_ms - rows[-1].close_time
    paper_m5 = [
        Kline(
            open_time=k.open_time + m5_offset_ms,
            open=k.open,
            high=k.high,
            low=k.low,
            close=k.close,
            close_time=k.close_time + m5_offset_ms,
            volume=k.volume,
            quote_volume=k.quote_volume,
        )
        for k in rows
    ]
    paper_last_h1_open = (
        (paper_now_ms // 3_600_000) - 1
    ) * 3_600_000

    def _paper_h1_raw(open_time: int, close: float) -> list:
        return [
            open_time,
            close - 0.1,
            close + 0.5,
            close - 0.5,
            close,
            1_000.0,
            open_time + 3_600_000 - 1,
            100_000.0,
        ]

    paper_h1_up_raw = [
        _paper_h1_raw(
            paper_last_h1_open - (99 - index) * 3_600_000,
            100.0 + index * 0.1,
        )
        for index in range(100)
    ]
    paper_h1_down_raw = [
        _paper_h1_raw(
            paper_last_h1_open - (99 - index) * 3_600_000,
            110.0 - index * 0.1,
        )
        for index in range(100)
    ]
    paper_ticker = [
        {
            "symbol": "HTFUSDT",
            "priceChangePercent": "18",
            "quoteVolume": "1000000000",
            "lastPrice": str(paper_m5[-1].close),
        }
    ]
    paper_h1_client = _FakeKlineClient(paper_h1_up_raw)
    paper_h1_cache = HtfTrendCache(paper_h1_client, paper_cfg)
    paper_scan_diagnostics = ScanDiagnostics()
    paper_m5_calls: list[str] = []
    with patch(
        "trading.pump_scanner_bot.state_mod.now_ms",
        return_value=paper_now_ms,
    ):
        paper_h1_cache.window_klines("HTFUSDT", paper_now_ms)
        paper_candidate = find_best_candidate(
            paper_ticker,
            lambda symbol: paper_m5_calls.append(symbol) or paper_m5,
            paper_cfg,
            tradable_symbols={"HTFUSDT"},
            diagnostics=paper_scan_diagnostics,
            confirm_close_time_ms=paper_signal_close_ms,
            htf_provider=paper_h1_cache.provider,
        )

    paper_down_client = _FakeKlineClient(paper_h1_down_raw)
    paper_down_cache = HtfTrendCache(paper_down_client, paper_cfg)
    paper_down_m5_calls: list[str] = []
    paper_down_diagnostics = ScanDiagnostics()
    with patch(
        "trading.pump_scanner_bot.state_mod.now_ms",
        return_value=paper_now_ms,
    ):
        paper_down_candidate = find_best_candidate(
            paper_ticker,
            lambda symbol: paper_down_m5_calls.append(symbol) or paper_m5,
            paper_cfg,
            tradable_symbols={"HTFUSDT"},
            diagnostics=paper_down_diagnostics,
            confirm_close_time_ms=paper_signal_close_ms,
            htf_provider=paper_down_cache.provider,
        )

    paper_stale_client = _FakeKlineClient(paper_h1_up_raw[:-1])
    paper_stale_cache = HtfTrendCache(paper_stale_client, paper_cfg)
    paper_stale_m5_calls: list[str] = []
    paper_stale_diagnostics = ScanDiagnostics()
    with patch(
        "trading.pump_scanner_bot.state_mod.now_ms",
        return_value=paper_now_ms,
    ):
        paper_stale_candidate = find_best_candidate(
            paper_ticker,
            lambda symbol: paper_stale_m5_calls.append(symbol) or paper_m5,
            paper_cfg,
            tradable_symbols={"HTFUSDT"},
            diagnostics=paper_stale_diagnostics,
            confirm_close_time_ms=paper_signal_close_ms,
            htf_provider=paper_stale_cache.provider,
        )

    paper_store_path = os.path.join(
        tempfile.gettempdir(), f"arena-paper-memory-{os.getpid()}.json"
    )

    class _InMemoryPaperStore(PaperStore):
        def __init__(self, path: str) -> None:
            self.save_attempts = 0
            super().__init__(path, {"USDT": 1_000.0})

        def _load_or_init(self) -> None:
            self.state = self._default_state()

        def _save_locked(self) -> None:
            self.save_attempts += 1

    paper_store = _InMemoryPaperStore(paper_store_path)
    paper_filters = SymbolFilters(
        Decimal("0.001"),
        Decimal("0.001"),
        Decimal("5"),
        Decimal("0.01"),
        order_types={"MARKET"},
    )
    paper_engine = PaperMatchingEngine(
        config=paper_cfg,
        store=paper_store,
        filters_provider=lambda _symbol: paper_filters,
        depth_provider=lambda _symbol: {
            "asks": [["100.00", "10.0"]],
            "bids": [["99.90", "10.0"]],
        },
        price_provider=lambda _symbol: 100.0,
        quote_asset="USDT",
    )
    paper_order = (
        paper_engine.place_order(
            "HTFUSDT",
            "BUY",
            "MARKET",
            quote_order_qty=50.0,
            client_order_id="mock-htf-paper",
        )
        if paper_candidate is not None and paper_candidate.confirmed
        else None
    )
    check(
        "paper_mode_memori_gate_mengizinkan_h1_lulus_dan_menolak_data_buruk",
        paper_mode == "PAPER"
        and paper_candidate is not None
        and paper_candidate.confirmed
        and paper_candidate.htf_trend is not None
        and paper_candidate.htf_trend.get("ok")
        and paper_h1_client.calls == 1
        and paper_down_candidate is None
        and paper_down_diagnostics.rejection_counts.get("HTF_TREND") == 1
        and not paper_down_m5_calls
        and paper_stale_candidate is None
        and paper_stale_diagnostics.rejection_counts.get("HTF_TREND") == 1
        and not paper_stale_m5_calls
        and bool(paper_m5_calls),
        f"lulus={paper_candidate is not None}, m5_lulus={len(paper_m5_calls)}, "
        f"bearish_m5={len(paper_down_m5_calls)}, stale_m5={len(paper_stale_m5_calls)}",
    )
    check(
        "paper_mock_order_terisi_hanya_di_memori_tanpa_file_akun",
        paper_order is not None
        and paper_order.get("status") == "FILLED"
        and paper_store.save_attempts == 1
        and len(paper_store.state.get("trade_history", [])) == 1
        and not os.path.exists(paper_store_path),
        (
            f"status={paper_order.get('status') if paper_order else None}, "
            f"persisten={os.path.exists(paper_store_path)}"
        ),
    )

    paper_resistance_cfg = dict(
        paper_cfg,
        HTF_RESISTANCE_FILTER_ENABLED=True,
        HTF_RESISTANCE_LOOKBACK_BARS=60,
        HTF_RESISTANCE_PIVOT_WING_BARS=2,
        HTF_RESISTANCE_MIN_ROOM_PCT=1.5,
    )

    def _paper_h1_with_resistance(level: float) -> list[Kline]:
        raw = [list(candle) for candle in paper_h1_up_raw]
        raw[75][2] = float(level)
        return strategy.parse_klines(raw)

    paper_far_h1_calls: list[str] = []
    paper_far_m5_calls: list[str] = []
    paper_far_diagnostics = ScanDiagnostics()
    paper_far_candidate = find_best_candidate(
        paper_ticker,
        lambda symbol: paper_far_m5_calls.append(symbol) or paper_m5,
        paper_resistance_cfg,
        tradable_symbols={"HTFUSDT"},
        diagnostics=paper_far_diagnostics,
        confirm_close_time_ms=paper_signal_close_ms,
        htf_provider=lambda symbol: paper_far_h1_calls.append(symbol)
        or _paper_h1_with_resistance(112.6),
    )

    paper_near_h1_calls: list[str] = []
    paper_near_m5_calls: list[str] = []
    paper_near_diagnostics = ScanDiagnostics()
    paper_near_candidate = find_best_candidate(
        paper_ticker,
        lambda symbol: paper_near_m5_calls.append(symbol) or paper_m5,
        paper_resistance_cfg,
        tradable_symbols={"HTFUSDT"},
        diagnostics=paper_near_diagnostics,
        confirm_close_time_ms=paper_signal_close_ms,
        htf_provider=lambda symbol: paper_near_h1_calls.append(symbol)
        or _paper_h1_with_resistance(112.0),
    )
    paper_near_log_lines = paper_near_diagnostics.log_lines(
        paper_resistance_cfg
    )
    check(
        "scanner_htf_resistance_uses_closed_m5_price_and_one_h1_fetch",
        paper_far_candidate is not None
        and paper_far_candidate.confirmed
        and paper_far_candidate.htf_resistance is not None
        and paper_far_candidate.htf_resistance.get("ok")
        and paper_far_candidate.htf_resistance.get("signal_price")
        == paper_m5[-1].close
        and paper_far_candidate.htf_resistance.get("distance_pct", 0.0) >= 1.5
        and paper_far_h1_calls == ["HTFUSDT"]
        and paper_far_m5_calls == ["HTFUSDT"],
        (
            f"ruang={paper_far_candidate.htf_resistance.get('distance_pct') if paper_far_candidate and paper_far_candidate.htf_resistance else None}, "
            f"H1={len(paper_far_h1_calls)}, M5={len(paper_far_m5_calls)}"
        ),
    )
    check(
        "scanner_htf_resistance_rejects_near_pivot_and_reports_reason",
        paper_near_candidate is None
        and paper_near_diagnostics.rejection_counts.get("HTF_RESISTANCE") == 1
        and paper_near_h1_calls == ["HTFUSDT"]
        and paper_near_m5_calls == ["HTFUSDT"]
        and any("Penolakan resistance 1h" in line for line in paper_near_log_lines),
        f"H1={len(paper_near_h1_calls)}, M5={len(paper_near_m5_calls)}",
    )

    paper_off_resistance_cfg = dict(
        paper_resistance_cfg, HTF_FILTER_ENABLED=False
    )
    paper_off_h1_calls: list[str] = []
    paper_off_candidate = find_best_candidate(
        paper_ticker,
        lambda _symbol: paper_m5,
        paper_off_resistance_cfg,
        tradable_symbols={"HTFUSDT"},
        confirm_close_time_ms=paper_signal_close_ms,
        htf_provider=lambda symbol: paper_off_h1_calls.append(symbol)
        or _paper_h1_with_resistance(112.0),
    )
    check(
        "scanner_htf_off_skips_resistance_provider_and_preserves_entry",
        paper_off_candidate is not None
        and paper_off_candidate.confirmed
        and paper_off_candidate.htf_resistance is None
        and not paper_off_h1_calls,
        str(paper_off_h1_calls),
    )

    cache_cfg = dict(htf_cfg, MARKET_SCAN_INTERVAL_SECONDS=30)
    cache_raw = [_raw_h1(index, 100.0 + index) for index in range(101)]
    cache_client = _FakeKlineClient(cache_raw)
    h1_cache = HtfTrendCache(cache_client, cache_cfg)
    now_first_h1 = 100 * 3_600_000 + 30 * 60_000
    cached_h1_first = h1_cache.window_klines("CACHEUSDT", now_first_h1)
    cached_h1_same_bar = h1_cache.window_klines(
        "CACHEUSDT", now_first_h1 + 10 * 60_000
    )
    check(
        "live_htf_cache_excludes_running_candle_and_reuses_symbol_window",
        len(cached_h1_first) == 60
        and cached_h1_first[-1].open_time == 99 * 3_600_000
        and all(k.close_time < now_first_h1 for k in cached_h1_first)
        and [k.open_time for k in cached_h1_same_bar] == [
            k.open_time for k in cached_h1_first
        ]
        and cache_client.calls == 1,
        f"bars={len(cached_h1_first)}, calls={cache_client.calls}",
    )
    cache_client.raw = [_raw_h1(index, 100.0 + index) for index in range(102)]
    cached_h1_next_bar = h1_cache.window_klines(
        "CACHEUSDT", 101 * 3_600_000 + 1
    )
    check(
        "live_htf_cache_refreshes_after_closed_hour",
        cache_client.calls == 2
        and cached_h1_next_bar[-1].open_time == 100 * 3_600_000,
        f"calls={cache_client.calls}",
    )

    stale_client = _FakeKlineClient(
        [_raw_h1(index, 100.0 + index) for index in range(99)]
    )
    stale_cache = HtfTrendCache(stale_client, cache_cfg)
    stale_first = stale_second = False
    try:
        stale_cache.window_klines("STALEUSDT", now_first_h1)
    except RuntimeError as exc:
        stale_first = "basi" in str(exc)
    try:
        stale_cache.window_klines("STALEUSDT", now_first_h1)
    except RuntimeError as exc:
        stale_second = "basi" in str(exc)
    check(
        "live_htf_cache_fails_closed_and_caches_stale_window",
        stale_first and stale_second and stale_client.calls == 1,
        f"calls={stale_client.calls}",
    )

    empty_client = _FakeKlineClient([])
    empty_cache = HtfTrendCache(empty_client, cache_cfg)
    empty_first = empty_cache.window_klines("EMPTYUSDT", now_first_h1)
    empty_second = empty_cache.window_klines("EMPTYUSDT", now_first_h1)
    check(
        "live_htf_cache_negative_caches_empty_data",
        not empty_first and not empty_second and empty_client.calls == 1,
        f"calls={empty_client.calls}",
    )

    error_client = _FakeKlineClient(error=RuntimeError("gangguan sementara"))
    error_cache = HtfTrendCache(error_client, cache_cfg)
    error_first = error_second = False
    try:
        error_cache.window_klines("ERRORUSDT", now_first_h1)
    except RuntimeError:
        error_first = True
    try:
        error_cache.window_klines("ERRORUSDT", now_first_h1)
    except RuntimeError:
        error_second = True
    check(
        "live_htf_cache_negative_caches_fetch_errors",
        error_first and error_second and error_client.calls == 1,
        f"calls={error_client.calls}",
    )

    htf_parity_cfg = {
        "HTF_FILTER_ENABLED": True,
        "HTF_INTERVAL": "1h",
        "HTF_EMA_FAST": 2,
        "HTF_EMA_SLOW": 3,
        "HTF_ADX_PERIOD": 2,
        "HTF_ADX_MIN": 0.0,
        "HTF_LOOKBACK_BARS": 20,
    }
    m5_with_future_flip = _m5_from_hourly_closes(
        [100.0 + index for index in range(80)] + [50.0]
    )
    h1_from_m5 = parity.build_htf_klines(
        m5_with_future_flip, htf_parity_cfg, "5m"
    )
    mid_hour_signal_close = m5_with_future_flip[80 * 12 + 5].close_time
    live_parity = strategy.evaluate_htf(
        h1_from_m5, htf_parity_cfg, signal_close_time_ms=mid_hour_signal_close
    )
    htf_lookup = parity.make_htf_lookup(
        h1_from_m5, htf_parity_cfg, "5m", sudah_dirangkai=True
    )
    backtest_parity = htf_lookup.verdict_at(mid_hour_signal_close)
    h1_with_future_candle = strategy.evaluate_htf(h1_from_m5, htf_parity_cfg)
    check(
        "backtest_h1_aggregator_keeps_only_complete_hourly_bars",
        len(h1_from_m5) == 81
        and all(
            candle.close_time == candle.open_time + 3_600_000 - 1
            for candle in h1_from_m5
        ),
        f"jumlah={len(h1_from_m5)}",
    )
    check(
        "live_backtest_htf_parity_and_no_lookahead",
        live_parity["ok"]
        and backtest_parity["ok"]
        and not h1_with_future_candle["ok"]
        and all(
            live_parity.get(key) == backtest_parity.get(key)
            for key in ("ok", "reason", "bars", "close", "ema_fast", "ema_slow", "adx")
        )
        and htf_lookup.window_at(mid_hour_signal_close)[-1].open_time
        == 79 * 3_600_000,
        f"live={live_parity['reason']} | backtest={backtest_parity['reason']}",
    )

    htf_latest_missing = parity.make_htf_lookup(
        h1_from_m5[:-1], htf_parity_cfg, "5m", sudah_dirangkai=True
    ).verdict_at(81 * 3_600_000 + 299_999)
    short_htf_lookup = parity.HtfLookup(
        [_h1(index, 100.0 + index) for index in range(4)], htf_parity_cfg
    )
    htf_too_short = short_htf_lookup.verdict_at(
        3 * 3_600_000 + 3_599_999
    )
    h1_with_gap = [
        _h1(index, 100.0 + index)
        for index in range(25)
        if index != 12
    ]
    htf_gap = strategy.evaluate_htf(
        h1_with_gap,
        htf_parity_cfg,
        signal_close_time_ms=h1_with_gap[-1].close_time,
    )
    check(
        "backtest_htf_rejects_missing_latest_closed_hour",
        not htf_latest_missing["ok"]
        and "terbaru tidak tersedia" in htf_latest_missing["reason"],
        htf_latest_missing["reason"],
    )
    check(
        "backtest_htf_rejects_insufficient_history",
        not htf_too_short["ok"] and "kurang" in htf_too_short["reason"],
        htf_too_short["reason"],
    )
    check(
        "live_backtest_htf_rejects_internal_data_gap",
        not htf_gap["ok"] and "celah" in htf_gap["reason"],
        htf_gap["reason"],
    )

    m5_rising_htf = _m5_from_hourly_closes(
        [100.0 + index for index in range(90)]
    )
    h1_rising_htf = parity.build_htf_klines(
        m5_rising_htf, htf_parity_cfg, "5m"
    )
    bt_cfg_htf = dict(
        deepcopy(PUMP_CONFIG),
        **htf_parity_cfg,
        DIRECTION_FILTER_ENABLED=False,
        DIRECTION_ZONE_FILTER_ENABLED=False,
        TIMING_FILTER_ENABLED=False,
        BREAKOUT_RETEST_FILTER_ENABLED=False,
        PUMP_MIN_24H_CHANGE_PCT=0.0,
        MIN_QUOTE_VOLUME_USDT_24H=1_000.0,
        TOP_N_CANDIDATES_TO_CONFIRM=1,
        BACKTEST_INITIAL_EQUITY_USDT=1_000.0,
        USE_EQUITY_STOP=False,
        _symbol="ETHUSDT",
    )
    bt_missing_htf = bt.run_backtest(
        m5_rising_htf, bt_cfg_htf, warmup_bars=0, htf_klines=[]
    )
    bt_missing_summary = bt.summarize(bt_missing_htf)
    bt_passing_htf = bt.run_backtest(
        m5_rising_htf, bt_cfg_htf, warmup_bars=0, htf_klines=h1_rising_htf
    )
    bt_passing_summary = bt.summarize(bt_passing_htf)
    check(
        "single_backtest_htf_fails_closed_and_reports_scan_skip",
        bt_missing_summary["htf_scans"] > 0
        and bt_missing_summary["htf_skips"] == bt_missing_summary["htf_scans"]
        and bt_missing_summary["total_trades"] == 0,
        str(
            (
                bt_missing_summary["htf_scans"],
                bt_missing_summary["htf_skips"],
                bt_missing_summary["total_trades"],
            )
        ),
    )
    check(
        "single_backtest_uses_aggregated_closed_h1",
        bt_passing_summary["htf_scans"] > 0
        and bt_passing_summary["htf_skips"] == 0,
        str(
            (
                bt_passing_summary["htf_scans"],
                bt_passing_summary["htf_skips"],
            )
        ),
    )
    m5_resistance_backtest = _m5_from_hourly_closes(
        [100.0 + index for index in range(90)]
    )
    resistance_spike_index = 20 * 12 + 6
    resistance_spike_candle = m5_resistance_backtest[resistance_spike_index]
    m5_resistance_backtest[resistance_spike_index] = Kline(
        resistance_spike_candle.open_time,
        resistance_spike_candle.open,
        124.5,
        resistance_spike_candle.low,
        resistance_spike_candle.close,
        resistance_spike_candle.close_time,
        resistance_spike_candle.volume,
        resistance_spike_candle.quote_volume,
    )
    bt_cfg_resistance = dict(
        bt_cfg_htf,
        HTF_RESISTANCE_FILTER_ENABLED=True,
        HTF_RESISTANCE_LOOKBACK_BARS=20,
        HTF_RESISTANCE_PIVOT_WING_BARS=2,
        HTF_RESISTANCE_MIN_ROOM_PCT=1.5,
    )
    h1_resistance_backtest = parity.build_htf_klines(
        m5_resistance_backtest, bt_cfg_resistance, "5m"
    )
    bt_resistance_result = bt.run_backtest(
        m5_resistance_backtest,
        bt_cfg_resistance,
        warmup_bars=0,
        htf_klines=h1_resistance_backtest,
    )
    bt_resistance_summary = bt.summarize(bt_resistance_result)
    check(
        "single_backtest_applies_resistance_to_closed_m5_price",
        bt_resistance_summary["htf_resistance_scans"] > 0
        and bt_resistance_summary["htf_resistance_skips"] > 0
        and any(
            "filter resistance HTF" in warning
            for warning in bt_resistance_result.warnings
        ),
        f"scan={bt_resistance_summary['htf_resistance_scans']}, "
        f"skip={bt_resistance_summary['htf_resistance_skips']}",
    )
    grid_resistance_result = gs.run_grid_search(
        m5_resistance_backtest,
        bt_cfg_resistance,
        {"MAX_HOLD_HOURS": [24.0]},
        warmup_bars=0,
        rasio_latih=0.7,
        min_trades=1,
    )
    grid_resistance_item = (
        grid_resistance_result.hasil[0] if grid_resistance_result.hasil else None
    )
    check(
        "single_grid_search_carries_resistance_metrics_into_train_and_test",
        grid_resistance_item is not None
        and grid_resistance_item.latih.get("htf_resistance_scans", 0) > 0
        and grid_resistance_item.latih.get("htf_resistance_skips", 0) > 0
        and grid_resistance_item.uji is not None
        and grid_resistance_item.uji.get("htf_resistance_scans", 0) > 0,
        (
            f"latih={grid_resistance_item.latih.get('htf_resistance_skips', 0)}/"
            f"{grid_resistance_item.latih.get('htf_resistance_scans', 0)}, "
            f"uji={grid_resistance_item.uji.get('htf_resistance_skips', 0)}/"
            f"{grid_resistance_item.uji.get('htf_resistance_scans', 0)}"
            if grid_resistance_item is not None and grid_resistance_item.uji
            else "kombinasi tidak menghasilkan ringkasan lengkap"
        ),
    )
    cfg_htf_off = dict(bt_cfg_htf, HTF_FILTER_ENABLED=False)
    bt_htf_off_implicit = bt.run_backtest(
        m5_rising_htf, cfg_htf_off, warmup_bars=0
    )
    bt_htf_off_explicit = bt.run_backtest(
        m5_rising_htf, cfg_htf_off, warmup_bars=0, htf_klines=[]
    )
    check(
        "htf_off_preserves_single_backtest_behavior",
        bt_htf_off_implicit.warnings == bt_htf_off_explicit.warnings
        and [t.entry_time for t in bt_htf_off_implicit.trades]
        == [t.entry_time for t in bt_htf_off_explicit.trades]
        and bt.summarize(bt_htf_off_implicit)["htf_scans"] == 0
        and bt.summarize(bt_htf_off_explicit)["htf_scans"] == 0,
    )
    cfg_htf_off_resistance = dict(
        cfg_htf_off, HTF_RESISTANCE_FILTER_ENABLED=True
    )
    bt_htf_off_resistance = bt.run_backtest(
        m5_rising_htf, cfg_htf_off_resistance, warmup_bars=0
    )
    check(
        "single_backtest_resistance_has_no_effect_when_htf_off",
        bt.summarize(bt_htf_off_resistance)["htf_resistance_scans"] == 0
        and bt.summarize(bt_htf_off_resistance)["htf_resistance_skips"] == 0
        and [t.entry_time for t in bt_htf_off_resistance.trades]
        == [t.entry_time for t in bt_htf_off_implicit.trades],
    )
    grid_htf_result = gs.run_grid_search(
        m5_rising_htf,
        bt_cfg_htf,
        {"MAX_HOLD_HOURS": [24.0]},
        warmup_bars=0,
        rasio_latih=0.7,
        min_trades=1,
    )
    grid_htf_item = grid_htf_result.hasil[0] if grid_htf_result.hasil else None
    check(
        "grid_search_builds_h1_for_train_and_test",
        grid_htf_item is not None
        and grid_htf_item.latih.get("htf_scans", 0) > 0
        and grid_htf_item.uji is not None
        and grid_htf_item.uji.get("htf_scans", 0) > 0,
        (
            f"latih={grid_htf_item.latih.get('htf_scans', 0)}, "
            f"uji={grid_htf_item.uji.get('htf_scans', 0) if grid_htf_item.uji else None}"
            if grid_htf_item is not None
            else "kombinasi tidak menghasilkan ringkasan"
        ),
    )

    with KlineStore.from_klines({"ETHUSDT": m5_rising_htf}) as htf_store:
        timeline_htf, series_htf = pbt.build_timeline(
            htf_store, "5m", include_ohlcv=True
        )
        prebuilt_lama = (timeline_htf, series_htf, {}, {})
        cache_htf = pbt.precompute_entry_signals(
            htf_store,
            bt_cfg_htf,
            "5m",
            warmup_ms=0,
            prebuilt=prebuilt_lama,
        )
        htf_of_prebuilt = pbt.build_htf_lookups(
            htf_store, ["ETHUSDT"], bt_cfg_htf, "5m"
        )
        prebuilt_baru = (*prebuilt_lama, htf_of_prebuilt)
        portfolio_htf_result = pbt.run_portfolio_backtest(
            htf_store,
            bt_cfg_htf,
            "5m",
            warmup_ms=0,
            prebuilt=prebuilt_baru,
            entry_signal_cache=cache_htf,
        )
        portfolio_htf_summary = pbt.summarize_portfolio(portfolio_htf_result)
        prebuilt_htf_kosong = (*prebuilt_lama, {})
        cache_htf_kosong = pbt.precompute_entry_signals(
            htf_store,
            bt_cfg_htf,
            "5m",
            warmup_ms=0,
            prebuilt=prebuilt_htf_kosong,
        )
        portfolio_htf_window_result = pbt.run_portfolio_backtest(
            htf_store,
            bt_cfg_htf,
            "5m",
            warmup_ms=0,
            start_ms=timeline_htf[0] + 45 * 3_600_000,
            prebuilt=prebuilt_htf_kosong,
            entry_signal_cache=cache_htf_kosong,
        )
        portfolio_htf_window_summary = pbt.summarize_portfolio(
            portfolio_htf_window_result
        )
        grid_portfolio_htf = gs.run_portfolio_grid_search(
            htf_store,
            bt_cfg_htf,
            "5m",
            warmup_ms=0,
            spec={"MAX_HOLD_HOURS": [24.0]},
            rasio_latih=0.7,
            min_trades=1,
            native_prebuilt=prebuilt_lama,
        )
        grid_portfolio_item = (
            grid_portfolio_htf.hasil[0] if grid_portfolio_htf.hasil else None
        )
    check(
        "portfolio_htf_supports_legacy_four_part_prebuilt_cache",
        cache_htf.htf_scans > 0 and cache_htf.htf_skips == 0,
        f"scan={cache_htf.htf_scans}, skip={cache_htf.htf_skips}",
    )
    check(
        "portfolio_htf_summary_uses_five_part_prebuilt",
        pbt.unpack_prebuilt_htf(prebuilt_baru) is htf_of_prebuilt
        and portfolio_htf_summary["htf_scans"] == cache_htf.htf_scans
        and portfolio_htf_summary["htf_skips"] == cache_htf.htf_skips,
        f"scan={portfolio_htf_summary['htf_scans']}, "
        f"skip={portfolio_htf_summary['htf_skips']}",
    )
    check(
        "portfolio_htf_cache_counts_are_scoped_and_fail_closed",
        0 < portfolio_htf_window_summary["htf_scans"] < cache_htf_kosong.htf_scans
        and portfolio_htf_window_summary["htf_skips"]
        == portfolio_htf_window_summary["htf_scans"],
        f"cache={cache_htf_kosong.htf_scans}, "
        f"scan={portfolio_htf_window_summary['htf_scans']}, "
        f"skip={portfolio_htf_window_summary['htf_skips']}",
    )
    check(
        "portfolio_grid_search_uses_htf_prebuilt_and_cache",
        grid_portfolio_item is not None
        and grid_portfolio_item.latih.get("htf_scans", 0) > 0
        and grid_portfolio_item.latih.get("htf_scans", 0) < cache_htf.htf_scans
        and grid_portfolio_item.uji is not None
        and 0 < grid_portfolio_item.uji.get("htf_scans", 0) < cache_htf.htf_scans,
        (
            f"cache={cache_htf.htf_scans}, "
            f"latih={grid_portfolio_item.latih.get('htf_scans', 0)}, "
            f"uji={grid_portfolio_item.uji.get('htf_scans', 0) if grid_portfolio_item.uji else None}"
            if grid_portfolio_item is not None
            else "kombinasi tidak menghasilkan ringkasan"
        ),
    )

    with KlineStore.from_klines(
        {"ETHUSDT": m5_resistance_backtest}
    ) as resistance_store:
        resistance_timeline, resistance_series = pbt.build_timeline(
            resistance_store, "5m", include_ohlcv=True
        )
        resistance_prebuilt = (
            resistance_timeline,
            resistance_series,
            {},
            {},
        )
        resistance_cache = pbt.precompute_entry_signals(
            resistance_store,
            bt_cfg_resistance,
            "5m",
            warmup_ms=0,
            prebuilt=resistance_prebuilt,
        )
        resistance_lookups = pbt.build_htf_lookups(
            resistance_store, ["ETHUSDT"], bt_cfg_resistance, "5m"
        )
        resistance_prebuilt_lengkap = (*resistance_prebuilt, resistance_lookups)
        cached_resistance_result = pbt.run_portfolio_backtest(
            resistance_store,
            bt_cfg_resistance,
            "5m",
            warmup_ms=0,
            prebuilt=resistance_prebuilt_lengkap,
            entry_signal_cache=resistance_cache,
        )
        cached_resistance_summary = pbt.summarize_portfolio(
            cached_resistance_result
        )
        direct_resistance_result = pbt.run_portfolio_backtest(
            resistance_store,
            bt_cfg_resistance,
            "5m",
            warmup_ms=0,
            prebuilt=resistance_prebuilt,
        )
        direct_resistance_summary = pbt.summarize_portfolio(
            direct_resistance_result
        )
        resistance_off_config = dict(
            bt_cfg_resistance, HTF_FILTER_ENABLED=False
        )
        resistance_off_prepared = pbt.build_htf_lookups(
            resistance_store, ["ETHUSDT"], resistance_off_config, "5m"
        )
        grid_resistance_portfolio = gs.run_portfolio_grid_search(
            resistance_store,
            bt_cfg_resistance,
            "5m",
            warmup_ms=0,
            spec={"MAX_HOLD_HOURS": [24.0]},
            rasio_latih=0.7,
            min_trades=1,
            native_prebuilt=resistance_prebuilt,
        )
        grid_resistance_portfolio_item = (
            grid_resistance_portfolio.hasil[0]
            if grid_resistance_portfolio.hasil
            else None
        )
    check(
        "portfolio_resistance_cache_and_direct_paths_filter_consistently",
        resistance_cache.htf_resistance_scans > 0
        and resistance_cache.htf_resistance_skips > 0
        and cached_resistance_summary["htf_resistance_scans"]
        == resistance_cache.htf_resistance_scans
        and cached_resistance_summary["htf_resistance_skips"]
        == resistance_cache.htf_resistance_skips
        and any(
            skipped.reason == "FILTER_HTF_RESISTANCE"
            for skipped in cached_resistance_result.skipped
        )
        and direct_resistance_summary["htf_resistance_scans"] > 0
        and direct_resistance_summary["htf_resistance_skips"] > 0
        and resistance_off_prepared == {},
        f"cache={resistance_cache.htf_resistance_skips}/"
        f"{resistance_cache.htf_resistance_scans}, "
        f"summary={cached_resistance_summary['htf_resistance_skips']}/"
        f"{cached_resistance_summary['htf_resistance_scans']}, "
        f"langsung={direct_resistance_summary['htf_resistance_skips']}/"
        f"{direct_resistance_summary['htf_resistance_scans']}",
    )
    check(
        "portfolio_grid_search_carries_resistance_metrics_into_train_and_test",
        grid_resistance_portfolio_item is not None
        and grid_resistance_portfolio_item.latih.get("htf_resistance_scans", 0) > 0
        and grid_resistance_portfolio_item.latih.get("htf_resistance_skips", 0) > 0
        and grid_resistance_portfolio_item.uji is not None
        and grid_resistance_portfolio_item.uji.get("htf_resistance_scans", 0) > 0,
        (
            f"latih={grid_resistance_portfolio_item.latih.get('htf_resistance_skips', 0)}/"
            f"{grid_resistance_portfolio_item.latih.get('htf_resistance_scans', 0)}, "
            f"uji={grid_resistance_portfolio_item.uji.get('htf_resistance_skips', 0)}/"
            f"{grid_resistance_portfolio_item.uji.get('htf_resistance_scans', 0)}"
            if grid_resistance_portfolio_item is not None
            and grid_resistance_portfolio_item.uji
            else "kombinasi tidak menghasilkan ringkasan lengkap"
        ),
    )

    zone_ok = strategy.evaluate_direction_zone(m15, cfg)
    check("m15_demand_zone_valid", zone_ok["ok"], zone_ok["reason"])
    closed_zone = strategy.evaluate_direction_zone(
        future_m15, cfg, signal_close_time_ms=m15[-1].close_time
    )
    check(
        "m15_demand_zone_closed_cutoff",
        closed_zone["ok"] and not strategy.evaluate_direction_zone(future_m15, cfg)["ok"],
    )

    spiked_m15 = list(m15)
    spiked_m15[-1] = _m15(64, 114.0, 118.0, 113.5, 117.5)
    zone_spike = strategy.evaluate_direction_zone(spiked_m15, cfg)
    check(
        "m15_demand_zone_rejects_spike_too_far",
        not zone_spike["ok"] and "terlalu jauh" in zone_spike["reason"],
        zone_spike["reason"],
    )

    spike_diag = ScanDiagnostics()
    spike_selected = find_best_candidate(
        [
            {
                "symbol": "SPIKEUSDT",
                "priceChangePercent": "22",
                "quoteVolume": "1000000000",
                "lastPrice": "117.5",
            }
        ],
        lambda _symbol: rows,
        cfg,
        tradable_symbols={"SPIKEUSDT"},
        direction_provider=lambda _symbol: spiked_m15,
        diagnostics=spike_diag,
    )
    check(
        "scanner_rejects_m15_spike",
        spike_selected is None
        and spike_diag.rejection_counts.get("DIRECTION_ZONE") == 1,
    )

    full_diag = ScanDiagnostics()
    htf_disabled_calls: list[str] = []
    full_selected = find_best_candidate(
        [
            {
                "symbol": "GOODUSDT",
                "priceChangePercent": "18",
                "quoteVolume": "1000000000",
                "lastPrice": "110.8",
            }
        ],
        lambda _symbol: rows,
        cfg,
        tradable_symbols={"GOODUSDT"},
        direction_provider=lambda _symbol: m15,
        diagnostics=full_diag,
        htf_provider=lambda symbol: htf_disabled_calls.append(symbol) or h1,
    )
    check(
        "scanner_full_two_timeframe_pipeline",
        full_selected is not None
        and full_selected.confirmed
        and full_selected.direction is not None
        and full_selected.direction_zone is not None
        and full_selected.timing is not None
        and full_selected.setup is not None,
    )
    check(
        "htf_disabled_does_not_call_live_provider",
        full_selected is not None and not htf_disabled_calls,
        str(htf_disabled_calls),
    )

    extra_cfg = dict(cfg)
    extra_cfg["ROLLING_VOLUME_LOOKBACK_BARS"] = 500
    without_rolling = strategy.required_lookback_bars(extra_cfg)
    extra_cfg["ROLLING_VOLUME_FILTER_ENABLED"] = True
    with_rolling = strategy.required_lookback_bars(extra_cfg)
    check(
        "disabled_extra_filter_does_not_extend_lookback",
        without_rolling == 60 and with_rolling == 501,
    )

    cleaned, errors, _warnings = validate_candidate(dict(cfg), "PAPER")
    check("config_valid", not errors, str(errors))
    invalid_dir = dict(cfg)
    invalid_dir["DIRECTION_EMA_SLOW"] = invalid_dir["DIRECTION_EMA_FAST"]
    _, dir_errors, _ = validate_candidate(invalid_dir, "PAPER")
    check("config_rejects_m15_ema_order", "DIRECTION_EMA_SLOW" in dir_errors)
    invalid_zone = dict(cfg)
    invalid_zone["DIRECTION_ZONE_MAX_DISTANCE_PCT"] = 0.5
    invalid_zone["DIRECTION_ZONE_BUFFER_PCT"] = 1.2
    _, zone_errors, _ = validate_candidate(invalid_zone, "PAPER")
    check("config_rejects_m15_zone_distance", "DIRECTION_ZONE_MAX_DISTANCE_PCT" in zone_errors)
    invalid_breakout = dict(cfg)
    invalid_breakout["BREAKOUT_LOOKBACK_BARS"] = 5
    invalid_breakout["BREAKOUT_PIVOT_WING_BARS"] = 3
    _, breakout_errors, _ = validate_candidate(invalid_breakout, "PAPER")
    check("config_rejects_invalid_pivot_window", "BREAKOUT_LOOKBACK_BARS" in breakout_errors)
    migrated = _migrate_legacy_keys(
        {
            "ZONE_FILTER_ENABLED": True,
            "ZONE_INTERVAL": "1h",
            "HTF_DEMAND_ZONE_BUFFER_PCT": 1.5,
            "DIRECTION_INTERVAL": "15m",
        }
    )
    _validate_override_payload(migrated)
    check(
        "legacy_zone_keys_migrated_cleanly",
        migrated == {"DIRECTION_INTERVAL": "15m"},
        str(migrated),
    )
    check(
        "default_two_timeframes",
        cleaned["DIRECTION_INTERVAL"] == "15m"
        and cleaned["CONFIRM_INTERVAL"] == "5m"
        and "ZONE_INTERVAL" not in cleaned
        and "ZONE_FILTER_ENABLED" not in cleaned,
    )
    check(
        "default_htf_gate_disabled_with_h1_parameters",
        cleaned["HTF_FILTER_ENABLED"] is False
        and cleaned["HTF_INTERVAL"] == "1h"
        and cleaned["HTF_EMA_FAST"] == 20
        and cleaned["HTF_EMA_SLOW"] == 50
        and cleaned["HTF_ADX_PERIOD"] == 14
        and cleaned["HTF_ADX_MIN"] == 30.0
        and cleaned["HTF_LOOKBACK_BARS"] == 60
        and cleaned["HTF_RESISTANCE_FILTER_ENABLED"] is False
        and cleaned["HTF_RESISTANCE_LOOKBACK_BARS"] == 60
        and cleaned["HTF_RESISTANCE_PIVOT_WING_BARS"] == 2
        and cleaned["HTF_RESISTANCE_MIN_ROOM_PCT"] == 1.5,
    )
    resistance_needs_htf = dict(cleaned, HTF_RESISTANCE_FILTER_ENABLED=True)
    _, resistance_needs_htf_errors, _ = validate_candidate(
        resistance_needs_htf, "PAPER"
    )
    check(
        "config_resistance_requires_htf_gate",
        "HTF_RESISTANCE_FILTER_ENABLED" in resistance_needs_htf_errors,
    )
    resistance_active = dict(
        cleaned,
        HTF_FILTER_ENABLED=True,
        HTF_RESISTANCE_FILTER_ENABLED=True,
    )
    resistance_active_cleaned, resistance_active_errors, resistance_warnings = (
        validate_candidate(resistance_active, "PAPER")
    )
    check(
        "config_accepts_resistance_defaults_only_with_htf_and_warns_simulation",
        not resistance_active_errors
        and resistance_active_cleaned["HTF_RESISTANCE_MIN_ROOM_PCT"] == 1.5
        and any("belum dihubungkan" in warning for warning in resistance_warnings),
        str(resistance_active_errors),
    )
    simulation_resistance_override = bt.apply_overrides(
        PUMP_CONFIG,
        {
            "HTF_FILTER_ENABLED": True,
            "HTF_RESISTANCE_FILTER_ENABLED": True,
            "HTF_RESISTANCE_LOOKBACK_BARS": int(
                PUMP_CONFIG.get("HTF_LOOKBACK_BARS", 60) or 60
            ),
        },
    )
    simulation_override_valid = True
    try:
        bt.validate_params(simulation_resistance_override)
    except bt.BacktestError:
        simulation_override_valid = False
    check(
        "backtest_override_resistance_is_simulation_only_and_validated",
        simulation_override_valid
        and simulation_resistance_override["HTF_FILTER_ENABLED"]
        and simulation_resistance_override["HTF_RESISTANCE_FILTER_ENABLED"]
        and PUMP_CONFIG.get("HTF_FILTER_ENABLED") is False
        and PUMP_CONFIG.get("HTF_RESISTANCE_FILTER_ENABLED") is False,
    )
    resistance_short_window = dict(
        resistance_active,
        HTF_RESISTANCE_LOOKBACK_BARS=5,
        HTF_RESISTANCE_PIVOT_WING_BARS=3,
    )
    _, resistance_window_errors, _ = validate_candidate(
        resistance_short_window, "PAPER"
    )
    check(
        "config_rejects_resistance_window_shorter_than_pivot",
        "HTF_RESISTANCE_LOOKBACK_BARS" in resistance_window_errors,
    )
    resistance_long_window = dict(
        resistance_active, HTF_RESISTANCE_LOOKBACK_BARS=61
    )
    _, resistance_long_errors, _ = validate_candidate(
        resistance_long_window, "PAPER"
    )
    check(
        "config_rejects_resistance_window_needing_extra_h1_data",
        "HTF_RESISTANCE_LOOKBACK_BARS" in resistance_long_errors,
    )
    resistance_bad_room = dict(
        resistance_active, HTF_RESISTANCE_MIN_ROOM_PCT=51.0
    )
    _, resistance_room_errors, _ = validate_candidate(
        resistance_bad_room, "PAPER"
    )
    check(
        "config_rejects_invalid_resistance_room",
        "HTF_RESISTANCE_MIN_ROOM_PCT" in resistance_room_errors,
    )
    invalid_htf = dict(cleaned)
    invalid_htf["HTF_FILTER_ENABLED"] = True
    invalid_htf["HTF_EMA_FAST"] = 50
    invalid_htf["HTF_EMA_SLOW"] = 20
    _, htf_ema_errors, _ = validate_candidate(invalid_htf, "PAPER")
    check("config_rejects_htf_ema_order", "HTF_EMA_SLOW" in htf_ema_errors)
    incompatible_htf = dict(cleaned)
    incompatible_htf["HTF_FILTER_ENABLED"] = True
    incompatible_htf["CONFIRM_INTERVAL"] = "2h"
    _, htf_interval_errors, _ = validate_candidate(incompatible_htf, "PAPER")
    check(
        "config_rejects_htf_interval_shorter_than_confirmation",
        "HTF_INTERVAL" in htf_interval_errors,
    )
    check(
        "default_ema_adx",
        all(
            cleaned[key] == value
            for key, value in (
                ("DIRECTION_EMA_FAST", 20),
                ("DIRECTION_EMA_SLOW", 50),
                ("DIRECTION_ADX_MIN", 32.0),
                ("DIRECTION_ZONE_LOOKBACK_BARS", 28),
                ("DIRECTION_ZONE_MAX_DISTANCE_PCT", 7.0),
                ("TIMING_EMA_FAST", 20),
                ("TIMING_EMA_SLOW", 50),
                ("TIMING_ADX_MIN", 18.0),
                ("ATR_MULT_TRAIL_START", 9.0),
                ("MAX_HOLD_HOURS", 24.0),
            )
        ),
    )
    check(
        "default_exact_strategy",
        cleaned["DIRECTION_FILTER_ENABLED"]
        and cleaned["DIRECTION_ZONE_FILTER_ENABLED"]
        and cleaned["TIMING_FILTER_ENABLED"]
        and cleaned["BREAKOUT_RETEST_FILTER_ENABLED"]
        and not cleaned["DIRECTION_ZONE_REQUIRE_INSIDE_ZONE"]
        and not cleaned["ROLLING_VOLUME_FILTER_ENABLED"]
        and not cleaned["DEMAND_ZONE_FILTER_ENABLED"],
    )
    check(
        "default_breakout_rules",
        cleaned["BREAKOUT_RETEST_MAX_BARS"] == 12
        and cleaned["BREAKOUT_RETEST_TOLERANCE_ATR"] == 0.2
        and cleaned["BREAKOUT_RETEST_REQUIRE_BULLISH"],
    )

    root = Path(__file__).resolve().parents[1]
    dashboard_source = (root / "web" / "dashboard.py").read_text()
    dashboard_html = (root / "templates" / "dashboard.html").read_text()
    check(
        "dashboard_resistance_control_is_simulation_only_and_wired",
        '"HTF_RESISTANCE_FILTER_ENABLED"' in dashboard_source
        and '"HTF_RESISTANCE_LOOKBACK_BARS"' in dashboard_source
        and 'id="btHtfResistanceEnabled"' in dashboard_html
        and "HTF_RESISTANCE_FILTER_ENABLED: document.getElementById('btHtfEnabled').checked" in dashboard_html
        and "resistance.disabled = !htf" in dashboard_html,
    )

    if verbose:
        print(f"Selftest entry selesai: {total - len(failures)}/{total} pemeriksaan lulus.")
    return not failures, failures


if __name__ == "__main__":
    success, _ = run(verbose=True)
    raise SystemExit(0 if success else 1)
