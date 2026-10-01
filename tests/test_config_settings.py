from __future__ import annotations

import json

import pytest

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


def _arahkan_settings_ke_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(ss, "SETTINGS_ERROR_FILE", tmp_path / "settings.error.json")


def test_unknown_override_ditandai_rusak(tmp_path, monkeypatch):
    _arahkan_settings_ke_tmp(monkeypatch, tmp_path)
    ss.SETTINGS_FILE.write_text(json.dumps({
        "active_mode": "PAPER",
        "overrides": {"PAPER": {"KEY_TIDAK_DIKENAL": 13.0}},
    }), encoding="utf-8")
    data, errors = ss.load_mode_override("PAPER")
    assert data == {}
    assert errors
    assert ss.SETTINGS_ERROR_FILE.exists()
    assert list(tmp_path.glob("settings.json.corrupt-*"))


def test_override_is_merged_per_mode(tmp_path, monkeypatch):
    _arahkan_settings_ke_tmp(monkeypatch, tmp_path)
    ss.save_mode_override("PAPER", {"LOOP_INTERVAL_SECONDS": 9})
    paper, errors = config.build_config_for_mode("PAPER")
    live, live_errors = config.build_config_for_mode("LIVE")
    assert not errors and not live_errors
    assert paper["LOOP_INTERVAL_SECONDS"] == 9
    assert live["LOOP_INTERVAL_SECONDS"] == config.PUMP_DEFAULTS["LOOP_INTERVAL_SECONDS"]


def test_corrupt_override_is_archived_and_marked(tmp_path, monkeypatch):
    _arahkan_settings_ke_tmp(monkeypatch, tmp_path)
    ss.SETTINGS_FILE.write_text("{rusak", encoding="utf-8")
    data, errors = ss.load_mode_override("PAPER")
    assert data == {}
    assert errors
    assert ss.SETTINGS_ERROR_FILE.exists()
    assert list(tmp_path.glob("settings.json.corrupt-*"))


def test_settings_tunggal_menyimpan_mode_dan_override_per_mode(tmp_path, monkeypatch):
    _arahkan_settings_ke_tmp(monkeypatch, tmp_path)
    ss.save_runtime_mode("LIVE")
    ss.save_mode_override("PAPER", {"LOOP_INTERVAL_SECONDS": 9})
    ss.save_mode_override("LIVE", {"LOOP_INTERVAL_SECONDS": 21})
    doc = json.loads(ss.SETTINGS_FILE.read_text(encoding="utf-8"))
    assert doc["active_mode"] == "LIVE"
    assert doc["overrides"]["PAPER"]["LOOP_INTERVAL_SECONDS"] == 9
    assert doc["overrides"]["LIVE"]["LOOP_INTERVAL_SECONDS"] == 21


def test_simpan_override_tidak_menghapus_mode_aktif(tmp_path, monkeypatch):
    _arahkan_settings_ke_tmp(monkeypatch, tmp_path)
    ss.save_runtime_mode("LIVE")
    ss.save_mode_override("PAPER", {"LOOP_INTERVAL_SECONDS": 9})
    mode, errors = ss.load_runtime_mode()
    assert (mode, errors) == ("LIVE", [])


def test_simpan_mode_tidak_menghapus_override(tmp_path, monkeypatch):
    _arahkan_settings_ke_tmp(monkeypatch, tmp_path)
    ss.save_mode_override("PAPER", {"LOOP_INTERVAL_SECONDS": 9})
    ss.save_runtime_mode("LIVE")
    data, errors = ss.load_mode_override("PAPER")
    assert not errors
    assert data == {"LOOP_INTERVAL_SECONDS": 9}


def test_perpindahan_mode_ditolak_bila_mode_berubah(tmp_path, monkeypatch):
    _arahkan_settings_ke_tmp(monkeypatch, tmp_path)
    ss.save_runtime_mode("PAPER")
    with pytest.raises(ss.ConcurrentSettingsError):
        ss.save_runtime_mode("LIVE", expected_current="LIVE")
    mode, _ = ss.load_runtime_mode()
    assert mode == "PAPER"


def test_mode_sesuai_expected_diizinkan(tmp_path, monkeypatch):
    _arahkan_settings_ke_tmp(monkeypatch, tmp_path)
    ss.save_runtime_mode("PAPER")
    ss.save_runtime_mode("LIVE", expected_current="PAPER")
    mode, errors = ss.load_runtime_mode()
    assert (mode, errors) == ("LIVE", [])


def test_file_settings_hilang_memakai_default(tmp_path, monkeypatch):
    _arahkan_settings_ke_tmp(monkeypatch, tmp_path)
    mode, errors = ss.load_runtime_mode()
    assert (mode, errors) == ("PAPER", [])
    data, errors = ss.load_mode_override("PAPER")
    assert (data, errors) == ({}, [])


def test_override_read_only_ditandai_rusak(tmp_path, monkeypatch):
    _arahkan_settings_ke_tmp(monkeypatch, tmp_path)
    ss.SETTINGS_FILE.write_text(json.dumps({
        "active_mode": "PAPER",
        "overrides": {"PAPER": {"LOG_FILE": "/tmp/curang.log"}},
    }), encoding="utf-8")
    data, errors = ss.load_mode_override("PAPER")
    assert data == {}
    assert errors
    assert ss.SETTINGS_ERROR_FILE.exists()
    assert list(tmp_path.glob("settings.json.corrupt-*"))


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
