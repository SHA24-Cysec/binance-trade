from __future__ import annotations

import json

import config
import settings_schema as ss


def test_schema_covers_every_final_config_key():
    # BACKTEST_INITIAL_EQUITY_USDT ditambahkan agar sizing backtest dapat
    # direproduksi tanpa membaca saldo LIVE.
    assert len(config.PUMP_CONFIG) == 91
    assert set(ss.PARAMETER_SCHEMA) == set(config.PUMP_CONFIG)


def test_kunci_strategi_lama_benar_benar_hilang():
    """MIN_PUMP_PCT_24H dan kawan-kawan dihapus total, bukan disembunyikan."""
    for kunci in ("MIN_PUMP_PCT_24H", "MOMENTUM_FADE_EXIT", "MOMENTUM_FADE_RANK_THRESHOLD"):
        assert kunci not in config.PUMP_CONFIG
        assert kunci not in config.PUMP_DEFAULTS
        assert kunci not in ss.PARAMETER_SCHEMA


def test_parameter_setup_baru_ada_di_schema():
    baru = ("SWING_LOOKBACK_BARS", "SWING_PIVOT_WING_BARS", "BREAKOUT_BUFFER_ATR_MULT",
            "RETEST_ZONE_ATR_MULT", "RETEST_VWAP_CONFLUENCE_ATR_MULT",
            "VWAP_MIN_BARS_AFTER_ANCHOR", "MAX_BARS_BREAKOUT_TO_RETEST",
            "MAX_RETEST_TOUCHES", "INVALIDATION_ATR_MULT", "MAX_EXTENSION_ATR_MULT",
            "MIN_CLOSE_POSITION_IN_RANGE", "SETUP_INVALIDATION_EXIT")
    for kunci in baru:
        assert kunci in ss.PARAMETER_SCHEMA, kunci
        assert kunci in config.PUMP_CONFIG, kunci


def test_override_lama_yang_memuat_kunci_terhapus_tidak_dianggap_rusak(tmp_path, monkeypatch):
    settings = tmp_path / "settings-paper.json"
    marker = tmp_path / "settings-paper.error.json"
    monkeypatch.setattr(ss, "settings_file", lambda mode: settings)
    monkeypatch.setattr(ss, "settings_error_file", lambda mode: marker)
    settings.write_text(json.dumps({"MIN_PUMP_PCT_24H": 13.0,
                                    "MOMENTUM_FADE_EXIT": True,
                                    "RISK_PERCENT": 12.5}), encoding="utf-8")
    data, errors = ss.load_mode_override("PAPER")
    # Kunci lama dibuang, setelan yang masih sah dipertahankan, dan file
    # TIDAK diarsipkan sebagai korup.
    assert data == {"RISK_PERCENT": 12.5}
    assert any("dihapus" in e for e in errors)
    assert not list(tmp_path.glob("settings-paper.json.corrupt-*"))


def test_relasi_invalidasi_tidak_boleh_lebih_dangkal_dari_zona_retest():
    kandidat = dict(config.PUMP_CONFIG)
    kandidat["RETEST_ZONE_ATR_MULT"] = 1.5
    kandidat["INVALIDATION_ATR_MULT"] = 0.5
    _cleaned, errors, _warn = ss.validate_candidate(kandidat, "PAPER")
    assert "INVALIDATION_ATR_MULT" in errors


def test_relasi_lookback_minimum_mengikuti_struktur_setup():
    kandidat = dict(config.PUMP_CONFIG)
    kandidat["CONFIRM_LOOKBACK_BARS"] = 5
    _cleaned, errors, _warn = ss.validate_candidate(kandidat, "PAPER")
    assert "CONFIRM_LOOKBACK_BARS" in errors


def test_tier_watchlist_lama_dimigrasikan():
    assert config.migrate_watchlist_tier("MOMENTUM") == "AKTIF"
    assert config.migrate_watchlist_tier("momentum") == "AKTIF"
    assert config.migrate_watchlist_tier("INTI") == "INTI"
    kandidat = dict(config.PUMP_CONFIG)
    kandidat["WATCHLIST"] = [{"symbol": "BTCUSDT", "tier": "MOMENTUM", "score": 80.0}]
    cleaned, errors, _warn = ss.validate_candidate(kandidat, "PAPER")
    assert not errors, errors
    assert cleaned["WATCHLIST"][0]["tier"] == "AKTIF"


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
