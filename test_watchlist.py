#!/usr/bin/env python3
"""Audit fitur watchlist pemantauan. Tidak butuh jaringan.

Jalankan:
    python test_watchlist.py

Yang diverifikasi:
  1. Helper config membaca, membersihkan, dan memvalidasi daftar dengan benar.
  2. Input yang rusak TIDAK membuat crash (daftar ini diedit manusia).
  3. build_watchlist() menghasilkan status yang benar terhadap ambang bot.
  4. Kegagalan jaringan ditangani anggun, bukan melempar exception.
  5. PALING PENTING: watchlist TIDAK mengubah keputusan trading apa pun.
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

import config as cfg_mod
import market_scanner as scanner
from strategy import Kline


def _harian_pump(_symbol: str, quote_volume: float = 1_000_000.0) -> list:
    """Tujuh candle harian penuh bervolume kecil.

    Dipakai tes yang menguji saringan STRUKTURAL (stablecoin, leveraged
    token, watchlist). Gerbang pump tetap berjalan pada tes-tes itu, jadi
    sumber candle harian wajib ada; volumenya dibuat kecil supaya simbol yang
    memang seharusnya lolos tidak tersandung syarat volume.
    """
    hari = 86_400_000
    return [Kline(open_time=i * hari, open=1.0, high=1.0, low=1.0, close=1.0,
                  close_time=(i + 1) * hari - 1, volume=quote_volume,
                  quote_volume=quote_volume)
            for i in range(7)]


def _ref_ms() -> int:
    """Waktu acuan sesudah candle harian _harian_pump() tertutup."""
    return 7 * 86_400_000 + 1

# Pengujian dashboard butuh Flask. Kalau dependensi belum dipasang, lebih
# baik pengujian itu DILEWATI dengan pesan yang jelas daripada memuntahkan
# belasan traceback ModuleNotFoundError yang menyesatkan -- kesalahannya ada
# di lingkungan, bukan di kode. Pengujian lain (config, scanner, template)
# tidak butuh Flask dan tetap berjalan normal.
try:
    import dashboard as _dash_mod
    _HAS_FLASK = True
    _FLASK_ERR = ""
except ImportError as _e:
    _dash_mod = None
    _HAS_FLASK = False
    _FLASK_ERR = str(_e)

_BUTUH_FLASK = unittest.skipUnless(
    _HAS_FLASK,
    f"Flask belum terpasang ({_FLASK_ERR}). "
    f"Jalankan: pip install -r requirements.txt",
)


def K(o, h, l, c, v=1000.0, qv=None, t=0):
    """Bantu bikin Kline; quote_volume default konsisten dengan close."""
    return Kline(open_time=t, open=o, high=h, low=l, close=c, volume=v,
                 close_time=t + 299_999, quote_volume=qv if qv is not None else v * c)


def _synthetic_strategy_config():
    """Fixture pendek untuk audit deteksi setup tanpa mengubah produksi."""
    c = dict(cfg_mod.PUMP_CONFIG)
    c.update({
        "CONFIRM_LOOKBACK_BARS": 48,
        "SWING_LOOKBACK_BARS": 12,
        "SWING_PIVOT_WING_BARS": 2,
        "VWAP_MIN_BARS_AFTER_ANCHOR": 2,
        "MAX_BARS_BREAKOUT_TO_RETEST": 12,
    })
    return c


class TestConfigHelper(unittest.TestCase):
    """Helper harus tahan input berantakan tanpa pernah melempar exception."""

    def test_daftar_bawaan_valid(self):
        wl = cfg_mod.get_watchlist()
        self.assertGreater(len(wl), 0, "watchlist bawaan tidak boleh kosong")
        for r in wl:
            self.assertTrue(r["symbol"].endswith("USDT"), f"{r['symbol']} bukan pair USDT")
            self.assertEqual(r["symbol"], r["symbol"].upper())
            self.assertIn(r["tier"], cfg_mod.VALID_WATCHLIST_TIERS)
            self.assertIsInstance(r["score"], float)
            self.assertTrue(0 <= r["score"] <= 100, f"skor {r['symbol']} di luar 0-100")
            self.assertTrue(r["note"], f"{r['symbol']} tidak punya catatan")

    def test_tidak_ada_simbol_duplikat(self):
        syms = [r["symbol"] for r in cfg_mod.get_watchlist()]
        self.assertEqual(len(syms), len(set(syms)), "ada simbol duplikat di watchlist")

    def test_terima_entry_berupa_string(self):
        c = {"WATCHLIST": ["arbusdt", " ZECUSDT "]}
        wl = cfg_mod.get_watchlist(c)
        self.assertEqual([r["symbol"] for r in wl], ["ARBUSDT", "ZECUSDT"])
        self.assertEqual(wl[0]["tier"], "LAINNYA")
        self.assertIsNone(wl[0]["score"])

    def test_buang_duplikat_dan_kosong(self):
        c = {"WATCHLIST": ["ARBUSDT", "ARBUSDT", "", "   ", None, 42, [], {"symbol": ""}]}
        wl = cfg_mod.get_watchlist(c)
        self.assertEqual([r["symbol"] for r in wl], ["ARBUSDT"])

    def test_tier_dan_skor_rusak_tidak_bikin_crash(self):
        c = {"WATCHLIST": [
            {"symbol": "AUSDT", "tier": "NGAWUR", "score": "bukan angka"},
            {"symbol": "BUSDT", "tier": None, "score": None},
            {"symbol": "CUSDT", "score": "77.5"},   # string angka tetap terbaca
        ]}
        wl = cfg_mod.get_watchlist(c)
        self.assertEqual(wl[0]["tier"], "LAINNYA")
        self.assertIsNone(wl[0]["score"])
        self.assertIsNone(wl[1]["score"])
        self.assertEqual(wl[2]["score"], 77.5)

    def test_watchlist_bukan_list(self):
        for bad in ({"WATCHLIST": "ARBUSDT"}, {"WATCHLIST": None},
                    {"WATCHLIST": 123}, {}):
            self.assertEqual(cfg_mod.get_watchlist(bad), [])

    def test_enabled_dibaca_longgar_tapi_aman(self):
        for val in (True, "true", "1", "ya", "YES", "on"):
            self.assertTrue(cfg_mod.watchlist_enabled({"WATCHLIST_ENABLED": val}), val)
        for val in (False, "false", "0", "tidak", "", None, "mungkin", 999):
            self.assertFalse(cfg_mod.watchlist_enabled({"WATCHLIST_ENABLED": val}), val)
        # kunci hilang sama sekali -> default aman False
        self.assertFalse(cfg_mod.watchlist_enabled({}))


class TestTidakMenyentuhTrading(unittest.TestCase):
    """Jaminan inti: watchlist murni kosmetik bagi mesin trading."""

    def test_modul_trading_tidak_membaca_watchlist(self):
        import inspect
        import pump_scanner_bot
        import portfolio_backtest
        import backtest
        for mod in (scanner, pump_scanner_bot, portfolio_backtest, backtest):
            src = inspect.getsource(mod)
            self.assertNotIn("WATCHLIST", src,
                             f"{mod.__name__} membaca WATCHLIST, ini melanggar "
                             f"janji bahwa watchlist read-only")

    def test_ranking_kandidat_identik_dengan_dan_tanpa_watchlist(self):
        tickers = [
            {"symbol": "AAAUSDT", "priceChangePercent": "25.0",
             "quoteVolume": "9000000", "lastPrice": "1.0"},
            {"symbol": "BBBUSDT", "priceChangePercent": "40.0",
             "quoteVolume": "7000000", "lastPrice": "2.0"},
            {"symbol": "ZECUSDT", "priceChangePercent": "15.0",
             "quoteVolume": "5000000", "lastPrice": "3.0"},
        ]
        base = dict(cfg_mod.PUMP_CONFIG)
        tanpa = dict(base); tanpa["WATCHLIST"] = []; tanpa["WATCHLIST_ENABLED"] = False
        dengan = dict(base)
        dengan["WATCHLIST"] = [{"symbol": "AAAUSDT", "tier": "INTI", "score": 99.0}]
        dengan["WATCHLIST_ENABLED"] = True

        r1 = [c.symbol for c in scanner.filter_and_rank_candidates(
            tickers, tanpa, get_daily_klines_fn=_harian_pump, reference_ms=_ref_ms())]
        r2 = [c.symbol for c in scanner.filter_and_rank_candidates(
            tickers, dengan, get_daily_klines_fn=_harian_pump, reference_ms=_ref_ms())]
        self.assertEqual(r1, r2, "watchlist mengubah ranking kandidat")
        # Urutan semesta kini murni berdasarkan volume kuotasi 24 jam, bukan
        # kenaikan harga. BBB naik paling tinggi tetapi volumenya lebih kecil
        # dari AAA, jadi AAA tetap di atas.
        self.assertEqual(r1, ["AAAUSDT", "BBBUSDT", "ZECUSDT"])

    def test_koin_di_luar_watchlist_tetap_boleh_masuk(self):
        """Ini yang membedakan mode pantau dari whitelist keras."""
        tickers = [{"symbol": "TIDAKADADIDAFTARUSDT", "priceChangePercent": "30.0",
                    "quoteVolume": "9000000", "lastPrice": "1.0"}]
        ranked = scanner.filter_and_rank_candidates(
            tickers, cfg_mod.PUMP_CONFIG,
            get_daily_klines_fn=_harian_pump, reference_ms=_ref_ms())
        self.assertEqual(len(ranked), 1,
                         "koin di luar watchlist seharusnya TETAP jadi kandidat")


class TestStablecoinBaru(unittest.TestCase):
    def test_usd1_dan_rlusd_dibuang(self):
        tickers = [
            {"symbol": "USD1USDT", "priceChangePercent": "0.00",
             "quoteVolume": "218600000", "lastPrice": "1.0"},
            {"symbol": "RLUSDUSDT", "priceChangePercent": "0.00",
             "quoteVolume": "88200000", "lastPrice": "1.0"},
        ]
        self.assertEqual(scanner.filter_and_rank_candidates(
            tickers, cfg_mod.PUMP_CONFIG,
            get_daily_klines_fn=_harian_pump, reference_ms=_ref_ms()), [])

    def test_stablecoin_lama_masih_dibuang(self):
        for base in ("USDC", "FDUSD", "TUSD", "DAI"):
            t = [{"symbol": f"{base}USDT", "priceChangePercent": "20.0",
                  "quoteVolume": "9000000", "lastPrice": "1.0"}]
            self.assertEqual(scanner.filter_and_rank_candidates(
                t, cfg_mod.PUMP_CONFIG,
                get_daily_klines_fn=_harian_pump, reference_ms=_ref_ms()), [],
                             f"{base} lolos padahal stablecoin")


@_BUTUH_FLASK
class TestBuildWatchlist(unittest.TestCase):
    """Uji builder dashboard dengan klien Binance palsu."""

    def setUp(self):
        dashboard = _dash_mod
        self.dash = dashboard
        dashboard._watchlist_cache.update({"data": None, "ts": 0, "error": None})

    def _patch(self, tickers, cfg_over=None, client_raises=False, client_none=False):
        base = dict(self.dash.PUMP_CONFIG)
        base["WATCHLIST_ENABLED"] = True
        base["MIN_QUOTE_VOLUME_USDT_24H"] = 2_000_000
        base["WATCHLIST"] = cfg_over if cfg_over is not None else [
            {"symbol": "AAAUSDT", "tier": "INTI", "score": 90.0, "note": "uji"},
        ]

        class FakeClient:
            def get_ticker_24hr_all(self):
                if client_raises:
                    raise RuntimeError("koneksi putus")
                return tickers

        getc = (lambda: None) if client_none else (lambda: FakeClient())
        return mock.patch.multiple(self.dash, PUMP_CONFIG=base, get_client=getc)

    def test_status_likuid(self):
        t = [{"symbol": "AAAUSDT", "lastPrice": "10", "priceChangePercent": "20",
              "quoteVolume": "5000000", "highPrice": "11", "lowPrice": "8", "count": 100}]
        with self._patch(t):
            w = self.dash.build_watchlist()
        r = w["items"][0]
        self.assertEqual(r["status"], "LIKUID")
        self.assertTrue(r["pass_volume"])
        # Panel watchlist hanya menilai likuiditas dan struktur; gerbang pump
        # ditegakkan di market_scanner saat scan, bukan di payload panel ini,
        # jadi kunci lama pass_pump/pump_gap tidak boleh muncul lagi.
        self.assertNotIn("pass_pump", r)
        self.assertNotIn("pump_gap", r)
        # posisi range: (10-8)/(11-8) = 0,667
        self.assertAlmostEqual(r["range_position"], 0.667, places=2)

    def test_koin_turun_tetap_likuid_asal_volumenya_cukup(self):
        """Status LIKUID pada panel watchlist hanya soal volume.

        Penilaian naik atau turun 24 jam dilakukan gerbang pump di
        market_scanner saat scan, bukan oleh label likuiditas panel ini.
        """
        t = [{"symbol": "AAAUSDT", "lastPrice": "10", "priceChangePercent": "-25",
              "quoteVolume": "5000000", "highPrice": "13", "lowPrice": "9", "count": 100}]
        with self._patch(t):
            w = self.dash.build_watchlist()
        self.assertEqual(w["items"][0]["status"], "LIKUID")

    def test_status_tipis_saat_volume_kurang(self):
        cases = [
            ("5", "5000000", "LIKUID"),     # likuid meski naiknya kecil
            ("20", "500000", "TIPIS"),      # volume kurang
            ("2", "500000", "TIPIS"),       # volume kurang
        ]
        for chg, vol, expect in cases:
            # Cache menyimpan SATU snapshot seluruh pasar dan berlaku 20 detik.
            # Itu perilaku yang benar di produksi, tapi di dalam loop uji ini
            # membuat kasus kedua membaca data kasus pertama. Jadi cache
            # dikosongkan tiap iterasi agar yang diuji memang logika status.
            self.dash._watchlist_cache.update({"data": None, "ts": 0, "error": None})
            t = [{"symbol": "AAAUSDT", "lastPrice": "10", "priceChangePercent": chg,
                  "quoteVolume": vol, "highPrice": "11", "lowPrice": "9", "count": 5}]
            with self._patch(t):
                w = self.dash.build_watchlist()
            self.assertEqual(w["items"][0]["status"], expect, f"chg={chg} vol={vol}")

    def test_ambang_tepat_di_batas_dihitung_lolos(self):
        """Scanner memakai >=, panel harus memakai perbandingan yang sama."""
        t = [{"symbol": "AAAUSDT", "lastPrice": "10", "priceChangePercent": "13.0",
              "quoteVolume": "2000000", "highPrice": "10", "lowPrice": "10", "count": 1}]
        with self._patch(t):
            w = self.dash.build_watchlist()
        self.assertEqual(w["items"][0]["status"], "LIKUID")

    def test_simbol_tanpa_data_tidak_hilang(self):
        with self._patch([]):
            w = self.dash.build_watchlist()
        self.assertEqual(len(w["items"]), 1)
        self.assertEqual(w["items"][0]["status"], "TIDAK ADA DATA")
        self.assertIsNone(w["items"][0]["price"])

    def test_harga_nol_tidak_bikin_bagi_nol(self):
        t = [{"symbol": "AAAUSDT", "lastPrice": "0", "priceChangePercent": "50",
              "quoteVolume": "9000000", "highPrice": "0", "lowPrice": "0", "count": 0}]
        with self._patch(t):
            w = self.dash.build_watchlist()
        self.assertEqual(w["items"][0]["status"], "TIDAK ADA DATA")

    def test_high_sama_dengan_low_tidak_bikin_bagi_nol(self):
        t = [{"symbol": "AAAUSDT", "lastPrice": "10", "priceChangePercent": "20",
              "quoteVolume": "9000000", "highPrice": "10", "lowPrice": "10", "count": 1}]
        with self._patch(t):
            w = self.dash.build_watchlist()
        self.assertIsNone(w["items"][0]["range_position"])

    def test_ticker_nilai_rusak_tidak_crash(self):
        t = [{"symbol": "AAAUSDT", "lastPrice": "abc", "priceChangePercent": None,
              "quoteVolume": {}, "highPrice": "x", "lowPrice": "y"}]
        with self._patch(t):
            w = self.dash.build_watchlist()
        self.assertEqual(w["items"][0]["status"], "TIDAK ADA DATA")

    def test_jaringan_gagal_ditangani_anggun(self):
        with self._patch([], client_raises=True):
            w = self.dash.build_watchlist()
        self.assertTrue(w["enabled"])
        self.assertIsNotNone(w["error"])
        self.assertEqual(len(w["items"]), 1)

    def test_klien_tidak_tersedia(self):
        with self._patch([], client_none=True):
            w = self.dash.build_watchlist()
        self.assertIsNotNone(w["error"])
        self.assertEqual(w["items"][0]["status"], "TIDAK ADA DATA")

    def test_cache_dipakai_saat_request_kedua_gagal(self):
        """Data lama harus bertahan supaya panel tidak berkedip kosong."""
        t = [{"symbol": "AAAUSDT", "lastPrice": "10", "priceChangePercent": "20",
              "quoteVolume": "9000000", "highPrice": "11", "lowPrice": "9", "count": 1}]
        with self._patch(t):
            w1 = self.dash.build_watchlist()
        self.assertEqual(w1["items"][0]["status"], "LIKUID")
        # paksa cache kedaluwarsa, lalu buat panggilan berikutnya gagal
        self.dash._watchlist_cache["ts"] = 0
        with self._patch([], client_raises=True):
            w2 = self.dash.build_watchlist()
        self.assertIsNotNone(w2["error"])
        self.assertEqual(w2["items"][0]["status"], "LIKUID", "data cache hilang")

    def test_urutan_likuid_paling_atas_lalu_volume_terbesar(self):
        wl = [{"symbol": s, "tier": "INTI", "score": 50.0} for s in
              ("TIPISUSDT", "BESARUSDT", "SEDANGUSDT")]
        t = [
            {"symbol": "TIPISUSDT", "lastPrice": "1", "priceChangePercent": "30",
             "quoteVolume": "100", "highPrice": "1", "lowPrice": "1", "count": 1},
            {"symbol": "BESARUSDT", "lastPrice": "1", "priceChangePercent": "1",
             "quoteVolume": "9000000", "highPrice": "1", "lowPrice": "1", "count": 1},
            {"symbol": "SEDANGUSDT", "lastPrice": "1", "priceChangePercent": "2",
             "quoteVolume": "3000000", "highPrice": "1", "lowPrice": "1", "count": 1},
        ]
        with self._patch(t, cfg_over=wl):
            w = self.dash.build_watchlist()
        self.assertEqual([r["symbol"] for r in w["items"]],
                         ["BESARUSDT", "SEDANGUSDT", "TIPISUSDT"])

    def test_nonaktif_mengembalikan_daftar_kosong(self):
        base = dict(self.dash.PUMP_CONFIG)
        base["WATCHLIST_ENABLED"] = False
        with mock.patch.object(self.dash, "PUMP_CONFIG", base):
            w = self.dash.build_watchlist()
        self.assertFalse(w["enabled"])
        self.assertEqual(w["items"], [])

    def test_hasil_bisa_diserialisasi_json(self):
        t = [{"symbol": "AAAUSDT", "lastPrice": "10", "priceChangePercent": "20",
              "quoteVolume": "9000000", "highPrice": "11", "lowPrice": "9", "count": 7}]
        with self._patch(t):
            w = self.dash.build_watchlist()
        json.dumps(w)  # harus tidak melempar

    def test_satu_panggilan_api_untuk_semua_simbol(self):
        """Panel tidak boleh memanggil Binance sekali per simbol."""
        wl = [{"symbol": f"S{i}USDT", "tier": "INTI", "score": 1.0} for i in range(30)]
        calls = []

        class Counting:
            def get_ticker_24hr_all(self):
                calls.append(1)
                return []

        base = dict(self.dash.PUMP_CONFIG)
        base["WATCHLIST_ENABLED"] = True
        base["WATCHLIST"] = wl
        with mock.patch.multiple(self.dash, PUMP_CONFIG=base,
                                 get_client=lambda: Counting()):
            self.dash.build_watchlist()
        self.assertEqual(len(calls), 1, f"30 simbol memicu {len(calls)} panggilan API")


@_BUTUH_FLASK
class TestEndpointFlask(unittest.TestCase):
    def test_endpoint_watchlist_balas_200(self):
        dashboard = _dash_mod
        dashboard._watchlist_cache.update({"data": None, "ts": 0, "error": None})
        dashboard.app.config["TESTING"] = True
        with dashboard.app.test_client() as c, \
                mock.patch.object(dashboard, "get_client", lambda: None):
            r = c.get("/api/watchlist")
            self.assertEqual(r.status_code, 200)
            self.assertIn("items", r.get_json())

    def test_api_all_menyertakan_watchlist(self):
        dashboard = _dash_mod
        dashboard.app.config["TESTING"] = True
        with dashboard.app.test_client() as c, \
                mock.patch.object(dashboard, "get_client", lambda: None):
            r = c.get("/api/all")
            self.assertEqual(r.status_code, 200)
            self.assertIn("watchlist", r.get_json())


class TestTemplate(unittest.TestCase):
    """Cegah panel rusak karena id HTML dan JS tidak sinkron."""

    def setUp(self):
        import os
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "templates", "dashboard.html")
        with open(p, encoding="utf-8") as f:
            self.html = f.read()

    def test_semua_id_yang_dipakai_js_ada_di_html(self):
        for el in ("watchlistPanel", "wlCount", "wlChips", "wlNote", "wlBody", "wlTable"):
            self.assertIn(f'id="{el}"', self.html, f"id {el} tidak ada di HTML")

    def test_render_dipanggil_di_refresh(self):
        self.assertIn("renderWatchlist(d.watchlist)", self.html)
        self.assertIn("function renderWatchlist(", self.html)

    def test_jumlah_kolom_header_cocok_dengan_colspan(self):
        import re
        blok = self.html.split('<table class="wl" id="wlTable">')[1].split("</table>")[0]
        n_th = len(re.findall(r"<th>", blok))
        self.assertEqual(n_th, 8, f"header watchlist punya {n_th} kolom, diharapkan 8")
        for m in re.findall(r'colspan="(\d+)"', blok):
            self.assertEqual(int(m), n_th, "colspan tidak cocok jumlah kolom")

    def test_kelas_css_status_dan_tier_terdefinisi(self):
        for c in ("st-siap", "st-tipis", "st-nodata",
                  "ti-inti", "ti-aktif", "ti-spekulatif", "ti-lainnya"):
            self.assertIn(f".tag.{c}", self.html, f"kelas CSS .tag.{c} belum ada")

    def test_tidak_ada_resource_eksternal_baru(self):
        """Preview dashboard berjalan tanpa jaringan; aset harus lokal."""
        blok = self.html.split("watchlist (panel pantau")[1][:4000]
        for bad in ("http://", "https://", "cdn."):
            self.assertNotIn(bad, blok, f"blok watchlist memuat resource eksternal: {bad}")


class TestRateLimitClient(unittest.TestCase):
    """Penanganan 429/418 harus menghormati Retry-After, bukan spam ulang.

    Ini kritis: dokumen resmi Binance menyatakan ban IP meningkat "from 2
    minutes to 3 days" bagi yang terus mengirim setelah kena 429. Kalau
    penyegar otomatis memicu itu, bot bisa gagal menutup posisi.
    """

    def setUp(self):
        try:
            import binance_client
        except ImportError as e:
            self.skipTest(f"requests belum terpasang ({e})")
        self.bc = binance_client

    def _client(self):
        return self.bc.BinanceSpotClient("k", "s", "https://contoh.invalid")

    def _resp(self, status, headers=None, body=None):
        class R:
            status_code = status
            text = json.dumps(body or {})

            def __init__(self, h):
                self.headers = h or {}

            def json(self):
                return body or {}
        return R(headers)

    def test_used_weight_dibaca_dari_header(self):
        c = self._client()
        c._record_used_weight({"x-mbx-used-weight-1m": "1234"})
        self.assertEqual(c.used_weight_1m, 1234)
        # 1234 dari 6000 -> sisa sekitar 79%
        self.assertAlmostEqual(c.weight_headroom(6000), 1 - 1234 / 6000, places=3)

    def test_header_rusak_tidak_bikin_crash(self):
        c = self._client()
        for bad in ({}, {"x-mbx-used-weight-1m": "abc"},
                    {"x-mbx-used-weight-1m": None}):
            c._record_used_weight(bad)   # tidak boleh melempar
        self.assertEqual(c.weight_headroom(6000), 1.0)

    def test_headroom_penuh_kalau_data_basi(self):
        """Data lebih dari semenit tidak boleh dipakai menahan bot."""
        c = self._client()
        c._record_used_weight({"x-mbx-used-weight-1m": "5900"})
        c.used_weight_ts = time.time() - 120
        self.assertEqual(c.weight_headroom(6000), 1.0)

    def test_retry_after_diparse(self):
        c = self._client()
        self.assertEqual(c._parse_retry_after({"Retry-After": "42"}), 42)
        self.assertEqual(c._parse_retry_after({"retry-after": "7.9"}), 7)
        self.assertIsNone(c._parse_retry_after({}))
        self.assertIsNone(c._parse_retry_after({"Retry-After": "xx"}))

    def test_429_melempar_rate_limit_error_dan_menghormati_retry_after(self):
        c = self._client()
        resp = self._resp(429, {"Retry-After": "3",
                                "x-mbx-used-weight-1m": "6000"},
                          {"code": -1003, "msg": "Too many requests"})
        tidur = []
        with mock.patch.object(c.session, "request", return_value=resp), \
                mock.patch.object(self.bc.time, "sleep", side_effect=tidur.append):
            with self.assertRaises(self.bc.BinanceRateLimitError) as cm:
                c._request("GET", "/api/v3/ping", max_retries=2)
        self.assertEqual(cm.exception.retry_after, 3)
        # harus tidur sesuai Retry-After, bukan backoff tebakan 2-10 detik --
        # dengan LANTAI 5 detik di titik sleep (perbaikan S-09): ada laporan
        # Retry-After bernilai 0/1, dan retry secepat itu justru mempercepat
        # eskalasi ke ban IP 418. Parser (diuji terpisah di atas) tetap setia
        # pada nilai header asli.
        self.assertIn(5.0, tidur)
        self.assertNotIn(3, tidur)

    def test_418_tidak_dicoba_ulang(self):
        """IP sudah diblokir; mencoba lagi hanya memperpanjang hukuman."""
        c = self._client()
        resp = self._resp(418, {"Retry-After": "120"}, {"code": -1003, "msg": "banned"})
        panggilan = []

        def fake(*a, **k):
            panggilan.append(1)
            return resp

        with mock.patch.object(c.session, "request", side_effect=fake), \
                mock.patch.object(self.bc.time, "sleep"):
            with self.assertRaises(self.bc.BinanceRateLimitError):
                c._request("GET", "/api/v3/ping", max_retries=5)
        self.assertEqual(len(panggilan), 1, "418 seharusnya tidak dicoba ulang")

    def test_is_rate_limited_aktif_setelah_429(self):
        c = self._client()
        c._note_rate_limited(429, 30)
        self.assertTrue(c.is_rate_limited())
        c.blocked_until = time.time() - 1
        self.assertFalse(c.is_rate_limited())

    def test_error_biasa_tetap_perilaku_lama(self):
        """Perbaikan ini tidak boleh mengubah penanganan error non-rate-limit."""
        c = self._client()
        resp = self._resp(400, {}, {"code": -1121, "msg": "Invalid symbol"})
        with mock.patch.object(c.session, "request", return_value=resp), \
                mock.patch.object(self.bc.time, "sleep"):
            with self.assertRaises(self.bc.BinanceAPIError) as cm:
                c._request("GET", "/api/v3/ping", max_retries=1)
        self.assertNotIsInstance(cm.exception, self.bc.BinanceRateLimitError)
        self.assertEqual(cm.exception.code, -1121)


class TestAutoRefreshKeamanan(unittest.TestCase):
    """Rem keamanan penyegar otomatis. Ini yang melindungi posisi Anda."""

    def setUp(self):
        try:
            import watchlist_auto
        except ImportError as e:
            self.skipTest(f"dependensi belum terpasang ({e})")
        self.wa = watchlist_auto

    def test_dilewati_saat_ada_posisi_terbuka(self):
        """REM UTAMA: jangan bersaing dengan bot yang sedang pegang uang."""
        dipanggil = []

        class Klien:
            def get_ticker_24hr_all(self):
                dipanggil.append(1)
                return []

        res = self.wa.refresh_once(Klien(), dict(cfg_mod.PUMP_CONFIG),
                                   has_open_position=lambda: True)
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("skipped"))
        self.assertEqual(len(dipanggil), 0,
                         "tidak boleh ada satu pun panggilan API saat posisi terbuka")

    def test_dilewati_saat_ip_kena_rate_limit(self):
        class Klien:
            def is_rate_limited(self):
                return True

            def get_ticker_24hr_all(self):
                raise AssertionError("tidak boleh dipanggil")

        res = self.wa.refresh_once(Klien(), dict(cfg_mod.PUMP_CONFIG),
                                   has_open_position=lambda: False)
        self.assertFalse(res["ok"])
        self.assertIn("batas rate", res["error"])

    def test_klien_none_tidak_crash(self):
        res = self.wa.refresh_once(None, dict(cfg_mod.PUMP_CONFIG))
        self.assertFalse(res["ok"])

    def test_budget_berhenti_saat_kuota_menipis(self):
        class Klien:
            def is_rate_limited(self):
                return False

            def weight_headroom(self, limit=6000):
                return 0.10          # sisa 10%, di bawah ambang 50%

        b = self.wa.Budget(Klien(), max_weight=900, pace_seconds=0,
                           min_headroom=0.5)
        self.assertFalse(b.can_spend(2))
        self.assertIn("sisa kuota", b.stopped_reason)

    def test_budget_berhenti_saat_plafon_weight_habis(self):
        b = self.wa.Budget(None, max_weight=100, pace_seconds=0, min_headroom=0.0)
        self.assertTrue(b.can_spend(80))
        b.spend(80)
        self.assertFalse(b.can_spend(80))
        self.assertIn("anggaran weight", b.stopped_reason)

    def test_anggaran_default_jauh_di_bawah_batas_binance(self):
        """Verifikasi angka config benar-benar aman, bukan sekadar diklaim."""
        c = cfg_mod.PUMP_CONFIG
        plafon = c["WATCHLIST_AUTO_MAX_WEIGHT"]
        pace = c["WATCHLIST_AUTO_PACE_SECONDS"]
        n = c["WATCHLIST_AUTO_MAX_SYMBOLS"]
        hari = c["WATCHLIST_AUTO_DAYS"]

        per_simbol = -(-(hari * 288) // 1000)
        perkiraan = 80 + 4 + n * per_simbol * 2
        self.assertLessEqual(perkiraan, plafon,
                             "plafon weight lebih kecil dari kebutuhan nyata")

        # Dengan jeda antar panggilan, laju per menit harus jauh di bawah 6000
        panggilan = n * per_simbol
        durasi_menit = max(1.0, panggilan * pace / 60.0)
        laju = perkiraan / durasi_menit
        self.assertLess(laju, 600, f"laju {laju:.0f} weight/menit terlalu tinggi")

    def test_file_hasil_terpisah_per_mode(self):
        t = dict(cfg_mod.PUMP_CONFIG); t["MODE"] = "PAPER"
        l = dict(cfg_mod.PUMP_CONFIG); l["MODE"] = "LIVE"
        self.assertNotEqual(self.wa._auto_file(t), self.wa._auto_file(l))
        self.assertIn("paper", self.wa._auto_file(t))
        self.assertIn("live", self.wa._auto_file(l))

    def test_load_result_tahan_file_rusak(self):
        import tempfile
        d = tempfile.mkdtemp()
        cwd = os.getcwd()
        try:
            os.chdir(d)
            c = dict(cfg_mod.PUMP_CONFIG)
            self.assertIsNone(self.wa.load_result(c))      # belum ada file
            with open(self.wa._auto_file(c), "w") as f:
                f.write("{bukan json")
            self.assertIsNone(self.wa.load_result(c))      # rusak, bukan crash
        finally:
            os.chdir(cwd)

    def test_simpan_lalu_baca_ulang(self):
        import tempfile
        d = tempfile.mkdtemp()
        cwd = os.getcwd()
        try:
            os.chdir(d)
            c = dict(cfg_mod.PUMP_CONFIG)
            data = {"items": [{"symbol": "ARBUSDT", "tier": "INTI", "score": 90.0}],
                    "generated_at": 123}
            self.wa.save_result(data, c)
            got = self.wa.load_result(c)
            self.assertEqual(got["items"][0]["symbol"], "ARBUSDT")
        finally:
            os.chdir(cwd)

    def test_to_klines_buang_baris_rusak(self):
        raw = [
            [1, "1", "2", "0.5", "1.5", "10", 2, "15"],   # valid
            [1, "x", "2", "0.5", "1.5", "10", 2, "15"],   # harga rusak
            [1, "1"],                                      # kolom kurang
        ]
        self.assertEqual(len(self.wa.to_klines(raw)), 1)

    def test_penyegar_otomatis_tidak_menyentuh_logika_trading(self):
        import inspect
        import pump_scanner_bot
        src = inspect.getsource(pump_scanner_bot)
        self.assertNotIn("watchlist_auto", src)
        self.assertNotIn("WATCHLIST", src)

    def test_helper_config_auto(self):
        on = dict(cfg_mod.PUMP_CONFIG)
        on["WATCHLIST_ENABLED"] = True
        on["WATCHLIST_AUTO_REFRESH"] = True
        self.assertTrue(cfg_mod.watchlist_auto_enabled(on))
        # panel mati -> auto ikut mati, apa pun isinya
        off = dict(on); off["WATCHLIST_ENABLED"] = False
        self.assertFalse(cfg_mod.watchlist_auto_enabled(off))


@_BUTUH_FLASK
class TestDashboardAutoIntegrasi(unittest.TestCase):
    def setUp(self):
        self.dash = _dash_mod
        self.dash._watchlist_cache.update({"data": None, "ts": 0, "error": None})

    def test_rem_posisi_terbuka_terbaca_dari_state(self):
        # Skema state yang BENAR (ditulis pump_scanner_bot.DEFAULT_STATE):
        # kunci top-level current_symbol + qty. Versi uji sebelumnya memakai
        # {"position": {"symbol": ...}} -- skema yang tidak pernah ditulis
        # bot -- sehingga uji mengunci bug, bukan perilaku benar (temuan T-02).
        with mock.patch.object(self.dash, "load_state",
                               lambda: {"current_symbol": "ARBUSDT", "qty": 1.0}):
            self.assertTrue(self.dash._bot_has_open_position())
        with mock.patch.object(self.dash, "load_state",
                               lambda: {"current_symbol": "XUSDT", "qty": "2.0"}):
            self.assertTrue(self.dash._bot_has_open_position())
        with mock.patch.object(self.dash, "load_state",
                               lambda: {"current_symbol": "XUSDT", "qty": 0.0}):
            self.assertFalse(self.dash._bot_has_open_position())
        with mock.patch.object(self.dash, "load_state", lambda: {}):
            self.assertFalse(self.dash._bot_has_open_position())
        # Skema LAMA yang keliru (position.symbol) tidak boleh dianggap
        # posisi terbuka -- kunci itu tidak pernah ditulis bot.
        with mock.patch.object(self.dash, "load_state",
                               lambda: {"position": {"symbol": "ARBUSDT"}}):
            self.assertFalse(self.dash._bot_has_open_position())

    def test_state_rusak_dianggap_ada_posisi(self):
        """Sikap aman: ragu berarti jangan ganggu bot."""
        def meledak():
            raise OSError("file rusak")
        with mock.patch.object(self.dash, "load_state", meledak):
            self.assertTrue(self.dash._bot_has_open_position())
        with mock.patch.object(self.dash, "load_state", lambda: "bukan dict"):
            self.assertTrue(self.dash._bot_has_open_position())

    def test_daftar_manual_jadi_cadangan_saat_auto_belum_ada(self):
        t = [{"symbol": "ZECUSDT", "lastPrice": "10", "priceChangePercent": "1",
              "quoteVolume": "9000000", "highPrice": "11", "lowPrice": "9", "count": 1}]

        class FakeClient:
            def get_ticker_24hr_all(self):
                return t

        base = dict(self.dash.PUMP_CONFIG)
        base["WATCHLIST_AUTO_REFRESH"] = True
        with mock.patch.multiple(self.dash, PUMP_CONFIG=base,
                                 get_client=lambda: FakeClient()), \
                mock.patch.object(self.dash.wl_auto, "load_result", lambda c: None):
            w = self.dash.build_watchlist()
        self.assertEqual(w["source"], "config")
        self.assertGreater(len(w["items"]), 0, "panel kosong padahal ada cadangan")

    def test_pakai_hasil_auto_kalau_tersedia(self):
        class FakeClient:
            def get_ticker_24hr_all(self):
                return []

        base = dict(self.dash.PUMP_CONFIG)
        base["WATCHLIST_AUTO_REFRESH"] = True
        palsu = {"items": [{"symbol": "XXXUSDT", "tier": "INTI", "score": 88.0,
                            "note": "dari auto"}],
                 "generated_at": 1700000000, "days": 14}
        with mock.patch.multiple(self.dash, PUMP_CONFIG=base,
                                 get_client=lambda: FakeClient()), \
                mock.patch.object(self.dash.wl_auto, "load_result", lambda c: palsu):
            w = self.dash.build_watchlist()
        self.assertEqual(w["source"], "auto")
        self.assertEqual([r["symbol"] for r in w["items"]], ["XXXUSDT"])

    def test_auto_mati_tetap_pakai_config(self):
        class FakeClient:
            def get_ticker_24hr_all(self):
                return []

        base = dict(self.dash.PUMP_CONFIG)
        base["WATCHLIST_AUTO_REFRESH"] = False
        with mock.patch.multiple(self.dash, PUMP_CONFIG=base,
                                 get_client=lambda: FakeClient()):
            w = self.dash.build_watchlist()
        self.assertEqual(w["source"], "config")

    def test_start_refresher_tidak_jalan_saat_dimatikan(self):
        base = dict(self.dash.PUMP_CONFIG)
        base["WATCHLIST_AUTO_REFRESH"] = False
        with mock.patch.multiple(self.dash, PUMP_CONFIG=base, _auto_refresher=None):
            self.dash.start_auto_refresher()
            self.assertIsNone(self.dash._auto_refresher)


class TestMigrasiTier(unittest.TestCase):
    """Nama tier lama harus tetap terbaca setelah MOMENTUM diganti AKTIF."""

    def test_migrate_tiers_mengubah_items_dan_detail(self):
        import watchlist_auto as wa
        data = {"items": [{"symbol": "AAAUSDT", "tier": "MOMENTUM"},
                          {"symbol": "BBBUSDT", "tier": "INTI"}],
                "detail": [{"symbol": "AAAUSDT", "tier": "MOMENTUM"}]}
        hasil = wa.migrate_tiers(data)
        self.assertEqual([r["tier"] for r in hasil["items"]], ["AKTIF", "INTI"])
        self.assertEqual(hasil["detail"][0]["tier"], "AKTIF")

    def test_migrate_tiers_aman_untuk_bentuk_data_aneh(self):
        import watchlist_auto as wa
        self.assertEqual(wa.migrate_tiers({}), {})
        self.assertEqual(wa.migrate_tiers({"items": "bukan list"}), {"items": "bukan list"})
        self.assertIsNone(wa.migrate_tiers(None))

    def test_file_watchlist_lama_dibaca_dengan_tier_baru(self):
        import json
        import tempfile
        import watchlist_auto as wa
        with tempfile.TemporaryDirectory() as d:
            cfg = dict(cfg_mod.PUMP_CONFIG)
            path = os.path.join(d, "watchlist_auto_paper.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"items": [{"symbol": "AAAUSDT", "tier": "MOMENTUM",
                                      "score": 80.0, "note": "lama"}]}, f)
            with mock.patch.object(wa, "_auto_file", lambda _c: path):
                hasil = wa.load_result(cfg)
        self.assertEqual(hasil["items"][0]["tier"], "AKTIF")

    def test_config_menolak_tier_yang_tidak_dikenal(self):
        self.assertEqual(cfg_mod.migrate_watchlist_tier("MOMENTUM"), "AKTIF")
        self.assertIn("AKTIF", cfg_mod.VALID_WATCHLIST_TIERS)
        self.assertNotIn("MOMENTUM", cfg_mod.VALID_WATCHLIST_TIERS)


class TestEntryTetapUtuh(unittest.TestCase):
    """Pastikan tidak ada logika entry yang ikut berubah saat mengedit file."""

    def test_deteksi_setup_masih_bekerja(self):
        from synthetic_data import skenario_pullback_retest
        hasil = scanner.detect_pullback_retest(
            skenario_pullback_retest("lolos"), _synthetic_strategy_config())
        self.assertTrue(hasil.ok, hasil.reason)
        self.assertGreater(hasil.breakout_level, 0)

    def test_setup_gagal_menyebut_alasan(self):
        from synthetic_data import skenario_pullback_retest
        hasil = scanner.detect_pullback_retest(
            skenario_pullback_retest("wick_saja"), _synthetic_strategy_config())
        self.assertFalse(hasil.ok)
        self.assertIn("breakout", hasil.reason)

    def test_data_kurang_ditolak(self):
        hasil = scanner.detect_pullback_retest([K(1, 1, 1, 1)] * 3, cfg_mod.PUMP_CONFIG)
        self.assertFalse(hasil.ok)
        self.assertIn("minimum", hasil.reason)

    def test_confirm_entry_tetap_mengembalikan_pasangan_bool_dan_alasan(self):
        """Kontrak lama confirm_entry() tidak boleh berubah."""
        from synthetic_data import skenario_pullback_retest
        ok, alasan = scanner.confirm_entry(
            skenario_pullback_retest("lolos"), _synthetic_strategy_config())
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(alasan, str)
        self.assertTrue(ok, alasan)


if __name__ == "__main__":
    print("=" * 66)
    print("AUDIT FITUR WATCHLIST PEMANTAUAN")
    print("=" * 66)
    r = unittest.main(exit=False, verbosity=2).result
    n_skip = len(getattr(r, "skipped", []))
    print("=" * 66)
    if r.wasSuccessful():
        pesan = f"LULUS SEMUA: {r.testsRun} pengujian, 0 gagal, 0 error"
        print(f"{pesan}, {n_skip} dilewati." if n_skip else f"{pesan}.")
        if n_skip:
            print()
            print("CATATAN: sebagian pengujian dilewati karena dependensi belum")
            print("terpasang. Itu bukan kegagalan kode. Untuk menguji lengkap:")
            print("    pip install -r requirements.txt")
    else:
        print(f"GAGAL: {len(r.failures)} failure, {len(r.errors)} error "
              f"dari {r.testsRun} pengujian ({n_skip} dilewati).")
    print("=" * 66)
    sys.exit(0 if r.wasSuccessful() else 1)
