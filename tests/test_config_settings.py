from __future__ import annotations

import json

import config
import settings_schema as ss


def test_schema_covers_every_final_config_key():
    assert len(config.PUMP_CONFIG) == 82
    assert set(ss.PARAMETER_SCHEMA) == set(config.PUMP_CONFIG)


def test_override_is_merged_per_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "settings_file", lambda mode: tmp_path / f"settings-{mode.lower()}.json")
    monkeypatch.setattr(ss, "settings_error_file", lambda mode: tmp_path / f"settings-{mode.lower()}.error.json")
    ss.save_mode_override("PAPER", {"RISK_PERCENT": 12.5, "LOOP_INTERVAL_SECONDS": 9})
    paper, errors = config.build_config_for_mode("PAPER")
    live, live_errors = config.build_config_for_mode("LIVE")
    assert not errors and not live_errors
    assert paper["RISK_PERCENT"] == 12.5
    assert paper["LOOP_INTERVAL_SECONDS"] == 9
    assert live["RISK_PERCENT"] == config.PUMP_DEFAULTS["RISK_PERCENT"]


def test_corrupt_override_is_archived_and_marked(tmp_path, monkeypatch):
    settings = tmp_path / "settings-paper.json"
    marker = tmp_path / "settings-paper.error.json"
    monkeypatch.setattr(ss, "settings_file", lambda mode: settings)
    monkeypatch.setattr(ss, "settings_error_file", lambda mode: marker)
    settings.write_text("{rusak", encoding="utf-8")
    data, errors = ss.load_mode_override("PAPER")
    assert data == {}
    assert errors
    assert marker.exists()
    assert list(tmp_path.glob("settings-paper.json.corrupt-*"))


def test_build_config_reports_tampered_invalid_override(tmp_path, monkeypatch):
    path = tmp_path / "settings-paper.json"
    path.write_text(json.dumps({"ATR_SL_MIN_PCT": 9, "ATR_SL_MAX_PCT": 1}), encoding="utf-8")
    monkeypatch.setattr(ss, "settings_file", lambda mode: path)
    monkeypatch.setattr(ss, "settings_error_file", lambda mode: tmp_path / "no-error.json")
    _, errors = config.build_config_for_mode("PAPER")
    assert any("ATR_SL_MIN_PCT" in item for item in errors)
    # Editor masih dapat memuat nilai invalid agar pengguna bisa memperbaikinya.
    _, structural_errors = config.build_config_for_mode("PAPER", validate=False)
    assert structural_errors == []


def test_relation_validation_and_live_position_cap():
    candidate = config.default_config_for_mode("LIVE")
    candidate["ATR_SL_MIN_PCT"] = 8
    candidate["ATR_SL_MAX_PCT"] = 2
    candidate["MAX_POSITION_USDT"] = 0
    _, errors, _ = ss.validate_candidate(candidate, "LIVE")
    assert "ATR_SL_MIN_PCT" in errors
    assert "MAX_POSITION_USDT" in errors


def test_watchlist_symbol_and_tier_validation():
    candidate = config.default_config_for_mode("PAPER")
    candidate["WATCHLIST"] = [{"symbol": " arbusdt ", "tier": "inti"}]
    cleaned, errors, _ = ss.validate_candidate(candidate, "PAPER")
    assert not errors
    assert cleaned["WATCHLIST"] == [{"symbol": "ARBUSDT", "tier": "INTI"}]

    candidate["WATCHLIST"] = [{"symbol": "../XUSDT", "tier": "INTI"}]
    _, errors, _ = ss.validate_candidate(candidate, "PAPER")
    assert "WATCHLIST" in errors


def test_schema_public_payload_never_contains_credentials():
    defaults = config.default_config_for_mode("PAPER")
    current = dict(defaults)
    defaults["API_KEY"] = "KEY-DEFAULT-SECRET"
    defaults["API_SECRET"] = "SECRET-DEFAULT"
    current["API_KEY"] = "KEY-CURRENT-SECRET"
    current["API_SECRET"] = "SECRET-CURRENT"
    payload = json.dumps(ss.public_schema(defaults, current))
    assert "KEY-DEFAULT-SECRET" not in payload
    assert "SECRET-DEFAULT" not in payload
    assert "KEY-CURRENT-SECRET" not in payload
    assert "SECRET-CURRENT" not in payload
