"""Regression test tata letak folder runtime.

Semua file log wajib berada di logs/ dan semua file state/kontrol/settings
wajib berada di data/, sesuai definisi tunggal di infrastructure/paths.py.
Test ini menjaga agar tidak ada penulis file yang kembali menumpuk artefak
runtime di akar repository.
"""

from __future__ import annotations

from pathlib import Path

from config import config
from infrastructure.paths import DATA_DIR, LOGS_DIR


def test_semua_file_log_berada_di_folder_logs():
    for mode in ("PAPER", "LIVE"):
        cfg = config.build_config_for_mode(mode)[0]
        assert Path(cfg["LOG_FILE"]).parent == LOGS_DIR, mode


def test_file_log_per_mode_terpisah():
    paper = Path(config.default_config_for_mode("PAPER")["LOG_FILE"])
    live = Path(config.default_config_for_mode("LIVE")["LOG_FILE"])
    assert paper != live
    assert paper.name == "pump_bot_paper.log"
    assert live.name == "pump_bot_live.log"


def test_semua_file_state_berada_di_folder_data():
    for mode in ("PAPER", "LIVE"):
        cfg = config.build_config_for_mode(mode)[0]
        for key in ("STATE_FILE", "CONTROL_FILE", "PAPER_ACCOUNT_STATE_FILE",
                    "RATE_LIMIT_STATE_FILE", "BACKTEST_CACHE_FILE"):
            assert Path(cfg[key]).parent == DATA_DIR, (mode, key)


def test_file_state_per_mode_terpisah():
    paper = Path(config.default_config_for_mode("PAPER")["STATE_FILE"])
    live = Path(config.default_config_for_mode("LIVE")["STATE_FILE"])
    assert paper != live
    assert paper.name == "pump_bot_state_paper.json"
    assert live.name == "pump_bot_state_live.json"


def test_lock_dan_proses_file_berada_di_folder_data():
    from infrastructure.process import runtime_control as rc
    for mode in ("PAPER", "LIVE"):
        assert rc.lock_file(mode).parent == DATA_DIR
        assert rc.process_file(mode).parent == DATA_DIR
        assert rc.reclaim_lock_file(mode).parent == DATA_DIR


def test_file_watchlist_berada_di_folder_data():
    from automation import watchlist_auto
    path = Path(watchlist_auto._auto_file(dict(config.PUMP_CONFIG)))
    assert path.parent == DATA_DIR
    assert "paper" in path.name


def test_file_settings_dan_audit_di_folder_yang_tepat():
    from config import settings_schema as ss
    assert ss.SETTINGS_FILE.parent == DATA_DIR
    assert ss.SETTINGS_FILE.name == "settings.json"
    assert ss.SETTINGS_ERROR_FILE.parent == DATA_DIR
    assert ss.AUDIT_FILE.parent == LOGS_DIR
    assert ss.AUDIT_FILE.name == "settings_audit.log"


def test_tidak_ada_nama_file_runtime_lama_di_kode_sumber():
    import automation
    import backtesting
    import config as config_pkg
    import infrastructure
    import trading
    import web
    nama_lama = (
        "pump_bot_runtime.json",
        "pump_bot_settings_",
        "pump_bot_settings_audit.log",
    )
    for paket in (automation, backtesting, config_pkg, infrastructure, trading, web):
        for py in Path(paket.__file__).parent.rglob("*.py"):
            teks = py.read_text(encoding="utf-8")
            for nama in nama_lama:
                assert nama not in teks, f"{py} masih memuat nama file lama: {nama}"
