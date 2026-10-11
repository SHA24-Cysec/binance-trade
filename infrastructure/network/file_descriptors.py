
from __future__ import annotations

import errno
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("file_descriptors")

EMFILE = getattr(errno, "EMFILE", 24)


def is_emfile(exc: BaseException) -> bool:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, OSError) and exc.errno == EMFILE:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def get_fd_count() -> int | None:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


def _proc_limits_soft_hard() -> tuple[int | None, int | None]:
    try:
        text = Path("/proc/self/limits").read_text(encoding="utf-8")
    except OSError:
        return (None, None)
    for line in text.splitlines():
        if not line.lower().startswith("max open files"):
            continue
        parts = line.split()
        try:
            soft = -1 if parts[-3] == "unlimited" else int(parts[-3])
            hard = -1 if parts[-2] == "unlimited" else int(parts[-2])
        except (IndexError, ValueError):
            return (None, None)
        return (soft, hard)
    return (None, None)


def get_fd_limit(which: str = "soft") -> int | None:
    try:
        import resource
    except ImportError:
        soft, hard = _proc_limits_soft_hard()
        return soft if which == "soft" else hard
    res = resource.RLIMIT_NOFILE if which == "soft" else resource.RLIMIT_NOFILE
    try:
        soft, hard = resource.getrlimit(res)
    except OSError:
        return None
    value = soft if which == "soft" else hard
    if value in (-1, 1 << 63):
        return None
    if sys.platform == "darwin" and which == "hard" and value == 9223372036854775807:
        return None
    return int(value)


def raise_fd_limit(desired: int = 65536) -> tuple[int | None, int | None, bool]:
    try:
        import resource
    except ImportError:
        return (None, None, False)
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except OSError as exc:
        logger.warning("Batas fd tidak dapat dibaca (%s), limitasi dibiarkan.", exc)
        return (None, None, False)
    target = int(desired)
    if hard not in (-1,):
        target = min(target, hard) if hard > 0 else target
    if soft >= target:
        logger.info("Batas fd proses sudah cukup: soft=%s hard=%s", soft, hard)
        return (int(soft), None if hard == -1 else int(hard), False)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (OSError, ValueError) as exc:
        logger.warning(
            "Gagal menaikkan batas fd dari %s ke %s (%s). Jalankan dengan "
            "`ulimit -n %s` atau naikkan LimitNOFILE di unit systemd bila "
            "pemakaian fd terus tumbuh.",
            soft,
            target,
            exc,
            target,
        )
        return (int(soft), None if hard == -1 else int(hard), False)
    new_soft, new_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    logger.info(
        "Batas fd proses dinaikkan dari %s menjadi %s (hard %s).",
        soft,
        new_soft,
        new_hard,
    )
    return (int(new_soft), None if new_hard == -1 else int(new_hard), True)


def fd_status() -> dict:
    count = get_fd_count()
    soft = get_fd_limit("soft")
    hard = get_fd_limit("hard")
    terpakai = None
    if count is not None and soft:
        terpakai = round(100.0 * count / float(soft), 1)
    return {
        "count": count,
        "soft_limit": soft,
        "hard_limit": hard,
        "percent_used": terpakai,
        "measured": count is not None,
    }
