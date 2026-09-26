"""
Tes backtest PORTOFOLIO setelah candle dipindah ke SQLite temporary.
====================================================================

Sebelum migrasi ini portfolio_backtest.py sama sekali belum punya tes di
tests/ (hanya selftest internal), jadi berkas ini sekaligus menutup lubang
cakupan tersebut. Semua tes berjalan TANPA jaringan: klien Binance diganti
objek palsu dan candle-nya sintetis, sehingga hasilnya deterministik.

Yang diuji:
  1. Hasil simulasi versi SQLite IDENTIK dengan hasil implementasi dict
     pra-migrasi (nilai acuan di GOLDEN_TRADES, lihat catatannya).
  2. File SQLite temporary benar-benar terhapus: sukses, error, dan batal.
  3. Daftar simbol gagal unduh tetap benar dan tidak menggagalkan job.
  4. Tidak ada lagi seluruh semesta list[Kline] hidup bersamaan di RAM.
  5. Tidak ada SQL yang dirakit dari f-string/format/concatenation.
"""

from __future__ import annotations

import os
import re
import threading

import pytest

import backtest as bt
import backtest_storage as storage
import market_scanner as scanner
import portfolio_backtest as pbt
from backtest_storage import KlineStore
from config import PUMP_CONFIG
from strategy import Kline
from synthetic_data import riwayat_harian, seri_banyak_setup


# ======================================================================
# Perkakas bersama
# ======================================================================

def config_uji(**override) -> dict:
    """Config simulasi yang melonggarkan gerbang pump, seperti selftest modul."""
    cfg = {
        "CONFIRM_LOOKBACK_BARS": 48,
        "MIN_CLOSE_POSITION_IN_RANGE": 0.0,
        "MIN_QUOTE_VOLUME_USDT_24H": 0,
        "TOP_N_CANDIDATES_TO_CONFIRM": 10,
        "COOLDOWN_MINUTES_AFTER_CLOSE": 0,
        "USE_STOP_LOSS": True, "USE_TP": True,
        "USE_BREAKEVEN": False, "USE_TRAILING": False,
        "SL_PCT": 2.0, "TP_PCT": 3.0,
        "BE_TRIGGER_PCT": 1.0, "BE_LOCK_PCT": 0.1,
        "TRAILING_START_PCT": 1.5, "TRAILING_STEP_PCT": 0.6,
        "TAKER_FEE_PCT": 0.1, "QUOTE_ASSET": "USDT",
        "PUMP_MIN_24H_CHANGE_PCT": -1000.0, "PUMP_VOLUME_SURGE_MULT": 0.0,
        "ROLLING_VOLUME_FILTER_ENABLED": False,
    }
    for kunci in ("SWING_LOOKBACK_BARS", "SWING_PIVOT_WING_BARS",
                  "VWAP_MIN_BARS_AFTER_ANCHOR", "MAX_BARS_BREAKOUT_TO_RETEST",
                  "MAX_RETEST_TOUCHES"):
        cfg[kunci] = PUMP_CONFIG[kunci]
    cfg.update(override)
    return cfg


def data_dua_simbol() -> dict:
    """Dua simbol dengan sepuluh siklus setup, sama dengan selftest modul."""
    return {
        "AUSDT": seri_banyak_setup(harga=100.0, siklus=10, volume=9_000_000.0),
        "BUSDT": seri_banyak_setup(harga=200.0, siklus=10, volume=5_000_000.0),
    }


def harian_dari(data: dict) -> dict:
    return {sym: riwayat_harian(kl) for sym, kl in data.items()}


def baris_mentah(candle: Kline) -> list:
    """Bentuk baris klines Binance (12 field, indeks sesuai dokumentasi)."""
    return [candle.open_time, f"{candle.open}", f"{candle.high}", f"{candle.low}",
            f"{candle.close}", f"{candle.volume}", candle.close_time,
            f"{candle.quote_volume}", 10, "0", "0", "0"]


class KlienPalsu:
    """Klien Binance palsu untuk tes unduh (tanpa jaringan sama sekali)."""

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


# ======================================================================
# 1. Paritas hasil dengan implementasi dict pra-migrasi
# ======================================================================

# Nilai acuan diambil dari implementasi LAMA (dict[str, list[Kline]] penuh di
# RAM, commit sebelum migrasi SQLite) yang dijalankan pada data dan config
# yang sama persis dengan test_hasil_identik_dengan_versi_pra_migrasi, pada
# 2026-09-26. Kalau tes ini merah, artinya migrasi penyimpanan mengubah
# hasil simulasi -- itu bug, bukan tes yang perlu diperbarui.
GOLDEN_TRADES = [
    ("BUSDT", 104699999, 202.0493202821, 105599999, 207.9026890906, "TAKE_PROFIT"),
    ("BUSDT", 117599999, 197.9193682265, 118499999, 203.6530923240, "TAKE_PROFIT"),
    ("BUSDT", 130499999, 193.8738336980, 131399999, 199.4903586602, "TAKE_PROFIT"),
    ("BUSDT", 143399999, 189.9109911757, 144299999, 195.4127125901, "TAKE_PROFIT"),
    ("BUSDT", 156299999, 186.0291504088, 157199999, 191.4184148962, "TAKE_PROFIT"),
    ("BUSDT", 169199999, 182.2266556958, 170099999, 187.5057619113, "TAKE_PROFIT"),
    ("BUSDT", 182099999, 178.5018851783, 182999999, 183.6730847919, "TAKE_PROFIT"),
    ("BUSDT", 194999999, 174.8532501490, 195899999, 179.9187488058, "TAKE_PROFIT"),
    ("BUSDT", 207899999, 171.2791943745, 208799999, 176.2411526355, "TAKE_PROFIT"),
]
GOLDEN_FINAL_EQUITY = 10001.213650000003
GOLDEN_BARS = 718


def test_hasil_identik_dengan_versi_pra_migrasi():
    data = data_dua_simbol()
    with KlineStore.from_klines(data, harian_dari(data)) as store:
        hasil = pbt.run_portfolio_backtest(store, config_uji(), "5m")

    ringkas = [(t.symbol, t.entry_time, round(t.entry_price, 10), t.exit_time,
                round(t.exit_price, 10), t.reason) for t in hasil.trades]
    assert ringkas == GOLDEN_TRADES
    assert hasil.bars_total == GOLDEN_BARS
    assert hasil.final_equity == pytest.approx(GOLDEN_FINAL_EQUITY, rel=0, abs=1e-9)
    # Urutan dan isi sinyal yang terlewat juga bagian dari kontrak hasil.
    assert [(s.symbol, s.reason, s.holding) for s in hasil.skipped] == [
        ("AUSDT", "KALAH_KUALITAS_SETUP", "BUSDT")] * 9


def test_pnl_tiap_trade_konsisten_dengan_rumus_fee():
    """PnL bersih = PnL kotor dikurangi fee taker pulang-pergi."""
    data = data_dua_simbol()
    with KlineStore.from_klines(data, harian_dari(data)) as store:
        hasil = pbt.run_portfolio_backtest(store, config_uji(), "5m")
    assert hasil.trades
    for t in hasil.trades:
        kotor = (t.exit_price / t.entry_price - 1.0) * 100.0
        assert t.gross_pnl_pct == pytest.approx(kotor, abs=1e-9)
        assert t.pnl_pct == pytest.approx(kotor - t.fee_pct, abs=1e-9)


def test_urutan_simbol_mengikuti_urutan_penulisan():
    """Papan kandidat mengandalkan urutan insertion, jadi store harus menjaganya."""
    data = data_dua_simbol()
    with KlineStore.from_klines({"BUSDT": data["BUSDT"], "AUSDT": data["AUSDT"]}) as store:
        assert store.symbols() == ["BUSDT", "AUSDT"]


def test_build_timeline_memakai_statistik_yang_sama_dengan_backtest_satu_simbol():
    data = data_dua_simbol()
    window = bt.bars_per_day("5m")
    with KlineStore.from_klines(data) as store:
        timeline, series_of = pbt.build_timeline(store, "5m")
        assert timeline == sorted({k.open_time for kl in data.values() for k in kl})
        for sym, klines in data.items():
            acuan = bt.compute_rolling_24h_stats(klines, window)
            seri = series_of[sym]
            assert len(seri) == len(klines)
            for i, st in enumerate(acuan):
                assert seri.index_at(klines[i].open_time) == i
                assert seri.ready(i) is (st is not None)
                if st is not None:
                    assert seri.pct24h(i) == st["pct24h"]
                    assert seri.vol24h(i) == st["vol24h"]


# ======================================================================
# 2. Pembersihan file temporary
# ======================================================================

def test_file_temporary_dihapus_setelah_job_selesai():
    data = data_dua_simbol()
    store = KlineStore.from_klines(data, harian_dari(data))
    path = store.db_path
    try:
        pbt.run_portfolio_backtest(store, config_uji(), "5m")
    finally:
        store.cleanup()
    assert not os.path.exists(path)
    assert not os.path.exists(os.path.dirname(path))


def test_file_temporary_dihapus_saat_job_gagal():
    store = KlineStore.create_temp()
    path = store.db_path
    with pytest.raises(bt.BacktestError):
        try:
            # Store kosong -> BacktestError, meniru jalur error dashboard.
            pbt.run_portfolio_backtest(store, config_uji(), "5m")
        finally:
            store.cleanup()
    assert not os.path.exists(path)


def test_file_temporary_dihapus_saat_job_dibatalkan():
    data = data_dua_simbol()
    store = KlineStore.from_klines(data, harian_dari(data))
    path = store.db_path
    with pytest.raises(bt.BacktestError, match="dibatalkan"):
        try:
            pbt.run_portfolio_backtest(store, config_uji(), "5m",
                                       cancel_cb=lambda: True)
        finally:
            store.cleanup()
    assert not os.path.exists(path)
    assert not os.path.exists(os.path.dirname(path))


def test_cleanup_boleh_dipanggil_berkali_kali():
    store = KlineStore.create_temp()
    store.cleanup()
    store.cleanup()
    assert store.closed


def test_dua_job_paralel_memakai_file_berbeda():
    """Dua pengguna menekan tombol backtest bersamaan tidak boleh saling timpa."""
    satu = KlineStore.create_temp()
    dua = KlineStore.create_temp()
    try:
        assert satu.db_path != dua.db_path
        assert os.path.dirname(satu.db_path) != os.path.dirname(dua.db_path)
        satu.write_symbol("AUSDT", data_dua_simbol()["AUSDT"])
        assert dua.symbols() == []
    finally:
        satu.cleanup()
        dua.cleanup()


# ======================================================================
# 3. Tahap unduh: daftar gagal, progress, dan pembatalan
# ======================================================================

def test_simbol_gagal_diunduh_masuk_daftar_gagal_tanpa_menggagalkan_job():
    data = data_dua_simbol()
    klien = KlienPalsu(intraday={"AUSDT": data["AUSDT"], "KOSONGUSDT": []},
                       error_simbol={"RUSAKUSDT": "koneksi putus"})
    with KlineStore.create_temp() as store:
        berhasil, gagal = pbt.fetch_universe_klines(
            klien, ["AUSDT", "RUSAKUSDT", "KOSONGUSDT"], "5m", 0, 10 ** 13, store)
        assert berhasil == ["AUSDT"]
        assert [g["symbol"] for g in gagal] == ["RUSAKUSDT", "KOSONGUSDT"]
        assert "koneksi putus" in gagal[0]["error"]
        assert "tidak ada data candle" in gagal[1]["error"]
        assert store.symbols() == ["AUSDT"]
        assert store.bar_count("AUSDT") == len(data["AUSDT"])


def test_progress_dan_cancel_tetap_bekerja_saat_unduh():
    data = data_dua_simbol()
    klien = KlienPalsu(intraday=data)
    kemajuan: list = []
    with KlineStore.create_temp() as store:
        pbt.fetch_universe_klines(klien, ["AUSDT", "BUSDT"], "5m", 0, 10 ** 13,
                                  store, progress_cb=lambda f, s: kemajuan.append((f, s)))
        assert kemajuan == [(0.5, "AUSDT"), (1.0, "BUSDT")]

    with KlineStore.create_temp() as store:
        with pytest.raises(bt.BacktestError, match="dibatalkan"):
            pbt.fetch_universe_klines(klien, ["AUSDT"], "5m", 0, 10 ** 13, store,
                                      cancel_cb=lambda: True)
        assert store.symbols() == []


def test_progress_simulasi_tetap_dipanggil_sampai_selesai():
    data = data_dua_simbol()
    kemajuan: list = []
    with KlineStore.from_klines(data, harian_dari(data)) as store:
        pbt.run_portfolio_backtest(store, config_uji(), "5m",
                                   progress_cb=kemajuan.append)
    assert kemajuan and kemajuan[0] == 0.0 and kemajuan[-1] == 1.0
    assert kemajuan == sorted(kemajuan)


def test_candle_harian_gagal_ditambal_agregasi_intraday():
    """Gerbang pump tetap punya deret harian walau unduhan 1d gagal."""
    data = data_dua_simbol()
    klien = KlienPalsu(intraday=data, harian={},
                       error_simbol={"BUSDT": "429 rate limit"})
    with KlineStore.create_temp() as store:
        berhasil, _gagal = pbt.fetch_universe_klines(
            klien, ["AUSDT", "BUSDT"], "5m", 0, 10 ** 13, store)
        # AUSDT: dijawab daftar kosong (tidak ada candle 1d), BUSDT: error.
        tersimpan = pbt.fetch_universe_daily_klines(
            klien, berhasil, 0, 10 ** 13, store)
        assert tersimpan == []
        pbt.ensure_daily_series(store, berhasil)
        for sym in berhasil:
            acuan = scanner.aggregate_to_daily(data[sym])
            assert store.daily_klines(sym) == acuan


def test_candle_harian_asli_tidak_ditimpa_agregasi():
    data = data_dua_simbol()
    harian = harian_dari(data)
    with KlineStore.from_klines(data, harian) as store:
        pbt.ensure_daily_series(store, list(data))
        assert store.daily_klines("AUSDT") == list(harian["AUSDT"])


# ======================================================================
# 4. Bukti hemat RAM
# ======================================================================

def test_hanya_sedikit_simbol_yang_utuh_di_ram():
    """Inti migrasi: TIDAK semua simbol boleh utuh di RAM bersamaan."""
    data = {f"S{i}USDT": seri_banyak_setup(harga=100.0 + i, siklus=2,
                                           volume=1_000_000.0 * (i + 1))
            for i in range(12)}
    with KlineStore.from_klines(data, harian_dari(data)) as store:
        store.set_cache_size(3)
        for sym in data:
            store.klines(sym)
            assert len(store.cached_symbols()) <= 3
        assert store.cached_symbols() == ["S9USDT", "S10USDT", "S11USDT"]


def test_deret_ringkas_jauh_lebih_kecil_daripada_list_kline():
    data = data_dua_simbol()
    with KlineStore.from_klines(data) as store:
        _timeline, series_of = pbt.build_timeline(store, "5m")
        seri = series_of["AUSDT"]
        per_bar = seri.memory_bytes() / max(1, len(seri))
        # 4 array 8 byte + 1 byte penanda = 41 byte per bar, jauh di bawah
        # satu objek Kline (~290 byte) plus dict index dan dict statistik.
        assert per_bar < 64


def test_cache_dibatasi_saat_simulasi_berjalan():
    data = {f"S{i}USDT": seri_banyak_setup(harga=100.0 + i, siklus=6,
                                           volume=1_000_000.0 * (i + 1))
            for i in range(8)}
    with KlineStore.from_klines(data, harian_dari(data)) as store:
        cfg = config_uji(TOP_N_CANDIDATES_TO_CONFIRM=3,
                         BACKTEST_SYMBOL_CACHE_SIZE=4)
        pbt.run_portfolio_backtest(store, cfg, "5m")
        # Lantai cache = 2 x top_n + 4 = 10, tetapi hanya 8 simbol yang ada.
        assert len(store.cached_symbols()) <= 10
        assert store.cache_size == pbt._resolve_symbol_cache_size(cfg, 3) == 10


# ======================================================================
# 5. Keamanan dan thread-safety
# ======================================================================

def test_tidak_ada_sql_yang_dirakit_dari_nilai_variabel():
    """Semua nilai harus lewat placeholder '?', tidak ada SQL dari f-string."""
    akar = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for modul in ("backtest_storage.py", "portfolio_backtest.py"):
        with open(os.path.join(akar, modul), encoding="utf-8") as handle:
            isi = handle.read()
        assert not re.search(r"execute(?:script|many)?\s*\(\s*f[\"']", isi), modul
        assert not re.search(r"execute(?:script|many)?\s*\([^)]*\.format\(", isi), modul
        assert not re.search(r"execute(?:script|many)?\s*\([^)]*%\s*\(", isi), modul
        # Tidak boleh ada f-string yang memuat kata kunci SQL sama sekali.
        for baris in isi.splitlines():
            tanpa_komentar = baris.split("#", 1)[0]
            if re.search(r"f[\"'][^\"']*\b(SELECT|INSERT|UPDATE|DELETE)\b", tanpa_komentar):
                raise AssertionError(f"SQL dari f-string di {modul}: {baris.strip()}")


def test_path_store_selalu_dari_tempfile_bukan_hardcode():
    import tempfile
    store = KlineStore.create_temp()
    try:
        assert store.db_path.startswith(tempfile.gettempdir())
        assert "binance_backtest_" in store.db_path
    finally:
        store.cleanup()


def test_store_aman_dipakai_dari_thread_lain():
    """_bt_run_job berjalan di thread terpisah dari thread HTTP dashboard."""
    data = data_dua_simbol()
    store = KlineStore.from_klines(data, harian_dari(data))
    hasil: dict = {}

    def kerja():
        try:
            res = pbt.run_portfolio_backtest(store, config_uji(), "5m")
            hasil["trades"] = len(res.trades)
        except Exception as exc:  # noqa: BLE001 - dilaporkan ke assertion
            hasil["error"] = repr(exc)

    thread = threading.Thread(target=kerja)
    thread.start()
    thread.join(timeout=120)
    # Pembersihan dilakukan dari thread utama, bukan thread job.
    path = store.db_path
    store.cleanup()
    assert "error" not in hasil, hasil.get("error")
    assert hasil["trades"] == len(GOLDEN_TRADES)
    assert not os.path.exists(path)


def test_akses_setelah_cleanup_ditolak_dengan_jelas():
    store = KlineStore.create_temp()
    store.cleanup()
    with pytest.raises(storage.StorageError):
        store.symbols()


# ==== RINGKASAN AUDIT (tests/test_portfolio_backtest.py) ==============
# Lingkup: berkas tes BARU. Sebelumnya portfolio_backtest.py sama sekali
#   tidak punya tes di tests/, jadi ini menambah cakupan yang kosong.
# Cakupan: paritas hasil dengan implementasi dict pra-migrasi (GOLDEN_TRADES),
#   konsistensi rumus fee, urutan simbol, kesamaan statistik dengan
#   backtest.compute_rolling_24h_stats, pembersihan file temporary pada jalur
#   sukses/error/batal, isolasi dua job paralel, daftar simbol gagal unduh,
#   progress_cb & cancel_cb di tahap unduh maupun simulasi, penambalan candle
#   harian, batas cache LRU, ukuran deret ringkas, larangan SQL dari f-string,
#   path tempfile, dan pemakaian store dari thread lain.
# Sintaks/tipe: tanpa jaringan sama sekali; klien Binance dipalsukan;
#   deterministik (data sintetis dari synthetic_data.py, tanpa random seed
#   tersembunyi). Tidak ada print()/TODO.
# Keamanan: test_tidak_ada_sql_yang_dirakit_dari_nilai_variabel membaca
#   sumber backtest_storage.py dan portfolio_backtest.py lalu menolak f-string
#   atau .format() yang memuat kata kunci SQL. Ini penjaga otomatis, bukan
#   sekadar klaim di komentar.
# Race condition: test_store_aman_dipakai_dari_thread_lain menjalankan
#   simulasi di thread terpisah lalu membersihkan store dari thread utama,
#   meniru pola _bt_run_job di dashboard.
# Kebersihan: setiap tes memakai context manager atau try/finally sehingga
#   tidak meninggalkan direktori temporary walau assertion gagal.
# =======================================================================
