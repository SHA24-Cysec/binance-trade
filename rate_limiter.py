"""Shared Binance REQUEST_WEIGHT limiter.

Binance menghitung REQUEST_WEIGHT per IP, bukan per instance Python. Modul ini
memakai state bersama dan lock file ketika diberi path, sehingga bot, dashboard,
dan proses backtest pada host yang sama tidak menganggap kuota IP sebagai kuota
masing-masing.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Iterator

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover, exercised on Windows
    fcntl = None

try:
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover, exercised on POSIX
    msvcrt = None


class RateLimitBlockedError(RuntimeError):
    """Dilempar ketika proses masih berada dalam jeda 429/418 bersama."""

    def __init__(self, retry_after: float):
        self.retry_after = max(1.0, float(retry_after))
        super().__init__(f"shared Binance rate limiter blocked for {self.retry_after:.1f}s")


class SharedRequestWeightLimiter:
    """Reservasi bobot request secara atomic di dalam satu host.

    ``state_file=None`` membuat limiter lokal per instance, cocok untuk client
    utility dan unit test. Production clients diberi file path dari config agar
    lintas proses pada host yang sama berbagi window dan status block.
    """

    _memory_lock = threading.RLock()

    def __init__(self, state_file: str | None = None, limit: int = 6000,
                 safety_margin: int = 100, window_seconds: int = 60) -> None:
        self.limit = max(1, int(limit))
        self.safety_margin = max(0, int(safety_margin))
        self.window_seconds = max(1, int(window_seconds))
        self.state_file = (
            os.path.abspath(os.path.expanduser(state_file))
            if state_file else None
        )
        # Client test/utility yang tidak diberi file tidak boleh mewarisi
        # status block client lain. Production selalu memberi state_file dari
        # config agar koordinasi lintas proses tetap aktif.
        self._memory_state: dict | None = None

    @staticmethod
    def _window_start(now: float, window_seconds: int) -> float:
        return float(int(now // window_seconds) * window_seconds)

    def _fresh_state(self, now: float) -> dict:
        return {
            "window_start": self._window_start(now, self.window_seconds),
            "used": 0,
            "blocked_until": 0.0,
        }

    def _normalise(self, state: dict | None, now: float) -> dict:
        if not isinstance(state, dict):
            return self._fresh_state(now)
        try:
            window_start = float(state.get("window_start", 0.0))
            used = max(0, int(state.get("used", 0)))
            blocked_until = max(0.0, float(state.get("blocked_until", 0.0)))
        except (TypeError, ValueError):
            return self._fresh_state(now)
        if now >= window_start + self.window_seconds:
            return {
                "window_start": self._window_start(now, self.window_seconds),
                "used": 0,
                "blocked_until": blocked_until,
            }
        return {
            "window_start": window_start,
            "used": used,
            "blocked_until": blocked_until,
        }

    def _read_file(self) -> dict | None:
        if not self.state_file:
            return None
        try:
            with open(self.state_file, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return None

    def _write_file(self, state: dict) -> None:
        if not self.state_file:
            return
        parent = os.path.dirname(self.state_file) or "."
        os.makedirs(parent, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".rate-limit-", dir=parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_file)
            try:
                os.chmod(self.state_file, 0o600)
            except OSError:
                pass
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    @contextmanager
    def _locked_state(self) -> Iterator[dict]:
        now = time.time()
        if not self.state_file:
            with self._memory_lock:
                state = self._normalise(self._memory_state, now)
                yield state
                self._memory_state = state
            return

        lock_path = self.state_file + ".lock"
        parent = os.path.dirname(lock_path) or "."
        os.makedirs(parent, mode=0o700, exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as lock_fh:
            if fcntl is not None:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:  # pragma: no cover
                lock_fh.seek(0)
                lock_fh.write("0")
                lock_fh.flush()
                msvcrt.locking(lock_fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                state = self._normalise(self._read_file(), time.time())
                yield state
                self._write_file(state)
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover
                    lock_fh.seek(0)
                    msvcrt.locking(lock_fh.fileno(), msvcrt.LK_UNLCK, 1)

    def reserve(self, weight: int, ignore_block: bool = False) -> None:
        """Tunggu sampai bobot aman untuk window saat ini, lalu reservasi."""
        requested = max(1, int(weight))
        effective_limit = max(1, self.limit - self.safety_margin)
        while True:
            wait = 0.0
            with self._locked_state() as state:
                now = time.time()
                if state["blocked_until"] > now and not ignore_block:
                    raise RateLimitBlockedError(state["blocked_until"] - now)
                if state["used"] + requested <= effective_limit or state["used"] == 0:
                    state["used"] += requested
                    return
                wait = max(0.05, state["window_start"] + self.window_seconds - now)
            time.sleep(wait)

    def observe_server_weight(self, used_weight: int) -> None:
        """Sinkronkan ledger dengan header absolut Binance."""
        try:
            used = max(0, int(used_weight))
        except (TypeError, ValueError):
            return
        with self._locked_state() as state:
            state["used"] = max(int(state.get("used", 0)), used)

    def record_retry_after_server_wait(self, weight: int) -> None:
        """Catat retry yang sudah menunggu Retry-After tanpa sleep kedua.

        Respons 429 dapat mengirim header weight pada atau di atas plafon.
        Retry internal tetap mengikuti kontrak server, tetapi tidak boleh
        kehilangan bobot dari ledger atau terjebak menunggu window lokal
        kedua. Request baru setelahnya tetap akan ditahan oleh ``reserve``.
        """
        requested = max(1, int(weight))
        with self._locked_state() as state:
            state["used"] = int(state.get("used", 0)) + requested

    def block(self, seconds: float) -> None:
        with self._locked_state() as state:
            state["blocked_until"] = max(
                float(state.get("blocked_until", 0.0)),
                time.time() + max(1.0, float(seconds)),
            )

    def headroom(self) -> float:
        with self._locked_state() as state:
            now = time.time()
            if state["blocked_until"] > now:
                return 0.0
            return max(0.0, 1.0 - state["used"] / float(self.limit))
