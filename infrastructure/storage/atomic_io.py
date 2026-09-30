"""Utilitas I/O atomik lintas Windows dan POSIX.

Semua file teks ditulis sebagai UTF-8. Penggantian target memakai os.replace
karena atomik jika temporary file berada pada filesystem yang sama. Windows
dapat menolak replace sementara bila target sedang dibuka proses lain, jadi
replace dicoba ulang dengan backoff singkat.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover, Windows
    fcntl = None

try:
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover, POSIX
    msvcrt = None


_REPLACE_DELAYS = (0.02, 0.04, 0.08, 0.16, 0.32, 0.50)

# Perbaikan audit 2026-09-27 (temuan RENDAH-03): sebelumnya SATU RLock global
# menserialisasi seluruh interprocess_lock dalam proses meski menyasar file
# berbeda (state, settings, audit, rate limit saling menunggu tanpa perlu).
# Sekarang setiap path lock mendapat RLock sendiri; sifat reentrant per path
# dipertahankan, dan tidak ada risiko deadlock ordering baru karena pemanggil
# yang sama tidak pernah memegang dua path lock bersarang dengan urutan
# berlawanan (pola pemakaian di repo ini: satu lock per operasi tulis).
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(lock_path: str) -> threading.RLock:
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(lock_path)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[lock_path] = lock
        return lock


@contextmanager
def interprocess_lock(path: os.PathLike | str) -> Iterator[None]:
    """Lock advisory lintas proses untuk satu file runtime.

    Atomic replace mencegah pembacaan setengah file, tetapi tidak mencegah dua
    proses melakukan read-modify-write yang saling menimpa. Lock ini dipakai
    state/settings/control yang memiliki lebih dari satu pembaca atau penulis.
    """
    target = os.fspath(path)
    lock_path = f"{target}.lock"
    parent = os.path.dirname(lock_path) or "."
    Path(parent).mkdir(parents=True, exist_ok=True)
    with _thread_lock_for(os.path.abspath(lock_path)):
        with open(lock_path, "a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:  # pragma: no cover, Windows
                handle.seek(0)
                handle.write("0")
                handle.flush()
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover, Windows
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def timestamp_tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def replace_with_retry(source: os.PathLike | str, target: os.PathLike | str,
                       delays: tuple[float, ...] = _REPLACE_DELAYS) -> None:
    """Ganti target secara atomik, dengan retry khusus kegagalan sharing.

    PermissionError adalah bentuk umum kegagalan sharing Windows. Beberapa
    build Python melaporkan sharing violation sebagai OSError dengan winerror
    5 atau 32, jadi keduanya diperlakukan sama hanya di Windows.
    """
    src = os.fspath(source)
    dst = os.fspath(target)
    last: BaseException | None = None
    for attempt in range(len(delays) + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last = exc
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) in (5, 32):
                last = exc
            else:
                raise
        if attempt < len(delays):
            time.sleep(delays[attempt])
    assert last is not None
    raise last


def atomic_write_text(path: os.PathLike | str, text: str, *, mode: int | None = None) -> None:
    """Tulis teks UTF-8 ke temporary unik lalu replace target."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        if mode is not None and os.name == "posix":
            # Buat temp langsung dengan 0600. Jangan pernah memberi jendela
            # singkat di mana secret .env sudah tertulis tetapi masih 0644.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            handle_cm = os.fdopen(fd, "w", encoding="utf-8", newline="")
        else:
            handle_cm = open(tmp, "x", encoding="utf-8", newline="")
        with handle_cm as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None and os.name == "posix":
            os.chmod(tmp, mode)
        replace_with_retry(tmp, target)
        if mode is not None and os.name == "posix":
            os.chmod(target, mode)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_json(path: os.PathLike | str, data: Any, *, mode: int | None = None) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    atomic_write_text(path, text, mode=mode)


def read_json(path: os.PathLike | str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def archive_corrupt(path: os.PathLike | str) -> Path | None:
    """Pindahkan file rusak ke *.corrupt-<timestamp>, tanpa menimpanya."""
    source = Path(path)
    if not source.exists():
        return None
    base = source.with_name(f"{source.name}.corrupt-{timestamp_tag()}")
    backup = base
    index = 1
    while backup.exists():
        backup = Path(f"{base}-{index}")
        index += 1
    replace_with_retry(source, backup)
    return backup


def append_json_line(path: os.PathLike | str, data: Any) -> None:
    """Tambahkan satu JSON object UTF-8 per baris dan paksa flush ke disk.

    Pemanggil wajib melakukan serialisasi antar-thread. Fungsi ini dipakai
    dashboard yang berjalan sebagai satu proses, bukan sebagai database umum.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
    with open(target, "a", encoding="utf-8", newline="") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
