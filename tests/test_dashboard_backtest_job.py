"""
Tes alur backtest PORTOFOLIO end-to-end lewat dashboard._bt_run_job.
=====================================================================

Tanpa jaringan: klien Binance diganti objek palsu yang membangkitkan candle
5 menit deterministik (termasuk paging 1000 candle seperti endpoint asli).

Fokusnya bukan angka hasil simulasi (itu diuji di
tests/test_portfolio_backtest.py), melainkan bahwa setelah candle pindah ke
SQLite temporary:
  - job selesai dengan status "done" dan payload lengkap,
  - file SQLite temporary job selalu terhapus, baik saat selesai maupun saat
    dibatalkan dari thread lain lewat cancel.
"""

from __future__ import annotations

import os
import time

import pytest

import dashboard
import portfolio_backtest as pbt

MS_PER_5M = 5 * 60 * 1000


class KlienBinancePalsu:
    """Membangkitkan candle deterministik untuk rentang waktu apa pun."""

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = list(symbols)
        self.jumlah_request = 0

    # -- endpoint pasar ------------------------------------------------
    def get_ticker_24hr_all(self) -> list:
        return [{"symbol": sym, "priceChangePercent": "5.0",
                 "quoteVolume": str(9_000_000 - 1000 * i), "lastPrice": "100.0"}
                for i, sym in enumerate(self.symbols)]

    def get_exchange_info(self) -> dict:
        return {"symbols": [{"symbol": sym, "status": "TRADING",
                             "isSpotTradingAllowed": True}
                            for sym in self.symbols]}

    def get_klines(self, symbol, interval="5m", limit=1000,
                   start_time_ms=None, end_time_ms=None) -> list:
        self.jumlah_request += 1
        langkah = 24 * 60 * 60 * 1000 if interval == "1d" else MS_PER_5M
        mulai = int(start_time_ms or 0)
        mulai -= mulai % langkah
        akhir = int(end_time_ms or mulai)
        dasar = 100.0 + 7.0 * (self.symbols.index(symbol) if symbol in self.symbols else 0)
        rows = []
        t = mulai
        while t <= akhir and len(rows) < int(limit):
            putaran = (t // langkah) % 24
            harga = dasar * (1.0 + 0.004 * putaran)
            tinggi = harga * 1.004
            rendah = harga * 0.996
            rows.append([t, f"{harga:.8f}", f"{tinggi:.8f}", f"{rendah:.8f}",
                         f"{harga * 1.001:.8f}", "1000", t + langkah - 1,
                         "5000000", 10, "0", "0", "0"])
            t += langkah
        return rows


@pytest.fixture
def job_palsu(monkeypatch, tmp_path):
    """Siapkan dashboard dengan klien palsu dan satu job kosong.

    Cache candle diarahkan ke tmp_path supaya tes tidak pernah menulis ke
    folder repo (bawaannya Data/backtest_cache.sqlite3).
    """
    klien = KlienBinancePalsu(["AAAUSDT", "BBBUSDT", "CCCUSDT"])
    monkeypatch.setattr(dashboard, "_HAS_CLIENT", True, raising=False)
    monkeypatch.setattr(dashboard, "BinanceSpotClient",
                        lambda *args, **kwargs: klien, raising=False)
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "BACKTEST_CACHE_FILE",
                        str(tmp_path / "backtest_cache.sqlite3"))

    job_id = "uji-backtest"
    with dashboard._bt_jobs_lock:
        dashboard._bt_jobs[job_id] = {"status": "running", "progress": 0.0,
                                      "stage": "", "created_at": time.time()}
    yield job_id, klien
    with dashboard._bt_jobs_lock:
        dashboard._bt_jobs.pop(job_id, None)


def _rekam_store(monkeypatch) -> list:
    """Catat path setiap store yang dibuat job supaya bisa dicek kebersihannya."""
    dibuat: list = []
    asli = pbt.new_backtest_store

    def pembungkus(config=None):
        store = asli(config)
        dibuat.append(store.db_path)
        return store

    monkeypatch.setattr(dashboard.pbt, "new_backtest_store", pembungkus)
    return dibuat


def test_job_backtest_portofolio_selesai_dan_membersihkan_file(job_palsu, monkeypatch):
    job_id, klien = job_palsu
    dibuat = _rekam_store(monkeypatch)

    dashboard._bt_run_job(job_id, days=3, overrides={}, max_symbols=3)

    with dashboard._bt_jobs_lock:
        job = dict(dashboard._bt_jobs[job_id])
    assert job["status"] == "done", job.get("error")
    payload = job["result"]
    assert payload["mode"] == "portfolio"
    assert payload["universe_with_data"] == 3
    assert payload["symbols_failed_count"] == 0
    assert payload["bars_total"] > 0
    assert "summary" in payload and "trades" in payload
    assert klien.jumlah_request > 0
    assert payload["cache"] is not None and payload["cache"]["rows"] > 0

    assert dibuat, "job harus membuat store SQLite temporary"
    for path in dibuat:
        assert not os.path.exists(path)
        assert not os.path.exists(os.path.dirname(path))


def test_job_yang_dibatalkan_tetap_menghapus_file_temporary(job_palsu, monkeypatch):
    """Pembatalan dari thread lain harus tetap menghapus file temporary."""
    job_id, _klien = job_palsu
    dibuat = _rekam_store(monkeypatch)

    # Tombol batal ditekan saat unduhan simbol pertama sudah jalan, yaitu
    # setelah store SQLite dibuat. Ini jalur yang paling rawan meninggalkan
    # file yatim kalau blok finally hilang.
    asli_fetch = pbt.fetch_universe_klines

    def fetch_lalu_batal(client, symbols, interval, start_ms, end_ms, store,
                         progress_cb=None, sleep_between_symbols=0.0, cancel_cb=None):
        def progres(frac, sym):
            with dashboard._bt_jobs_lock:
                dashboard._bt_jobs[job_id]["cancel"] = True
            if progress_cb:
                progress_cb(frac, sym)
        return asli_fetch(client, symbols, interval, start_ms, end_ms, store,
                          progress_cb=progres,
                          sleep_between_symbols=sleep_between_symbols,
                          cancel_cb=cancel_cb)

    monkeypatch.setattr(dashboard.pbt, "fetch_universe_klines", fetch_lalu_batal)

    dashboard._bt_run_job(job_id, days=3, overrides={}, max_symbols=3)

    with dashboard._bt_jobs_lock:
        job = dict(dashboard._bt_jobs[job_id])
    assert job["status"] == "error"
    assert "dibatalkan" in job["error"].lower()
    assert dibuat, "store tetap harus sudah dibuat sebelum pembatalan"
    for path in dibuat:
        assert not os.path.exists(path)
        assert not os.path.exists(os.path.dirname(path))




def test_job_kedua_memakai_cache_dan_jauh_lebih_sedikit_request(job_palsu, monkeypatch):
    """Backtest ulang tidak boleh mengunduh candle historis dari nol lagi."""
    job_id, klien = job_palsu

    dashboard._bt_run_job(job_id, days=5, overrides={}, max_symbols=3)
    with dashboard._bt_jobs_lock:
        job_pertama = dict(dashboard._bt_jobs[job_id])
    assert job_pertama["status"] == "done", job_pertama.get("error")
    request_pertama = klien.jumlah_request

    with dashboard._bt_jobs_lock:
        dashboard._bt_jobs[job_id] = {"status": "running", "progress": 0.0,
                                      "stage": "", "created_at": time.time()}
    klien.jumlah_request = 0
    dashboard._bt_run_job(job_id, days=5, overrides={}, max_symbols=3)
    with dashboard._bt_jobs_lock:
        job_kedua = dict(dashboard._bt_jobs[job_id])
    assert job_kedua["status"] == "done", job_kedua.get("error")
    request_kedua = klien.jumlah_request

    assert request_kedua < request_pertama, (request_pertama, request_kedua)
    # Hasil simulasi harus tetap sama persis walau sumber candle-nya cache.
    assert job_kedua["result"]["summary"] == job_pertama["result"]["summary"]
    assert job_kedua["result"]["trades"] == job_pertama["result"]["trades"]


# ==== RINGKASAN AUDIT (tests/test_dashboard_backtest_job.py) ==========
# Lingkup: berkas tes BARU untuk alur _bt_run_job end-to-end.
# Cakupan: job sukses (status done, payload lengkap termasuk ringkasan cache,
#   file temporary terhapus), job dibatalkan saat unduhan berjalan (status
#   error "dibatalkan", file temporary tetap terhapus), dan job kedua yang
#   memakai cache candle sehingga jumlah request turun sementara hasil
#   simulasinya sama persis dengan job pertama.
# Sintaks/tipe: tanpa jaringan; BinanceSpotClient di dashboard di-monkeypatch
#   dengan klien palsu yang juga meniru paging 1.000 candle.
# Keamanan: tidak menyentuh kredensial. BACKTEST_CACHE_FILE diarahkan ke
#   tmp_path lewat monkeypatch.setitem, jadi tes tidak pernah menulis ke
#   folder repo (bawaan produksi Data/backtest_cache.sqlite3).
# Race condition: pembatalan disuntikkan lewat progress_cb sehingga terjadi
#   SETELAH store dibuat, yaitu titik paling rawan meninggalkan file yatim;
#   deterministik, tanpa sleep-race.
# Kebersihan: entri _bt_jobs dibuang lagi oleh fixture supaya tes lain tidak
#   melihat sisa job; store dan cache ditutup oleh kode produksi di finally.
# =======================================================================
