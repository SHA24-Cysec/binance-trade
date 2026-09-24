from __future__ import annotations

import pytest

import dashboard


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
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "MAX_POSITION_USDT", 10.0)
    monkeypatch.setattr(dashboard._process_manager, "status", lambda mode=None: {"status": "STOPPED"})
    monkeypatch.setattr(dashboard._process_manager, "position", lambda mode=None: {
        "has_position": True, "symbol": "ARBUSDT", "qty": 1.0, "entry_price": 1.0
    })
    monkeypatch.setattr(dashboard, "_credentials_tested_for_active_values", lambda: True)
    response = client.post("/api/mode/prepare", json={"target": target}, headers=headers())
    assert response.status_code == 409
    assert "Posisi" in response.get_json()["error"]


def test_live_commit_requires_exact_live_phrase(client, monkeypatch):
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "MODE", "PAPER")
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "API_KEY", "key")
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "API_SECRET", "secret")
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "MAX_POSITION_USDT", 10.0)
    monkeypatch.setattr(dashboard._process_manager, "status", lambda mode=None: {"status": "STOPPED"})
    monkeypatch.setattr(dashboard._process_manager, "position", lambda mode=None: {"has_position": False})
    monkeypatch.setattr(dashboard, "_credentials_tested_for_active_values", lambda: True)
    original_build = dashboard.config_mod.build_config_for_mode
    def live_config(mode):
        cfg, errors = original_build(mode)
        if mode == "LIVE":
            cfg["MAX_POSITION_USDT"] = 10.0
        return cfg, errors
    monkeypatch.setattr(dashboard.config_mod, "build_config_for_mode", live_config)
    prepared = client.post("/api/mode/prepare", json={"target": "LIVE"}, headers=headers())
    assert prepared.status_code == 200
    confirmation = prepared.get_json()["confirmation_id"]
    response = client.post("/api/mode/commit", json={"confirmation_id": confirmation, "phrase": "live"}, headers=headers())
    assert response.status_code == 400
    assert "LIVE" in response.get_json()["error"]
