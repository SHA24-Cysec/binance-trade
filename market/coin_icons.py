"""Pencarian ikon koin berdasarkan simbol aset dasar (mis. BOME dari BOMEUSDT).

Sumber: endpoint publik CoinGecko /api/v3/search (tanpa API key). Hasil disimpan
di cache file JSON agar setiap simbol hanya dicari sesekali. Bila CoinGecko
membatasi permintaan (HTTP 429), pencarian dihentikan sementara dan tidak
disimpan sebagai "tidak ditemukan", sehingga dicoba lagi nanti.

Catatan atribusi: data CoinGecko wajib dicantumkan sumbernya di tampilan
produksi sesuai syarat penggunaan CoinGecko.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

import requests

from infrastructure.storage.atomic_io import atomic_write_json, read_json

logger = logging.getLogger("coin_icons")

SEARCH_URL = "https://api.coingecko.com/api/v3/search"
SEARCH_TIMEOUT_S = 8
TTL_FOUND_S = 30 * 86400      # ikon yang ditemukan: simpan 30 hari
TTL_MISSING_S = 1 * 86400     # tidak ditemukan: coba lagi setelah 1 hari
RATE_LIMIT_PAUSE_S = 600      # setelah HTTP 429: jeda pencarian 10 menit
SYMBOL_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")

_lock = threading.Lock()
_store: Optional[dict] = None
_store_path: Optional[Path] = None
_blocked_until = 0.0


def configure(path: Path) -> None:
    """Tentukan lokasi file cache. Dipanggil sekali saat dashboard dimuat."""
    global _store_path, _store
    with _lock:
        _store_path = Path(path)
        _store = None


def _load_store() -> dict:
    global _store
    if _store is None:
        data = read_json(_store_path, default={}) if _store_path else {}
        _store = data if isinstance(data, dict) else {}
    return _store


def _save_store() -> None:
    if _store_path is None:
        return
    try:
        _store_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(_store_path, _store or {})
    except OSError as exc:
        logger.warning("Cache ikon koin tidak bisa ditulis: %s", exc)


def _pick_best(coins: list, symbol: str) -> Optional[str]:
    """Ambil ikon dari koin yang simbolnya persis sama dan peringkat pasarnya tertinggi."""
    exact = [
        c for c in coins
        if str(c.get("symbol", "")).upper() == symbol and c.get("large")
    ]
    if not exact:
        return None
    best = min(exact, key=lambda c: c.get("market_cap_rank") or 10**9)
    return str(best["large"])


def resolve_icon_url(symbol: str) -> Optional[str]:
    """Kembalikan URL gambar ikon untuk simbol, atau None bila tidak tersedia."""
    global _blocked_until
    if not SYMBOL_RE.match(symbol or ""):
        return None
    sym = symbol.upper()
    now = time.time()
    with _lock:
        store = _load_store()
        entry = store.get(sym)
        if isinstance(entry, dict):
            ttl = TTL_FOUND_S if entry.get("url") else TTL_MISSING_S
            if now - float(entry.get("t", 0) or 0) < ttl:
                return entry.get("url")

        if now < _blocked_until:
            # Sedang dibatasi: pakai hasil lama bila ada, jangan cari ulang.
            return entry.get("url") if isinstance(entry, dict) else None

        try:
            resp = requests.get(
                SEARCH_URL, params={"query": sym}, timeout=SEARCH_TIMEOUT_S
            )
        except requests.RequestException as exc:
            logger.warning("Pencarian ikon %s gagal: %s", sym, exc)
            return entry.get("url") if isinstance(entry, dict) else None

        if resp.status_code == 429:
            _blocked_until = now + RATE_LIMIT_PAUSE_S
            logger.warning("CoinGecko membatasi permintaan; pencarian ikon dijeda.")
            return entry.get("url") if isinstance(entry, dict) else None
        if resp.status_code != 200:
            logger.warning("Pencarian ikon %s: HTTP %s", sym, resp.status_code)
            return entry.get("url") if isinstance(entry, dict) else None

        try:
            coins = resp.json().get("coins", []) or []
        except ValueError:
            return entry.get("url") if isinstance(entry, dict) else None

        url = _pick_best(coins, sym)
        store[sym] = {"url": url, "t": now}
        _save_store()
        return url
