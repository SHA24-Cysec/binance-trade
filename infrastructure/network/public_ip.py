from __future__ import annotations

import ipaddress
import json
import logging
import threading
import time
from typing import Callable

import requests

logger = logging.getLogger(__name__)

_PUBLIC_IP_URL = "https://api.ipify.org?format=json"
_PUBLIC_IP_SUCCESS_TTL_SECONDS = 600.0
_PUBLIC_IP_RETRY_TTL_SECONDS = 60.0
_PUBLIC_IP_TIMEOUT = (2.0, 3.0)
_PUBLIC_IP_MAX_BODY_BYTES = 512


class PublicEgressIpProvider:
    def __init__(
        self,
        lookup: Callable[[], str] | None = None,
        *,
        success_ttl_seconds: float = _PUBLIC_IP_SUCCESS_TTL_SECONDS,
        retry_ttl_seconds: float = _PUBLIC_IP_RETRY_TTL_SECONDS,
    ) -> None:
        self._lookup = lookup or self._lookup_public_ip
        self._success_ttl = max(1.0, float(success_ttl_seconds))
        self._retry_ttl = max(1.0, float(retry_ttl_seconds))
        self._lock = threading.Lock()
        self._state: dict = {
            "ip": None,
            "status": "UNCHECKED",
            "checked_at": None,
            "attempted_at": None,
        }
        self._checked_monotonic: float | None = None
        self._refreshing = False

    @staticmethod
    def _lookup_public_ip() -> str:
        headers = {
            "Accept": "application/json",
            "User-Agent": "binance-trade-dashboard/1.0",
        }
        with requests.get(
            _PUBLIC_IP_URL,
            headers=headers,
            timeout=_PUBLIC_IP_TIMEOUT,
            allow_redirects=False,
            stream=True,
        ) as response:
            if response.status_code != 200:
                raise ValueError(f"layanan IP mengembalikan HTTP {response.status_code}")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=128):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > _PUBLIC_IP_MAX_BODY_BYTES:
                    raise ValueError("respons layanan IP terlalu besar")
        payload = json.loads(bytes(body).decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("ip"), str):
            raise ValueError("respons layanan IP tidak memuat alamat IP")
        return str(ipaddress.ip_address(payload["ip"].strip()))

    def _snapshot_locked(self, display_status: str | None = None) -> dict:
        return {
            "ip": self._state.get("ip"),
            "status": display_status or self._state.get("status", "UNCHECKED"),
            "checked_at": self._state.get("checked_at"),
            "attempted_at": self._state.get("attempted_at"),
        }

    def _record_failure_locked(self) -> None:
        previous_ip = self._state.get("ip")
        self._state = {
            "ip": previous_ip,
            "status": "STALE" if previous_ip else "ERROR",
            "checked_at": self._state.get("checked_at"),
            "attempted_at": time.time(),
        }
        self._checked_monotonic = time.monotonic()

    def _refresh(self) -> None:
        try:
            value = self._lookup()
            normalized = str(ipaddress.ip_address(str(value).strip()))
        except Exception as exc:
            logger.warning("Lookup IP publik gagal (%s).", type(exc).__name__)
            with self._lock:
                self._record_failure_locked()
                self._refreshing = False
            return

        with self._lock:
            checked_at = time.time()
            self._state = {
                "ip": normalized,
                "status": "OK",
                "checked_at": checked_at,
                "attempted_at": checked_at,
            }
            self._checked_monotonic = time.monotonic()
            self._refreshing = False

    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            age = (
                now - self._checked_monotonic
                if self._checked_monotonic is not None
                else None
            )
            current_status = self._state.get("status")
            if (
                current_status == "OK"
                and age is not None
                and age < self._success_ttl
            ):
                return self._snapshot_locked()
            if (
                current_status in ("ERROR", "STALE")
                and age is not None
                and age < self._retry_ttl
            ):
                return self._snapshot_locked()

            if not self._refreshing:
                self._refreshing = True
                worker = threading.Thread(
                    target=self._refresh,
                    name="dashboard-public-ip",
                    daemon=True,
                )
                try:
                    worker.start()
                except Exception as exc:
                    logger.warning(
                        "Thread lookup IP publik gagal dimulai (%s).",
                        type(exc).__name__,
                    )
                    self._record_failure_locked()
                    self._refreshing = False

            if self._refreshing:
                display_status = "REFRESHING" if self._state.get("ip") else "CHECKING"
                return self._snapshot_locked(display_status)
            return self._snapshot_locked()


_public_egress_ip_provider = PublicEgressIpProvider()


def get_public_egress_ip_status() -> dict:
    return _public_egress_ip_provider.status()
