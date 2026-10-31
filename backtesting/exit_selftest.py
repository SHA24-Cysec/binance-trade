from __future__ import annotations

from copy import deepcopy
from typing import Callable

from backtesting import backtest as bt
from backtesting import parity
from backtesting import synthetic_data as sd
from config.config import CONFIG_LOAD_ERRORS, PUMP_CONFIG
from strategy import entry_rule
from strategy import indicators as strategy

BAR_MS_5M = 300_000
MS_PER_JAM = 3_600_000
PANJANG_EKOR = 900
HARGA_AWAL = 100.0
CAP_TETAP = entry_rule.EXIT_MAX_HOLD_HOURS
PARAMETER_EXIT = ("EXIT_TRAILING_PCT",)
KUNCI_EXIT_DIHAPUS = (
    "ENTRY_BREAKEVEN_ENABLED",
    "ENTRY_BREAKEVEN_R_MULTIPLE",
    "ENTRY_BREAKEVEN_LOCK_PCT",
    "EXIT_TRAILING_ENABLED",
    "EXIT_TRAILING_MODE",
    "EXIT_TRAILING_START_R",
    "EXIT_TRAILING_ATR_MULT",
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
            "BTC_FILTER_ENABLED": False,
            "MIN_QUOTE_VOLUME_USDT_24H": 0.0,
            "ENTRY_SL_BUFFER_PCT": 0.30,
            "ENTRY_MIN_RR_RATIO": 2.0,
            "ENTRY_MAX_SL_PCT": 5.0,
            "USE_RISK_PERCENT": False,
            "POSITION_SIZE_USDT": 100.0,
            "MAX_POSITION_USDT": 100.0,
            "BACKTEST_INITIAL_EQUITY_USDT": 1000.0,
        }
    )
    return cfg


def jalankan(ekor: str = "datar", panjang_ekor: int = PANJANG_EKOR):
    cfg = config_dasar()
    klines = sd.seri_dengan_setup(harga=HARGA_AWAL, panjang_ekor=panjang_ekor, ekor=ekor)
    warmup = max(strategy.required_lookback_bars(cfg), 60)
    bt.validate_params(cfg)
    return bt.run_backtest(klines, cfg, warmup)


def _posisi(levels: dict) -> parity.PositionState:
    dasar = {
        "source": "ENTRY_RULE",
        "sl_pct": 2.0,
        "tp_pct": 8.0,
        "sl_price": 98.0,
        "tp_price": 108.0,
    }
    dasar.update(levels)
    return parity.PositionState(100.0, dasar)


def _candle(high: float, low: float, close: float, detik: int = 0,
            open_price: float | None = None) -> parity.Kline:
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


def cek_batas_tahan_tetap() -> str:
    assert CAP_TETAP == 24.0, f"batas tahan harus tetap 24 jam, bukan {CAP_TETAP}"
    assert strategy.max_hold_ms({}, "5m") == 24 * MS_PER_JAM
    assert strategy.max_hold_ms({}, "15m") == 24 * MS_PER_JAM
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
    return f"{CAP_TETAP:g} jam pada 5m dan 15m, tidak terpengaruh config lama"


def cek_time_stop_menutup_posisi() -> str:
    hasil = jalankan("datar", 400)
    assert hasil.trades, "skenario time stop tidak menghasilkan trade tertutup"
    alasan = {t.reason for t in hasil.trades}
    assert strategy.TIME_STOP_REASON in alasan, f"TIME_STOP tidak muncul: {alasan}"
    maks = max(t.hold_minutes for t in hasil.trades) / 60.0
    assert maks <= CAP_TETAP + 0.5, f"ada posisi ditahan {maks:.2f} jam, di atas batas {CAP_TETAP:g}"
    return f"{len(hasil.trades)} trade, umur maksimum {maks:.2f} jam"


def cek_prioritas_exit_harga() -> str:
    turun = jalankan("invalidasi", 400)
    assert turun.trades, "skenario harga turun tidak menghasilkan trade"
    alasan = {t.reason for t in turun.trades}
    assert alasan == {"STOP_LOSS"}, (
        f"skenario turun harus keluar lewat stop awal saja, dapat {sorted(alasan)}"
    )
    assert strategy.TIME_STOP_REASON not in alasan, "time stop muncul padahal stop lebih dulu"

    naik = jalankan("naik", 400)
    assert naik.trades, "skenario harga naik tidak menghasilkan trade"
    alasan_naik = {t.reason for t in naik.trades}
    assert "STOP_LOSS" not in alasan_naik, (
        f"stop awal terpicu pada tren naik tanpa take profit: {sorted(alasan_naik)}"
    )
    maks = max(t.hold_minutes for t in naik.trades) / 60.0
    assert maks <= CAP_TETAP + 0.5, f"umur {maks:.2f} jam melampaui batas {CAP_TETAP:g} jam"
    return (
        f"turun: {len(turun.trades)} trade alasan {sorted(alasan)}; "
        f"naik: {len(naik.trades)} trade alasan {sorted(alasan_naik)}"
    )


def cek_kapital_berputar() -> str:
    hasil = jalankan("datar", PANJANG_EKOR)
    assert hasil.trades, "batas tahan tetap 24 jam tidak menutup posisi apa pun"
    maks = max(t.hold_minutes for t in hasil.trades) / 60.0
    assert maks <= CAP_TETAP + 0.5, f"umur {maks:.2f} jam melampaui batas {CAP_TETAP:g} jam"
    return f"{len(hasil.trades)} trade tertutup, umur maksimum {maks:.2f} jam"


def cek_paritas_aturan_live() -> str:
    import trading.pump_scanner_bot as bot

    cfg = config_dasar()
    cap_ms = strategy.max_hold_ms(cfg, "5m")
    assert bot._max_hold_cap_ms(cfg) == cap_ms, (
        "batas yang dihitung bot live berbeda dari batas mesin backtest"
    )

    entry = 1_700_000_000_000
    beda = 0
    for umur in (
        0,
        BAR_MS_5M,
        cap_ms - 2 * BAR_MS_5M,
        cap_ms - BAR_MS_5M,
        cap_ms - BAR_MS_5M + 1,
        cap_ms - 1,
        cap_ms,
        cap_ms + BAR_MS_5M,
    ):
        pulihkan = _ctx_now_ms(entry + umur)
        try:
            harapan = strategy.time_stop_due(entry, entry + umur, cap_ms, BAR_MS_5M)
            hasil = bot._time_stop_due(cfg, {"entry_time": entry})
        finally:
            pulihkan()
        if bool(hasil) != bool(harapan):
            beda += 1
    assert beda == 0, f"{beda} dari 8 umur posisi memberi keputusan live yang berbeda"

    pulihkan = _ctx_now_ms(entry + cap_ms * 2)
    try:
        assert bot._time_stop_due(cfg, {"entry_time": 0}) is False, (
            "posisi lama tanpa entry_time tidak boleh ditutup paksa"
        )
        assert bot._time_stop_due(cfg, {}) is False, (
            "state tanpa entry_time tidak boleh ditutup paksa"
        )
    finally:
        pulihkan()
    return "8 umur posisi sama dengan mesin backtest, state lama aman"


def cek_integrasi_parameter() -> str:
    from config.settings_schema import PARAMETER_SCHEMA, validate_candidate

    assert not CONFIG_LOAD_ERRORS, f"config runtime punya error: {CONFIG_LOAD_ERRORS}"
    assert "EXIT_TRAILING_PCT" in PUMP_CONFIG, "default config tidak punya jarak trailing"
    assert "EXIT_TRAILING_PCT" in PARAMETER_SCHEMA, "schema setting tidak punya kuncinya"
    assert "EXIT_TRAILING_PCT" in bt.OVERRIDE_CASTERS, "override backtest belum mengenalnya"

    spec = PARAMETER_SCHEMA["EXIT_TRAILING_PCT"]
    assert spec["type"] == "float", f"tipe schema {spec['type']} bukan float"
    assert spec["min"] == entry_rule.EXIT_TRAILING_MIN_PCT, "batas bawah schema tidak cocok"
    assert spec["max"] == entry_rule.EXIT_TRAILING_MAX_PCT, "batas atas schema tidak cocok"

    _, errors, _ = validate_candidate(deepcopy(PUMP_CONFIG), "PAPER")
    assert not errors, f"config default sendiri tidak lolos validasi: {errors}"
    kandidat = deepcopy(PUMP_CONFIG)
    kandidat["EXIT_TRAILING_PCT"] = 3.5
    bersih, errors, _ = validate_candidate(kandidat, "PAPER")
    assert not errors and bersih["EXIT_TRAILING_PCT"] == 3.5, (
        f"override jarak trailing ditolak validasi: {errors}"
    )
    kurang = deepcopy(PUMP_CONFIG)
    kurang.pop("EXIT_TRAILING_PCT", None)
    _, errors_kurang, _ = validate_candidate(kurang, "PAPER")
    assert errors_kurang.get("EXIT_TRAILING_PCT") == "nilai tidak tersedia", (
        "schema seharusnya menandai kunci yang hilang sebagai tidak tersedia"
    )

    for buruk in ("bukan angka", 0.0, -2.0):
        try:
            cfg = bt.apply_overrides(config_dasar(), {"EXIT_TRAILING_PCT": buruk})
            bt.validate_params(cfg)
        except bt.BacktestError:
            continue
        raise AssertionError(f"validate_params menerima jarak trailing {buruk!r}")

    catatan = ""
    try:
        from web import dashboard as dash

        assert "EXIT_TRAILING_PCT" in dash.BT_PARAM_KEYS, (
            "dashboard belum mengenal jarak trailing"
        )
    except ImportError as exc:
        catatan = f" (cek dashboard dilewati: {exc})"
    return "config, schema, override backtest, validator, dan dashboard terhubung" + catatan


def cek_exit_aturan_entry() -> str:
    from config.settings_schema import PARAMETER_SCHEMA, REMOVED_PARAMETERS
    from market.market_scanner import detect_entry_setup

    kunci_exit_lama = (
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
    for k in kunci_exit_lama:
        assert k not in PUMP_CONFIG, f"{k} masih ada di PUMP_CONFIG"
        assert k not in PARAMETER_SCHEMA, f"{k} masih ada di PARAMETER_SCHEMA"
        assert k not in bt.OVERRIDE_CASTERS, f"{k} masih ada di OVERRIDE_CASTERS"
        assert k in REMOVED_PARAMETERS, f"{k} belum masuk REMOVED_PARAMETERS"

    for k_lama in (
        "CONFLUENCE_FILTER_ENABLED",
        "CONFLUENCE_STRUCTURE_INTERVAL",
        "CONFLUENCE_LOOKBACK_BARS",
        "CONFLUENCE_SL_BUFFER_PCT",
        "CONFLUENCE_MIN_RR_RATIO",
        "CONFLUENCE_BREAKEVEN_ENABLED",
        "VP_FILTER_ENABLED",
        "FIB_FILTER_ENABLED",
        "LIQUIDITY_FILTER_ENABLED",
        "FVG_FILTER_ENABLED",
        "OB_FILTER_ENABLED",
    ):
        assert k_lama not in PUMP_CONFIG, f"{k_lama} masih ada di PUMP_CONFIG"
        assert k_lama not in PARAMETER_SCHEMA, f"{k_lama} masih ada di PARAMETER_SCHEMA"
        assert k_lama not in bt.OVERRIDE_CASTERS, f"{k_lama} masih ada di OVERRIDE_CASTERS"
        assert k_lama in REMOVED_PARAMETERS, f"{k_lama} belum masuk REMOVED_PARAMETERS"

    for k_baru in (
        "ENTRY_RULE_ENABLED",
        "ENTRY_BULL_CANDLES",
        "ENTRY_SL_ANCHOR",
        "ENTRY_SL_BUFFER_PCT",
        "ENTRY_MIN_RR_RATIO",
        "ENTRY_MAX_SL_PCT",
        "EXIT_TRAILING_PCT",
    ):
        assert k_baru in PUMP_CONFIG, f"{k_baru} hilang dari PUMP_CONFIG"
        assert k_baru in PARAMETER_SCHEMA, f"{k_baru} hilang dari PARAMETER_SCHEMA"
        assert k_baru in bt.OVERRIDE_CASTERS, f"{k_baru} hilang dari OVERRIDE_CASTERS"

    cfg = deepcopy(PUMP_CONFIG)
    cfg["CONFIRM_INTERVAL"] = "5m"
    cfg["ENTRY_RULE_ENABLED"] = True
    cfg["ENTRY_BULL_CANDLES"] = 3
    cfg["ENTRY_SL_ANCHOR"] = "SIGNAL"
    rangkaian = sd.seri_dengan_setup(harga=HARGA_AWAL, panjang_ekor=20, ekor="naik")
    setup = detect_entry_setup(rangkaian, cfg)
    assert setup.ok, f"setup 3 candle bullish harus lolos: {setup.reason}"
    assert setup.sl_price is not None and setup.tp_price is not None
    assert setup.sl_price < float(setup.signal_close) < setup.tp_price
    assert abs(
        setup.sl_price
        - float(rangkaian[-1].low) * (1.0 - cfg["ENTRY_SL_BUFFER_PCT"] / 100.0)
    ) < 1e-9, "SL wajib dihitung dari low candle sinyal dikurangi buffer"
    assert setup.rr_ratio is not None and abs(setup.rr_ratio - cfg["ENTRY_MIN_RR_RATIO"]) < 1e-9

    cfg_batas = deepcopy(cfg)
    cfg_batas["ENTRY_MAX_SL_PCT"] = 0.05
    setup_tolak = detect_entry_setup(rangkaian, cfg_batas)
    assert not setup_tolak.ok and "jarak SL" in setup_tolak.reason, (
        f"setup dengan jarak SL di atas ENTRY_MAX_SL_PCT harus ditolak: {setup_tolak.reason}"
    )

    lv = strategy.resolve_exit_levels(cfg, setup=setup, entry_price=setup.signal_close)
    assert lv["source"] == "ENTRY_RULE"
    assert abs(lv["tp_price"] - setup.tp_price) < 1e-9
    assert abs(lv["sl_price"] - setup.sl_price) < 1e-9
    return (
        f"SL={lv['sl_price']:.4f} (-{lv['sl_pct']:.2f}%), "
        f"TP={lv['tp_price']:.4f} (+{lv['tp_pct']:.2f}%), "
        f"R:R=1:{setup.rr_ratio:.2f}, {len(kunci_exit_lama)} parameter lama dihapus bersih"
    )


def cek_trailing_persen() -> str:
    cfg = config_dasar()
    params = strategy.exit_trailing_params(cfg)
    assert params["enabled"] is True, "trailing harus selalu aktif"
    assert params["start_r"] == 1.0, f"ambang trailing harus 1R, bukan {params['start_r']}"
    assert params["pct"] == 2.0, f"jarak trailing default harus 2 persen, bukan {params['pct']}"
    sempit = dict(cfg)
    sempit["EXIT_TRAILING_PCT"] = 0.0
    assert strategy.exit_trailing_params(sempit)["pct"] == entry_rule.EXIT_TRAILING_MIN_PCT, (
        "jarak trailing nol harus dijepit ke batas bawah"
    )

    pos = _posisi({})
    assert parity.evaluate_candle_exit(pos, _candle(103.0, 101.0, 102.5, 0), cfg) is None
    assert parity.evaluate_candle_exit(pos, _candle(103.0, 102.6, 102.8, 1), cfg) is None
    assert pos.trailing_active, "trailing tidak aktif padahal puncak 3 persen di atas entry"
    harap = 103.0 * 0.98
    assert abs(pos.trailing_stop_price - harap) < 1e-9, (
        f"stop trailing {pos.trailing_stop_price} bukan {harap}"
    )
    assert parity.evaluate_candle_exit(pos, _candle(102.9, 102.7, 102.8, 2), cfg) is None
    assert abs(pos.trailing_stop_price - harap) < 1e-9, "stop trailing turun tanpa puncak baru"
    hasil = parity.evaluate_candle_exit(pos, _candle(102.5, 100.8, 101.0, 3), cfg)
    assert hasil is not None, "trailing tidak menutup posisi setelah harga jatuh ke stop"
    assert hasil[0] == parity.TRAILING_STOP_REASON, f"alasan exit {hasil[0]}"
    assert abs(hasil[1] - harap) < 1e-9, f"harga exit {hasil[1]} bukan stop trailing {harap}"
    return f"aktif di 1R, stop {harap:.4f}, exit {hasil[0]} di {hasil[1]:.4f}"


def cek_tanpa_take_profit() -> str:
    cfg = config_dasar()
    assert entry_rule.EXIT_TAKE_PROFIT_ENABLED is False, "take profit harus mati permanen"
    pos = _posisi({})
    for detik, (tinggi, rendah, tutup) in enumerate(
        ((108.4, 107.9, 108.2), (112.0, 111.4, 111.8), (118.0, 117.2, 117.6))
    ):
        hasil = parity.evaluate_candle_exit(pos, _candle(tinggi, rendah, tutup, detik), cfg)
        assert hasil is None, f"posisi ditutup di atas target padahal take profit sudah dihapus: {hasil}"
    assert pos.trailing_active, "trailing tidak aktif padahal harga sudah jauh di atas 1R"
    assert pos.trailing_stop_price > pos.entry_price, (
        f"stop trailing {pos.trailing_stop_price} belum di atas harga entry"
    )
    return (
        f"harga naik ke 118 tanpa exit, stop trailing naik ke {pos.trailing_stop_price:.4f}"
    )


def cek_breakeven_tetap() -> str:
    params = strategy.entry_breakeven_params(config_dasar())
    assert params["enabled"] is True, "breakeven harus selalu aktif"
    assert params["r_multiple"] == 1.0 and params["lock_pct"] == 0.0, (
        f"breakeven harus tetap 1R tanpa lock, bukan {params}"
    )
    rusak = config_dasar()
    rusak["ENTRY_BREAKEVEN_ENABLED"] = False
    rusak["ENTRY_BREAKEVEN_R_MULTIPLE"] = 5.0
    assert strategy.entry_breakeven_params(rusak) == params, (
        "config lama masih bisa mengubah breakeven"
    )
    assert abs(strategy.entry_breakeven_trigger(100.0, 98.0, 1.0) - 102.0) < 1e-9
    assert abs(strategy.entry_breakeven_price(100.0, 98.0, 1.0, 0.0) - 100.0) < 1e-9

    pos = _posisi({})
    hasil = parity.evaluate_candle_exit(pos, _candle(102.5, 100.5, 101.8, 0), config_dasar())
    assert hasil is None, f"breakeven tidak boleh menutup posisi sendiri: {hasil}"
    assert pos.be_active, "breakeven tidak aktif padahal harga menembus 1R"
    assert abs(pos.sl_price - 100.0) < 1e-9, f"stop breakeven {pos.sl_price} bukan harga entry"
    keluar = parity.evaluate_candle_exit(pos, _candle(100.2, 99.8, 99.9, 1), config_dasar())
    assert keluar is not None and keluar[0] == parity.FIXED_BREAKEVEN_REASON, (
        f"exit breakeven memberi {keluar}"
    )
    return f"aktif di 1R, stop pindah ke 100.0000, exit {keluar[0]} di {keluar[1]:.4f}"


def cek_parameter_exit_tunggal() -> str:
    assert entry_rule.EXIT_PARAM_KEYS == PARAMETER_EXIT, (
        f"daftar parameter exit berubah: {entry_rule.EXIT_PARAM_KEYS}"
    )
    sisa = {
        k
        for k in PUMP_CONFIG
        if k.startswith("EXIT_") or k.endswith("_BREAKEVEN") or "BREAKEVEN" in k or "MAX_HOLD" in k
    }
    assert sisa == set(PARAMETER_EXIT), f"masih ada parameter exit lain di config: {sorted(sisa)}"

    from config.settings_schema import PARAMETER_SCHEMA

    sisa_schema = {
        k
        for k in PARAMETER_SCHEMA
        if k.startswith("EXIT_") or "BREAKEVEN" in k or "MAX_HOLD" in k
    }
    assert sisa_schema == set(PARAMETER_EXIT), (
        f"masih ada parameter exit lain di schema: {sorted(sisa_schema)}"
    )
    sisa_caster = {
        k
        for k in bt.OVERRIDE_CASTERS
        if k.startswith("EXIT_") or "BREAKEVEN" in k or "MAX_HOLD" in k
    }
    assert sisa_caster == set(PARAMETER_EXIT), (
        f"masih ada parameter exit lain di override backtest: {sorted(sisa_caster)}"
    )
    for k in KUNCI_EXIT_DIHAPUS:
        assert k not in PUMP_CONFIG, f"{k} masih ada di PUMP_CONFIG"
        assert k not in PARAMETER_SCHEMA, f"{k} masih ada di PARAMETER_SCHEMA"
        assert k not in bt.OVERRIDE_CASTERS, f"{k} masih ada di OVERRIDE_CASTERS"

    tetap = strategy.exit_fixed_params()
    assert tetap["max_hold_hours"] == CAP_TETAP
    assert tetap["take_profit_enabled"] is False
    assert tetap["breakeven_enabled"] is True
    assert tetap["breakeven_r_multiple"] == 1.0 and tetap["breakeven_lock_pct"] == 0.0
    assert tetap["trailing_start_r"] == 1.0
    return (
        f"{len(PARAMETER_EXIT)} parameter exit ({PARAMETER_EXIT[0]}), "
        f"{len(KUNCI_EXIT_DIHAPUS)} kunci lain dihapus bersih dari config, schema, dan override"
    )


CEK: tuple[tuple[str, Callable[[], str]], ...] = (
    ("batas_tahan_tetap", cek_batas_tahan_tetap),
    ("time_stop_menutup_posisi", cek_time_stop_menutup_posisi),
    ("prioritas_exit_harga", cek_prioritas_exit_harga),
    ("kapital_berputar", cek_kapital_berputar),
    ("paritas_aturan_live", cek_paritas_aturan_live),
    ("integrasi_parameter", cek_integrasi_parameter),
    ("exit_aturan_entry", cek_exit_aturan_entry),
    ("trailing_persen", cek_trailing_persen),
    ("tanpa_take_profit", cek_tanpa_take_profit),
    ("breakeven_tetap", cek_breakeven_tetap),
    ("parameter_exit_tunggal", cek_parameter_exit_tunggal),
)


def run(verbose: bool = True) -> tuple[bool, list[str]]:
    hasil: list[str] = []
    gagal = 0
    for nama, fungsi in CEK:
        try:
            pesan = fungsi()
            baris = f"[OK]    {nama}: {pesan}"
        except AssertionError as exc:
            gagal += 1
            baris = f"[GAGAL] {nama}: {exc}"
        except Exception as exc:
            gagal += 1
            baris = f"[ERROR] {nama}: {type(exc).__name__}: {exc}"
        hasil.append(baris)
        if verbose:
            print(baris)
    if verbose:
        total = len(CEK)
        if gagal:
            print(f"Selftest exit selesai: {gagal} dari {total} pemeriksaan GAGAL.")
        else:
            print(f"Selftest exit selesai: {total}/{total} pemeriksaan lulus.")
    return gagal == 0, hasil
