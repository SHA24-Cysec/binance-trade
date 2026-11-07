from __future__ import annotations

from copy import deepcopy

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
from strategy import exit_atr
from strategy import indicators as strategy
from strategy.indicators import Kline

MS_1H = 3_600_000
ATR_PERIOD_UJI = 14


def _candle(
    index: int,
    open_: float,
    close: float,
    low: float,
    high: float | None = None,
    volume: float = 1_000.0,
    bar_ms: int = MS_1H,
) -> Kline:
    if high is None:
        high = max(open_, close) * 1.001
    return sd.make_candle(index, open_, high, low, close, volume, bar_ms=bar_ms)


def seri_datar(
    panjang: int = 60, harga: float = 100.0, volume: float = 1_000.0
) -> list[Kline]:
    return [
        _candle(i, harga, harga * 1.0005, harga * 0.998, harga * 1.002, volume)
        for i in range(panjang)
    ]


def seri_momentum(
    sebelum: int = 40,
    harga: float = 100.0,
    volume: float = 1_000.0,
    lonjakan: float = 1.02,
    volume_kali: float = 3.0,
    body_kali: float = 1.0,
) -> list[Kline]:
    bars = seri_datar(sebelum, harga, volume)
    acuan = bars[-1]
    open_ = float(acuan.close)
    high_acuan = float(acuan.high)
    close = high_acuan * lonjakan
    tinggi = close
    if body_kali < 1.0:
        close = open_ + (close - open_) * body_kali
        tinggi = close * 1.0005
    bars.append(
        _candle(
            sebelum,
            open_,
            close,
            min(open_, close) * 0.999,
            tinggi,
            volume * volume_kali,
        )
    )
    return bars


def config_dasar() -> dict:
    cfg = deepcopy(PUMP_CONFIG)
    cfg.update(
        {
            "_symbol": "",
            "MODE": "PAPER",
            "ENTRY_RULE_ENABLED": True,
            "CONFIRM_INTERVAL": "1h",
            "CONFIRM_LOOKBACK_BARS": 121,
            "MIN_QUOTE_VOLUME_USDT_24H": 0.0,
            "GAINER_MIN_CHANGE_PCT": 0.0,
            "GAINER_MAX_CHANGE_PCT": 0.0,
            "GAINER_VOLUME_FILTER_ENABLED": False,
            "MOMENTUM_BREAKOUT_LOOKBACK": 1,
            "MOMENTUM_BODY_ATR_MULT": 0.5,
            "MOMENTUM_VOLUME_MULT": 1.5,
            "MOMENTUM_VOLUME_LOOKBACK": 20,
            "ENTRY_ATR_PERIOD": ATR_PERIOD_UJI,
            "EXIT_ATR_SL_MULT": 1.5,
            "EXIT_ATR_TP_MULT": 3.0,
            "EXIT_ATR_TRAIL_MULT": 2.0,
            "EXIT_ATR_BREAKEVEN_MULT": 1.0,
            "EXIT_ATR_MAX_SL_PCT": 5.0,
            "ENTRY_MIN_RR_RATIO": 2.0,
            "SAME_COIN_BLOCK_HOURS": 0.0,
            "USE_RISK_PERCENT": False,
            "POSITION_SIZE_USDT": 100.0,
            "MAX_POSITION_USDT": 100.0,
            "BACKTEST_INITIAL_EQUITY_USDT": 1000.0,
        }
    )
    return cfg


def cek_konfigurasi_bersih() -> str:
    kunci_baru = (
        "GAINER_MIN_CHANGE_PCT",
        "GAINER_MAX_CHANGE_PCT",
        "MOMENTUM_BREAKOUT_LOOKBACK",
        "MOMENTUM_BODY_ATR_MULT",
        "MOMENTUM_VOLUME_MULT",
        "MOMENTUM_VOLUME_LOOKBACK",
        "ENTRY_ATR_PERIOD",
        "EXIT_ATR_SL_MULT",
        "EXIT_ATR_TP_MULT",
        "EXIT_ATR_TRAIL_MULT",
        "EXIT_ATR_BREAKEVEN_MULT",
        "EXIT_ATR_MAX_SL_PCT",
    )
    for kunci in kunci_baru:
        assert kunci in PUMP_CONFIG, f"{kunci} hilang dari PUMP_CONFIG"
        assert kunci in PARAMETER_SCHEMA, f"{kunci} hilang dari PARAMETER_SCHEMA"
        assert kunci in bt.OVERRIDE_CASTERS, f"{kunci} hilang dari OVERRIDE_CASTERS"

    for kunci in (
        "BTC_FILTER_ENABLED",
        "BTC_MAX_DROP_PCT",
        "BTC_LOOKBACK_BARS",
        "CONFLUENCE_FILTER_ENABLED",
        "CONFLUENCE_MIN_SCORE",
        "VP_FILTER_ENABLED",
        "FIB_FILTER_ENABLED",
        "LIQUIDITY_FILTER_ENABLED",
        "FVG_FILTER_ENABLED",
        "OB_FILTER_ENABLED",
    ):
        assert kunci not in PUMP_CONFIG, f"{kunci} masih ada di PUMP_CONFIG"
        assert kunci not in PARAMETER_SCHEMA, f"{kunci} masih ada di PARAMETER_SCHEMA"
        assert kunci in REMOVED_PARAMETERS, f"{kunci} belum masuk REMOVED_PARAMETERS"

    assert not CONFIG_LOAD_ERRORS, f"config gagal dimuat: {CONFIG_LOAD_ERRORS}"
    sisa = sorted(set(PUMP_CONFIG) - set(PARAMETER_SCHEMA))
    assert not sisa, f"kunci config tanpa skema: {sisa}"
    pelanggaran = relation_violations(PUMP_CONFIG, mode_aware=True)
    assert not pelanggaran, f"kombinasi default tidak konsisten: {pelanggaran}"
    assert PUMP_CONFIG["CONFIRM_INTERVAL"] == "1h", (
        f"interval momentum default harus 1h, bukan {PUMP_CONFIG['CONFIRM_INTERVAL']}"
    )
    return "parameter gainer, momentum H1, dan exit ATR tersinkron di config dan schema"


def cek_modul_lama_hilang() -> str:
    import importlib.util

    for nama in ("strategy.confluence_code", "backtesting.confluence_fixture"):
        assert importlib.util.find_spec(nama) is None, f"modul {nama} masih bisa diimpor"
    assert not hasattr(strategy, "evaluate_confluence_code"), "fungsi konfluensi masih ada"
    return "modul lama tetap terhapus dan tidak ada sisa simbol di strategy"


def cek_momentum_lolos() -> str:
    cfg = config_dasar()
    hasil = entry_rule.evaluate_entry_rule(seri_momentum(), cfg)
    assert hasil["ok"], hasil["reason"]
    assert hasil["interval"] == "1h", hasil["interval"]
    assert hasil["body_atr"] >= cfg["MOMENTUM_BODY_ATR_MULT"], hasil["body_atr"]
    assert hasil["volume_ratio"] >= cfg["MOMENTUM_VOLUME_MULT"], hasil["volume_ratio"]
    params = exit_atr.atr_params(cfg)
    assert abs(hasil["sl_price"] - (hasil["signal_close"] - params["sl_mult"] * hasil["atr"])) < 1e-9
    assert abs(hasil["tp_price"] - (hasil["signal_close"] + params["tp_mult"] * hasil["atr"])) < 1e-9
    return (
        f"breakout + body {hasil['body_atr']:.2f} x ATR + volume "
        f"{hasil['volume_ratio']:.2f} x lolos, SL dan TP dari ATR"
    )


def cek_ditolak_bukan_bullish() -> str:
    cfg = config_dasar()
    bars = seri_datar(40)
    bars.append(
        _candle(40, 100.0, 99.5, 99.0, 100.6, 3_000.0)
    )
    hasil = entry_rule.evaluate_entry_rule(bars, cfg)
    assert not hasil["ok"], hasil
    assert hasil["stage"] == entry_rule.STAGE_SEQUENCE, hasil["stage"]
    return "candle bearish ditolak di tahap momentum bull"


def seri_volatil(panjang: int = 40, harga: float = 100.0) -> list[Kline]:
    return [
        _candle(i, harga, harga * 1.005, harga * 0.98, harga * 1.02)
        for i in range(panjang)
    ]


def cek_ditolak_body_kecil() -> str:
    cfg = config_dasar()
    bars = seri_volatil(40)
    acuan = bars[-1]
    bars.append(
        _candle(
            40,
            float(acuan.close),
            float(acuan.high) * 1.002,
            float(acuan.close) * 0.999,
            float(acuan.high) * 1.003,
            3_000.0,
        )
    )
    hasil = entry_rule.evaluate_entry_rule(bars, cfg)
    assert not hasil["ok"], hasil
    assert hasil["stage"] == entry_rule.STAGE_BODY, hasil["stage"]
    return (
        f"body {hasil['body_atr']:.2f} x ATR ditolak karena minimum "
        f"{cfg['MOMENTUM_BODY_ATR_MULT']:g} x ATR"
    )


def cek_ditolak_tanpa_breakout() -> str:
    cfg = config_dasar()
    bars = seri_datar(40)
    acuan = bars[-1]
    bars.append(
        _candle(
            40,
            99.6,
            100.1,
            99.55,
            100.15,
            3_000.0,
        )
    )
    hasil = entry_rule.evaluate_entry_rule(bars, cfg)
    assert not hasil["ok"], hasil
    assert hasil["stage"] == entry_rule.STAGE_BREAKOUT, hasil["stage"]
    assert float(acuan.high) > 100.1, acuan.high
    return "close yang tidak melewati high candle sebelumnya ditolak di tahap breakout"


def cek_ditolak_volume_tipis() -> str:
    cfg = config_dasar()
    hasil = entry_rule.evaluate_entry_rule(seri_momentum(volume_kali=0.4), cfg)
    assert not hasil["ok"], hasil
    assert hasil["stage"] == entry_rule.STAGE_VOLUME, hasil["stage"]
    return (
        f"volume {hasil['volume_ratio']:.2f} x rata-rata ditolak karena minimum "
        f"{cfg['MOMENTUM_VOLUME_MULT']:g} x"
    )


def cek_lookback_breakout() -> str:
    cfg = config_dasar()
    cfg["MOMENTUM_BREAKOUT_LOOKBACK"] = 5
    lulus = entry_rule.evaluate_entry_rule(seri_momentum(lonjakan=1.02), cfg)
    assert lulus["ok"], lulus["reason"]
    gagal = entry_rule.evaluate_entry_rule(seri_momentum(lonjakan=0.0005), cfg)
    assert not gagal["ok"], gagal
    return "lookback breakout 5 candle dipakai saat menilai penembusan high"


def cek_riwayat_kurang() -> str:
    cfg = config_dasar()
    hasil = entry_rule.evaluate_entry_rule(seri_datar(10), cfg)
    assert not hasil["ok"], hasil
    assert hasil["stage"] == entry_rule.STAGE_HISTORY, hasil["stage"]
    assert "kurang" in hasil["reason"], hasil["reason"]
    return f"riwayat {len(seri_datar(10))} candle ditolak dari minimum {hasil['required']}"


def cek_anti_look_ahead() -> str:
    cfg = config_dasar()
    bars = seri_momentum()
    sinyal = bars[-1]
    hasil = entry_rule.evaluate_entry_rule(
        bars,
        cfg,
        signal_close_time_ms=int(sinyal.close_time) - MS_1H - 1,
    )
    assert not hasil["ok"], "candle setelah waktu sinyal ikut dinilai"
    assert int(hasil["signal_close_time"]) < int(sinyal.close_time), hasil
    return "candle yang tutup setelah waktu sinyal tidak pernah dipakai"


def cek_atr_wajib() -> str:
    cfg = config_dasar()
    cfg["ENTRY_ATR_PERIOD"] = 30
    hasil = entry_rule.evaluate_entry_rule(seri_momentum(sebelum=15), cfg)
    assert not hasil["ok"], hasil
    assert hasil["stage"] in (entry_rule.STAGE_HISTORY, entry_rule.STAGE_RISK), hasil["stage"]
    return "ATR yang tidak bisa dihitung dari riwayat menolak entry"


def cek_batas_jarak_stop() -> str:
    cfg = config_dasar()
    cfg["EXIT_ATR_MAX_SL_PCT"] = 0.2
    hasil = entry_rule.evaluate_entry_rule(seri_momentum(), cfg)
    assert not hasil["ok"], hasil
    assert hasil["stage"] == entry_rule.STAGE_RISK, hasil["stage"]
    return "stop ATR yang lebih lebar dari batas persen menolak setup"


def cek_momentum_nonaktif() -> str:
    cfg = config_dasar()
    cfg["ENTRY_RULE_ENABLED"] = False
    hasil = entry_rule.evaluate_entry_rule(seri_datar(60), cfg)
    assert hasil["ok"], hasil["reason"]
    assert hasil["stage"] == entry_rule.STAGE_DISABLED, hasil["stage"]
    assert hasil["sl_price"] is not None and hasil["tp_price"] is not None, hasil
    return "filter nonaktif tetap menghasilkan level cadangan tanpa error"


def cek_ranking_gainer() -> str:
    cfg = config_dasar()
    tickers = [
        {
            "symbol": "AAAUSDT",
            "quoteVolume": "9000000",
            "lastPrice": "1.2",
            "priceChangePercent": "18.5",
        },
        {
            "symbol": "BBBUSDT",
            "quoteVolume": "50000000",
            "lastPrice": "2.5",
            "priceChangePercent": "2.0",
        },
        {
            "symbol": "CCCUSDT",
            "quoteVolume": "7000000",
            "lastPrice": "0.4",
            "priceChangePercent": "42.0",
        },
        {
            "symbol": "DDDUSDT",
            "quoteVolume": "100000",
            "lastPrice": "0.1",
            "priceChangePercent": "80.0",
        },
        {
            "symbol": "EEEUSDT",
            "quoteVolume": "8000000",
            "lastPrice": "3.0",
            "priceChangePercent": "-12.0",
        },
    ]
    cfg["MIN_QUOTE_VOLUME_USDT_24H"] = 2_000_000.0
    cfg["GAINER_MIN_CHANGE_PCT"] = 3.0
    ranked = scanner.filter_and_rank_candidates(tickers, cfg)
    urutan = [c.symbol for c in ranked]
    assert urutan == ["CCCUSDT", "AAAUSDT"], urutan
    assert ranked[0].change_pct > ranked[1].change_pct, urutan
    assert "DDDUSDT" not in urutan, "koin dengan turnover kecil ikut masuk peringkat"
    assert "BBBUSDT" not in urutan, "koin dengan kenaikan di bawah ambang ikut masuk"
    assert "EEEUSDT" not in urutan, "koin yang turun ikut masuk peringkat gainer"
    return f"peringkat gainer: {urutan}"


def cek_batas_atas_gain() -> str:
    cfg = config_dasar()
    cfg["GAINER_MIN_CHANGE_PCT"] = 3.0
    cfg["GAINER_MAX_CHANGE_PCT"] = 30.0
    ok, alasan = scanner.evaluate_market_gate(42.0, 9_000_000.0, cfg)
    assert not ok, alasan
    assert "melampaui batas" in alasan, alasan
    ok2, _ = scanner.evaluate_market_gate(12.0, 9_000_000.0, cfg)
    assert ok2, "kenaikan wajar justru ditolak"
    return "batas atas kenaikan menolak koin yang sudah terbang terlalu jauh"


def cek_integrasi_scanner() -> str:
    cfg = config_dasar()
    cfg["TOP_N_CANDIDATES_TO_CONFIRM"] = 5
    cfg["MIN_QUOTE_VOLUME_USDT_24H"] = 0.0
    tickers = [
        {
            "symbol": "AAAUSDT",
            "quoteVolume": "9000000",
            "lastPrice": "100",
            "priceChangePercent": "40.0",
        },
        {
            "symbol": "BBBUSDT",
            "quoteVolume": "8000000",
            "lastPrice": "100",
            "priceChangePercent": "20.0",
        },
    ]
    bars = seri_momentum(sebelum=60)
    panggilan: list[str] = []

    def fetcher(simbol: str):
        panggilan.append(simbol)
        if simbol == "AAAUSDT":
            return seri_datar(60)
        return bars

    terpilih = scanner.find_best_candidate(tickers, fetcher, cfg)
    assert terpilih is not None, "tidak ada kandidat yang lolos padahal BBB punya momentum"
    assert terpilih.symbol == "BBBUSDT", terpilih.symbol
    assert panggilan[0] == "AAAUSDT", "urutan pemeriksaan tidak mengikuti peringkat gainer"
    assert terpilih.setup is not None and terpilih.setup.atr_value is not None, terpilih.setup
    assert terpilih.setup.body_atr is not None, terpilih.setup
    return f"kandidat gainer teratas tanpa momentum dilewati, terpilih {terpilih.symbol}"


def cek_scanner_diagnostik() -> str:
    cfg = config_dasar()
    diag = scanner.ScanDiagnostics()
    diag.market_candidates = 10
    diag.checked_candidates = 3
    diag.reject("MOMENTUM_VOLUME", "AAAUSDT", "volume 0.20 x rata-rata 20 candle")
    baris = diag.log_lines(cfg)
    gabung = " ".join(baris)
    assert "filter momentum" in gabung, gabung
    assert "body minimal" in gabung, gabung
    assert "MOMENTUM_VOLUME" not in gabung, "nama tahap mentah bocor ke log pengguna"
    assert "volume di bawah" in gabung, gabung
    return "log diagnostik menyebut aturan momentum H1 dengan bahasa yang bisa dibaca"


def cek_backtest_menghasilkan_trade() -> str:
    cfg = config_dasar()
    cfg.update(
        {
            "CONFIRM_INTERVAL": "5m",
            "MOMENTUM_VOLUME_MULT": 1.5,
            "TAKER_FEE_PCT": 0.1,
            "USE_BNB_FEE_DISCOUNT": False,
            "COOLDOWN_MINUTES_AFTER_CLOSE": 0,
            "MIN_SECONDS_BETWEEN_TRADES": 0,
            "MAX_CHASE_PCT": 0.0,
        }
    )
    bt.validate_params(cfg)
    klines = sd.seri_banyak_setup(harga=100.0, siklus=10, volume=9_000_000.0)
    hasil = bt.run_backtest(klines, cfg, warmup_bars=120)
    assert hasil.trades, "filter momentum tidak menghasilkan trade pada data uji"
    for t in hasil.trades:
        assert t.exit_source == "ATR", t.exit_source
        assert 0.0 < t.sl_pct < 100.0, t.sl_pct
        assert t.tp_pct > 0.0, t.tp_pct
    return f"{len(hasil.trades)} trade, level exit semuanya bersumber ATR"


def cek_atr_tunggal() -> str:
    cfg = config_dasar()
    klines = seri_momentum(sebelum=200)
    verdict = entry_rule.evaluate_entry_rule(klines, cfg)
    assert verdict["ok"], verdict["reason"]
    atr_aturan = verdict["atr"]
    assert atr_aturan and atr_aturan > 0.0, atr_aturan

    setup = scanner.detect_entry_setup(klines, cfg)
    assert setup.ok, setup.reason
    assert abs(float(setup.atr_value) - float(atr_aturan)) < 1e-12, (
        f"ATR setup {setup.atr_value} beda dari ATR aturan {atr_aturan}"
    )

    level = strategy.resolve_exit_levels(cfg, setup=setup, entry_price=setup.signal_close)
    assert abs(float(level["atr"]) - float(atr_aturan)) < 1e-12, (
        f"ATR level exit {level['atr']} beda dari ATR aturan {atr_aturan}"
    )
    assert abs(float(level["sl_price"]) - float(setup.sl_price)) < 1e-9, (
        f"SL level exit {level['sl_price']} beda dari SL setup {setup.sl_price}"
    )
    assert abs(float(level["tp_price"]) - float(setup.tp_price)) < 1e-9, (
        f"TP level exit {level['tp_price']} beda dari TP setup {setup.tp_price}"
    )

    jendela = entry_rule.atr_window_bars(cfg)
    assert jendela >= 121, jendela
    panjang = entry_rule.atr_value(klines[-jendela:], ATR_PERIOD_UJI)
    assert abs(float(panjang) - float(atr_aturan)) < 1e-12, (
        f"ATR jendela {jendela} bar {panjang} beda dari ATR aturan {atr_aturan}"
    )
    potong = entry_rule.evaluate_entry_rule(klines[-jendela:], cfg)
    assert potong["ok"], potong["reason"]
    assert abs(float(potong["atr"]) - float(atr_aturan)) < 1e-12, (
        "ATR berubah saat jumlah candle yang dikirim berbeda; jendela ATR harus tetap"
    )
    assert abs(float(strategy.atr(klines, ATR_PERIOD_UJI)) - float(panjang)) < 1e-12, (
        "strategy.atr harus memakai perhitungan yang sama dengan entry_rule"
    )
    return (
        f"ATR aturan, level exit, dan atr_at_entry sama persis di jendela {jendela} bar "
        f"({atr_aturan:.6g})"
    )


def seri_turnover(
    hari: int = 4,
    volume_lampau: float = 1_000.0,
    volume_kini: float = 5_000.0,
    harga: float = 100.0,
) -> list[Kline]:
    per_hari = 24
    total = per_hari * hari
    bars: list[Kline] = []
    for i in range(total):
        segmen = i // per_hari
        vol = volume_kini if segmen == hari - 1 else volume_lampau
        bars.append(
            _candle(
                i,
                harga,
                harga * 1.0004,
                harga * 0.9985,
                harga * 1.0012,
                volume=vol / harga,
            )
        )
    return bars


def cek_gerbang_turnover() -> str:
    cfg = config_dasar()
    cfg["GAINER_VOLUME_FILTER_ENABLED"] = True
    cfg["GAINER_VOLUME_RISE_MULT"] = 1.2
    cfg["GAINER_VOLUME_AVG_DAYS"] = 3
    cfg["CONFIRM_INTERVAL"] = "1h"
    cfg["CONFIRM_LOOKBACK_BARS"] = 121

    naik = strategy.volume_rise_check(seri_turnover(hari=4, volume_kini=5_000.0), cfg)
    assert naik["ok"], naik["reason"]
    assert not naik["skipped"], naik["reason"]
    assert naik["ratio"] and naik["ratio"] > 1.2, naik["ratio"]

    turun = strategy.volume_rise_check(seri_turnover(hari=4, volume_kini=500.0), cfg)
    assert not turun["ok"], turun["reason"]
    assert turun["stage"] == entry_rule.STAGE_VOLUME_24H, turun["stage"]

    mati = dict(cfg)
    mati["GAINER_VOLUME_FILTER_ENABLED"] = False
    lewat = strategy.volume_rise_check(seri_turnover(hari=4, volume_kini=500.0), mati)
    assert lewat["ok"], lewat["reason"]

    pendek = strategy.volume_rise_check(seri_turnover(hari=1, volume_kini=9_000.0), cfg)
    assert pendek["ok"] and pendek["skipped"], pendek["reason"]

    setup = scanner.detect_entry_setup(seri_turnover(hari=4, volume_kini=500.0), cfg)
    assert not setup.ok, setup.reason
    assert setup.stage == entry_rule.STAGE_VOLUME_24H, setup.stage
    return (
        f"turnover naik {naik['ratio']:.2f}x lolos, turnover turun ditolak di tahap "
        f"{turun['stage']}, data kurang dilewati dengan catatan"
    )


def cek_batas_tahan_parameter() -> str:
    cfg = config_dasar()
    cfg["CONFIRM_INTERVAL"] = "1h"
    cfg["EXIT_MAX_HOLD_HOURS"] = 48.0
    assert abs(entry_rule.max_hold_hours(cfg) - 48.0) < 1e-9
    assert strategy.max_hold_ms(cfg, "1h") == 48 * 3_600_000

    cfg["EXIT_MAX_HOLD_HOURS"] = 0.0
    assert entry_rule.max_hold_hours(cfg) == 0.0
    assert strategy.max_hold_ms(cfg, "1h") == 0

    cfg["EXIT_MAX_HOLD_HOURS"] = 24.0
    tetap = strategy.exit_fixed_params(cfg)
    assert abs(float(tetap["max_hold_hours"]) - 24.0) < 1e-9, tetap
    assert abs(float(tetap["trailing_atr_mult"]) - float(cfg["EXIT_ATR_TRAIL_MULT"])) < 1e-9
    return "batas tahan mengikuti parameter, 0 berarti tanpa batas waktu"


def cek_folder_terhapus() -> str:
    from pathlib import Path

    akar = Path(__file__).resolve().parent.parent
    for nama in ("confluence", "confluence_fixture.py"):
        assert not (akar / "strategy" / nama).exists(), f"sisa berkas {nama}"
    return "tidak ada berkas modul lama yang tertinggal di folder strategy"


CEK = [
    ("konfigurasi bersih", cek_konfigurasi_bersih),
    ("modul lama hilang", cek_modul_lama_hilang),
    ("momentum H1 lolos", cek_momentum_lolos),
    ("candle bearish ditolak", cek_ditolak_bukan_bullish),
    ("body kecil ditolak", cek_ditolak_body_kecil),
    ("tanpa breakout ditolak", cek_ditolak_tanpa_breakout),
    ("volume tipis ditolak", cek_ditolak_volume_tipis),
    ("lookback breakout", cek_lookback_breakout),
    ("riwayat kurang", cek_riwayat_kurang),
    ("anti look ahead", cek_anti_look_ahead),
    ("ATR wajib", cek_atr_wajib),
    ("batas jarak stop", cek_batas_jarak_stop),
    ("momentum nonaktif", cek_momentum_nonaktif),
    ("ranking gainer", cek_ranking_gainer),
    ("batas atas gain", cek_batas_atas_gain),
    ("integrasi scanner", cek_integrasi_scanner),
    ("diagnostik scanner", cek_scanner_diagnostik),
    ("backtest menghasilkan trade", cek_backtest_menghasilkan_trade),
    ("ATR tunggal", cek_atr_tunggal),
    ("gerbang turnover 24 jam", cek_gerbang_turnover),
    ("batas tahan parameter", cek_batas_tahan_parameter),
    ("folder lama bersih", cek_folder_terhapus),
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
        print("Selftest filter momentum H1 dan pemilihan gainer:")
        for item in baris:
            print(f"  {item}")
        print("HASIL: " + ("SEMUA LULUS" if ok_semua else "ADA YANG GAGAL"))
    return ok_semua, baris


if __name__ == "__main__":
    import sys

    lulus, _ = run(verbose=True)
    sys.exit(0 if lulus else 1)
