"""Ukur dan naikkan batas file descriptor proses.

Satu proses dibatasi oleh jumlah file descriptor. Setiap berkas yang dibuka,
setiap soket jaringan, dan setiap lock file dihitung pada batas yang sama.
Ketika batas itu tercapai, sistem mengembalikan EMFILE yang di Python tampil
sebagai ``OSError(24, 'Too many open files')``. Kegagalannya menular ke semua
subsystem sekaligus: lock state, socket Binance, bahkan socket epoll milik
server web.

Catatan historis: kejadian 07-10-2026 di mesin Linux menunjukkan 500 pada
``GET /api/control/status`` dan ``Max retries exceeded`` pada
``GET /api/v3/ticker/price`` dengan penyebab yang sama, yaitu fd habis. Modul
ini dibuat supaya pemakaian fd bisa dilihat langsung dari dashboard, sehingga
kebocoran ketahuan sebelum proses mati, bukan sesudahnya.
"""

from __future__ import annotations

import errno
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("file_descriptors")

# EMFILE: "Too many open files". Kode ini menandai semua gejala fd habis.
EMFILE = getattr(errno, "EMFILE", 24)


def is_emfile(exc: BaseException) -> bool:
    """True kalau pengecualian disebabkan batas fd tercapai.

    Dipakai agar pesan galat bisa menjelaskan penyebab sebenarnya, bukan
    sekadar "koneksi Binance gagal".
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, OSError) and exc.errno == EMFILE:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def get_fd_count() -> int | None:
    """Jumlah fd yang sedang terbuka pada proses ini, atau None bila tak terukur."""
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


def _proc_limits_soft_hard() -> tuple[int | None, int | None]:
    """Baca batas "Max open files" dari /proc/self/limits (khusus Linux).

    Mengembalikan (soft, hard). None bila berkas tidak ada atau formatnya asing.
    """
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
    """Batas fd proses (soft atau hard). None bila platform tidak mendukung."""
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
        # macOS melaporkan hard limit tak terbatas, angka ini tidak dipakai kernel.
        return None
    return int(value)


def raise_fd_limit(desired: int = 65536) -> tuple[int | None, int | None, bool]:
    """Naikkan soft limit mendekati ``desired``, tidak pernah melewati hard limit.

    Mengembalikan ``(soft_sekarang, hard_limit, berubah)``. Upaya menaikkan
    dibungkus ``try/except``: kegagalan (mis. sistem melampaui hard limit yang
    hanya bisa diubah root) dicatat sebagai peringatan, bukan exception, karena
    menjalankan bot dengan batas lama tetap lebih baik daripada berhenti.
    """
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
    """Ringkasan pemakaian fd untuk dashboard, JSON-safe."""
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


def selftest() -> int:
    """Uji pengukuran fd dan penaikan limit pada Linux."""
    import tempfile

    gagal = 0
    sebelum = get_fd_count()
    if sebelum is None:
        print("  [SKIP] /proc/self/fd tidak tersedia (bukan Linux)")
    else:
        dengan_buka = []
        try:
            for _ in range(5):
                fd = os.open(tempfile.mkstemp()[1], os.O_RDONLY)
                dengan_buka.append(fd)
            sesudah = get_fd_count()
            assert (
                sesudah >= sebelum + 5
            ), f"fd harus bertambah minimal 5, dapat {sesudah - sebelum}"
            print(f"  fd terukur: {sebelum} -> {sesudah} (+{sesudah - sebelum}) -> OK")
        except AssertionError as exc:
            print(f"  [GAGAL] {exc}")
            gagal += 1
        finally:
            for fd in dengan_buka:
                try:
                    os.close(fd)
                except OSError:
                    pass
    status = fd_status()
    try:
        assert isinstance(status, dict) and "soft_limit" in status
        assert status["measured"] in (True, False)
        print(f"  fd_status() JSON-safe: {status} -> OK")
    except AssertionError as exc:
        print(f"  [GAGAL] fd_status: {exc}")
        gagal += 1
    soft, _hard, berubah = raise_fd_limit(4096)
    try:
        assert soft is not None, "soft limit harus terbaca di Linux"
        assert not berubah or soft >= 4096, "soft limit wajib naik bila hard mengizinkan"
        print(f"  raise_fd_limit(): soft={soft} berubah={berubah} -> OK")
    except AssertionError as exc:
        print(f"  [GAGAL] raise_fd_limit: {exc}")
        gagal += 1
    emfile = OSError(EMFILE, "Too many open files")
    rantainya = RuntimeError("koneksi gagal")
    rantainya.__cause__ = emfile
    try:
        assert is_emfile(emfile) and is_emfile(rantainya)
        assert not is_emfile(ValueError("bukan fd"))
        print("  is_emfile() mengenali rantai pengecualian -> OK")
    except AssertionError as exc:
        print(f"  [GAGAL] is_emfile: {exc}")
        gagal += 1
    if gagal:
        print(f"SELFTEST file_descriptors: {gagal} GAGAL")
        return 1
    print("SEMUA SELFTEST file_descriptors.py LULUS.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(selftest())
