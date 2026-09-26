"""
Tes cache candle backtest lintas job (backtest_cache.py).
=========================================================

Tanpa jaringan: klien Binance dipalsukan dan menghitung setiap request,
sehingga klaim "backtest ulang tidak mengunduh dari nol" bisa dibuktikan
angkanya, bukan sekadar diklaim.

Yang diuji:
  1. Aritmetika rentang (merge/subtract) yang menjadi dasar unduh inkremental.
  2. Job kedua hanya mengunduh jendela segar, bukan seluruh periode.
  3. Kebijakan KETAT: 24 jam terakhir selalu diunduh ulang dan menimpa cache.
  4. Rentang yang memang kosong tidak diminta ulang setiap job.
  5. Hasil simulasi identik dengan maupun tanpa cache.
  6. Pemangkasan TTL, pembersihan manual, dan kenaikan versi skema.
  7. Cache rusak tidak menggagalkan backtest (fallback unduh penuh).
"""

from __future__ import annotations

import os
import re
import sqlite3
import time

import pytest

import backtest_cache as kcache
import portfolio_backtest as pbt
from backtest_cache import KlineCache, merge_ranges, subtract_ranges
from backtest_storage import KlineStore
from strategy import Kline

MS_5M = 5 * 60 * 1000
MS_HARI = 24 * 60 * 60 * 1000


# ======================================================================
# Perkakas
# ======================================================================

def deret_5m(bars: int, akhir_ms: int, harga_awal: float = 100.0) -> list[Kline]:
    """Candle 5 menit deterministik yang berakhir pada ``akhir_ms``."""
    mulai = akhir_ms - bars * MS_5M
    mulai -= mulai % MS_5M
    keluar = []
    for i in range(bars):
        t = mulai + i * MS_5M
        harga = harga_awal + (i % 20) * 0.1
        keluar.append(Kline(open_time=t, open=harga, high=harga * 1.002,
                            low=harga * 0.998, close=harga * 1.001,
                            close_time=t + MS_5M - 1, volume=1000.0,
                            quote_volume=500_000.0))
    return keluar


def baris_mentah(k: Kline) -> list:
    return [k.open_time, f"{k.open}", f"{k.high}", f"{k.low}", f"{k.close}",
            f"{k.volume}", k.close_time, f"{k.quote_volume}", 10, "0", "0", "0"]


class KlienPencatat:
    """Klien palsu yang menghormati rentang waktu dan menghitung request."""

    def __init__(self, data: dict[str, list[Kline]]) -> None:
        self.data = data
        self.request = 0
        self.rentang_diminta: list[tuple[str, int, int]] = []

    def get_klines(self, symbol, interval="5m", limit=1000,
                   start_time_ms=None, end_time_ms=None):
        self.request += 1
        awal = int(start_time_ms or 0)
        akhir = int(end_time_ms or 2 ** 62)
        self.rentang_diminta.append((symbol, awal, akhir))
        cocok = [k for k in self.data.get(symbol, [])
                 if awal <= k.open_time <= akhir]
        return [baris_mentah(k) for k in cocok[:int(limit)]]


@pytest.fixture
def cache(tmp_path) -> KlineCache:
    obj = KlineCache(str(tmp_path / "cache.sqlite3"), fresh_hours=24, ttl_days=30)
    yield obj
    obj.close()


# ======================================================================
# 1. Aritmetika rentang
# ======================================================================

def test_merge_ranges_menggabungkan_yang_tumpang_tindih_dan_berdempetan():
    assert merge_ranges([(10, 20), (15, 30)]) == [(10, 30)]
    assert merge_ranges([(10, 20), (21, 30)]) == [(10, 30)]
    assert merge_ranges([(10, 20), (25, 30)]) == [(10, 20), (25, 30)]
    assert merge_ranges([]) == []
    assert merge_ranges([(30, 10)]) == []


def test_subtract_ranges_menyisakan_bagian_yang_belum_tertutup():
    assert subtract_ranges(0, 100, []) == [(0, 100)]
    assert subtract_ranges(0, 100, [(0, 100)]) == []
    assert subtract_ranges(0, 100, [(0, 40)]) == [(41, 100)]
    assert subtract_ranges(0, 100, [(60, 100)]) == [(0, 59)]
    assert subtract_ranges(0, 100, [(30, 50)]) == [(0, 29), (51, 100)]
    assert subtract_ranges(0, 100, [(30, 50), (70, 80)]) == [(0, 29), (51, 69), (81, 100)]
    assert subtract_ranges(100, 50, [(0, 10)]) == []


def test_missing_ranges_menghormati_jendela_segar(cache):
    sekarang = int(time.time() * 1000)
    awal = sekarang - 10 * MS_HARI
    assert cache.missing_ranges("AUSDT", "5m", awal, sekarang) == [(awal, sekarang)]

    cache.put("AUSDT", "5m", [], awal, sekarang)
    sisa = cache.missing_ranges("AUSDT", "5m", awal, sekarang)
    # Bagian historis dipercaya, 24 jam terakhir tetap wajib diunduh ulang.
    assert len(sisa) == 1
    lebar_jam = (sisa[0][1] - sisa[0][0]) / 3_600_000
    assert 23.9 <= lebar_jam <= 24.1
    assert sisa[0][1] == sekarang


def test_jendela_segar_nol_membuat_seluruh_cakupan_dipercaya(tmp_path):
    with KlineCache(str(tmp_path / "c.sqlite3"), fresh_hours=0) as c:
        sekarang = int(time.time() * 1000)
        c.put("AUSDT", "5m", [], sekarang - MS_HARI, sekarang)
        assert c.missing_ranges("AUSDT", "5m", sekarang - MS_HARI, sekarang) == []


# ======================================================================
# 2 & 3. Unduh inkremental dan kesegaran
# ======================================================================

def _unduh(klien, cache, symbols, awal, akhir) -> dict:
    """Jalankan satu 'job' unduh ke store sementara, kembalikan isinya."""
    with KlineStore.create_temp() as store:
        berhasil, gagal = pbt.fetch_universe_klines(
            klien, symbols, "5m", awal, akhir, store, cache=cache)
        isi = {sym: store.load_klines(sym) for sym in berhasil}
    return {"berhasil": berhasil, "gagal": gagal, "isi": isi}


def test_job_kedua_hanya_mengunduh_jendela_segar(cache):
    # Sepuluh hari = 2.880 candle 5 menit, jadi unduh penuh butuh tiga
    # halaman (batas 1.000 candle per request) sedangkan job kedua cukup
    # satu halaman untuk jendela segar 24 jam.
    sekarang = int(time.time() * 1000)
    data = {"AUSDT": deret_5m(10 * 288, sekarang)}
    awal = data["AUSDT"][0].open_time
    klien = KlienPencatat(data)

    pertama = _unduh(klien, cache, ["AUSDT"], awal, sekarang)
    request_pertama = klien.request
    assert pertama["berhasil"] == ["AUSDT"]
    assert request_pertama >= 1

    klien.request = 0
    klien.rentang_diminta.clear()
    kedua = _unduh(klien, cache, ["AUSDT"], awal, sekarang)

    assert request_pertama >= 3, request_pertama
    assert klien.request < request_pertama
    # Yang diminta hanya jendela segar, bukan seluruh periode tiga hari.
    for _sym, minta_awal, _minta_akhir in klien.rentang_diminta:
        assert minta_awal >= cache.trusted_until() - MS_5M
    # Datanya tetap utuh: job kedua melihat candle yang sama persis.
    assert kedua["isi"]["AUSDT"] == pertama["isi"]["AUSDT"]


def test_candle_dalam_jendela_segar_selalu_diperbarui(cache):
    sekarang = int(time.time() * 1000)
    data = {"AUSDT": deret_5m(3 * 288, sekarang)}
    awal = data["AUSDT"][0].open_time
    klien = KlienPencatat(data)
    _unduh(klien, cache, ["AUSDT"], awal, sekarang)

    # Bursa merevisi candle terakhir (mis. candle tadinya belum tertutup).
    terakhir = data["AUSDT"][-1]
    data["AUSDT"][-1] = terakhir._replace(close=terakhir.close * 1.5,
                                          high=terakhir.high * 1.5)
    hasil = _unduh(klien, cache, ["AUSDT"], awal, sekarang)

    assert hasil["isi"]["AUSDT"][-1].close == pytest.approx(terakhir.close * 1.5)
    assert cache.read("AUSDT", "5m", awal, sekarang)[-1].close == pytest.approx(
        terakhir.close * 1.5)


def test_candle_lama_tidak_ikut_berubah_saat_sumber_direvisi(cache):
    """Bukti bahwa bagian historis memang tidak diunduh ulang."""
    sekarang = int(time.time() * 1000)
    data = {"AUSDT": deret_5m(3 * 288, sekarang)}
    awal = data["AUSDT"][0].open_time
    klien = KlienPencatat(data)
    _unduh(klien, cache, ["AUSDT"], awal, sekarang)

    lama = data["AUSDT"][0]
    data["AUSDT"][0] = lama._replace(close=lama.close * 9)
    hasil = _unduh(klien, cache, ["AUSDT"], awal, sekarang)

    assert hasil["isi"]["AUSDT"][0].close == pytest.approx(lama.close)


def test_rentang_kosong_tidak_diminta_ulang_setiap_job(cache):
    """Koin yang belum listing tidak boleh memicu request berulang selamanya."""
    sekarang = int(time.time() * 1000)
    lampau_awal = sekarang - 30 * MS_HARI
    lampau_akhir = sekarang - 20 * MS_HARI
    klien = KlienPencatat({"BARUUSDT": []})

    pbt._klines_untuk_simbol(klien, "BARUUSDT", "5m", lampau_awal, lampau_akhir, cache)
    assert klien.request >= 1
    klien.request = 0
    pbt._klines_untuk_simbol(klien, "BARUUSDT", "5m", lampau_awal, lampau_akhir, cache)
    assert klien.request == 0


def test_celah_di_tengah_periode_diunduh_terpisah(cache):
    sekarang = int(time.time() * 1000)
    a1, a2 = sekarang - 30 * MS_HARI, sekarang - 25 * MS_HARI
    b1, b2 = sekarang - 20 * MS_HARI, sekarang - 15 * MS_HARI
    cache.put("AUSDT", "5m", [], a1, a2)
    cache.put("AUSDT", "5m", [], b1, b2)
    sisa = cache.missing_ranges("AUSDT", "5m", a1, b2)
    assert sisa == [(a2 + 1, b1 - 1)]


def test_tanpa_cache_perilaku_sama_seperti_sebelum_ada_cache():
    sekarang = int(time.time() * 1000)
    data = {"AUSDT": deret_5m(600, sekarang)}
    awal = data["AUSDT"][0].open_time
    klien = KlienPencatat(data)
    hasil = _unduh(klien, None, ["AUSDT"], awal, sekarang)
    assert hasil["isi"]["AUSDT"] == data["AUSDT"]
    assert klien.request >= 1


def test_simbol_gagal_tetap_masuk_daftar_gagal_walau_cache_aktif(cache):
    sekarang = int(time.time() * 1000)

    class KlienRusak(KlienPencatat):
        def get_klines(self, symbol, *args, **kwargs):
            if symbol == "RUSAKUSDT":
                raise RuntimeError("koneksi putus")
            return super().get_klines(symbol, *args, **kwargs)

    data = {"AUSDT": deret_5m(300, sekarang)}
    klien = KlienRusak(data)
    awal = data["AUSDT"][0].open_time
    hasil = _unduh(klien, cache, ["AUSDT", "RUSAKUSDT"], awal, sekarang)
    assert hasil["berhasil"] == ["AUSDT"]
    assert [g["symbol"] for g in hasil["gagal"]] == ["RUSAKUSDT"]
    # Kegagalan tidak boleh mencatat cakupan palsu.
    assert cache.coverage("RUSAKUSDT", "5m") == []


# ======================================================================
# 4. Hasil simulasi tidak berubah karena cache
# ======================================================================

def test_hasil_simulasi_sama_dengan_dan_tanpa_cache(tmp_path):
    from tests.test_portfolio_backtest import (config_uji, data_dua_simbol,
                                               harian_dari)
    data = data_dua_simbol()
    harian = harian_dari(data)
    klien = KlienPencatat(data)
    awal = min(k.open_time for kl in data.values() for k in kl)
    akhir = max(k.open_time for kl in data.values() for k in kl)

    def jalankan(cache_obj):
        with KlineStore.create_temp() as store:
            berhasil, _gagal = pbt.fetch_universe_klines(
                klien, list(data), "5m", awal, akhir, store, cache=cache_obj)
            for sym in berhasil:
                store.write_daily(sym, harian[sym])
            store.finish_writing()
            return pbt.run_portfolio_backtest(store, config_uji(), "5m")

    tanpa = jalankan(None)
    with KlineCache(str(tmp_path / "c.sqlite3")) as c:
        dengan_dingin = jalankan(c)
        dengan_hangat = jalankan(c)

    for lain in (dengan_dingin, dengan_hangat):
        assert [(t.symbol, t.entry_time, t.exit_time, t.reason, t.pnl_pct)
                for t in lain.trades] == [
            (t.symbol, t.entry_time, t.exit_time, t.reason, t.pnl_pct)
            for t in tanpa.trades]
        assert lain.final_equity == tanpa.final_equity


# ======================================================================
# 5. Perawatan cache
# ======================================================================

def test_prune_membuang_simbol_yang_lama_tidak_dipakai(tmp_path):
    with KlineCache(str(tmp_path / "c.sqlite3"), ttl_days=7) as c:
        sekarang = int(time.time() * 1000)
        c.put("AUSDT", "5m", deret_5m(10, sekarang), sekarang - MS_HARI, sekarang)
        assert c.stats()["rows"] == 10
        assert c.prune() == 0
        # Seolah-olah pemakaian terakhir 30 hari lalu.
        assert c.prune(now_ms=sekarang + 30 * MS_HARI) == 1
        assert c.stats()["rows"] == 0
        assert c.coverage("AUSDT", "5m") == []


def test_ttl_nol_tidak_pernah_memangkas(tmp_path):
    with KlineCache(str(tmp_path / "c.sqlite3"), ttl_days=0) as c:
        sekarang = int(time.time() * 1000)
        c.put("AUSDT", "5m", deret_5m(5, sekarang), sekarang - MS_HARI, sekarang)
        assert c.prune(now_ms=sekarang + 3650 * MS_HARI) == 0
        assert c.stats()["rows"] == 5


def test_clear_mengosongkan_seluruh_cache(cache):
    sekarang = int(time.time() * 1000)
    cache.put("AUSDT", "5m", deret_5m(5, sekarang), sekarang - MS_HARI, sekarang)
    cache.clear()
    assert cache.stats()["rows"] == 0
    assert cache.missing_ranges("AUSDT", "5m", sekarang - MS_HARI, sekarang)


def test_versi_skema_berbeda_membuang_isi_cache_lama(tmp_path):
    path = str(tmp_path / "c.sqlite3")
    with KlineCache(path) as c:
        sekarang = int(time.time() * 1000)
        c.put("AUSDT", "5m", deret_5m(5, sekarang), sekarang - MS_HARI, sekarang)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE meta SET value = ? WHERE key = ?",
                     ("0", "schema_version"))
        conn.commit()
    with KlineCache(path) as c:
        assert c.stats()["rows"] == 0


def test_cache_dipakai_dua_objek_sekaligus_tanpa_saling_merusak(tmp_path):
    """Dua job backtest paralel boleh menulis file cache yang sama."""
    path = str(tmp_path / "c.sqlite3")
    sekarang = int(time.time() * 1000)
    with KlineCache(path) as satu, KlineCache(path) as dua:
        satu.put("AUSDT", "5m", deret_5m(10, sekarang), sekarang - MS_HARI, sekarang)
        dua.put("BUSDT", "5m", deret_5m(10, sekarang), sekarang - MS_HARI, sekarang)
        assert len(satu.read("BUSDT", "5m", 0, sekarang)) == 10
        assert len(dua.read("AUSDT", "5m", 0, sekarang)) == 10


# ======================================================================
# 6. Konfigurasi, kegagalan, dan keamanan
# ======================================================================

def test_open_kline_cache_menghormati_config(tmp_path):
    path = str(tmp_path / "c.sqlite3")
    assert pbt.open_kline_cache({"BACKTEST_CACHE_ENABLED": False,
                                 "BACKTEST_CACHE_FILE": path}) is None
    assert pbt.open_kline_cache({"BACKTEST_CACHE_ENABLED": True,
                                 "BACKTEST_CACHE_FILE": ""}) is None
    c = pbt.open_kline_cache({"BACKTEST_CACHE_ENABLED": True,
                              "BACKTEST_CACHE_FILE": path,
                              "BACKTEST_CACHE_FRESH_HOURS": 6,
                              "BACKTEST_CACHE_TTL_DAYS": 3})
    try:
        assert c is not None
        assert c.fresh_ms == 6 * 3_600_000
        assert c.ttl_ms == 3 * MS_HARI
    finally:
        if c is not None:
            c.close()


def test_cache_tidak_bisa_dibuka_tidak_menggagalkan_backtest(tmp_path):
    """Disk penuh atau izin tulis hilang tidak boleh membunuh job."""
    tabrakan = tmp_path / "bukan_folder"
    tabrakan.write_text("ini file, bukan direktori", encoding="utf-8")
    hasil = pbt.open_kline_cache({"BACKTEST_CACHE_ENABLED": True,
                                  "BACKTEST_CACHE_FILE": str(tabrakan / "c.sqlite3")})
    assert hasil is None

    # Unduh tetap jalan tanpa cache.
    sekarang = int(time.time() * 1000)
    data = {"AUSDT": deret_5m(300, sekarang)}
    klien = KlienPencatat(data)
    keluar = _unduh(klien, hasil, ["AUSDT"], data["AUSDT"][0].open_time, sekarang)
    assert keluar["berhasil"] == ["AUSDT"]


def test_akses_setelah_close_ditolak(tmp_path):
    c = KlineCache(str(tmp_path / "c.sqlite3"))
    c.close()
    with pytest.raises(kcache.CacheError):
        c.read("AUSDT", "5m", 0, 1)


def test_tidak_ada_sql_dari_fstring_di_modul_cache():
    akar = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(akar, "backtest_cache.py"), encoding="utf-8") as handle:
        isi = handle.read()
    assert not re.search(r"execute(?:script|many)?\s*\(\s*f[\"']", isi)
    assert not re.search(r"execute(?:script|many)?\s*\([^)]*\.format\(", isi)
    for baris in isi.splitlines():
        tanpa_komentar = baris.split("#", 1)[0]
        if re.search(r"f[\"'][^\"']*\b(SELECT|INSERT|UPDATE|DELETE)\b", tanpa_komentar):
            raise AssertionError(f"SQL dari f-string: {baris.strip()}")


# ==== RINGKASAN AUDIT (tests/test_backtest_cache.py) ==================
# Lingkup: berkas tes BARU untuk backtest_cache.py dan integrasinya di
#   portfolio_backtest.fetch_universe_klines().
# Cakupan: aritmetika rentang, unduh inkremental (dibuktikan dengan
#   penghitung request), kebijakan kesegaran ketat 24 jam (candle baru
#   tertimpa, candle lama tidak diunduh ulang), rentang kosong tidak diminta
#   ulang, celah di tengah periode, daftar simbol gagal, paritas hasil
#   simulasi dengan dan tanpa cache, prune TTL, clear, kenaikan versi skema,
#   dua objek cache pada satu file, config, fallback saat cache gagal dibuka,
#   dan larangan SQL dari f-string.
# Sintaks/tipe: tanpa jaringan, tanpa tidur, deterministik. Semua cache
#   dibuat di tmp_path sehingga tes tidak pernah menulis ke folder repo
#   (bawaan produksi Data/backtest_cache.sqlite3).
# Keamanan: satu tes khusus membaca sumber backtest_cache.py dan menolak SQL
#   yang dirakit dari f-string atau .format().
# Race condition: test_cache_dipakai_dua_objek_sekaligus_tanpa_saling_merusak
#   meniru dua job backtest paralel yang memakai satu file cache (WAL +
#   busy_timeout).
# Kebersihan: fixture memakai yield lalu close(), dan tes lain memakai
#   context manager, jadi tidak ada koneksi menggantung.
# =======================================================================
