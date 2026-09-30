from __future__ import annotations

import re
from pathlib import Path

import pytest

from web import dashboard


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
    response = client.post("/api/control/prepare", json={"action": "START"},
                           headers={"X-Admin-Token": dashboard._ADMIN_TOKEN})
    assert response.status_code == 403
    assert "Origin" in response.get_json()["error"]


def test_write_rate_limit_returns_429_after_120_attempts(client):
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
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert response.headers["Cache-Control"] == "no-store"


def test_host_with_wrong_port_is_rejected(client):
    response = client.get("/api/status", base_url="http://localhost:9999")
    assert response.status_code == 400
    assert "Host" in response.get_json()["error"]


def test_dashboard_backtest_text_is_escaped_before_inner_html() -> None:
    source = (Path(__file__).resolve().parents[1] / "templates" / "dashboard.html").read_text(
        encoding="utf-8"
    )

    terlarang = [
        "${w}",
        "${l}",
        "${t.symbol}",
        "${t.reason}",
        "${t.entry_time}",
        "${t.exit_time}",
        "${x.symbol}",
        "${s.symbol}",
        "${x.holding}",
        "${x.time}",
    ]
    bocor = [pola for pola in terlarang if pola in source]
    assert not bocor, f"interpolasi mentah ke innerHTML: {bocor}"


def test_render_trade_memakai_esc_untuk_semua_field_teks() -> None:
    source = (Path(__file__).resolve().parents[1] / "templates" / "dashboard.html").read_text(
        encoding="utf-8"
    )
    awal = source.find("tb.innerHTML = trades.map")
    assert awal != -1, "blok render baris trade tidak ditemukan"
    blok = source[awal:awal + 1500]

    for field in ("t.symbol", "t.entry_time", "t.exit_time"):
        assert f"esc({field})" in blok, f"{field} tidak dibungkus esc()"

    assert "btReasonLabel(t.reason)" in blok
    assert "btReasonTagClass(t.reason)" in blok


def test_pemeta_alasan_exit_memakai_whitelist() -> None:
    source = (Path(__file__).resolve().parents[1] / "templates" / "dashboard.html").read_text(
        encoding="utf-8"
    )
    awal = source.find("function btReasonTagClass")
    assert awal != -1
    blok = source[awal:source.find("}", source.find("return", awal)) + 1]

    assert "map[key] ||" in blok, "btReasonTagClass tidak punya fallback whitelist"
    assert "return key" not in blok, (
        "btReasonTagClass mengembalikan input mentah ke atribut class"
    )


def test_dashboard_dynamic_css_classes_are_whitelisted() -> None:
    source = (Path(__file__).resolve().parents[1] / "templates" / "dashboard.html").read_text(
        encoding="utf-8"
    )
    assert 'class="logline ${safeLogClass(e.level)}"' in source
    assert "const safeLogClass" in source
    assert "const safeEntryClass" not in source


def test_backtest_ui_exposes_exit_modes_only() -> None:
    source = (Path(__file__).resolve().parents[1] / "templates" / "dashboard.html").read_text(
        encoding="utf-8"
    )
    assert 'id="btExitMode"' in source
    assert 'value="atr"' in source
    assert 'value="fixed"' in source
    assert 'id="btAtrSl"' in source
    assert 'id="btAtrTp"' in source
    assert 'id="btTradeBody"' in source
    assert 'id="btTradeCount"' in source
