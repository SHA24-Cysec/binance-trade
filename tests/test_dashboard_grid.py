"""Tes route grid search dashboard (grid di atas backtest portofolio).

Ditambahkan 2026-10-01 bersama integrasi grid search ke dashboard.

Yang dijaga:
  1. Route tunduk pada rem yang sama dengan backtest portofolio:
     ditolak saat mode LIVE, wajib token admin, satu job berjalan.
  2. Validasi masukan ketat: hanya parameter exit numerik yang boleh
     di-grid, nilai harus angka, batas kombinasi ditegakkan di server.
  3. Hasil job grid bisa diambil lewat route status yang sama.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from web import dashboard


@pytest.fixture()
def client():
    dashboard.app.config.update(TESTING=True)
    dashboard._confirmations.clear()
    dashboard._last_dangerous_action.clear()
    dashboard._write_attempts.clear()
    with dashboard._bt_jobs_lock:
        dashboard._bt_jobs.clear()
    with dashboard.app.test_client() as value:
        yield value
    with dashboard._bt_jobs_lock:
        dashboard._bt_jobs.clear()


def write_headers(*, token=True, origin="http://localhost"):
    headers = {"Origin": origin}
    if token:
        headers["X-Admin-Token"] = dashboard._ADMIN_TOKEN
    return headers


def test_grid_start_ditolak_saat_mode_live(client, monkeypatch):
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "MODE", "LIVE")
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "SHOW_BACKTEST_IN_LIVE", False)
    blocked = client.post("/api/backtest/grid/start", json={"days": 7},
                          headers=write_headers())
    assert blocked.status_code == 403
    assert blocked.get_json()["backtest_disabled"] is True


def test_grid_start_validasi_masukan_sisi_server(client):
    nilai_13 = [float(i) for i in range(1, 14)]
    kasus = [
        ({"days": 1, "spec": {"ATR_MULT_SL": [12.0]}}, "minimal 2"),
        ({"days": 30, "max_symbols": 1, "spec": {"ATR_MULT_SL": [12.0]}}, "2 dan 600"),
        ({"days": 200, "max_symbols": 600, "spec": {"ATR_MULT_SL": [12.0]}}, "terlalu besar"),
        ({"days": 30}, "Spec grid kosong"),
        ({"days": 30, "spec": {}}, "Spec grid kosong"),
        ({"days": 30, "spec": {"KUNCI_PALSU": [1.0]}}, "tidak diperbolehkan"),
        ({"days": 30, "spec": {"ATR_MULT_SL": [12.0, "dua"]}}, "harus angka"),
        ({"days": 30, "spec": {"ATR_MULT_SL": [True]}}, "harus angka"),
        ({"days": 30, "spec": {"ATR_MULT_SL": [float(i) for i in range(1, 52)]}}, "maksimal 50"),
        ({"days": 30, "spec": {"ATR_MULT_SL": nilai_13, "ATR_MULT_TP": nilai_13}}, "melebihi batas"),
        ({"days": 30, "spec": {"ATR_MULT_SL": [12.0]}, "metrik": "pnl"}, "tidak dikenal"),
        ({"days": 30, "spec": {"ATR_MULT_SL": [12.0]}, "rasio_latih": 1.5}, "0.1 dan 1.0"),
        ({"days": 30, "spec": {"ATR_MULT_SL": [12.0]}, "rasio_latih": "abc"}, "tidak valid"),
        ({"days": 30, "spec": {"ATR_MULT_SL": [12.0]}, "min_trades": 0}, "1 dan 1000"),
    ]
    for payload, pesan in kasus:
        response = client.post("/api/backtest/grid/start", json=payload,
                               headers=write_headers())
        assert response.status_code == 400, (payload, response.get_data(as_text=True))
        assert pesan in response.get_json()["error"], (payload, response.get_json()["error"])


def test_grid_start_menolak_parameter_mati_sesuai_mode(client, monkeypatch):
    # Mode bawaan konfigurasi adalah exit ATR: parameter persen tidak
    # berpengaruh apa pun dan harus ditolak dengan pesan yang menjelaskan.
    r = client.post("/api/backtest/grid/start",
                    json={"days": 30, "spec": {"SL_PCT": [1.0], "TP_PCT": [2.0]}},
                    headers=write_headers())
    assert r.status_code == 400
    assert "tidak berpengaruh" in r.get_json()["error"]
    assert "ATR" in r.get_json()["error"]

    monkeypatch.setitem(dashboard.PUMP_CONFIG, "USE_ATR_EXIT", False)
    r = client.post("/api/backtest/grid/start",
                    json={"days": 30, "spec": {"ATR_MULT_SL": [12.0]}},
                    headers=write_headers())
    assert r.status_code == 400
    assert "tidak berpengaruh" in r.get_json()["error"]


def test_grid_start_menerima_spec_sah_lalu_status_bisa_dipoll(client, monkeypatch):
    def stub_job(job_id, days, max_symbols, spec, rasio_latih, metrik,
                 min_trades, total_kombinasi):
        with dashboard._bt_jobs_lock:
            dashboard._bt_jobs[job_id].update({
                "status": "done", "progress": 1.0, "stage": "selesai",
                "result": {
                    "kind": "grid", "mode": "grid",
                    "grid": {"total_kombinasi": total_kombinasi},
                    "rows": [],
                },
            })

    monkeypatch.setattr(dashboard, "_bt_run_grid_job", stub_job)
    response = client.post(
        "/api/backtest/grid/start",
        json={"days": 7, "max_symbols": 10,
              "spec": {"ATR_MULT_SL": [12.0, 16.0], "ATR_MULT_TP": [24.0, 48.0]},
              "rasio_latih": 0.7, "metrik": "total_return_pct", "min_trades": 1},
        headers=write_headers())
    assert response.status_code == 200
    job_id = response.get_json()["job_id"]

    body = None
    for _ in range(40):
        status = client.get(f"/api/backtest/status/{job_id}")
        assert status.status_code == 200
        body = status.get_json()
        if body.get("status") != "running":
            break
        time.sleep(0.05)
    assert body and body.get("status") == "done"
    assert body["result"]["kind"] == "grid"
    assert body["result"]["grid"]["total_kombinasi"] == 4


def test_grid_start_satu_job_berjalan_untuk_semua_mode(client):
    with dashboard._bt_jobs_lock:
        dashboard._bt_jobs["berjalan"] = {
            "status": "running", "progress": 0.5, "stage": "x",
            "created_at": time.time(), "cancel": False,
        }
    response = client.post("/api/backtest/grid/start",
                           json={"days": 7, "spec": {"ATR_MULT_SL": [12.0]}},
                           headers=write_headers())
    assert response.status_code == 429
    portfolio = client.post("/api/backtest/start", json={"days": 7},
                            headers=write_headers())
    assert portfolio.status_code == 429


def test_defaults_kini_memuat_info_grid(client):
    response = client.get("/api/backtest/defaults")
    assert response.status_code == 200
    data = response.get_json()
    assert data["grid_max_kombinasi"] == dashboard.gs.MAX_KOMBINASI_PORTFOLIO
    assert "TP_PCT" in data["grid_params"]
    assert "USE_ATR_EXIT" not in data["grid_params"], \
        "boolean mode exit tidak boleh bisa di-grid"
    assert "total_return_pct" in data["grid_metrik"]


def test_template_memuat_elemen_dan_route_grid():
    source = (Path(__file__).resolve().parents[1] / "templates" / "dashboard.html").read_text(
        encoding="utf-8")
    for penanda in (
        'id="btModeGrid"',
        'id="btGridRows"',
        'id="btGridBody"',
        'id="btGridBox"',
        "/api/backtest/grid/start",
        "renderGridResult",
        "btPakaiGridParams",
        ">PF L/U<",
        ">Win rate L/U<",
        'colspan="11"',
        "BT_GRID_PARAMS_ATR",
        "BT_GRID_PARAMS_PERSEN",
        "btGridSeedDefaultRow",
        "btGridParamsForMode",
    ):
        assert penanda in source, penanda


# ---------------------------------------------------------------------------
# End-to-end job grid tanpa jaringan (klien Binance palsu, pola
# test_dashboard_backtest_job.py): payload kind=grid lengkap dan file
# temporary selalu dibersihkan.
# ---------------------------------------------------------------------------

MS_PER_5M = 5 * 60 * 1000


class KlienBinancePalsu:
    def __init__(self, symbols):
        self.symbols = list(symbols)
        self.jumlah_request = 0

    def get_ticker_24hr_all(self):
        return [{"symbol": sym, "priceChangePercent": "5.0",
                 "quoteVolume": str(9_000_000 - 1000 * i), "lastPrice": "100.0"}
                for i, sym in enumerate(self.symbols)]

    def get_exchange_info(self):
        return {"symbols": [{"symbol": sym, "status": "TRADING",
                             "isSpotTradingAllowed": True}
                            for sym in self.symbols]}

    def get_klines(self, symbol, interval="5m", limit=1000,
                   start_time_ms=None, end_time_ms=None):
        self.jumlah_request += 1
        langkah = 24 * 60 * 60 * 1000 if interval == "1d" else MS_PER_5M
        mulai = int(start_time_ms or 0)
        mulai -= mulai % langkah
        akhir = int(end_time_ms or mulai)
        dasar = 100.0 + 7.0 * (self.symbols.index(symbol)
                               if symbol in self.symbols else 0)
        rows = []
        t = mulai
        while t <= akhir and len(rows) < int(limit):
            putaran = (t // langkah) % 24
            harga = dasar * (1.0 + 0.004 * putaran)
            rows.append([t, f"{harga:.8f}", f"{harga * 1.004:.8f}",
                         f"{harga * 0.996:.8f}", f"{harga * 1.001:.8f}",
                         "1000", t + langkah - 1, "5000000", 10, "0", "0", "0"])
            t += langkah
        return rows


def test_job_grid_end_to_end_dan_membersihkan_file(monkeypatch, tmp_path):
    import os

    from backtesting import portfolio_backtest as pbt

    klien = KlienBinancePalsu(["AAAUSDT", "BBBUSDT", "CCCUSDT"])
    monkeypatch.setattr(dashboard, "_HAS_CLIENT", True, raising=False)
    monkeypatch.setattr(dashboard, "BinanceSpotClient",
                        lambda *args, **kwargs: klien, raising=False)
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "BACKTEST_CACHE_FILE",
                        str(tmp_path / "backtest_cache.sqlite3"))
    monkeypatch.setitem(dashboard.PUMP_CONFIG, "MIN_QUOTE_VOLUME_USDT_24H",
                        1_000_000)

    dibuat = []
    asli = pbt.new_backtest_store

    def pembungkus(config=None):
        store = asli(config)
        dibuat.append(store.db_path)
        return store

    monkeypatch.setattr(dashboard.pbt, "new_backtest_store", pembungkus)

    job_id = "uji-grid"
    with dashboard._bt_jobs_lock:
        dashboard._bt_jobs[job_id] = {"status": "running", "progress": 0.0,
                                      "stage": "", "created_at": time.time()}
    try:
        dashboard._bt_run_grid_job(
            job_id, days=3, max_symbols=3,
            spec={"ATR_MULT_TP": [24.0, 48.0]}, rasio_latih=0.7,
            metrik="total_return_pct", min_trades=1, total_kombinasi=2)

        with dashboard._bt_jobs_lock:
            job = dict(dashboard._bt_jobs[job_id])
        assert job["status"] == "done", job.get("error")
        payload = job["result"]
        assert payload["kind"] == "grid"
        assert payload["mode"] == "grid"
        assert payload["grid"]["total_kombinasi"] == 2
        assert payload["grid"]["bar_latih"] > 0
        assert payload["grid"]["bar_uji"] > 0
        assert len(payload["rows"]) == 2
        assert {r["params"]["ATR_MULT_TP"] for r in payload["rows"]} == {24.0, 48.0}
        assert "skor_latih" in payload["rows"][0]
        assert "pf_latih" in payload["rows"][0]
        assert "winrate_latih" in payload["rows"][0]
        # Seluruh payload grid harus bisa diserialisasi sebagai JSON standar
        # (tanpa Infinity/NaN yang membuat JSON.parse browser gagal).
        json.dumps(payload["rows"], allow_nan=False)
        assert payload["limitations"]
        assert klien.jumlah_request > 0
        assert payload["cache"] is not None and payload["cache"]["rows"] > 0
    finally:
        with dashboard._bt_jobs_lock:
            dashboard._bt_jobs.pop(job_id, None)

    assert dibuat, "job grid harus membuat store SQLite temporary"
    for path in dibuat:
        assert not os.path.exists(path)
