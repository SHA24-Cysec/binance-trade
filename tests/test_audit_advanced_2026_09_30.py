"""Regresi audit lanjutan 2026-09-30.

Setiap test di file ini GAGAL pada kode sebelum perbaikan audit dan LULUS
setelahnya. Urutan penomoran mengikuti laporan audit (KRITIS-01, TINGGI-01, dst).
"""

from __future__ import annotations

import io
import logging
from unittest import mock

import pytest
import requests

from config import config as config_mod
from infrastructure.storage import state as state_mod
from trading import pump_scanner_bot as bot
from trading.clients.binance_client import BinanceSpotClient


def _state_with_position() -> dict:
    st = dict(bot.DEFAULT_STATE)
    st["current_symbol"] = "XXXUSDT"
    st["qty"] = 10.0
    st["entry_price"] = 100.0
    st["peak_equity"] = 1000.0
    st["day_start_equity"] = 1000.0
    st["day_start_date"] = state_mod.today_str()
    return st


def test_default_config_mengaktifkan_minimal_satu_rem_akun():
    cfg = config_mod.PUMP_CONFIG
    assert cfg.get("USE_EQUITY_STOP") or cfg.get("USE_DAILY_STOP"), (
        "USE_EQUITY_STOP dan USE_DAILY_STOP dua-duanya False pada default config: "
        "bot berjalan tanpa rem kerugian tingkat akun."
    )


def test_close_all_at_limit_bekerja_pada_default_config():
    cfg = dict(config_mod.PUMP_CONFIG)
    cfg["CLOSE_ALL_AT_LIMIT"] = True
    st = _state_with_position()

    paused = bot.update_equity_controls(st, 100.0, cfg)
    assert paused, "kill switch tidak aktif meski equity turun 90%"

    calls = []
    with mock.patch.object(bot, "close_position",
                           side_effect=lambda *a, **k: calls.append(a)):
        bot.maybe_force_close_at_risk_limit(object(), cfg, {}, st, paused, 50.0)
    assert calls, "CLOSE_ALL_AT_LIMIT tidak menutup posisi saat limit risiko tercapai"


def test_live_tanpa_rem_akun_ditolak_start():
    cfg = dict(config_mod.PUMP_CONFIG)
    cfg["MODE"] = "LIVE"
    cfg["USE_EQUITY_STOP"] = False
    cfg["USE_DAILY_STOP"] = False
    ok, reason = bot.account_risk_gate(cfg)
    assert ok is False
    assert "USE_EQUITY_STOP" in reason


def test_live_dengan_satu_rem_aktif_boleh_jalan():
    cfg = dict(config_mod.PUMP_CONFIG)
    cfg["MODE"] = "LIVE"
    cfg["USE_EQUITY_STOP"] = True
    cfg["USE_DAILY_STOP"] = False
    ok, _ = bot.account_risk_gate(cfg)
    assert ok is True


def test_paper_tanpa_rem_akun_tetap_boleh_jalan():
    cfg = dict(config_mod.PUMP_CONFIG)
    cfg["MODE"] = "PAPER"
    cfg["USE_EQUITY_STOP"] = False
    cfg["USE_DAILY_STOP"] = False
    ok, _ = bot.account_risk_gate(cfg)
    assert ok is True


def test_gate_bisa_dilewati_dengan_override_eksplisit(monkeypatch):
    monkeypatch.setenv("ALLOW_LIVE_WITHOUT_ACCOUNT_STOP", "1")
    cfg = dict(config_mod.PUMP_CONFIG)
    cfg["MODE"] = "LIVE"
    cfg["USE_EQUITY_STOP"] = False
    cfg["USE_DAILY_STOP"] = False
    ok, _ = bot.account_risk_gate(cfg)
    assert ok is True, "override eksplisit operator harus dihormati"


def test_deskripsi_mode_exit_sesuai_yang_benar_benar_dipakai():
    cfg = dict(config_mod.PUMP_CONFIG)
    cfg["USE_ATR_EXIT"] = True
    st = dict(bot.DEFAULT_STATE)

    desc = bot.describe_exit_mode(cfg, st)
    assert "ATR" not in desc.upper() or "tidak aktif" in desc.lower(), (
        f"deskripsi menyesatkan: {desc!r}"
    )
    assert f"{cfg['SL_PCT']:.2f}" in desc, "deskripsi harus menyebut SL persen yang nyata dipakai"


def test_deskripsi_mode_exit_atr_saat_state_memang_atr():
    cfg = dict(config_mod.PUMP_CONFIG)
    cfg["USE_ATR_EXIT"] = True
    st = dict(bot.DEFAULT_STATE)
    st["exit_source"] = "ATR"
    st["sl_pct"] = 5.0
    desc = bot.describe_exit_mode(cfg, st)
    assert "ATR" in desc.upper()


def test_dd_stopped_lama_dilepas_saat_equity_stop_dimatikan():
    st = dict(bot.DEFAULT_STATE)
    st["dd_stopped"] = True
    st["dd_stop_until"] = 0
    st["peak_equity"] = 1000.0
    st["day_start_equity"] = 1000.0
    st["day_start_date"] = state_mod.today_str()

    cfg = dict(config_mod.PUMP_CONFIG)
    cfg["USE_EQUITY_STOP"] = False

    paused = bot.update_equity_controls(st, 1000.0, cfg)
    assert paused is False, "dd_stopped tetap menyala padahal USE_EQUITY_STOP sudah mati"
    assert st["dd_stopped"] is False


def test_dd_stop_until_hilang_diperlakukan_sebagai_kedaluwarsa():
    st = dict(bot.DEFAULT_STATE)
    st["dd_stopped"] = True
    st["dd_stop_until"] = 0
    st["peak_equity"] = 1000.0
    st["day_start_equity"] = 1000.0
    st["day_start_date"] = state_mod.today_str()

    cfg = dict(config_mod.PUMP_CONFIG)
    cfg["USE_EQUITY_STOP"] = True

    bot.update_equity_controls(st, 1000.0, cfg)
    assert st["dd_stopped"] is False, "tidak ada jalan keluar dari dd_stopped tanpa deadline"


def test_dd_cooldown_yang_masih_berjalan_tetap_dihormati():
    st = dict(bot.DEFAULT_STATE)
    st["dd_stopped"] = True
    st["dd_stop_until"] = state_mod.now_ms() + 3_600_000
    st["peak_equity"] = 1000.0
    st["day_start_equity"] = 1000.0
    st["day_start_date"] = state_mod.today_str()

    cfg = dict(config_mod.PUMP_CONFIG)
    cfg["USE_EQUITY_STOP"] = True

    paused = bot.update_equity_controls(st, 1000.0, cfg)
    assert paused is True and st["dd_stopped"] is True


def test_signature_tidak_bocor_ke_log_saat_error_jaringan():
    client = BinanceSpotClient("KEY_AAA", "SECRET_BBB", "https://api.binance.com",
                               allow_signed=True)
    client._time_offset_ms = 0

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.DEBUG)

    def boom(method, url, **kwargs):
        raise requests.exceptions.ConnectionError(
            f"HTTPSConnectionPool(host='api.binance.com', port=443): "
            f"Max retries exceeded with url: {url}"
        )

    try:
        with mock.patch.object(client.session, "request", side_effect=boom):
            with pytest.raises(requests.exceptions.RequestException):
                client._request("POST", "/api/v3/order",
                                {"symbol": "BTCUSDT", "side": "SELL"},
                                signed=True, max_retries=1)
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)

    output = buf.getvalue()
    import re as _re
    assert not _re.search(r"signature=[0-9a-fA-F]{16,}", output), \
        "nilai signature HMAC bocor ke log"
    assert "SECRET_BBB" not in output, "API secret bocor ke log"
    assert "<REDACTED>" in output, "redaksi tidak diterapkan pada jalur log ini"


def test_redaksi_menyisakan_konteks_yang_berguna():
    raw = ("HTTPSConnectionPool(host='api.binance.com', port=443): Max retries "
           "exceeded with url: https://api.binance.com/api/v3/order?symbol=BTCUSDT"
           "&timestamp=1700000000000&signature=deadbeef" + "0" * 56)
    safe = BinanceSpotClient._redact(raw)
    assert "deadbeef" not in safe, "nilai signature masih terbaca"
    assert "api.binance.com" in safe, "host hilang, nilai diagnostik berkurang"
    assert "BTCUSDT" in safe, "simbol hilang, nilai diagnostik berkurang"
    assert "signature=<REDACTED>" in safe


def test_redaksi_juga_menutup_parameter_rahasia_lain():
    for raw, bocor in [
        ("?apiKey=ABCDEF123456&x=1", "ABCDEF123456"),
        ("secret=topsecretvalue", "topsecretvalue"),
        ("token=abc.def.ghi", "abc.def.ghi"),
    ]:
        safe = BinanceSpotClient._redact(raw)
        assert bocor not in safe, f"parameter rahasia masih bocor: {raw}"


def test_setup_logging_tidak_menggandakan_handler(tmp_path):
    cfg = {"LOG_FILE": str(tmp_path / "bot.log")}
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        root.handlers = []
        bot.setup_logging(cfg)
        first = len(root.handlers)
        bot.setup_logging(cfg)
        second = len(root.handlers)
        assert first == second, (
            f"handler menumpuk: {first} -> {second}; setiap baris log akan ditulis ganda"
        )
    finally:
        for handler in root.handlers:
            try:
                handler.close()
            except Exception:
                pass
        root.handlers = saved


def _kandidat_settings(**override) -> dict:
    from config import settings_schema
    base = {key: config_mod.PUMP_CONFIG.get(key)
            for key in settings_schema.PARAMETER_SCHEMA}
    base.update(override)
    return base


def test_validasi_memperingatkan_saat_kedua_rem_akun_mati():
    from config import settings_schema
    _, errors, warnings = settings_schema.validate_candidate(
        _kandidat_settings(USE_EQUITY_STOP=False, USE_DAILY_STOP=False,
                           CLOSE_ALL_AT_LIMIT=True),
        "PAPER",
    )
    assert not errors, errors
    gabungan = " ".join(warnings)
    assert "rem kerugian tingkat akun" in gabungan
    assert "CLOSE_ALL_AT_LIMIT" in gabungan, (
        "operator harus diberi tahu bahwa CLOSE_ALL_AT_LIMIT jadi mati total"
    )


def test_validasi_tidak_memperingatkan_saat_satu_rem_aktif():
    from config import settings_schema
    _, errors, warnings = settings_schema.validate_candidate(
        _kandidat_settings(USE_EQUITY_STOP=True, USE_DAILY_STOP=False),
        "PAPER",
    )
    assert not errors, errors
    assert not any("rem kerugian tingkat akun" in w for w in warnings)


def test_validasi_memperingatkan_stop_loss_sangat_lebar():
    from config import settings_schema
    _, errors, warnings = settings_schema.validate_candidate(
        _kandidat_settings(USE_STOP_LOSS=True, SL_PCT=28.8, USE_TP=True, TP_PCT=80.0),
        "PAPER",
    )
    assert not errors, errors
    assert any("sangat lebar" in w for w in warnings)
