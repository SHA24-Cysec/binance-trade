"""Uji GERBANG PUMP: naik 24 jam dan volume sedang naik.

Gerbang ini WAJIB dan berjalan sebelum deteksi pullback retest. Semua data
candle berasal dari synthetic_data.py, modul yang SAMA dengan yang dipakai
selftest bot, backtest satu simbol, dan backtest portofolio, supaya kalau
kontrak gerbang berubah semua lapisan gagal bersamaan.

Tidak ada satu pun tes di sini yang menyentuh jaringan: candle harian
di-inject lewat fungsi pengambil palsu.
"""
from __future__ import annotations

import backtest
import config as cfg_mod
import market_scanner as scanner
import portfolio_backtest as pbt
from strategy import Kline
from synthetic_data import riwayat_harian, seri_dengan_setup

CFG = dict(cfg_mod.PUMP_CONFIG)
HARI_MS = 86_400_000
# Waktu acuan sesudah tujuh candle harian pertama (indeks 0..6) tertutup.
REF_MS = 7 * HARI_MS + 1


def harian(quote_volume: float, jumlah: int = 7, mulai: int = 0) -> list[Kline]:
    """Candle 1d datar dengan quote_volume yang sudah ditentukan."""
    return [
        Kline(open_time=(mulai + i) * HARI_MS, open=1.0, high=1.0, low=1.0, close=1.0,
              close_time=(mulai + i + 1) * HARI_MS - 1,
              volume=quote_volume, quote_volume=quote_volume)
        for i in range(jumlah)
    ]


def ticker(symbol: str, pct: float, qv: float, last: float = 1.0) -> dict:
    return {"symbol": symbol, "priceChangePercent": str(pct),
            "quoteVolume": str(qv), "lastPrice": str(last)}


# ======================================================================
# (a) Syarat 1: kenaikan harga 24 jam
# ======================================================================

def test_naik_di_bawah_ambang_ditolak():
    # Threshold disematkan agar tes menguji LOGIKA gerbang, bukan nilai tuning
    # default (yang boleh berubah saat optimasi). Kenaikan di bawah ambang
    # harus ditolak.
    cfg = dict(CFG)
    cfg["PUMP_MIN_24H_CHANGE_PCT"] = 10.0
    ok, alasan = scanner.is_pumping_today(
        "AUSDT", 9.0, 10_000_000.0, lambda s: harian(1_000_000.0), cfg,
        reference_ms=REF_MS)
    assert not ok
    assert "kenaikan 24 jam" in alasan


def test_naik_di_atas_ambang_lolos_syarat_kenaikan():
    cfg = dict(CFG)
    cfg["PUMP_MIN_24H_CHANGE_PCT"] = 9.0
    ok, alasan = scanner.is_pumping_today(
        "AUSDT", 10.0, 10_000_000.0, lambda s: harian(1_000_000.0), cfg,
        reference_ms=REF_MS)
    assert ok, alasan


def test_koin_yang_turun_tidak_pernah_jadi_kandidat():
    tickers = [ticker("CUSDT", -3.0, 9_000_000.0)]
    hasil = scanner.filter_and_rank_candidates(
        tickers, CFG, get_daily_klines_fn=lambda s: harian(1_000.0),
        reference_ms=REF_MS)
    assert hasil == []


def test_syarat_kenaikan_tidak_memakai_request_klines():
    """Simbol yang gagal syarat 1 tidak boleh memicu request candle harian.

    Ini yang menjaga beban rate limit: candle harian hanya diminta untuk
    subset kecil yang sudah lolos syarat kenaikan.
    """
    dipanggil = []

    def fetcher(symbol):
        dipanggil.append(symbol)
        return harian(1_000.0)

    scanner.is_pumping_today("AUSDT", 1.0, 10_000_000.0, fetcher, CFG,
                             reference_ms=REF_MS)
    assert dipanggil == []


# ======================================================================
# (b) Syarat 2: volume sedang naik
# ======================================================================

def test_volume_di_bawah_ambang_ditolak():
    """Rasio di bawah PUMP_VOLUME_SURGE_MULT (threshold disematkan) ditolak.

    Ambang di-pin ke 3.0 agar tes menguji LOGIKA gerbang volume, bukan nilai
    tuning default yang boleh berubah saat optimasi. Rasio 2.5x < 3.0 ditolak.
    """
    cfg = dict(CFG)
    cfg["PUMP_VOLUME_SURGE_MULT"] = 3.0
    ok, alasan = scanner.is_pumping_today(
        "AUSDT", 20.0, 2_500_000.0, lambda s: harian(1_000_000.0), cfg,
        reference_ms=REF_MS)
    assert not ok
    assert "2.50x" in alasan


def test_volume_tiga_kali_lolos():
    ok, alasan = scanner.is_pumping_today(
        "AUSDT", 20.0, 3_000_000.0, lambda s: harian(1_000_000.0), CFG,
        reference_ms=REF_MS)
    assert ok, alasan


def test_rata_rata_memakai_tujuh_hari_penuh_terakhir():
    """Candle harian berjalan (belum tertutup) tidak boleh ikut dirata-rata."""
    tujuh = harian(1_000_000.0)                      # hari 0..6, semua tertutup
    berjalan = Kline(open_time=7 * HARI_MS, open=1.0, high=1.0, low=1.0, close=1.0,
                     close_time=8 * HARI_MS - 1, volume=9e9, quote_volume=9e9)
    rata, _ = scanner.average_prior_daily_quote_volume(tujuh + [berjalan], REF_MS)
    assert rata == 1_000_000.0


def test_ambang_bisa_diubah_lewat_config():
    cfg = dict(CFG)
    cfg["PUMP_MIN_24H_CHANGE_PCT"] = 25.0
    cfg["PUMP_VOLUME_SURGE_MULT"] = 3.0
    fetcher = lambda s: harian(1_000_000.0)  # noqa: E731

    ok_a, _ = scanner.is_pumping_today("AUSDT", 20.0, 9_000_000.0, fetcher, cfg,
                                       reference_ms=REF_MS)
    ok_b, _ = scanner.is_pumping_today("AUSDT", 30.0, 2_900_000.0, fetcher, cfg,
                                       reference_ms=REF_MS)
    ok_c, _ = scanner.is_pumping_today("AUSDT", 30.0, 3_000_000.0, fetcher, cfg,
                                       reference_ms=REF_MS)
    assert not ok_a and not ok_b and ok_c


# ======================================================================
# (c) Lolos syarat 1 tetapi gagal syarat 2 tetap ditolak
# ======================================================================

def test_naik_banyak_tapi_volume_tidak_naik_tetap_ditolak():
    tickers = [ticker("EUSDT", 40.0, 4_000_000.0)]
    hasil = scanner.filter_and_rank_candidates(
        tickers, CFG, get_daily_klines_fn=lambda s: harian(4_000_000.0),
        reference_ms=REF_MS)
    assert hasil == []


def test_dua_syarat_harus_terpenuhi_bersamaan():
    fetcher = lambda s: harian(1_000_000.0)  # noqa: E731
    hanya_volume, _ = scanner.is_pumping_today("AUSDT", 2.0, 5_000_000.0, fetcher,
                                               CFG, reference_ms=REF_MS)
    hanya_naik, _ = scanner.is_pumping_today("AUSDT", 40.0, 1_000_000.0, fetcher,
                                             CFG, reference_ms=REF_MS)
    keduanya, _ = scanner.is_pumping_today("AUSDT", 40.0, 5_000_000.0, fetcher,
                                           CFG, reference_ms=REF_MS)
    assert not hanya_volume and not hanya_naik and keduanya


# ======================================================================
# (d) Kasus tepi: riwayat harian kurang, data rusak, request gagal
# ======================================================================

def test_candle_harian_kurang_dari_tujuh_ditolak():
    ok, alasan = scanner.is_pumping_today(
        "BARUUSDT", 50.0, 9_000_000.0, lambda s: harian(1_000.0, jumlah=6), CFG,
        reference_ms=REF_MS)
    assert not ok
    assert "riwayat harian kurang" in alasan


def test_quote_volume_nan_tidak_lolos_diam_diam():
    rusak = harian(1_000_000.0)
    rusak[-1] = rusak[-1]._replace(quote_volume=float("nan"))
    rata, alasan = scanner.average_prior_daily_quote_volume(rusak, REF_MS)
    assert rata is None
    assert "tidak wajar" in alasan

    ok, _ = scanner.is_pumping_today("AUSDT", 50.0, 9_000_000.0, lambda s: rusak,
                                     CFG, reference_ms=REF_MS)
    assert not ok


def test_quote_volume_negatif_dan_tak_hingga_ditolak():
    for nilai in (-1.0, float("inf")):
        rusak = harian(1_000_000.0)
        rusak[0] = rusak[0]._replace(quote_volume=nilai)
        rata, _ = scanner.average_prior_daily_quote_volume(rusak, REF_MS)
        assert rata is None, nilai


def test_request_klines_gagal_hanya_membuang_simbol_itu():
    def fetcher(symbol):
        if symbol == "GAGALUSDT":
            raise RuntimeError("timeout")
        return harian(1_000_000.0)

    tickers = [ticker("GAGALUSDT", 30.0, 9_000_000.0),
               ticker("AUSDT", 30.0, 9_000_000.0)]
    hasil = scanner.filter_and_rank_candidates(
        tickers, CFG, get_daily_klines_fn=fetcher, reference_ms=REF_MS)
    assert [c.symbol for c in hasil] == ["AUSDT"]


def test_tanpa_sumber_candle_harian_semua_ditolak():
    """Fail closed, bukan fail open."""
    tickers = [ticker("AUSDT", 50.0, 9_000_000.0)]
    assert scanner.filter_and_rank_candidates(tickers, CFG) == []


def test_log_menyebut_nol_kandidat(caplog):
    tickers = [ticker("CUSDT", -3.0, 9_000_000.0)]
    with caplog.at_level("INFO", logger="market_scanner"):
        scanner.filter_and_rank_candidates(
            tickers, CFG, get_daily_klines_fn=lambda s: harian(1_000.0),
            reference_ms=REF_MS)
    assert any("0 kandidat lolos gerbang pump" in r.getMessage()
               for r in caplog.records)


# ======================================================================
# (e) Tidak ada look-ahead pada jalur backtest
# ======================================================================

def test_tidak_ada_look_ahead_rata_rata_harian():
    """Volume hari-hari SESUDAH titik waktu uji tidak boleh ikut terhitung."""
    lama = harian(1_000_000.0)                       # hari 0..6
    nanti = harian(9_000_000_000.0, jumlah=3, mulai=7)  # hari 7..9, masa depan
    rata_titik_waktu, _ = scanner.average_prior_daily_quote_volume(lama + nanti, REF_MS)
    assert rata_titik_waktu == 1_000_000.0

    # Pada titik waktu yang lebih maju, barulah volume besar itu terlihat.
    rata_kemudian, _ = scanner.average_prior_daily_quote_volume(
        lama + nanti, 10 * HARI_MS + 1)
    assert rata_kemudian > 1_000_000.0


def test_backtest_satu_simbol_memakai_gerbang_yang_sama():
    """Data yang sama: gerbang longgar menghasilkan trade, gerbang ketat nol."""
    kl = seri_dengan_setup(ekor="naik", panjang_ekor=20)
    daily = riwayat_harian(kl, quote_volume_harian=1_000.0)

    longgar = dict(CFG)
    longgar.update({"MIN_QUOTE_VOLUME_USDT_24H": 1_000_000,
                    "PUMP_MIN_24H_CHANGE_PCT": -1000.0,
                    "PUMP_VOLUME_SURGE_MULT": 0.0})
    hasil_longgar = backtest.run_backtest(kl, longgar, warmup_bars=0,
                                          daily_klines=daily)
    assert len(hasil_longgar.trades) >= 1

    ketat = dict(longgar)
    ketat["PUMP_MIN_24H_CHANGE_PCT"] = 500.0      # mustahil dicapai data ini
    hasil_ketat = backtest.run_backtest(kl, ketat, warmup_bars=0, daily_klines=daily)
    assert hasil_ketat.trades == []

    ketat_volume = dict(longgar)
    ketat_volume["PUMP_VOLUME_SURGE_MULT"] = 1e9
    hasil_kv = backtest.run_backtest(kl, ketat_volume, warmup_bars=0,
                                     daily_klines=daily)
    assert hasil_kv.trades == []


def test_backtest_tanpa_riwayat_harian_penuh_menolak_entry():
    """Fail closed juga berlaku di backtest: riwayat kurang berarti nol trade."""
    kl = seri_dengan_setup(ekor="naik", panjang_ekor=20)
    cfg = dict(CFG)
    cfg.update({"MIN_QUOTE_VOLUME_USDT_24H": 1_000_000,
                "PUMP_MIN_24H_CHANGE_PCT": -1000.0,
                "PUMP_VOLUME_SURGE_MULT": 0.0})
    kurang = riwayat_harian(kl, hari=3, quote_volume_harian=1_000.0)
    assert backtest.run_backtest(kl, cfg, warmup_bars=0,
                                 daily_klines=kurang).trades == []


def test_backtest_portofolio_memakai_gerbang_yang_sama():
    kl = seri_dengan_setup(harga=100.0, ekor="naik", panjang_ekor=20)
    data = {"AUSDT": kl}
    daily = {"AUSDT": riwayat_harian(kl, quote_volume_harian=1_000.0)}

    cfg = dict(CFG)
    cfg.update({"MIN_QUOTE_VOLUME_USDT_24H": 0, "COOLDOWN_MINUTES_AFTER_CLOSE": 0,
                "PUMP_MIN_24H_CHANGE_PCT": -1000.0, "PUMP_VOLUME_SURGE_MULT": 0.0})

    longgar = pbt.run_portfolio_backtest(data, cfg, "5m", daily_klines=daily)
    assert len(longgar.trades) >= 1

    ketat = dict(cfg)
    ketat["PUMP_MIN_24H_CHANGE_PCT"] = 500.0
    assert pbt.run_portfolio_backtest(data, ketat, "5m",
                                      daily_klines=daily).trades == []
