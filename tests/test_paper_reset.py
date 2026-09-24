from __future__ import annotations

from copy import deepcopy
from pathlib import Path

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


def headers():
    return {"Origin": "http://localhost", "X-Admin-Token": dashboard._ADMIN_TOKEN}


def test_paper_reset_archives_files_and_saves_positive_balances(client, tmp_path, monkeypatch):
    account = tmp_path / "paper-account.json"
    bot_state = tmp_path / "paper-state.json"
    control = tmp_path / "paper-control.json"
    account.write_text('{"balances":{"USDT":10}}\n', encoding="utf-8")
    bot_state.write_text('{"current_symbol":null}\n', encoding="utf-8")

    monkeypatch.setitem(dashboard.PUMP_CONFIG, "MODE", "PAPER")
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "PAPER_ACCOUNT_STATE_FILE", str(account))
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "STATE_FILE", str(bot_state))
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "CONTROL_FILE", str(control))
    monkeypatch.setattr(dashboard._process_manager, "status", lambda mode=None: {"status": "STOPPED"})

    defaults = deepcopy(dashboard.config_mod.default_config_for_mode("PAPER"))
    current = deepcopy(defaults)
    monkeypatch.setattr(dashboard, "_config_pair", lambda mode: (defaults, current, []))
    saved = {}
    monkeypatch.setattr(dashboard, "save_mode_override", lambda mode, values: saved.update(mode=mode, values=values))
    monkeypatch.setattr(dashboard.config_mod, "reload_config", lambda: dashboard.PUMP_CONFIG)
    monkeypatch.setattr(dashboard, "_refresh_runtime_globals", lambda: None)
    monkeypatch.setattr(dashboard, "audit_change", lambda event: None)

    prepared = client.post("/api/paper/reset/prepare", json={"balances": {"USDT": 2500}}, headers=headers())
    assert prepared.status_code == 200
    confirmation = prepared.get_json()["confirmation_id"]
    committed = client.post("/api/paper/reset/commit",
                            json={"confirmation_id": confirmation, "phrase": "RESET"}, headers=headers())
    assert committed.status_code == 200
    assert not account.exists() and not bot_state.exists()
    assert list(tmp_path.glob("paper-account.json.bak-*"))
    assert list(tmp_path.glob("paper-state.json.bak-*"))
    assert saved["mode"] == "PAPER"
    assert saved["values"]["PAPER_INITIAL_BALANCES"] == {"USDT": 2500.0}


def test_paper_reset_restores_archives_if_settings_write_fails(client, tmp_path, monkeypatch):
    account = tmp_path / "account.json"
    bot_state = tmp_path / "state.json"
    account.write_text("account-old", encoding="utf-8")
    bot_state.write_text("state-old", encoding="utf-8")
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "MODE", "PAPER")
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "PAPER_ACCOUNT_STATE_FILE", str(account))
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "STATE_FILE", str(bot_state))
    monkeypatch.setattr(dashboard._process_manager, "status", lambda mode=None: {"status": "STOPPED"})
    defaults = deepcopy(dashboard.config_mod.default_config_for_mode("PAPER"))
    monkeypatch.setattr(dashboard, "_config_pair", lambda mode: (defaults, deepcopy(defaults), []))
    monkeypatch.setattr(dashboard, "save_mode_override", lambda mode, values: (_ for _ in ()).throw(OSError("disk penuh")))

    prepared = client.post("/api/paper/reset/prepare", json={"balances": {"USDT": 5}}, headers=headers())
    confirmation = prepared.get_json()["confirmation_id"]
    response = client.post("/api/paper/reset/commit",
                           json={"confirmation_id": confirmation, "phrase": "RESET"}, headers=headers())
    assert response.status_code == 500
    assert account.read_text(encoding="utf-8") == "account-old"
    assert bot_state.read_text(encoding="utf-8") == "state-old"


def test_paper_reset_rejects_nonpositive_balance(client, monkeypatch):
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "MODE", "PAPER")
    monkeypatch.setattr(dashboard._process_manager, "status", lambda mode=None: {"status": "STOPPED"})
    response = client.post("/api/paper/reset/prepare", json={"balances": {"USDT": 0}}, headers=headers())
    assert response.status_code == 400


def test_paper_reset_requires_stopped_bot(client, monkeypatch):
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "MODE", "PAPER")
    monkeypatch.setattr(dashboard._process_manager, "status", lambda mode=None: {"status": "RUNNING"})
    response = client.post("/api/paper/reset/prepare", json={"balances": {"USDT": 1}}, headers=headers())
    assert response.status_code == 409
