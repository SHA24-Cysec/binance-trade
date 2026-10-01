from __future__ import annotations

import os
import stat

import pytest

from infrastructure.security.credential_store import credential_status, update_env


def test_update_env_preserves_crlf_and_unrelated_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"# catatan\r\nOTHER=value\r\nBINANCE_API_KEY=old\r\n")
    result = update_env(api_key="new-key", api_secret="new-secret", path=path)
    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert b"# catatan\r\nOTHER=value\r\n" in raw
    assert b"BINANCE_API_KEY='new-key'\r\n" in raw
    assert b"BINANCE_API_SECRET='new-secret'\r\n" in raw
    assert result["platform"] in {"posix", "windows"}


def test_blank_values_can_be_written_without_disclosure(tmp_path):
    path = tmp_path / ".env"
    update_env(api_key="secret-key", api_secret="secret-value", path=path)
    update_env(api_key="", api_secret="", path=path)
    text = path.read_text(encoding="utf-8")
    assert "secret-key" not in text
    assert "secret-value" not in text
    assert "BINANCE_API_KEY=" in text
    assert "BINANCE_API_SECRET=" in text


@pytest.mark.skipif(os.name != "posix", reason="Mode file 0600 hanya berlaku di POSIX")
def test_env_permissions_are_0600_on_posix(tmp_path):
    path = tmp_path / ".env"
    update_env(api_key="abc", api_secret="def", path=path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    status = credential_status(path)
    assert status["permissions"]["ok"] is True


@pytest.mark.skipif(os.name != "nt", reason="ACL icacls hanya dapat diverifikasi di Windows")
def test_windows_acl_status_is_explicit(tmp_path):
    path = tmp_path / ".env"
    result = update_env(api_key="abc", api_secret="def", path=path)
    assert result["platform"] == "windows"
    assert isinstance(result["ok"], bool)
    assert result["message"]



# ============================================================
# Test verifikasi ACL Windows.
# Bug yang diperbaiki: icacls menampilkan NAMA akun (contoh
# DESKTOP-ABC\john), bukan SID mentah, sehingga cek lama yang
# mencari string SID selalu gagal di Windows normal.
# Fungsi yang diuji di sini sengaja tidak bergantung os.name
# supaya bisa diuji di Linux maupun Windows; alur cabang nt
# penuh diuji test khusus Windows di bawah.
# ============================================================

_SID = "S-1-5-21-3623811015-3361044348-30300820-1013"
_NAMA = "DESKTOP-ABC\\john"


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _whoami(nama=_NAMA, sid=_SID):
    return _Proc(stdout=f'"{nama}","{sid}"\r\n')


def test_identity_in_text_menerima_sid_atau_nama():
    from infrastructure.security import credential_store as cs
    assert cs._identity_in_text(f"{_NAMA}:(F)", _SID, _NAMA)
    assert cs._identity_in_text(f"{_SID}:(F)", _SID, _NAMA)
    assert cs._identity_in_text("desktop-abc\\JOHN:(F)", _SID, _NAMA)
    assert not cs._identity_in_text("BUILTIN\\Administrators:(F)", _SID, _NAMA)
    assert not cs._identity_in_text("", _SID, _NAMA)
    assert not cs._identity_in_text(f"{_NAMA}:(F)", None, None)


def test_windows_identity_membaca_sid_dan_nama(monkeypatch):
    from infrastructure.security import credential_store as cs
    monkeypatch.setattr(cs.subprocess, "run", lambda cmd, **kw: _whoami())
    assert cs._windows_current_identity() == (_SID, _NAMA)


def test_windows_identity_output_kosong(monkeypatch):
    from infrastructure.security import credential_store as cs
    monkeypatch.setattr(cs.subprocess, "run", lambda cmd, **kw: _Proc(stdout="\r\n"))
    assert cs._windows_current_identity() == (None, None)


def test_acl_check_icacls_bentuk_nama(monkeypatch, tmp_path):
    # Regresi bug yang dilaporkan pengguna: icacls normal menampilkan
    # NAMA akun. Cek lama mencari SID mentah sehingga selalu gagal.
    from infrastructure.security import credential_store as cs
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY='abc'\n", encoding="utf-8")
    monkeypatch.setattr(cs, "_windows_read_sddl", lambda t: None)
    monkeypatch.setattr(
        cs.subprocess, "run",
        lambda cmd, **kw: _Proc(stdout=f"{env} {_NAMA}:(F)\r\n    NT AUTHORITY\\SYSTEM:(F)\r\n"),
    )
    check = cs._windows_acl_check(env, _SID, _NAMA)
    assert check == {"ok": True, "via": "icacls", "verified": True}


def test_acl_check_icacls_bentuk_sid(monkeypatch, tmp_path):
    from infrastructure.security import credential_store as cs
    env = tmp_path / ".env"
    env.write_text("x", encoding="utf-8")
    monkeypatch.setattr(cs, "_windows_read_sddl", lambda t: None)
    monkeypatch.setattr(
        cs.subprocess, "run",
        lambda cmd, **kw: _Proc(stdout=f"{env} {_SID}:(F)\r\n"),
    )
    check = cs._windows_acl_check(env, _SID, _NAMA)
    assert check["ok"] is True and check["verified"] is True


def test_acl_check_acl_tanpa_akun_pengguna(monkeypatch, tmp_path):
    from infrastructure.security import credential_store as cs
    env = tmp_path / ".env"
    env.write_text("x", encoding="utf-8")
    monkeypatch.setattr(cs, "_windows_read_sddl", lambda t: None)
    monkeypatch.setattr(
        cs.subprocess, "run",
        lambda cmd, **kw: _Proc(stdout=f"{env} BUILTIN\\Administrators:(F)\r\n"),
    )
    check = cs._windows_acl_check(env, _SID, _NAMA)
    assert check == {"ok": False, "via": "icacls", "verified": True}


def test_acl_check_icacls_gagal_dengan_pesan(monkeypatch, tmp_path):
    from infrastructure.security import credential_store as cs
    env = tmp_path / ".env"
    env.write_text("x", encoding="utf-8")
    monkeypatch.setattr(cs, "_windows_read_sddl", lambda t: None)
    monkeypatch.setattr(
        cs.subprocess, "run",
        lambda cmd, **kw: _Proc(returncode=1, stderr="Access is denied"),
    )
    check = cs._windows_acl_check(env, _SID, _NAMA)
    assert check["ok"] is False and check["verified"] is False
    assert "Access is denied" in check["error"]


def test_acl_check_subprocess_error_tidak_crash(monkeypatch, tmp_path):
    from infrastructure.security import credential_store as cs
    env = tmp_path / ".env"
    env.write_text("x", encoding="utf-8")
    monkeypatch.setattr(cs, "_windows_read_sddl", lambda t: None)

    def meledak(cmd, **kw):
        raise OSError("icacls tidak ditemukan")
    monkeypatch.setattr(cs.subprocess, "run", meledak)
    check = cs._windows_acl_check(env, _SID, _NAMA)
    assert check["ok"] is False and check["verified"] is False
    assert "icacls tidak ditemukan" in check["error"]


def test_acl_check_sddl_dipakai_lebih_dulu(monkeypatch, tmp_path):
    # SDDL memuat SID mentah; bila terbaca, icacls tidak boleh dipanggil.
    from infrastructure.security import credential_store as cs
    env = tmp_path / ".env"
    env.write_text("x", encoding="utf-8")
    sddl = f"O:{_SID}G:S-1-5-21-999D:AI(A;;FA;;;{_SID})(A;;FA;;;SY)"
    monkeypatch.setattr(cs, "_windows_read_sddl", lambda t: sddl)

    def tidak_boleh(cmd, **kw):
        raise AssertionError("icacls tidak boleh dipanggil bila SDDL terbaca")
    monkeypatch.setattr(cs.subprocess, "run", tidak_boleh)
    check = cs._windows_acl_check(env, _SID, _NAMA)
    assert check == {"ok": True, "via": "sddl", "verified": True}


def test_acl_check_sddl_tanpa_akun_pengguna(monkeypatch, tmp_path):
    from infrastructure.security import credential_store as cs
    env = tmp_path / ".env"
    env.write_text("x", encoding="utf-8")
    monkeypatch.setattr(cs, "_windows_read_sddl",
                        lambda t: "O:S-1-5-21-999D:AI(A;;FA;;;BA)(A;;FA;;;SY)")
    check = cs._windows_acl_check(env, _SID, _NAMA)
    assert check == {"ok": False, "via": "sddl", "verified": True}


def test_read_sddl_none_di_luar_windows(tmp_path):
    # Guard os.name memastikan fungsi ini tidak menyentuh ctypes di POSIX.
    from infrastructure.security import credential_store as cs
    if os.name == "nt":
        pytest.skip("Test ini khusus non-Windows.")
    assert cs._windows_read_sddl(tmp_path / ".env") is None


@pytest.mark.skipif(os.name != "nt", reason="Alur ACL penuh hanya berjalan di Windows")
def test_windows_alur_lengkap_status_dan_secure(tmp_path):
    # Di Windows asli: simpan kredensial -> ACL dikunci -> status OK.
    # Ini test regresi sebenarnya untuk bug "ACL .env tidak dapat diverifikasi".
    result = update_env(api_key="abc", api_secret="def", path=tmp_path / ".env")
    assert result["platform"] == "windows"
    assert result["ok"] is True, result
    status = credential_status(tmp_path / ".env")
    assert status["permissions"]["ok"] is True, status["permissions"]
    assert status["permissions"]["message"] == "ACL pengguna aktif terdeteksi."
