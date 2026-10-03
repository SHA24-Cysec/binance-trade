from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
import threading
from array import array
from bisect import bisect_left
from collections import OrderedDict
from pathlib import Path
from typing import Mapping, Optional, Sequence

from strategy.indicators import Kline

logger = logging.getLogger(__name__)


DEFAULT_SYMBOL_CACHE_SIZE = 8

_INSERT_BATCH = 2_000


_KLINE_COLUMNS = "open_time, open, high, low, close, close_time, volume, quote_volume"
_KLINE_PLACEHOLDERS = "?, ?, ?, ?, ?, ?, ?, ?"

_SQL_INSERT_KLINES = ("INSERT OR REPLACE INTO klines (symbol, " + _KLINE_COLUMNS
                      + ") VALUES (?, " + _KLINE_PLACEHOLDERS + ")")
_SQL_SELECT_KLINES = ("SELECT " + _KLINE_COLUMNS
                      + " FROM klines WHERE symbol = ? ORDER BY open_time")


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
                 "_vol24h", "_ready", "_open", "_high", "_low", "_close",
                 "_volume", "_quote_volume")

    def __init__(self, symbol: str, open_times: array, close_times: array,
                 pct24h: array, vol24h: array, ready: bytearray,
                 open_prices: Optional[array] = None,
                 high_prices: Optional[array] = None,
                 low_prices: Optional[array] = None,
                 close_prices: Optional[array] = None,
                 volumes: Optional[array] = None,
                 quote_volumes: Optional[array] = None) -> None:
        self.symbol = str(symbol)
        self._open_times = open_times
        self._close_times = close_times
        self._pct24h = pct24h
        self._vol24h = vol24h
        self._ready = ready
        self._open = open_prices
        self._high = high_prices
        self._low = low_prices
        self._close = close_prices
        self._volume = volumes
        self._quote_volume = quote_volumes

    @classmethod
    def build(cls, symbol: str, klines: Sequence[Kline],
              stats: Sequence[Optional[dict]],
              include_ohlcv: bool = False) -> "SymbolSeries":
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
        raw_arrays = ()
        if include_ohlcv:
            raw_arrays = (
                array("d", [float(k.open) for k in klines]),
                array("d", [float(k.high) for k in klines]),
                array("d", [float(k.low) for k in klines]),
                array("d", [float(k.close) for k in klines]),
                array("d", [float(k.volume) for k in klines]),
                array("d", [float(k.quote_volume) for k in klines]),
            )
        return cls(symbol, open_times, close_times, pct, vol, ready, *raw_arrays)

    @property
    def has_ohlcv(self) -> bool:
        return all(values is not None for values in (
            self._open, self._high, self._low, self._close,
            self._volume, self._quote_volume,
        ))

    def kline_at(self, index: int) -> Kline:
        if not self.has_ohlcv:
            raise StorageError(f"OHLCV untuk {self.symbol} tidak dimuat dalam SymbolSeries.")
        i = int(index)
        return Kline(
            open_time=int(self._open_times[i]),
            open=self._open[i],
            high=self._high[i],
            low=self._low[i],
            close=self._close[i],
            close_time=int(self._close_times[i]),
            volume=self._volume[i],
            quote_volume=self._quote_volume[i],
        )

    def klines_slice(self, start: int, end: int) -> list[Kline]:
        if not self.has_ohlcv:
            raise StorageError(f"OHLCV untuk {self.symbol} tidak dimuat dalam SymbolSeries.")
        lo = max(0, int(start))
        hi = min(len(self), max(lo, int(end)))
        return [self.kline_at(i) for i in range(lo, hi)]

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

    def board_arrays(self) -> tuple:
        return (self._open_times, self._close_times, self._pct24h,
                self._vol24h, self._ready)


class KlineStore:

    def __init__(self, db_path: str, *, temp_dir: Optional[str] = None,
                 cache_size: int = DEFAULT_SYMBOL_CACHE_SIZE) -> None:
        self.db_path = str(db_path)
        self._temp_dir = str(temp_dir) if temp_dir else None
        self._cache_size = max(1, int(cache_size))
        self._lock = threading.RLock()
        self._cache: "OrderedDict[str, list[Kline]]" = OrderedDict()
        self._closed = False

        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=OFF")
            self._conn.execute("PRAGMA synchronous=OFF")
            self._conn.execute("PRAGMA temp_store=MEMORY")
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()

    @classmethod
    def create_temp(cls, *, prefix: str = "binance_backtest_",
                    cache_size: int = DEFAULT_SYMBOL_CACHE_SIZE) -> "KlineStore":
        temp_dir = tempfile.mkdtemp(prefix=prefix)
        db_path = str(Path(temp_dir) / "klines.sqlite3")
        return cls(db_path, temp_dir=temp_dir, cache_size=cache_size)

    @classmethod
    def from_klines(cls, data: Mapping[str, Sequence[Kline]],
                    *, cache_size: int = DEFAULT_SYMBOL_CACHE_SIZE) -> "KlineStore":
        store = cls.create_temp(cache_size=cache_size)
        try:
            for symbol, klines in data.items():
                store.write_symbol(symbol, klines)
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

    def finish_writing(self) -> None:
        with self._lock:
            self._require_open()
            self._conn.commit()
            try:
                self._conn.execute("ANALYZE")
                self._conn.commit()
            except sqlite3.Error as exc:
                logger.debug("ANALYZE store backtest dilewati: %s", exc)

    def symbols(self) -> list[str]:
        with self._lock:
            self._require_open()
            cur = self._conn.execute("SELECT symbol FROM symbols ORDER BY seq")
            return [str(row[0]) for row in cur.fetchall()]

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
                return cached
        data = self.load_klines(sym)
        with self._lock:
            self._cache[sym] = data
            self._cache.move_to_end(sym)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return data

    def set_cache_size(self, size: int) -> None:
        with self._lock:
            self._cache_size = max(1, int(size))
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    @property
    def cache_size(self) -> int:
        return self._cache_size

    def file_size_bytes(self) -> int:
        try:
            return Path(self.db_path).stat().st_size
        except OSError:
            return 0

    def close(self) -> None:
        with self._lock:
            self._cache.clear()
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error as exc:
                logger.warning("Gagal menutup koneksi store backtest: %s", exc)

    def cleanup(self) -> None:
        self.close()
        try:
            if self._temp_dir:
                shutil.rmtree(self._temp_dir, ignore_errors=True)
            else:
                Path(self.db_path).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Gagal menghapus file backtest temporary %s: %s",
                           self.db_path, exc)

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_open(self) -> None:
        if self._closed:
            raise StorageError("Store backtest sudah ditutup.")
