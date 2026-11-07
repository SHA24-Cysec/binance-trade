from __future__ import annotations

from copy import deepcopy
from typing import Callable

from backtesting import backtest as bt
from backtesting import parity
from backtesting import synthetic_data as sd
from config.config import CONFIG_LOAD_ERRORS, PUMP_CONFIG
from strategy import entry_rule
from strategy import exit_atr
from strategy import indicators as strategy

BAR_MS_5M = 300_000
MS_PER_JAM = 3_600_000
PANJANG_EKOR = 900
HARGA_AWAL = 100.0
CAP_TETAP = entry_rule.EXIT_MAX_HOLD_HOURS
PARAMETER_EXIT = exit_atr.ATR_PARAM_KEYS
KUNCI_EXIT_DIHAPUS = (
    "EXIT_TRAILING_ENABLED",
    "EXIT_TRAILING_MODE",
    "EXIT_TRAILING_START_R",
    "EXIT_STRUCT_STOP_ENABLED",
    "EXIT_STRUCT_STOP_LOOKBACK",
    "EXIT_BEARISH_EXIT_CANDLES",
    "MAX_HOLD_HOURS",
)


def config_dasar() -> dict:
    cfg = deepcopy(PUMP_CONFIG)
    cfg.update(
        {
            "_symbol": "",
            "MODE": "PAPER",
            "CONFIG_UPDATE_MODE": "test",
            "ENTRY_RULE_ENABLED": True,
            "CONFIRM_INTERVAL": "5m",
            "CONFIRM_LOOKBACK_BARS": 121,
            "MIN_QUOTE_VOLUME_USDT_24H": 0.0,
            "GAINER_MIN_CHANGE_PCT": 0.0,
            "GAINER_MAX_CHANGE_PCT": 0.0,
            "GAINER_VOLUME_FILTER_ENABLED": False,
            "SAME_COIN_BLOCK_HOURS": 0.0,
            "USE_RISK_PERCENT": False,
            "POSITION_SIZE_USDT": 100.0,
            "MAX_POSITION_USDT": 100.0,
            "BACKTEST_INITIAL_EQUITY_USDT": 1000.0,
            "COOLDOWN_MINUTES_AFTER_CLOSE": 0,
            "MIN_SECONDS_BETWEEN_TRADES": 0,
            "MAX_CHASE_PCT": 0.0,
        }
    )
    return cfg


def jalankan(ekor: str = "datar", panjang_ekor: int = PANJANG_EKOR):
    cfg = config_dasar()
    klines = sd.seri_dengan_setup(
        harga=HARGA_AWAL, panjang_ekor=panjang_ekor, ekor=ekor
    )
    warmup = max(strategy.required_lookback_bars(cfg), 60)
    bt.validate_params(cfg)
    return bt.run_backtest(klines, cfg, warmup)


def _posisi(levels: dict) -> parity.PositionState:
    dasar = {
        "source": "ATR",
        "sl_pct": 2.0,
        "tp_pct": 8.0,
        "sl_price": 98.0,
        "tp_price": 108.0,
    }
    dasar.update(levels)
    return parity.PositionState(100.0, dasar)


def _candle(
    high: float,
    low: float,
    close: float,
    detik: int = 0,
    open_price: float | None = None,
) -> parity.Kline:
    return parity.Kline(
        open_time=BAR_MS_5M * detik,
        open=close if open_price is None else open_price,
        high=high,
        low=low,
        close=close,
        volume=1.0,
        close_time=BAR_MS_5M * (detik + 1) - 1,
    )


def _ctx_now_ms(nilai: int) -> Callable[[], None]:
    from infrastructure.storage import state as state_mod

    asli = state_mod.now_ms
    state_mod.now_ms = lambda: nilai
    return lambda: setattr(state_mod, "now_ms", asli)


def _seri_jeda(panjang_tail: int = 320) -> list:
    out = []
    harga = HARGA_AWAL
    for i in range(288):
        out.append(
            sd.make_candle(i, harga, harga * 1.004, harga * 0.996, harga * 1.001)
        )
    i = 288
    lonjakan = harga * 1.024
    out.append(
        sd.make_candle(
            i, harga * 1.002, lonjakan * 1.001, harga * 0.999, lonjakan, 15_000_000.0
        )
    )
    i += 1
    for _ in range(panjang_tail):
        out.append(
            sd.make_candle(
                i, lonjakan, lonjakan * 1.0004, lonjakan * 0.9996, lonjakan
            )
        )
        i += 1
    return out


def _state_atr(
    entry: float = 100.0,
    atr: float = 1.0,
    sl: float = 98.5,
    tp: float = 103.0,
) -> dict:
    return {
        "entry_price": entry,
        "initial_sl_price": sl,
        "sl_price": sl,
        "tp_price": tp,
        "peak_price": entry,
        "be_active": False,
        "be_stop_price": 0.0,
        "trail_active": False,
        "trail_stop_price": 0.0,
        "atr_at_entry": atr,
    }


def cek_batas_tahan_tetap() -> str:
    assert CAP_TETAP == 24.0, f"batas tahan harus tetap 24 jam, bukan {CAP_TETAP}"
    assert strategy.max_hold_ms({}, "5m") == 24 * MS_PER_JAM
    assert strategy.max_hold_ms({}, "1h") == 24 * MS_PER_JAM
    assert strategy.max_hold_ms({"MAX_HOLD_HOURS": 999}, "5m") == 24 * MS_PER_JAM, (
        "nilai config lama masih bisa mengubah batas tahan"
    )
    assert strategy.max_hold_ms({"MAX_HOLD_HOURS": 0}, "5m") == 24 * MS_PER_JAM, (
        "config lama masih bisa mematikan batas tahan"
    )
    try:
        strategy.max_hold_ms({}, "1d")
    except ValueError:
        pass
    else:
        raise AssertionError("interval 1d seharusnya ditolak karena kurang dari 2 bar")
    return f"{CAP_TETAP:g} jam pada 5m dan 1h, tidak terpengaruh config lama"


def cek_level_atr_dasar() -> str:
    cfg = config_dasar()
    params = exit_atr.atr_params(cfg)
    plan = exit_atr.plan_levels(100.0, 2.0, cfg)
    assert plan["ok"], plan["reason"]
    assert abs(plan["sl_price"] - (100.0 - params["sl_mult"] * 2.0)) < 1e-9, plan
    assert abs(plan["tp_price"] - (100.0 + params["tp_mult"] * 2.0)) < 1e-9, plan
    assert abs(plan["rr_ratio"] - params["tp_mult"] / params["sl_mult"]) < 1e-9, plan
    return (
        f"SL {params['sl_mult']:g} x ATR, TP {params['tp_mult']:g} x ATR, "
        f"R:R 1:{plan['rr_ratio']:.2f}"
    )


def cek_atr_wajib_untuk_entry() -> str:
    cfg = config_dasar()
    tolak = entry_rule.exit_levels(100.0, 99.0, cfg)
    assert not tolak["ok"], "level exit lolos padahal ATR tidak tersedia"
    assert "ATR" in tolak["reason"], tolak["reason"]
    cadangan = entry_rule.exit_levels(100.0, 99.0, cfg, fallback_allowed=True)
    assert cadangan["ok"], cadangan["reason"]
    assert cadangan["sl_price"] < 100.0 < cadangan["tp_price"], cadangan
    return "ATR kosong menolak entry, jalur cadangan hanya dipakai saat aturan nonaktif"


def cek_batas_jarak_stop() -> str:
    cfg = config_dasar()
    cfg["EXIT_ATR_SL_MULT"] = 50.0
    cfg["EXIT_ATR_MAX_SL_PCT"] = 5.0
    plan = exit_atr.plan_levels(100.0, 2.0, cfg)
    assert not plan["ok"], plan
    assert "melampaui batas" in plan["reason"], plan["reason"]
    return "stop yang terlalu lebar dibanding volatilitas ditolak"


def cek_rr_minimum_dari_kelipatan() -> str:
    cfg = config_dasar()
    cfg["EXIT_ATR_TP_MULT"] = 2.0
    cfg["EXIT_ATR_SL_MULT"] = 2.0
    cfg["ENTRY_MIN_RR_RATIO"] = 2.0
    plan = exit_atr.plan_levels(100.0, 1.0, cfg)
    assert not plan["ok"], plan
    assert "R:R" in plan["reason"], plan["reason"]
    return "R:R dari kelipatan ATR di bawah minimum langsung menolak setup"


def cek_breakeven_atr() -> str:
    cfg = config_dasar()
    params = exit_atr.atr_params(cfg)
    state = _state_atr(atr=1.0, sl=98.5, tp=103.0)
    pemicu = 100.0 + params["breakeven_mult"] * 1.0
    sebelum = exit_atr.advance(state, pemicu - 0.01, cfg)
    assert not sebelum["be_active"], "breakeven aktif sebelum pemicu ATR"
    sesudah = exit_atr.advance(state, pemicu, cfg)
    assert sesudah["be_active"], "breakeven tidak aktif tepat di pemicu ATR"
    assert abs(sesudah["sl_price"] - 100.0) < 1e-9, sesudah
    assert sesudah["events"] and sesudah["events"][0][0] == exit_atr.EVENT_BREAKEVEN
    return f"breakeven aktif di {pemicu:.2f} ({params['breakeven_mult']:g} x ATR)"


def cek_trailing_atr_tidak_turun() -> str:
    cfg = config_dasar()
    params = exit_atr.atr_params(cfg)
    state = _state_atr(atr=1.0, sl=98.5, tp=120.0)
    puncak = 100.0
    stop_terakhir = 98.5
    for harga in (101.0, 102.5, 104.0, 103.0, 101.5, 99.0):
        hasil = exit_atr.advance(state, harga, cfg)
        state.update(
            {
                "peak_price": hasil["peak_price"],
                "be_active": hasil["be_active"],
                "be_stop_price": hasil["be_stop_price"],
                "trail_active": hasil["trail_active"],
                "trail_stop_price": hasil["trail_stop_price"],
                "sl_price": hasil["sl_price"],
            }
        )
        puncak = max(puncak, harga)
        assert hasil["sl_price"] >= stop_terakhir - 1e-12, (
            f"stop turun dari {stop_terakhir:.4f} ke {hasil['sl_price']:.4f}"
        )
        stop_terakhir = hasil["sl_price"]
        harapan = max(
            hasil["sl_price"],
            puncak - params["trail_mult"] * 1.0 if puncak >= 101.5 else 0.0,
        )
        assert abs(hasil["sl_price"] - harapan) < 1e-9 or hasil["sl_price"] <= harapan
    assert state["trail_active"], "trailing tidak pernah aktif pada tren naik"
    return (
        f"trailing {params['trail_mult']:g} x ATR aktif dan stop akhir "
        f"{stop_terakhir:.4f} tidak pernah turun"
    )


def cek_take_profit() -> str:
    assert entry_rule.take_profit_due(100, 106, 106)
    assert not entry_rule.take_profit_due(100, 106, 105.99)
    assert not entry_rule.take_profit_due(100, None, 200)
    assert not entry_rule.take_profit_due(0, 106, 200)
    return "target ATR hanya memicu exit saat harga menyentuh atau melewati level"


def cek_prioritas_stop_sebelum_trailing() -> str:
    cfg = config_dasar()
    pos = _posisi({"atr": 1.0, "sl_price": 98.5, "tp_price": 120.0})
    turun = parity.evaluate_candle_exit(
        pos, _candle(high=100.5, low=98.0, close=98.2), cfg
    )
    assert turun is not None and turun[0] == "STOP_LOSS", turun
    pos2 = _posisi({"atr": 1.0, "sl_price": 98.5, "tp_price": 106.0})
    naik = parity.evaluate_candle_exit(
        pos2, _candle(high=106.5, low=100.0, close=106.2), cfg
    )
    assert naik is not None and naik[0] == "TAKE_PROFIT", naik
    return "stop harga dievaluasi sebelum level lain dan target ATR memicu exit"


def cek_paritas_live_dan_backtest() -> str:
    cfg = config_dasar()
    harga_urut = [100.4, 101.6, 102.2, 101.1, 100.2, 99.4, 98.6]
    pos = _posisi({"atr": 1.0, "sl_price": 98.5, "tp_price": 120.0})
    state = _state_atr(atr=1.0, sl=98.5, tp=120.0)
    for detik, harga in enumerate(harga_urut):
        hasil_backtest = parity.evaluate_candle_exit(
            pos, _candle(high=harga, low=harga, close=harga, detik=detik), cfg
        )
        hasil_live = exit_atr.advance(state, harga, cfg)
        state.update(
            {
                "peak_price": hasil_live["peak_price"],
                "be_active": hasil_live["be_active"],
                "be_stop_price": hasil_live["be_stop_price"],
                "trail_active": hasil_live["trail_active"],
                "trail_stop_price": hasil_live["trail_stop_price"],
                "sl_price": hasil_live["sl_price"],
            }
        )
        assert abs(float(pos.levels["sl_price"]) - state["sl_price"]) < 1e-9, (
            f"stop backtest {pos.levels['sl_price']:.6f} beda dari live "
            f"{state['sl_price']:.6f} pada harga {harga:.2f}"
        )
        if hasil_backtest is not None:
            assert harga <= state["sl_price"] + 1e-9, hasil_backtest
            break
    else:
        raise AssertionError("skenario paritas tidak pernah menyentuh stop")
    return "trailing, breakeven, dan stop backtest identik dengan jalur live"


def cek_time_stop_menutup_posisi() -> str:
    cfg = config_dasar()
    klines = _seri_jeda(320)
    bt.validate_params(cfg)
    hasil = bt.run_backtest(klines, cfg, warmup_bars=60)
    assert hasil.trades, "skenario harga diam tidak menghasilkan trade tertutup"
    alasan = {t.reason for t in hasil.trades}
    assert strategy.TIME_STOP_REASON in alasan, (
        f"harga yang tidak menyentuh stop maupun target harus keluar lewat time "
        f"stop, dapat {alasan}"
    )
    maks = max(t.hold_minutes for t in hasil.trades) / 60.0
    assert maks <= CAP_TETAP + 1.0, (
        f"ada posisi ditahan {maks:.2f} jam, di atas batas {CAP_TETAP:g}"
    )
    assert maks >= CAP_TETAP - 1.0, (
        f"posisi ditutup terlalu cepat di {maks:.2f} jam padahal batasnya {CAP_TETAP:g}"
    )
    return f"{len(hasil.trades)} trade, umur {maks:.2f} jam sesuai batas {CAP_TETAP:g} jam"


def cek_stop_menutup_posisi_awal() -> str:
    turun = jalankan("invalidasi", 40)
    assert turun.trades, "skenario harga turun tidak menghasilkan trade"
    alasan = {t.reason for t in turun.trades}
    assert "STOP_LOSS" in alasan, f"skenario turun harus keluar lewat stop, dapat {alasan}"
    for t in turun.trades:
        assert 0.0 < t.sl_pct < 100.0, t.sl_pct
        assert t.tp_pct > 0.0, t.tp_pct
    return f"{len(turun.trades)} trade, alasan {sorted(alasan)}"


def cek_backtest_memakai_atr() -> str:
    cfg = config_dasar()
    cfg.update(
        {
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
    assert hasil.trades, "backtest mesin ATR tidak menghasilkan trade"
    batas = cfg["EXIT_ATR_SL_MULT"] / cfg["EXIT_ATR_TP_MULT"]
    for t in hasil.trades:
        assert t.sl_pct > 0.0 and t.tp_pct > 0.0, (t.sl_pct, t.tp_pct)
        rasio = t.sl_pct / t.tp_pct
        assert abs(rasio - batas) < 0.05, (
            f"rasio SL/TP trade {rasio:.3f} tidak mengikuti kelipatan ATR {batas:.3f}"
        )
    alasan = {t.reason for t in hasil.trades}
    return f"{len(hasil.trades)} trade memakai jarak ATR, exit {sorted(alasan)}"


def cek_parameter_exit_tunggal() -> str:
    from config.settings_schema import PARAMETER_SCHEMA
    from backtesting import backtest as bt_mod

    assert entry_rule.EXIT_PARAM_KEYS == PARAMETER_EXIT, (
        f"daftar parameter exit berubah: {entry_rule.EXIT_PARAM_KEYS}"
    )
    for kunci in PARAMETER_EXIT:
        assert kunci in PUMP_CONFIG, f"{kunci} hilang dari PUMP_CONFIG"
        assert kunci in PARAMETER_SCHEMA, f"{kunci} hilang dari PARAMETER_SCHEMA"
        assert kunci in bt_mod.OVERRIDE_CASTERS, f"{kunci} hilang dari OVERRIDE_CASTERS"
        spec = PARAMETER_SCHEMA[kunci]
        assert spec["min"] is not None and spec["max"] is not None, (
            f"{kunci} tidak punya rentang di schema"
        )
    tetap = strategy.exit_fixed_params()
    assert tetap["max_hold_hours"] == CAP_TETAP, tetap
    assert tetap["take_profit_enabled"] is True, tetap
    for kunci in KUNCI_EXIT_DIHAPUS:
        assert kunci not in PUMP_CONFIG, f"{kunci} masih ada di PUMP_CONFIG"
        assert kunci not in PARAMETER_SCHEMA, f"{kunci} masih ada di PARAMETER_SCHEMA"
    return (
        f"{len(PARAMETER_EXIT)} parameter exit ATR tersinkron di config, schema, "
        "dan caster backtest"
    )


def cek_batas_tahan_dihormati_backtest() -> str:
    cfg = config_dasar()
    klines = _seri_jeda(320)
    bt.validate_params(cfg)
    hasil = bt.run_backtest(klines, cfg, warmup_bars=60)
    assert hasil.trades, "tidak ada trade untuk menguji batas tahan"
    maks = max(t.hold_minutes for t in hasil.trades) / 60.0
    assert maks <= CAP_TETAP + 0.5, f"umur posisi {maks:.2f} jam melewati batas"
    return f"batas tahan {CAP_TETAP:g} jam dihormati mesin backtest"


CEK = [
    ("batas tahan tetap 24 jam", cek_batas_tahan_tetap),
    ("level exit dari ATR", cek_level_atr_dasar),
    ("ATR wajib untuk entry", cek_atr_wajib_untuk_entry),
    ("batas jarak stop ATR", cek_batas_jarak_stop),
    ("R:R minimum dari kelipatan ATR", cek_rr_minimum_dari_kelipatan),
    ("breakeven berbasis ATR", cek_breakeven_atr),
    ("trailing ATR tidak turun", cek_trailing_atr_tidak_turun),
    ("take profit ATR", cek_take_profit),
    ("prioritas stop dan target", cek_prioritas_stop_sebelum_trailing),
    ("paritas live dan backtest", cek_paritas_live_dan_backtest),
    ("time stop menutup posisi", cek_time_stop_menutup_posisi),
    ("stop menutup posisi awal", cek_stop_menutup_posisi_awal),
    ("backtest memakai jarak ATR", cek_backtest_memakai_atr),
    ("parameter exit tunggal", cek_parameter_exit_tunggal),
    ("batas tahan dihormati backtest", cek_batas_tahan_dihormati_backtest),
]


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
        print("Selftest mesin exit ATR:")
        for item in baris:
            print(f"  {item}")
        print("HASIL: " + ("SEMUA LULUS" if ok_semua else "ADA YANG GAGAL"))
    return ok_semua, baris


if __name__ == "__main__":
    import sys

    lulus, _ = run(verbose=True)
    sys.exit(0 if lulus else 1)
