
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

from infrastructure.paths import DATA_DIR
from infrastructure.process import procctl
from infrastructure.process import runtime_control as rtctl
from infrastructure.storage.atomic_io import atomic_write_json, read_json

_MODES = ("PAPER", "LIVE")

_DISABLE_ENV = "PUMP_DISABLE_EXIT_CLEANUP"


def cleanup_disabled() -> bool:
    return str(os.environ.get(_DISABLE_ENV, "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "ya",
        "on",
    )


def dashboard_registry_path(data_dir: Path | None = None) -> Path:
    base = Path(data_dir) if data_dir is not None else DATA_DIR
    return base / "pump_dashboard_process.json"


def register_dashboard(data_dir: Path | None = None) -> None:
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
        pass


def unregister_dashboard(data_dir: Path | None = None) -> None:
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


def _probe_lock_holder(lock_path: Path, data_dir: Path) -> str | None:
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except FileNotFoundError:
        return "hilang"
    except OSError as exc:
        return exc.__class__.__name__
    try:
        if not _try_acquire_lock(fd):
            return "masih dipegang proses lain"
        try:
            if live_runtime_owner(data_dir):
                return "proses muncul mendadak"
        finally:
            _release_lock(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return None


def _remove_if_unheld(lock_path: Path, report: dict, data_dir: Path) -> None:
    alasan = _probe_lock_holder(lock_path, data_dir)
    if alasan == "hilang":
        return
    if alasan is not None:
        report["kept"].append(f"{lock_path.name} ({alasan})")
        return
    try:
        lock_path.unlink(missing_ok=True)
    except OSError as exc:
        report["kept"].append(f"{lock_path.name} ({exc.__class__.__name__})")
        return
    report["deleted"].append(lock_path.name)


def cleanup_runtime_leftovers(
    *, data_dir: Path | None = None, include_locks: bool = True
) -> dict:
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
    except Exception as exc:
        report["aborted"] = f"Galat pembersihan: {type(exc).__name__}: {exc}"
    return report


def main(argv: list[str]) -> int:
    argparse.ArgumentParser(description="Pembersihan file runtime").parse_args(argv)
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
