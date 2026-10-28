from __future__ import annotations

import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Callable

from backtesting import backtest as bt
from backtesting import grid_search as gs
from backtesting import parity
from backtesting import portfolio_backtest as pbt
from backtesting.confluence_fixture import (
    STEP_15M_MS,
    STEP_5M_MS,
    build_flat_series,
    build_one_zone_series,
    make_bar,
)
from config.config import CONFIG_LOAD_ERRORS, PUMP_CONFIG
from config.settings_schema import (
    PARAMETER_SCHEMA,
    REMOVED_PARAMETERS,
    _migrate_legacy_keys,
    _validate_override_payload,
    relation_violations,
    validate_candidate,
)
from market.market_scanner import (
    ScanDiagnostics,
    detect_entry_setup,
    evaluate_market_gate,
    evaluate_orderbook,
    find_best_candidate,
)
from strategy import confluence_code as cc
from strategy import indicators as strategy
from trading.pump_scanner_bot import ConfluenceStructureCache

KUNCI_KONFLUENSI = (
    "CONFLUENCE_FILTER_ENABLED",
    "CONFLUENCE_STRUCTURE_INTERVAL",
    "CONFLUENCE_LOOKBACK_BARS",
    "CONFLUENCE_MIN_IMPULSE_PCT",
    "CONFLUENCE_IMPULSE_SELECTION",
    "CONFLUENCE_ZONE_TOLERANCE_RATIO",
    "CONFLUENCE_REQUIRE_ALL_FIVE",
    "CONFLUENCE_MIN_SCORE",
    "VP_FILTER_ENABLED",
    "VP_BINS",
    "VP_HVN_TOP_RATIO",
    "FIB_FILTER_ENABLED",
    "FIB_RETRACEMENT_MIN",
    "FIB_RETRACEMENT_MAX",
    "LIQUIDITY_FILTER_ENABLED",
    "LIQUIDITY_EQUAL_LEVEL_TOL_RATIO",
    "LIQUIDITY_SWEEP_LOOKBACK_BARS",
    "FVG_FILTER_ENABLED",
    "OB_FILTER_ENABLED",
    "CONFLUENCE_REQUIRE_BULLISH_REACTION",
    "CONFLUENCE_MIN_CLOSE_POSITION",
    "CONFLUENCE_BREAKEVEN_ENABLED",
    "CONFLUENCE_BREAKEVEN_R_MULTIPLE",
    "CONFLUENCE_BREAKEVEN_LOCK_PCT",
    "CONFLUENCE_SL_BUFFER_PCT",
    "CONFLUENCE_MIN_RR_RATIO",
)

KUNCI_LAMA = (
    "PUMP_MIN_24H_CHANGE_PCT",
    "HTF_FILTER_ENABLED",
    "HTF_RESISTANCE_FILTER_ENABLED",
    "DIRECTION_FILTER_ENABLED",
    "DIRECTION_ZONE_FILTER_ENABLED",
    "TIMING_FILTER_ENABLED",
    "BREAKOUT_RETEST_FILTER_ENABLED",
    "ROLLING_VOLUME_FILTER_ENABLED",
    "DEMAND_ZONE_FILTER_ENABLED",
    "USE_ATR_EXIT",
    "ATR_PERIOD",
    "ATR_MULT_SL",
    "ATR_MULT_TP",
    "ATR_MULT_TRAIL",
    "ATR_MULT_BE_TRIGGER",
    "ATR_MULT_BE_LOCK",
    "ATR_MULT_TRAIL_START",
    "USE_TP",
    "TP_PCT",
    "USE_STOP_LOSS",
    "SL_PCT",
    "USE_BREAKEVEN",
    "BE_TRIGGER_PCT",
    "BE_LOCK_PCT",
    "USE_TRAILING",
    "TRAILING_START_PCT",
    "TRAILING_STEP_PCT",
)


def _cfg_4h(**overrides) -> dict:
    cfg = deepcopy(PUMP_CONFIG)
    cfg["CONFIRM_INTERVAL"] = "4h"
    cfg["CONFLUENCE_STRUCTURE_INTERVAL"] = "4h"
    cfg["CONFLUENCE_LOOKBACK_BARS"] = 40
    cfg["CONFIRM_LOOKBACK_BARS"] = 40
    cfg.update(overrides)
    return cfg


def cek_schema_dan_migrasi() -> str:
    assert not CONFIG_LOAD_ERRORS, f"CONFIG_LOAD_ERRORS tidak kosong: {CONFIG_LOAD_ERRORS}"
    for key in KUNCI_KONFLUENSI:
        assert key in PUMP_CONFIG, f"{key} hilang dari PUMP_CONFIG"
        assert key in PARAMETER_SCHEMA, f"{key} hilang dari PARAMETER_SCHEMA"
        assert key in bt.OVERRIDE_CASTERS, f"{key} hilang dari OVERRIDE_CASTERS"

    for key in KUNCI_LAMA:
        assert key not in PUMP_CONFIG, f"kunci lama {key} masih ada di PUMP_CONFIG"
        assert key not in PARAMETER_SCHEMA, f"kunci lama {key} masih ada di PARAMETER_SCHEMA"
        assert key in REMOVED_PARAMETERS, f"kunci lama {key} belum masuk REMOVED_PARAMETERS"

    payload_lama = {
        "PUMP_MIN_24H_CHANGE_PCT": 5.0,
        "HTF_FILTER_ENABLED": True,
        "DIRECTION_FILTER_ENABLED": True,
        "TIMING_FILTER_ENABLED": True,
        "BREAKOUT_RETEST_FILTER_ENABLED": True,
        "ROLLING_VOLUME_FILTER_ENABLED": True,
        "DEMAND_ZONE_FILTER_ENABLED": True,
        "CONFLUENCE_FILTER_ENABLED": True,
    }
    hasil_migrasi = _migrate_legacy_keys(payload_lama)
    _validate_override_payload(hasil_migrasi)
    assert hasil_migrasi == {"CONFLUENCE_FILTER_ENABLED": True}

    _, err_paper, _ = validate_candidate(deepcopy(PUMP_CONFIG), "PAPER")
    assert not err_paper, f"validasi default PAPER gagal: {err_paper}"
    assert PUMP_CONFIG["CONFIRM_INTERVAL"] == "15m"
    assert PUMP_CONFIG["CONFLUENCE_STRUCTURE_INTERVAL"] == "15m"
    assert PUMP_CONFIG["CONFLUENCE_REQUIRE_ALL_FIVE"] is True
    assert strategy.confluence_uses_mtf(PUMP_CONFIG) is False

    buruk_fib = deepcopy(PUMP_CONFIG)
    buruk_fib["FIB_RETRACEMENT_MIN"] = 0.80
    buruk_fib["FIB_RETRACEMENT_MAX"] = 0.50
    assert "FIB_RETRACEMENT_MIN" in relation_violations(buruk_fib)

    buruk_tf = deepcopy(PUMP_CONFIG)
    buruk_tf["CONFIRM_INTERVAL"] = "4h"
    buruk_tf["CONFLUENCE_STRUCTURE_INTERVAL"] = "15m"
    assert "CONFLUENCE_STRUCTURE_INTERVAL" in relation_violations(buruk_tf)

    buruk_lima = deepcopy(PUMP_CONFIG)
    buruk_lima["CONFLUENCE_REQUIRE_ALL_FIVE"] = True
    buruk_lima["OB_FILTER_ENABLED"] = False
    assert "OB_FILTER_ENABLED" in relation_violations(buruk_lima)

    longgar = deepcopy(PUMP_CONFIG)
    longgar["CONFLUENCE_REQUIRE_ALL_FIVE"] = False
    longgar["OB_FILTER_ENABLED"] = False
    assert "OB_FILTER_ENABLED" not in relation_violations(longgar)

    return (
        f"{len(KUNCI_KONFLUENSI)} parameter Confluence Code aktif, "
        f"{len(KUNCI_LAMA)} filter lama bermigrasi bersih, default 15m single timeframe"
    )


def cek_one_zone_engine() -> str:
    cfg = _cfg_4h()
    bars = build_one_zone_series()
    struktur = strategy.evaluate_confluence_structure(bars[:-1], cfg)

    assert struktur["ok"], f"struktur one zone seharusnya lolos: {struktur['reason']}"
    assert struktur["poi_low"] is not None and struktur["poi_high"] is not None
    assert struktur["score"] >= 90.0, f"skor konfluensi terlalu rendah: {struktur['score']}"
    assert all(struktur["checks"].values()), f"komponen belum lengkap: {struktur['checks']}"

    poi = cc.Zone(struktur["poi_low"], struktur["poi_high"], "POI")
    for nama, rendah, tinggi in (
        ("fibonacci", struktur.get("fib_zone_low"), struktur.get("fib_zone_high")),
        ("volume_profile", struktur.get("vp_zone_low"), struktur.get("vp_zone_high")),
        ("liquidity", struktur.get("liquidity_zone_low"), struktur.get("liquidity_zone_high")),
        ("fvg", struktur.get("fvg_zone_low"), struktur.get("fvg_zone_high")),
        ("order_block", struktur.get("ob_zone_low"), struktur.get("ob_zone_high")),
    ):
        assert rendah is not None and tinggi is not None, f"zona {nama} tidak terbentuk"
        zona = cc.Zone(float(rendah), float(tinggi), nama)
        assert cc.zone_overlap(zona, poi) >= 0.0, (
            f"{nama} tidak beririsan dengan POI {poi.low:.6g}..{poi.high:.6g} "
            f"(zona {float(rendah):.6g}..{float(tinggi):.6g})"
        )

    tanpa_vp = strategy.evaluate_confluence_structure(
        build_one_zone_series(hvn_in_discount=False)[:-1], cfg
    )
    assert not tanpa_vp["ok"] and tanpa_vp["stage"] == "VOLUME_PROFILE", tanpa_vp["reason"]

    tanpa_fvg = strategy.evaluate_confluence_structure(
        build_one_zone_series(with_fvg=False)[:-1], cfg
    )
    assert not tanpa_fvg["ok"] and tanpa_fvg["stage"] == "FVG", tanpa_fvg["reason"]

    lemah = strategy.evaluate_confluence_structure(
        build_one_zone_series(impulse_scale=0.25)[:-1], cfg
    )
    assert not lemah["ok"], "impuls lemah seharusnya ditolak"

    cfg_ob_off = _cfg_4h()
    cfg_ob_off["OB_FILTER_ENABLED"] = False
    bolong = strategy.evaluate_confluence_structure(bars[:-1], cfg_ob_off)
    assert not bolong["ok"] and "belum lengkap" in bolong["reason"], bolong["reason"]

    cfg_bebas = _cfg_4h()
    cfg_bebas["CONFLUENCE_REQUIRE_ALL_FIVE"] = False
    cfg_bebas["OB_FILTER_ENABLED"] = False
    longgar = strategy.evaluate_confluence_structure(bars[:-1], cfg_bebas)
    assert longgar["ok"], f"tanpa kewajiban 5/5 seharusnya lolos: {longgar['reason']}"

    cfg_off = _cfg_4h()
    cfg_off["CONFLUENCE_FILTER_ENABLED"] = False
    nonaktif = strategy.evaluate_confluence_structure(bars[:-1], cfg_off)
    assert nonaktif["ok"] and nonaktif["stage"] == "DISABLED"

    potong = strategy.evaluate_confluence_structure(
        bars, cfg, signal_close_time_ms=bars[-3].close_time
    )
    assert potong["bars"] <= len(bars), "filter anti look-ahead tidak memotong candle"

    return (
        f"one zone tervalidasi: POI {struktur['poi_low']:.6g}..{struktur['poi_high']:.6g}, "
        f"skor {struktur['score']:.0f}/100, lima komponen beririsan"
    )


def cek_pemilihan_impuls() -> str:
    harga = [
        100, 99, 100, 101, 103, 108, 113, 118, 120, 119, 118, 117, 116, 115,
        114, 113, 112, 111, 112, 115, 118, 121, 120, 119, 120,
    ]
    bars = []
    for i, tutup in enumerate(harga):
        buka = harga[i - 1] if i else tutup
        bars.append(
            make_bar(
                i,
                buka,
                max(buka, tutup) + 0.2,
                min(buka, tutup) - 0.2,
                tutup,
                step_ms=STEP_15M_MS,
            )
        )
    bars[3] = make_bar(3, 101.0, 101.2, 100.0, 101.0, step_ms=STEP_15M_MS)
    bars[17] = make_bar(17, 112.0, 112.2, 110.0, 111.0, step_ms=STEP_15M_MS)

    terkuat = cc.detect_impulse_leg(bars, 2.0, 0.05, "STRONGEST")
    terbaru = cc.detect_impulse_leg(bars, 2.0, 0.05, "RECENT")
    assert terkuat is not None and terbaru is not None
    assert terkuat.swing_low_index < terbaru.swing_low_index, (
        "mode STRONGEST harus memilih low yang lebih dalam dan lebih awal"
    )
    assert terbaru.swing_low > terkuat.swing_low, (
        f"mode RECENT harus memilih low terbaru yang lebih tinggi, dapat {terbaru.swing_low}"
    )
    assert abs(terbaru.swing_low - 110.0) < 1e-9, (
        f"low terbaru pada data uji adalah 110, dapat {terbaru.swing_low}"
    )
    assert terkuat.impulse_pct > terbaru.impulse_pct
    bawaan = cc.detect_impulse_leg(bars, 2.0, 0.05)
    assert bawaan.swing_low_index == terkuat.swing_low_index, (
        "bawaan harus mengikuti mode STRONGEST"
    )
    assert cc.detect_impulse_leg(bars, 50.0, 0.05) is None
    assert cc.detect_impulse_leg(bars[:10], 2.0, 0.05) is None

    return (
        "STRONGEST memilih low terdalam, RECENT memilih low terbaru, dan bawaan "
        "mengikuti hasil uji terbaik"
    )


def cek_trigger_reaction() -> str:
    cfg = _cfg_4h()
    bars = build_one_zone_series()
    struktur = strategy.evaluate_confluence_structure(bars[:-1], cfg)

    setup = detect_entry_setup(bars, cfg, structure_verdict=struktur)
    assert setup.ok, f"setup one zone seharusnya lolos: {setup.reason}"
    assert setup.sl_price is not None and setup.sl_price < float(bars[-1].close)
    assert setup.tp_price is not None and setup.tp_price > float(bars[-1].close)
    assert setup.rr_ratio is not None and setup.rr_ratio >= 1.0

    tanpa_sapuan = detect_entry_setup(
        build_one_zone_series(sweep_ssl=False), cfg, structure_verdict=struktur
    )
    assert not tanpa_sapuan.ok and "liquidity" in tanpa_sapuan.reason.lower(), (
        tanpa_sapuan.reason
    )

    bearish = detect_entry_setup(
        build_one_zone_series(bullish_reaction=False), cfg, structure_verdict=struktur
    )
    assert not bearish.ok and "bullish" in bearish.reason.lower(), bearish.reason

    cfg_ketat = _cfg_4h()
    cfg_ketat["CONFLUENCE_MIN_CLOSE_POSITION"] = 0.99
    ketat = detect_entry_setup(bars, cfg_ketat, structure_verdict=struktur)
    assert not ketat.ok and "close position" in ketat.reason, ketat.reason

    cfg_liq_off = _cfg_4h()
    cfg_liq_off["LIQUIDITY_FILTER_ENABLED"] = False
    cfg_liq_off["CONFLUENCE_REQUIRE_ALL_FIVE"] = False
    tanpa_filter = detect_entry_setup(
        build_one_zone_series(sweep_ssl=False), cfg_liq_off, structure_verdict=None
    )
    assert tanpa_filter.ok or "liquidity" not in tanpa_filter.reason.lower(), (
        f"filter likuiditas nonaktif tapi masih ditolak: {tanpa_filter.reason}"
    )

    return (
        f"reaksi tervalidasi: SL {setup.sl_price:.6g}, TP {setup.tp_price:.6g}, "
        f"R:R 1:{setup.rr_ratio:.2f}"
    )


def cek_scanner_dan_cache_live() -> str:
    cfg = _cfg_4h()
    cfg["MIN_QUOTE_VOLUME_USDT_24H"] = 1_000_000.0
    bars = build_one_zone_series()
    bars_bad = build_one_zone_series(bullish_reaction=False)

    class _DummyClient:
        def __init__(self) -> None:
            self.calls = 0

        def get_klines(self, symbol: str, interval: str, limit: int = 100):
            self.calls += 1
            return [
                [
                    k.open_time,
                    str(k.open),
                    str(k.high),
                    str(k.low),
                    str(k.close),
                    str(k.volume),
                    k.close_time,
                    str(k.quote_volume),
                ]
                for k in bars
            ]

    dummy = _DummyClient()
    cache = ConfluenceStructureCache(dummy, cfg)
    now_after_last = bars[-1].close_time + 1000
    pertama = cache.window_klines("AAAUSDT", now_after_last)
    kedua = cache.window_klines("AAAUSDT", now_after_last + 5000)
    expected_window = strategy.confluence_structure_window_bars(cfg)
    assert expected_window <= len(pertama) <= expected_window + 1 and dummy.calls == 1
    assert expected_window <= len(kedua) <= expected_window + 1 and dummy.calls == 1

    tickers = [
        {"symbol": "AAAUSDT", "quoteVolume": "8000000", "lastPrice": "103.5"},
        {"symbol": "BBBUSDT", "quoteVolume": "6000000", "lastPrice": "101.5"},
    ]
    diag = ScanDiagnostics()
    best = find_best_candidate(
        tickers,
        lambda sym: bars if sym == "AAAUSDT" else bars_bad,
        cfg,
        {"AAAUSDT", "BBBUSDT"},
        structure_provider=lambda sym: bars,
        diagnostics=diag,
    )
    assert best is not None and best.symbol == "AAAUSDT", f"kandidat terbaik salah: {best}"
    assert sum(diag.rejection_counts.values()) >= 1, "penolakan BBBUSDT tidak tercatat"
    assert any("The Confluence Code" in line for line in diag.log_lines(cfg))

    ok_gate, _ = evaluate_market_gate(5_000_000.0, cfg)
    assert ok_gate
    ok_book, _, _ = evaluate_orderbook(
        {
            "bids": [["100.0", "300"], ["99.9", "300"], ["99.8", "300"], ["99.7", "300"], ["99.6", "300"]],
            "asks": [["100.1", "200"], ["100.2", "200"], ["100.3", "200"], ["100.4", "200"], ["100.5", "200"]],
        },
        1_000.0,
        cfg,
    )
    assert ok_book

    return "ConfluenceStructureCache 4H, find_best_candidate, ScanDiagnostics, dan gerbang pasar sinkron"


def cek_jendela_live_dan_migrasi() -> str:
    from config.settings_schema import _migrate_confluence_scalping, _migrate_confluence_v2
    from infrastructure.storage import state as state_mod
    from market.market_scanner import diagnostic_stage
    from trading.pump_scanner_bot import ConfluenceStructureCache as _Cache

    cfg = deepcopy(PUMP_CONFIG)
    required = strategy.required_lookback_bars(cfg)
    window = strategy.confirm_window_bars(cfg)
    assert window >= required, (
        f"jendela konfirmasi {window} lebih kecil dari kebutuhan {required} sehingga "
        "struktur single timeframe kekurangan satu candle"
    )
    assert strategy.confluence_structure_required_bars(cfg) + 1 <= required, (
        "kebutuhan candle harus menyisakan satu candle sinyal di luar jendela struktur"
    )

    bars = build_flat_series(200, step_ms=STEP_15M_MS, price=100.0) + build_one_zone_series(
        start_index=200, step_ms=STEP_15M_MS
    )
    now_ms = bars[-1].open_time + STEP_15M_MS
    tertutup = [k for k in bars if k.close_time < now_ms]
    klines = tertutup[-window:]
    assert len(klines) == window

    struktur = strategy.evaluate_confluence_structure(
        klines, cfg, is_single_timeframe=True
    )
    assert struktur["ok"], (
        f"struktur single timeframe harus lolos pada jendela live {window} candle: "
        f"{struktur['reason']}"
    )

    mentah = [
        [
            k.open_time,
            str(k.open),
            str(k.high),
            str(k.low),
            str(k.close),
            str(k.volume),
            k.close_time,
            str(k.quote_volume),
        ]
        for k in bars
    ]

    class _Client:
        def get_klines(self, symbol, interval, limit=100):
            return mentah[-limit:]

    asli = state_mod.now_ms
    state_mod.now_ms = lambda: now_ms
    try:
        cache = _Cache(_Client(), cfg)
        for label, provider in (
            ("dengan cache struktur", cache.provider),
            ("tanpa penyedia struktur", None),
        ):
            diag = ScanDiagnostics()
            best = find_best_candidate(
                [
                    {
                        "symbol": "AAAUSDT",
                        "quoteVolume": "9000000",
                        "lastPrice": str(bars[-1].close),
                    }
                ],
                lambda symbol: klines,
                cfg,
                {"AAAUSDT"},
                structure_provider=provider,
                diagnostics=diag,
            )
            assert best is not None and best.setup is not None, (
                f"jalur live {label} menolak setup valid: {diag.rejection_counts} "
                f"{diag.examples}"
            )
            assert best.setup.stage == "OK", best.setup.stage
    finally:
        state_mod.now_ms = asli

    assert diagnostic_stage("ONE_ZONE", "") == "CONFLUENCE_STRUCTURE"
    assert diagnostic_stage("LIQUIDITY", "") == "CONFLUENCE_STRUCTURE"
    assert diagnostic_stage("ZONE_TOUCH", "") == "CONFLUENCE_TRIGGER"
    assert diagnostic_stage("REACTION_CONFIRM", "") == "CONFLUENCE_TRIGGER"
    assert diagnostic_stage("HISTORY_CONFIRM", "") == "HISTORY_CONFIRM"
    assert diagnostic_stage("", "data candle kurang: 5 dari 20") == "HISTORY_CONFIRM"
    assert diagnostic_stage("TAHAP_BARU", "alasan lain") == "CONFLUENCE_TRIGGER"

    lama = {
        "CONFIRM_INTERVAL": "5m",
        "CONFLUENCE_STRUCTURE_INTERVAL": "15m",
        "CONFLUENCE_LOOKBACK_BARS": 60,
        "CONFLUENCE_MIN_IMPULSE_PCT": 0.8,
        "VP_BINS": 15,
        "VP_HVN_TOP_RATIO": 0.25,
        "FIB_RETRACEMENT_MIN": 0.40,
        "FIB_RETRACEMENT_MAX": 0.786,
        "CONFLUENCE_MIN_CLOSE_POSITION": 0.50,
        "CONFLUENCE_SL_BUFFER_PCT": 0.20,
        "CONFLUENCE_MIN_RR_RATIO": 1.2,
        "MIN_QUOTE_VOLUME_USDT_24H": 5_000_000,
    }
    hasil, berubah = _migrate_confluence_v2(lama)
    assert berubah and hasil["CONFIRM_INTERVAL"] == "4h"
    assert hasil["CONFLUENCE_STRUCTURE_INTERVAL"] == "4h"
    assert hasil["VP_BINS"] == 24 and hasil["CONFLUENCE_MIN_RR_RATIO"] == 1.5
    assert hasil["MIN_QUOTE_VOLUME_USDT_24H"] == 5_000_000

    kustom = dict(lama)
    kustom["VP_BINS"] = 30
    hasil_kustom, berubah_kustom = _migrate_confluence_v2(kustom)
    assert not berubah_kustom and hasil_kustom == kustom, (
        "migrasi tidak boleh menimpa setelan yang sudah disesuaikan pengguna"
    )

    modern = {"CONFIRM_INTERVAL": "4h", "CONFLUENCE_STRUCTURE_INTERVAL": "4h"}
    assert _migrate_confluence_v2(modern) == (modern, False)

    paket_4h = {
        "CONFIRM_INTERVAL": "4h",
        "CONFLUENCE_STRUCTURE_INTERVAL": "4h",
        "CONFLUENCE_LOOKBACK_BARS": 120,
        "CONFIRM_LOOKBACK_BARS": 121,
        "CONFLUENCE_MIN_IMPULSE_PCT": 2.0,
        "VP_BINS": 24,
        "VP_HVN_TOP_RATIO": 0.30,
        "FIB_RETRACEMENT_MIN": 0.50,
        "FIB_RETRACEMENT_MAX": 0.79,
        "CONFLUENCE_MIN_CLOSE_POSITION": 0.60,
        "CONFLUENCE_SL_BUFFER_PCT": 0.50,
        "CONFLUENCE_MIN_RR_RATIO": 1.5,
        "MIN_QUOTE_VOLUME_USDT_24H": 5_000_000,
    }
    cfg_pas = deepcopy(PUMP_CONFIG)
    cfg_pas["MIN_QUOTE_VOLUME_USDT_24H"] = 1_000_000
    wajib = strategy.required_lookback_bars(cfg_pas)
    seri_pas = build_flat_series(200, price=100.0) + build_one_zone_series(start_index=200)
    kurang = detect_entry_setup(seri_pas[-(wajib - 1) :], cfg_pas)
    assert not kurang.ok and kurang.stage == "HISTORY_CONFIRM", (
        "satu candle kurang harus ditolak dengan alasan jumlah candle"
    )
    pas = detect_entry_setup(seri_pas[-wajib:], cfg_pas)
    assert pas.ok and pas.stage == "OK", (
        f"jumlah candle pas ({wajib}) harus diterima tanpa penyedia struktur: {pas.reason}"
    )

    hasil_scalp, berubah_scalp = _migrate_confluence_scalping(paket_4h)
    assert berubah_scalp, "paket 4H bawaan harus ikut pindah ke 15m"
    assert hasil_scalp["CONFIRM_INTERVAL"] == "15m"
    assert hasil_scalp["CONFLUENCE_STRUCTURE_INTERVAL"] == "15m"
    assert hasil_scalp["CONFLUENCE_LOOKBACK_BARS"] == 120
    assert hasil_scalp["MIN_QUOTE_VOLUME_USDT_24H"] == 5_000_000

    kustom_scalp = dict(paket_4h)
    kustom_scalp["CONFLUENCE_LOOKBACK_BARS"] = 300
    hasil_kustom_scalp, berubah_kustom_scalp = _migrate_confluence_scalping(kustom_scalp)
    assert not berubah_kustom_scalp and hasil_kustom_scalp == kustom_scalp, (
        "setelan yang sudah disesuaikan pengguna tidak boleh ditimpa migrasi timeframe"
    )

    sudah_15m = {"CONFIRM_INTERVAL": "15m", "CONFLUENCE_STRUCTURE_INTERVAL": "15m"}
    assert _migrate_confluence_scalping(sudah_15m) == (sudah_15m, False)

    return (
        f"jendela live {window} candle cukup untuk single timeframe 15m, jalur cache "
        "dan non-cache sama-sama entry, migrasi 4H ke 15m hanya menyentuh default lama"
    )


def cek_breakeven_mandiri() -> str:
    from trading import pump_scanner_bot as bot

    params = strategy.confluence_breakeven_params(PUMP_CONFIG)
    assert params["enabled"] is True
    assert abs(params["r_multiple"] - 1.0) < 1e-9
    assert abs(params["lock_pct"]) < 1e-9

    mati = deepcopy(PUMP_CONFIG)
    mati["CONFLUENCE_BREAKEVEN_ENABLED"] = False
    assert strategy.confluence_breakeven_params(mati)["enabled"] is False

    assert abs(strategy.confluence_breakeven_trigger(100.0, 98.0, 1.0) - 102.0) < 1e-9
    assert abs(strategy.confluence_breakeven_trigger(100.0, 98.0, 1.5) - 103.0) < 1e-9
    assert strategy.confluence_breakeven_trigger(100.0, 100.0, 1.0) == 0.0
    assert strategy.confluence_breakeven_trigger(0.0, 98.0, 1.0) == 0.0
    assert abs(strategy.confluence_breakeven_price(100.0, 98.0, 1.0, 0.0) - 100.0) < 1e-9
    assert abs(strategy.confluence_breakeven_price(100.0, 98.0, 1.0, 0.3) - 100.3) < 1e-9

    cfg = _cfg_4h()
    levels = {"sl_pct": 2.0, "tp_pct": 6.0, "source": "CONFLUENCE"}

    datar = parity.PositionState(100.0, levels)
    assert parity.evaluate_candle_exit(datar, make_bar(1, 100.5, 101.0, 99.5, 100.8), cfg) is None
    assert datar.be_active is False, "breakeven tidak boleh aktif sebelum pemicu tercapai"
    assert abs(datar.sl_price - 98.0) < 1e-9

    kena = parity.PositionState(100.0, levels)
    assert parity.evaluate_candle_exit(kena, make_bar(1, 100.5, 102.5, 100.2, 102.2), cfg) is None
    assert kena.be_active is True
    assert abs(kena.sl_price - 100.0) < 1e-9, "stop wajib pindah tepat ke harga entry"
    assert abs(kena.levels["sl_price"] - 100.0) < 1e-9
    keluar = parity.evaluate_candle_exit(kena, make_bar(2, 102.0, 102.4, 99.5, 99.8), cfg)
    assert keluar is not None and keluar[0] == "STOP_LOSS"
    assert abs(keluar[1] - 100.0) < 1e-9, "kerugian setelah breakeven harus nol"

    kunci = parity.PositionState(100.0, levels)
    cfg_kunci = _cfg_4h()
    cfg_kunci["CONFLUENCE_BREAKEVEN_LOCK_PCT"] = 0.5
    parity.evaluate_candle_exit(kunci, make_bar(1, 100.5, 102.5, 100.2, 102.2), cfg_kunci)
    assert abs(kunci.sl_price - 100.5) < 1e-9, "kunci profit harus menahan stop di atas entry"

    nonaktif = parity.PositionState(100.0, levels)
    parity.evaluate_candle_exit(nonaktif, make_bar(1, 100.5, 102.5, 100.2, 102.2), mati)
    assert nonaktif.be_active is False
    assert abs(nonaktif.sl_price - 98.0) < 1e-9

    pesanan: list[str] = []

    def _palsu_close(client, config, filters_cache, state, reason, **kwargs):
        pesanan.append(reason)

    asli_close = bot.close_position
    asli_native = bot._native_protection_enabled
    bot.close_position = _palsu_close
    bot._native_protection_enabled = lambda config: False
    try:
        cfg_uji = deepcopy(PUMP_CONFIG)
        cfg_uji["STATE_FILE"] = str(Path(tempfile.mkdtemp()) / "be_state.json")
        state = deepcopy(bot.DEFAULT_STATE)
        state["current_symbol"] = "AAAUSDT"
        state["entry_price"] = 100.0
        state["qty"] = 10.0
        state["sl_price"] = 98.0
        state["tp_price"] = 106.0
        bot.manage_exit(None, cfg_uji, {"AAAUSDT": None}, state, 102.2)
        assert not pesanan, "breakeven tidak boleh mengirim order jual apa pun"
        assert state["be_active"] is True
        assert abs(state["be_stop_price"] - 100.0) < 1e-9
        assert abs(state["sl_price"] - 100.0) < 1e-9

        pesanan.clear()
        bot.manage_exit(None, cfg_uji, {"AAAUSDT": None}, state, 101.0)
        assert not pesanan, "stop di titik impas tidak boleh memicu jual saat harga di atasnya"
        bot.manage_exit(None, cfg_uji, {"AAAUSDT": None}, state, 99.5)
        assert pesanan == ["STOP_LOSS"], "harga di bawah stop breakeven harus keluar"

        pesanan.clear()
        state2 = deepcopy(bot.DEFAULT_STATE)
        state2["current_symbol"] = "BBBUSDT"
        state2["entry_price"] = 100.0
        state2["qty"] = 10.0
        state2["sl_price"] = 98.0
        state2["tp_price"] = 106.0
        cfg_mati = deepcopy(mati)
        cfg_mati["STATE_FILE"] = str(Path(tempfile.mkdtemp()) / "be_mati.json")
        bot.manage_exit(None, cfg_mati, {"BBBUSDT": None}, state2, 102.2)
        assert state2["be_active"] is False, "saklar breakeven mati harus dihormati"
        assert abs(state2["sl_price"] - 98.0) < 1e-9
    finally:
        bot.close_position = asli_close
        bot._native_protection_enabled = asli_native

    return (
        "breakeven mandiri aktif di 1R tanpa order jual, stop pindah ke harga entry, "
        "kunci profit dan saklar mati dihormati"
    )


def cek_paritas_backtest_dan_portofolio() -> str:
    cfg = deepcopy(PUMP_CONFIG)
    cfg.update(
        {
            "_symbol": "TESTUSDT",
            "MODE": "PAPER",
            "CONFLUENCE_FILTER_ENABLED": True,
            "CONFIRM_INTERVAL": "5m",
            "CONFLUENCE_STRUCTURE_INTERVAL": "5m",
            "CONFLUENCE_LOOKBACK_BARS": 40,
            "CONFLUENCE_MIN_IMPULSE_PCT": 2.0,
            "BTC_FILTER_ENABLED": False,
            "MIN_QUOTE_VOLUME_USDT_24H": 0.0,
            "CONFLUENCE_SL_BUFFER_PCT": 0.50,
            "CONFLUENCE_MIN_RR_RATIO": 1.5,
            "USE_RISK_PERCENT": False,
            "POSITION_SIZE_USDT": 100.0,
            "MAX_POSITION_USDT": 0.0,
            "BACKTEST_INITIAL_EQUITY_USDT": 1000.0,
            "MAX_HOLD_HOURS": 0.0,
            "COOLDOWN_MINUTES_AFTER_CLOSE": 0,
            "MIN_SECONDS_BETWEEN_TRADES": 0,
            "MAX_CHASE_PCT": 0.0,
        }
    )

    bars = build_flat_series(300, step_ms=STEP_5M_MS, price=100.0)
    setup_bars = build_one_zone_series(
        start_index=len(bars), step_ms=STEP_5M_MS
    )
    bars.extend(setup_bars)
    index = len(bars)
    for step in range(15):
        point = 104.5 + (step + 1) * 0.60
        bars.append(
            pbt_helpers_bar(index, point - 0.2, point + 0.3, point - 0.3, point)
        )
        index += 1

    res_bt = bt.run_backtest(bars, cfg, warmup_bars=288)
    assert len(res_bt.trades) >= 1, (
        f"backtest Confluence tidak menghasilkan trade: {res_bt.warnings}"
    )

    store = pbt.new_backtest_store(cfg)
    try:
        store.write_symbol("TESTUSDT", bars)
        warmup_ms = 288 * 5 * bt.MS_PER_MIN
        res_pf = pbt.run_portfolio_backtest(store, cfg, "5m", warmup_ms=warmup_ms)
        cache_sig = pbt.precompute_entry_signals(store, cfg, "5m")
        res_pf_cached = pbt.run_portfolio_backtest(
            store, cfg, "5m", warmup_ms=warmup_ms, entry_signal_cache=cache_sig
        )
        jejak_bt = [(t.entry_time, t.exit_time, round(t.pnl_pct, 6), t.reason) for t in res_bt.trades]
        jejak_pf = [(t.entry_time, t.exit_time, round(t.pnl_pct, 6), t.reason) for t in res_pf.trades]
        jejak_cached = [
            (t.entry_time, t.exit_time, round(t.pnl_pct, 6), t.reason)
            for t in res_pf_cached.trades
        ]
        assert jejak_bt == jejak_pf == jejak_cached, (
            f"paritas gagal: bt={jejak_bt}, pf={jejak_pf}, cached={jejak_cached}"
        )
        res_pf_window = pbt.run_portfolio_backtest(
            store,
            cfg,
            "5m",
            warmup_ms=0,
            start_ms=bars[330].open_time,
            entry_signal_cache=cache_sig,
        )
        assert res_pf_window.confluence_trigger_scans < cache_sig.confluence_trigger_scans, (
            "confluence_trigger_scans_by_time harus menyaring bar di luar start_ms/end_ms"
        )
        assert res_pf_cached.confluence_trigger_scans >= res_pf.confluence_trigger_scans > 0
        btc_l1 = parity.BtcDropLookup(bars[:10], 300_000, 3)
        btc_l2 = parity.BtcDropLookup(bars[:10], 3, 300_000)
        assert btc_l1.drop_pct_at(bars[5].open_time) == btc_l2.drop_pct_at(bars[5].open_time)
        sum_pf = pbt.summarize_portfolio(res_pf)
        for kunci in (
            "total_return_pct",
            "gross_return_pct",
            "trades_per_day",
            "avg_trade_pct",
            "median_trade_pct",
            "payoff_ratio",
            "max_consecutive_losses",
            "reason_counts",
            "top_symbols",
            "equity_curve",
            "confluence_structure_scans",
            "confluence_structure_skips",
            "confluence_trigger_scans",
            "confluence_trigger_skips",
        ):
            assert kunci in sum_pf, f"kunci ringkasan portofolio hilang: {kunci}"
    finally:
        store.close()

    from backtesting.backtest_cache import KlineCache

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "kcache.sqlite3"
        with KlineCache(str(cache_path), fresh_hours=0.0, ttl_days=7.0) as kcache_inst:
            class _FakeClient:
                def get_klines(self, symbol, interval, limit=1000, start_time_ms=None, end_time_ms=None):
                    return [
                        [
                            k.open_time,
                            str(k.open),
                            str(k.high),
                            str(k.low),
                            str(k.close),
                            str(k.volume),
                            k.close_time,
                            str(k.quote_volume),
                        ]
                        for k in bars[:20]
                        if (start_time_ms is None or k.open_time >= start_time_ms)
                        and (end_time_ms is None or k.open_time <= end_time_ms)
                    ][:limit]

            pulled = pbt._klines_untuk_simbol(
                _FakeClient(),
                "TESTUSDT",
                "5m",
                bars[0].open_time,
                bars[19].open_time,
                kcache_inst,
            )
            assert len(pulled) == 20, f"KlineCache gagal melayani candle: {len(pulled)}"

    assert parity.RISK_LIMIT_REASON == "RISK_LIMIT_TRIGGERED"
    rc = parity.AccountRiskControls(
        {
            "USE_EQUITY_STOP": True,
            "MAX_DRAWDOWN_PERCENT": 5.0,
            "CLOSE_ALL_AT_LIMIT": True,
            "DD_COOLDOWN_HOURS": 1.0,
        },
        1000.0,
    )
    assert not rc.update(1000, 1000.0)
    paused = rc.update(2000, 940.0)
    assert paused and rc.force_close_due(paused, True) and not rc.force_close_due(paused, True)
    rc_sum = rc.summary()
    assert rc_sum["dd_stop"] == 1 and rc_sum["forced_close"] == 1

    assert gs.HasilKombinasi is gs.BarisGrid
    assert gs.buat_rentang(1.0, 2.0, 0.5, 4) == [1.0, 1.5, 2.0]
    komb_pos, _ = gs.expand_grid({"CONFLUENCE_SL_BUFFER_PCT": [0.1, 0.2]}, 150, False)
    assert len(komb_pos) == 2

    with pbt.KlineStore.from_klines({"TESTUSDT": bars}) as st_conf:
        hasil_conf_grid = gs.run_portfolio_grid_search(
            st_conf,
            deepcopy(cfg),
            "5m",
            warmup_ms=warmup_ms,
            spec={"CONFLUENCE_SL_BUFFER_PCT": [0.45, 0.55], "CONFLUENCE_MIN_RR_RATIO": [1.8, 2.2]},
            min_trades=1,
        )
        assert hasil_conf_grid.total_kombinasi == 4 and len(hasil_conf_grid.hasil) == 4

    return (
        f"paritas penuh tercapai ({len(res_bt.trades)} trade identik di single, "
        "portfolio, EntrySignalCache, dan KlineCache)"
    )


def pbt_helpers_bar(index: int, open_price: float, high: float, low: float, close: float):
    from backtesting.confluence_fixture import make_bar

    return make_bar(index, open_price, high, low, close, step_ms=STEP_5M_MS)


def cek_rekonsiliasi_debu() -> str:
    from trading import pump_scanner_bot as bot

    class KlienUji:
        def __init__(self, saldo: dict, harga: dict, gagal_harga: bool = False) -> None:
            self.saldo = saldo
            self.harga = harga
            self.gagal_harga = gagal_harga

        def get_account(self) -> dict:
            return {
                "balances": [
                    {"asset": aset, "free": str(jumlah), "locked": "0"}
                    for aset, jumlah in self.saldo.items()
                ],
                "permissions": ["SPOT"],
                "accountType": "SPOT",
                "canTrade": True,
            }

        def get_price(self, symbol: str, max_retries: int = 3) -> float:
            if self.gagal_harga:
                raise RuntimeError("harga tidak tersedia")
            return self.harga[symbol]

    harga = {"PEPEUSDT": 0.00001, "BTCUSDT": 80000.0}
    folder = Path(tempfile.mkdtemp())

    def jalankan(nama: str, saldo: dict, ubah: dict | None = None, **extra) -> tuple[bool, dict]:
        config = deepcopy(PUMP_CONFIG)
        config["MODE"] = "PAPER"
        config["STATE_FILE"] = str(folder / f"{nama}.json")
        for kunci, nilai in (ubah or {}).items():
            config[kunci] = nilai
        state = bot.load_pump_state(
            config["STATE_FILE"],
            fail_closed=True,
            allow_missing_state=True,
            mode="PAPER",
        )
        ok = bot.reconcile_state_with_exchange(KlienUji(saldo, harga, **extra), config, state)
        return ok, state

    ok_debu, state_debu = jalankan("debu", {"USDT": 981.66, "PEPE": 0.09})
    assert ok_debu is True, "debu di bawah ambang tidak boleh menggagalkan rekonsiliasi state kosong"
    assert not state_debu["reconciliation_required"], "debu tidak boleh memblokir entry baru"
    assert state_debu["reconciliation_reason"] == "", "debu tidak boleh mengisi alasan rekonsiliasi"
    assert [item["asset"] for item in state_debu["dust_assets"]] == ["PEPE"], (
        "aset debu harus tercatat di state agar bisa ditampilkan di dashboard"
    )
    assert state_debu["dust_assets"][0]["value_quote"] < PUMP_CONFIG["DUST_VALUE_THRESHOLD_USDT"]

    ok_posisi, state_posisi = jalankan("posisi", {"USDT": 900.0, "BTC": 0.000625})
    assert ok_posisi is False, "aset di atas ambang debu tetap harus memblokir entry"
    assert state_posisi["reconciliation_required"], "posisi tidak terkelola wajib memblokir entry"
    assert state_posisi["reconciliation_reason"] == "UNMANAGED_BASE_ASSETS"
    assert state_posisi["reconciliation_assets"] == ["BTC"]

    ok_tanpa_harga, state_tanpa_harga = jalankan(
        "tanpa_harga", {"USDT": 900.0, "PEPE": 0.09}, gagal_harga=True
    )
    assert ok_tanpa_harga is False, "aset tanpa harga harus dianggap tidak terkelola, bukan diabaikan"
    assert state_tanpa_harga["reconciliation_required"]

    ok_ambang_nol, state_ambang_nol = jalankan(
        "ambang_nol", {"USDT": 981.66, "PEPE": 0.09}, ubah={"DUST_VALUE_THRESHOLD_USDT": 0.0}
    )
    assert ok_ambang_nol is False, "ambang 0 berarti tidak ada toleransi debu"
    assert state_ambang_nol["reconciliation_required"]

    return (
        "debu 0,0009 USDT diabaikan tanpa memblokir entry, "
        "posisi 50 USDT dan aset tanpa harga tetap memblokir"
    )


def cek_integrasi_dashboard() -> str:
    try:
        from web import dashboard as dash
    except ImportError as exc:
        return f"cek dashboard dilewati ({exc})"

    for key in (
        "CONFLUENCE_FILTER_ENABLED",
        "CONFLUENCE_STRUCTURE_INTERVAL",
        "CONFLUENCE_LOOKBACK_BARS",
        "CONFLUENCE_REQUIRE_ALL_FIVE",
        "CONFLUENCE_MIN_SCORE",
        "VP_FILTER_ENABLED",
        "FIB_FILTER_ENABLED",
        "LIQUIDITY_FILTER_ENABLED",
        "LIQUIDITY_EQUAL_LEVEL_TOL_RATIO",
        "FVG_FILTER_ENABLED",
        "OB_FILTER_ENABLED",
        "CONFLUENCE_REQUIRE_BULLISH_REACTION",
    ):
        assert key in dash.BT_PARAM_KEYS, f"{key} belum terdaftar di dash.BT_PARAM_KEYS"

    dash.app.config["TESTING"] = True
    tc = dash.app.test_client()
    r_def = tc.get("/api/backtest/defaults")
    assert r_def.status_code == 200
    d_json = r_def.get_json()
    assert d_json["CONFLUENCE_FILTER_ENABLED"] is True
    assert d_json["CONFLUENCE_STRUCTURE_INTERVAL"] == "15m"
    assert d_json["CONFIRM_INTERVAL"] == "15m"
    assert d_json["CONFLUENCE_REQUIRE_ALL_FIVE"] is True
    assert d_json["VP_FILTER_ENABLED"] is True
    assert d_json["FIB_FILTER_ENABLED"] is True
    assert d_json["LIQUIDITY_FILTER_ENABLED"] is True
    assert d_json["FVG_FILTER_ENABLED"] is True
    assert d_json["OB_FILTER_ENABLED"] is True

    kepala = {
        "Host": "127.0.0.1:8080",
        "Origin": "http://127.0.0.1:8080",
        "Referer": "http://127.0.0.1:8080/",
        "X-Admin-Token": dash._ADMIN_TOKEN,
    }
    salah_bentuk = tc.post(
        "/api/backtest/start",
        json={"days": 60, "max_symbols": 2, "overrides": {"VP_BINS": 20}},
        headers=kepala,
    )
    assert salah_bentuk.status_code == 400, (
        "parameter yang dibungkus objek lain harus ditolak, bukan diabaikan diam-diam"
    )
    assert "overrides" in (salah_bentuk.get_json() or {}).get("error", "")

    seleksi_salah = tc.post(
        "/api/backtest/start",
        json={
            "days": 5,
            "max_symbols": 2,
            "CONFLUENCE_IMPULSE_SELECTION": "NGACO",
        },
        headers=kepala,
    )
    assert seleksi_salah.status_code == 400, (
        "nilai pemilihan impuls di luar pilihan harus ditolak"
    )
    assert "CONFLUENCE_IMPULSE_SELECTION" in (
        seleksi_salah.get_json() or {}
    ).get("error", "")

    kunci_asing = tc.post(
        "/api/backtest/start",
        json={"days": 60, "max_symbols": 2, "kunci_ngawur": 1},
        headers=kepala,
    )
    assert kunci_asing.status_code == 400
    assert "kunci_ngawur" in (kunci_asing.get_json() or {}).get("error", "")

    kombinasi_salah = tc.post(
        "/api/backtest/start",
        json={
            "days": 60,
            "max_symbols": 2,
            "OB_FILTER_ENABLED": False,
            "CONFLUENCE_REQUIRE_ALL_FIVE": True,
        },
        headers=kepala,
    )
    assert kombinasi_salah.status_code == 400, (
        "OB dimatikan padahal wajib 5 dari 5 harus ditolak endpoint backtest"
    )
    return "endpoint dashboard, BT_PARAM_KEYS, dan validasi body backtest tersinkron"


CEK: tuple[tuple[str, Callable[[], str]], ...] = (
    ("schema_dan_migrasi", cek_schema_dan_migrasi),
    ("one_zone_engine", cek_one_zone_engine),
    ("pemilihan_impuls", cek_pemilihan_impuls),
    ("trigger_reaction", cek_trigger_reaction),
    ("scanner_dan_cache_live", cek_scanner_dan_cache_live),
    ("jendela_live_dan_migrasi", cek_jendela_live_dan_migrasi),
    ("rekonsiliasi_debu", cek_rekonsiliasi_debu),
    ("breakeven_mandiri", cek_breakeven_mandiri),
    ("paritas_backtest_dan_portofolio", cek_paritas_backtest_dan_portofolio),
    ("integrasi_dashboard", cek_integrasi_dashboard),
)


def run(verbose: bool = True) -> tuple[bool, list[str]]:
    baris: list[str] = []
    gagal = 0
    for nama, fungsi in CEK:
        try:
            detail = fungsi()
        except AssertionError as exc:
            gagal += 1
            teks = f"[GAGAL] {nama}: {exc}"
        except Exception as exc:
            gagal += 1
            teks = f"[ERROR] {nama}: {type(exc).__name__}: {exc}"
        else:
            teks = f"[OK]    {nama}: {detail}"
        baris.append(teks)
        if verbose:
            print(teks)
    total = len(CEK)
    ringkas = (
        f"Selftest entry The Confluence Code selesai: {total - gagal}/{total} pemeriksaan lulus."
        if gagal == 0
        else f"Selftest entry The Confluence Code selesai: {gagal} dari {total} pemeriksaan GAGAL."
    )
    if verbose:
        print(ringkas)
    baris.append(ringkas)
    return gagal == 0, baris


if __name__ == "__main__":
    sukses, _ = run(verbose=True)
    raise SystemExit(0 if sukses else 1)
