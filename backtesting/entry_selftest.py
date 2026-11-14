from __future__ import annotations

from copy import deepcopy

from backtesting import synthetic_data as sd
from config.config import CONFIG_LOAD_ERRORS, PUMP_CONFIG
from config.settings_schema import PARAMETER_SCHEMA, REMOVED_PARAMETERS, relation_violations
from market import market_scanner as scanner
from strategy import entry_rule
from strategy import indicators as strategy
from strategy import vidya
from strategy.indicators import Kline

KUNCI_LAMA = ("ENTRY_FILTER_MODE", "DEMAND_EMA_PERIOD", "DEMAND_IMPULSE_ATR_MULT")
KUNCI_VIDYA = (
    "VIDYA_LENGTH",
    "VIDYA_MOMENTUM",
    "VIDYA_BAND_MULT",
    "VIDYA_ATR_PERIOD",
    "VIDYA_DELTA_LOOKBACK",
    "VIDYA_DELTA_MIN_RATIO",
)


def config_dasar(**ganti) -> dict:
    cfg = deepcopy(PUMP_CONFIG)
    cfg.update({
        "GAINER_VOLUME_FILTER_ENABLED": False,
        "CONFIRM_INTERVAL": "15m",
        "ENTRY_SL_ATR_BUFFER_MULT": 0.5,
        "ENTRY_ATR_PERIOD": 14,
        "ENTRY_MIN_RR_RATIO": 3.0,
        "EXIT_ATR_MAX_SL_PCT": 10.0,
        "VIDYA_DELTA_MIN_RATIO": 0.0,
        "VIDYA_ENTRY_TRIGGER": "FLIP",
        "VIDYA_SL_ANCHOR": "LOWER_BAND",
    })
    cfg.update(ganti)
    return cfg


def seri_sinyal(cfg: dict | None = None, **opsi) -> list[Kline]:
    """Seri sintetis yang berakhir tepat di candle flip tren naik VIDYA."""
    bars = sd.seri_vidya_flip(siklus=1, bar_lanjut=0, **opsi)
    series = vidya.compute_series(bars, cfg or PUMP_CONFIG)
    indeks = [i for i, naik in enumerate(series["cross_up"]) if naik]
    assert indeks, "generator sintetis tidak menghasilkan flip tren naik"
    return bars[: indeks[-1] + 1]


def ganti_terakhir(bars: list[Kline], open_: float, close: float, low: float, high: float) -> list[Kline]:
    last = bars[-1]
    out = list(bars)
    out[-1] = Kline(
        open_time=last.open_time, open=open_, high=high, low=low, close=close,
        close_time=last.close_time, volume=last.volume, quote_volume=last.quote_volume,
    )
    return out


def cek_konfigurasi() -> str:
    for key in KUNCI_VIDYA:
        assert key in PUMP_CONFIG and key in PARAMETER_SCHEMA, key
    for key in KUNCI_LAMA:
        assert key not in PUMP_CONFIG and key not in PARAMETER_SCHEMA, key
        assert key in REMOVED_PARAMETERS, key
    assert entry_rule.signal_params(PUMP_CONFIG)["mode"] == entry_rule.ENTRY_FILTER_VIDYA
    assert PUMP_CONFIG["CONFIRM_INTERVAL"] in entry_rule.VIDYA_TRIGGER_INTERVALS
    assert PUMP_CONFIG["CONFIRM_LOOKBACK_BARS"] >= entry_rule.required_bars(PUMP_CONFIG)
    assert entry_rule.required_bars(PUMP_CONFIG) <= entry_rule.KLINE_FETCH_LIMIT
    assert not CONFIG_LOAD_ERRORS, CONFIG_LOAD_ERRORS
    assert not relation_violations(PUMP_CONFIG), relation_violations(PUMP_CONFIG)
    salah = deepcopy(PUMP_CONFIG)
    salah["CONFIRM_INTERVAL"] = "1h"
    assert "CONFIRM_INTERVAL" in relation_violations(salah), "interval 1h harus ditolak skema"
    return "konfigurasi VIDYA lengkap, kunci filter lama sudah dihapus, dan riwayat candle sinkron"


def cek_interval_didukung() -> str:
    for interval in entry_rule.VIDYA_TRIGGER_INTERVALS:
        assert entry_rule.trigger_interval_ok(interval, PUMP_CONFIG), interval
    for interval in ("1h", "4h", "1d", "2m"):
        assert not entry_rule.trigger_interval_ok(interval, PUMP_CONFIG), interval
    bars = seri_sinyal()
    result = entry_rule.evaluate_entry_rule(bars, config_dasar(CONFIRM_INTERVAL="1h"))
    assert not result["ok"] and "interval" in result["reason"], result
    return "interval scalping 1m sampai 30m didukung dan interval di atasnya ditolak"


def cek_sinyal_lolos() -> str:
    cfg = config_dasar()
    bars = seri_sinyal(cfg)
    series = vidya.compute_series(bars, cfg)
    indeks = len(bars) - 1
    assert series["cross_up"][indeks], "bar terakhir harus flip tren naik"
    result = entry_rule.evaluate_entry_rule(bars, cfg)
    assert result["ok"], result
    assert result["stage"] == entry_rule.STAGE_OK
    assert result["zone_low"] is not None and result["zone_high"] is not None
    assert result["zone_low"] < result["zone_high"]
    assert result["signal_close"] > result["zone_high"] > result["ema_m15"]
    assert result["zone_timeframe"] == "15m"
    assert result["sl_price"] < result["entry_low"] < result["signal_close"] < result["tp_price"]
    assert abs(result["tp_pct"] / result["sl_pct"] - 3.0) < 1e-9
    assert all(result["checks"].values()), result["checks"]
    assert result["values"]["sl_anchor"] == "LOWER_BAND"
    return "flip tren naik dengan delta volume beli dominan lolos, SL di bawah anchor dengan buffer ATR"


def cek_tanpa_flip_ditolak() -> str:
    cfg = config_dasar()
    bars = seri_sinyal(cfg)
    potong = bars[:-5]
    result = entry_rule.evaluate_entry_rule(potong, cfg)
    assert not result["ok"], result
    assert result["stage"] == entry_rule.STAGE_VIDYA_TREND, result
    assert result["checks"]["vidya_ready"] and not result["checks"]["vidya_flip"]
    assert not result["checks"]["delta_volume"] and not result["checks"]["risk"]
    return "periode tanpa flip tren naik ditolak di tahap VIDYA_TREND"


def cek_delta_ditolak() -> str:
    cfg = config_dasar()
    bars = seri_sinyal(cfg, volume_dorong=100_000.0)
    result = entry_rule.evaluate_entry_rule(bars, cfg)
    assert not result["ok"], result
    assert result["stage"] == entry_rule.STAGE_DELTA_VOLUME, result
    assert result["checks"]["vidya_ready"] and not result["checks"]["delta_volume"]
    rasio = vidya.compute_series(bars, cfg)["delta_ratio"][-1]
    assert rasio is not None and rasio < 0.0, rasio
    return "flip naik dengan delta volume jual dominan ditolak di tahap DELTA_VOLUME"


def cek_riwayat_kurang() -> str:
    cfg = config_dasar()
    bars = seri_sinyal(cfg)
    butuh = entry_rule.required_bars(cfg)
    result = entry_rule.evaluate_entry_rule(bars[: butuh - 1], cfg)
    assert not result["ok"], result
    assert result["stage"] == entry_rule.STAGE_HISTORY, result
    assert "kurang" in result["reason"]
    assert not result["checks"]["history"]
    return f"riwayat kurang dari {butuh} candle ditolak di tahap riwayat"


def cek_candle_tidak_valid() -> str:
    cfg = config_dasar()
    bars = seri_sinyal(cfg)
    last = bars[-1]
    rusak = ganti_terakhir(bars, float(last.open), float(last.close), float(last.high), float(last.low))
    result = entry_rule.evaluate_entry_rule(rusak, cfg)
    assert not result["ok"], result
    assert result["stage"] == entry_rule.STAGE_CANDLE, result
    assert result["checks"]["history"] and not result["checks"]["candles"]
    return "candle dengan high di bawah low ditolak di tahap validasi candle"


def cek_batas_risiko() -> str:
    cfg = config_dasar(EXIT_ATR_MAX_SL_PCT=0.001)
    bars = seri_sinyal(cfg)
    result = entry_rule.evaluate_entry_rule(bars, cfg)
    assert not result["ok"], result
    assert result["stage"] == entry_rule.STAGE_RISK, result
    assert "melampaui" in result["reason"]
    assert result["checks"]["delta_volume"] and not result["checks"]["risk"]
    return "jarak stop yang melewati batas EXIT_ATR_MAX_SL_PCT ditolak di tahap RISK_SL"


def cek_anchor_sl() -> str:
    hasil = {}
    for anchor in entry_rule.VIDYA_SL_ANCHORS:
        cfg = config_dasar(VIDYA_SL_ANCHOR=anchor)
        bars = seri_sinyal(cfg)
        result = entry_rule.evaluate_entry_rule(bars, cfg)
        assert result["ok"], (anchor, result)
        assert result["values"]["sl_anchor"] == anchor
        assert result["sl_price"] < result["entry_low"] < result["signal_close"] < result["tp_price"]
        hasil[anchor] = result["sl_price"]
    assert len(set(round(v, 10) for v in hasil.values())) == len(hasil), hasil
    return "ketiga anchor stop loss menghasilkan SL berbeda yang selalu di bawah harga entry"


def cek_trigger_pullback() -> str:
    cfg = config_dasar(VIDYA_ENTRY_TRIGGER="PULLBACK")
    bars = seri_sinyal(cfg)
    flip = entry_rule.evaluate_entry_rule(bars, config_dasar(VIDYA_ENTRY_TRIGGER="FLIP"))
    assert flip["ok"], "mode FLIP harus menerima candle flip"
    result = entry_rule.evaluate_entry_rule(bars, cfg)
    assert not result["ok"], result
    assert result["stage"] == entry_rule.STAGE_VIDYA_TREND, result
    assert "pullback" in result["reason"]
    return "mode PULLBACK menolak candle flip dan menunggu konfirmasi pullback ke garis VIDYA"


def cek_anti_lookahead() -> str:
    cfg = config_dasar()
    bars = sd.seri_vidya_flip(siklus=1, bar_lanjut=40)
    series = vidya.compute_series(bars, cfg)
    indeks = [i for i, naik in enumerate(series["cross_up"]) if naik]
    assert indeks, "generator sintetis tidak menghasilkan flip tren naik"
    target = indeks[0]
    sebelum = entry_rule.evaluate_entry_rule(bars, cfg, int(bars[target - 1].close_time))
    assert not sebelum["ok"], sebelum
    assert sebelum.get("signal_close_time") == int(bars[target - 1].close_time)
    tepat = entry_rule.evaluate_entry_rule(bars, cfg, int(bars[target].close_time))
    assert tepat["ok"] and tepat["signal_close_time"] == int(bars[target].close_time)
    return "candle setelah waktu evaluasi tidak ikut dihitung (anti lookahead)"


def cek_nonaktif() -> str:
    cfg = config_dasar(ENTRY_RULE_ENABLED=False)
    bars = seri_sinyal(cfg)
    result = entry_rule.evaluate_entry_rule(bars, cfg)
    assert result["ok"], result
    assert result["stage"] == entry_rule.STAGE_DISABLED, result
    assert result["sl_price"] is not None and result["tp_price"] is not None
    assert result["sl_price"] < result["signal_close"] < result["tp_price"]
    return "filter entry nonaktif tetap mengembalikan level SL dan TP yang bisa dipakai"


def cek_snapshot() -> str:
    cfg = config_dasar()
    bars = seri_sinyal(cfg)
    snapshot = strategy.vidya_snapshot(bars, cfg)
    assert snapshot["ok"], snapshot
    assert snapshot["mode"] == entry_rule.ENTRY_FILTER_VIDYA
    assert len(snapshot["vidya"]) == len(snapshot["upper"]) == len(snapshot["lower"])
    assert snapshot["cross_up"][-1] is True
    result = entry_rule.evaluate_entry_rule(bars, cfg)
    assert abs(float(snapshot["vidya"][-1]) - float(result["ema_m15"])) < 1e-9
    assert abs(float(snapshot["lower"][-1]) - float(result["zone_low"])) < 1e-9
    return "snapshot VIDYA sejajar dengan hasil evaluasi entry pada candle yang sama"


def cek_scanner_sinkron() -> str:
    cfg = config_dasar()
    bars = seri_sinyal(cfg)
    setup = scanner.detect_entry_setup(bars, cfg)
    assert setup.ok, setup.reason
    result = entry_rule.evaluate_entry_rule(bars, cfg)
    assert result["ok"], result
    assert abs(float(setup.sl_price) - float(result["sl_price"])) < 1e-9
    assert abs(float(setup.tp_price) - float(result["tp_price"])) < 1e-9
    levels = strategy.resolve_exit_levels(cfg, setup=setup, entry_price=float(setup.signal_close))
    assert levels["sl_price"] and abs(levels["sl_price"] - float(setup.sl_price)) < 1e-9
    return "scanner dan mesin exit memakai level VIDYA yang sama dengan aturan entry"


CHECKS = (
    cek_konfigurasi,
    cek_interval_didukung,
    cek_sinyal_lolos,
    cek_tanpa_flip_ditolak,
    cek_delta_ditolak,
    cek_riwayat_kurang,
    cek_candle_tidak_valid,
    cek_batas_risiko,
    cek_anchor_sl,
    cek_trigger_pullback,
    cek_anti_lookahead,
    cek_nonaktif,
    cek_snapshot,
    cek_scanner_sinkron,
)


def run(verbose: bool = False) -> tuple[bool, list[str]]:
    messages: list[str] = []
    ok = True
    for check in CHECKS:
        try:
            messages.append(check())
            if verbose:
                print(f"  [LULUS] {messages[-1]}")
        except Exception as exc:
            ok = False
            messages.append(f"GAGAL: {check.__name__}: {exc}")
            if verbose:
                print(f"  [GAGAL] {check.__name__}: {exc}")
    return ok, messages


def main() -> int:
    print("Selftest aturan entry VIDYA:")
    ok, pesan = run(verbose=True)
    print(f"HASIL: {len(CHECKS)}/{len(CHECKS)} lulus" if ok else "HASIL: ADA YANG GAGAL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
