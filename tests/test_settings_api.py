from __future__ import annotations

from copy import deepcopy

import pytest

import config
import dashboard
import settings_schema as ss


@pytest.fixture()
def client():
    dashboard.app.config.update(TESTING=True)
    dashboard._confirmations.clear()
    dashboard._last_dangerous_action.clear()
    dashboard._write_attempts.clear()
    with dashboard.app.test_client() as value:
        yield value


def headers():
    return {"Origin": "http://localhost", "X-Admin-Token": dashboard._ADMIN_TOKEN}


def test_invalid_settings_never_create_confirmation(client):
    response = client.post("/api/settings/preview", json={
        "mode": "PAPER", "values": {"CONFIRM_LOOKBACK_BARS": 1}
    }, headers=headers())
    assert response.status_code == 400
    assert "CONFIRM_LOOKBACK_BARS" in response.get_json()["fields"]
    assert not dashboard._confirmations


def test_settings_preview_then_commit_is_transactional(client, monkeypatch):
    defaults = config.default_config_for_mode("PAPER")
    current = deepcopy(defaults)
    monkeypatch.setattr(dashboard, "_config_pair", lambda mode: (deepcopy(defaults), deepcopy(current), []))
    monkeypatch.setattr(dashboard._process_manager, "status", lambda mode=None: {"status": "STOPPED"})
    saved = {}
    audits = []
    monkeypatch.setattr(dashboard, "save_mode_override", lambda mode, values, **kwargs: saved.update(mode=mode, values=values))
    monkeypatch.setattr(dashboard, "audit_change", audits.append)
    monkeypatch.setattr(dashboard.config_mod, "reload_config", lambda: dashboard.PUMP_CONFIG)
    monkeypatch.setattr(dashboard, "_refresh_runtime_globals", lambda: None)

    prepared = client.post("/api/settings/preview", json={
        "mode": "PAPER", "values": {"RISK_PERCENT": 2.5}, "position_policy": "REQUIRE_EMPTY"
    }, headers=headers())
    assert prepared.status_code == 200
    data = prepared.get_json()
    assert data["diff"][0]["key"] == "RISK_PERCENT"
    committed = client.post("/api/settings/commit", json={
        "confirmation_id": data["confirmation_id"]
    }, headers=headers())
    assert committed.status_code == 200
    assert saved["mode"] == "PAPER"
    assert saved["values"]["RISK_PERCENT"] == 2.5
    assert audits and audits[0]["event"] == "SETTING_CHANGED"


def test_settings_write_failure_is_reported_without_audit(client, monkeypatch):
    defaults = config.default_config_for_mode("PAPER")
    monkeypatch.setattr(dashboard, "_config_pair", lambda mode: (deepcopy(defaults), deepcopy(defaults), []))
    monkeypatch.setattr(dashboard._process_manager, "status", lambda mode=None: {"status": "STOPPED"})
    monkeypatch.setattr(dashboard, "save_mode_override", lambda mode, values, **kwargs: (_ for _ in ()).throw(OSError("disk penuh")))
    audits = []
    monkeypatch.setattr(dashboard, "audit_change", audits.append)
    prepared = client.post("/api/settings/preview", json={
        "mode": "PAPER", "values": {"RISK_PERCENT": 2.0}
    }, headers=headers())
    response = client.post("/api/settings/commit", json={
        "confirmation_id": prepared.get_json()["confirmation_id"]
    }, headers=headers())
    assert response.status_code == 500
    assert audits == []


def test_live_risk_relaxation_requires_phrase(client, monkeypatch):
    defaults = config.default_config_for_mode("LIVE")
    defaults["MAX_POSITION_USDT"] = 100
    current = deepcopy(defaults)
    monkeypatch.setattr(dashboard, "_config_pair", lambda mode: (deepcopy(defaults), deepcopy(current), []))
    monkeypatch.setattr(dashboard._process_manager, "status", lambda mode=None: {"status": "STOPPED"})

    prepared = client.post("/api/settings/preview", json={
        "mode": "LIVE", "values": {"RISK_PERCENT": current["RISK_PERCENT"] + 1}
    }, headers=headers())
    assert prepared.status_code == 200
    data = prepared.get_json()
    assert data["requires_risk_phrase"] is True
    response = client.post("/api/settings/commit", json={
        "confirmation_id": data["confirmation_id"], "risk_phrase": "saya paham"
    }, headers=headers())
    assert response.status_code == 400
    assert "SAYA PAHAM" in response.get_json()["error"]


def test_risk_relaxation_detection_covers_core_guards():
    old = config.default_config_for_mode("LIVE")
    old["MAX_POSITION_USDT"] = 100
    new = deepcopy(old)
    new["MAX_POSITION_USDT"] = 150
    new["RISK_PERCENT"] = old["RISK_PERCENT"] + 1
    new["USE_DAILY_STOP"] = False
    warnings = ss.dangerous_relaxations(old, new)
    assert any("MAX_POSITION_USDT" in item for item in warnings)
    assert any("RISK_PERCENT" in item for item in warnings)
    assert any("Daily Stop" in item for item in warnings)
