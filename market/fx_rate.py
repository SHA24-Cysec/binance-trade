
from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from infrastructure.paths import PROJECT_ROOT
from infrastructure.storage.atomic_io import (
    atomic_write_json,
    interprocess_lock,
    read_json,
)

logger = logging.getLogger("fx_rate")

SOURCE_BINANCE = "BINANCE"
SOURCE_CACHE = "CACHE"
SOURCE_MANUAL = "MANUAL"
SOURCE_NONE = "NONE"

RATE_MODES = ("AUTO", "MANUAL")
DEFAULT_SYMBOL = "USDTIDR"
DEFAULT_STATE_FILE = "data/fx_rate_idr.json"
DEFAULT_REFRESH_SECONDS = 300.0
DEFAULT_MAX_AGE_SECONDS = 3600.0

MIN_REFRESH_SECONDS = 15.0

MIN_SANE_RATE = 1.0
MAX_SANE_RATE = 1_000_000_000.0

CACHE_HARD_MAX_AGE_SECONDS = 7 * 24 * 3600.0

FAILURE_BACKOFF_SECONDS = 60.0

MIN_FORCE_INTERVAL_SECONDS = 5.0

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,30}$")


def resolve_state_path(path: Any) -> str:
    raw = str(path or "").strip() or DEFAULT_STATE_FILE
    expanded = os.path.expanduser(raw)
    if os.path.isabs(expanded):
        return expanded
    return str(PROJECT_ROOT / expanded)


def _parse_rate(value: Any) -> "float | None":
    if isinstance(value, bool):
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rate):
        return None
    if rate < MIN_SANE_RATE or rate > MAX_SANE_RATE:
        return None
    return rate


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "ya", "on"):
        return True
    if text in ("0", "false", "no", "tidak", "off", ""):
        return False
    return default


def _coerce_seconds(value: Any, default: float, minimum: float) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(seconds) or seconds <= 0:
        return default
    return max(minimum, seconds)


def _format_ts(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


class IdrRateProvider:

    def __init__(
        self,
        config: dict,
        client: Any = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = config if isinstance(config, dict) else {}
        self._client = client
        self._clock = clock

        self._state_lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._client_lock = threading.Lock()

        self._mem_rate: "float | None" = None
        self._mem_ts: float = 0.0
        self._mem_source: str = SOURCE_NONE
        self._mem_error: "str | None" = None

        self._last_attempt_ts: float = 0.0
        self._last_force_ts: float = 0.0


    def enabled(self) -> bool:
        return _coerce_bool(self._cfg.get("IDR_DISPLAY_ENABLED", True), True)

    def mode(self) -> str:
        raw = str(self._cfg.get("IDR_RATE_MODE", "AUTO") or "AUTO").strip().upper()
        return raw if raw in RATE_MODES else "AUTO"

    def symbol(self) -> str:
        raw = str(self._cfg.get("IDR_RATE_SYMBOL", DEFAULT_SYMBOL) or "").strip().upper()
        if not _SYMBOL_RE.fullmatch(raw):
            return DEFAULT_SYMBOL
        return raw

    def refresh_seconds(self) -> float:
        return _coerce_seconds(
            self._cfg.get("IDR_RATE_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS),
            DEFAULT_REFRESH_SECONDS,
            MIN_REFRESH_SECONDS,
        )

    def max_age_seconds(self) -> float:
        return _coerce_seconds(
            self._cfg.get("IDR_RATE_MAX_AGE_SECONDS", DEFAULT_MAX_AGE_SECONDS),
            DEFAULT_MAX_AGE_SECONDS,
            60.0,
        )

    def manual_rate(self) -> "float | None":
        return _parse_rate(self._cfg.get("IDR_RATE_MANUAL"))

    def state_path(self) -> str:
        return resolve_state_path(self._cfg.get("IDR_RATE_STATE_FILE"))


    def get(self, *, force: bool = False) -> dict:
        if not self.enabled():
            return self._payload(rate=None, source=SOURCE_NONE, updated_at_unix=None)

        if self.mode() == "MANUAL":
            rate = self.manual_rate()
            if rate is None:
                return self._payload(
                    rate=None,
                    source=SOURCE_NONE,
                    updated_at_unix=None,
                    error=(
                        "IDR_RATE_MODE=MANUAL tetapi IDR_RATE_MANUAL belum diisi "
                        "atau tidak wajar."
                    ),
                )
            return self._payload(rate=rate, source=SOURCE_MANUAL, updated_at_unix=None)

        return self._get_auto(force=force)

    def refresh(self) -> dict:
        return self.get(force=True)


    def _get_auto(self, *, force: bool) -> dict:
        now = self._clock()

        with self._state_lock:
            if not force and self._memory_is_fresh(now):
                return self._payload(
                    rate=self._mem_rate,
                    source=self._mem_source,
                    updated_at_unix=self._mem_ts,
                )
            last_attempt = self._last_attempt_ts
            last_force = self._last_force_ts

        if force and (now - last_force) < MIN_FORCE_INTERVAL_SECONDS:
            force = False
            with self._state_lock:
                if self._memory_is_fresh(now):
                    return self._payload(
                        rate=self._mem_rate,
                        source=self._mem_source,
                        updated_at_unix=self._mem_ts,
                    )

        if not force and last_attempt and (now - last_attempt) < FAILURE_BACKOFF_SECONDS:
            return self._fallback_payload(self._mem_error)

        if not self._fetch_lock.acquire(blocking=False):
            return self._fallback_payload(self._mem_error)

        try:
            now = self._clock()
            with self._state_lock:
                if not force and self._memory_is_fresh(now):
                    return self._payload(
                        rate=self._mem_rate,
                        source=self._mem_source,
                        updated_at_unix=self._mem_ts,
                    )
                self._last_attempt_ts = now
                if force:
                    self._last_force_ts = now

            error: "str | None" = None
            rate: "float | None" = None
            try:
                rate = self._fetch_from_exchange()
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                logger.debug("Gagal mengambil kurs %s: %s", self.symbol(), error)

            if rate is not None:
                ts = self._clock()
                with self._state_lock:
                    self._mem_rate = rate
                    self._mem_ts = ts
                    self._mem_source = SOURCE_BINANCE
                    self._mem_error = None
                self._write_disk(rate, ts)
                return self._payload(
                    rate=rate, source=SOURCE_BINANCE, updated_at_unix=ts
                )

            with self._state_lock:
                self._mem_error = error
            return self._fallback_payload(error)
        finally:
            self._fetch_lock.release()

    def _memory_is_fresh(self, now: float) -> bool:
        if self._mem_rate is None:
            return False
        return (now - self._mem_ts) < self.refresh_seconds()

    def _fallback_payload(self, error: "str | None") -> dict:
        with self._state_lock:
            mem_rate = self._mem_rate
            mem_ts = self._mem_ts
            mem_source = self._mem_source

        if mem_rate is not None:
            return self._payload(
                rate=mem_rate, source=mem_source, updated_at_unix=mem_ts, error=error
            )

        cached = self._read_disk()
        if cached is not None:
            rate, ts = cached
            with self._state_lock:
                self._mem_rate = rate
                self._mem_ts = ts
                self._mem_source = SOURCE_CACHE
            return self._payload(
                rate=rate, source=SOURCE_CACHE, updated_at_unix=ts, error=error
            )

        manual = self.manual_rate()
        if manual is not None:
            return self._payload(
                rate=manual, source=SOURCE_MANUAL, updated_at_unix=None, error=error
            )

        return self._payload(
            rate=None,
            source=SOURCE_NONE,
            updated_at_unix=None,
            error=error or "kurs rupiah belum tersedia.",
        )

    def _fetch_from_exchange(self) -> float:
        client = self._ensure_client()
        symbol = self.symbol()
        price = client.get_price(symbol, max_retries=1)
        rate = _parse_rate(price)
        if rate is None:
            raise ValueError(f"harga {symbol} dari bursa tidak wajar: {price!r}")
        return rate

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                from trading.clients.binance_client import BinanceSpotClient

                cfg = self._cfg
                self._client = BinanceSpotClient(
                    "",
                    "",
                    str(cfg.get("LIVE_BASE_URL", "https://api.binance.com")),
                    allow_signed=False,
                    rate_limit_state_file=cfg.get("RATE_LIMIT_STATE_FILE"),
                    rate_limit_limit=int(cfg.get("RATE_LIMIT_WEIGHT_LIMIT", 6000) or 6000),
                    rate_limit_safety_margin=int(
                        cfg.get("RATE_LIMIT_SAFETY_MARGIN", 100) or 100
                    ),
                )
        return self._client


    def _read_disk(self) -> "tuple[float, float] | None":
        path = self.state_path()
        try:
            doc = read_json(path, default=None)
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None

        symbol = str(doc.get("symbol", "")).strip().upper()
        if symbol != self.symbol():
            return None

        rate = _parse_rate(doc.get("rate"))
        if rate is None:
            return None

        try:
            ts = float(doc.get("updated_at_unix"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(ts) or ts <= 0:
            return None
        if (self._clock() - ts) > CACHE_HARD_MAX_AGE_SECONDS:
            return None
        return rate, ts

    def _write_disk(self, rate: float, ts: float) -> None:
        path = self.state_path()
        payload = {
            "version": 1,
            "symbol": self.symbol(),
            "rate": rate,
            "updated_at_unix": ts,
            "updated_at": _format_ts(ts),
        }
        try:
            with interprocess_lock(path):
                atomic_write_json(path, payload)
        except Exception as exc:
            logger.debug("Gagal menyimpan cache kurs ke %s: %s", path, exc)


    def _payload(
        self,
        *,
        rate: "float | None",
        source: str,
        updated_at_unix: "float | None",
        error: "str | None" = None,
    ) -> dict:
        age: "float | None" = None
        if rate is not None and updated_at_unix not in (None, 0):
            age = max(0.0, self._clock() - float(updated_at_unix))
        stale = bool(age is not None and age > self.max_age_seconds())
        return {
            "enabled": True,
            "rate": rate,
            "source": source,
            "symbol": self.symbol(),
            "updated_at": _format_ts(updated_at_unix) if updated_at_unix else None,
            "updated_at_unix": updated_at_unix,
            "age_seconds": age,
            "stale": stale,
            "error": error,
        }
