"""Test tampilan mode exit ATR vs persen lama di dashboard.

Latar belakang: pengguna mengira bot masih memakai persen lama karena dua
tempat di dashboard menampilkan nilai fallback SL_PCT/TP_PCT tanpa penanda
mode ATR:

1. Panel "Parameter Strategi" (configBox): tanpa posisi terbuka nilainya
   jatuh ke persen fallback dan baris mode menulis "PERSEN LAMA" mentah.
2. "Ringkasan risiko target" (checklist pindah mode): hanya menampilkan
   SL_PCT/TP_PCT, tidak pernah menampilkan status ATR.

Test di sini menjaga agar payload API dan template selalu membedakan level
yang sedang dipakai bot dari nilai fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import config
from web import dashboard

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "dashboard.html"


@pytest.fixture()
def client():
    dashboard.app.config.update(TESTING=True)
    with dashboard.app.test_client() as value:
        yield value


def test_risk_summary_menandai_atr_dan_fallback_persen():
    cfg = config.default_config_for_mode("PAPER")
    cfg["USE_ATR_EXIT"] = True
    ringkas = dashboard._risk_summary(cfg)
    assert ringkas["USE_ATR_EXIT"] is True
    assert ringkas["ATR_MULT_SL"] == cfg["ATR_MULT_SL"]
    assert ringkas["ATR_MULT_TP"] == cfg["ATR_MULT_TP"]
    assert "SL_PCT (fallback)" in ringkas
    assert "TP_PCT (fallback)" in ringkas
    # kunci tanpa label tidak boleh ikut supaya tidak tertukar
    assert "SL_PCT" not in ringkas
    assert "TP_PCT" not in ringkas


def test_risk_summary_tanpa_atr_memakai_persen_biasa():
    cfg = config.default_config_for_mode("PAPER")
    cfg["USE_ATR_EXIT"] = False
    ringkas = dashboard._risk_summary(cfg)
    assert ringkas["USE_ATR_EXIT"] is False
    assert ringkas["SL_PCT"] == cfg["SL_PCT"]
    assert ringkas["TP_PCT"] == cfg["TP_PCT"]
    assert "SL_PCT (fallback)" not in ringkas
    assert "ATR_MULT_SL" not in ringkas
    assert "ATR_MULT_TP" not in ringkas


def test_status_menyertakan_penanda_mode_exit(client):
    response = client.get("/api/status")
    assert response.status_code == 200
    c = response.get_json()["config"]
    assert c["use_atr_exit"] == bool(dashboard.PUMP_CONFIG.get("USE_ATR_EXIT"))
    assert c["atr_mult_sl"] == dashboard.PUMP_CONFIG.get("ATR_MULT_SL")
    assert c["atr_mult_tp"] == dashboard.PUMP_CONFIG.get("ATR_MULT_TP")


def test_checklist_menyertakan_mode_exit(client):
    response = client.get("/api/mode/checklist?target=PAPER")
    assert response.status_code == 200
    risk = response.get_json()["risk"]
    assert "USE_ATR_EXIT" in risk
    if risk["USE_ATR_EXIT"]:
        assert "ATR_MULT_SL" in risk and "ATR_MULT_TP" in risk
        assert "SL_PCT (fallback)" in risk


def test_template_membedakan_mode_exit_dan_fallback():
    src = TEMPLATE.read_text(encoding="utf-8")
    # label tiga keadaan baru wajib ada
    assert "'ATR (belum ada posisi)'" in src
    assert "'PERSEN LAMA (posisi ini)'" in src
    assert "(fallback)" in src
    # BE dan trailing wajib memakai satuan dinamis (harga saat ATR), bukan % mentah
    assert "${esc(c.be_trigger_pct)}${satuan}" in src
    assert "${esc(c.trailing_start_pct)}${satuan}" in src


def test_template_tidak_meninggalkan_persen_mentah_saat_atr():
    # Baris lama yang menampilkan BE/trailing dengan '%' tetap tidak boleh balik
    # dalam bentuk aslinya (satuan salah saat posisi ATR).
    src = TEMPLATE.read_text(encoding="utf-8")
    assert "${esc(c.be_trigger_pct)}%" not in src
    assert "${esc(c.trailing_start_pct)}%" not in src
