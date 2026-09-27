"""Uji regresi skor entry panel; tidak memanggil jalur trading."""
from market_scanner import score_entry_signal
from strategy import Kline


def _klines(n=40, volume=10.0):
    return [Kline(i * 300000, 100 + i * .1, 101 + i * .1,
                  99 + i * .1, 100 + i * .1, volume, (i + 1) * 300000,
                  volume) for i in range(n)]


def _config(**extra):
    cfg = {
        "ROLLING_VOLUME_FILTER_ENABLED": False,
        "SWING_PIVOT_WING_BARS": 2,
        "WATCHLIST_ENTRY_WEIGHT_EMA": 25,
        "WATCHLIST_ENTRY_WEIGHT_RSI": 25,
        "WATCHLIST_ENTRY_WEIGHT_MACD": 25,
        "WATCHLIST_ENTRY_WEIGHT_HL": 25,
        "WATCHLIST_ENTRY_EMA_GAP_PCT": 1.0,
        "WATCHLIST_ENTRY_RSI_DECAY_PTS": 15,
    }
    cfg.update(extra)
    return cfg


def test_volume_gate_hard_fail():
    result = score_entry_signal(_klines(10), _config(
        ROLLING_VOLUME_FILTER_ENABLED=True,
        ROLLING_VOLUME_LOOKBACK_BARS=20,
        ROLLING_VOLUME_CONFIRMATION_BARS=1,
    ))
    assert result.score == 0.0
    assert result.disqualified
    assert result.status == "TIDAK LOLOS"


def test_components_have_expected_shape():
    result = score_entry_signal(_klines(), _config())
    assert set(result.components) == {"ema", "rsi", "macd", "higher_low"}
    assert 0 <= result.score <= 100


def test_disqualifier_for_spread_forces_zero():
    result = score_entry_signal(_klines(), _config(),
                                {"symbol": "ABCUSDT", "spread_pct": 10})
    assert result.score == 0.0
    assert result.status == "TIDAK LOLOS"
    assert result.disqualified
