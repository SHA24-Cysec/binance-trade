"""Uji gerbang pump rolling-volume, spread contract, dan filter BTC."""
from __future__ import annotations
import market_scanner as scanner
from strategy import Kline
from config import PUMP_CONFIG

DAY = 86400000

def daily(volume=1000.0, n=7):
    return [Kline(i*DAY, 1, 1, 1, 1, (i+1)*DAY-1, volume, volume)
            for i in range(n)]

def test_volume_rolling_minimal_dua_kali():
    c = dict(PUMP_CONFIG, PUMP_MIN_24H_CHANGE_PCT=5, PUMP_VOLUME_SURGE_MULT=2)
    ok, reason = scanner.is_pumping_today("SOLUSDT", 10, 1999, lambda _: daily(), c,
                                          reference_ms=7*DAY+1)
    assert not ok and "volume" in reason
    ok, _ = scanner.is_pumping_today("SOLUSDT", 10, 2000, lambda _: daily(), c,
                                      reference_ms=7*DAY+1)
    assert ok

def test_hanya_candle_harian_tertutup_dipakai():
    running = Kline(7*DAY,1,1,1,1,8*DAY-1,10**9,10**9)
    avg, _ = scanner.average_prior_daily_quote_volume(daily()+[running], 7*DAY+1)
    assert avg == 1000

def test_btc_filter_menolak_penurunan_tajam():
    c = dict(PUMP_CONFIG, PUMP_MIN_24H_CHANGE_PCT=0, PUMP_VOLUME_SURGE_MULT=1,
             BTC_FILTER_ENABLED=True, BTC_MAX_DROP_PCT=3)
    ok, reason = scanner.evaluate_pump_gate(10, 1000, 1000, c, btc_drop_pct=-3.1)
    assert not ok and "BTC" in reason
    ok, _ = scanner.evaluate_pump_gate(10, 1000, 1000, c, btc_drop_pct=-2.9)
    assert ok

def test_fail_closed_dan_nan():
    c = dict(PUMP_CONFIG, PUMP_MIN_24H_CHANGE_PCT=0, PUMP_VOLUME_SURGE_MULT=1)
    assert not scanner.filter_and_rank_candidates([{"symbol":"SOLUSDT","priceChangePercent":"10",
        "quoteVolume":"1000","lastPrice":"1"}], c)
    broken = daily(); broken[0] = broken[0]._replace(quote_volume=float("nan"))
    assert scanner.average_prior_daily_quote_volume(broken, 7*DAY+1)[0] is None
