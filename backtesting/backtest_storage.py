#!/usr/bin/env python3
"""
Penyimpanan candle backtest portofolio berbasis SQLite temporary.
=================================================================

Kenapa modul ini ada
--------------------
Backtest portofolio (portfolio_backtest.py) menjalankan simulasi di atas
SELURUH semesta simbol USDT sekaligus. Versi lama menyimpan hasil unduhan
sebagai ``dict[str, list[Kline]]`` penuh di RAM: untuk 150 simbol x 8.640
candle 5 menit (30 hari) itu berarti sekitar 1,3 JUTA objek Kline hidup
bersamaan, ditambah satu dict index dan satu dict statistik per candle.
Pemakaian RAM itulah yang membuat job backtest besar mencekik VPS kecil.

Modul ini memindahkan candle ke satu file SQLite temporary per job. Yang
tetap tinggal di RAM hanyalah:

  1. deret ringkas per simbol (:class:`SymbolSeries`) berisi ``array``
     bertipe int64/float64: open_time, close_time, pct24h, vol24h, dan
     penanda kesiapan statistik. Sekitar 41 byte per bar per simbol, bukan
     ~600 byte seperti kombinasi Kline + dict index + dict statistik.
  2. cache LRU kecil berisi list[Kline] PENUH untuk beberapa simbol yang
     sedang aktif (simbol yang dipegang, dan simbol yang sedang masuk papan
     kandidat top-N). Ukurannya bisa diatur; lihat ``cache_size``.

Simbol yang tidak sedang aktif dibaca ulang dari SQLite saat dibutuhkan.

Referensi API yang dipakai
--------------------------
Dokumentasi resmi ``sqlite3`` (standard library, TANPA dependency baru)
dicek 2026-09-26 di https://docs.python.org/3/library/sqlite3.html :
  - ``sqlite3.connect(path, check_same_thread=False)`` mematikan penjagaan
    "satu koneksi satu thread" bawaan modul; dokumentasi menegaskan bahwa
    setelah itu serialisasi antar-thread menjadi tanggung jawab pemanggil,
    maka di sini SEMUA akses koneksi dibungkus ``threading.RLock``.
  - ``Cursor.executemany()`` untuk insert massal dengan placeholder ``?``.
  - ``Cursor.fetchmany()`` untuk membaca bertahap, supaya satu simbol besar
    pun tidak pernah dimaterialisasi dua kali di RAM.
  - Kontrol transaksi default masih ``LEGACY_TRANSACTION_CONTROL`` (atribut
    ``Connection.autocommit`` baru sejak Python 3.12 dan sengaja TIDAK
    dipakai supaya modul ini tetap jalan di Python 3.10 seperti sisa repo),
    jadi ``commit()`` dipanggil eksplisit setelah fase tulis.

Semua query memakai placeholder ``?``. Tidak ada satu pun SQL yang dirakit
dari f-string, ``.format()``, atau concatenation variabel.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
import threading
from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from pathlib import Path
from typing import Iterator, Mapping, Optional, Sequence

from strategy.indicators import Kline

logger = logging.getLogger(__name__)


DEFAULT_SYMBOL_CACHE_SIZE = 8

DEFAULT_DAILY_CACHE_SIZE = 4

_INSERT_BATCH = 2_000



_KLINE_COLUMNS = "open_time, open, high, low, close, close_time, volume, quote_volume"
_KLINE_PLACEHOLDERS = "?, ?, ?, ?, ?, ?, ?, ?"

_SQL_INSERT_KLINES = ("INSERT OR REPLACE INTO klines (symbol, " + _KLINE_COLUMNS
                      + ") VALUES (?, " + _KLINE_PLACEHOLDERS + ")")
_SQL_INSERT_DAILY = ("INSERT OR REPLACE INTO daily_klines (symbol, " + _KLINE_COLUMNS
                     + ") VALUES (?, " + _KLINE_PLACEHOLDERS + ")")
_SQL_SELECT_KLINES = ("SELECT " + _KLINE_COLUMNS
                      + " FROM klines WHERE symbol = ? ORDER BY open_time")
_SQL_SELECT_DAILY = ("SELECT " + _KLINE_COLUMNS
                     + " FROM daily_klines WHERE symbol = ? ORDER BY open_time")


def _row_to_kline(row: Sequence) -> Kline:
    return Kline(
        open_time=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        close_time=int(row[5]),
        volume=float(row[6]),
        quote_volume=float(row[7]),
    )


def _kline_to_row(symbol: str, candle: Kline) -> tuple:
    return (
        str(symbol),
        int(candle.open_time),
        float(candle.open),
        float(candle.high),
        float(candle.low),
        float(candle.close),
        int(candle.close_time),
        float(candle.volume),
        float(candle.quote_volume),
    )


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS klines (
    symbol      TEXT    NOT NULL,
    open_time   INTEGER NOT NULL,
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    close_time  INTEGER NOT NULL,
    volume      REAL    NOT NULL DEFAULT 0,
    quote_volume REAL   NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, open_time)
);

CREATE TABLE IF NOT EXISTS daily_klines (
    symbol      TEXT    NOT NULL,
    open_time   INTEGER NOT NULL,
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    close_time  INTEGER NOT NULL,
    volume      REAL    NOT NULL DEFAULT 0,
    quote_volume REAL   NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, open_time)
);

CREATE TABLE IF NOT EXISTS symbols (
    symbol TEXT PRIMARY KEY,
    seq    INTEGER NOT NULL,
    bars   INTEGER NOT NULL DEFAULT 0
);
"""


class StorageError(RuntimeError):
    pass



class SymbolSeries:

    __slots__ = ("symbol", "_open_times", "_close_times", "_pct24h",
                 "_vol24h", "_ready", "_daily_close_times")

    def __init__(self, symbol: str, open_times: array, close_times: array,
                 pct24h: array, vol24h: array, ready: bytearray,
                 daily_close_times: array) -> None:
        self.symbol = str(symbol)
        self._open_times = open_times
        self._close_times = close_times
        self._pct24h = pct24h
        self._vol24h = vol24h
        self._ready = ready
        self._daily_close_times = daily_close_times

    @classmethod
    def build(cls, symbol: str, klines: Sequence[Kline],
              stats: Sequence[Optional[dict]],
              daily_close_times: Sequence[int] = ()) -> "SymbolSeries":
        n = len(klines)
        if len(stats) != n:
            raise StorageError(
                f"Panjang statistik ({len(stats)}) tidak sama dengan jumlah "
                f"candle ({n}) untuk {symbol}."
            )
        open_times = array("q", [int(k.open_time) for k in klines])
        close_times = array("q", [int(k.close_time) for k in klines])
        pct = array("d", [0.0] * n)
        vol = array("d", [0.0] * n)
        ready = bytearray(n)
        for i, st in enumerate(stats):
            if st is None:
                continue
            pct[i] = float(st["pct24h"])
            vol[i] = float(st["vol24h"])
            ready[i] = 1
        return cls(symbol, open_times, close_times, pct, vol, ready,
                   array("q", [int(t) for t in daily_close_times]))

    def __len__(self) -> int:
        return len(self._open_times)

    def index_at(self, open_time: int) -> Optional[int]:
        times = self._open_times
        pos = bisect_left(times, int(open_time))
        if pos < len(times) and times[pos] == open_time:
            return pos
        return None

    def ready(self, index: int) -> bool:
        return bool(self._ready[index])

    def pct24h(self, index: int) -> float:
        return self._pct24h[index]

    def vol24h(self, index: int) -> float:
        return self._vol24h[index]

    def open_time(self, index: int) -> int:
        return self._open_times[index]

    def close_time(self, index: int) -> int:
        return self._close_times[index]

    def open_times(self) -> array:
        return self._open_times

    def closed_daily_count(self, reference_ms: int) -> int:
        return bisect_right(self._daily_close_times, int(reference_ms))

    def board_arrays(self) -> tuple:
        return (self._open_times, self._close_times, self._pct24h,
                self._vol24h, self._ready, self._daily_close_times)

    def memory_bytes(self) -> int:
        return (self._open_times.buffer_info()[1] * self._open_times.itemsize
                + self._close_times.buffer_info()[1] * self._close_times.itemsize
                + self._pct24h.buffer_info()[1] * self._pct24h.itemsize
                + self._vol24h.buffer_info()[1] * self._vol24h.itemsize
                + len(self._ready)
                + self._daily_close_times.buffer_info()[1] * self._daily_close_times.itemsize)



class KlineStore:

    def __init__(self, db_path: str, *, temp_dir: Optional[str] = None,
                 cache_size: int = DEFAULT_SYMBOL_CACHE_SIZE,
                 daily_cache_size: int = DEFAULT_DAILY_CACHE_SIZE) -> None:
        self.db_path = str(db_path)
        self._temp_dir = str(temp_dir) if temp_dir else None
        self._cache_size = max(1, int(cache_size))
        self._daily_cache_size = max(1, int(daily_cache_size))
        self._lock = threading.RLock()
        self._cache: "OrderedDict[str, list[Kline]]" = OrderedDict()
        self._daily_cache: "OrderedDict[str, list[Kline]]" = OrderedDict()
        self._closed = False
        self.cache_hits = 0
        self.cache_misses = 0

        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=OFF")
            self._conn.execute("PRAGMA synchronous=OFF")
            self._conn.execute("PRAGMA temp_store=MEMORY")
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()

    @classmethod
    def create_temp(cls, *, prefix: str = "binance_backtest_",
                    cache_size: int = DEFAULT_SYMBOL_CACHE_SIZE,
                    daily_cache_size: int = DEFAULT_DAILY_CACHE_SIZE) -> "KlineStore":
        temp_dir = tempfile.mkdtemp(prefix=prefix)
        db_path = str(Path(temp_dir) / "klines.sqlite3")
        return cls(db_path, temp_dir=temp_dir, cache_size=cache_size,
                   daily_cache_size=daily_cache_size)

    @classmethod
    def from_klines(cls, data: Mapping[str, Sequence[Kline]],
                    daily_klines: Optional[Mapping[str, Sequence[Kline]]] = None,
                    *, cache_size: int = DEFAULT_SYMBOL_CACHE_SIZE) -> "KlineStore":
        store = cls.create_temp(cache_size=cache_size)
        try:
            for symbol, klines in data.items():
                store.write_symbol(symbol, klines)
            for symbol, klines in (daily_klines or {}).items():
                store.write_daily(symbol, klines)
            store.finish_writing()
        except Exception:
            store.cleanup()
            raise
        return store

    def __enter__(self) -> "KlineStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cleanup()

    def write_symbol(self, symbol: str, klines: Sequence[Kline]) -> int:
        sym = str(symbol)
        rows = [_kline_to_row(sym, k) for k in klines]
        if not rows:
            return 0
        with self._lock:
            self._require_open()
            cur = self._conn.cursor()
            for start in range(0, len(rows), _INSERT_BATCH):
                cur.executemany(_SQL_INSERT_KLINES,
                                rows[start:start + _INSERT_BATCH])
            cur.execute(
                "INSERT INTO symbols (symbol, seq, bars) "
                "VALUES (?, (SELECT COALESCE(MAX(seq), -1) + 1 FROM symbols), ?) "
                "ON CONFLICT(symbol) DO UPDATE SET bars = excluded.bars",
                (sym, len(rows)),
            )
            self._conn.commit()
            self._cache.pop(sym, None)
        return len(rows)

    def write_daily(self, symbol: str, klines: Sequence[Kline]) -> int:
        sym = str(symbol)
        rows = [_kline_to_row(sym, k) for k in klines]
        if not rows:
            return 0
        with self._lock:
            self._require_open()
            cur = self._conn.cursor()
            cur.executemany(_SQL_INSERT_DAILY, rows)
            self._conn.commit()
            self._daily_cache.pop(sym, None)
        return len(rows)

    def finish_writing(self) -> None:
        with self._lock:
            self._require_open()
            self._conn.commit()
            try:
                self._conn.execute("ANALYZE")
                self._conn.commit()
            except sqlite3.Error as exc:  # noqa: BLE001
                logger.debug("ANALYZE store backtest dilewati: %s", exc)

    def symbols(self) -> list[str]:
        with self._lock:
            self._require_open()
            cur = self._conn.execute("SELECT symbol FROM symbols ORDER BY seq")
            return [str(row[0]) for row in cur.fetchall()]

    def has_symbol(self, symbol: str) -> bool:
        with self._lock:
            self._require_open()
            cur = self._conn.execute(
                "SELECT 1 FROM symbols WHERE symbol = ? LIMIT 1", (str(symbol),))
            return cur.fetchone() is not None

    def bar_count(self, symbol: str) -> int:
        with self._lock:
            self._require_open()
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM klines WHERE symbol = ?", (str(symbol),))
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def daily_count(self, symbol: str) -> int:
        with self._lock:
            self._require_open()
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM daily_klines WHERE symbol = ?", (str(symbol),))
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def iter_klines(self, symbol: str, batch_size: int = 1000) -> Iterator[Kline]:
        sym = str(symbol)
        size = max(1, int(batch_size))
        with self._lock:
            self._require_open()
            cur = self._conn.execute(_SQL_SELECT_KLINES, (sym,))
        while True:
            with self._lock:
                self._require_open()
                rows = cur.fetchmany(size)
            if not rows:
                return
            for row in rows:
                yield _row_to_kline(row)

    def load_klines(self, symbol: str) -> list[Kline]:
        sym = str(symbol)
        with self._lock:
            self._require_open()
            cur = self._conn.execute(_SQL_SELECT_KLINES, (sym,))
            rows = cur.fetchall()
        return [_row_to_kline(r) for r in rows]

    def klines(self, symbol: str) -> list[Kline]:
        sym = str(symbol)
        with self._lock:
            cached = self._cache.get(sym)
            if cached is not None:
                self._cache.move_to_end(sym)
                self.cache_hits += 1
                return cached
        data = self.load_klines(sym)
        with self._lock:
            self.cache_misses += 1
            self._cache[sym] = data
            self._cache.move_to_end(sym)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return data

    def daily_klines(self, symbol: str) -> list[Kline]:
        sym = str(symbol)
        with self._lock:
            cached = self._daily_cache.get(sym)
            if cached is not None:
                self._daily_cache.move_to_end(sym)
                return cached
            self._require_open()
            cur = self._conn.execute(_SQL_SELECT_DAILY, (sym,))
            rows = cur.fetchall()
        data = [_row_to_kline(r) for r in rows]
        with self._lock:
            self._daily_cache[sym] = data
            self._daily_cache.move_to_end(sym)
            while len(self._daily_cache) > self._daily_cache_size:
                self._daily_cache.popitem(last=False)
        return data

    def daily_close_times(self, symbol: str) -> list[int]:
        with self._lock:
            self._require_open()
            cur = self._conn.execute(
                "SELECT close_time FROM daily_klines WHERE symbol = ? "
                "ORDER BY open_time", (str(symbol),))
            return [int(r[0]) for r in cur.fetchall()]

    def set_cache_size(self, size: int) -> None:
        with self._lock:
            self._cache_size = max(1, int(size))
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    @property
    def cache_size(self) -> int:
        return self._cache_size

    def cached_symbols(self) -> list[str]:
        with self._lock:
            return list(self._cache.keys())

    def file_size_bytes(self) -> int:
        try:
            return Path(self.db_path).stat().st_size
        except OSError:
            return 0

    def close(self) -> None:
        with self._lock:
            self._cache.clear()
            self._daily_cache.clear()
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error as exc:  # noqa: BLE001
                logger.warning("Gagal menutup koneksi store backtest: %s", exc)

    def cleanup(self) -> None:
        self.close()
        try:
            if self._temp_dir:
                shutil.rmtree(self._temp_dir, ignore_errors=True)
            else:
                Path(self.db_path).unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001
            logger.warning("Gagal menghapus file backtest temporary %s: %s",
                           self.db_path, exc)

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_open(self) -> None:
        if self._closed:
            raise StorageError("Store backtest sudah ditutup.")
