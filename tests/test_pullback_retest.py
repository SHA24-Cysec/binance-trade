"""Uji sinyal momentum baru, ATR exit, dan kontrak confirm_entry."""
from __future__ import annotations

import pytest
import strategy
import market_scanner as scanner
from config import PUMP_CONFIG


def candles(values):
    return [strategy.Kline(i * 300000, v, v + 1, max(.01, v - 1), v,
                           i * 300000 + 299999, 1000.0, v * 1000.0)
            for i, v in enumerate(values)]


def momentum_data():
    return [100.0] * 30 + [100.2,100.4,99.4,98.4,97.4,97.6,98.6,98.1,98.3,97.8,
                            98.8,99.8,98.8,99.8,99.3,99.5,98.5,99.5,98.5,100.0]


def cfg():
    c = dict(PUMP_CONFIG)
    c.update({"CONFIRM_LOOKBACK_BARS": 50, "SWING_PIVOT_WING_BARS": 2,
              "USE_ATR_EXIT": False})
    return c


def test_fungsi_indikator_kronologis():
    closes = [100, 101, 100, 102, 103, 102, 104] * 5
    assert len(strategy.ema(closes, 9)) == len(closes)
    assert len(strategy.rsi(closes, 14)) == len(closes)
    line, signal, hist = strategy.macd(closes)
    assert len(line) == len(signal) == len(hist) == len(closes)


def test_minimal_tiga_dari_empat_konfirmasi_lolos():
    result = scanner.detect_pullback_retest(candles(momentum_data()), cfg())
    assert result.ok, result.reason
    assert result.atr_value is not None
    assert "3/4" in result.reason or "4/4" in result.reason


def test_konfirmasi_yang_kurang_ditolak():
    values = [100.0] * 50
    result = scanner.detect_pullback_retest(candles(values), cfg())
    assert not result.ok
    assert "3/4" in result.reason or "kurang" in result.reason


def test_confirm_entry_tetap_bool_dan_str():
    ok, reason = scanner.confirm_entry(candles(momentum_data()), cfg())
    assert ok is True and isinstance(reason, str)


def test_tidak_lookahead():
    base = candles(momentum_data())
    result = scanner.detect_pullback_retest(base, cfg())
    future = base + candles([90, 120])
    assert scanner.detect_pullback_retest(future[:len(base)], cfg()) == result


def test_atr_exit_menerapkan_invariant_dan_fallback():
    kl = candles([100 + (i % 3) for i in range(30)])
    atr_value = strategy.atr(kl, 14)
    assert atr_value is not None and atr_value > 0
    atr_levels = strategy.resolve_exit_levels({"USE_ATR_EXIT": True, "ATR_MULT_SL": 1.5,
        "ATR_MULT_TP": 3, "ATR_MULT_TRAIL": 2, "ATR_MULT_BE_TRIGGER": 2,
        "ATR_MULT_BE_LOCK": 3, "ATR_MULT_TRAIL_START": 1})
    assert atr_levels["source"] == "ATR"
    assert atr_levels["atr_mult_trail"] <= atr_levels["atr_mult_sl"]
    assert atr_levels["be_trigger_pct"] <= atr_levels["trail_start_pct"]
    assert atr_levels["be_lock_pct"] <= atr_levels["be_trigger_pct"]
    fixed = strategy.resolve_exit_levels({"USE_ATR_EXIT": False, "SL_PCT": 3, "TP_PCT": 6,
        "BE_TRIGGER_PCT": 2, "BE_LOCK_PCT": 1, "TRAILING_START_PCT": 4,
        "TRAILING_STEP_PCT": 5})
    assert fixed["source"] == "FIXED" and fixed["sl_pct"] == 3
    assert fixed["trail_step_pct"] <= fixed["sl_pct"]
