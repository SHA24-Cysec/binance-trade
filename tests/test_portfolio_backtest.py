from __future__ import annotations

import os
import re

import pytest

from backtesting import backtest as bt
from backtesting import backtest_storage as storage
from backtesting import portfolio_backtest as pbt
from backtesting.backtest_storage import KlineStore
from backtesting.synthetic_data import riwayat_harian, seri_data
from market import market_scanner as scanner
from strategy.indicators import Kline


def config_uji(**override) -> dict:
    cfg = {
        "QUOTE_ASSET": "USDT",
        "EXTRA_EXCLUDE_SYMBOLS": [],
        "MIN_QUOTE_VOLUME_USDT_24H": 0,
        "BACKTEST_INITIAL_EQUITY_USDT": 10_000.0,
    }
    cfg.update(override)
    return cfg


def data_dua_simbol() -> dict:
    return {
        "AUSDT": seri_data(harga=100.0, bars=400, volume=9_000_000.0),
        "BUSDT": seri_data(harga=200.0, bars=400, volume=5_000_000.0),
    }


def harian_dari(data: dict) -> dict:
    return {sym: riwayat_harian(kl) for sym, kl in data.items()}


def baris_mentah(candle: Kline) -> list:
    return [candle.open_time, f"{candle.open}", f"{candle.high}", f"{candle.low}",
            f"{candle.close}", f"{candle.volume}", candle.close_time,
            f"{candle.quote_volume}", 10, "0", "0", "0"]


class KlienPalsu:
    def __init__(self, intraday: dict, harian: dict | None = None,
                 error_simbol: dict | None = None) -> None:
        self.intraday = intraday
        self.harian = harian or {}
        self.error_simbol = error_simbol or {}
        self.panggilan: list = []

    def get_klines(self, symbol, interval="5m", limit=1000,
                   start_time_ms=None, end_time_ms=None):
        self.panggilan.append((symbol, interval))
        if symbol in self.error_simbol:
            raise RuntimeError(self.error_simbol[symbol])
        sumber = self.harian if interval == "1d" else self.intraday
        return [baris_mentah(k) for k in sumber.get(symbol, [])]


def test_portfolio_selalu_tanpa_trade():
    data = data_dua_simbol()
    with KlineStore.from_klines(data, harian_dari(data)) as store:
        result = pbt.run_portfolio_backtest(store, config_uji(), "5m")
    assert result.trades == []
    assert result.final_equity == pytest.approx(10_000.0)
    assert any("tidak membuka posisi" in warning for warning in result.warnings)
    summary = pbt.summarize_portfolio(result)
    assert summary["total_trades"] == 0
    assert summary["equity_curve"] == [0.0]


def test_backtest_satu_simbol_selalu_tanpa_trade():
    candles = seri_data(bars=400)
    result = bt.run_backtest(candles, config_uji(_symbol="TESTUSDT"), warmup_bars=0)
    assert result.trades == []
    assert result.final_equity == pytest.approx(10_000.0)
    assert any("tidak membuat trade baru" in warning for warning in result.warnings)
    assert bt.summarize(result)["total_trades"] == 0


def test_timeline_dan_statistik_candle_tetap_tersedia():
    data = data_dua_simbol()
    window = bt.bars_per_day("5m")
    with KlineStore.from_klines(data) as store:
        timeline, series_of = pbt.build_timeline(store, "5m")
        assert timeline == sorted({k.open_time for candles in data.values() for k in candles})
        for symbol, candles in data.items():
            stats = bt.compute_rolling_24h_stats(candles, window)
            series = series_of[symbol]
            assert len(series) == len(candles)
            assert series.ready(window - 1) is True
            assert series.pct24h(window - 1) == stats[window - 1]["pct24h"]


def test_select_universe_hanya_filter_monitoring():
    tickers = [
        {"symbol": "BTCUSDT", "priceChangePercent": "1", "quoteVolume": "9e9", "lastPrice": "60000"},
        {"symbol": "USDCUSDT", "priceChangePercent": "1", "quoteVolume": "9e9", "lastPrice": "1"},
        {"symbol": "BTCUPUSDT", "priceChangePercent": "1", "quoteVolume": "9e9", "lastPrice": "10"},
        {"symbol": "ETHBTC", "priceChangePercent": "1", "quoteVolume": "9e9", "lastPrice": "0.05"},
    ]
    assert pbt.select_universe(tickers, config_uji()) == ["BTCUSDT"]


def test_fetch_universe_klines_mencatat_simbol_gagal():
    data = {"AUSDT": seri_data(bars=10), "BUSDT": seri_data(bars=10)}
    client = KlienPalsu(data, error_simbol={"BUSDT": "timeout"})
    with KlineStore.create_temp() as store:
        ok, failed = pbt.fetch_universe_klines(
            client, list(data), "5m", 0, 10_000_000, store, sleep_between_symbols=0
        )
        assert ok == ["AUSDT"]
        assert failed and failed[0]["symbol"] == "BUSDT"
        assert store.symbols() == ["AUSDT"]


def test_candle_harian_dapat_diisi_dari_sumber_palsu():
    data = {"AUSDT": seri_data(bars=10)}
    daily = {"AUSDT": riwayat_harian(data["AUSDT"])}
    client = KlienPalsu(data, daily)
    with KlineStore.create_temp() as store:
        ok = pbt.fetch_universe_daily_klines(
            client, ["AUSDT"], -1_000_000_000, 10_000_000, store,
        )
        assert ok == ["AUSDT"]
        assert len(store.daily_klines("AUSDT")) == 7


def test_store_temporary_dihapus():
    store = KlineStore.create_temp()
    path = store.db_path
    directory = os.path.dirname(path)
    store.cleanup()
    store.cleanup()
    assert not os.path.exists(path)
    assert not os.path.exists(directory)


def test_store_paralel_memakai_file_berbeda():
    satu = KlineStore.create_temp()
    dua = KlineStore.create_temp()
    try:
        assert satu.db_path != dua.db_path
    finally:
        satu.cleanup()
        dua.cleanup()


def test_cache_symbol_dibatasi():
    data = {f"S{i}USDT": seri_data(harga=100 + i, bars=12) for i in range(5)}
    with KlineStore.from_klines(data) as store:
        for symbol in data:
            store.klines(symbol)
        assert len(store.cached_symbols()) <= store.cache_size


def test_tidak_ada_sql_dari_f_string():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for module in ("backtest_storage.py", "portfolio_backtest.py"):
        with open(os.path.join(root, "backtesting", module), encoding="utf-8") as handle:
            content = handle.read()
        assert not re.search(r"execute(?:script|many)?\s*\(\s*f[\"']", content)
        assert not re.search(r"execute(?:script|many)?\s*\([^)]*\.format\(", content)


def test_path_store_dari_tempfile():
    import tempfile
    store = KlineStore.create_temp()
    try:
        assert store.db_path.startswith(tempfile.gettempdir())
    finally:
        store.cleanup()
