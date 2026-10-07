"""Pembersihan file sisa runtime saat semua proses sudah berhenti.

Latar belakang
--------------
Folder ``data/`` menumpuk file mekanis sisa eksekusi: sidecar ``*.lock``
(yang sengaja dibiarkan oleh ``interprocess_lock`` demi keamanan lock
antar proses), file registrasi proses ``pump_bot_process_{mode}.json``
berstatus STOPPED, dan sisa ``*.reclaim`` dari protokol perebutan lock.
Ukurannya kecil (ratusan byte), tetapi membuat folder terlihat penuh.
Modul ini membersihkannya HANYA ketika tidak ada satu pun proses runtime
(bot PAPER/LIVE maupun dashboard) yang masih hidup, sehingga tidak ada
file yang ditarik dari bawah proses yang sedang bekerja.

Prinsip keamanan
----------------
1. Gerbang hidup: jika ``lock_owner``/``lifecycle_owner``/``global_lock_owner``
   mode apa pun masih menunjuk proses hidup, atau registri dashboard masih
   hidup, pembersihan dibatalkan total.
2. Lock sidecar hanya dihapus bila kita berhasil mengambil lock secara
   non-blocking; jika ada proses/thread lain yang sedang memegangnya,
   file dibiarkan.
3. File registrasi proses hanya dihapus bila statusnya STOPPED dan PID
   sudah mati. Status CRASHED dipertahankan sebagai bahan diagnosis.
4. Semua kegagalan bersifat lunak: pembersihan tidak boleh pernah
   menggugurkan proses shutdown; galat apa pun dilaporkan, bukan dilempar.

Penonaktifan
------------
Set variabel lingkungan ``PUMP_DISABLE_EXIT_CLEANUP=1`` untuk mematikan
pembersihan otomatis tanpa mengubah kode.

Jalankan ``python -m infrastructure.process.runtime_cleanup`` untuk
membersihkan manual, atau ``--selftest`` untuk menguji modul ini.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - tergantung platform
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - tergantung platform
    msvcrt = None

from infrastructure.paths import DATA_DIR
from infrastructure.process import procctl
from infrastructure.process import runtime_control as rtctl
from infrastructure.storage.atomic_io import atomic_write_json, read_json

_MODES = ("PAPER", "LIVE")

_DISABLE_ENV = "PUMP_DISABLE_EXIT_CLEANUP"


def cleanup_disabled() -> bool:
    """True bila user mematikan pembersihan lewat variabel lingkungan."""
    return str(os.environ.get(_DISABLE_ENV, "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "ya",
        "on",
    )


def dashboard_registry_path(data_dir: Path | None = None) -> Path:
    """Lokasi registri proses dashboard, sejajar dengan registri bot."""
    base = Path(data_dir) if data_dir is not None else DATA_DIR
    return base / "pump_dashboard_process.json"


def register_dashboard(data_dir: Path | None = None) -> None:
    """Catat PID dashboard agar proses lain tahu ada pemakai folder data."""
    payload = {
        "kind": "dashboard",
        "pid": os.getpid(),
        "process_identity": procctl.process_identity(os.getpid()),
        "status": "RUNNING",
        "started_at": time.time(),
        "updated_at": time.time(),
    }
    try:
        atomic_write_json(dashboard_registry_path(data_dir), payload)
    except OSError:
        # Registri bersifat informatif; kegagalan tidak boleh fatal.
        pass


def unregister_dashboard(data_dir: Path | None = None) -> None:
    """Hapus registri dashboard bila milik proses ini."""
    path = dashboard_registry_path(data_dir)
    data = read_json(path, {})
    if not isinstance(data, dict):
        return
    try:
        pid = int(data.get("pid", 0))
    except (TypeError, ValueError):
        pid = 0
    if pid == os.getpid():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _dashboard_owner(data_dir: Path | None = None) -> dict:
    data = read_json(dashboard_registry_path(data_dir), {})
    if not isinstance(data, dict) or not data:
        return {}
    try:
        pid = int(data.get("pid", 0))
    except (TypeError, ValueError):
        return {}
    if procctl.is_process_alive(pid, data.get("process_identity")):
        return data
    return {}


def live_runtime_owner(data_dir: Path | None = None) -> dict:
    """Kembalikan info proses runtime yang masih hidup, atau {} bila kosong.

    Memeriksa lock bot (per mode dan global), registri siklus hidup bot,
    dan registri dashboard. Dipakai sebagai gerbang sebelum menghapus
    apa pun.
    """
    owners: list[tuple[str, dict]] = []
    if rtctl.global_lock_owner():
        owners.append(("global", rtctl.global_lock_owner()))
    for mode in _MODES:
        if rtctl.lock_owner(mode):
            owners.append((mode, rtctl.lock_owner(mode)))
        if rtctl.lifecycle_owner(mode):
            owners.append((mode, rtctl.lifecycle_owner(mode)))
    dash = _dashboard_owner(data_dir)
    if dash:
        owners.append(("DASHBOARD", dash))
    if not owners:
        return {}
    label, info = owners[0]
    return {"scope": label, "pid": info.get("pid"), "detail": info}


def _try_acquire_lock(fd: int) -> bool:
    """Ambil lock secara non-blocking; True bila berhasil (berarti kosong)."""
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if msvcrt is not None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    # Tidak ada mekanisme lock yang tersedia: jangan menghapus apa pun.
    return False


def _release_lock(fd: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _remove_if_unheld(lock_path: Path, report: dict, data_dir: Path) -> None:
    """Hapus satu sidecar .lock hanya bila tidak sedang dipegang proses lain."""
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except FileNotFoundError:
        return
    except OSError as exc:
        report["kept"].append(f"{lock_path.name} ({exc.__class__.__name__})")
        return
    try:
        if not _try_acquire_lock(fd):
            report["kept"].append(f"{lock_path.name} (masih dipegang proses lain)")
            return
        try:
            # Cek ulang sesudah lock didapat: bila mendadak ada proses hidup
            # (misalnya bot baru mulai), batalkan penghapusan.
            if live_runtime_owner(data_dir):
                report["kept"].append(f"{lock_path.name} (proses muncul mendadak)")
                return
            lock_path.unlink(missing_ok=True)
            report["deleted"].append(lock_path.name)
        finally:
            _release_lock(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def cleanup_runtime_leftovers(
    *, data_dir: Path | None = None, include_locks: bool = True
) -> dict:
    """Hapus file mekanis sisa runtime bila tidak ada proses hidup.

    Mengembalikan laporan ``{"deleted": [...], "kept": [...], "aborted": ...}``.
    Tidak pernah melempar pengecualian ke pemanggil.
    """
    report: dict[str, Any] = {"deleted": [], "kept": [], "aborted": None}
    try:
        if cleanup_disabled():
            report["aborted"] = f"{_DISABLE_ENV} aktif; pembersihan dilewati."
            return report

        base = Path(data_dir) if data_dir is not None else DATA_DIR
        owner = live_runtime_owner(data_dir)
        if owner:
            report["aborted"] = (
                f"Proses {owner.get('scope')} PID {owner.get('pid')} masih "
                "hidup; pembersihan dibatalkan demi keamanan."
            )
            return report
        if not base.exists():
            return report

        # 1) Registrasi proses bot: hapus hanya bila STOPPED dan PID mati.
        for mode in _MODES:
            proc_path = base / f"pump_bot_process_{mode.lower()}.json"
            data = read_json(proc_path, {})
            if not isinstance(data, dict) or not data:
                continue
            status = str(data.get("status") or "").upper()
            try:
                pid = int(data.get("pid", 0))
            except (TypeError, ValueError):
                pid = 0
            # PID sama dengan proses ini berarti registri milik kita sendiri
            # yang baru saja ditulis STOPPED saat shutdown: aman dihapus.
            sendiri = pid == os.getpid()
            if status == "STOPPED" and (
                sendiri
                or not procctl.is_process_alive(pid, data.get("process_identity"))
            ):
                try:
                    proc_path.unlink(missing_ok=True)
                    report["deleted"].append(proc_path.name)
                except OSError as exc:
                    report["kept"].append(f"{proc_path.name} ({exc.__class__.__name__})")
            else:
                report["kept"].append(
                    f"{proc_path.name} (status {status or 'tidak dikenal'})"
                )

        # 2) Registri dashboard basi (PID mati) ikut dibersihkan.
        dash_path = dashboard_registry_path(base)
        dash = read_json(dash_path, {})
        if isinstance(dash, dict) and dash:
            try:
                pid = int(dash.get("pid", 0))
            except (TypeError, ValueError):
                pid = 0
            if not procctl.is_process_alive(pid, dash.get("process_identity")):
                try:
                    dash_path.unlink(missing_ok=True)
                    report["deleted"].append(dash_path.name)
                except OSError as exc:
                    report["kept"].append(f"{dash_path.name} ({exc.__class__.__name__})")

        if not include_locks:
            return report

        # 3) Sidecar .lock dan sisa .reclaim, satu per satu dengan try-lock.
        for path in sorted(base.iterdir()):
            if path.name.endswith(".lock"):
                _remove_if_unheld(path, report, base)
            elif path.name.endswith(".reclaim"):
                if live_runtime_owner(base):
                    report["kept"].append(f"{path.name} (proses muncul mendadak)")
                    continue
                try:
                    path.unlink(missing_ok=True)
                    report["deleted"].append(path.name)
                except OSError as exc:
                    report["kept"].append(f"{path.name} ({exc.__class__.__name__})")
    except Exception as exc:  # noqa: BLE001 - pembersihan tak boleh fatal
        report["aborted"] = f"Galat pembersihan: {type(exc).__name__}: {exc}"
    return report


def selftest() -> int:
    """Uji gerbang keamanan dan penghapusan sidecar lock."""
    import tempfile

    gagal = 0

    def cek(nama: str, syarat: bool, keterangan: str = "") -> None:
        nonlocal gagal
        print(
            f"  [{'OK  ' if syarat else 'GAGAL'}] {nama}"
            f"{(' -> ' + keterangan) if keterangan else ''}"
        )
        if not syarat:
            gagal += 1

    tmp = Path(tempfile.mkdtemp(prefix="uji-runtime-cleanup-"))
    asli_rt = rtctl.DATA_DIR
    try:
        rtctl.DATA_DIR = tmp

        print("=== 1. Tidak ada proses hidup: file mekanis dihapus ===")
        from infrastructure.storage.atomic_io import interprocess_lock

        target = tmp / "pump_bot_state_paper.json"
        with interprocess_lock(target):
            pass
        atomic_write_json(
            tmp / "pump_bot_process_paper.json",
            {
                "mode": "PAPER",
                "status": "STOPPED",
                "pid": 4194304,  # PID di luar rentang wajar: pasti mati.
                "process_identity": "selftest-mati",
            },
        )
        cek(
            "sidecar lock ada sebelum cleanup",
            (tmp / f"{target.name}.lock").exists(),
        )
        laporan = cleanup_runtime_leftovers(data_dir=tmp)
        cek(
            "sidecar lock terhapus",
            f"{target.name}.lock" in laporan["deleted"],
            str(laporan),
        )
        cek(
            "registrasi STOPPED terhapus",
            "pump_bot_process_paper.json" in laporan["deleted"],
        )
        cek("tidak ada abort", laporan["aborted"] is None, str(laporan["aborted"]))

        print("\n=== 2. Ada pemilik lock hidup: cleanup dibatalkan ===")
        atomic_write_json(
            rtctl.lock_file("PAPER"),
            {
                "mode": "PAPER",
                "pid": os.getpid(),  # proses ini hidup -> gerbang harus menolak
                "process_identity": procctl.process_identity(os.getpid()),
                "token": "selftest",
            },
        )
        target2 = tmp / "settings.json"
        with interprocess_lock(target2):
            pass
        laporan2 = cleanup_runtime_leftovers(data_dir=tmp)
        cek("abort terisi", bool(laporan2["aborted"]), str(laporan2["aborted"]))
        cek(
            "sidecar lock dipertahankan",
            (tmp / f"{target2.name}.lock").exists(),
        )

        print("\n=== 3. Lock sedang dipegang thread lain: file dipertahankan ===")
        rtctl.lock_file("PAPER").unlink(missing_ok=True)
        lock_path = tmp / f"{target2.name}.lock"
        with interprocess_lock(target2):
            laporan3 = cleanup_runtime_leftovers(data_dir=tmp)
        cek(
            "lock tertahan dilaporkan kept",
            any(f"{target2.name}.lock" in item for item in laporan3["kept"]),
            str(laporan3),
        )
        cek("file lock masih ada", lock_path.exists())

        print("\n=== 4. Registri STOPPED milik proses sendiri: dihapus ===")
        atomic_write_json(
            tmp / "pump_bot_process_paper.json",
            {
                "mode": "PAPER",
                "status": "STOPPED",
                "pid": os.getpid(),
                "process_identity": procctl.process_identity(os.getpid()),
            },
        )
        laporan4 = cleanup_runtime_leftovers(data_dir=tmp)
        cek(
            "registri sendiri terhapus",
            "pump_bot_process_paper.json" in laporan4["deleted"],
            str(laporan4),
        )
    finally:
        rtctl.DATA_DIR = asli_rt

    print()
    print("HASIL:", "LULUS" if gagal == 0 else f"{gagal} KEGAGALAN")
    return 1 if gagal else 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    laporan = cleanup_runtime_leftovers()
    if laporan["aborted"]:
        print(laporan["aborted"])
    if laporan["deleted"]:
        print("Terhapus:", ", ".join(laporan["deleted"]))
    if laporan["kept"]:
        print("Dipertahankan:", ", ".join(laporan["kept"]))
    if not laporan["deleted"] and not laporan["kept"] and not laporan["aborted"]:
        print("Tidak ada file runtime yang perlu dibersihkan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
