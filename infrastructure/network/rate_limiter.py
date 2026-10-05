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
    replace_with_retry,
)

# Jeda percobaan ulang penulisan ledger. Lebih pendek dari bawaan atomic_io
# supaya request API tidak tertahan lama saat berkas terkunci sementara.
_DELAY_GANTI_LEDGER = (0.02, 0.05, 0.10)

# Setelah sekian kegagalan tulis berturut-turut, penulisan dijeda dulu supaya
# setiap request tidak selalu menunggu percobaan yang gagal.
_MAKS_GAGAL_BERTURUT = 3


class RateLimitBlockedError(RuntimeError):

    def __init__(self, retry_after: float):
        self.retry_after = max(1.0, float(retry_after))
        super().__init__(
            f"shared Binance rate limiter blocked for {self.retry_after:.1f}s"
        )


class SharedRequestWeightLimiter:

    _memory_lock = threading.RLock()

    def __init__(
        self,
        state_file: str | None = None,
        limit: int = 6000,
        safety_margin: int = 100,
        window_seconds: int = 60,
        *,
        jeda_gagal_tulis: float = 30.0,
    ) -> None:
        self.limit = max(1, int(limit))
        self.safety_margin = max(0, int(safety_margin))
        self.window_seconds = max(1, int(window_seconds))
        self.state_file = (
            os.path.abspath(os.path.expanduser(state_file)) if state_file else None
        )
        self._memory_state: dict | None = None
        # Penulisan ledger bersifat best-effort. Kalau berkas terkunci (mis.
        # OneDrive/antivirus di Windows), penulisan dijeda sementara dan
        # pencatatan tetap jalan dari memori supaya bot tidak berhenti.
        self._jeda_gagal_tulis = max(1.0, float(jeda_gagal_tulis))
        self._tulis_dijeda_sampai = 0.0
        self._gagal_tulis_berturut = 0
        self._peringatan_terakhir = 0.0

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
        except OSError as exc:
            # Tidak bisa dibaca (mis. terkunci OneDrive/antivirus). Sebelumnya
            # kondisi ini memblokir seluruh request; sekarang pencatatan lanjut
            # dari memori supaya trading tidak berhenti.
            self._log_gangguan_ledger(exc, aksi="dibaca")
            return None
        except (ValueError, TypeError) as exc:
            logger.error(
                "Ledger rate limit %s tidak dapat diverifikasi: %s. Request diblokir.",
                self.state_file,
                exc,
            )
            raise RateLimitBlockedError(self.window_seconds) from exc

    def _gabung_dengan_memori(
        self, dari_berkas: dict | None, now: float
    ) -> dict | None:
        """Ambil nilai tertinggi antara ledger berkas dan catatan memori.

        Dipakai supaya pembatasan tetap benar ketika penulisan berkas gagal:
        tanpa ini, setiap request membaca berkas lama yang belum diperbarui dan
        pemakaian weight bisa terlihat lebih rendah dari kenyataan.
        """
        memori = self._memory_state
        if memori is None:
            return dari_berkas
        if dari_berkas is None:
            return dict(memori)
        try:
            a = self._normalise(dict(dari_berkas), now)
            b = self._normalise(dict(memori), now)
        except RateLimitBlockedError:
            # Isi salah satu rusak: pakai yang masih bisa dibaca.
            try:
                return self._normalise(dict(memori), now)
            except RateLimitBlockedError:
                return dari_berkas
        if a["window_start"] != b["window_start"]:
            return a if a["window_start"] >= b["window_start"] else b
        return {
            "window_start": a["window_start"],
            "used": max(a["used"], b["used"]),
            "blocked_until": max(a["blocked_until"], b["blocked_until"]),
        }

    def _log_gangguan_ledger(
        self, exc: BaseException, *, aksi: str = "ditulis", kritis: bool = False
    ) -> None:
        """Catat kegagalan penulisan ledger, dibatasi supaya log tidak banjir."""
        now = time.time()
        if now - self._peringatan_terakhir < 30.0:
            return
        self._peringatan_terakhir = now
        pesan = (
            "Ledger rate limit %s tidak dapat %s: %s. Pencatatan lanjut "
            "dari memori proses ini sehingga request TIDAK dihentikan. "
            "Biasanya berkas terkunci oleh OneDrive/antivirus: pindahkan folder "
            "data keluar dari OneDrive atau jeda sinkronisasinya.",
            self.state_file,
            aksi,
            exc,
        )
        if kritis:
            logger.error(*pesan)
        else:
            logger.warning(*pesan)

    def _write_file(self, state: dict) -> bool:
        """Tulis ledger ke berkas. Kembalikan True kalau berhasil.

        Tidak pernah melempar pengecualian: kegagalan penulisan hanya membuat
        ledger tidak tersinkron antar proses, sedangkan pembatasan tetap jalan
        dari memori. Sebelumnya os.replace yang gagal (WinError 5) menjalar
        sampai loop utama bot dan membuat equity/manage_exit terlewat.
        """
        if not self.state_file:
            return False
        now = time.time()
        if now < self._tulis_dijeda_sampai:
            return False
        parent = os.path.dirname(self.state_file) or "."
        tmp = None
        try:
            os.makedirs(parent, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".rate-limit-", dir=parent, text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            replace_with_retry(tmp, self.state_file, delays=_DELAY_GANTI_LEDGER)
            tmp = None
            try:
                os.chmod(self.state_file, 0o600)
            except OSError:
                pass
        except OSError as exc:
            self._gagal_tulis_berturut += 1
            if self._gagal_tulis_berturut >= _MAKS_GAGAL_BERTURUT:
                self._tulis_dijeda_sampai = now + self._jeda_gagal_tulis
            self._log_gangguan_ledger(
                exc, aksi="ditulis", kritis=self._gagal_tulis_berturut == 1
            )
            return False
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        self._gagal_tulis_berturut = 0
        return True

    @contextmanager
    def _locked_state_memori(self) -> Iterator[dict]:
        """Mode cadangan tanpa berkas: semua pencatatan hanya di memori."""
        now = time.time()
        with self._memory_lock:
            state = self._normalise(self._memory_state, now)
            yield state
            self._memory_state = state

    @contextmanager
    def _locked_state(self) -> Iterator[dict]:
        if not self.state_file:
            with self._locked_state_memori() as state:
                yield state
            return

        lock_path = self.state_file + ".lock"
        parent = os.path.dirname(lock_path) or "."
        fd = None
        try:
            os.makedirs(parent, mode=0o700, exist_ok=True)
            fd = _open_lock_fd(lock_path)
            _acquire_lock(fd)
        except OSError as exc:
            # Lock tidak bisa diambil (berkas terkunci proses lain seperti
            # OneDrive). Lanjut dengan catatan memori supaya request tidak
            # gagal total; keamanan tetap dijaga per proses.
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._log_gangguan_ledger(exc, aksi="dikunci")
            with self._locked_state_memori() as state:
                yield state
            return
        try:
            state = self._normalise(
                self._gabung_dengan_memori(self._read_file(), time.time()),
                time.time(),
            )
            yield state
            self._write_file(state)
            self._memory_state = state
        finally:
            _release_lock(fd)
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
                            requested,
                            effective_limit,
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


def selftest() -> int:
    """Uji ketahanan ledger terhadap berkas yang terkunci (WinError 5).

    Meniru kejadian di log LIVE 2026-10-04: os.replace gagal dengan
    PermissionError sehingga loop utama bot berhenti memantau posisi.
    """
    import shutil
    import unittest.mock as mock

    gagal = 0

    def cek(nama: str, syarat: bool, keterangan: str = "") -> None:
        nonlocal gagal
        print(
            f"  [{'OK  ' if syarat else 'GAGAL'}] {nama}"
            f"{(' -> ' + keterangan) if keterangan else ''}"
        )
        if not syarat:
            gagal += 1

    tmpdir = tempfile.mkdtemp(prefix="uji-rate-limit-")
    try:
        berkas = os.path.join(tmpdir, "binance_rate_limit_state.json")

        print("=== 1. Operasi normal ===")
        lim = SharedRequestWeightLimiter(berkas, limit=6000, safety_margin=100)
        lim.reserve(10)
        lim.observe_server_weight(250)
        cek("ledger tertulis", os.path.exists(berkas))
        isi = json.loads(open(berkas, encoding="utf-8").read())
        cek("weight tercatat 250", int(isi.get("used", 0)) == 250, str(isi))

        print("\n=== 2. Berkas terkunci: os.replace gagal WinError 5 ===")
        lim2 = SharedRequestWeightLimiter(berkas, limit=6000, safety_margin=100)
        galat = PermissionError(13, "Access is denied")
        galat.winerror = 5
        with mock.patch("os.replace", side_effect=galat):
            try:
                lim2.reserve(100)
                lim2.observe_server_weight(400)
                lim2.record_retry_after_server_wait(5)
                lempar = False
            except BaseException as exc:  # noqa: BLE001
                lempar = True
                print("      pengecualian bocor:", type(exc).__name__, exc)
            cek("tidak ada pengecualian yang bocor ke pemanggil", not lempar)

        print("\n=== 3. Pencatatan tetap jalan dari memori ===")
        with mock.patch("os.replace", side_effect=galat):
            for _ in range(5):
                lim2.reserve(100)
            # baca ulang lewat jalur memori: pakai nilai tertinggi
            with lim2._locked_state() as state:  # noqa: SLF001
                dipakai = int(state["used"])
        cek(
            "weight tetap terhitung walau berkas tak bisa ditulis",
            dipakai >= 400,
            f"used={dipakai}",
        )
        cek(
            "penulisan dijeda setelah beberapa kegagalan",
            lim2._tulis_dijeda_sampai > 0,  # noqa: SLF001
            f"jeda sampai {lim2._tulis_dijeda_sampai:.0f}",
        )  # noqa: SLF001

        print("\n=== 4. Ledger pulih saat berkas bisa ditulis lagi ===")
        lim3 = SharedRequestWeightLimiter(
            berkas, limit=6000, safety_margin=100, jeda_gagal_tulis=1.0
        )
        with mock.patch("os.replace", side_effect=galat):
            for _ in range(4):
                lim3.reserve(50)
        waktu = json.loads(open(berkas, encoding="utf-8").read())
        sebelum = int(waktu.get("used", 0))
        lim3._tulis_dijeda_sampai = 0.0  # noqa: SLF001
        lim3.reserve(1)
        sesudah = json.loads(open(berkas, encoding="utf-8").read())
        cek(
            "berkas diperbarui lagi setelah pulih",
            int(sesudah.get("used", 0)) >= sebelum + 1,
            f"{sebelum} -> {sesudah.get('used')}",
        )
        cek(
            "tidak ada berkas sementara yang tertinggal",
            not [n for n in os.listdir(tmpdir) if n.startswith(".rate-limit-")],
            str(os.listdir(tmpdir)),
        )

        print("\n=== 5. Lock tidak bisa diambil: tetap jalan ===")
        lim4 = SharedRequestWeightLimiter(berkas, limit=6000, safety_margin=100)
        with mock.patch(
            "infrastructure.network.rate_limiter._open_lock_fd", side_effect=galat
        ):
            try:
                lim4.reserve(7)
                lim4.observe_server_weight(300)
                bocor = False
            except BaseException as exc:  # noqa: BLE001
                bocor = True
                print("      pengecualian bocor:", type(exc).__name__, exc)
        cek("lock gagal tidak menghentikan request", not bocor)

        print("\n=== 6. Isi ledger rusak tetap fail-closed ===")
        with open(berkas, "w", encoding="utf-8") as fh:
            fh.write("{bukan json")
        lim5 = SharedRequestWeightLimiter(berkas, limit=6000, safety_margin=100)
        try:
            lim5.reserve(1)
            diblokir = False
        except RateLimitBlockedError:
            diblokir = True
        cek("ledger rusak memblokir request (perilaku lama dipertahankan)", diblokir)

        print("\n=== 7. Jendela baru mereset pemakaian ===")
        lim6 = SharedRequestWeightLimiter(
            None, limit=6000, safety_margin=100, window_seconds=1
        )
        lim6.reserve(50)
        with lim6._locked_state() as state:  # noqa: SLF001
            state["window_start"] = time.time() - 5
        time.sleep(0.01)
        with lim6._locked_state() as state:  # noqa: SLF001
            cek(
                "pemakaian direset pada jendela baru",
                state["used"] == 0,
                f"used={state['used']}",
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    if gagal:
        print(f"HASIL: {gagal} pemeriksaan GAGAL")
        return 1
    print("SEMUA SELFTEST rate_limiter.py LULUS.")
    return 0


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(selftest())
