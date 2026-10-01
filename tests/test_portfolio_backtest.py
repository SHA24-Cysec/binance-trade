from __future__ import annotations

import os
import re

import pytest


from backtesting import backtest as bt
from backtesting import portfolio_backtest as pbt
from backtesting.backtest_storage import KlineStore
from backtesting.synthetic_data import riwayat_harian, seri_banyak_setup, seri_data
from strategy.indicators import Kline


def config_uji(**override) -> dict:
    from config.config import PUMP_CONFIG
    cfg = dict(PUMP_CONFIG)
    cfg.update({
        "QUOTE_ASSET": "USDT",
        "EXTRA_EXCLUDE_SYMBOLS": [],
        "MIN_QUOTE_VOLUME_USDT_24H": 0,
        "BACKTEST_INITIAL_EQUITY_USDT": 10_000.0,
    })
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


def test_portfolio_memuat_bar_dan_tidak_lagi_stub():
    data = data_dua_simbol()
    with KlineStore.from_klines(data, harian_dari(data)) as store:
        result = pbt.run_portfolio_backtest(store, config_uji(), "5m")
    assert result.bars_total > 0, "timeline tidak diproses sama sekali"
    assert result.symbols_with_data == 2
    assert not any("tidak membuka posisi" in w for w in result.warnings)
    summary = pbt.summarize_portfolio(result)
    assert summary["total_trades"] == 0
    assert summary["equity_curve"] == [0.0]


def test_backtest_satu_simbol_benar_benar_mensimulasikan():
    from backtesting.synthetic_data import (
        cfg_gerbang_pump_nonaktif, riwayat_harian, seri_banyak_setup,
    )
    candles = seri_banyak_setup(harga=100.0, siklus=8, bar_datar=288)
    cfg = cfg_gerbang_pump_nonaktif(config_uji(_symbol="TESTUSDT"))
    cfg["ROLLING_VOLUME_FILTER_ENABLED"] = False
    result = bt.run_backtest(candles, cfg, warmup_bars=288,
                             daily_klines=riwayat_harian(candles))
    assert result.trades, "backtest tidak menghasilkan satu trade pun"
    ringkas = bt.summarize(result)
    assert ringkas["total_trades"] == len(result.trades)
    assert not any("tidak membuat trade baru" in w for w in result.warnings)


def test_hasil_backtest_berubah_saat_parameter_berubah():
    from backtesting.synthetic_data import (
        cfg_gerbang_pump_nonaktif, riwayat_harian, seri_banyak_setup,
    )
    candles = seri_banyak_setup(harga=100.0, siklus=8, bar_datar=288)
    daily = riwayat_harian(candles)

    hasil = set()
    for sl, tp in [(1.0, 2.0), (3.0, 6.0), (5.0, 10.0)]:
        cfg = cfg_gerbang_pump_nonaktif(config_uji(_symbol="TESTUSDT"))
        cfg["ROLLING_VOLUME_FILTER_ENABLED"] = False
        cfg["USE_ATR_EXIT"] = False
        cfg["SL_PCT"] = sl
        cfg["TP_PCT"] = tp
        ringkas = bt.summarize(
            bt.run_backtest(candles, cfg, warmup_bars=288, daily_klines=daily))
        hasil.add((ringkas["total_trades"], round(ringkas["total_return_pct"], 6)))

    assert len(hasil) > 1, f"semua parameter memberi hasil identik: {hasil}"


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


# ---------------------------------------------------------------------------
# Batas jendela waktu (start_ms/end_ms): fondasi split latih/uji grid search
# ---------------------------------------------------------------------------

def _cfg_portfolio_uji() -> dict:
    from backtesting.synthetic_data import cfg_gerbang_pump_nonaktif
    cfg = cfg_gerbang_pump_nonaktif(config_uji())
    cfg["ROLLING_VOLUME_FILTER_ENABLED"] = False
    cfg["USE_ATR_EXIT"] = False
    return cfg


def _data_portfolio_banyak_setup() -> dict:
    return {
        "AUSDT": seri_banyak_setup(harga=100.0, siklus=12, bar_datar=288),
        "BUSDT": seri_banyak_setup(harga=200.0, siklus=12, bar_datar=288),
    }


def test_jendela_waktu_membatasi_entry_tanpa_menghapus_warmup():
    data = _data_portfolio_banyak_setup()
    harian = harian_dari(data)
    cfg = _cfg_portfolio_uji()
    interval_ms = 300_000
    warmup_ms = 288 * interval_ms

    with KlineStore.from_klines(data, harian) as store:
        penuh = pbt.run_portfolio_backtest(store, cfg, "5m", warmup_ms=warmup_ms)
        assert penuh.trades, "data uji seharusnya menghasilkan trade pada jalan penuh"

        timeline, _ = pbt.build_timeline(store, "5m")
        potong_ms = timeline[0] + warmup_ms + int(
            (timeline[-1] - timeline[0] - warmup_ms) * 0.7)

        latih = pbt.run_portfolio_backtest(
            store, cfg, "5m", warmup_ms=warmup_ms, end_ms=potong_ms)
        uji = pbt.run_portfolio_backtest(
            store, cfg, "5m", warmup_ms=warmup_ms, start_ms=potong_ms - warmup_ms)

        assert latih.trades, "periode latih seharusnya menghasilkan trade"
        assert all(t.entry_time <= potong_ms for t in latih.trades), \
            "entry latih tidak boleh melewati titik potong"
        assert all(t.entry_time >= potong_ms for t in uji.trades), \
            "entry uji tidak boleh menyusup sebelum titik potong (bocor data latih)"
        assert all(t.entry_time >= timeline[0] + warmup_ms for t in penuh.trades), \
            "jalan penuh juga harus menghormati warmup"


def test_jendela_waktu_kosong_ditolak_dengan_jelas():
    data = _data_portfolio_banyak_setup()
    harian = harian_dari(data)
    cfg = _cfg_portfolio_uji()
    with KlineStore.from_klines(data, harian) as store:
        timeline, _ = pbt.build_timeline(store, "5m")
        with pytest.raises(pbt.BacktestError, match="tidak memuat satu bar pun"):
            pbt.run_portfolio_backtest(
                store, cfg, "5m",
                start_ms=timeline[-1] + 10 * 86_400_000,
                end_ms=timeline[-1] + 20 * 86_400_000)


def test_prebuilt_menghasilkan_hasil_identik_dan_tidak_dimutasi():
    data = _data_portfolio_banyak_setup()
    harian = harian_dari(data)
    cfg = _cfg_portfolio_uji()
    with KlineStore.from_klines(data, harian) as store:
        prebuilt = pbt.build_timeline(store, "5m")
        pertama = pbt.run_portfolio_backtest(
            store, cfg, "5m", warmup_ms=288 * 300_000, prebuilt=prebuilt)
        kedua = pbt.run_portfolio_backtest(
            store, cfg, "5m", warmup_ms=288 * 300_000, prebuilt=prebuilt)
        assert len(pertama.trades) == len(kedua.trades), \
            "prebuilt harus aman dipakai berulang antar kombinasi grid"
        if pertama.trades:
            assert [t.entry_time for t in pertama.trades] == \
                   [t.entry_time for t in kedua.trades]
