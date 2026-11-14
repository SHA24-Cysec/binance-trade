from __future__ import annotations

from copy import deepcopy

from config.config import PUMP_CONFIG
from config.settings_schema import relation_violations
from market import market_scanner as scanner
from strategy import entry_rule
from strategy import indicators as strategy
from strategy import vidya
from strategy.indicators import Kline

BAR_MS = 300_000


def config_dasar(interval: str = "5m") -> dict:
    cfg = deepcopy(PUMP_CONFIG)
    cfg.update({
        "GAINER_VOLUME_FILTER_ENABLED": False,
        "CONFIRM_INTERVAL": interval,
        "CONFIRM_LOOKBACK_BARS": 997,
        "ENTRY_ATR_PERIOD": 14,
        "ENTRY_SL_ATR_BUFFER_MULT": 1.0,
        "ENTRY_MIN_RR_RATIO": 2.0,
        "EXIT_ATR_MAX_SL_PCT": 15.0,
        "VIDYA_DELTA_MIN_RATIO": 0.0,
        "VIDYA_ENTRY_TRIGGER": "FLIP",
    })
    return cfg


def buat_bars(closes: list[float], volumes: list[float] | None = None) -> list[Kline]:
    out = []
    n = len(closes)
    for i in range(n):
        c = closes[i]
        o = closes[i - 1] if i else c
        hi = max(o, c) * 1.001
        lo = min(o, c) * 0.999
        v = volumes[i] if volumes else 1000.0
        out.append(Kline(
            open_time=i * BAR_MS, open=o, high=hi, low=lo, close=c,
            close_time=i * BAR_MS + BAR_MS - 1, volume=v, quote_volume=v * c,
        ))
    return out


def seri_flip_naik() -> list[Kline]:
    import math
    closes = []
    for i in range(420):
        base = 100.0 + 9.0 * math.sin(i / 14.0)
        if i >= 360:
            base += (i - 360) * 0.9
        closes.append(base)
    volumes = []
    for i in range(len(closes)):
        bullish = i > 0 and closes[i] > closes[i - 1]
        volumes.append(4000.0 if bullish else 1000.0)
    return buat_bars(closes, volumes)


def cek_math_vidya() -> str:
    bars = seri_flip_naik()
    series = vidya.compute_series(bars, config_dasar())
    assert len(series["vidya"]) == len(bars)
    valid = [v for v in series["vidya"] if v is not None]
    assert valid, "VIDYA tidak menghasilkan nilai"
    closes = [b.close for b in bars]
    warm = [v for v in series["vidya"][entry_rule.required_bars(config_dasar()):] if v is not None]
    assert warm and min(closes) * 0.5 < min(warm) and max(warm) < max(closes) * 1.5
    assert any(series["cross_up"]), "tidak ada flip naik pada seri uptrend"
    idx = next(i for i in range(len(bars)) if series["cross_up"][i])
    assert series["trend_up"][idx] and not series["trend_up"][idx - 1]
    assert series["upper"][idx] is not None and closes[idx] > series["upper"][idx]
    return "garis VIDYA, band ATR, dan flip trend naik konsisten dengan definisi CMO"


def cek_sinyal_lolos() -> str:
    bars = seri_flip_naik()
    series = vidya.compute_series(bars, config_dasar())
    idx = next(i for i in range(len(bars)) if series["cross_up"][i] and (series["delta_ratio"][i] or -1) >= 0)
    result = entry_rule.evaluate_entry_rule(bars[: idx + 1], config_dasar())
    assert result["ok"], result
    assert result["stage"] == entry_rule.STAGE_OK
    assert result["sl_price"] < result["signal_close"] < result["tp_price"]
    assert result["checks"]["vidya_flip"] and result["checks"]["delta_volume"]
    assert abs(result["rr_ratio"] - 2.0) < 1e-9
    return "flip naik VIDYA dengan delta volume beli dominan lolos dan menghasilkan SL/TP valid"


def cek_delta_ditolak() -> str:
    bars = seri_flip_naik()
    cfg = config_dasar()
    series = vidya.compute_series(bars, cfg)
    idx = next(i for i in range(len(bars)) if series["cross_up"][i])
    series["delta_ratio"][idx] = -40.0
    series["delta"][idx] = -1.0
    sig = vidya.signal_at(series, idx, cfg)
    assert not sig["ok"] and sig["stage"] == entry_rule.STAGE_DELTA_VOLUME, sig
    cfg["VIDYA_DELTA_MIN_RATIO"] = 50.0
    series["delta_ratio"][idx] = 20.0
    sig2 = vidya.signal_at(series, idx, cfg)
    assert not sig2["ok"] and sig2["stage"] == entry_rule.STAGE_DELTA_VOLUME, sig2
    return "flip naik dengan delta volume di bawah ambang ditolak pada tahap DELTA_VOLUME"


def cek_tanpa_flip_ditolak() -> str:
    bars = seri_flip_naik()[:300]
    result = entry_rule.evaluate_entry_rule(bars, config_dasar())
    assert not result["ok"] and result["stage"] == entry_rule.STAGE_VIDYA_TREND, result
    return "periode tanpa flip trend naik ditolak pada tahap VIDYA_TREND"


def cek_riwayat_kurang() -> str:
    bars = seri_flip_naik()[:100]
    result = entry_rule.evaluate_entry_rule(bars, config_dasar())
    assert not result["ok"] and result["stage"] == entry_rule.STAGE_HISTORY, result
    return "riwayat candle kurang dari kebutuhan VIDYA ditolak dengan alasan jelas"


def cek_interval_scalping() -> str:
    for interval in ("1m", "3m", "5m", "15m", "30m"):
        cfg = config_dasar(interval)
        assert entry_rule.trigger_interval_ok(interval, cfg), interval
        assert not relation_violations(cfg, mode_aware=True), interval
    cfg = config_dasar("1h")
    assert not entry_rule.trigger_interval_ok("1h", cfg)
    assert "CONFIRM_INTERVAL" in relation_violations(cfg, mode_aware=True)
    return "interval scalping 1m/3m/5m/15m/30m didukung dan 1h ditolak untuk mode VIDYA"


def cek_scanner_sinkron() -> str:
    bars = seri_flip_naik()
    cfg = config_dasar()
    series = vidya.compute_series(bars, cfg)
    idx = next(i for i in range(len(bars)) if series["cross_up"][i] and (series["delta_ratio"][i] or -1) >= 0)
    setup = scanner.detect_entry_setup(bars[: idx + 1], cfg)
    assert setup.ok, setup.reason
    levels = strategy.resolve_exit_levels(cfg, setup=setup, entry_price=float(setup.signal_close))
    assert levels["sl_price"] and abs(levels["sl_price"] - setup.sl_price) < 1e-9
    return "scanner dan mesin exit memakai level VIDYA yang sama"


def cek_anti_lookahead() -> str:
    bars = seri_flip_naik()
    cfg = config_dasar()
    series = vidya.compute_series(bars, cfg)
    idx = next(i for i in range(len(bars)) if series["cross_up"][i] and (series["delta_ratio"][i] or -1) >= 0)
    sebelum = entry_rule.evaluate_entry_rule(bars, cfg, int(bars[idx - 1].close_time))
    assert not sebelum["ok"]
    tepat = entry_rule.evaluate_entry_rule(bars, cfg, int(bars[idx].close_time))
    assert tepat["ok"] and tepat["signal_close_time"] == int(bars[idx].close_time)
    return "candle setelah waktu evaluasi tidak ikut dihitung (anti lookahead)"


def cek_pullback_state_machine() -> str:
    p = vidya.vidya_params({
        "VIDYA_ENTRY_TRIGGER": "PULLBACK", "VIDYA_PULLBACK_MAX_BARS": 5,
        "VIDYA_PULLBACK_TOUCH_ATR": 0.25, "VIDYA_PULLBACK_TARGET": "VIDYA_LINE",
        "VIDYA_PULLBACK_REQUIRE_BULLISH": True,
    })
    n = 8
    closes = [99.0, 99.0, 103.0, 102.0, 100.5, 101.0, 102.0, 103.0]
    opens = [99.0, 99.0, 100.0, 103.0, 102.0, 100.0, 101.0, 102.0]
    lows = [98.0, 98.0, 100.0, 101.5, 99.9, 100.0, 101.0, 102.0]
    line = [100.0] * n
    atr = [1.0] * n
    lower = [98.0] * n
    trend = [False, False, True, True, True, True, True, True]
    cu = [False, False, True, False, False, False, False, False]
    cd = [False] * n
    out = vidya._pullback_entries(closes, opens, lows, line, atr, lower, trend, cu, cd, p)
    assert out[5] and not out[2] and not out[4] and sum(out) == 1, out
    out2 = vidya._pullback_entries(closes, opens, lows, line, atr, lower, trend, cu, cd, dict(p, pullback_max_bars=2))
    assert not any(out2), out2
    cd2 = list(cd)
    cd2[4] = True
    trend2 = list(trend)
    trend2[4] = False
    out3 = vidya._pullback_entries(closes, opens, lows, line, atr, lower, trend2, cu, cd2, p)
    assert not any(out3), out3
    return "konfirmasi pullback menyala setelah sentuh garis VIDYA lalu reclaim bullish, batal saat kedaluwarsa atau flip turun"


def cek_pullback_signal_at() -> str:
    cfg = config_dasar()
    cfg["VIDYA_ENTRY_TRIGGER"] = "PULLBACK"
    n = 6
    series = {
        "ready": [True] * n, "cross_up": [False, False, True, False, False, False],
        "pullback_entry": [False, False, False, False, False, True],
        "trend_up": [False, False, True, True, True, True],
        "delta_ratio": [10.0] * n, "delta": [1.0] * n, "vidya": [100.0] * n,
        "upper": [102.0] * n, "lower": [98.0] * n, "atr": [1.0] * n,
    }
    sig_flip = vidya.signal_at(series, 2, cfg)
    assert not sig_flip["ok"] and sig_flip["stage"] == entry_rule.STAGE_VIDYA_TREND, sig_flip
    sig_ok = vidya.signal_at(series, 5, cfg)
    assert sig_ok["ok"] and sig_ok["entry_trigger"] == "PULLBACK", sig_ok
    cfg_flip = config_dasar()
    cfg_flip["VIDYA_ENTRY_TRIGGER"] = "FLIP"
    assert vidya.signal_at(series, 2, cfg_flip)["ok"]
    return "mode PULLBACK menolak bar flip dan menerima bar reclaim, mode FLIP tetap menerima bar flip"


def cek_pullback_tidak_di_bar_flip() -> str:
    cfg = config_dasar()
    cfg["VIDYA_ENTRY_TRIGGER"] = "PULLBACK"
    bars = seri_flip_naik()
    series = vidya.compute_series(bars, cfg)
    idx = next(i for i in range(len(bars)) if series["cross_up"][i])
    assert not series["pullback_entry"][idx]
    assert not vidya.signal_at(series, idx, cfg)["ok"]
    for i, fired in enumerate(series["pullback_entry"]):
        if fired:
            assert series["trend_up"][i] and i > idx
    return "pada seri nyata, konfirmasi pullback tidak pernah jatuh tepat di bar flip naik"


CHECKS = (
    cek_math_vidya, cek_sinyal_lolos, cek_delta_ditolak, cek_tanpa_flip_ditolak,
    cek_riwayat_kurang, cek_interval_scalping, cek_scanner_sinkron,
    cek_anti_lookahead, cek_pullback_state_machine,
    cek_pullback_signal_at, cek_pullback_tidak_di_bar_flip,
)


def run(verbose: bool = False) -> tuple[bool, list[str]]:
    messages = []
    try:
        for check in CHECKS:
            message = check()
            messages.append(message)
            if verbose:
                print(f"[OK] {message}")
        return True, messages
    except Exception as exc:
        messages.append(str(exc))
        if verbose:
            print(f"[GAGAL] {exc}")
        return False, messages


def main() -> int:
    for check in CHECKS:
        print(f"[OK] {check()}")
    print(f"SELFTEST VIDYA OK: {len(CHECKS)} pemeriksaan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
