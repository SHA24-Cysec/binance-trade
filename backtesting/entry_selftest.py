from __future__ import annotations

from copy import deepcopy

from config.config import PUMP_CONFIG
from config.settings_schema import (
    _migrate_legacy_keys,
    _validate_override_payload,
    validate_candidate,
)
from market.market_scanner import ScanDiagnostics, detect_entry_setup, find_best_candidate
from strategy import indicators as strategy
from strategy.indicators import Kline


def _m5(index: int, open_price: float, high: float, low: float, close: float) -> Kline:
    start = index * 300_000
    return Kline(start, open_price, high, low, close, start + 299_999, 1_000.0, 100_000.0)


def _m15(index: int, open_price: float, high: float, low: float, close: float) -> Kline:
    start = index * 900_000
    return Kline(start, open_price, high, low, close, start + 899_999, 1_000.0, 100_000.0)


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

    if verbose:
        print(f"Selftest entry selesai: {total - len(failures)}/{total} pemeriksaan lulus.")
    return not failures, failures


if __name__ == "__main__":
    success, _ = run(verbose=True)
    raise SystemExit(0 if success else 1)
