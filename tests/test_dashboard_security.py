from __future__ import annotations

import json

import pytest

import dashboard


@pytest.fixture()
def client():
    dashboard.app.config.update(TESTING=True)
    dashboard._confirmations.clear()
    dashboard._last_dangerous_action.clear()
    dashboard._write_attempts.clear()
    with dashboard.app.test_client() as value:
        yield value


def write_headers(*, token=True, origin="http://localhost"):
    headers = {"Origin": origin}
    if token:
        headers["X-Admin-Token"] = dashboard._ADMIN_TOKEN
    return headers


def test_every_post_route_rejects_missing_admin_token(client):
    routes = []
    for rule in dashboard.app.url_map.iter_rules():
        if "POST" in rule.methods and "<" not in rule.rule:
            routes.append(rule.rule)
    assert routes
    for route in routes:
        response = client.post(route, json={}, headers=write_headers(token=False))
        assert response.status_code == 403, (route, response.status_code, response.get_data(as_text=True))
        assert "Token admin" in response.get_json()["error"]


def test_bad_origin_is_rejected_even_with_token(client):
    response = client.post("/api/control/prepare", json={"action": "START"},
                           headers=write_headers(origin="https://evil.example"))
    assert response.status_code == 403
    assert "Origin" in response.get_json()["error"]


def test_bad_host_is_rejected(client):
    response = client.get("/api/control/status", base_url="http://evil.example")
    assert response.status_code == 400


def test_non_loopback_binding_disables_all_writes(client, monkeypatch):
    monkeypatch.setattr(dashboard, "_DASHBOARD_HOST", "0.0.0.0")
    response = client.post("/api/control/prepare", json={"action": "START"},
                           headers=write_headers())
    assert response.status_code == 403
    assert "dinonaktifkan" in response.get_json()["error"]


def test_live_mode_prepare_is_guarded_without_credentials(client, monkeypatch):
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "MODE", "PAPER")
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "API_KEY", "")
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "API_SECRET", "")
    monkeypatch.setattr(dashboard._process_manager, "status", lambda mode=None: {"status": "STOPPED"})
    monkeypatch.setattr(dashboard._process_manager, "position", lambda mode=None: {"has_position": False})
    response = client.post("/api/mode/prepare", json={"target": "LIVE"},
                           headers=write_headers())
    assert response.status_code == 409
    assert "API key" in response.get_json()["error"]


def test_process_confirmation_cannot_target_replacement_pid(client, monkeypatch):
    current = {"status": "RUNNING", "pid": 111}
    monkeypatch.setattr(dashboard._process_manager, "status", lambda mode=None: dict(current))
    monkeypatch.setattr(dashboard._process_manager, "position", lambda mode=None: {"has_position": False})
    prepared = client.post("/api/control/prepare", json={"action": "STOP"},
                           headers=write_headers())
    assert prepared.status_code == 200
    current["pid"] = 222
    response = client.post("/api/control/execute", json={
        "confirmation_id": prepared.get_json()["confirmation_id"]
    }, headers=write_headers())
    assert response.status_code == 409
    assert "PID" in response.get_json()["error"]


def test_credential_response_never_echoes_secret(client, monkeypatch):
    secret = "NEVER-ECHO-THIS-SECRET"
    key = "TEST-KEY-1234"
    monkeypatch.setattr(dashboard, "update_env", lambda **kwargs: {
        "ok": True, "platform": "posix", "message": "aman"
    })
    monkeypatch.setattr(dashboard.config_mod, "reload_config", lambda: dashboard.PUMP_CONFIG)
    monkeypatch.setattr(dashboard, "_refresh_runtime_globals", lambda: None)
    monkeypatch.setattr(dashboard, "audit_change", lambda event: None)
    response = client.post("/api/credentials/save", json={"api_key": key, "api_secret": secret},
                           headers=write_headers())
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert secret not in body
    assert key not in body
    assert "1234" in body
