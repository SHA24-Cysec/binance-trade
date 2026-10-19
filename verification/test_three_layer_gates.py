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


M5_MS = 300_000
H1_MS = 3_600_000
DAY_MS = 86_400_000
START = DAY_MS * 1_800_000


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Jaringan tidak boleh dipakai dalam uji tiga layer")

    monkeypatch.setattr(socket.socket, "connect", blocked)


def candle(index, open_price, close_price, low=None, high=None, step=M5_MS, base=START):
    t = base + index * step
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


def daily(index, open_price, close_price, low=None, high=None):
    return candle(
        index, open_price, close_price, low, high, step=DAY_MS, base=START
    )


def hourly(index, open_price, close_price, low=None, high=None):
    return candle(
        index, open_price, close_price, low, high, step=H1_MS, base=START
    )


def rising_m5(count=600, step=M5_MS, base=START):
    bars = []
    for i in range(count):
        opened = 100.0 + i * 0.003
        closed = opened + 0.003
        t = base + i * step
        bars.append(
            strategy.Kline(
                t, opened, closed + 0.055, opened - 0.055,
                closed, t + step - 1, 1000.0, 10_000_000.0,
            )
        )
    return bars


def hourly_set(count=80, base=None):
    base = START + 58 * DAY_MS if base is None else base
    return [
        candle(i, 100.0, 100.0, step=H1_MS, base=base)
        for i in range(count)
    ]


def signal_set(count=120):
    return rising_m5(count, base=START + 61 * DAY_MS)


def simple_backtest_config():
    return {
        **PUMP_CONFIG,
        "DIRECTION_FILTER_ENABLED": False,
        "DIRECTION_ZONE_FILTER_ENABLED": False,
        "ZONE_FILTER_ENABLED": False,
        "TIMING_FILTER_ENABLED": False,
        "DEMAND_ZONE_FILTER_ENABLED": False,
        "ROLLING_VOLUME_FILTER_ENABLED": False,
        "PUMP_MIN_24H_CHANGE_PCT": -100.0,
        "MIN_QUOTE_VOLUME_USDT_24H": 0.0,
        "USE_ATR_EXIT": False,
        "USE_EQUITY_STOP": False,
        "TOP_N_CANDIDATES_TO_CONFIRM": 1,
    }


def test_defaults_locked_three_layer_and_schema():
    assert {k: PUMP_CONFIG[k] for k in (
        "DIRECTION_FILTER_ENABLED", "ZONE_FILTER_ENABLED", "TIMING_FILTER_ENABLED"
    )} == {
        "DIRECTION_FILTER_ENABLED": True,
        "ZONE_FILTER_ENABLED": True,
        "TIMING_FILTER_ENABLED": True,
    }
    assert [PUMP_CONFIG[k] for k in (
        "DIRECTION_INTERVAL", "DIRECTION_EMA_FAST", "DIRECTION_EMA_SLOW",
        "DIRECTION_ADX_PERIOD", "DIRECTION_ADX_MIN", "DIRECTION_LOOKBACK_BARS",
    )] == ["1d", 20, 50, 14, 20.0, 60]
    assert [PUMP_CONFIG[k] for k in (
        "DIRECTION_ZONE_FILTER_ENABLED", "DIRECTION_ZONE_LOOKBACK_BARS",
        "DIRECTION_ZONE_BUFFER_PCT", "DIRECTION_ZONE_MAX_DISTANCE_PCT",
        "DIRECTION_ZONE_MIN_CLOSE_POSITION", "DIRECTION_ZONE_REQUIRE_INSIDE_ZONE",
    )] == [True, 20, 2.0, 5.0, 0.35, True]
    assert PUMP_CONFIG["ZONE_REQUIRE_INSIDE_ZONE"] is True
    assert PUMP_CONFIG["DEMAND_REQUIRE_INSIDE_ZONE"] is True
    assert [PUMP_CONFIG[k] for k in (
        "ZONE_INTERVAL", "ZONE_LOOKBACK_BARS", "ZONE_BUFFER_PCT",
        "ZONE_MAX_DISTANCE_PCT", "ZONE_MIN_CLOSE_POSITION",
    )] == ["1h", 72, 1.5, 4.0, 0.35]
    assert [PUMP_CONFIG[k] for k in (
        "TIMING_EMA_FAST", "TIMING_EMA_SLOW", "TIMING_ADX_PERIOD",
        "TIMING_ADX_MIN", "TIMING_LOOKBACK_BARS",
    )] == [20, 50, 14, 20.0, 60]
    for kunci in (
        "DIRECTION_ZONE_FILTER_ENABLED", "DIRECTION_ZONE_LOOKBACK_BARS",
        "DIRECTION_ZONE_BUFFER_PCT", "DIRECTION_ZONE_MAX_DISTANCE_PCT",
        "DIRECTION_ZONE_MIN_CLOSE_POSITION", "DIRECTION_ZONE_REQUIRE_INSIDE_ZONE",
        "ZONE_REQUIRE_INSIDE_ZONE", "DEMAND_REQUIRE_INSIDE_ZONE",
    ):
        assert kunci in bt.OVERRIDE_CASTERS
        assert kunci in schema.PARAMETER_SCHEMA
    for kunci in (
        "DIRECTION_ZONE_REQUIRE_INSIDE_ZONE", "ZONE_REQUIRE_INSIDE_ZONE",
        "DEMAND_REQUIRE_INSIDE_ZONE",
    ):
        assert schema.PARAMETER_SCHEMA[kunci]["type"] == "bool"
        assert PUMP_CONFIG[kunci] is True
    assert strategy.direction_interval(PUMP_CONFIG) == "1d"
    assert strategy.zone_interval(PUMP_CONFIG) == "1h"
    assert strategy.timing_interval(PUMP_CONFIG) == "5m"
    assert strategy.direction_required_bars(PUMP_CONFIG) == 50
    assert strategy.direction_window_bars(PUMP_CONFIG) == 60
    assert strategy.direction_zone_window_bars(PUMP_CONFIG) == 21
    assert strategy.zone_window_bars(PUMP_CONFIG) == 73
    assert strategy.timing_required_bars(PUMP_CONFIG) == 50
    assert strategy.timing_window_bars(PUMP_CONFIG) == 60
    for lama in ("M15_DEMAND_FILTER_ENABLED", "M15_TREND_FILTER_ENABLED",
                 "TREND_FILTER_ENABLED", "HTF_DEMAND_FILTER_ENABLED",
                 "DAILY_TREND_FILTER_ENABLED", "DAILY_DEMAND_FILTER_ENABLED"):
        assert lama not in PUMP_CONFIG
        assert lama not in schema.PARAMETER_SCHEMA
    assert set(PUMP_CONFIG) == set(schema.PARAMETER_SCHEMA)
    assert bt.validate_params(PUMP_CONFIG) is None


def test_legacy_keys_are_migrated_on_settings_load(tmp_path, monkeypatch):
    monkeypatch.setattr(schema, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(schema, "SETTINGS_ERROR_FILE", tmp_path / "settings.error.json")
    schema.SETTINGS_FILE.write_text(json.dumps({
        "active_mode": "PAPER",
        "overrides": {"PAPER": {
            "TREND_FILTER_ENABLED": False,
            "TREND_EMA_FAST": 12,
            "TREND_EMA_SLOW": 30,
            "TREND_ADX_MIN": 25.0,
            "TREND_LOOKBACK_BARS": 90,
            "HTF_DEMAND_FILTER_ENABLED": False,
            "HTF_DEMAND_LOOKBACK_BARS": 48,
            "HTF_DEMAND_ZONE_BUFFER_PCT": 1.2,
            "M15_TREND_FILTER_ENABLED": False,
            "M15_TREND_ADX_MIN": 30.0,
            "M15_TREND_LOOKBACK_BARS": 80,
            "SL_PCT": 2.0,
        }},
    }))
    override, errors = schema.load_mode_override("PAPER")
    assert not errors
    assert not any(key.startswith(("TREND_", "HTF_DEMAND_", "M15_")) for key in override)
    assert override["DIRECTION_FILTER_ENABLED"] is False
    assert override["DIRECTION_EMA_FAST"] == 12
    assert override["DIRECTION_EMA_SLOW"] == 30
    assert override["DIRECTION_ADX_MIN"] == 25.0
    assert override["DIRECTION_LOOKBACK_BARS"] == 90
    assert override["ZONE_FILTER_ENABLED"] is False
    assert override["ZONE_LOOKBACK_BARS"] == 48
    assert override["ZONE_BUFFER_PCT"] == 1.2
    assert override["TIMING_FILTER_ENABLED"] is False
    assert override["TIMING_ADX_MIN"] == 30.0
    assert override["TIMING_LOOKBACK_BARS"] == 80
    assert override["SL_PCT"] == 2.0
    config, errors = build_config_for_mode("PAPER")
    assert not errors
    assert config["DIRECTION_FILTER_ENABLED"] is False
    assert config["DIRECTION_EMA_FAST"] == 12
    assert config["ZONE_BUFFER_PCT"] == 1.2
    assert config["TIMING_ADX_MIN"] == 30.0
    assert config["SL_PCT"] == 2.0


def test_removed_parameters_are_reported():
    removed = schema.REMOVED_PARAMETERS
    for kunci in ("TREND_FILTER_ENABLED", "HTF_DEMAND_FILTER_ENABLED",
                  "M15_TREND_FILTER_ENABLED", "M15_DEMAND_FILTER_ENABLED"):
        assert kunci in removed


@pytest.mark.parametrize("interval,valid", [
    ("1m", True), ("3m", True), ("5m", True), ("15m", True),
    ("30m", True), ("1h", True), ("1d", False),
])
def test_confirmation_interval_compatibility(interval, valid):
    cfg = {**PUMP_CONFIG, "CONFIRM_INTERVAL": interval}
    errors = schema.relation_violations(cfg, mode_aware=True)
    if valid:
        assert not errors
        assert bt.validate_params(cfg) is None
    else:
        assert "ZONE_INTERVAL" in errors
        with pytest.raises(bt.BacktestError):
            bt.validate_params(cfg)


def test_ema_slow_must_be_below_binance_limit():
    cfg = {**PUMP_CONFIG, "DIRECTION_EMA_SLOW": 1000}
    _, errors, _ = schema.validate_candidate(cfg, "PAPER")
    assert "DIRECTION_EMA_SLOW" in errors
    with pytest.raises(bt.BacktestError, match="DIRECTION"):
        bt.validate_params(cfg)


def test_lookback_must_cover_ema_and_adx():
    cfg = {**PUMP_CONFIG, "DIRECTION_LOOKBACK_BARS": 20,
           "DIRECTION_EMA_SLOW": 60}
    errors = schema.relation_violations(cfg, mode_aware=True)
    assert "DIRECTION_LOOKBACK_BARS" in errors
    cfg = {**PUMP_CONFIG, "TIMING_LOOKBACK_BARS": 20,
           "TIMING_EMA_SLOW": 60}
    errors = schema.relation_violations(cfg, mode_aware=True)
    assert "TIMING_LOOKBACK_BARS" in errors


def test_direction_requires_minimum_daily_history():
    cfg = {**PUMP_CONFIG}
    verdict = strategy.evaluate_direction([], cfg)
    assert verdict["ok"] is False
    assert "kurang" in verdict["reason"]
    kurang = [daily(i, 100 + i * 0.2, 100 + i * 0.2) for i in range(49)]
    verdict = strategy.evaluate_direction(kurang, cfg)
    assert verdict["ok"] is False
    cukup = [daily(i, 100 + i * 0.2, 100 + i * 0.2) for i in range(50)]
    verdict = strategy.evaluate_direction(cukup, cfg)
    assert verdict["ok"] is True
    assert verdict["interval"] == "1d"
    assert verdict["bars"] == 50


def test_direction_fails_closed_on_flat_daily_candles():
    cfg = {**PUMP_CONFIG}
    datar = [daily(i, 100, 100) for i in range(60)]
    verdict = strategy.evaluate_direction(datar, cfg)
    assert verdict["ok"] is False


def test_direction_verdict_uses_only_closed_daily_candles():
    naik = [daily(i, 100 + i * 0.2, 100 + i * 0.2) for i in range(60)]
    sinyal_close = naik[-1].close_time
    with_future = list(naik)
    with_future.append(daily(60, 200, 10))
    sebelum = strategy.evaluate_direction(
        with_future, PUMP_CONFIG, signal_close_time_ms=sinyal_close
    )
    sesudah = strategy.evaluate_direction(with_future, PUMP_CONFIG)
    assert sebelum["ok"] is True
    assert sesudah["ok"] is False


def test_direction_lookup_excludes_future_crash():
    naik = [daily(i, 100 + i * 0.2, 100 + i * 0.2) for i in range(60)]
    with_future = list(naik)
    with_future.append(daily(60, 200, 10))
    lookup = parity.make_direction_lookup(
        with_future, PUMP_CONFIG, "1d", sudah_dirangkai=True
    )
    sebelum = lookup.verdict_at(naik[-1].close_time)
    sesudah = lookup.verdict_at(with_future[-1].close_time)
    assert sebelum["ok"] is True
    assert sebelum["interval"] == "1d"
    assert sesudah["ok"] is False
    assert len(lookup.window_at(naik[-1].close_time)) == 60


def test_zone_lookup_uses_h1_demand_zone():
    harga = [hourly(i, 100 - i * 0.05, 100 - i * 0.05) for i in range(80)]
    lookup = parity.make_zone_lookup(harga, PUMP_CONFIG, "1h", sudah_dirangkai=True)
    hasil = lookup.zone_at(harga[-1].close_time)
    assert hasil["interval"] == "1h"
    assert "ok" in hasil


def test_timing_lookup_uses_confirmation_candles():
    bars = rising_m5(300)
    lookup = parity.make_timing_lookup(bars, PUMP_CONFIG)
    hasil = lookup.verdict_at(bars[-1].close_time)
    assert hasil["interval"] == "5m"
    assert hasil["ok"] is True
    assert len(lookup.window_at(bars[-1].close_time)) == 60


def test_timing_requires_minimum_m5_history():
    cfg = {**PUMP_CONFIG}
    verdict = strategy.evaluate_timing([], cfg)
    assert verdict["ok"] is False
    kurang = rising_m5(49)
    verdict = strategy.evaluate_timing(kurang, cfg)
    assert verdict["ok"] is False
    cukup = rising_m5(50)
    verdict = strategy.evaluate_timing(cukup, cfg)
    assert verdict["ok"] is True


def test_missing_live_provider_fails_closed():
    for verdict in (market.direction_verdict, market.zone_verdict):
        assert verdict("SOLUSDT", PUMP_CONFIG, None)["ok"] is False
        assert verdict("SOLUSDT", PUMP_CONFIG, lambda sym: [])["ok"] is False

        def broken(symbol):
            raise OSError("offline")

        assert verdict("SOLUSDT", PUMP_CONFIG, broken)["ok"] is False


def test_candidate_pipeline_checks_three_layers_at_signal_close(monkeypatch):
    daily_bars = [daily(i, 100 + i * 0.05, 100 + i * 0.05) for i in range(60)]
    hourly_bars = hourly_set()
    m5_bars = signal_set()
    candidate = market.Candidate("SOLUSDT", "SOL", 10.0, 1_000_000, 100.3)
    cfg = {**PUMP_CONFIG}
    monkeypatch.setattr(market, "filter_and_rank_candidates",
                        lambda *args: [candidate])
    monkeypatch.setattr(market, "detect_entry_setup",
                        lambda *args: market.SetupResult(True, "sinyal", 100.3, .1))
    best = market.find_best_candidate(
        [], lambda symbol: m5_bars, cfg,
        direction_provider=lambda symbol: daily_bars,
        zone_provider=lambda symbol: hourly_bars,
    )
    assert best is candidate
    assert best.direction["ok"] is True
    assert best.zone["ok"] is True
    assert best.timing["ok"] is True


def test_candidate_pipeline_rejects_when_direction_history_missing(monkeypatch):
    candidate = market.Candidate("SOLUSDT", "SOL", 10.0, 1_000_000, 100.3)
    cfg = {**PUMP_CONFIG}
    monkeypatch.setattr(market, "filter_and_rank_candidates",
                        lambda *args: [candidate])
    monkeypatch.setattr(market, "detect_entry_setup",
                        lambda *args: market.SetupResult(True, "sinyal", 100.3, .1))
    m5_bars = signal_set()
    best = market.find_best_candidate(
        [], lambda symbol: m5_bars, cfg,
        direction_provider=lambda symbol: [],
        zone_provider=lambda symbol: hourly_set(),
    )
    assert best is None
    assert candidate.direction["ok"] is False


def test_scan_diagnostics_report_three_layers():
    diagnostics = market.ScanDiagnostics()
    candidate = market.Candidate("SOLUSDT", "SOL", 10.0, 1_000_000, 100.3)
    cfg = {**PUMP_CONFIG}
    diagnostics.checked_candidates = 1
    diagnostics.reject("DIRECTION", "SOLUSDT", "arah 1d ditolak")
    diagnostics.reject("ZONE", "SOLUSDT", "zona 1h ditolak")
    diagnostics.reject("TIMING", "SOLUSDT", "timing 5m ditolak")
    lines = diagnostics.log_lines(PUMP_CONFIG)
    teks = "\n".join(lines)
    assert "arah 1d" in teks
    assert "zona 1h" in teks
    assert "timing 5m" in teks
    assert diagnostics.rejection_counts == {"DIRECTION": 1, "ZONE": 1, "TIMING": 1}
    assert candidate.symbol == "SOLUSDT"


def test_live_direction_cache_is_shared_and_rejects_stale_data(monkeypatch):
    now = START + 61 * DAY_MS
    rows = [daily(i, 100, 100) for i in range(61)]

    def raw(row):
        return [row.open_time, row.open, row.high, row.low,
                row.close, row.volume, row.close_time, row.quote_volume]

    class OfflineClient:
        calls = []

        def get_klines(self, symbol, interval, limit):
            self.calls.append((symbol, interval, limit))
            return [raw(row) for row in rows[-limit:]]

    client = OfflineClient()
    cache = bot.DirectionCache(client, PUMP_CONFIG)
    monkeypatch.setattr(bot.state_mod, "now_ms", lambda: now)
    closed = cache.provider("SOLUSDT")
    assert len(closed) == 60
    assert closed[-1].open_time == START + 60 * DAY_MS
    assert cache.provider("SOLUSDT") == closed
    assert client.calls == [("SOLUSDT", "1d", 61)]


def test_live_zone_cache_uses_hourly_candles(monkeypatch):
    now = START + 100 * H1_MS
    rows = [hourly(i, 100, 100) for i in range(100)]

    def raw(row):
        return [row.open_time, row.open, row.high, row.low,
                row.close, row.volume, row.close_time, row.quote_volume]

    class OfflineClient:
        calls = []

        def get_klines(self, symbol, interval, limit):
            self.calls.append((symbol, interval, limit))
            return [raw(row) for row in rows[-limit:]]

    client = OfflineClient()
    cache = bot.ZoneCache(client, PUMP_CONFIG)
    monkeypatch.setattr(bot.state_mod, "now_ms", lambda: now)
    closed = cache.provider("SOLUSDT")
    assert len(closed) == 73
    assert client.calls == [("SOLUSDT", "1h", 74)]


def test_extreme_window_respects_binance_maximum():
    cfg = {**PUMP_CONFIG, "DIRECTION_LOOKBACK_BARS": 999,
           "DIRECTION_EMA_SLOW": 999}
    assert bt.validate_params(cfg) is None

    class OfflineClient:
        calls = []

        def get_klines(self, symbol, interval, limit):
            self.calls.append((interval, limit))
            rows = [daily(i, 100, 100) for i in range(1001)]
            return [[k.open_time, k.open, k.high, k.low, k.close,
                     k.volume, k.close_time, k.quote_volume] for k in rows[-limit:]]

    client = OfflineClient()
    cache = bot.DirectionCache(client, cfg)
    assert cache.window_bars() == 999
    closed = cache.window_klines("SOLUSDT", START + 1000 * DAY_MS + 10_000)
    assert len(closed) == 999
    assert client.calls == [("1d", 1000)]


def test_single_and_portfolio_backtests_evaluate_three_layers():
    cfg = {**simple_backtest_config()}
    cfg["TIMING_FILTER_ENABLED"] = True
    bars = rising_m5()
    result = bt.run_backtest(bars, cfg, 365)
    assert result.timing_scans > 0
    assert result.timing_skips == 0
    assert result.mark_to_market_open is True
    with KlineStore.from_klines({"SOLUSDT": bars}) as store:
        portfolio = pbt.run_portfolio_backtest(store, cfg, "5m", warmup_ms=365*M5_MS)
    assert portfolio.timing_scans > 0
    assert portfolio.timing_skips == 0
    assert portfolio.mark_to_market_open is True


def test_portfolio_missing_direction_data_does_not_allow_entry(monkeypatch):
    cfg = {**simple_backtest_config()}
    cfg["DIRECTION_FILTER_ENABLED"] = True
    monkeypatch.setattr(pbt, "_siapkan_arah", lambda *args: {})
    with KlineStore.from_klines({"SOLUSDT": rising_m5()}) as store:
        portfolio = pbt.run_portfolio_backtest(store, cfg, "5m", warmup_ms=365*M5_MS)
    assert portfolio.direction_scans > 0
    assert portfolio.direction_skips == portfolio.direction_scans
    assert portfolio.mark_to_market_open is False
    assert any(x.reason == "FILTER_DIRECTION" for x in portfolio.skipped)


def test_portfolio_missing_zone_data_does_not_allow_entry(monkeypatch):
    cfg = {**simple_backtest_config()}
    cfg["ZONE_FILTER_ENABLED"] = True
    monkeypatch.setattr(pbt, "_siapkan_zona", lambda *args: {})
    with KlineStore.from_klines({"SOLUSDT": rising_m5()}) as store:
        portfolio = pbt.run_portfolio_backtest(store, cfg, "5m", warmup_ms=365*M5_MS)
    assert portfolio.zone_scans > 0
    assert portfolio.zone_skips == portfolio.zone_scans
    assert portfolio.mark_to_market_open is False
    assert any(x.reason == "FILTER_ZONE" for x in portfolio.skipped)


def test_portfolio_missing_timing_data_does_not_allow_entry(monkeypatch):
    cfg = {**simple_backtest_config()}
    cfg["TIMING_FILTER_ENABLED"] = True
    monkeypatch.setattr(pbt, "_siapkan_timing", lambda *args: {})
    with KlineStore.from_klines({"SOLUSDT": rising_m5()}) as store:
        portfolio = pbt.run_portfolio_backtest(store, cfg, "5m", warmup_ms=365*M5_MS)
    assert portfolio.timing_scans > 0
    assert portfolio.timing_skips == portfolio.timing_scans
    assert portfolio.mark_to_market_open is False
    assert any(x.reason == "FILTER_TIMING" for x in portfolio.skipped)


def test_native_lookups_bypass_aggregation_warmup():
    cfg = {**PUMP_CONFIG}
    daily_bars = [daily(i, 100 + i * 0.2, 100 + i * 0.2) for i in range(60)]
    lookup = parity.make_direction_lookup(
        daily_bars, cfg, "1d", sudah_dirangkai=True
    )
    assert lookup is not None
    assert lookup.window == 60
    assert lookup.required == 50
    assert lookup.verdict_at(daily_bars[-1].close_time)["ok"] is True


def test_dashboard_defaults_and_minimum_period():
    from web import dashboard as dash

    minimal, catatan = dash._bt_min_days_note(PUMP_CONFIG, "5m")
    assert minimal >= 2
    assert catatan
    cfg = {**PUMP_CONFIG, "DIRECTION_FILTER_ENABLED": False,
           "DIRECTION_ZONE_FILTER_ENABLED": False,
           "ZONE_FILTER_ENABLED": False, "TIMING_FILTER_ENABLED": False}
    assert dash._bt_min_days_note(cfg, "5m")[0] == 2
    with dash.app.test_client() as client:
        response = client.get("/api/backtest/defaults",
                              base_url=f"http://localhost:{dash._DASHBOARD_PORT}")
        assert response.status_code == 200
        data = response.get_json()
    assert data["direction_enabled"] is True
    assert data["direction_zone_enabled"] is True
    assert data["direction_zone_interval"] == "1d"
    assert data["direction_zone_max_distance_pct"] == 5.0
    assert data["zone_enabled"] is True
    assert data["timing_enabled"] is True
    assert data["direction_interval"] == "1d"
    assert data["zone_interval"] == "1h"
    assert data["timing_interval"] == "5m"
    assert data["bars_per_day"] == bt.bars_per_day("5m")
    assert not any(key.startswith(("m15_", "trend_", "htf_")) for key in data)


def test_single_grid_search_keeps_timing_enabled():
    cfg = simple_backtest_config()
    cfg["TIMING_FILTER_ENABLED"] = True
    result = grid_search.run_grid_search(
        rising_m5(), cfg, {"SL_PCT": [cfg["SL_PCT"]]},
        warmup_bars=365, rasio_latih=1.0, min_trades=1,
    )
    assert result.total_kombinasi == 1
    assert result.bar_latih > 0
    assert result.hasil[0].latih["timing_scans"] > 0


def test_portfolio_grid_search_keeps_timing_enabled():
    cfg = simple_backtest_config()
    cfg["TIMING_FILTER_ENABLED"] = True
    with KlineStore.from_klines({"SOLUSDT": rising_m5()}) as store:
        result = grid_search.run_portfolio_grid_search(
            store, cfg, "5m", 365*M5_MS, {"SL_PCT": [cfg["SL_PCT"]]},
            rasio_latih=1.0, min_trades=1,
        )
    assert result.total_kombinasi == 1
    assert result.dilewati == 0
    assert result.hasil[0].latih["timing_scans"] > 0


class FakeBinanceClient:
    """Client palsu yang menghasilkan candle naik untuk semua timeframe."""

    step_map = {"1d": DAY_MS, "1h": H1_MS, "5m": M5_MS}

    def __init__(self):
        self.calls = []

    def get_klines(
        self,
        symbol,
        interval,
        limit=500,
        start_time_ms=None,
        end_time_ms=None,
        **kwargs,
    ):
        self.calls.append((symbol, interval, limit, start_time_ms, end_time_ms))
        step = self.step_map.get(interval, M5_MS)
        mulai = int(start_time_ms if start_time_ms is not None else START)
        selesai = int(end_time_ms if end_time_ms is not None else START + 400 * step)
        out = []
        t = mulai - (mulai % step)
        while t < selesai and len(out) < 1000:
            close = 100.0 + (t // step) * 0.05
            out.append([t, close, close + 0.5, close - 0.5, close, 1000.0, t + step - 1, 100000.0])
            t += step
        return out


def test_native_htf_path_avoids_aggregation_warmup():
    cfg = {
        **simple_backtest_config(),
        "DIRECTION_FILTER_ENABLED": True,
        "ZONE_FILTER_ENABLED": True,
        "TIMING_FILTER_ENABLED": True,
    }
    hari = 30
    end_ms = START + hari * DAY_MS
    start_ms = end_ms - hari * DAY_MS
    warmup_ms = DAY_MS + 30 * M5_MS
    client = FakeBinanceClient()
    arah, zona = pbt.fetch_native_lookups(
        client, ["SOLUSDT"], cfg, start_ms, end_ms, None, 1
    )
    assert set(arah) == {"SOLUSDT"}
    assert set(zona) == {"SOLUSDT"}
    interval_dipanggil = sorted({call[1] for call in client.calls})
    assert interval_dipanggil == ["1d", "1h"]
    awal_harian = min(call[3] for call in client.calls if call[1] == "1d")
    assert awal_harian <= start_ms - 59 * DAY_MS
    bars = []
    t = start_ms - warmup_ms
    while t < end_ms:
        close = 100.0 + (t // M5_MS) * 0.001
        bars.append(
            strategy.Kline(
                t, close, close + 0.05, close - 0.05, close,
                t + M5_MS - 1, 1000.0, 100000.0,
            )
        )
        t += M5_MS
    with KlineStore.from_klines({"SOLUSDT": bars}) as store:
        portfolio = pbt.run_portfolio_backtest(
            store, cfg, "5m", warmup_ms=warmup_ms,
            prebuilt=(None, None, arah, zona, None),
        )
    assert portfolio.direction_scans > 0
    assert portfolio.direction_skips == 0
    assert portfolio.zone_scans == portfolio.direction_scans
    assert portfolio.timing_scans == portfolio.direction_scans
    assert not any(
        "dinaikkan" in w for w in portfolio.warnings
    )


def test_native_htf_lookup_missing_symbol_fails_closed():
    cfg = {
        **simple_backtest_config(),
        "DIRECTION_FILTER_ENABLED": True,
        "ZONE_FILTER_ENABLED": True,
        "TIMING_FILTER_ENABLED": True,
    }
    hari = 30
    end_ms = START + hari * DAY_MS
    start_ms = end_ms - hari * DAY_MS
    warmup_ms = DAY_MS + 30 * M5_MS
    client = FakeBinanceClient()
    arah, zona = pbt.fetch_native_lookups(
        client, ["SOLUSDT"], cfg, start_ms, end_ms, None, 1
    )
    bars = []
    t = start_ms - warmup_ms
    while t < end_ms:
        close = 100.0 + (t // M5_MS) * 0.001
        bars.append(
            strategy.Kline(
                t, close, close + 0.05, close - 0.05, close,
                t + M5_MS - 1, 1000.0, 100000.0,
            )
        )
        t += M5_MS
    with KlineStore.from_klines({"SOLUSDT": bars}) as store:
        portfolio = pbt.run_portfolio_backtest(
            store, cfg, "5m", warmup_ms=warmup_ms,
            prebuilt=(None, None, {}, {}, None),
        )
    assert portfolio.direction_scans > 0
    assert portfolio.direction_skips == portfolio.direction_scans
    assert portfolio.mark_to_market_open is False
    assert any(x.reason == "FILTER_DIRECTION" for x in portfolio.skipped)


def test_live_and_backtest_give_identical_verdicts_for_same_data():
    cfg = {**PUMP_CONFIG}
    arah_candle = [daily(i, 100 + i * 0.2, 100 + i * 0.2) for i in range(60)]
    zona_candle = hourly_set()
    m5_candle = signal_set()
    sinyal_close = int(m5_candle[-1].close_time)

    live_arah = market.direction_verdict(
        "SOLUSDT", cfg, lambda symbol: arah_candle, sinyal_close
    )
    live_zona = market.zone_verdict(
        "SOLUSDT", cfg, lambda symbol: zona_candle, sinyal_close
    )
    live_timing = market.timing_verdict(cfg, m5_candle, sinyal_close)

    arah_lookup = parity.make_direction_lookup(
        arah_candle, cfg, "1d", sudah_dirangkai=True
    )
    zona_lookup = parity.make_zone_lookup(
        zona_candle, cfg, "1h", sudah_dirangkai=True
    )
    timing_lookup = parity.make_timing_lookup(m5_candle, cfg)

    bt_arah = arah_lookup.verdict_at(sinyal_close)
    bt_zona = zona_lookup.zone_at(sinyal_close)
    bt_timing = timing_lookup.verdict_at(sinyal_close)

    assert live_arah["ok"] is True
    assert live_zona["ok"] is True
    assert live_timing["ok"] is True
    for live, back in (
        (live_arah, bt_arah),
        (live_zona, bt_zona),
        (live_timing, bt_timing),
    ):
        assert live["ok"] == back["ok"]
        assert live["bars"] == back["bars"]
        assert live["interval"] == back["interval"]
        assert live.get("close") == back.get("close")
        assert live.get("ema_fast") == back.get("ema_fast")
        assert live.get("ema_slow") == back.get("ema_slow")
        assert live.get("adx") == back.get("adx")
        assert live.get("distance_pct") == back.get("distance_pct")
        assert live.get("close_position") == back.get("close_position")


def test_live_and_backtest_reject_the_same_candidate():
    cfg = {**PUMP_CONFIG}
    arah_candle = [daily(i, 100 + i * 0.2, 100 + i * 0.2) for i in range(60)]
    m5_candle = signal_set()
    sinyal_close = int(m5_candle[-1].close_time)
    arah_lookup = parity.make_direction_lookup(
        arah_candle, cfg, "1d", sudah_dirangkai=True
    )
    bt_ok = arah_lookup.verdict_at(sinyal_close)["ok"]
    live_ok = market.direction_verdict(
        "SOLUSDT", cfg, lambda symbol: arah_candle, sinyal_close
    )["ok"]
    assert bt_ok == live_ok is True

    arah_kurang = arah_candle[:10]
    bt_ok = parity.make_direction_lookup(
        arah_kurang, cfg, "1d", sudah_dirangkai=True
    ).verdict_at(sinyal_close)["ok"]
    live_ok = market.direction_verdict(
        "SOLUSDT", cfg, lambda symbol: arah_kurang, sinyal_close
    )["ok"]
    assert bt_ok == live_ok is False


@pytest.mark.parametrize("mode", ["PAPER", "LIVE"])
def test_both_modes_resolve_the_same_three_layers(mode, tmp_path, monkeypatch):
    monkeypatch.setattr(schema, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(schema, "SETTINGS_ERROR_FILE", tmp_path / "settings.error.json")
    schema.SETTINGS_FILE.write_text(json.dumps({
        "active_mode": mode,
        "overrides": {mode: {
            "TREND_FILTER_ENABLED": False,
            "TREND_EMA_FAST": 12,
            "TREND_EMA_SLOW": 30,
            "HTF_DEMAND_FILTER_ENABLED": False,
            "M15_TREND_FILTER_ENABLED": False,
        }},
    }))
    cfg, errors = build_config_for_mode(mode)
    assert not errors
    assert cfg["DIRECTION_FILTER_ENABLED"] is False
    assert cfg["ZONE_FILTER_ENABLED"] is False
    assert cfg["TIMING_FILTER_ENABLED"] is False
    assert cfg["DIRECTION_EMA_FAST"] == 12
    assert cfg["DIRECTION_EMA_SLOW"] == 30
    assert strategy.direction_interval(cfg) == "1d"
    assert strategy.zone_interval(cfg) == "1h"
    assert strategy.timing_interval(cfg) == "5m"
    assert not any(
        key.startswith(("TREND_", "HTF_DEMAND_", "M15_")) for key in cfg
    )


def test_confirm_cutoff_helper():
    assert strategy.last_closed_bar_close_ms("5m", 1_792_248_300_000) == 1_792_248_299_999
    assert strategy.last_closed_bar_close_ms("5m", 1_792_248_299_999) == 1_792_247_999_999
    assert strategy.last_closed_bar_close_ms("1d", 1_792_248_300_000) == 1_792_195_199_999
    with pytest.raises(ValueError):
        strategy.last_closed_bar_close_ms("7m", 1_792_248_300_000)


def test_backtest_menilai_layer_arah_sebelum_konfirmasi_m5(monkeypatch):
    cfg = {
        **simple_backtest_config(),
        "DIRECTION_FILTER_ENABLED": True,
        "ROLLING_VOLUME_FILTER_ENABLED": True,
        "ROLLING_VOLUME_SURGE_MULT": 1_000_000.0,
    }
    monkeypatch.setattr(pbt, "_siapkan_arah", lambda *args: {})
    with KlineStore.from_klines({"SOLUSDT": rising_m5()}) as store:
        portfolio = pbt.run_portfolio_backtest(
            store, cfg, "5m", warmup_ms=365 * M5_MS
        )
    assert portfolio.direction_scans > 0
    assert portfolio.direction_skips == portfolio.direction_scans
    assert not portfolio.trades


def zona_harian(sinyal, jumlah=21):
    dasar = [daily(i, 100.0, 100.0, low=99.0, high=101.0) for i in range(jumlah - 1)]
    return dasar + [sinyal]


def sinyal_di_dalam_zona():
    return daily(20, 100.2, 100.5, low=100.0, high=100.8)


def sinyal_di_atas_zona():
    return daily(20, 101.0, 101.5, low=100.5, high=102.0)


def test_zona_demand_daily_menuntut_harga_di_dalam_zona():
    longgar = {**PUMP_CONFIG, "DIRECTION_ZONE_REQUIRE_INSIDE_ZONE": False}
    dalam = zona_harian(sinyal_di_dalam_zona())
    atas = zona_harian(sinyal_di_atas_zona())

    hasil = strategy.evaluate_direction_zone(dalam, PUMP_CONFIG)
    assert hasil["ok"] is True
    assert hasil["interval"] == "1d"
    assert hasil["required"] == 21
    assert hasil["bars"] == 21
    assert hasil["zone_low"] == pytest.approx(99.0)
    assert hasil["zone_high"] == pytest.approx(100.98)
    assert hasil["distance_pct"] == 0.0
    assert "di dalam zona" in hasil["reason"]

    ketat = strategy.evaluate_direction_zone(atas, PUMP_CONFIG)
    assert ketat["ok"] is False
    assert "wajib di dalam zona" in ketat["reason"]
    assert ketat["distance_pct"] == pytest.approx(0.515, abs=1e-3)
    assert strategy.evaluate_direction_zone(atas, longgar)["ok"] is True

    jauh = zona_harian(daily(20, 101.0, 108.0, low=100.5, high=108.5))
    for cfg in (PUMP_CONFIG, longgar):
        hasil_jauh = strategy.evaluate_direction_zone(jauh, cfg)
        assert hasil_jauh["ok"] is False
        assert "terlalu jauh" in hasil_jauh["reason"]

    tembus = zona_harian(daily(20, 99.5, 98.0, low=97.5, high=100.0))
    assert "ditembus" in strategy.evaluate_direction_zone(tembus, PUMP_CONFIG)["reason"]

    lemah = zona_harian(daily(20, 101.5, 100.0, low=99.5, high=102.0))
    assert "reaksi demand lemah" in strategy.evaluate_direction_zone(
        lemah, PUMP_CONFIG
    )["reason"]


def test_nol_pada_kunci_jarak_berarti_toleransi_nol():
    longgar_nol = {
        **PUMP_CONFIG,
        "DIRECTION_ZONE_REQUIRE_INSIDE_ZONE": False,
        "DIRECTION_ZONE_MAX_DISTANCE_PCT": 0.0,
    }
    atas = zona_harian(sinyal_di_atas_zona())
    dalam = zona_harian(sinyal_di_dalam_zona())
    hasil_atas = strategy.evaluate_direction_zone(atas, longgar_nol)
    assert hasil_atas["ok"] is False
    assert "batas 0%" in hasil_atas["reason"]
    assert strategy.evaluate_direction_zone(dalam, longgar_nol)["ok"] is True


def test_mode_wajib_di_dalam_zona_berlaku_di_tiga_timeframe():
    h1 = [hourly(i, 100.0, 100.0) for i in range(80)]
    dalam = strategy.evaluate_zone(h1, PUMP_CONFIG)
    assert dalam["ok"] is True

    h1_atas = [hourly(i, 100.0, 100.0) for i in range(79)] + [
        hourly(79, 100.5, 101.0)
    ]
    ketat_h1 = strategy.evaluate_zone(h1_atas, PUMP_CONFIG)
    longgar_h1 = strategy.evaluate_zone(
        h1_atas, {**PUMP_CONFIG, "ZONE_REQUIRE_INSIDE_ZONE": False}
    )
    assert ketat_h1["ok"] is False and "wajib di dalam zona" in ketat_h1["reason"]
    assert longgar_h1["ok"] is True

    m5 = [candle(i, 100.0, 100.0, step=M5_MS) for i in range(25)]
    assert "di dalam zona" in strategy.detect_demand_zone(m5, PUMP_CONFIG)["reason"]

    ujung = [candle(i, 100.0, 100.0, step=M5_MS) for i in range(24)] + [
        candle(24, 100.5, 101.0, step=M5_MS)
    ]
    ketat_m5 = strategy.detect_demand_zone(ujung, PUMP_CONFIG)
    longgar_m5 = strategy.detect_demand_zone(
        ujung, {**PUMP_CONFIG, "DEMAND_REQUIRE_INSIDE_ZONE": False}
    )
    assert ketat_m5["ok"] is False and "wajib di dalam zona" in ketat_m5["reason"]
    assert longgar_m5["ok"] is True
    assert "di dalam zona" in dalam["reason"]


def test_zona_demand_daily_bisa_dimatikan_dan_butuh_riwayat():
    nonaktif = {**PUMP_CONFIG, "DIRECTION_ZONE_FILTER_ENABLED": False}
    assert strategy.direction_zone_window_bars(nonaktif) == 0
    assert strategy.evaluate_direction_zone([], nonaktif)["ok"] is True
    assert "nonaktif" in strategy.evaluate_direction_zone([], nonaktif)["reason"]

    kurang = zona_harian(sinyal_di_dalam_zona())[:15]
    hasil = strategy.evaluate_direction_zone(kurang, PUMP_CONFIG)
    assert hasil["ok"] is False
    assert "kurang" in hasil["reason"]
    assert hasil["required"] == 21


def test_zona_demand_daily_pakai_ari_seri_arah_tanpa_lookahead():
    dekat = zona_harian(sinyal_di_dalam_zona())
    batas = dekat[-1].close_time
    dengan_crash = dekat + [daily(21, 108.0, 60.0, low=59.0, high=109.0)]
    sebelum = strategy.evaluate_direction_zone(
        dengan_crash, PUMP_CONFIG, signal_close_time_ms=batas
    )
    sesudah = strategy.evaluate_direction_zone(dengan_crash, PUMP_CONFIG)
    assert sebelum["ok"] is True
    assert sesudah["ok"] is False

    lookup = parity.make_direction_lookup(
        dengan_crash, PUMP_CONFIG, "1d", sudah_dirangkai=True
    )
    assert lookup.zone_window == 21
    via_lookup = lookup.zone_at(batas)
    assert via_lookup["ok"] is True
    assert via_lookup["bars"] == 21
    assert via_lookup["zone_low"] == sebelum["zone_low"]
    assert (
        market.daily_zone_verdict(
            "SOLUSDT", PUMP_CONFIG, lambda symbol: dengan_crash, batas
        )
        == sebelum
    )


def test_zona_demand_daily_fail_closed_bila_data_tidak_tersedia():
    assert market.daily_zone_verdict("SOLUSDT", PUMP_CONFIG, None)["ok"] is False
    assert market.daily_zone_verdict("SOLUSDT", PUMP_CONFIG, lambda s: [])["ok"] is False

    def rusak(symbol):
        raise OSError("offline")

    assert market.daily_zone_verdict("SOLUSDT", PUMP_CONFIG, rusak)["ok"] is False
    nonaktif = {**PUMP_CONFIG, "DIRECTION_ZONE_FILTER_ENABLED": False}
    assert market.daily_zone_verdict("SOLUSDT", nonaktif, None)["ok"] is True


def test_zona_demand_daily_dinilai_setelah_arah_dan_sebelum_zona_h1(monkeypatch):
    kandidat = market.Candidate("SOLUSDT", "SOL", 10.0, 1_000_000, 100.3)
    urutan = []
    monkeypatch.setattr(
        market, "filter_and_rank_candidates", lambda *args: [kandidat]
    )
    monkeypatch.setattr(
        market,
        "direction_verdict",
        lambda *args: urutan.append("arah")
        or {"ok": True, "interval": "1d", "reason": "arah lolos"},
    )
    monkeypatch.setattr(
        market,
        "daily_zone_verdict",
        lambda *args: urutan.append("zona_harian")
        or {"ok": False, "interval": "1d", "reason": "harga terlalu jauh"},
    )
    monkeypatch.setattr(
        market,
        "zone_verdict",
        lambda *args: urutan.append("zona_h1")
        or {"ok": True, "interval": "1h", "reason": "zona lolos"},
    )
    diag = market.ScanDiagnostics()
    best = market.find_best_candidate(
        [],
        lambda symbol: urutan.append("unduh_m5") or dengan_crash_seri(),
        {**PUMP_CONFIG},
        direction_provider=lambda symbol: [],
        zone_provider=lambda symbol: [],
        diagnostics=diag,
    )
    assert best is None
    assert urutan == ["arah", "zona_harian"]
    assert kandidat.daily_zone["ok"] is False
    assert "zona demand 1d ditolak" in kandidat.confirm_reason
    assert diag.rejection_counts == {"DAILY_ZONE": 1}
    assert any(
        "zona demand 1d" in baris and "harga terlalu jauh" in baris
        for baris in diag.log_lines(PUMP_CONFIG)
    )


def dengan_crash_seri():
    return [candle(0, 100.0, 100.0, low=99.0, high=101.0)]


def _arah_harian_sebelum_seri(bars, cfg):
    lookup = parity.make_direction_lookup(bars, cfg, "1d", sudah_dirangkai=True)
    return {"SOLUSDT": lookup}


def test_zona_demand_daily_menyaring_entry_di_backtest_portfolio():
    cfg = {**simple_backtest_config()}
    cfg["DIRECTION_ZONE_FILTER_ENABLED"] = True
    harian = [
        candle(i - 20, 100.0, 100.0, low=99.0, high=101.0, step=DAY_MS)
        for i in range(20)
    ]
    lolos = harian + [
        candle(0, 100.2, 100.5, low=100.0, high=100.8, step=DAY_MS)
    ]
    jauh = harian + [
        candle(0, 101.0, 108.0, low=100.5, high=108.5, step=DAY_MS)
    ]
    assert strategy.evaluate_direction_zone(lolos, cfg)["ok"] is True
    assert strategy.evaluate_direction_zone(jauh, cfg)["ok"] is False

    with KlineStore.from_klines({"SOLUSDT": rising_m5()}) as store:
        portfolio_lolos = pbt.run_portfolio_backtest(
            store, cfg, "5m", warmup_ms=365 * M5_MS,
            prebuilt=(None, None, _arah_harian_sebelum_seri(lolos, cfg), {}, None),
        )
        portfolio_jauh = pbt.run_portfolio_backtest(
            store, cfg, "5m", warmup_ms=365 * M5_MS,
            prebuilt=(None, None, _arah_harian_sebelum_seri(jauh, cfg), {}, None),
        )
    assert portfolio_lolos.daily_zone_scans > 0
    assert portfolio_lolos.daily_zone_skips == 0
    assert portfolio_lolos.direction_scans == 0
    assert portfolio_lolos.mark_to_market_open is True
    assert portfolio_jauh.daily_zone_scans > 0
    assert portfolio_jauh.daily_zone_skips == portfolio_jauh.daily_zone_scans
    assert portfolio_jauh.mark_to_market_open is False
    assert not portfolio_jauh.trades
    assert any(x.reason == "FILTER_DAILY_ZONE" for x in portfolio_jauh.skipped)
    assert any("zona demand" in w for w in portfolio_jauh.warnings)


def test_zona_demand_daily_fail_closed_bila_lookup_tidak_ada(monkeypatch):
    cfg = {**simple_backtest_config()}
    cfg["DIRECTION_ZONE_FILTER_ENABLED"] = True
    monkeypatch.setattr(pbt, "_siapkan_arah", lambda *args: {})
    with KlineStore.from_klines({"SOLUSDT": rising_m5()}) as store:
        portfolio = pbt.run_portfolio_backtest(store, cfg, "5m", warmup_ms=365 * M5_MS)
    assert portfolio.daily_zone_scans > 0
    assert portfolio.daily_zone_skips == portfolio.daily_zone_scans
    assert portfolio.mark_to_market_open is False
    assert any(x.reason == "FILTER_DAILY_ZONE" for x in portfolio.skipped)


def test_cache_sinyal_tidak_menggandakan_penghitung_gerbang(monkeypatch):
    cfg = {**simple_backtest_config()}
    cfg["DIRECTION_FILTER_ENABLED"] = True
    cfg["DIRECTION_ZONE_FILTER_ENABLED"] = True
    monkeypatch.setattr(pbt, "_siapkan_arah", lambda *args: {})
    bars = rising_m5()
    with KlineStore.from_klines({"SOLUSDT": bars}) as store:
        cache = pbt.precompute_entry_signals(store, cfg, "5m", warmup_ms=365 * M5_MS)
        dengan_cache = pbt.run_portfolio_backtest(
            store, cfg, "5m", warmup_ms=365 * M5_MS, entry_signal_cache=cache
        )
        tanpa_cache = pbt.run_portfolio_backtest(
            store, cfg, "5m", warmup_ms=365 * M5_MS
        )
    assert cache.direction_scans > 0
    assert cache.daily_zone_scans == 0
    assert dengan_cache.direction_scans == cache.direction_scans
    assert dengan_cache.direction_skips == cache.direction_skips
    assert dengan_cache.daily_zone_scans == cache.daily_zone_scans
    assert dengan_cache.daily_zone_skips == cache.daily_zone_skips
    assert dengan_cache.direction_scans == tanpa_cache.direction_scans
    assert len(dengan_cache.skipped) == len(tanpa_cache.skipped)


def test_zona_demand_daily_tidak_menambah_unduhan_di_jalur_native():
    cfg = {
        **simple_backtest_config(),
        "DIRECTION_FILTER_ENABLED": True,
        "DIRECTION_ZONE_FILTER_ENABLED": True,
        "ZONE_FILTER_ENABLED": True,
        "TIMING_FILTER_ENABLED": True,
    }
    hari = 30
    end_ms = START + hari * DAY_MS
    start_ms = end_ms - hari * DAY_MS
    warmup_ms = DAY_MS + 30 * M5_MS
    client = FakeBinanceClient()
    arah, zona = pbt.fetch_native_lookups(
        client, ["SOLUSDT"], cfg, start_ms, end_ms, None, 1
    )
    assert sorted({call[1] for call in client.calls}) == ["1d", "1h"]
    bars = []
    t = start_ms - warmup_ms
    while t < end_ms:
        close = 100.0 + (t // M5_MS) * 0.001
        bars.append(
            strategy.Kline(
                t, close, close + 0.05, close - 0.05, close,
                t + M5_MS - 1, 1000.0, 100000.0,
            )
        )
        t += M5_MS
    with KlineStore.from_klines({"SOLUSDT": bars}) as store:
        portfolio = pbt.run_portfolio_backtest(
            store, cfg, "5m", warmup_ms=warmup_ms,
            prebuilt=(None, None, arah, zona, None),
        )
    assert portfolio.direction_scans > 0
    assert portfolio.daily_zone_scans == portfolio.direction_scans
    assert portfolio.daily_zone_skips == 0
    assert portfolio.zone_scans == portfolio.direction_scans
    assert not any("dinaikkan" in w for w in portfolio.warnings)


def test_jalur_native_tetap_ambil_candle_harian_kala_hanya_zona_daily_aktif():
    cfg = {**simple_backtest_config(), "DIRECTION_ZONE_FILTER_ENABLED": True}
    hari = 30
    end_ms = START + hari * DAY_MS
    start_ms = end_ms - hari * DAY_MS
    warmup_ms = DAY_MS + 30 * M5_MS
    client = FakeBinanceClient()
    arah, zona = pbt.fetch_native_lookups(
        client, ["SOLUSDT"], cfg, start_ms, end_ms, None, 1
    )
    assert set(arah) == {"SOLUSDT"}
    assert zona == {}
    assert sorted({call[1] for call in client.calls}) == ["1d"]
    assert strategy.native_history_ms(cfg) >= 21 * DAY_MS
    assert arah["SOLUSDT"].zone_at(end_ms - M5_MS)["ok"] is True
    bars = []
    t = start_ms - warmup_ms
    while t < end_ms:
        close = 100.0 + (t // M5_MS) * 0.001
        bars.append(
            strategy.Kline(
                t, close, close + 0.05, close - 0.05, close,
                t + M5_MS - 1, 1000.0, 100000.0,
            )
        )
        t += M5_MS
    with KlineStore.from_klines({"SOLUSDT": bars}) as store:
        cache = pbt.precompute_entry_signals(
            store, cfg, "5m", warmup_ms=warmup_ms,
            prebuilt=(None, None, arah, zona, None),
        )
        portfolio = pbt.run_portfolio_backtest(
            store, cfg, "5m", warmup_ms=warmup_ms,
            prebuilt=(None, None, arah, zona, None),
        )
    assert cache.daily_zone_scans > 0
    assert cache.daily_zone_skips == 0
    assert cache.direction_scans == 0
    assert portfolio.daily_zone_scans > 0
    assert portfolio.daily_zone_skips == 0
    assert portfolio.direction_scans == 0
    assert portfolio.daily_zone_scans > 0
    assert portfolio.daily_zone_skips == 0
    assert portfolio.trades
    assert not any(x.reason == "FILTER_DAILY_ZONE" for x in portfolio.skipped)
    assert not any("dinaikkan" in w for w in portfolio.warnings)


def test_relasi_kunci_zona_daily_divalidasi():
    longgar = {"DIRECTION_ZONE_REQUIRE_INSIDE_ZONE": False}
    keluar = schema.relation_violations(
        {**PUMP_CONFIG, **longgar, "DIRECTION_ZONE_MAX_DISTANCE_PCT": 1.0},
        mode_aware=True,
    )
    assert "DIRECTION_ZONE_MAX_DISTANCE_PCT" in keluar
    keluar = schema.relation_violations(
        {**PUMP_CONFIG, "DIRECTION_ZONE_MAX_DISTANCE_PCT": 1.0}, mode_aware=True
    )
    assert "DIRECTION_ZONE_MAX_DISTANCE_PCT" not in keluar
    keluar = schema.relation_violations(
        {**PUMP_CONFIG, "DIRECTION_LOOKBACK_BARS": 15}, mode_aware=True
    )
    assert "DIRECTION_LOOKBACK_BARS" in keluar


def test_mode_wajib_di_dalam_zona_dilaporkan_dan_diperingatkan():
    _, errors, warnings = schema.validate_candidate(dict(PUMP_CONFIG), "PAPER")
    assert not errors
    gabung = " ".join(warnings)
    assert "DEMAND_REQUIRE_INSIDE_ZONE aktif" in gabung
    assert "ZONE_REQUIRE_INSIDE_ZONE aktif" in gabung
    assert "DIRECTION_ZONE_REQUIRE_INSIDE_ZONE aktif" in gabung
    longgar = {
        **PUMP_CONFIG,
        "ZONE_REQUIRE_INSIDE_ZONE": False,
        "DEMAND_REQUIRE_INSIDE_ZONE": False,
        "DIRECTION_ZONE_REQUIRE_INSIDE_ZONE": False,
    }
    _, _, warnings_longgar = schema.validate_candidate(dict(longgar), "PAPER")
    assert not any("aktif: close wajib berada" in w for w in warnings_longgar)
    assert bt.validate_params(PUMP_CONFIG) is None
