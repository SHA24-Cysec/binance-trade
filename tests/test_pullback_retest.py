"""Uji strategi entry pullback retest beserta exit SETUP_INVALIDATED.

Semua data candle berasal dari synthetic_data.py, modul yang SAMA dengan yang
dipakai selftest bot, backtest satu simbol, dan backtest portofolio. Tujuannya
supaya kalau kontrak deteksi berubah, semua lapisan gagal bersamaan, bukan
hanya salah satunya.
"""
from __future__ import annotations

import backtest
import config as cfg_mod
import market_scanner as scanner
import strategy
from strategy import Kline
from synthetic_data import (
    lanjutan_setelah_entry, make_candle, seri_dengan_setup, skenario_pullback_retest,
)

CFG = dict(cfg_mod.PUMP_CONFIG)


# ======================================================================
# anchored_vwap
# ======================================================================

def test_anchored_vwap_cocok_dengan_hitungan_manual():
    kl = [
        make_candle(0, 10, 12, 8, 10, volume=100.0),    # rata-rata (12+8+10)/3 = 10
        make_candle(1, 10, 22, 14, 18, volume=50.0),    # rata-rata (22+14+18)/3 = 18
        make_candle(2, 18, 26, 22, 24, volume=25.0),    # rata-rata (26+22+24)/3 = 24
    ]
    # VWAP dari indeks 1: (18*50*18 ... ) dihitung dari quote_volume yang
    # sudah diisi make_candle sebagai volume x harga rata-rata.
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
    """Candle bervolume besar harus menarik VWAP ke arah harganya."""
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
              "MAX_BARS_BREAKOUT_TO_RETEST": 8, "ATR_PERIOD": 14})
    # 10 + 2*2 + 8 = 22, tetapi ATR butuh 15 candle, jadi yang menang 22.
    assert strategy.required_lookback_bars(c) == 22
    c["SWING_LOOKBACK_BARS"] = 2
    c["MAX_BARS_BREAKOUT_TO_RETEST"] = 1
    # Sekarang struktur hanya butuh 7 candle, ATR yang menentukan.
    assert strategy.required_lookback_bars(c) >= c["ATR_PERIOD"] + 1


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
    assert hasil.zone_low < hasil.breakout_level < hasil.zone_high
    assert hasil.anchored_vwap is not None
    assert hasil.invalidation_price < hasil.breakout_level
    assert hasil.atr_pct and hasil.atr_pct > 0


def test_skenario_gagal_beserta_alasannya():
    kasus = {
        "wick_saja": "tidak ada breakout",
        "close_di_bawah_level": "tidak kembali di atas level",
        "close_lemah": "dari range",
        "kedaluwarsa": "candle sejak breakout",
        "terlalu_jauh": "anti-kejar",
        "di_bawah_vwap": "di bawah anchored VWAP",
        "vwap_jauh": "tanpa konfluensi",
        "invalidasi": "di bawah batas invalidasi",
        "datar": "ATR tidak bisa dihitung",
        "volume_nol": "anchored VWAP tidak bisa dihitung",
        "data_kurang": "minimum",
    }
    for nama, potongan in kasus.items():
        hasil = scanner.detect_pullback_retest(skenario_pullback_retest(nama), CFG)
        assert not hasil.ok, f"{nama} seharusnya ditolak"
        assert potongan in hasil.reason, f"{nama}: {hasil.reason}"


def test_daftar_kosong_dan_none_tidak_membuat_crash():
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
    """Hasil pada jendela yang sama harus identik, berapa pun data sesudahnya."""
    dasar = skenario_pullback_retest("lolos")
    awal = scanner.detect_pullback_retest(dasar, CFG)
    for arah in ("naik", "turun", "datar"):
        panjang = dasar + lanjutan_setelah_entry(dasar, arah, 20)
        ulang = scanner.detect_pullback_retest(panjang[:len(dasar)], CFG)
        assert ulang == awal, f"deteksi berubah setelah menambahkan candle {arah}"


def test_pivot_kanan_yang_belum_tertutup_tidak_dipakai():
    """Candle tertinggi paling ujung belum bisa menjadi pivot yang sah.

    Kalau sayap kanan pivot belum lengkap, memakainya berarti melihat masa
    depan. Data di bawah menempatkan puncak tepat di candle terakhir.
    """
    kl = [make_candle(i, 100, 100.2, 99.8, 100, volume=1000.0) for i in range(30)]
    kl[-1] = make_candle(29, 100, 120, 99.8, 119, volume=1000.0)
    hasil = scanner.detect_pullback_retest(kl, CFG)
    assert not hasil.ok
    assert "pivot" in hasil.reason, hasil.reason


# ======================================================================
# Paritas live dan backtest
# ======================================================================

def test_paritas_deteksi_live_dan_backtest():
    """Jendela yang sama harus memberi keputusan yang sama di kedua jalur."""
    kl = seri_dengan_setup(ekor="naik", panjang_ekor=20)
    window = strategy.confirm_window_bars(CFG)
    entry_bar = len(kl) - 21  # candle retest, sebelum ekor

    langsung = scanner.detect_pullback_retest(
        kl[max(0, entry_bar - window + 1): entry_bar + 1], CFG)
    assert langsung.ok, langsung.reason

    cfg_bt = dict(CFG)
    cfg_bt["MIN_QUOTE_VOLUME_USDT_24H"] = 1_000_000
    hasil = backtest.run_backtest(kl, cfg_bt, warmup_bars=0)
    assert len(hasil.trades) == 1
    trade = hasil.trades[0]
    assert trade.entry_time == kl[entry_bar].close_time
    assert abs(trade.entry_price - kl[entry_bar].close) < 1e-12


def test_backtest_mengunci_level_invalidasi_saat_entry():
    kl = seri_dengan_setup(ekor="invalidasi", panjang_ekor=10)
    cfg_bt = dict(CFG)
    cfg_bt.update({"MIN_QUOTE_VOLUME_USDT_24H": 1_000_000,
                   "SETUP_INVALIDATION_EXIT": True, "USE_STOP_LOSS": False,
                   "USE_TP": False, "USE_BREAKEVEN": False, "USE_TRAILING": False,
                   "MAX_HOLD_MINUTES": 100000})
    hasil = backtest.run_backtest(kl, cfg_bt, warmup_bars=0)
    assert [t.reason for t in hasil.trades] == ["SETUP_INVALIDATED"]


def test_exit_invalidasi_tidak_terpicu_saat_harga_bertahan():
    kl = seri_dengan_setup(ekor="bertahan", panjang_ekor=10)
    cfg_bt = dict(CFG)
    cfg_bt.update({"MIN_QUOTE_VOLUME_USDT_24H": 1_000_000,
                   "SETUP_INVALIDATION_EXIT": True, "USE_STOP_LOSS": False,
                   "USE_TP": False, "USE_BREAKEVEN": False, "USE_TRAILING": False,
                   "MAX_HOLD_MINUTES": 100000})
    hasil = backtest.run_backtest(kl, cfg_bt, warmup_bars=0)
    assert [t.reason for t in hasil.trades] == ["END_OF_DATA"]


def test_flag_setup_invalidation_exit_dihormati():
    kl = seri_dengan_setup(ekor="invalidasi", panjang_ekor=10)
    cfg_bt = dict(CFG)
    cfg_bt.update({"MIN_QUOTE_VOLUME_USDT_24H": 1_000_000,
                   "SETUP_INVALIDATION_EXIT": False, "USE_STOP_LOSS": False,
                   "USE_TP": False, "USE_BREAKEVEN": False, "USE_TRAILING": False,
                   "MAX_HOLD_MINUTES": 100000})
    hasil = backtest.run_backtest(kl, cfg_bt, warmup_bars=0)
    assert "SETUP_INVALIDATED" not in [t.reason for t in hasil.trades]


# ======================================================================
# Exit SETUP_INVALIDATED pada bot live
# ======================================================================

class _KlineClientPalsu:
    """Klien minimal: hanya mengembalikan candle yang sudah disiapkan."""

    def __init__(self, klines: list[Kline]):
        self.klines = klines
        self.panggilan = 0

    def get_klines(self, symbol, interval, limit=3, **_kw):
        self.panggilan += 1
        out = []
        for k in self.klines[-limit:]:
            out.append([k.open_time, str(k.open), str(k.high), str(k.low), str(k.close),
                        str(k.volume), k.close_time, str(k.quote_volume), 10,
                        "0", "0", "0"])
        return out


def _state_posisi(batas: float, entry_time: int) -> dict:
    return {
        "current_symbol": "AAAUSDT", "qty": 1.0, "entry_price": 100.0,
        "entry_time": entry_time, "setup_invalidation_price": batas,
        "breakout_level": batas * 1.01, "last_setup_check_close_time": 0,
    }


def test_exit_invalidasi_live_hanya_memakai_candle_tertutup(monkeypatch):
    import pump_scanner_bot as bot

    jatuh = [make_candle(i, 100, 100.1, 98.0, 98.5, volume=1000.0) for i in range(3)]
    client = _KlineClientPalsu(jatuh)
    state = _state_posisi(99.0, entry_time=0)

    ditutup = {}

    def fake_close(_c, _cfg, _f, st, alasan):
        ditutup["alasan"] = alasan
        st["current_symbol"] = None

    monkeypatch.setattr(bot, "close_position", fake_close)
    # Semua candle di atas sudah tertutup jauh di masa lalu.
    monkeypatch.setattr(bot.state_mod, "now_ms", lambda: jatuh[-1].close_time + 1)

    cfg = dict(CFG)
    cfg["SETUP_INVALIDATION_EXIT"] = True
    assert bot.check_setup_invalidation(client, cfg, {}, state) is True
    assert ditutup["alasan"] == "SETUP_INVALIDATED"


def test_exit_invalidasi_live_mengabaikan_candle_sebelum_entry(monkeypatch):
    import pump_scanner_bot as bot

    jatuh = [make_candle(i, 100, 100.1, 98.0, 98.5, volume=1000.0) for i in range(3)]
    client = _KlineClientPalsu(jatuh)
    # Entry terjadi SETELAH candle terakhir tertutup, jadi tidak ada candle
    # pasca-entry yang boleh menutup posisi.
    state = _state_posisi(99.0, entry_time=jatuh[-1].close_time + 1)

    monkeypatch.setattr(bot, "close_position",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("tidak boleh menutup")))
    monkeypatch.setattr(bot.state_mod, "now_ms", lambda: jatuh[-1].close_time + 2)

    cfg = dict(CFG)
    cfg["SETUP_INVALIDATION_EXIT"] = True
    assert bot.check_setup_invalidation(client, cfg, {}, state) is False


def test_exit_invalidasi_live_dilewati_untuk_posisi_lama(monkeypatch):
    """Posisi dari versi sebelum fitur ini tidak punya level tersimpan."""
    import pump_scanner_bot as bot

    client = _KlineClientPalsu([make_candle(0, 100, 100, 90, 90, volume=10.0)])
    state = _state_posisi(0.0, entry_time=0)

    cfg = dict(CFG)
    cfg["SETUP_INVALIDATION_EXIT"] = True
    assert bot.check_setup_invalidation(client, cfg, {}, state) is False
    assert client.panggilan == 0, "tidak boleh membuang rate limit untuk posisi tanpa level"


def test_exit_invalidasi_live_satu_panggilan_klines_per_candle(monkeypatch):
    import pump_scanner_bot as bot

    tenang = [make_candle(i, 100, 100.5, 99.9, 100.2, volume=1000.0) for i in range(3)]
    client = _KlineClientPalsu(tenang)
    state = _state_posisi(99.0, entry_time=0)

    monkeypatch.setattr(bot.state_mod, "now_ms", lambda: tenang[-1].close_time + 1)
    cfg = dict(CFG)
    cfg["SETUP_INVALIDATION_EXIT"] = True

    bot.check_setup_invalidation(client, cfg, {}, state)
    bot.check_setup_invalidation(client, cfg, {}, state)
    bot.check_setup_invalidation(client, cfg, {}, state)
    assert client.panggilan == 1, "candle yang sama tidak boleh diunduh berulang kali"
