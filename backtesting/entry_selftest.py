from __future__ import annotations

from copy import deepcopy
from typing import Callable

from backtesting import backtest as bt
from backtesting import grid_search as gs
from backtesting import parity
from backtesting import portfolio_backtest as pbt
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
    confluence_structure_verdict,
    detect_entry_setup,
    evaluate_market_gate,
    evaluate_orderbook,
    find_best_candidate,
)
from strategy import indicators as strategy
from strategy.indicators import Kline
from trading.pump_scanner_bot import ConfluenceStructureCache


def _bar(
    index: int,
    open_price: float,
    high: float,
    low: float,
    close: float,
    *,
    step_ms: int = 300_000,
    volume: float = 1_000.0,
    quote_volume: float | None = None,
) -> Kline:
    start = index * step_ms
    qv = float(quote_volume) if quote_volume is not None else float(volume) * float(close)
    return Kline(
        start,
        float(open_price),
        float(high),
        float(low),
        float(close),
        start + step_ms - 1,
        float(volume),
        qv,
    )


def _build_confluence_m15_series(
    start_index: int = 0,
    *,
    with_fvg: bool = True,
    hvn_in_discount: bool = True,
    impulse_scale: float = 1.0,
) -> list[Kline]:
    step_ms = 900_000
    bars: list[Kline] = []
    idx = start_index
    for _ in range(55):
        bars.append(
            _bar(idx, 100.0, 100.8, 99.6, 100.2, step_ms=step_ms, volume=1_000.0)
        )
        idx += 1

    disc_vol = 9_500.0 if hvn_in_discount else 100.0
    top_vol = 800.0 if hvn_in_discount else 25_000.0

    base_low = 100.0 - 2.0 * impulse_scale
    base_close = base_low + 0.4 * impulse_scale
    bars.append(
        _bar(
            idx,
            100.2,
            100.4,
            base_low,
            base_close,
            step_ms=step_ms,
            volume=disc_vol,
        )
    )
    idx += 1

    p1_close = base_low + 3.2 * impulse_scale
    p1_high = base_low + 3.5 * impulse_scale
    bars.append(
        _bar(
            idx,
            base_close,
            p1_high,
            base_low + 0.2 * impulse_scale,
            p1_close,
            step_ms=step_ms,
            volume=disc_vol,
        )
    )
    idx += 1

    p2_open = p1_close
    p2_close = base_low + 7.5 * impulse_scale
    p2_high = base_low + 8.0 * impulse_scale
    p2_low = (
        (base_low + 3.0 * impulse_scale)
        if with_fvg
        else (base_low + 1.5 * impulse_scale)
    )
    bars.append(
        _bar(
            idx,
            p2_open,
            p2_high,
            p2_low,
            p2_close,
            step_ms=step_ms,
            volume=1_200.0 if hvn_in_discount else 50.0,
        )
    )
    idx += 1

    p3_open = p2_close
    p3_low = (
        (base_low + 4.0 * impulse_scale)
        if with_fvg
        else (base_low + 2.0 * impulse_scale)
    )
    p3_high = base_low + 9.0 * impulse_scale if not hvn_in_discount else base_low + 12.0 * impulse_scale
    p3_close = base_low + 8.8 * impulse_scale if not hvn_in_discount else base_low + 11.5 * impulse_scale
    bars.append(
        _bar(
            idx,
            p3_open,
            p3_high,
            p3_low,
            p3_close,
            step_ms=step_ms,
            volume=1_200.0 if hvn_in_discount else 50.0,
        )
    )
    idx += 1

    if not hvn_in_discount:
        bars.append(
            _bar(
                idx,
                base_low + 8.8 * impulse_scale,
                base_low + 12.0 * impulse_scale,
                base_low + 7.5 * impulse_scale,
                base_low + 11.8 * impulse_scale,
                step_ms=step_ms,
                volume=top_vol,
            )
        )
        idx += 1

    for _ in range(6):
        bars.append(
            _bar(
                idx,
                base_low + 6.5 * impulse_scale,
                base_low + 7.2 * impulse_scale,
                base_low + 5.2 * impulse_scale,
                base_low + 6.0 * impulse_scale,
                step_ms=step_ms,
                volume=1_500.0,
            )
        )
        idx += 1

    return bars


def _build_confluence_m5_trigger_series(
    start_index: int = 138,
    *,
    sweep_ssl: bool = True,
    bullish_reaction: bool = True,
) -> list[Kline]:
    step_ms = 300_000
    bars: list[Kline] = []
    idx = start_index
    for _ in range(52):
        bars.append(_bar(idx, 104.5, 105.0, 104.0, 104.6, step_ms=step_ms))
        idx += 1

    bars.append(_bar(idx, 104.6, 104.8, 103.8, 104.2, step_ms=step_ms))
    idx += 1
    bars.append(_bar(idx, 104.2, 104.9, 103.2, 104.5, step_ms=step_ms))
    idx += 1
    bars.append(_bar(idx, 104.5, 104.7, 103.9, 104.1, step_ms=step_ms))
    idx += 1
    bars.append(_bar(idx, 103.4, 103.6, 102.9, 103.1, step_ms=step_ms))
    idx += 1

    sweep_low = 101.2 if sweep_ssl else 103.3
    if bullish_reaction:
        bars.append(
            _bar(idx, 101.8, 104.1, sweep_low, 103.9, step_ms=step_ms, volume=4_000.0)
        )
    else:
        bars.append(
            _bar(idx, 103.5, 103.8, sweep_low, 101.5, step_ms=step_ms, volume=4_000.0)
        )
    return bars


def _build_single_tf_5m_confluence_series(start_index: int = 0) -> list[Kline]:
    step_ms = 300_000
    bars: list[Kline] = []
    idx = start_index
    for _ in range(55):
        bars.append(
            _bar(idx, 100.0, 100.6, 99.6, 100.1, step_ms=step_ms, volume=1_000.0)
        )
        idx += 1

    bars.append(_bar(idx, 100.1, 100.3, 98.0, 98.3, step_ms=step_ms, volume=9_500.0))
    idx += 1
    bars.append(_bar(idx, 98.3, 101.5, 98.1, 101.2, step_ms=step_ms, volume=9_500.0))
    idx += 1
    bars.append(_bar(idx, 101.2, 106.0, 101.0, 105.5, step_ms=step_ms, volume=1_500.0))
    idx += 1
    bars.append(_bar(idx, 105.5, 110.0, 102.0, 109.5, step_ms=step_ms, volume=1_200.0))
    idx += 1

    bars.append(_bar(idx, 109.5, 109.6, 104.5, 105.0, step_ms=step_ms, volume=1_200.0))
    idx += 1
    bars.append(_bar(idx, 105.0, 105.4, 103.2, 104.6, step_ms=step_ms, volume=1_200.0))
    idx += 1
    bars.append(_bar(idx, 104.6, 104.8, 103.9, 104.1, step_ms=step_ms, volume=1_200.0))
    idx += 1
    bars.append(_bar(idx, 103.5, 103.6, 102.9, 103.1, step_ms=step_ms, volume=1_200.0))
    idx += 1
    bars.append(_bar(idx, 101.8, 104.1, 101.1, 103.9, step_ms=step_ms, volume=4_200.0))
    return bars


def cek_schema_dan_migrasi() -> str:
    assert not CONFIG_LOAD_ERRORS, f"CONFIG_LOAD_ERRORS tidak kosong: {CONFIG_LOAD_ERRORS}"
    kunci_confluence = (
        "CONFLUENCE_FILTER_ENABLED",
        "CONFLUENCE_STRUCTURE_INTERVAL",
        "CONFLUENCE_LOOKBACK_BARS",
        "CONFLUENCE_MIN_IMPULSE_PCT",
        "CONFLUENCE_ZONE_TOLERANCE_RATIO",
        "VP_FILTER_ENABLED",
        "VP_BINS",
        "VP_HVN_TOP_RATIO",
        "FIB_FILTER_ENABLED",
        "FIB_RETRACEMENT_MIN",
        "FIB_RETRACEMENT_MAX",
        "LIQUIDITY_FILTER_ENABLED",
        "LIQUIDITY_SWEEP_LOOKBACK_BARS",
        "FVG_FILTER_ENABLED",
        "OB_FILTER_ENABLED",
        "CONFLUENCE_REQUIRE_BULLISH_REACTION",
        "CONFLUENCE_MIN_CLOSE_POSITION",
        "CONFLUENCE_SL_BUFFER_PCT",
        "CONFLUENCE_MIN_RR_RATIO",
    )
    for k in kunci_confluence:
        assert k in PUMP_CONFIG, f"{k} hilang dari PUMP_CONFIG"
        assert k in PARAMETER_SCHEMA, f"{k} hilang dari PARAMETER_SCHEMA"
        assert k in bt.OVERRIDE_CASTERS, f"{k} hilang dari OVERRIDE_CASTERS"

    kunci_lama = (
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
    for k in kunci_lama:
        assert k not in PUMP_CONFIG, f"kunci lama {k} masih ada di PUMP_CONFIG"
        assert k not in PARAMETER_SCHEMA, f"kunci lama {k} masih ada di PARAMETER_SCHEMA"
        assert k not in bt.OVERRIDE_CASTERS, f"kunci lama {k} masih ada di OVERRIDE_CASTERS"
        assert k in REMOVED_PARAMETERS, f"kunci lama {k} belum masuk REMOVED_PARAMETERS"

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

    buruk_fib = deepcopy(PUMP_CONFIG)
    buruk_fib["FIB_RETRACEMENT_MIN"] = 0.80
    buruk_fib["FIB_RETRACEMENT_MAX"] = 0.50
    pelanggaran = relation_violations(buruk_fib)
    assert "FIB_RETRACEMENT_MIN" in pelanggaran

    buruk_tf = deepcopy(PUMP_CONFIG)
    buruk_tf["CONFIRM_INTERVAL"] = "15m"
    buruk_tf["CONFLUENCE_STRUCTURE_INTERVAL"] = "5m"
    pelanggaran_tf = relation_violations(buruk_tf)
    assert "CONFLUENCE_STRUCTURE_INTERVAL" in pelanggaran_tf

    cfg_off_1h = deepcopy(PUMP_CONFIG)
    cfg_off_1h["CONFLUENCE_FILTER_ENABLED"] = False
    cfg_off_1h["CONFIRM_INTERVAL"] = "1h"
    assert not relation_violations(cfg_off_1h, mode_aware=True)
    bt.validate_params(cfg_off_1h)
    assert "CONFLUENCE_STRUCTURE_INTERVAL" in relation_violations(
        cfg_off_1h, mode_aware=False
    )

    return f"{len(kunci_confluence)} parameter Confluence aktif dan {len(kunci_lama)} filter lama dimigrasikan bersih"


def cek_lima_komponen_confluence() -> str:
    cfg = deepcopy(PUMP_CONFIG)
    m15 = _build_confluence_m15_series()
    m5 = _build_confluence_m5_trigger_series()

    struktur = strategy.evaluate_confluence_structure(m15, cfg)
    assert struktur["ok"], f"struktur M15 seharusnya lolos: {struktur['reason']}"
    assert struktur["poi_low"] is not None and struktur["poi_high"] is not None
    assert struktur["vp_poc"] is not None
    assert struktur["fvg_low"] is not None and struktur["ob_low"] is not None

    setup = detect_entry_setup(m5, cfg, structure_verdict=struktur)
    assert setup.ok, f"setup M15+M5 seharusnya lolos: {setup.reason}"

    m15_lemah = _build_confluence_m15_series(impulse_scale=0.05)
    s_lemah = strategy.evaluate_confluence_structure(m15_lemah, cfg)
    assert not s_lemah["ok"] and "BoS" in s_lemah["reason"]

    cfg_bos_saja = deepcopy(cfg)
    for k_sub in (
        "VP_FILTER_ENABLED",
        "FIB_FILTER_ENABLED",
        "LIQUIDITY_FILTER_ENABLED",
        "FVG_FILTER_ENABLED",
        "OB_FILTER_ENABLED",
        "CONFLUENCE_REQUIRE_BULLISH_REACTION",
    ):
        cfg_bos_saja[k_sub] = False
    assert strategy.confluence_enabled(cfg_bos_saja) is True
    assert not strategy.evaluate_confluence_structure(m15_lemah, cfg_bos_saja)["ok"]
    s_bos_ok = strategy.evaluate_confluence_structure(m15, cfg_bos_saja)
    assert s_bos_ok["ok"]
    assert detect_entry_setup(m5, cfg_bos_saja, structure_verdict=s_bos_ok).ok

    m15_tanpa_vp = _build_confluence_m15_series(hvn_in_discount=False)
    s_tanpa_vp = strategy.evaluate_confluence_structure(m15_tanpa_vp, cfg)
    assert not s_tanpa_vp["ok"], "seharusnya gagal ketika HVN tidak di area diskon"
    cfg_vp_off = deepcopy(cfg)
    cfg_vp_off["VP_FILTER_ENABLED"] = False
    assert strategy.evaluate_confluence_structure(m15_tanpa_vp, cfg_vp_off)["ok"]

    m15_tanpa_fvg = _build_confluence_m15_series(with_fvg=False)
    s_tanpa_fvg = strategy.evaluate_confluence_structure(m15_tanpa_fvg, cfg)
    assert not s_tanpa_fvg["ok"] and "FVG" in s_tanpa_fvg["reason"]
    cfg_fvg_off = deepcopy(cfg)
    cfg_fvg_off["FVG_FILTER_ENABLED"] = False
    assert strategy.evaluate_confluence_structure(m15_tanpa_fvg, cfg_fvg_off)["ok"]

    m5_tanpa_sweep = _build_confluence_m5_trigger_series(sweep_ssl=False)
    setup_no_sweep = detect_entry_setup(m5_tanpa_sweep, cfg, structure_verdict=struktur)
    assert not setup_no_sweep.ok

    m5_bearish = _build_confluence_m5_trigger_series(bullish_reaction=False)
    setup_bearish = detect_entry_setup(m5_bearish, cfg, structure_verdict=struktur)
    assert not setup_bearish.ok and "reaksi" in setup_bearish.reason

    s5m = _build_single_tf_5m_confluence_series()
    cfg_single = deepcopy(cfg)
    cfg_single["CONFLUENCE_STRUCTURE_INTERVAL"] = "5m"
    setup_single = detect_entry_setup(s5m, cfg_single)
    assert setup_single.ok, f"single-TF 5m seharusnya lolos: {setup_single.reason}"

    m5_sweep_bar3 = list(m5[:-2]) + [
        _bar(193, 103.5, 103.8, 101.1, 103.3, step_ms=300_000, volume=3_500.0),
        _bar(194, 103.3, 103.7, 102.6, 103.4, step_ms=300_000, volume=2_500.0),
        _bar(195, 102.2, 104.2, 102.1, 104.0, step_ms=300_000, volume=4_000.0),
    ]
    setup_sweep3 = detect_entry_setup(m5_sweep_bar3, cfg, structure_verdict=struktur)
    assert setup_sweep3.ok, f"sweep SSL pada bar ke-3 terakhir seharusnya lolos: {setup_sweep3.reason}"

    struktur_minimal = {
        "ok": True,
        "poi_low": struktur["poi_low"],
        "poi_high": struktur["poi_high"],
        "fib_low": struktur["fib_low"],
        "swing_high": struktur["swing_high"],
        "swing_high_idx": 0,
    }
    trig_min = strategy.evaluate_confluence_trigger(m5, cfg, structure=struktur_minimal)
    assert trig_min["ok"], f"struktur minimal alias seharusnya lolos: {trig_min['reason']}"

    return "kelima komponen (VP, Fib, Liquidity, FVG, OB) + reaksi bullish tervalidasi MTF dan Single-TF"


def cek_scanner_dan_cache_live() -> str:
    cfg = deepcopy(PUMP_CONFIG)
    cfg["MIN_QUOTE_VOLUME_USDT_24H"] = 1_000_000.0

    m15 = _build_confluence_m15_series()
    m5 = _build_confluence_m5_trigger_series()

    class _DummyClient:
        def __init__(self) -> None:
            self.calls = 0

        def get_klines(self, symbol: str, interval: str, limit: int = 100):
            self.calls += 1
            src = m15 if interval == "15m" else m5
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
                for k in src
            ]

    dummy = _DummyClient()
    cache = ConfluenceStructureCache(dummy, cfg)
    now_after_last = m15[-1].close_time + 1000
    kl_cached_1 = cache.window_klines("AAAUSDT", now_after_last)
    kl_cached_2 = cache.window_klines("AAAUSDT", now_after_last + 5000)
    expected_window = strategy.confluence_structure_window_bars(cfg)
    assert len(kl_cached_1) == expected_window and dummy.calls == 1
    assert len(kl_cached_2) == expected_window and dummy.calls == 1

    tickers = [
        {
            "symbol": "AAAUSDT",
            "quoteVolume": "8000000",
            "lastPrice": "103.5",
        },
        {
            "symbol": "BBBUSDT",
            "quoteVolume": "6000000",
            "lastPrice": "101.5",
        },
    ]
    m5_bad = _build_confluence_m5_trigger_series(bullish_reaction=False)
    diag = ScanDiagnostics()
    best = find_best_candidate(
        tickers,
        lambda sym: m5 if sym == "AAAUSDT" else m5_bad,
        cfg,
        {"AAAUSDT", "BBBUSDT"},
        structure_provider=lambda sym: m15,
        diagnostics=diag,
    )
    assert best is not None and best.symbol == "AAAUSDT"
    assert diag.rejection_counts.get("CONFLUENCE_TRIGGER", 0) == 1
    lines = diag.log_lines(cfg)
    assert any("The Confluence Code" in line for line in lines)

    ok_gate, _ = evaluate_market_gate(5_000_000.0, cfg)
    assert ok_gate
    ok_book, _, _ = evaluate_orderbook(
        {
            "bids": [
                ["100.0", "300"],
                ["99.9", "300"],
                ["99.8", "300"],
                ["99.7", "300"],
                ["99.6", "300"],
            ],
            "asks": [
                ["100.1", "200"],
                ["100.2", "200"],
                ["100.3", "200"],
                ["100.4", "200"],
                ["100.5", "200"],
            ],
        },
        1_000.0,
        cfg,
    )
    assert ok_book

    return "ConfluenceStructureCache, find_best_candidate, ScanDiagnostics, dan gerbang pasar/orderbook sinkron"


def cek_paritas_backtest_dan_portofolio() -> str:
    cfg = deepcopy(PUMP_CONFIG)
    cfg.update(
        {
            "_symbol": "TESTUSDT",
            "MODE": "PAPER",
            "CONFLUENCE_FILTER_ENABLED": True,
            "CONFLUENCE_STRUCTURE_INTERVAL": "5m",
            "CONFLUENCE_LOOKBACK_BARS": 45,
            "BTC_FILTER_ENABLED": False,
            "MIN_QUOTE_VOLUME_USDT_24H": 0.0,
            "CONFLUENCE_SL_BUFFER_PCT": 0.20,
            "CONFLUENCE_MIN_RR_RATIO": 1.2,
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

    bars: list[Kline] = []
    idx = 0
    for _ in range(300):
        bars.append(_bar(idx, 100.0, 100.5, 99.5, 100.0))
        idx += 1
    setup_bars = _build_single_tf_5m_confluence_series(start_index=idx)
    bars.extend(setup_bars)
    idx = len(bars)
    for step in range(15):
        p = 103.6 + (step + 1) * 0.50
        bars.append(_bar(idx, p - 0.2, p + 0.3, p - 0.3, p))
        idx += 1

    res_bt = bt.run_backtest(bars, cfg, warmup_bars=288)
    assert len(res_bt.trades) >= 1, f"backtest Confluence tidak menghasilkan trade: {res_bt.warnings}"

    store = pbt.new_backtest_store(cfg)
    try:
        store.write_symbol("TESTUSDT", bars)
        warmup_ms = 288 * 5 * bt.MS_PER_MIN
        res_pf = pbt.run_portfolio_backtest(store, cfg, "5m", warmup_ms=warmup_ms)
        cache_sig = pbt.precompute_entry_signals(store, cfg, "5m")
        res_pf_cached = pbt.run_portfolio_backtest(
            store, cfg, "5m", warmup_ms=warmup_ms, entry_signal_cache=cache_sig
        )
        jejak_bt = [
            (t.entry_time, t.exit_time, round(t.pnl_pct, 6), t.reason)
            for t in res_bt.trades
        ]
        jejak_pf = [
            (t.entry_time, t.exit_time, round(t.pnl_pct, 6), t.reason)
            for t in res_pf.trades
        ]
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
        for k_sum in (
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
            assert k_sum in sum_pf, f"kunci ringkasan portofolio hilang: {k_sum}"
    finally:
        store.close()

    import tempfile
    from pathlib import Path
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

    from backtesting import grid_search as gs

    assert gs.HasilKombinasi is gs.BarisGrid
    assert gs.buat_rentang(1.0, 2.0, 0.5, 4) == [1.0, 1.5, 2.0]
    komb_pos, _ = gs.expand_grid({"CONFLUENCE_SL_BUFFER_PCT": [0.1, 0.2]}, 150, False)
    assert len(komb_pos) == 2

    with pbt.KlineStore.from_klines({"TESTUSDT": bars}) as st_conf:
        cfg_conf_grid = deepcopy(cfg)
        hasil_conf_grid = gs.run_portfolio_grid_search(
            st_conf,
            cfg_conf_grid,
            "5m",
            warmup_ms=warmup_ms,
            spec={"CONFLUENCE_SL_BUFFER_PCT": [0.15, 0.25], "CONFLUENCE_MIN_RR_RATIO": [1.0, 1.4]},
            min_trades=1,
        )
        assert hasil_conf_grid.total_kombinasi == 4 and len(hasil_conf_grid.hasil) == 4

    return f"paritas penuh tercapai ({len(res_bt.trades)} trade identik di single, portfolio, EntrySignalCache, dan KlineCache)"


def cek_integrasi_dashboard() -> str:
    try:
        from web import dashboard as dash
    except ImportError as exc:
        return f"cek dashboard dilewati ({exc})"

    for k in (
        "CONFLUENCE_FILTER_ENABLED",
        "CONFLUENCE_STRUCTURE_INTERVAL",
        "CONFLUENCE_LOOKBACK_BARS",
        "VP_FILTER_ENABLED",
        "FIB_FILTER_ENABLED",
        "LIQUIDITY_FILTER_ENABLED",
        "FVG_FILTER_ENABLED",
        "OB_FILTER_ENABLED",
        "CONFLUENCE_REQUIRE_BULLISH_REACTION",
    ):
        assert k in dash.BT_PARAM_KEYS, f"{k} belum terdaftar di dash.BT_PARAM_KEYS"

    dash.app.config["TESTING"] = True
    tc = dash.app.test_client()
    r_def = tc.get("/api/backtest/defaults")
    assert r_def.status_code == 200
    d_json = r_def.get_json()
    assert d_json["CONFLUENCE_FILTER_ENABLED"] is True
    assert d_json["CONFLUENCE_STRUCTURE_INTERVAL"] == "15m"
    assert d_json["VP_FILTER_ENABLED"] is True
    assert d_json["FIB_FILTER_ENABLED"] is True
    assert d_json["LIQUIDITY_FILTER_ENABLED"] is True
    assert d_json["FVG_FILTER_ENABLED"] is True
    assert d_json["OB_FILTER_ENABLED"] is True
    return "endpoint dashboard dan BT_PARAM_KEYS tersinkron penuh dengan Confluence Code"


CEK: tuple[tuple[str, Callable[[], str]], ...] = (
    ("schema_dan_migrasi", cek_schema_dan_migrasi),
    ("lima_komponen_confluence", cek_lima_komponen_confluence),
    ("scanner_dan_cache_live", cek_scanner_dan_cache_live),
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
        f"Selftest entry Confluence Code selesai: {total - gagal}/{total} pemeriksaan lulus."
        if gagal == 0
        else f"Selftest entry Confluence Code selesai: {gagal} dari {total} pemeriksaan GAGAL."
    )
    baris.append(ringkas)
    if verbose:
        print(ringkas)
    return gagal == 0, baris


if __name__ == "__main__":
    sukses, _ = run(verbose=True)
    raise SystemExit(0 if sukses else 1)
