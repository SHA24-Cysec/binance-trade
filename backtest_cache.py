#!/usr/bin/env python3
"""
Cache candle backtest lintas job (SQLite permanen, unduh inkremental).
======================================================================

Masalah yang diselesaikan
-------------------------
Setiap job backtest portofolio mengunduh ulang seluruh candle dari Binance,
padahal alur paling lazim justru menjalankan ulang backtest dengan parameter
exit yang berbeda di atas DATA YANG SAMA. Untuk 150 simbol x 30 hari itu
berarti sekitar 1.350 request identik setiap kali tombol ditekan.

Modul ini menyimpan candle yang sudah pernah diunduh ke satu file SQLite
PERMANEN (default ``Data/backtest_cache.sqlite3`` di folder repo), lalu job
berikutnya hanya mengunduh bagian yang BELUM ada. Bedakan dengan
backtest_storage.KlineStore yang tetap sekali pakai dan tetap dihapus setiap
job selesai; cache ini hanya sumber data untuk mengisi store itu.

Kesegaran data (kebijakan KETAT)
--------------------------------
Candle paling akhir bisa berubah: candle yang belum tertutup masih bergerak,
dan bursa sesekali merevisi data. Karena itu cakupan cache hanya DIPERCAYA
sampai ``now - BACKTEST_CACHE_FRESH_HOURS`` (bawaan 24 jam). Bagian setelah
batas itu SELALU diunduh ulang dan menimpa baris lama, berapa kali pun job
dijalankan. Yang dihemat adalah ekor panjang data historis yang memang sudah
beku.

Kenapa ada tabel cakupan terpisah
---------------------------------
Ketiadaan baris TIDAK sama dengan "belum pernah diunduh". Koin yang baru
listing, pasar yang dihentikan sementara, dan celah data bursa menghasilkan
periode yang memang kosong secara sah. Tanpa catatan cakupan, periode kosong
seperti itu akan diunduh ulang selamanya. Tabel ``coverage`` mencatat rentang
waktu yang SUDAH pernah diminta ke Binance dan selesai, terlepas dari ada
atau tidaknya candle di sana.

Referensi API
-------------
Dokumentasi resmi ``sqlite3`` (standard library, tanpa dependency baru)
dicek 2026-09-26 di https://docs.python.org/3/library/sqlite3.html :
``connect(..., check_same_thread=False)`` memindahkan tanggung jawab
serialisasi ke pemanggil (di sini: satu RLock per objek cache), ``PRAGMA
busy_timeout`` menahan penulis kedua alih-alih langsung melempar
``database is locked``, dan ``executemany()`` dipakai untuk insert massal.
Semua nilai masuk lewat placeholder ``?``; tidak ada SQL yang dirakit dari
f-string atau concatenation nilai runtime.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Optional, Sequence

from strategy import Kline

logger = logging.getLogger(__name__)


# Versi skema cache. Kalau bentuk tabel berubah di masa depan, angka ini
# dinaikkan dan isi cache lama dibuang otomatis, bukan dipakai setengah-
# setengah dengan skema baru.
CACHE_SCHEMA_VERSION = 1

DEFAULT_FRESH_HOURS = 24
DEFAULT_TTL_DAYS = 30

_INSERT_BATCH = 2_000

# Kolom candle, satu-satunya kontrak antara tabel dan NamedTuple Kline.
_KLINE_COLUMNS = "open_time, open, high, low, close, close_time, volume, quote_volume"
_KLINE_PLACEHOLDERS = "?, ?, ?, ?, ?, ?, ?, ?"

# Statemen SQL dirakit sekali dari konstanta di atas memakai penggabungan
# string biasa, bukan f-string di dalam execute(), supaya mudah dibuktikan
# bahwa tidak ada nilai runtime yang pernah menjadi bagian teks SQL.
_SQL_INSERT_KLINE = ("INSERT OR REPLACE INTO cached_klines (symbol, interval, "
                     + _KLINE_COLUMNS + ") VALUES (?, ?, " + _KLINE_PLACEHOLDERS + ")")
_SQL_SELECT_KLINE = ("SELECT " + _KLINE_COLUMNS + " FROM cached_klines "
                     "WHERE symbol = ? AND interval = ? "
                     "AND open_time >= ? AND open_time <= ? ORDER BY open_time")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cached_klines (
    symbol       TEXT    NOT NULL,
    interval     TEXT    NOT NULL,
    open_time    INTEGER NOT NULL,
    open         REAL    NOT NULL,
    high         REAL    NOT NULL,
    low          REAL    NOT NULL,
    close        REAL    NOT NULL,
    close_time   INTEGER NOT NULL,
    volume       REAL    NOT NULL DEFAULT 0,
    quote_volume REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, interval, open_time)
);

CREATE TABLE IF NOT EXISTS coverage (
    symbol     TEXT    NOT NULL,
    interval   TEXT    NOT NULL,
    start_ms   INTEGER NOT NULL,
    end_ms     INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL,
    PRIMARY KEY (symbol, interval, start_ms)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class CacheError(RuntimeError):
    """Kesalahan cache candle backtest."""


# ======================================================================
# Aritmetika rentang waktu (murni, mudah diuji, tanpa I/O)
# ======================================================================

def merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Gabungkan rentang [start, end] inklusif yang tumpang tindih/berdempetan."""
    bersih = sorted((int(a), int(b)) for a, b in ranges if int(b) >= int(a))
    hasil: list[tuple[int, int]] = []
    for awal, akhir in bersih:
        if hasil and awal <= hasil[-1][1] + 1:
            if akhir > hasil[-1][1]:
                hasil[-1] = (hasil[-1][0], akhir)
        else:
            hasil.append((awal, akhir))
    return hasil


def subtract_ranges(awal: int, akhir: int,
                    dikurangi: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Bagian dari [awal, akhir] yang TIDAK tertutup oleh rentang ``dikurangi``."""
    awal, akhir = int(awal), int(akhir)
    if akhir < awal:
        return []
    sisa: list[tuple[int, int]] = []
    kursor = awal
    for potong_awal, potong_akhir in merge_ranges(dikurangi):
        if potong_akhir < kursor:
            continue
        if potong_awal > akhir:
            break
        if potong_awal > kursor:
            sisa.append((kursor, min(akhir, potong_awal - 1)))
        kursor = max(kursor, potong_akhir + 1)
        if kursor > akhir:
            return sisa
    if kursor <= akhir:
        sisa.append((kursor, akhir))
    return sisa


# ======================================================================
# Cache
# ======================================================================

class KlineCache:
    """File SQLite permanen berisi candle yang pernah diunduh backtest.

    Dipakai HANYA oleh jalur backtest. Bot live, paper engine, dan scanner
    tidak menyentuhnya sama sekali, supaya keputusan trading sungguhan tidak
    pernah dibuat dari data yang mungkin sudah basi.

    Thread-safety: koneksi dibuka ``check_same_thread=False`` dengan seluruh
    akses diserialisasi RLock milik objek ini, plus ``busy_timeout`` untuk
    menahan proses lain yang kebetulan menulis file yang sama (dua job
    backtest paralel, atau dashboard dan skrip manual). Mode WAL dipilih
    supaya pembaca tidak terblokir penulis; ini KEBALIKAN dari store
    sementara yang memakai journal_mode=OFF, karena file ini permanen
    sehingga ketahanan datanya memang berarti.
    """

    def __init__(self, path: str, *, fresh_hours: float = DEFAULT_FRESH_HOURS,
                 ttl_days: float = DEFAULT_TTL_DAYS) -> None:
        self.path = str(path)
        self.fresh_ms = max(0, int(float(fresh_hours) * 3_600_000))
        self.ttl_ms = max(0, int(float(ttl_days) * 86_400_000))
        self._lock = threading.RLock()
        self._closed = False
        # Statistik untuk pelaporan dan tes (bukan untuk logika apa pun).
        self.rows_served = 0
        self.rows_downloaded = 0
        self.ranges_downloaded = 0

        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False,
                                     timeout=30.0)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()
            self._enforce_schema_version()

    # -- siklus hidup --------------------------------------------------
    def _enforce_schema_version(self) -> None:
        """Buang isi cache kalau versi skemanya berbeda dari versi modul."""
        cur = self._conn.execute("SELECT value FROM meta WHERE key = ?",
                                 ("schema_version",))
        row = cur.fetchone()
        versi = int(row[0]) if row and str(row[0]).isdigit() else None
        if versi == CACHE_SCHEMA_VERSION:
            return
        if versi is not None:
            logger.warning("Skema cache backtest berubah (%s -> %s). Isi cache "
                           "lama dibuang supaya tidak tercampur.",
                           versi, CACHE_SCHEMA_VERSION)
            self._conn.execute("DELETE FROM cached_klines")
            self._conn.execute("DELETE FROM coverage")
        self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                           ("schema_version", str(CACHE_SCHEMA_VERSION)))
        self._conn.commit()

    def __enter__(self) -> "KlineCache":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """Tutup koneksi. File SENGAJA tidak dihapus, itu inti cache ini."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error as exc:  # noqa: BLE001 - penutupan tidak boleh menggagalkan job
                logger.warning("Gagal menutup cache backtest: %s", exc)

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_open(self) -> None:
        if self._closed:
            raise CacheError("Cache backtest sudah ditutup.")

    # -- cakupan -------------------------------------------------------
    def coverage(self, symbol: str, interval: str) -> list[tuple[int, int]]:
        """Rentang waktu yang sudah pernah diunduh untuk simbol dan interval ini."""
        with self._lock:
            self._require_open()
            cur = self._conn.execute(
                "SELECT start_ms, end_ms FROM coverage "
                "WHERE symbol = ? AND interval = ? ORDER BY start_ms",
                (str(symbol), str(interval)))
            return [(int(a), int(b)) for a, b in cur.fetchall()]

    def trusted_until(self, now_ms: Optional[int] = None) -> int:
        """Batas waktu terakhir yang isi cache-nya masih boleh dipercaya."""
        sekarang = int(now_ms if now_ms is not None else time.time() * 1000)
        return sekarang - self.fresh_ms

    def missing_ranges(self, symbol: str, interval: str, start_ms: int,
                       end_ms: int, now_ms: Optional[int] = None,
                       ) -> list[tuple[int, int]]:
        """Bagian [start_ms, end_ms] yang masih harus diunduh dari Binance.

        Cakupan yang tercatat hanya dihitung sampai :meth:`trusted_until`,
        sehingga jendela segar (bawaan 24 jam terakhir) SELALU ikut diunduh
        ulang walaupun datanya sudah ada di cache.
        """
        batas = self.trusted_until(now_ms)
        dipercaya = [(a, min(b, batas)) for a, b in self.coverage(symbol, interval)
                     if a <= batas]
        return subtract_ranges(start_ms, end_ms, dipercaya)

    def _record_coverage(self, symbol: str, interval: str, start_ms: int,
                         end_ms: int) -> None:
        """Catat satu rentang selesai unduh, digabung dengan cakupan lama."""
        sym, itv = str(symbol), str(interval)
        gabungan = merge_ranges(self.coverage(sym, itv) + [(int(start_ms), int(end_ms))])
        sekarang = int(time.time() * 1000)
        self._conn.execute(
            "DELETE FROM coverage WHERE symbol = ? AND interval = ?", (sym, itv))
        self._conn.executemany(
            "INSERT OR REPLACE INTO coverage (symbol, interval, start_ms, end_ms, "
            "updated_ms) VALUES (?, ?, ?, ?, ?)",
            [(sym, itv, a, b, sekarang) for a, b in gabungan])

    # -- tulis & baca --------------------------------------------------
    def put(self, symbol: str, interval: str, klines: Sequence[Kline],
            range_start_ms: int, range_end_ms: int) -> int:
        """Simpan hasil unduhan satu rentang, lalu catat cakupannya.

        Cakupan dicatat walaupun ``klines`` kosong. Rentang yang memang tidak
        punya candle (koin belum listing, pasar dihentikan) tetap dianggap
        selesai supaya tidak diminta ulang ke Binance setiap job.
        """
        sym, itv = str(symbol), str(interval)
        rows = [(sym, itv, int(k.open_time), float(k.open), float(k.high),
                 float(k.low), float(k.close), int(k.close_time),
                 float(k.volume), float(k.quote_volume)) for k in klines]
        with self._lock:
            self._require_open()
            for mulai in range(0, len(rows), _INSERT_BATCH):
                self._conn.executemany(_SQL_INSERT_KLINE,
                                       rows[mulai:mulai + _INSERT_BATCH])
            self._record_coverage(sym, itv, range_start_ms, range_end_ms)
            self._conn.commit()
        self.rows_downloaded += len(rows)
        self.ranges_downloaded += 1
        return len(rows)

    def read(self, symbol: str, interval: str, start_ms: int,
             end_ms: int) -> list[Kline]:
        """Candle tersimpan untuk rentang ini, urut kronologis."""
        with self._lock:
            self._require_open()
            cur = self._conn.execute(_SQL_SELECT_KLINE,
                                     (str(symbol), str(interval),
                                      int(start_ms), int(end_ms)))
            rows = cur.fetchall()
        hasil = [Kline(open_time=int(r[0]), open=float(r[1]), high=float(r[2]),
                       low=float(r[3]), close=float(r[4]), close_time=int(r[5]),
                       volume=float(r[6]), quote_volume=float(r[7])) for r in rows]
        self.rows_served += len(hasil)
        return hasil

    # -- perawatan -----------------------------------------------------
    def prune(self, now_ms: Optional[int] = None) -> int:
        """Buang data simbol yang sudah lama tidak dipakai. Return jumlah pasangan.

        TTL nol berarti cache tidak pernah dipangkas otomatis. Pemangkasan
        memakai waktu PEMAKAIAN terakhir (kolom updated_ms pada cakupan),
        bukan umur candle, supaya data lama yang masih sering dipakai untuk
        backtest periode panjang tidak ikut terbuang.
        """
        if self.ttl_ms <= 0:
            return 0
        sekarang = int(now_ms if now_ms is not None else time.time() * 1000)
        batas = sekarang - self.ttl_ms
        with self._lock:
            self._require_open()
            cur = self._conn.execute(
                "SELECT symbol, interval FROM coverage "
                "GROUP BY symbol, interval HAVING MAX(updated_ms) < ?", (batas,))
            basi = [(str(a), str(b)) for a, b in cur.fetchall()]
            for sym, itv in basi:
                self._conn.execute(
                    "DELETE FROM cached_klines WHERE symbol = ? AND interval = ?",
                    (sym, itv))
                self._conn.execute(
                    "DELETE FROM coverage WHERE symbol = ? AND interval = ?",
                    (sym, itv))
            if basi:
                self._conn.commit()
        if basi:
            logger.info("Cache backtest dipangkas: %d pasangan simbol/interval "
                        "tidak dipakai lebih dari %.0f hari.",
                        len(basi), self.ttl_ms / 86_400_000)
        return len(basi)

    def clear(self) -> None:
        """Kosongkan seluruh cache (tombol 'paksa unduh ulang' yang manual)."""
        with self._lock:
            self._require_open()
            self._conn.execute("DELETE FROM cached_klines")
            self._conn.execute("DELETE FROM coverage")
            self._conn.commit()

    def file_size_bytes(self) -> int:
        total = 0
        for akhiran in ("", "-wal", "-shm"):
            try:
                total += Path(self.path + akhiran).stat().st_size
            except OSError:
                pass
        return total

    def stats(self) -> dict:
        """Ringkasan isi cache untuk logging dan tes."""
        with self._lock:
            self._require_open()
            baris = self._conn.execute("SELECT COUNT(*) FROM cached_klines").fetchone()
            pasangan = self._conn.execute(
                "SELECT COUNT(DISTINCT symbol || '|' || interval) FROM coverage"
            ).fetchone()
        return {
            "rows": int(baris[0]) if baris else 0,
            "pairs": int(pasangan[0]) if pasangan else 0,
            "file_bytes": self.file_size_bytes(),
            "rows_downloaded": self.rows_downloaded,
            "rows_served": self.rows_served,
            "ranges_downloaded": self.ranges_downloaded,
        }


# ==== RINGKASAN AUDIT (backtest_cache.py) =============================
# Lingkup: modul BARU. Pemanggilnya hanya portfolio_backtest.open_kline_cache()
#   dan fetch_universe_klines(); dashboard.py memakainya lewat dua fungsi itu.
#   Grep seluruh repo untuk "backtest_cache" (2026-09-26): tidak ada modul
#   live (pump_scanner_bot.py, live_client.py, paper_engine.py, market_*.py)
#   yang menyentuhnya, dan itu disengaja -- keputusan trading sungguhan tidak
#   boleh dibuat dari candle yang mungkin sudah basi.
# Sintaks/tipe: type hints lengkap; docstring Bahasa Indonesia; fungsi
#   aritmetika rentang (merge_ranges/subtract_ranges) murni tanpa I/O
#   sehingga bisa diuji langsung. Tidak ada print()/TODO/kode sisa.
# Keamanan SQL: seluruh nilai lewat placeholder "?"; teks SQL hanya dirakit
#   sekali di konstanta modul dari daftar kolom literal. Tidak ada f-string
#   atau .format() di dalam execute()/executemany(). Dijaga otomatis oleh
#   tests/test_backtest_cache.py.
# Keamanan berkas: path berasal dari config (BACKTEST_CACHE_FILE) yang
#   di-absolutkan config._runtime_path() ke folder repo; direktori induk
#   dibuat dengan mkdir(parents=True) sehingga tidak menulis ke lokasi acak.
#   File cache masuk .gitignore agar tidak pernah ter-commit.
# Race condition: dua job backtest paralel boleh memakai file yang sama.
#   journal_mode=WAL membuat pembaca tidak terblokir penulis, busy_timeout
#   30 detik menahan penulis kedua alih-alih melempar "database is locked",
#   dan seluruh akses koneksi milik satu objek diserialisasi RLock. Kasus
#   terburuk dua job mengunduh simbol yang sama bersamaan hanyalah pekerjaan
#   ganda, bukan data rusak, karena penulisan memakai INSERT OR REPLACE dan
#   cakupan ditulis ulang sebagai hasil merge.
# Kesegaran data: cakupan hanya dipercaya sampai now - fresh_ms (bawaan 24
#   jam) sehingga candle yang belum tertutup dan revisi bursa selalu tertimpa
#   unduhan baru. Versi skema disimpan di tabel meta; kalau formatnya berubah
#   isi cache lama dibuang, bukan dipakai setengah-setengah.
# Konsistensi hasil: cache hanya mengubah ASAL baris candle, bukan isinya.
#   Paritas hasil simulasi dengan dan tanpa cache diuji di
#   tests/test_backtest_cache.py::test_hasil_simulasi_sama_dengan_dan_tanpa_cache.
# Catatan terbuka: pemangkasan memakai DELETE tanpa VACUUM, jadi ukuran file
#   tidak langsung menyusut setelah prune (ruangnya dipakai ulang oleh data
#   berikutnya). VACUUM sengaja dihindari karena mengunci file lama untuk
#   cache berukuran ratusan MB.
# =======================================================================
