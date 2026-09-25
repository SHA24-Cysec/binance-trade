"""Uji strategi entry pullback retest dan paritas mesin backtest."""
from __future__ import annotations

import backtest
import config as cfg_mod
import market_scanner as scanner
import pytest
import strategy
from synthetic_data import (
    cfg_gerbang_pump_nonaktif, lanjutan_setelah_entry, make_candle, riwayat_harian,
    seri_dengan_setup, skenario_pullback_retest,
)

CFG = dict(cfg_mod.PUMP_CONFIG)
CFG.update({
    "CONFIRM_LOOKBACK_BARS": 48,
    "SWING_LOOKBACK_BARS": 12,
    "SWING_PIVOT_WING_BARS": 2,
    "VWAP_MIN_BARS_AFTER_ANCHOR": 2,
    "MAX_BARS_BREAKOUT_TO_RETEST": 12,
    "MAX_RETEST_TOUCHES": 1,
})


# ======================================================================
# anchored_vwap
# ======================================================================

def test_anchored_vwap_cocok_dengan_hitungan_manual():
    kl = [
        make_candle(0, 10, 12, 8, 10, volume=100.0),
        make_candle(1, 10, 22, 14, 18, volume=50.0),
        make_candle(2, 18, 26, 22, 24, volume=25.0),
    ]
    qv1 = 50.0 * 18.0
    qv2 = 25.0 * 24.0
    manual = (qv1 + qv2) / (50.0 + 25.0)
    hasil = strategy.anchored_vwap(kl, 1)
    assert hasil is not None
    assert abs(hasil - manual) < 1e-9


def test_anchored_vwap_none_saat_volume_nol():
    kl = [make_candle(i, 10, 11, 9, 10, volume=0.0) for i in range(3)]
    assert strategy.anchored_vwap(kl, 0) is None


def test_anchored_vwap_none_saat_indeks_di_luar_jangkauan():
    kl = [make_candle(i, 10, 11, 9, 10, volume=5.0) for i in range(3)]
    assert strategy.anchored_vwap(kl, 5) is None
    assert strategy.anchored_vwap([], 0) is None


def test_anchored_vwap_memakai_quote_volume_bukan_close_saja():
    kl = [
        make_candle(0, 10, 10, 10, 10, volume=1.0),
        make_candle(1, 20, 20, 20, 20, volume=1000.0),
    ]
    hasil = strategy.anchored_vwap(kl, 0)
    assert hasil is not None
    assert hasil > 19.9, hasil


# ======================================================================
# Jumlah candle minimum
# ======================================================================

def test_required_lookback_bars_mengikuti_parameter_struktur():
    c = dict(CFG)
    c.update({"SWING_LOOKBACK_BARS": 10, "SWING_PIVOT_WING_BARS": 2,
              "MAX_BARS_BREAKOUT_TO_RETEST": 8})
    assert strategy.required_lookback_bars(c) == 22
    c["SWING_LOOKBACK_BARS"] = 2
    c["MAX_BARS_BREAKOUT_TO_RETEST"] = 1
    assert strategy.required_lookback_bars(c) == 7


def test_confirm_window_bars_tidak_pernah_di_bawah_minimum():
    c = dict(CFG)
    c["CONFIRM_LOOKBACK_BARS"] = 3
    assert strategy.confirm_window_bars(c) == strategy.required_lookback_bars(c)
    c["CONFIRM_LOOKBACK_BARS"] = 500
    assert strategy.confirm_window_bars(c) == 500


# ======================================================================
# Skenario deteksi
# ======================================================================

def test_skenario_lolos():
    hasil = scanner.detect_pullback_retest(skenario_pullback_retest("lolos"), CFG)
    assert hasil.ok, hasil.reason
    assert hasil.breakout_level == 101
    assert hasil.zone_low == hasil.breakout_level == hasil.zone_high
    assert hasil.anchored_vwap is not None


def test_skenario_gagal_beserta_alasannya():
    kasus = {
        "wick_saja": "tidak ada breakout",
        "close_di_bawah_level": "tidak kembali di atas level",
        "close_lemah": "dari range",
        "kedaluwarsa": "candle sejak breakout",
        "di_bawah_vwap": "di bawah anchored VWAP",
        "volume_nol": "anchored VWAP tidak bisa dihitung",
        "data_kurang": "minimum",
    }
    for nama, potongan in kasus.items():
        hasil = scanner.detect_pullback_retest(skenario_pullback_retest(nama), CFG)
        assert not hasil.ok, f"{nama} seharusnya ditolak"
        assert potongan in hasil.reason, f"{nama}: {hasil.reason}"


def test_daftar_kosong_tidak_membuat_crash():
    assert not scanner.detect_pullback_retest([], CFG).ok
    assert not scanner.detect_pullback_retest([make_candle(0, 1, 1, 1, 1)], CFG).ok


def test_confirm_entry_tetap_mengembalikan_bool_dan_alasan():
    ok, alasan = scanner.confirm_entry(skenario_pullback_retest("lolos"), CFG)
    assert ok is True and isinstance(alasan, str)
    ok2, alasan2 = scanner.confirm_entry(skenario_pullback_retest("wick_saja"), CFG)
    assert ok2 is False and isinstance(alasan2, str)


# ======================================================================
# Tanpa look-ahead
# ======================================================================

def test_deteksi_tidak_berubah_saat_candle_masa_depan_ditambahkan():
    dasar = skenario_pullback_retest("lolos")
    awal = scanner.detect_pullback_retest(dasar, CFG)
    for arah in ("naik", "turun", "datar"):
        panjang = dasar + lanjutan_setelah_entry(dasar, arah, 20)
        ulang = scanner.detect_pullback_retest(panjang[:len(dasar)], CFG)
        assert ulang == awal, f"deteksi berubah setelah menambahkan candle {arah}"


def test_pivot_kanan_yang_belum_tertutup_tidak_dipakai():
    kl = [make_candle(i, 100, 100.2, 99.8, 100, volume=1000.0) for i in range(30)]
    kl[-1] = make_candle(29, 100, 120, 99.8, 119, volume=1000.0)
    hasil = scanner.detect_pullback_retest(kl, CFG)
    assert not hasil.ok
    assert "pivot" in hasil.reason, hasil.reason


# ======================================================================
# Paritas live dan backtest
# ======================================================================

def test_paritas_deteksi_live_dan_backtest():
    kl = seri_dengan_setup(ekor="naik", panjang_ekor=20)
    window = strategy.confirm_window_bars(CFG)
    entry_bar = len(kl) - 21

    langsung = scanner.detect_pullback_retest(
        kl[max(0, entry_bar - window + 1): entry_bar + 1], CFG)
    assert langsung.ok, langsung.reason

    cfg_bt = cfg_gerbang_pump_nonaktif(CFG)
    cfg_bt["MIN_QUOTE_VOLUME_USDT_24H"] = 1_000_000
    hasil = backtest.run_backtest(kl, cfg_bt, warmup_bars=0,
                                  daily_klines=riwayat_harian(kl))
    assert len(hasil.trades) == 1
    trade = hasil.trades[0]
    assert trade.entry_time == kl[entry_bar].close_time
    assert abs(trade.entry_price - kl[entry_bar].close) < 1e-12


# ======================================================================
# Batas waktu lama sudah tidak ada
# ======================================================================

def test_posisi_tidak_pernah_ditutup_karena_batas_waktu():
    kl = seri_dengan_setup(ekor="bertahan", panjang_ekor=60)
    cfg_bt = cfg_gerbang_pump_nonaktif(CFG)
    cfg_bt.update({"MIN_QUOTE_VOLUME_USDT_24H": 1_000_000,
                   "USE_STOP_LOSS": False, "USE_TP": False,
                   "USE_BREAKEVEN": False, "USE_TRAILING": False})
    hasil = backtest.run_backtest(kl, cfg_bt, warmup_bars=0,
                                  daily_klines=riwayat_harian(kl))
    assert [t.reason for t in hasil.trades] == ["END_OF_DATA"]
    assert hasil.trades[0].hold_minutes > 45


def test_alasan_exit_max_hold_time_tidak_ada_lagi_di_kode():
    import inspect
    import portfolio_backtest
    import pump_scanner_bot

    for mod in (backtest, portfolio_backtest, pump_scanner_bot, scanner):
        src = inspect.getsource(mod)
        assert "MAX_HOLD_TIME" not in src, mod.__name__
        assert "MAX_HOLD_MINUTES" not in src, mod.__name__


# ======================================================================
# Regresi audit B-06: fill exit gap-aware di kedua mesin backtest
# ======================================================================

def test_backtest_sl_tertembus_gap_diisi_pada_open():
    import portfolio_backtest as pbt

    kl = seri_dengan_setup(ekor="bertahan", panjang_ekor=0)
    entry = kl[-1].close
    idx = len(kl)
    kl.append(make_candle(idx, entry * 0.95, entry * 0.951, entry * 0.949,
                          entry * 0.95, volume=5_000_000.0))

    cfg = cfg_gerbang_pump_nonaktif(dict(CFG))
    cfg.update({
        "MIN_QUOTE_VOLUME_USDT_24H": 1_000_000,
        "USE_STOP_LOSS": True, "SL_PCT": 3.0,
        "USE_TP": False, "USE_BREAKEVEN": False, "USE_TRAILING": False,
        "_symbol": "AUSDT",
    })
    daily = riwayat_harian(kl, quote_volume_harian=1_000.0)

    hasil = backtest.run_backtest(kl, cfg, warmup_bars=0, daily_klines=daily)
    assert len(hasil.trades) == 1
    t = hasil.trades[0]
    assert t.reason == "STOP_LOSS"
    assert t.exit_price == pytest.approx(entry * 0.95)
    assert t.gross_pnl_pct == pytest.approx(-5.0)

    res_p = pbt.run_portfolio_backtest({"AUSDT": kl}, cfg, "5m", daily_klines={"AUSDT": daily})
    assert len(res_p.trades) == 1
    tp = res_p.trades[0]
    assert tp.reason == "STOP_LOSS"
    assert tp.exit_price == pytest.approx(entry * 0.95)
    assert tp.gross_pnl_pct == pytest.approx(-5.0)
