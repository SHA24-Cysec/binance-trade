"""Kurs IDR (Rupiah) untuk lapisan tampilan.

Modul ini sengaja hanya melayani kebutuhan tampilan: tidak ada satu pun
konsumen di jalur eksekusi (scanner, sizing, order, proteksi akun, dan
backtest) yang membaca berkas ini. Karena itu, kegagalan jaringan, pair
yang belum tersedia, atau kurs yang tidak wajar tidak akan pernah
menghentikan proses trading.

Sumber kurs bawaan adalah pair spot Binance USDTIDR (aktif sejak November
2025) lewat endpoint publik /api/v3/ticker/price sehingga tidak butuh API
key. Kurs terakhir disimpan ke berkas JSON secara atomik supaya angka
rupiah tetap tampil, dengan penanda basi, ketika bursa sedang tidak bisa
dihubungi atau ketika dashboard baru dijalankan ulang.

Urutan fallback saat pengambilan kurs gagal:
1. kurs terakhir di memori (masih disertai penanda basi bila terlalu tua),
2. kurs terakhir di berkas cache (sumber CACHE),
3. kurs tetap manual bila diisi (sumber MANUAL),
4. tidak ada kurs sama sekali (sumber NONE, dashboard menyembunyikan
   seluruh elemen rupiah tanpa menampilkan angka menyesatkan).
"""

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

# Pengaman sisi klien: satu request /ticker/price berbobot kecil, tetapi
# refresh di bawah 15 detik tetap tidak masuk akal untuk kurs dan hanya
# membebani limit IP yang dipakai bersama bot.
MIN_REFRESH_SECONDS = 15.0

# Batas penerimaan kurs. Di luar rentang ini, data dianggap salah baca
# (misalnya respons error yang ter-parse sebagai angka) dan ditolak.
MIN_SANE_RATE = 1.0
MAX_SANE_RATE = 1_000_000_000.0

# Kurs dari berkas cache yang lebih tua dari ini diabaikan sama sekali:
# menampilkan kurs berumur berbulan-bulan justru menyesatkan.
CACHE_HARD_MAX_AGE_SECONDS = 7 * 24 * 3600.0

# Setelah satu percobaan gagal, tunggu sekian detik sebelum mencoba lagi.
# Tanpa jeda, dashboard yang di-refresh cepat akan menghajar bursa yang
# sedang bermasalah di setiap permintaan.
FAILURE_BACKOFF_SECONDS = 60.0

# Jeda minimum antar permintaan paksa (force) dari endpoint dashboard,
# supaya tombol atau skrip yang menekan refresh bertubi-tubi tidak
# menembus TTL dan membebani bursa.
MIN_FORCE_INTERVAL_SECONDS = 5.0

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,30}$")


def resolve_state_path(path: Any) -> str:
    """Jadikan path berkas state kurs absolut terhadap akar proyek.

    Konfigurasi lain di repo ini memakai path relatif terhadap direktori
    kerja saat bot dijalankan. Untuk berkas kurs, path relatif diarahkan
    ke akar proyek supaya dashboard yang dijalankan dari direktori lain
    tetap menulis ke berkas yang sama.
    """
    raw = str(path or "").strip() or DEFAULT_STATE_FILE
    expanded = os.path.expanduser(raw)
    if os.path.isabs(expanded):
        return expanded
    return str(PROJECT_ROOT / expanded)


def _parse_rate(value: Any) -> "float | None":
    """Ubah nilai apa pun menjadi kurs yang wajar, atau None bila tidak layak."""
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
    """Penyedia kurs 1 USDT dalam rupiah untuk kebutuhan tampilan.

    Objek ini aman dipakai dari banyak thread dashboard sekaligus:
    - pembacaan konfigurasi dan status dilakukan di bawah kunci state,
    - pengambilan ke bursa dibatasi satu thread pada satu waktu (fetch
      lock non-blocking). Thread lain langsung dilayani kurs terakhir
      daripada ikut menunggu jaringan.

    Seluruh metode publik tidak melempar exception. Kegagalan apa pun
    dikembalikan pada field ``error`` di payload.
    """

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

    # -------------------------------------------------------------- konfigurasi

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

    # ------------------------------------------------------------------- publik

    def get(self, *, force: bool = False) -> dict:
        """Payload kurs terkini beserta metadata kesegarannya.

        Payload selalu berbentuk kamus dengan kunci tetap:
        enabled, rate, source, symbol, updated_at, updated_at_unix,
        age_seconds, stale, error. Nilai rate None berarti dashboard
        harus menyembunyikan seluruh elemen rupiah.
        """
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
            # Kurs tetap tidak pernah basi: tidak ada jam kedaluwarsa.
            return self._payload(rate=rate, source=SOURCE_MANUAL, updated_at_unix=None)

        return self._get_auto(force=force)

    def refresh(self) -> dict:
        """Paksa pengambilan kurs baru, dengan jeda minimum antar permintaan."""
        return self.get(force=True)

    # -------------------------------------------------------------------- auto

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
            # Permintaan paksa terlalu rapat: layani kurs terakhir saja.
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
            # Thread lain sedang mengambil kurs. Jangan menumpuk request.
            return self._fallback_payload(self._mem_error)

        try:
            now = self._clock()
            with self._state_lock:
                # Periksa ulang: bisa jadi thread lain baru selesai mengambil
                # kurs tepat sebelum kunci fetch didapat.
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
            except Exception as exc:  # noqa: BLE001 - jalur tampilan tidak boleh meledak
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
                # Import ditunda supaya modul ini bisa diuji (dan diimpor
                # dashboard) tanpa memuat seluruh tumpukan klien bursa.
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

    # -------------------------------------------------------------- berkas state

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
            # Kurs tersimpan milik pair lain (simbol sempat diubah user).
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
        except Exception as exc:  # noqa: BLE001 - cache bersifat best-effort
            logger.debug("Gagal menyimpan cache kurs ke %s: %s", path, exc)

    # ------------------------------------------------------------------ payload

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


class _FakeClient:
    """Klien tiruan untuk selftest offline (tidak pernah menyentuh jaringan)."""

    def __init__(self, prices: dict, fail: bool = False) -> None:
        self._prices = dict(prices)
        self._fail = fail
        self.calls = 0

    def get_price(self, symbol: str, max_retries: int = 3) -> float:
        self.calls += 1
        if self._fail:
            raise RuntimeError("jaringan uji sedang gagal")
        return float(self._prices[symbol])


def selftest() -> int:
    """Uji offline untuk perilaku provider kurs. Tidak butuh jaringan."""
    import tempfile

    failures = 0

    def cek(nama: str, kondisi: bool) -> None:
        nonlocal failures
        if kondisi:
            print(f"  OK   {nama}")
        else:
            failures += 1
            print(f"  GAGAL {nama}")

    print("=== SELFTEST: IdrRateProvider ===")

    cek("_parse_rate menolak nol", _parse_rate(0) is None)
    cek("_parse_rate menolak negatif", _parse_rate(-5) is None)
    cek("_parse_rate menolak NaN", _parse_rate(float("nan")) is None)
    cek("_parse_rate menolak inf", _parse_rate(float("inf")) is None)
    cek("_parse_rate menolak teks", _parse_rate("abc") is None)
    cek("_parse_rate menerima angka wajar", _parse_rate("17910.5") == 17910.5)

    with tempfile.TemporaryDirectory() as tmp:
        state_file = os.path.join(tmp, "fx.json")

        # 1. Mode disabled: tidak ada rate dan tidak ada panggilan jaringan.
        client = _FakeClient({"USDTIDR": 17910.0})
        provider = IdrRateProvider(
            {"IDR_DISPLAY_ENABLED": False, "IDR_RATE_STATE_FILE": state_file},
            client=client,
        )
        payload = provider.get()
        cek("disabled: rate kosong", payload["rate"] is None)
        cek("disabled: klien tidak dipanggil", client.calls == 0)

        # 2. Mode AUTO sukses: rate terisi, sumber BINANCE, cache ditulis.
        client = _FakeClient({"USDTIDR": 17910.0})
        provider = IdrRateProvider(
            {"IDR_RATE_STATE_FILE": state_file, "IDR_RATE_REFRESH_SECONDS": 300},
            client=client,
        )
        payload = provider.get()
        cek("auto sukses: rata terbaca", payload["rate"] == 17910.0)
        cek("auto sukses: sumber BINANCE", payload["source"] == SOURCE_BINANCE)
        cek("auto sukses: belum basi", payload["stale"] is False)
        cek("auto sukses: satu panggilan", client.calls == 1)
        provider.get()
        cek("auto sukses: TTL menahan panggilan kedua", client.calls == 1)
        cek("auto sukses: cache disk tertulis", os.path.exists(state_file))

        # 3. Instance baru (dashboard restart) lalu jaringan gagal: pakai CACHE.
        failing = _FakeClient({"USDTIDR": 17910.0}, fail=True)
        restarted = IdrRateProvider(
            {"IDR_RATE_STATE_FILE": state_file, "IDR_RATE_REFRESH_SECONDS": 300},
            client=failing,
        )
        payload = restarted.get()
        cek("restart gagal jaringan: sumber CACHE", payload["source"] == SOURCE_CACHE)
        cek("restart gagal jaringan: rate dari disk", payload["rate"] == 17910.0)
        cek("restart gagal jaringan: error terisi", bool(payload["error"]))

        # 4. Gagal jaringan, tanpa cache, ada kurs manual: pakai MANUAL.
        manual_only = IdrRateProvider(
            {
                "IDR_RATE_STATE_FILE": os.path.join(tmp, "kosong.json"),
                "IDR_RATE_MANUAL": 18000.0,
            },
            client=_FakeClient({}, fail=True),
        )
        payload = manual_only.get()
        cek("fallback manual: sumber MANUAL", payload["source"] == SOURCE_MANUAL)
        cek("fallback manual: rate manual", payload["rate"] == 18000.0)

        # 5. Gagal jaringan tanpa apa pun: NONE + pesan error, tidak meledak.
        kosong = IdrRateProvider(
            {"IDR_RATE_STATE_FILE": os.path.join(tmp, "kosong2.json")},
            client=_FakeClient({}, fail=True),
        )
        payload = kosong.get()
        cek("tanpa sumber: rate kosong", payload["rate"] is None)
        cek("tanpa sumber: sumber NONE", payload["source"] == SOURCE_NONE)
        cek("tanpa sumber: ada pesan error", bool(payload["error"]))

        # 6. Mode MANUAL eksplisit.
        manual_mode = IdrRateProvider(
            {
                "IDR_RATE_MODE": "MANUAL",
                "IDR_RATE_MANUAL": 17500.0,
                "IDR_RATE_STATE_FILE": state_file,
            },
            client=_FakeClient({"USDTIDR": 1.0}),
        )
        payload = manual_mode.get()
        cek("mode manual: rate terpakai", payload["rate"] == 17500.0)
        cek("mode manual: sumber MANUAL", payload["source"] == SOURCE_MANUAL)
        cek("mode manual: tidak basi", payload["stale"] is False)

        # 7. Cache yang sangat tua diabaikan, bukan ditampilkan.
        tua = os.path.join(tmp, "tua.json")
        atomic_write_json(
            tua,
            {
                "version": 1,
                "symbol": "USDTIDR",
                "rate": 16000.0,
                "updated_at_unix": time.time() - (CACHE_HARD_MAX_AGE_SECONDS + 60),
            },
        )
        provider_tua = IdrRateProvider(
            {"IDR_RATE_STATE_FILE": tua}, client=_FakeClient({}, fail=True)
        )
        payload = provider_tua.get()
        cek("cache sangat tua diabaikan", payload["rate"] is None)

        # 8. Cache milik simbol lain diabaikan.
        beda = os.path.join(tmp, "beda.json")
        atomic_write_json(
            beda,
            {
                "version": 1,
                "symbol": "USDCIDR",
                "rate": 17950.0,
                "updated_at_unix": time.time(),
            },
        )
        provider_beda = IdrRateProvider(
            {"IDR_RATE_STATE_FILE": beda}, client=_FakeClient({}, fail=True)
        )
        payload = provider_beda.get()
        cek("cache simbol lain diabaikan", payload["rate"] is None)

        # 9. Kurs tidak wajar dari bursa ditolak, fallback tetap jalan.
        aneh = IdrRateProvider(
            {"IDR_RATE_STATE_FILE": os.path.join(tmp, "aneh.json")},
            client=_FakeClient({"USDTIDR": 0.0}),
        )
        payload = aneh.get()
        cek("kurs nol dari bursa ditolak", payload["rate"] is None)

        # 10. Penanda basi muncul saat umur kurs melewati ambang.
        now = {"t": 1_000_000.0}
        klien_basi = _FakeClient({"USDTIDR": 17900.0})
        provider_basi = IdrRateProvider(
            {
                "IDR_RATE_STATE_FILE": os.path.join(tmp, "basi.json"),
                "IDR_RATE_REFRESH_SECONDS": 15,
                "IDR_RATE_MAX_AGE_SECONDS": 60,
                "IDR_RATE_MANUAL": 0,
            },
            client=klien_basi,
            clock=lambda: now["t"],
        )
        payload = provider_basi.get()
        cek("basi: awal belum basi", payload["stale"] is False)
        # Bursa mati setelah pengambilan pertama: kurs lama dipakai lagi
        # dan umurnya kini melewati ambang 60 detik.
        klien_basi._fail = True
        now["t"] += 120
        payload = provider_basi.get()
        cek("basi: kurs lama tetap dilayani", payload["rate"] == 17900.0)
        cek("basi: penanda aktif setelah 120 detik", payload["stale"] is True)
        cek("basi: ada pesan error jaringan", bool(payload["error"]))

    print(f"=== SELFTEST selesai: {failures} kegagalan ===")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(selftest())
