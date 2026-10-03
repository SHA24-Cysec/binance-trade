from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None


logger = logging.getLogger("atomic_io")

_REPLACE_DELAYS = (0.02, 0.04, 0.08, 0.16, 0.32, 0.50)

_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(lock_path: str) -> threading.RLock:
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(lock_path)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[lock_path] = lock
        return lock


_LOCK_OFFSET = 0
_LOCK_LENGTH = 1


def _open_lock_fd(lock_path: str) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        if msvcrt is not None and fcntl is None:
            if os.lseek(fd, 0, os.SEEK_END) == 0:
                os.write(fd, b"0")
    except OSError:
        os.close(fd)
        raise
    return fd


def _acquire_lock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
    elif msvcrt is not None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, _LOCK_LENGTH)


def _release_lock(fd: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_LENGTH)
    except OSError as exc:
        logger.warning(
            "Gagal melepas lock file secara eksplisit (%s). File descriptor "
            "tetap ditutup, sehingga lock dilepas oleh sistem operasi.", exc,
        )


@contextmanager
def interprocess_lock(path: os.PathLike | str) -> Iterator[None]:
    target = os.fspath(path)
    lock_path = f"{target}.lock"
    parent = os.path.dirname(lock_path) or "."
    Path(parent).mkdir(parents=True, exist_ok=True)
    with _thread_lock_for(os.path.abspath(lock_path)):
        fd = _open_lock_fd(lock_path)
        try:
            _acquire_lock(fd)
            try:
                yield
            finally:
                _release_lock(fd)
        finally:
            os.close(fd)


def timestamp_tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def replace_with_retry(source: os.PathLike | str, target: os.PathLike | str,
                       delays: tuple[float, ...] = _REPLACE_DELAYS) -> None:
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
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        if mode is not None and os.name == "posix":
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
