from __future__ import annotations

from copy import deepcopy
from typing import Callable

from backtesting import backtest as bt
from backtesting import synthetic_data as sd
from config.config import CONFIG_LOAD_ERRORS, PUMP_CONFIG
from config.settings_schema import (
    PARAMETER_SCHEMA,
    REMOVED_PARAMETERS,
    relation_violations,
)
from market import market_scanner as scanner
from strategy import entry_rule
from strategy import indicators as strategy
from strategy.indicators import Kline

MS_5M = 300_000


def _candle(
    index: int,
    open_: float,
    close: float,
    low: float,
    high: float | None = None,
    volume: float = 1_000.0,
) -> Kline:
    if high is None:
        high = max(open_, close) * 1.001
    return sd.make_candle(index, open_, high, low, close, volume, bar_ms=MS_5M)


def _datar(index: int, harga: float = 100.0) -> Kline:
    return _candle(index, harga, harga * 1.0001, harga * 0.9995, harga * 1.001)


def _hijau(index: int, prev_close: float, langkah: float = 0.004) -> Kline:
    open_ = prev_close
    close = prev_close * (1.0 + langkah)
    return _candle(index, open_, close, open_ * 0.999)


def _merah(index: int, prev_close: float, langkah: float = 0.004) -> Kline:
    open_ = prev_close
    close = prev_close * (1.0 - langkah)
    return _candle(index, open_, close, min(open_, close) * 0.999)


def seri_netral(panjang: int = 40, harga: float = 100.0, mulai: int = 0) -> list[Kline]:
    return [_datar(mulai + i, harga) for i in range(panjang)]


def seri_dengan_bull(
    bull: int = 3,
    sebelum: int = 40,
    harga: float = 100.0,
    langkah: float = 0.004,
) -> list[Kline]:
    bars = seri_netral(sebelum, harga)
    prev = bars[-1].close
    for i in range(bull):
        bar = _hijau(sebelum + i, prev, langkah)
        bars.append(bar)
        prev = bar.close
    return bars


def config_dasar() -> dict:
    cfg = deepcopy(PUMP_CONFIG)
    cfg.update(
        {
            "MODE": "PAPER",
            "ENTRY_RULE_ENABLED": True,
            "ENTRY_BULL_CANDLES": 3,
            "ENTRY_SL_ANCHOR": "SIGNAL",
            "ENTRY_SL_BUFFER_PCT": 0.30,
            "ENTRY_MIN_RR_RATIO": 2.0,
            "ENTRY_MAX_SL_PCT": 3.0,
            "CONFIRM_INTERVAL": "5m",
            "CONFIRM_LOOKBACK_BARS": 60,
        }
    )
    return cfg


def cek_konfigurasi_bersih() -> str:
    for kunci in (
        "ENTRY_RULE_ENABLED",
        "ENTRY_BULL_CANDLES",
        "ENTRY_SL_ANCHOR",
        "ENTRY_SL_BUFFER_PCT",
        "ENTRY_MIN_RR_RATIO",
        "ENTRY_MAX_SL_PCT",
        "EXIT_TRAILING_PCT",
    ):
        assert kunci in PUMP_CONFIG, f"{kunci} hilang dari PUMP_CONFIG"
        assert kunci in PARAMETER_SCHEMA, f"{kunci} hilang dari PARAMETER_SCHEMA"
        assert kunci in bt.OVERRIDE_CASTERS, f"{kunci} hilang dari OVERRIDE_CASTERS"

    for kunci in (
        "BTC_FILTER_ENABLED",
        "BTC_MAX_DROP_PCT",
        "BTC_LOOKBACK_BARS",
        "CONFLUENCE_FILTER_ENABLED",
        "CONFLUENCE_STRUCTURE_INTERVAL",
        "CONFLUENCE_LOOKBACK_BARS",
        "CONFLUENCE_MIN_IMPULSE_PCT",
        "CONFLUENCE_MIN_SCORE",
        "CONFLUENCE_REQUIRE_ALL_FIVE",
        "CONFLUENCE_REQUIRE_BULLISH_REACTION",
        "CONFLUENCE_MIN_CLOSE_POSITION",
        "CONFLUENCE_SL_BUFFER_PCT",
        "CONFLUENCE_MIN_RR_RATIO",
        "CONFLUENCE_BREAKEVEN_ENABLED",
        "CONFLUENCE_BREAKEVEN_R_MULTIPLE",
        "CONFLUENCE_BREAKEVEN_LOCK_PCT",
        "VP_FILTER_ENABLED",
        "VP_BINS",
        "FIB_FILTER_ENABLED",
        "LIQUIDITY_FILTER_ENABLED",
        "FVG_FILTER_ENABLED",
        "OB_FILTER_ENABLED",
    ):
        assert kunci not in PUMP_CONFIG, f"{kunci} masih ada di PUMP_CONFIG"
        assert kunci not in PARAMETER_SCHEMA, f"{kunci} masih ada di PARAMETER_SCHEMA"
        assert kunci not in bt.OVERRIDE_CASTERS, f"{kunci} masih ada di OVERRIDE_CASTERS"
        assert kunci in REMOVED_PARAMETERS, f"{kunci} belum masuk REMOVED_PARAMETERS"

    assert not CONFIG_LOAD_ERRORS, f"config gagal dimuat: {CONFIG_LOAD_ERRORS}"
    sisa = sorted(set(PUMP_CONFIG) - set(PARAMETER_SCHEMA))
    assert not sisa, f"kunci config tanpa skema: {sisa}"
    assert not relation_violations(PUMP_CONFIG, mode_aware=True), (
        f"kombinasi default tidak konsisten: {relation_violations(PUMP_CONFIG)}"
    )
    return "semua kunci entry tersinkron dan seluruh kunci konfluensi sudah dihapus"


def cek_modul_konfluensi_hilang() -> str:
    import importlib.util

    for nama in ("strategy.confluence_code", "backtesting.confluence_fixture"):
        assert importlib.util.find_spec(nama) is None, f"modul {nama} masih bisa diimpor"
    assert not hasattr(strategy, "evaluate_confluence_code"), "fungsi konfluensi masih ada"
    assert not hasattr(strategy, "compute_confluence_exit_levels"), "exit konfluensi masih ada"
    return "tiga modul konfluensi terhapus dan tidak ada sisa simbol di strategy"


def cek_aturan_tiga_candle() -> str:
    cfg = config_dasar()
    bars = seri_dengan_bull(3)
    hasil = strategy.evaluate_entry(bars, cfg)
    assert hasil["ok"], f"3 candle bullish harus lolos: {hasil['reason']}"
    assert hasil["bull_count"] >= 3, hasil
    assert hasil["stage"] == entry_rule.STAGE_OK
    assert hasil["signal_close"] == float(bars[-1].close)
    return f"lolos pada {hasil['bull_count']} candle, close sinyal {hasil['signal_close']:.4f}"


def cek_dua_candle_ditolak() -> str:
    cfg = config_dasar()
    bars = seri_dengan_bull(2)
    hasil = strategy.evaluate_entry(bars, cfg)
    assert not hasil["ok"], "2 candle bullish tidak boleh lolos"
    assert hasil["stage"] == entry_rule.STAGE_SEQUENCE
    assert "butuh 3" in hasil["reason"], hasil["reason"]
    return hasil["reason"]


def cek_close_di_bawah_close_sebelumnya() -> str:
    cfg = config_dasar()
    bars = seri_dengan_bull(3)
    prev = bars[-2].close
    open_akhir = prev * 0.9995
    close_akhir = prev * 0.9998
    bars[-1] = _candle(len(bars) - 1, open_akhir, close_akhir, open_akhir * 0.9995)
    hasil = strategy.evaluate_entry(bars, cfg)
    assert close_akhir > open_akhir, "candle uji harus tetap hijau"
    assert close_akhir < prev, "close harus di bawah close sebelumnya"
    assert not hasil["ok"], "candle hijau dengan close di bawah close sebelumnya harus ditolak"
    return "candle hijau tanpa higher close tetap ditolak"


def cek_doji_ditolak() -> str:
    cfg = config_dasar()
    bars = seri_dengan_bull(3)
    prev = bars[-2].close
    bars[-1] = _candle(len(bars) - 1, prev * 1.001, prev * 1.001, prev * 0.999)
    hasil = strategy.evaluate_entry(bars, cfg)
    assert not hasil["ok"], "doji (close sama dengan open) harus ditolak"
    return "close sama dengan open dianggap bukan candle bullish"


def cek_sl_dari_low_candle_sinyal() -> str:
    cfg = config_dasar()
    bars = seri_dengan_bull(3)
    hasil = strategy.evaluate_entry(bars, cfg)
    low_sinyal = float(bars[-1].low)
    harapan = low_sinyal * (1.0 - cfg["ENTRY_SL_BUFFER_PCT"] / 100.0)
    assert abs(hasil["sl_price"] - harapan) < 1e-9, (hasil["sl_price"], harapan)
    assert abs(hasil["rr_ratio"] - cfg["ENTRY_MIN_RR_RATIO"]) < 1e-9
    tp_harapan = hasil["signal_close"] + (hasil["signal_close"] - harapan) * cfg[
        "ENTRY_MIN_RR_RATIO"
    ]
    assert abs(hasil["tp_price"] - tp_harapan) < 1e-9, (hasil["tp_price"], tp_harapan)
    return f"SL {hasil['sl_price']:.4f} dan TP {hasil['tp_price']:.4f} sesuai rumus R:R"


def cek_anchor_sequence() -> str:
    cfg = config_dasar()
    cfg["ENTRY_SL_ANCHOR"] = "SEQUENCE"
    bars = seri_dengan_bull(3)
    hasil = strategy.evaluate_entry(bars, cfg)
    low_terendah = min(float(b.low) for b in bars[-3:])
    harapan = low_terendah * (1.0 - cfg["ENTRY_SL_BUFFER_PCT"] / 100.0)
    assert abs(hasil["sl_price"] - harapan) < 1e-9, (hasil["sl_price"], harapan)
    assert hasil["sl_price"] < bars[-1].low, "SL SEQUENCE harus di bawah sinyal"
    return "anchor SEQUENCE memakai low terendah dari seluruh rangkaian"


def cek_batas_jarak_sl() -> str:
    cfg = config_dasar()
    bars = seri_netral(40)
    prev = bars[-1].close
    for i in range(3):
        open_ = prev
        close = prev * 1.01
        bars.append(_candle(40 + i, open_, close, open_ * 0.95))
        prev = close
    ketat = dict(cfg)
    ketat["ENTRY_MAX_SL_PCT"] = 0.5
    hasil = strategy.evaluate_entry(bars, ketat)
    assert not hasil["ok"], "jarak SL lebar harus ditolak"
    assert hasil["stage"] == entry_rule.STAGE_RISK
    longgar = dict(cfg)
    longgar["ENTRY_MAX_SL_PCT"] = 10.0
    assert strategy.evaluate_entry(bars, longgar)["ok"], "batas longgar harus lolos"
    return hasil["reason"]


def cek_riwayat_kurang() -> str:
    cfg = config_dasar()
    bars = seri_dengan_bull(3, sebelum=2)
    hasil = strategy.evaluate_entry(bars, cfg)
    assert not hasil["ok"] and hasil["stage"] == entry_rule.STAGE_HISTORY
    assert "kurang" in hasil["reason"], hasil["reason"]
    return hasil["reason"]


def cek_anti_look_ahead() -> str:
    cfg = config_dasar()
    bars = seri_dengan_bull(3)
    batas = int(bars[-2].close_time)
    hasil = strategy.evaluate_entry(bars, cfg, signal_close_time_ms=batas)
    assert not hasil["ok"], "candle setelah batas waktu tidak boleh dipakai"
    penuh = strategy.evaluate_entry(bars, cfg, signal_close_time_ms=int(bars[-1].close_time))
    assert penuh["ok"], penuh["reason"]
    return "candle di atas batas waktu sinyal diabaikan"


def cek_integrasi_scanner() -> str:
    cfg = config_dasar()
    bars = seri_dengan_bull(3)
    setup = scanner.detect_entry_setup(bars, cfg)
    assert setup.ok, setup.reason
    assert setup.entry_low is not None and setup.entry_low > 0
    assert setup.bull_count >= 3 and setup.bull_required == 3
    assert setup.entry_interval == "5m"
    assert setup.sl_price < setup.signal_close < setup.tp_price
    assert scanner.diagnostic_stage(setup.stage, setup.reason) == "ENTRY_RULE"
    tolak = scanner.detect_entry_setup(seri_dengan_bull(2), cfg)
    assert not tolak.ok and scanner.diagnostic_stage(tolak.stage, tolak.reason) == "ENTRY_RULE"
    return f"SetupResult lengkap: bull_count={setup.bull_count}, low={setup.entry_low:.4f}"


def cek_aturan_nonaktif() -> str:
    cfg = config_dasar()
    cfg["ENTRY_RULE_ENABLED"] = False
    bars = seri_dengan_bull(2)
    setup = scanner.detect_entry_setup(bars, cfg)
    assert setup.ok, setup.reason
    assert setup.stage == entry_rule.STAGE_DISABLED
    assert setup.sl_price and setup.tp_price and setup.sl_price < setup.signal_close
    return "aturan nonaktif tetap menghasilkan SL/TP cadangan"


def cek_breakeven() -> str:
    params = strategy.entry_breakeven_params(config_dasar())
    assert params["enabled"] is True and params["r_multiple"] == 1.0
    assert params["lock_pct"] == 0.0
    mati = config_dasar()
    mati["ENTRY_RULE_ENABLED"] = False
    assert strategy.entry_breakeven_params(mati)["enabled"] is False
    assert abs(strategy.entry_breakeven_trigger(100.0, 98.0, 1.0) - 102.0) < 1e-9
    assert abs(strategy.entry_breakeven_trigger(100.0, 98.0, 1.5) - 103.0) < 1e-9
    assert strategy.entry_breakeven_trigger(100.0, 100.0, 1.0) == 0.0
    assert abs(strategy.entry_breakeven_price(100.0, 98.0, 1.0, 0.0) - 100.0) < 1e-9
    assert abs(strategy.entry_breakeven_price(100.0, 98.0, 1.0, 0.3) - 100.3) < 1e-9
    return "pemicu dan harga breakeven sesuai kelipatan risiko"


def cek_backtest_menghasilkan_trade() -> str:
    cfg = config_dasar()
    cfg.update(
        {
            "_symbol": "",
            "MIN_QUOTE_VOLUME_USDT_24H": 0.0,
            "USE_RISK_PERCENT": False,
            "POSITION_SIZE_USDT": 100.0,
            "MAX_POSITION_USDT": 0.0,
            "BACKTEST_INITIAL_EQUITY_USDT": 1000.0,
            "COOLDOWN_MINUTES_AFTER_CLOSE": 0,
            "MIN_SECONDS_BETWEEN_TRADES": 0,
            "MAX_CHASE_PCT": 0.0,
            "TAKER_FEE_PCT": 0.1,
            "USE_BNB_FEE_DISCOUNT": False,
        }
    )
    klines = sd.seri_banyak_setup(harga=100.0, siklus=10, volume=9_000_000.0)
    hasil = bt.run_backtest(klines, cfg, warmup_bars=120)
    assert hasil.trades, "backtest aturan entry tidak menghasilkan trade"
    alasan = {t.reason for t in hasil.trades}
    assert alasan & {
        "STOP_LOSS",
        "TAKE_PROFIT",
        "BREAKEVEN",
        "TRAILING_STOP",
        "TIME_STOP",
    }, alasan
    for t in hasil.trades:
        assert 0.0 < t.sl_pct < 100.0, t.sl_pct
        assert t.tp_pct > 0.0, t.tp_pct
    return f"{len(hasil.trades)} trade, exit {sorted(alasan)}"


def cek_folder_terhapus() -> str:
    from pathlib import Path

    akar = Path(__file__).resolve().parent.parent
    for relatif in (
        "strategy/confluence_code.py",
        "backtesting/confluence_fixture.py",
        "tools/confluence_probe.py",
    ):
        assert not (akar / relatif).exists(), f"{relatif} masih ada"
    return "berkas konfluensi sudah tidak ada di repo"


CEK: tuple[tuple[str, Callable[[], str]], ...] = (
    ("konfigurasi_bersih", cek_konfigurasi_bersih),
    ("modul_konfluensi_hilang", cek_modul_konfluensi_hilang),
    ("folder_terhapus", cek_folder_terhapus),
    ("aturan_tiga_candle", cek_aturan_tiga_candle),
    ("dua_candle_ditolak", cek_dua_candle_ditolak),
    ("close_di_bawah_close_sebelumnya", cek_close_di_bawah_close_sebelumnya),
    ("doji_ditolak", cek_doji_ditolak),
    ("sl_dari_low_candle_sinyal", cek_sl_dari_low_candle_sinyal),
    ("anchor_sequence", cek_anchor_sequence),
    ("batas_jarak_sl", cek_batas_jarak_sl),
    ("riwayat_kurang", cek_riwayat_kurang),
    ("anti_look_ahead", cek_anti_look_ahead),
    ("integrasi_scanner", cek_integrasi_scanner),
    ("aturan_nonaktif", cek_aturan_nonaktif),
    ("breakeven", cek_breakeven),
    ("backtest_menghasilkan_trade", cek_backtest_menghasilkan_trade),
)


def run(verbose: bool = True) -> tuple[bool, list[str]]:
    baris: list[str] = []
    ok_semua = True
    for nama, fungsi in CEK:
        try:
            catatan = fungsi()
            baris.append(f"[LULUS] {nama}: {catatan}")
        except AssertionError as exc:
            ok_semua = False
            baris.append(f"[GAGAL] {nama}: {exc}")
        except Exception as exc:
            ok_semua = False
            baris.append(f"[ERROR] {nama}: {type(exc).__name__}: {exc}")
    if verbose:
        print("Selftest aturan entry:")
        for item in baris:
            print(f"  {item}")
        print("HASIL: " + ("SEMUA LULUS" if ok_semua else "ADA YANG GAGAL"))
    return ok_semua, baris


if __name__ == "__main__":
    import sys

    lulus, _ = run(verbose=True)
    sys.exit(0 if lulus else 1)
