"""Kontrol proses lintas Windows dan POSIX untuk bot.

Semua cabang spesifik OS sengaja dikumpulkan di modul ini. Jalur shutdown
utama tetap file kontrol kooperatif. Fungsi sinyal di sini hanya cadangan
setelah timeout.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional


if os.name == "nt":  # pragma: no cover - hanya dieksekusi di Windows
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _STILL_ACTIVE = 259
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    # ctypes memakai c_int sebagai default restype. Tanpa deklarasi ini,
    # HANDLE Job Object 64-bit dapat terpotong di Windows 11.
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL


def process_identity(pid: int) -> str | None:
    """Identitas waktu pembuatan proses untuk melindungi dari PID reuse."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None

    if os.name == "nt":  # pragma: no cover - Windows
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not _kernel32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exit_time),
                ctypes.byref(kernel), ctypes.byref(user)
            ):
                return None
            value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return f"win:{value}"
        finally:
            _kernel32.CloseHandle(handle)

    stat_path = Path(f"/proc/{pid}/stat")
    try:
        raw = stat_path.read_text(encoding="utf-8")
        # Field kedua dapat mengandung spasi dan tanda kurung. Field 22
        # dihitung sesudah kurung tutup terakhir; indeks relatifnya 19.
        rest = raw[raw.rfind(")") + 2:].split()
        return f"proc:{rest[19]}"
    except (OSError, IndexError, ValueError):
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return None
        return f"pid:{pid}"


def is_process_alive(pid: int, expected_identity: str | None = None) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    if os.name == "nt":  # pragma: no cover - Windows
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            alive = code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            alive = True
        except PermissionError:
            alive = True
        except (ProcessLookupError, OSError):
            alive = False
    if not alive:
        return False
    if expected_identity:
        current = process_identity(pid)
        return current is not None and current == expected_identity
    return True


class ProcessTreeHandle:
    """Windows Job Object untuk memastikan child tree ikut dihentikan.

    Di POSIX process group menangani fungsi yang sama. Jika assignment Job
    Object gagal, atribut error berisi alasan dan caller tetap dapat memakai
    CTRL_BREAK lalu TerminateProcess pada PID utama.
    """

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.handle = None
        self.error: str | None = None
        if os.name != "nt":
            return
        try:  # pragma: no cover - Windows
            handle = _kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW gagal")
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = _kernel32.SetInformationJobObject(
                handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info)
            )
            if not ok:
                err = ctypes.get_last_error()
                _kernel32.CloseHandle(handle)
                raise OSError(err, "SetInformationJobObject gagal")
            if not _kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(proc._handle)):
                err = ctypes.get_last_error()
                _kernel32.CloseHandle(handle)
                raise OSError(err, "AssignProcessToJobObject gagal")
            self.handle = handle
        except Exception as exc:
            self.error = str(exc)

    @property
    def active(self) -> bool:
        return self.handle is not None

    def terminate(self, exit_code: int = 1) -> bool:
        if os.name != "nt" or self.handle is None:
            return False
        try:  # pragma: no cover - Windows
            return bool(_kernel32.TerminateJobObject(self.handle, int(exit_code)))
        except Exception:
            return False

    def close(self) -> None:
        if os.name == "nt" and self.handle is not None:  # pragma: no cover - Windows
            _kernel32.CloseHandle(self.handle)
            self.handle = None


def spawn_python(script: os.PathLike | str, *, args: Optional[list[str]] = None,
                 cwd: os.PathLike | str | None = None,
                 env: Optional[dict[str, str]] = None) -> tuple[subprocess.Popen, ProcessTreeHandle]:
    command = [sys.executable, os.fspath(script), *(args or [])]
    kwargs: dict = {
        "cwd": os.fspath(cwd) if cwd is not None else None,
        "env": env,
        "stdout": None,
        "stderr": None,
    }
    if os.name == "nt":  # pragma: no cover - Windows
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(command, **kwargs)
    return proc, ProcessTreeHandle(proc)


def send_graceful_signal(pid: int, *, process_group: bool = True) -> None:
    if os.name == "nt":  # pragma: no cover - Windows
        if not process_group:
            raise RuntimeError("CTRL_BREAK_EVENT membutuhkan child CREATE_NEW_PROCESS_GROUP")
        os.kill(int(pid), signal.CTRL_BREAK_EVENT)
    else:
        if process_group:
            os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
        else:
            os.kill(int(pid), signal.SIGTERM)


def force_kill(pid: int, tree: ProcessTreeHandle | None = None,
               *, process_group: bool = True) -> None:
    if os.name == "nt":  # pragma: no cover - Windows
        if tree is not None and tree.terminate(exit_code=1):
            return
        handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, int(pid))
        if not handle:
            return
        try:
            _kernel32.TerminateProcess(handle, 1)
        finally:
            _kernel32.CloseHandle(handle)
    else:
        if process_group:
            os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
        else:
            os.kill(int(pid), signal.SIGKILL)
