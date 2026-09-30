"""Test untuk grid search parameter backtest.

Ditambahkan 1 Oktober 2026 bersama fitur grid search.

Fokus test ini bukan sekadar "fungsinya jalan", melainkan menjaga sifat sifat
yang membuat hasil optimasi bisa dipercaya:

  1. Periode uji benar benar terpisah dari periode latih.
  2. Peringkat disusun dari skor latih, bukan skor uji. Kalau skor uji ikut
     memilih, ia berhenti menjadi data yang belum pernah dilihat.
  3. Kombinasi mubazir dipangkas, bukan dijalankan berulang.
  4. Batas keras jumlah kombinasi ditegakkan.
  5. Sampel kecil ditandai tidak andal, bukan disodorkan sebagai pemenang.
"""

from __future__ import annotations

import pytest

from backtesting import grid_search as gs
from backtesting.synthetic_data import (
    cfg_gerbang_pump_nonaktif, riwayat_harian, seri_banyak_setup,
)


def test_rentang_inklusif_dan_bebas_galat_float():
    assert gs.buat_rentang(1.0, 3.0, 0.5) == [1.0, 1.5, 2.0, 2.5, 3.0]
    assert gs.buat_rentang(0.1, 0.5, 0.1) == [0.1, 0.2, 0.3, 0.4, 0.5]
    assert gs.buat_rentang(2.0, 2.0, 0.5) == [2.0]


def test_rentang_menolak_masukan_tidak_masuk_akal():
    with pytest.raises(gs.GridSearchError):
        gs.buat_rentang(1.0, 3.0, 0.0)
    with pytest.raises(gs.GridSearchError):
        gs.buat_rentang(1.0, 3.0, -0.5)
    with pytest.raises(gs.GridSearchError):
        gs.buat_rentang(3.0, 1.0, 0.5)


def test_kombinasi_mubazir_dipangkas():
    spec = {
        "USE_ATR_EXIT": [True],
        "ATR_MULT_SL": [1.5, 2.0],
        "SL_PCT": [1.0, 2.0, 3.0],
    }
    komb, dipangkas = gs.expand_grid(spec)
    assert len(komb) == 2, "SL_PCT seharusnya tidak melipatgandakan kombinasi"
    assert dipangkas == 4


def test_kombinasi_mubazir_dipangkas_arah_sebaliknya():
    spec = {
        "USE_ATR_EXIT": [False],
        "SL_PCT": [1.0, 2.0],
        "ATR_MULT_SL": [1.5, 2.0, 2.5],
    }
    komb, dipangkas = gs.expand_grid(spec)
    assert len(komb) == 2
    assert dipangkas == 4


def test_kedua_mode_tetap_diuji_terpisah():
    spec = {"USE_ATR_EXIT": [True, False], "SL_PCT": [1.0], "ATR_MULT_SL": [1.5]}
    komb, _ = gs.expand_grid(spec)
    mode = {k["USE_ATR_EXIT"] for k in komb}
    assert mode == {True, False}


def test_batas_keras_kombinasi_ditegakkan():
    with pytest.raises(gs.GridSearchError, match="melebihi batas|Terlalu besar"):
        gs.expand_grid({"SL_PCT": gs.buat_rentang(0.1, 60.0, 0.01)})


def test_grid_kosong_ditolak():
    with pytest.raises(gs.GridSearchError):
        gs.expand_grid({})
    with pytest.raises(gs.GridSearchError):
        gs.expand_grid({"SL_PCT": []})


def test_skor_menetralkan_nilai_tak_hingga():
    assert gs.hitung_skor({"profit_factor": float("inf")}, "profit_factor") == 1e9
    assert gs.hitung_skor({"profit_factor": float("nan")}, "profit_factor") == 0.0


def test_return_per_drawdown_tidak_meledak_saat_drawdown_nol():
    skor = gs.hitung_skor(
        {"total_return_pct": 5.0, "max_drawdown_pct": 0.0}, "return_per_drawdown")
    assert skor == 5.0, "drawdown nol harus diberi lantai, bukan pembagian nol"


def test_metrik_tidak_dikenal_ditolak():
    with pytest.raises(gs.GridSearchError):
        gs.hitung_skor({}, "sharpe_ratio_imajiner")


@pytest.fixture()
def data_uji():
    kl = seri_banyak_setup(harga=100.0, siklus=20, bar_datar=288)
    daily = riwayat_harian(kl, hari=7)
    from config.config import PUMP_CONFIG
    cfg = cfg_gerbang_pump_nonaktif(dict(PUMP_CONFIG))
    cfg["_symbol"] = "TESTUSDT"
    cfg["MIN_QUOTE_VOLUME_USDT_24H"] = 1_000_000
    cfg["ROLLING_VOLUME_FILTER_ENABLED"] = False
    return kl, daily, cfg


def test_grid_search_menghasilkan_peringkat(data_uji):
    kl, daily, cfg = data_uji
    hasil = gs.run_grid_search(
        kl, cfg, {"USE_ATR_EXIT": [False], "TP_PCT": [2.0, 4.0, 6.0]},
        warmup_bars=288, daily_klines=daily, min_trades=1)
    assert hasil.total_kombinasi == 3
    assert len(hasil.hasil) == 3
    assert hasil.bar_latih > 0 and hasil.bar_uji > 0


def test_periode_latih_dan_uji_benar_benar_terpisah(data_uji):
    kl, daily, cfg = data_uji
    hasil = gs.run_grid_search(
        kl, cfg, {"USE_ATR_EXIT": [False], "TP_PCT": [4.0]},
        warmup_bars=288, daily_klines=daily, rasio_latih=0.7, min_trades=1)
    total_bar_tradable = len(kl) - 288
    assert hasil.bar_latih < total_bar_tradable
    assert hasil.bar_uji < total_bar_tradable
    assert hasil.bar_latih > hasil.bar_uji


def test_peringkat_memakai_skor_latih_bukan_skor_uji(data_uji):
    kl, daily, cfg = data_uji
    hasil = gs.run_grid_search(
        kl, cfg, {"USE_ATR_EXIT": [False], "TP_PCT": [2.0, 3.0, 4.0, 5.0, 6.0]},
        warmup_bars=288, daily_klines=daily, min_trades=1)
    andal = [h for h in hasil.hasil if h.andal]
    skor = [h.skor_latih for h in andal]
    assert skor == sorted(skor, reverse=True), "urutan tidak menurun menurut skor latih"


def test_sampel_kecil_ditandai_tidak_andal(data_uji):
    kl, daily, cfg = data_uji
    hasil = gs.run_grid_search(
        kl, cfg, {"USE_ATR_EXIT": [False], "TP_PCT": [4.0]},
        warmup_bars=288, daily_klines=daily, min_trades=10_000)
    assert all(not h.andal for h in hasil.hasil)
    assert any("terlalu kecil" in w for w in hasil.peringatan)


def test_hasil_tidak_andal_selalu_di_bawah(data_uji):
    kl, daily, cfg = data_uji
    hasil = gs.run_grid_search(
        kl, cfg, {"USE_ATR_EXIT": [False], "TP_PCT": [2.0, 4.0, 80.0]},
        warmup_bars=288, daily_klines=daily, min_trades=5)
    andal = [i for i, h in enumerate(hasil.hasil) if h.andal]
    tidak = [i for i, h in enumerate(hasil.hasil) if not h.andal]
    if andal and tidak:
        assert max(andal) < min(tidak), "hasil tidak andal menyusup ke atas"


def test_degradasi_dihitung_dari_selisih_latih_dan_uji(data_uji):
    kl, daily, cfg = data_uji
    hasil = gs.run_grid_search(
        kl, cfg, {"USE_ATR_EXIT": [False], "TP_PCT": [4.0]},
        warmup_bars=288, daily_klines=daily, min_trades=1)
    h = hasil.hasil[0]
    assert h.skor_uji is not None
    assert h.degradasi == pytest.approx(h.skor_latih - h.skor_uji)


def test_mematikan_periode_uji_memunculkan_peringatan(data_uji):
    kl, daily, cfg = data_uji
    hasil = gs.run_grid_search(
        kl, cfg, {"USE_ATR_EXIT": [False], "TP_PCT": [4.0]},
        warmup_bars=288, daily_klines=daily, rasio_latih=1.0, min_trades=1)
    assert hasil.bar_uji == 0
    assert any("rentan overfitting" in w for w in hasil.peringatan)
    assert hasil.hasil[0].uji is None
    assert hasil.hasil[0].degradasi is None


def test_banyak_kombinasi_memunculkan_peringatan_pengujian_berganda(data_uji):
    kl, daily, cfg = data_uji
    hasil = gs.run_grid_search(
        kl, cfg,
        {"USE_ATR_EXIT": [False], "TP_PCT": gs.buat_rentang(2.0, 12.0, 0.1)},
        warmup_bars=288, daily_klines=daily, min_trades=1)
    assert hasil.total_kombinasi > 100
    assert any("kebetulan" in w for w in hasil.peringatan)


def test_kombinasi_tidak_valid_dilewati_bukan_menggagalkan(data_uji):
    kl, daily, cfg = data_uji
    hasil = gs.run_grid_search(
        kl, cfg, {"USE_ATR_EXIT": [False], "TP_PCT": [4.0], "SL_PCT": [2.0, 99999.0]},
        warmup_bars=288, daily_klines=daily, min_trades=1)
    assert hasil.dilewati >= 1
    assert len(hasil.hasil) >= 1


def test_pembatalan_dihormati(data_uji):
    kl, daily, cfg = data_uji
    panggilan = {"n": 0}

    def batal():
        panggilan["n"] += 1
        return panggilan["n"] > 2

    hasil = gs.run_grid_search(
        kl, cfg, {"USE_ATR_EXIT": [False], "TP_PCT": [2.0, 3.0, 4.0, 5.0, 6.0, 7.0]},
        warmup_bars=288, daily_klines=daily, min_trades=1, cancel_cb=batal)
    assert hasil.dibatalkan
    assert len(hasil.hasil) < 6


def test_progress_mencapai_seratus_persen(data_uji):
    kl, daily, cfg = data_uji
    jejak: list[float] = []
    gs.run_grid_search(
        kl, cfg, {"USE_ATR_EXIT": [False], "TP_PCT": [2.0, 4.0]},
        warmup_bars=288, daily_klines=daily, min_trades=1,
        progress_cb=jejak.append)
    assert jejak and jejak[-1] == 1.0
    assert all(0.0 <= p <= 1.0 for p in jejak)


def test_data_terlalu_pendek_ditolak(data_uji):
    kl, daily, cfg = data_uji
    with pytest.raises(gs.GridSearchError, match="terlalu pendek"):
        gs.run_grid_search(kl[:290], cfg, {"TP_PCT": [4.0]}, warmup_bars=288,
                           daily_klines=daily)


def test_rasio_latih_di_luar_rentang_ditolak(data_uji):
    kl, daily, cfg = data_uji
    for rasio in (0.0, 1.5, -1.0):
        with pytest.raises(gs.GridSearchError):
            gs.run_grid_search(kl, cfg, {"TP_PCT": [4.0]}, warmup_bars=288,
                               daily_klines=daily, rasio_latih=rasio)


def test_ringkas_untuk_tabel_membatasi_jumlah_baris(data_uji):
    kl, daily, cfg = data_uji
    hasil = gs.run_grid_search(
        kl, cfg, {"USE_ATR_EXIT": [False], "TP_PCT": [2.0, 3.0, 4.0, 5.0]},
        warmup_bars=288, daily_klines=daily, min_trades=1)
    baris = gs.ringkas_untuk_tabel(hasil, top_n=2)
    assert len(baris) == 2
    assert "degradasi" in baris[0]
    assert "params" in baris[0]


def test_candle_harian_periode_latih_tidak_bocor_dari_masa_depan(data_uji):
    kl, _, cfg = data_uji

    from strategy.indicators import Kline
    ms_hari = 86_400_000
    awal = kl[0].open_time - 7 * ms_hari
    akhir = kl[-1].close_time
    daily = []
    t = awal
    while t <= akhir:
        daily.append(Kline(open_time=t, open=1.0, high=1.0, low=1.0, close=1.0,
                           close_time=t + ms_hari - 1, volume=1_000_000.0,
                           quote_volume=1_000_000.0))
        t += ms_hari
    assert len(daily) > 8, "deret harian uji harus melampaui periode intraday"

    terpakai: list[tuple[int, list]] = []
    asli = gs.bt.run_backtest

    def rekam(klines, config, warmup_bars, daily_klines=None, **kw):
        terpakai.append((klines[-1].close_time, list(daily_klines or [])))
        return asli(klines, config, warmup_bars, daily_klines=daily_klines, **kw)

    gs.bt.run_backtest = rekam
    try:
        gs.run_grid_search(kl, cfg, {"USE_ATR_EXIT": [False], "TP_PCT": [4.0]},
                           warmup_bars=288, daily_klines=daily,
                           rasio_latih=0.7, min_trades=1)
    finally:
        gs.bt.run_backtest = asli

    assert len(terpakai) == 2, "harus ada satu panggilan latih dan satu uji"
    (batas_latih, harian_latih), (batas_uji, harian_uji) = terpakai

    assert all(d.close_time <= batas_latih for d in harian_latih), \
        "periode latih memakai candle harian yang belum tertutup"
    assert all(d.close_time <= batas_uji for d in harian_uji), \
        "periode uji memakai candle harian yang belum tertutup"

    assert batas_latih < batas_uji
    assert len(harian_latih) <= len(harian_uji)
    assert harian_uji[:len(harian_latih)] == harian_latih, \
        "riwayat harian periode latih bukan awalan dari periode uji"


def test_parse_spec_rentang_dan_daftar():
    spec = gs.parse_spec_cli("USE_ATR_EXIT=false,SL_PCT=1:3:1,TP_PCT=2|4|6")
    assert spec["USE_ATR_EXIT"] == [False]
    assert spec["SL_PCT"] == [1.0, 2.0, 3.0]
    assert spec["TP_PCT"] == [2.0, 4.0, 6.0]


def test_parse_spec_boolean_string_false_tidak_menjadi_true():
    assert gs.parse_spec_cli("USE_ATR_EXIT=false")["USE_ATR_EXIT"] == [False]
    assert gs.parse_spec_cli("USE_ATR_EXIT=0")["USE_ATR_EXIT"] == [False]
    assert gs.parse_spec_cli("USE_ATR_EXIT=true")["USE_ATR_EXIT"] == [True]


def test_parse_spec_atr_period_tetap_integer():
    spec = gs.parse_spec_cli("ATR_PERIOD=10:20:5")
    assert spec["ATR_PERIOD"] == [10, 15, 20]
    assert all(isinstance(v, int) for v in spec["ATR_PERIOD"])


@pytest.mark.parametrize("teks", [
    "", "   ", "SL_PCT", "SL_PCT=1:4", "SL_PCT=abc",
    "USE_ATR_EXIT=mungkin", "USE_ATR_EXIT=1:3:1", "=5",
])
def test_parse_spec_menolak_masukan_salah(teks):
    with pytest.raises(gs.GridSearchError):
        gs.parse_spec_cli(teks)
