from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger("rate_limiter")

from infrastructure.storage.atomic_io import (
    _acquire_lock,
    _open_lock_fd,
    _release_lock,
)


class RateLimitBlockedError(RuntimeError):

    def __init__(self, retry_after: float):
        self.retry_after = max(1.0, float(retry_after))
        super().__init__(f"shared Binance rate limiter blocked for {self.retry_after:.1f}s")


class SharedRequestWeightLimiter:

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
        if state is None:
            return self._fresh_state(now)
        if not isinstance(state, dict):
            raise RateLimitBlockedError(self.window_seconds)
        try:
            window_start = float(state["window_start"])
            used = max(0, int(state["used"]))
            blocked_until = max(0.0, float(state["blocked_until"]))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            logger.error("Ledger rate limit Binance rusak: %s", exc)
            raise RateLimitBlockedError(self.window_seconds) from exc
        if (
            not math.isfinite(window_start)
            or not math.isfinite(blocked_until)
            or used > 10**15
        ):
            logger.error("Ledger rate limit Binance memuat angka tidak valid.")
            raise RateLimitBlockedError(self.window_seconds)
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
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError) as exc:
            logger.error(
                "Ledger rate limit %s tidak dapat diverifikasi: %s. Request diblokir.",
                self.state_file, exc,
            )
            raise RateLimitBlockedError(self.window_seconds) from exc

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
        fd = _open_lock_fd(lock_path)
        try:
            _acquire_lock(fd)
            try:
                state = self._normalise(self._read_file(), time.time())
                yield state
                self._write_file(state)
            finally:
                _release_lock(fd)
        finally:
            os.close(fd)

    def reserve(self, weight: int, ignore_block: bool = False) -> None:
        requested = max(1, int(weight))
        effective_limit = max(1, self.limit - self.safety_margin)
        while True:
            wait = 0.0
            with self._locked_state() as state:
                now = time.time()
                if state["blocked_until"] > now and not ignore_block:
                    raise RateLimitBlockedError(state["blocked_until"] - now)
                if state["used"] + requested <= effective_limit or state["used"] == 0:
                    if state["used"] == 0 and requested > effective_limit:
                        logger.warning(
                            "Rate limiter meloloskan satu request berbobot %d "
                            "yang melebihi limit efektif %d pada jendela kosong.",
                            requested, effective_limit,
                        )
                    state["used"] += requested
                    return
                wait = max(0.05, state["window_start"] + self.window_seconds - now)
            time.sleep(wait)

    def observe_server_weight(self, used_weight: int) -> None:
        try:
            used = max(0, int(used_weight))
        except (TypeError, ValueError):
            return
        with self._locked_state() as state:
            state["used"] = max(int(state.get("used", 0)), used)

    def record_retry_after_server_wait(self, weight: int) -> None:
        requested = max(1, int(weight))
        with self._locked_state() as state:
            state["used"] = int(state.get("used", 0)) + requested

    def block(self, seconds: float) -> None:
        with self._locked_state() as state:
            state["blocked_until"] = max(
                float(state.get("blocked_until", 0.0)),
                time.time() + max(1.0, float(seconds)),
            )
