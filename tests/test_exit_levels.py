"""Uji ATR dan resolusi level exit.

PERBAIKAN AUDIT 2026-09-30 (temuan TINGGI-06).

File ini menggantikan tests/test_pullback_retest.py. Berkas lama menguji
scanner.detect_pullback_retest, scanner.confirm_entry,
scanner._rolling_volume_confirmation, dan strategy.ema, yang semuanya sudah
dihapus bersama jalur sinyal entry pada commit "Hapus sinyal entry EMA, dll".
Tujuh dari delapan test di sana gagal dengan AttributeError dan hanya
membuat suite berisik tanpa melindungi apa pun.

Satu test yang masih relevan (invariant level exit) dipertahankan utuh di
bawah ini, ditambah beberapa test batas yang memperkuat cakupannya.
"""

from __future__ import annotations

from strategy import indicators as strategy


def candles(values):
    out = []
    for i, v in enumerate(values):
        volume = 3000.0 if i == len(values) - 1 else 1000.0
        out.append(strategy.Kline(i * 300000, v, v + 1, max(.01, v - 1), v,
                                  i * 300000 + 299999, volume, volume * v))
    return out


def test_atr_exit_menerapkan_invariant_dan_fallback():
    kl = candles([100 + (i % 3) for i in range(30)])
    atr_value = strategy.atr(kl, 14)
    assert atr_value is not None and atr_value > 0
    atr_levels = strategy.resolve_exit_levels({
        "USE_ATR_EXIT": True, "ATR_MULT_SL": 1.5,
        "ATR_MULT_TP": 3, "ATR_MULT_TRAIL": 2, "ATR_MULT_BE_TRIGGER": 2,
        "ATR_MULT_BE_LOCK": 3, "ATR_MULT_TRAIL_START": 1})
    assert atr_levels["source"] == "ATR"
    assert atr_levels["atr_mult_trail"] <= atr_levels["atr_mult_sl"]
    assert atr_levels["be_trigger_pct"] <= atr_levels["trail_start_pct"]
    assert atr_levels["be_lock_pct"] <= atr_levels["be_trigger_pct"]
    fixed = strategy.resolve_exit_levels({
        "USE_ATR_EXIT": False, "SL_PCT": 3, "TP_PCT": 6,
        "BE_TRIGGER_PCT": 2, "BE_LOCK_PCT": 1, "TRAILING_START_PCT": 4,
        "TRAILING_STEP_PCT": 5})
    assert fixed["source"] == "FIXED" and fixed["sl_pct"] == 3
    assert fixed["trail_step_pct"] <= fixed["sl_pct"]


def test_atr_mengembalikan_none_bila_data_kurang():
    assert strategy.atr(candles([100.0] * 3), 14) is None
    assert strategy.atr([], 14) is None


def test_atr_menolak_harga_datar_sempurna():
    flat = [strategy.Kline(i * 300000, 100.0, 100.0, 100.0, 100.0,
                           i * 300000 + 299999, 1000.0, 100000.0)
            for i in range(30)]
    assert strategy.atr(flat, 14) is None


def test_invariant_trailing_tidak_pernah_melebihi_stop_loss():
    levels = strategy.resolve_exit_levels({
        "USE_ATR_EXIT": False, "SL_PCT": 2, "TP_PCT": 10,
        "BE_TRIGGER_PCT": 1, "BE_LOCK_PCT": 0.5,
        "TRAILING_START_PCT": 3, "TRAILING_STEP_PCT": 99})
    assert levels["trail_step_pct"] <= levels["sl_pct"], (
        "trailing step dibiarkan melebihi SL; stop loss jadi tidak pernah relevan"
    )
