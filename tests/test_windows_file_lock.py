"""Regresi lock file jalur Windows (msvcrt).

PERBAIKAN AUDIT 2026-09-30 (temuan KRITIS-02).

Jalur msvcrt di atomic_io.py dan rate_limiter.py ditandai
"pragma: no cover, Windows" dan tidak pernah diuji sama sekali. Akibatnya
sebuah bug yang membuat bot dan dashboard GAGAL START di Windows bisa
bertahan lama tanpa terdeteksi:

    PermissionError: [Errno 13] Permission denied
      File "infrastructure/storage/atomic_io.py", line 81, in interprocess_lock
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

Penyebabnya, msvcrt.locking() mengunci region relatif terhadap posisi file
saat itu, sedangkan file dibuka dengan mode "a+" (O_APPEND) yang memaksa
setiap write ke akhir berkas. LOCK jatuh di offset akhir file, UNLOCK dicoba
di offset 0, dan membuka kunci region yang tidak terkunci menghasilkan EACCES.

File ini memasang msvcrt tiruan yang meniru kontrak _locking() Windows,
sehingga jalur tersebut dapat diuji dari Linux maupun macOS.

Referensi kontrak: dokumentasi Microsoft untuk _locking() menyebut EACCES
sebagai "Locking violation (file already locked or unlocked)". EACCES adalah
errno 13, yang dipetakan Python menjadi PermissionError.
"""

from __future__ import annotations

import errno
import importlib
import os
import sys
import types

import pytest


class MsvcrtPalsu(types.ModuleType):

    LK_LOCK = 0
    LK_NBLCK = 1
    LK_NBRLCK = 2
    LK_RLCK = 3
    LK_UNLCK = 4

    def __init__(self, name: str = "msvcrt") -> None:
        super().__init__(name)
        self.terkunci: dict[tuple[int, int], int] = {}
        self.jejak: list[tuple[str, int]] = []

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        offset = os.lseek(fd, 0, os.SEEK_CUR)
        kunci = (os.fstat(fd).st_ino, offset)
        if mode == self.LK_UNLCK:
            self.jejak.append(("unlock", offset))
            if kunci not in self.terkunci:
                raise PermissionError(errno.EACCES, "Permission denied")
            del self.terkunci[kunci]
            return
        self.jejak.append(("lock", offset))
        if kunci in self.terkunci:
            raise PermissionError(errno.EACCES, "Permission denied")
        self.terkunci[kunci] = nbytes


@pytest.fixture()
def atomic_io_windows(monkeypatch):
    palsu = MsvcrtPalsu()
    monkeypatch.setitem(sys.modules, "msvcrt", palsu)

    import infrastructure.storage.atomic_io as aio
    modul = importlib.reload(aio)
    monkeypatch.setattr(modul, "fcntl", None)
    monkeypatch.setattr(modul, "msvcrt", palsu)
    yield modul, palsu
    sys.modules.pop("msvcrt", None)
    importlib.reload(aio)


def test_lock_dan_unlock_memakai_offset_yang_sama(atomic_io_windows, tmp_path):
    modul, palsu = atomic_io_windows
    target = tmp_path / "pump_bot_settings_paper.json"

    with modul.interprocess_lock(target):
        pass

    offset_lock = [off for aksi, off in palsu.jejak if aksi == "lock"]
    offset_unlock = [off for aksi, off in palsu.jejak if aksi == "unlock"]
    assert offset_lock == offset_unlock == [0], (
        f"offset tidak cocok: lock={offset_lock} unlock={offset_unlock}"
    )
    assert not palsu.terkunci, "masih ada region yang belum dilepas"


def test_tidak_melempar_permission_error_di_windows(atomic_io_windows, tmp_path):
    modul, _ = atomic_io_windows
    target = tmp_path / "pump_bot_settings_paper.json"
    with modul.interprocess_lock(target):
        pass


def test_lock_berulang_tetap_stabil(atomic_io_windows, tmp_path):
    modul, palsu = atomic_io_windows
    target = tmp_path / "pump_bot_state_paper.json"
    for _ in range(50):
        with modul.interprocess_lock(target):
            pass
    assert not palsu.terkunci
    assert all(off == 0 for _, off in palsu.jejak)


def test_lock_file_tidak_tumbuh_setiap_akuisisi(atomic_io_windows, tmp_path):
    modul, _ = atomic_io_windows
    target = tmp_path / "pump_bot_state_paper.json"
    lock_file = tmp_path / "pump_bot_state_paper.json.lock"

    with modul.interprocess_lock(target):
        pass
    ukuran_awal = lock_file.stat().st_size

    for _ in range(25):
        with modul.interprocess_lock(target):
            pass

    assert lock_file.stat().st_size == ukuran_awal, (
        f"lock file tumbuh dari {ukuran_awal} menjadi "
        f"{lock_file.stat().st_size} byte setelah 25 akuisisi"
    )
    assert ukuran_awal <= 1


def test_unlock_gagal_tidak_menutupi_exception_asli(atomic_io_windows, tmp_path):
    modul, palsu = atomic_io_windows
    target = tmp_path / "pump_bot_state_paper.json"

    def unlock_selalu_gagal(fd, mode, nbytes):
        if mode == palsu.LK_UNLCK:
            raise PermissionError(errno.EACCES, "Permission denied")

    with pytest.raises(ValueError, match="kesalahan asli"):
        with modul.interprocess_lock(target):
            monkeypatch_locking = unlock_selalu_gagal
            palsu.locking = monkeypatch_locking
            raise ValueError("kesalahan asli dari blok kritis")


def test_file_descriptor_selalu_ditutup(atomic_io_windows, tmp_path):
    modul, _ = atomic_io_windows
    target = tmp_path / "pump_bot_state_paper.json"

    def hitung_fd() -> int:
        try:
            return len(os.listdir(f"/proc/{os.getpid()}/fd"))
        except OSError:
            pytest.skip("penghitungan fd hanya tersedia di Linux")

    sebelum = hitung_fd()
    for _ in range(30):
        with modul.interprocess_lock(target):
            pass
    assert hitung_fd() <= sebelum + 2


def test_rate_limiter_memakai_primitif_lock_yang_sama():
    import infrastructure.network.rate_limiter as rl

    for nama in ("_open_lock_fd", "_acquire_lock", "_release_lock"):
        fungsi = getattr(rl, nama)
        assert fungsi.__module__ == "infrastructure.storage.atomic_io", (
            f"{nama} tidak berasal dari atomic_io"
        )

    import inspect
    sumber = inspect.getsource(rl.SharedRequestWeightLimiter._locked_state)
    assert "LK_UNLCK" not in sumber, (
        "rate_limiter kembali memanggil msvcrt.locking sendiri, "
        "bug offset berpotensi terulang"
    )


def _flag_yang_dipakai(fungsi) -> set[str]:
    import ast
    import inspect
    import textwrap

    pohon = ast.parse(textwrap.dedent(inspect.getsource(fungsi)))
    return {
        simpul.attr
        for simpul in ast.walk(pohon)
        if isinstance(simpul, ast.Attribute)
        and isinstance(simpul.value, ast.Name)
        and simpul.value.id == "os"
    }


def test_mode_append_tidak_dipakai_lagi():
    import infrastructure.storage.atomic_io as aio

    flag = _flag_yang_dipakai(aio._open_lock_fd)
    assert "O_APPEND" not in flag, (
        "O_APPEND dipakai lagi pada file lock; offset lock akan bergeser "
        "dan PermissionError di Windows akan kembali"
    )
    assert "O_RDWR" in flag and "O_CREAT" in flag


def test_offset_lock_dan_unlock_diset_eksplisit():
    import infrastructure.storage.atomic_io as aio

    assert "lseek" in _flag_yang_dipakai(aio._acquire_lock)
    assert "lseek" in _flag_yang_dipakai(aio._release_lock)
