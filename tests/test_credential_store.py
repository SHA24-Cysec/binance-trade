from __future__ import annotations

import os
import stat

import pytest

from credential_store import credential_status, update_env


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
