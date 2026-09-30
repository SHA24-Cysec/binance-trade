from __future__ import annotations

import json

from config import config
from config import settings_schema as ss


def test_schema_covers_every_final_config_key():
    assert len(config.PUMP_CONFIG) == len(ss.PARAMETER_SCHEMA)
    assert set(ss.PARAMETER_SCHEMA) == set(config.PUMP_CONFIG)


def test_pengaturan_monitoring_ada_di_schema():
    for key in ("PUMP_MIN_24H_CHANGE_PCT", "PUMP_VOLUME_SURGE_MULT",
                "BTC_FILTER_ENABLED", "BTC_MAX_DROP_PCT", "BTC_LOOKBACK_BARS"):
        assert key in ss.PARAMETER_SCHEMA
        assert key in config.PUMP_CONFIG


def test_unknown_override_ditandai_rusak(tmp_path, monkeypatch):
    settings = tmp_path / "settings-paper.json"
    marker = tmp_path / "settings-paper.error.json"
    monkeypatch.setattr(ss, "settings_file", lambda mode: settings)
    monkeypatch.setattr(ss, "settings_error_file", lambda mode: marker)
    settings.write_text(json.dumps({"KEY_TIDAK_DIKENAL": 13.0}), encoding="utf-8")
    data, errors = ss.load_mode_override("PAPER")
    assert data == {}
    assert errors
    assert marker.exists()
    assert list(tmp_path.glob("settings-paper.json.corrupt-*"))


def test_override_is_merged_per_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "settings_file", lambda mode: tmp_path / f"settings-{mode.lower()}.json")
    monkeypatch.setattr(ss, "settings_error_file", lambda mode: tmp_path / f"settings-{mode.lower()}.error.json")
    ss.save_mode_override("PAPER", {"LOOP_INTERVAL_SECONDS": 9})
    paper, errors = config.build_config_for_mode("PAPER")
    live, live_errors = config.build_config_for_mode("LIVE")
    assert not errors and not live_errors
    assert paper["LOOP_INTERVAL_SECONDS"] == 9
    assert live["LOOP_INTERVAL_SECONDS"] == config.PUMP_DEFAULTS["LOOP_INTERVAL_SECONDS"]


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


def test_relation_validation_exit():
    candidate = config.default_config_for_mode("PAPER")
    candidate["TP_PCT"] = 0
    _, errors, _ = ss.validate_candidate(candidate, "PAPER")
    assert "TP_PCT" in errors


def test_watchlist_manual_dihapus_dari_schema_dan_config():
    assert "WATCHLIST" not in config.PUMP_CONFIG
    assert "WATCHLIST" not in ss.PARAMETER_SCHEMA
    candidate = config.default_config_for_mode("PAPER")
    candidate["WATCHLIST_TOP_N"] = 10
    cleaned, errors, _ = ss.validate_candidate(candidate, "PAPER")
    assert not errors
    assert cleaned["WATCHLIST_TOP_N"] == 10
    candidate["WATCHLIST_TOP_N"] = 0
    _, errors, _ = ss.validate_candidate(candidate, "PAPER")
    assert "WATCHLIST_TOP_N" in errors


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
