"""Utilitas I/O atomik lintas Windows dan POSIX.

Semua file teks ditulis sebagai UTF-8. Penggantian target memakai os.replace
karena atomik jika temporary file berada pada filesystem yang sama. Windows
dapat menolak replace sementara bila target sedang dibuka proses lain, jadi
replace dicoba ulang dengan backoff singkat.
"""

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
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover, Windows
    fcntl = None

try:
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover, POSIX
    msvcrt = None


logger = logging.getLogger("atomic_io")

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


# =====================================================================
# Primitif lock file lintas platform.
#
# PERBAIKAN AUDIT 2026-09-30 (temuan KRITIS-02).
#
# Implementasi lama memakai open(lock_path, "a+") lalu:
#     seek(0); write("0"); flush(); msvcrt.locking(fd, LK_LOCK, 1)
#   ... blok kritis ...
#     seek(0); msvcrt.locking(fd, LK_UNLCK, 1)
#
# Ada dua fakta yang bertabrakan di sana:
#   1. msvcrt.locking() mengunci region relatif terhadap POSISI FILE SAAT INI.
#   2. Mode "a+" berarti O_APPEND, sehingga setiap write() dipaksa ke AKHIR
#      file tanpa peduli seek(0) yang baru saja dipanggil, dan posisi file
#      ikut pindah ke akhir.
#
# Akibatnya LOCK diambil di offset akhir file (1, lalu 2, lalu 3, ... karena
# file tumbuh satu byte setiap kali), sedangkan UNLOCK selalu dicoba di
# offset 0. Membuka kunci region yang tidak pernah terkunci membuat CRT
# Windows mengembalikan EACCES, yang muncul di Python sebagai
# "PermissionError: [Errno 13] Permission denied" dan menggagalkan import
# config sebelum bot maupun dashboard sempat start.
#
# Bug ini tidak pernah terlihat di Linux atau macOS karena di sana cabang
# fcntl.flock yang dipakai, dan flock mengunci seluruh berkas tanpa konsep
# offset. Kedua cabang msvcrt juga ditandai "pragma: no cover" sehingga tidak
# pernah teruji sama sekali.
#
# Perbaikan: pakai file descriptor mentah tanpa O_APPEND, posisikan offset
# secara eksplisit ke 0 dengan os.lseek() sebelum lock MAUPUN unlock, dan
# tulis byte penanda maksimal sekali seumur berkas.
# =====================================================================

_LOCK_OFFSET = 0
_LOCK_LENGTH = 1


def _open_lock_fd(lock_path: str) -> int:
    """Buka file lock sebagai fd mentah, sengaja TANPA O_APPEND.

    O_BINARY hanya ada di Windows dan wajib supaya CRT tidak melakukan
    terjemahan newline yang bisa menggeser offset.
    """
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        if msvcrt is not None and fcntl is None:  # pragma: no cover, Windows
            # Windows butuh minimal satu byte untuk dikunci. Ditulis hanya
            # ketika berkas masih kosong, supaya lock file tidak tumbuh satu
            # byte setiap kali lock diambil seperti pada versi lama.
            if os.lseek(fd, 0, os.SEEK_END) == 0:
                os.write(fd, b"0")
    except OSError:
        os.close(fd)
        raise
    return fd


def _acquire_lock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
    elif msvcrt is not None:  # pragma: no cover, Windows
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, _LOCK_LENGTH)


def _release_lock(fd: int) -> None:
    """Lepas lock. Kegagalan di sini TIDAK boleh menutupi exception asli.

    Menutup file descriptor sudah melepaskan lock pada kedua platform, jadi
    kegagalan unlock aman untuk diturunkan menjadi peringatan. Versi lama
    membiarkannya naik dari blok finally, sehingga error asli dari blok
    kritis tertimpa PermissionError yang menyesatkan.
    """
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover, Windows
            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_LENGTH)
    except OSError as exc:
        logger.warning(
            "Gagal melepas lock file secara eksplisit (%s). File descriptor "
            "tetap ditutup, sehingga lock dilepas oleh sistem operasi.", exc,
        )


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
