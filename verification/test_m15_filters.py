import json
import socket

import pytest

from backtesting import backtest as bt
from backtesting import grid_search, parity, portfolio_backtest as pbt
from backtesting.backtest_storage import KlineStore
from config import settings_schema as schema
from config.config import PUMP_CONFIG, build_config_for_mode
from market import market_scanner as market
from strategy import indicators as strategy
from trading import pump_scanner_bot as bot


M15_MS = 900_000
M5_MS = 300_000
START = M15_MS * 1_800_000


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Jaringan tidak boleh dipakai dalam uji M15")

    monkeypatch.setattr(socket.socket, "connect", blocked)


def candle(index, open_price, close_price, low=None, high=None, step=M15_MS):
    t = START + index * step
    return strategy.Kline(
        t,
        float(open_price),
        float(high if high is not None else max(open_price, close_price) + 0.5),
        float(low if low is not None else min(open_price, close_price) - 0.5),
        float(close_price),
        t + step - 1,
        1000.0,
        1_000_000.0,
    )


def as_m5(source):
    out = []
    for row in source:
        for i in range(3):
            t = row.open_time + i * M5_MS
            out.append(
                strategy.Kline(
                    t,
                    row.open,
                    row.high,
                    row.low,
                    row.close if i == 2 else row.open,
                    t + M5_MS - 1,
                    row.volume / 3,
                    row.quote_volume / 3,
                )
            )
    return out


def rising_m5(count=600):
    bars = []
    for i in range(count):
        opened = 100.0 + i * 0.003
        closed = opened + 0.003
        t = START + i * M5_MS
        bars.append(
            strategy.Kline(
                t, opened, closed + 0.055, opened - 0.055,
                closed, t + M5_MS - 1, 1000.0, 10_000_000.0,
            )
        )
    return bars


def simple_backtest_config():
    return {
        **PUMP_CONFIG,
        "TREND_FILTER_ENABLED": False,
        "HTF_DEMAND_FILTER_ENABLED": False,
        "DEMAND_ZONE_FILTER_ENABLED": False,
        "ROLLING_VOLUME_FILTER_ENABLED": False,
        "PUMP_MIN_24H_CHANGE_PCT": -100.0,
        "MIN_QUOTE_VOLUME_USDT_24H": 0.0,
        "USE_ATR_EXIT": False,
        "USE_EQUITY_STOP": False,
        "TOP_N_CANDIDATES_TO_CONFIRM": 1,
    }


def test_defaults_locked_interval_and_schema():
    assert {k: PUMP_CONFIG[k] for k in (
        "M15_DEMAND_FILTER_ENABLED", "M15_TREND_FILTER_ENABLED"
    )} == {
        "M15_DEMAND_FILTER_ENABLED": True,
        "M15_TREND_FILTER_ENABLED": True,
    }
    assert [PUMP_CONFIG[k] for k in (
        "M15_DEMAND_LOOKBACK_BARS", "M15_DEMAND_ZONE_BUFFER_PCT",
        "M15_DEMAND_MAX_DISTANCE_PCT", "M15_DEMAND_MIN_CLOSE_POSITION",
        "M15_TREND_EMA_FAST", "M15_TREND_EMA_SLOW", "M15_TREND_ADX_PERIOD",
        "M15_TREND_ADX_MIN", "M15_TREND_LOOKBACK_BARS",
    )] == [20, 1.0, 5.0, 0.45, 20, 50, 14, 20.0, 120]
    assert strategy.m15_demand_interval(PUMP_CONFIG) == "15m"
    assert strategy.m15_trend_interval(PUMP_CONFIG) == "15m"
    assert "M15_DEMAND_INTERVAL" not in schema.PARAMETER_SCHEMA
    assert "M15_TREND_INTERVAL" not in schema.PARAMETER_SCHEMA
    assert "DAILY_DEMAND_FILTER_ENABLED" not in PUMP_CONFIG
    assert "DAILY_TREND_FILTER_ENABLED" not in PUMP_CONFIG
    assert set(PUMP_CONFIG) == set(schema.PARAMETER_SCHEMA)
    assert strategy.m15_demand_window_bars(PUMP_CONFIG) == 21
    assert strategy.m15_trend_window_bars(PUMP_CONFIG) == 120
    assert strategy.htf_gate_warmup_bars(PUMP_CONFIG, "5m") == 1452
    assert bt.validate_params(PUMP_CONFIG) is None


def test_legacy_daily_override_ignored_without_losing_other_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(schema, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(schema, "SETTINGS_ERROR_FILE", tmp_path / "settings.error.json")
    schema.SETTINGS_FILE.write_text(json.dumps({
        "active_mode": "PAPER",
        "overrides": {"PAPER": {
            "DAILY_DEMAND_FILTER_ENABLED": False,
            "DAILY_DEMAND_INTERVAL": "1d",
            "DAILY_TREND_FILTER_ENABLED": False,
            "DAILY_TREND_INTERVAL": "1d",
            "M15_DEMAND_ZONE_BUFFER_PCT": 1.2,
            "SL_PCT": 2.0,
        }},
    }))
    override, errors = schema.load_mode_override("PAPER")
    assert not errors
    assert not any(key.startswith("DAILY_") for key in override)
    assert override["SL_PCT"] == 2.0
    assert override["M15_DEMAND_ZONE_BUFFER_PCT"] == 1.2
    config, errors = build_config_for_mode("PAPER")
    assert not errors
    assert config["M15_DEMAND_FILTER_ENABLED"] is True
    assert config["M15_TREND_FILTER_ENABLED"] is True
    assert config["M15_DEMAND_ZONE_BUFFER_PCT"] == 1.2
    assert config["SL_PCT"] == 2.0


@pytest.mark.parametrize("interval,valid", [
    ("1m", True), ("3m", True), ("5m", True), ("15m", True),
    ("30m", False), ("1h", False), ("1d", False),
])
def test_confirmation_interval_compatibility(interval, valid):
    cfg = {**PUMP_CONFIG, "CONFIRM_INTERVAL": interval,
           "TREND_FILTER_ENABLED": False, "HTF_DEMAND_FILTER_ENABLED": False}
    errors = schema.relation_violations(cfg, mode_aware=True)
    assert ("CONFIRM_INTERVAL" not in errors) is valid
    if valid:
        assert strategy.m15_ratio(interval) == 15 // strategy.INTERVAL_MINUTES[interval]
        assert bt.validate_params(cfg) is None
    else:
        with pytest.raises(ValueError, match="M15"):
            strategy.m15_ratio(interval)
        with pytest.raises(bt.BacktestError, match="konfirmasi.*M15"):
            bt.validate_params(cfg)


def test_ema_slow_must_fit_binance_limit():
    cfg = {**PUMP_CONFIG, "M15_TREND_EMA_SLOW": 1000}
    _, errors, _ = schema.validate_candidate(cfg, "PAPER")
    assert "M15_TREND_EMA_SLOW" in errors
    with pytest.raises(bt.BacktestError, match="M15_TREND_EMA_SLOW"):
        bt.validate_params(cfg)


def test_m15_demand_lookup_uses_only_candles_closed_at_signal():
    source = [candle(i, 100, 100, 99.5, 100.5) for i in range(20)]
    source += [candle(20, 100, 100.5, 99.5, 101)]
    source += [candle(21, 100.5, 98, 97, 101)]
    m5 = as_m5(source)
    assert len(parity.build_m15_klines(m5[:-1], PUMP_CONFIG, "5m")) == 21
    aggregated = parity.build_m15_klines(m5, PUMP_CONFIG, "5m")
    assert len(aggregated) == len(source)
    assert aggregated[20].close_time == source[20].close_time
    lookup = parity.make_m15_lookup(m5, PUMP_CONFIG, "5m")
    before = lookup.m15_demand_at(source[20].close_time)
    after = lookup.m15_demand_at(source[21].close_time)
    assert before["ok"] is True
    assert before["interval"] == "15m"
    assert len(lookup.window_at(source[20].close_time)) == 21
    assert after["ok"] is False
    assert strategy.evaluate_m15_demand([], PUMP_CONFIG)["ok"] is False


def test_m15_trend_lookup_excludes_future_crash():
    source = [candle(i, 100 + i * .04, 100 + i * .04, 99 + i * .04,
                     101 + i * .04) for i in range(130)]
    source += [candle(130, 105.16, 80, 79, 106)]
    lookup = parity.make_m15_trend_lookup(as_m5(source), PUMP_CONFIG, "5m")
    before = lookup.verdict_at(source[129].close_time)
    after = lookup.verdict_at(source[130].close_time)
    assert before["ok"] is True
    assert before["interval"] == "15m"
    assert len(lookup.window_at(source[129].close_time)) == 120
    assert after["ok"] is False
    assert strategy.evaluate_m15_trend([], PUMP_CONFIG)["ok"] is False


@pytest.mark.parametrize("verdict", [market.m15_demand_verdict, market.m15_trend_verdict])
def test_missing_m15_live_provider_fails_closed(verdict):
    assert verdict("SOLUSDT", PUMP_CONFIG, None)["ok"] is False
    assert verdict("SOLUSDT", PUMP_CONFIG, lambda sym: [])["ok"] is False

    def broken(symbol):
        raise OSError("offline")

    assert verdict("SOLUSDT", PUMP_CONFIG, broken)["ok"] is False


def test_candidate_pipeline_checks_m15_at_signal_close_not_future(monkeypatch):
    source = [candle(i, 100 + i * .04, 100 + i * .04,
                     99 + i * .04, 101 + i * .04) for i in range(130)]
    source += [candle(130, 105.16, 80, 79, 106)]
    candidate = market.Candidate("SOLUSDT", "SOL", 10.0, 1_000_000, 105.16)
    cfg = {**PUMP_CONFIG, "TREND_FILTER_ENABLED": False,
           "HTF_DEMAND_FILTER_ENABLED": False}
    monkeypatch.setattr(market, "filter_and_rank_candidates",
                        lambda *args: [candidate])
    monkeypatch.setattr(market, "detect_entry_setup",
                        lambda *args: market.SetupResult(True, "sinyal", 105.16, .1))
    signal = as_m5(source[129:130])[-1]
    best = market.find_best_candidate(
        [], lambda symbol: [signal], cfg,
        m15_provider=lambda symbol: source,
        m15_trend_provider=lambda symbol: source,
    )
    assert best is candidate
    assert best.m15_demand["ok"] is True
    assert best.m15_trend["ok"] is True
    assert best.m15_demand["bars"] == 130
    assert best.m15_trend["close"] == source[129].close


def test_live_m15_cache_is_shared_closed_only_and_rejects_stale_data(monkeypatch):
    current = START + 121 * M15_MS
    now = current + 10_000
    rows = [candle(i, 100, 100) for i in range(122)]

    def raw(row):
        return [row.open_time, row.open, row.high, row.low,
                row.close, row.volume, row.close_time, row.quote_volume]

    class OfflineClient:
        calls = []

        def get_klines(self, symbol, interval, limit):
            self.calls.append((symbol, interval, limit))
            return [raw(row) for row in rows[-limit:]]

    client = OfflineClient()
    cache = bot.M15KlineCache(client, PUMP_CONFIG)
    monkeypatch.setattr(bot.state_mod, "now_ms", lambda: now)
    closed = cache.provider("SOLUSDT")
    assert len(closed) == 121
    assert closed[-1].open_time == current - M15_MS
    assert cache.provider("SOLUSDT") == closed
    assert client.calls == [("SOLUSDT", "15m", 122)]
    rows.pop()
    assert cache.window_klines("SOLUSDT", now + M15_MS) == []
    assert len(client.calls) == 2
    rows.extend([candle(121, 100, 100), candle(122, 100, 100)])
    assert cache.window_klines("SOLUSDT", now + M15_MS)[-1].open_time == current
    assert len(client.calls) == 3


def test_extreme_m15_window_respects_binance_maximum():
    cfg = {**PUMP_CONFIG, "M15_TREND_LOOKBACK_BARS": 999,
           "M15_TREND_EMA_SLOW": 999}
    assert bt.validate_params(cfg) is None

    class OfflineClient:
        calls = []

        def get_klines(self, symbol, interval, limit):
            self.calls.append((interval, limit))
            rows = [candle(i, 100, 100) for i in range(1001)]
            return [[k.open_time, k.open, k.high, k.low, k.close,
                     k.volume, k.close_time, k.quote_volume] for k in rows[-limit:]]

    client = OfflineClient()
    cache = bot.M15KlineCache(client, cfg)
    assert cache.window_bars() == 1000
    closed = cache.window_klines("SOLUSDT", START + 1000 * M15_MS + 10_000)
    assert len(closed) == 999
    assert client.calls == [("15m", 1000)]


def test_single_and_portfolio_backtests_evaluate_both_m15_gates():
    cfg = simple_backtest_config()
    bars = rising_m5()
    result = bt.run_backtest(bars, cfg, 365)
    assert result.m15_demand_scans > 0
    assert result.m15_trend_scans > 0
    assert result.m15_demand_skips == result.m15_trend_skips == 0
    assert result.mark_to_market_open is True
    with KlineStore.from_klines({"SOLUSDT": bars}) as store:
        portfolio = pbt.run_portfolio_backtest(store, cfg, "5m", warmup_ms=365*M5_MS)
    assert portfolio.m15_demand_scans > 0
    assert portfolio.m15_trend_scans > 0
    assert portfolio.m15_demand_skips == portfolio.m15_trend_skips == 0
    assert portfolio.mark_to_market_open is True


def test_portfolio_missing_m15_data_does_not_allow_entry(monkeypatch):
    cfg = simple_backtest_config()
    monkeypatch.setattr(pbt, "_siapkan_m15", lambda *args: {})
    with KlineStore.from_klines({"SOLUSDT": rising_m5()}) as store:
        portfolio = pbt.run_portfolio_backtest(store, cfg, "5m", warmup_ms=365*M5_MS)
    assert portfolio.m15_demand_scans > 0
    assert portfolio.m15_demand_skips == portfolio.m15_demand_scans
    assert portfolio.mark_to_market_open is False
    assert any(x.reason == "FILTER_M15_DEMAND" for x in portfolio.skipped)


def test_portfolio_missing_m15_trend_data_does_not_allow_entry(monkeypatch):
    cfg = simple_backtest_config()
    monkeypatch.setattr(pbt, "_siapkan_tren_m15", lambda *args: {})
    with KlineStore.from_klines({"SOLUSDT": rising_m5()}) as store:
        portfolio = pbt.run_portfolio_backtest(store, cfg, "5m", warmup_ms=365*M5_MS)
    assert portfolio.m15_demand_scans > 0
    assert portfolio.m15_demand_skips == 0
    assert portfolio.m15_trend_scans > 0
    assert portfolio.m15_trend_skips == portfolio.m15_trend_scans
    assert portfolio.mark_to_market_open is False
    assert any(x.reason == "FILTER_M15_TREND" for x in portfolio.skipped)


def test_dashboard_defaults_and_minimum_period():
    from web import dashboard as dash

    assert dash._bt_min_days_note(PUMP_CONFIG, "5m")[0] == 7
    cfg = {**PUMP_CONFIG, "TREND_FILTER_ENABLED": False,
           "HTF_DEMAND_FILTER_ENABLED": False}
    assert dash._bt_min_days_note(cfg, "5m")[0] == 3
    with dash.app.test_client() as client:
        response = client.get("/api/backtest/defaults",
                              base_url=f"http://localhost:{dash._DASHBOARD_PORT}")
        assert response.status_code == 200
        data = response.get_json()
    assert data["m15_demand_enabled"] is True
    assert data["m15_trend_enabled"] is True
    assert data["m15_demand_interval"] == data["m15_trend_interval"] == "15m"
    assert data["bars_per_day"] == bt.bars_per_day("5m")
    assert not any(key.startswith("daily_") for key in data)


def test_single_grid_search_keeps_m15_enabled():
    cfg = simple_backtest_config()
    result = grid_search.run_grid_search(
        rising_m5(), cfg, {"SL_PCT": [cfg["SL_PCT"]]},
        warmup_bars=365, rasio_latih=1.0, min_trades=1,
    )
    assert result.total_kombinasi == 1
    assert result.bar_latih > 0
    assert result.hasil[0].latih["m15_demand_scans"] > 0
    assert result.hasil[0].latih["m15_trend_scans"] > 0


def test_portfolio_grid_search_keeps_m15_enabled():
    cfg = simple_backtest_config()
    with KlineStore.from_klines({"SOLUSDT": rising_m5()}) as store:
        result = grid_search.run_portfolio_grid_search(
            store, cfg, "5m", 365*M5_MS, {"SL_PCT": [cfg["SL_PCT"]]},
            rasio_latih=1.0, min_trades=1,
        )
    assert result.total_kombinasi == 1
    assert result.dilewati == 0
    assert result.hasil[0].latih["m15_demand_scans"] > 0
    assert result.hasil[0].latih["m15_trend_scans"] > 0
