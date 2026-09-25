from __future__ import annotations

import re

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


def test_parameterized_post_routes_reject_missing_admin_token(client):
    # S-01: route POST dengan parameter jalur (mis. /api/backtest/cancel/<job_id>)
    # juga wajib ditolak tanpa token; sebelumnya tidak ikut diuji.
    routes = []
    for rule in dashboard.app.url_map.iter_rules():
        if "POST" in rule.methods and "<" in rule.rule:
            routes.append(re.sub(r"<[^>]+>", "dummy", rule.rule))
    assert routes
    for route in routes:
        response = client.post(route, json={}, headers=write_headers(token=False))
        assert response.status_code == 403, (route, response.status_code)
        assert "Token admin" in response.get_json()["error"]


def test_write_without_origin_and_referer_is_rejected(client):
    # S-02: POST tanpa Origin DAN tanpa Referer harus ditolak, walau tokennya benar.
    response = client.post("/api/control/prepare", json={"action": "START"},
                           headers={"X-Admin-Token": dashboard._ADMIN_TOKEN})
    assert response.status_code == 403
    assert "Origin" in response.get_json()["error"]


def test_write_rate_limit_returns_429_after_120_attempts(client):
    # S-02: batas global 120 request tulis per menit per alamat; percobaan
    # dengan token salah pun dihitung, jadi percobaan ke-121 ditolak 429
    # sebelum token sempat diperiksa.
    bad = {"Origin": "http://localhost", "X-Admin-Token": "token-salah"}
    for _ in range(120):
        response = client.post("/api/control/prepare", json={"action": "START"},
                               headers=bad)
        assert response.status_code == 403
    response = client.post("/api/control/prepare", json={"action": "START"},
                           headers=write_headers())
    assert response.status_code == 429
    assert "Terlalu banyak" in response.get_json()["error"]


def test_backtest_endpoints_return_403_in_live_mode(client, monkeypatch):
    # S-02: seluruh endpoint /api/backtest/* wajib 403 saat mode LIVE,
    # termasuk saat permintaan sudah lolos token+origin yang sah.
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "MODE", "LIVE")
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "SHOW_BACKTEST_IN_LIVE", False)
    blocked = client.get("/api/backtest/defaults")
    assert blocked.status_code == 403
    assert blocked.get_json()["backtest_disabled"] is True
    started = client.post("/api/backtest/start", json={"days": 7},
                          headers=write_headers())
    assert started.status_code == 403
    assert started.get_json()["backtest_disabled"] is True
    status = client.get("/api/backtest/status/dummy")
    assert status.status_code == 403


def test_security_headers_present_on_every_response(client):
    # S-02: header higiene wajib ada di semua response (after_request).
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert response.headers["Cache-Control"] == "no-store"


def test_host_with_wrong_port_is_rejected(client):
    # S-02: Host dengan port yang tidak cocok binding ditolak 400.
    response = client.get("/api/status", base_url="http://localhost:9999")
    assert response.status_code == 400
    assert "Host" in response.get_json()["error"]
