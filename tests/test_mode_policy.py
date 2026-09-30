from __future__ import annotations

import pytest

from web import dashboard


@pytest.fixture()
def client():
    dashboard.app.config.update(TESTING=True)
    dashboard._confirmations.clear()
    dashboard._write_attempts.clear()
    with dashboard.app.test_client() as value:
        yield value


def headers():
    return {"Origin": "http://localhost", "X-Admin-Token": dashboard._ADMIN_TOKEN}


@pytest.mark.parametrize(("current", "target"), [("PAPER", "LIVE"), ("LIVE", "PAPER")])
def test_open_position_blocks_mode_change_in_both_directions(client, monkeypatch, current, target):
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "MODE", current)
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "API_KEY", "key")
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "API_SECRET", "secret")
    monkeypatch.setattr(dashboard._process_manager, "status", lambda mode=None: {"status": "STOPPED"})
    monkeypatch.setattr(dashboard._process_manager, "position", lambda mode=None: {
        "has_position": True, "symbol": "ARBUSDT", "qty": 1.0, "entry_price": 1.0
    })
    monkeypatch.setattr(dashboard, "_credentials_tested_for_active_values", lambda: True)
    response = client.post("/api/mode/prepare", json={"target": target}, headers=headers())
    assert response.status_code == 409
    assert "Posisi" in response.get_json()["error"]


def _siapkan_live_siap(monkeypatch):
    """Kondisi di mana perpindahan ke LIVE seharusnya lolos semua gerbang."""
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "MODE", "PAPER")
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "API_KEY", "key")
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "API_SECRET", "secret")
    monkeypatch.setattr(dashboard._process_manager, "status", lambda mode=None: {"status": "STOPPED"})
    monkeypatch.setattr(dashboard._process_manager, "position", lambda mode=None: {"has_position": False})
    monkeypatch.setattr(dashboard, "_credentials_tested_for_active_values", lambda: True)
    # Gerbang audit 2026-09-30 (temuan TINGGI-05): key wajib terbukti tidak
    # punya izin penarikan dana.
    monkeypatch.setattr(dashboard, "_withdrawal_permission_safe", lambda: True)


def test_live_commit_requires_exact_live_phrase(client, monkeypatch):
    _siapkan_live_siap(monkeypatch)
    prepared = client.post("/api/mode/prepare", json={"target": "LIVE"}, headers=headers())
    assert prepared.status_code == 200
    confirmation = prepared.get_json()["confirmation_id"]
    response = client.post("/api/mode/commit", json={"confirmation_id": confirmation, "phrase": "live"}, headers=headers())
    assert response.status_code == 400
    assert "LIVE" in response.get_json()["error"]


def test_live_ditolak_bila_api_key_masih_boleh_menarik_dana(client, monkeypatch):
    """Temuan TINGGI-05: key dengan izin withdrawal tidak boleh dipakai LIVE."""
    _siapkan_live_siap(monkeypatch)
    monkeypatch.setattr(dashboard, "_withdrawal_permission_safe", lambda: False)
    response = client.post("/api/mode/prepare", json={"target": "LIVE"}, headers=headers())
    assert response.status_code == 409
    assert "penarikan" in response.get_json()["error"].lower()


def test_live_ditolak_bila_kedua_rem_akun_mati(client, monkeypatch):
    """Temuan KRITIS-01: LIVE tanpa DD stop dan daily stop harus diblokir."""
    _siapkan_live_siap(monkeypatch)

    asli = dashboard.config_mod.build_config_for_mode

    def tanpa_rem(mode):
        cfg, errors = asli(mode)
        cfg = dict(cfg)
        cfg["USE_EQUITY_STOP"] = False
        cfg["USE_DAILY_STOP"] = False
        return cfg, errors

    monkeypatch.setattr(dashboard.config_mod, "build_config_for_mode", tanpa_rem)
    response = client.post("/api/mode/prepare", json={"target": "LIVE"}, headers=headers())
    assert response.status_code == 409
    assert "USE_EQUITY_STOP" in response.get_json()["error"]


def test_checklist_menampilkan_gerbang_baru(client, monkeypatch):
    _siapkan_live_siap(monkeypatch)
    response = client.get("/api/mode/checklist?target=LIVE")
    assert response.status_code == 200
    checks = response.get_json()["checks"]
    assert "withdrawal_disabled" in checks
    assert "account_stop_enabled" in checks


def test_withdrawal_gate_fail_closed_tanpa_uji_koneksi(monkeypatch):
    """Selama Uji Koneksi belum jalan, status penarikan dianggap TIDAK aman."""
    monkeypatch.delenv("ALLOW_LIVE_WITHDRAWAL_KEY", raising=False)
    monkeypatch.setattr(dashboard, "_credentials_tested_for_active_values", lambda: False)
    assert dashboard._withdrawal_permission_safe() is False


def test_withdrawal_gate_menolak_hasil_uji_versi_lama(monkeypatch):
    """Hasil uji lama tanpa field can_withdraw tidak boleh dianggap aman."""
    monkeypatch.delenv("ALLOW_LIVE_WITHDRAWAL_KEY", raising=False)
    monkeypatch.setattr(dashboard, "_credentials_tested_for_active_values", lambda: True)
    with dashboard._credential_lock:
        dashboard._credential_test_state["account"] = {"can_trade": True}
    assert dashboard._withdrawal_permission_safe() is False


def test_withdrawal_gate_meloloskan_key_tanpa_izin_tarik(monkeypatch):
    monkeypatch.delenv("ALLOW_LIVE_WITHDRAWAL_KEY", raising=False)
    monkeypatch.setattr(dashboard, "_credentials_tested_for_active_values", lambda: True)
    with dashboard._credential_lock:
        dashboard._credential_test_state["account"] = {"can_trade": True, "can_withdraw": False}
    assert dashboard._withdrawal_permission_safe() is True


def test_withdrawal_gate_memblokir_key_dengan_izin_tarik(monkeypatch):
    monkeypatch.delenv("ALLOW_LIVE_WITHDRAWAL_KEY", raising=False)
    monkeypatch.setattr(dashboard, "_credentials_tested_for_active_values", lambda: True)
    with dashboard._credential_lock:
        dashboard._credential_test_state["account"] = {"can_trade": True, "can_withdraw": True}
    assert dashboard._withdrawal_permission_safe() is False
