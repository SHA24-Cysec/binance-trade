from __future__ import annotations

from copy import deepcopy

from backtesting import synthetic_data as sd
from config.config import CONFIG_LOAD_ERRORS, PUMP_CONFIG
from config.settings_schema import PARAMETER_SCHEMA, REMOVED_PARAMETERS, relation_violations
from market import market_scanner as scanner
from strategy import entry_rule
from strategy import indicators as strategy
from strategy.indicators import Kline

SINYAL_INDEX = 960 + 19


def config_dasar() -> dict:
    cfg = deepcopy(PUMP_CONFIG)
    cfg.update({"GAINER_VOLUME_FILTER_ENABLED": False, "CONFIRM_INTERVAL": "15m", "CONFIRM_LOOKBACK_BARS": 1000, "DEMAND_EMA_PERIOD": 50, "DEMAND_IMPULSE_ATR_MULT": 1.5, "ENTRY_SL_ATR_BUFFER_MULT": 0.5, "ENTRY_ATR_PERIOD": 14, "ENTRY_MIN_RR_RATIO": 3.0, "EXIT_ATR_MAX_SL_PCT": 10.0})
    return cfg


def seri_sinyal() -> list[Kline]:
    bars = sd.seri_zona_demand(siklus=1)
    return bars[: SINYAL_INDEX + 1]


def ganti_terakhir(bars: list[Kline], open_: float, close: float, low: float, high: float) -> list[Kline]:
    last = bars[-1]
    out = list(bars)
    out[-1] = Kline(open_time=last.open_time, open=open_, high=high, low=low, close=close, close_time=last.close_time, volume=last.volume, quote_volume=last.quote_volume)
    return out


def cek_konfigurasi() -> str:
    for key in ("DEMAND_EMA_PERIOD", "DEMAND_IMPULSE_ATR_MULT", "ENTRY_SL_ATR_BUFFER_MULT"):
        assert key in PUMP_CONFIG and key in PARAMETER_SCHEMA, key
    for key in ("EMA_FAST_PERIOD", "EMA_SLOW_PERIOD", "MOMENTUM_BREAKOUT_LOOKBACK", "MOMENTUM_BODY_ATR_MULT", "MOMENTUM_VOLUME_MULT", "MOMENTUM_VOLUME_LOOKBACK"):
        assert key not in PUMP_CONFIG and key not in PARAMETER_SCHEMA and key in REMOVED_PARAMETERS, key
    assert PUMP_CONFIG["CONFIRM_INTERVAL"] == "15m"
    assert PUMP_CONFIG["CONFIRM_LOOKBACK_BARS"] >= entry_rule.required_bars(PUMP_CONFIG)
    assert entry_rule.required_bars(PUMP_CONFIG) <= entry_rule.KLINE_FETCH_LIMIT
    assert not CONFIG_LOAD_ERRORS, CONFIG_LOAD_ERRORS
    assert not relation_violations(PUMP_CONFIG, mode_aware=True)
    salah = deepcopy(PUMP_CONFIG)
    salah["CONFIRM_INTERVAL"] = "1h"
    assert "CONFIRM_INTERVAL" in relation_violations(salah, mode_aware=True)
    return "config interval 15m, EMA tiga timeframe, zona demand, dan riwayat candle sinkron"


def cek_resample() -> str:
    bars = sd.seri_zona_demand(siklus=1)
    h1 = entry_rule.resample_closed(bars, 15, 60)
    h4 = entry_rule.resample_closed(bars, 15, 240)
    assert len(h1) == len(bars) // 4 and len(h4) == len(bars) // 16
    blok = bars[16:32]
    assert h4[1].open == blok[0].open and h4[1].close == blok[-1].close
    assert h4[1].high == max(b.high for b in blok) and h4[1].low == min(b.low for b in blok)
    assert h4[1].open_time == blok[0].open_time and h4[1].close_time == blok[-1].close_time
    potong = entry_rule.resample_closed(bars[3:37], 15, 240)
    assert len(potong) == 1 and potong[0].open_time == bars[16].open_time
    bolong = bars[:20] + bars[21:48]
    assert [b.open_time for b in entry_rule.resample_closed(bolong, 15, 240)] == [bars[0].open_time, bars[32].open_time]
    return "candle 15m digabung menjadi 1h dan 4h hanya dari blok yang lengkap dan sudah tertutup"


def cek_sinyal_lolos() -> str:
    bars = seri_sinyal()
    result = entry_rule.evaluate_entry_rule(bars, config_dasar())
    assert result["ok"], result
    assert result["zone_timeframe"] in ("1h", "4h")
    assert result["zone_low"] < result["zone_high"]
    assert float(bars[-1].low) <= result["zone_high"] and result["signal_close"] > result["zone_low"]
    assert result["signal_close"] > result["ema_m15"] and result["signal_close"] > result["ema_h1"] and result["signal_close"] > result["ema_h4"]
    assert abs(result["rr_ratio"] - 3.0) < 1e-12
    assert result["entry_low"] <= result["zone_low"]
    assert result["sl_price"] < result["entry_low"] < result["signal_close"] < result["tp_price"]
    assert all(result["checks"].values()), result["checks"]
    return "candle bullish 15m di zona demand 1h dengan close di atas EMA tiga timeframe lolos, SL di bawah zona"


def cek_pola_ditolak() -> str:
    bars = seri_sinyal()
    last = bars[-1]
    bars = ganti_terakhir(bars, float(last.open), float(last.open) * 0.999, float(last.low), float(last.high))
    result = entry_rule.evaluate_entry_rule(bars, config_dasar())
    assert not result["ok"] and result["stage"] == entry_rule.STAGE_BULLISH_CANDLE, result
    return "candle bearish di zona demand ditolak"


def cek_ema_ditolak() -> str:
    bars = seri_sinyal()
    last = bars[-1]
    low = float(last.low)
    bars = ganti_terakhir(bars, low * 1.0001, low * 1.0006, low, low * 1.0008)
    result = entry_rule.evaluate_entry_rule(bars, config_dasar())
    assert not result["ok"] and result["stage"] == entry_rule.STAGE_EMA_TREND, result
    return "candle bullish di zona demand tetapi close di bawah EMA ditolak"


def cek_zona_ditolak() -> str:
    bars = seri_sinyal()
    last = bars[-1]
    bars = ganti_terakhir(bars, float(last.open), float(last.close), float(last.open) * 0.9995, float(last.high))
    result = entry_rule.evaluate_entry_rule(bars, config_dasar())
    assert not result["ok"] and result["stage"] == entry_rule.STAGE_DEMAND_ZONE, result
    assert result["zones_active"] >= 1
    return "candle bullish di atas EMA yang tidak menyentuh zona demand ditolak"


def cek_zona_tembus() -> str:
    bars = seri_sinyal()
    acuan = entry_rule.evaluate_entry_rule(bars, config_dasar())
    sebelum = bars[-2]

    def tembus(harga: float) -> dict:
        salinan = list(bars)
        salinan[-2] = Kline(open_time=sebelum.open_time, open=float(sebelum.open), high=float(sebelum.high), low=harga * 0.999, close=harga, close_time=sebelum.close_time, volume=sebelum.volume, quote_volume=sebelum.quote_volume)
        return entry_rule.evaluate_entry_rule(salinan, config_dasar())

    sebagian = tembus(float(acuan["zone_low"]) * 0.9995)
    assert sebagian["zone_low"] is None or sebagian["zone_low"] < acuan["zone_low"], sebagian
    semua = tembus(float(acuan["zone_low"]) * 0.99)
    assert not semua["ok"] and semua["stage"] == entry_rule.STAGE_DEMAND_ZONE and semua["zones_active"] == 0, semua
    return "zona demand yang sudah ditembus close di bawahnya tidak dipakai lagi"


def cek_batas_strict() -> str:
    bars = seri_sinyal()
    last = bars[-1]
    doji = Kline(open_time=last.open_time, open=float(last.open), high=float(last.high), low=float(last.low), close=float(last.open), close_time=last.close_time)
    assert not entry_rule.is_bullish_candle(doji)
    assert entry_rule.is_bullish_candle(last)
    return "candle doji (close == open) ditolak dan hanya close > open yang diterima"


def cek_anti_lookahead() -> str:
    bars = sd.seri_zona_demand(siklus=1)
    result = entry_rule.evaluate_entry_rule(bars, config_dasar(), int(bars[SINYAL_INDEX - 1].close_time))
    assert not result["ok"]
    assert result.get("signal_close_time") == int(bars[SINYAL_INDEX - 1].close_time)
    tepat = entry_rule.evaluate_entry_rule(bars, config_dasar(), int(bars[SINYAL_INDEX].close_time))
    assert tepat["ok"] and tepat["signal_close_time"] == int(bars[SINYAL_INDEX].close_time)
    sebelum_impuls = entry_rule.evaluate_entry_rule(bars, config_dasar(), int(bars[sd.BAR_PER_H4_15M * 60 + 6].close_time))
    assert not sebelum_impuls["ok"] and sebelum_impuls["zone_low"] is None
    return "candle setelah waktu evaluasi dan candle 1h yang belum tertutup tidak ikut dihitung"


def cek_interval() -> str:
    cfg = config_dasar()
    cfg["CONFIRM_INTERVAL"] = "1h"
    result = entry_rule.evaluate_entry_rule(seri_sinyal(), cfg)
    assert not result["ok"] and result["stage"] == entry_rule.STAGE_HISTORY and "tidak didukung" in result["reason"]
    pendek = entry_rule.evaluate_entry_rule(seri_sinyal()[-200:], config_dasar())
    assert not pendek["ok"] and pendek["stage"] == entry_rule.STAGE_HISTORY
    return "interval di luar 15m dan 30m serta riwayat yang kurang ditolak dengan alasan jelas"


def cek_scanner() -> str:
    setup = scanner.detect_entry_setup(seri_sinyal(), config_dasar())
    assert setup.ok, setup.reason
    assert setup.ema_fast and setup.ema_slow
    assert setup.entry_low and setup.sl_price and setup.sl_price < setup.entry_low
    levels = strategy.resolve_exit_levels(config_dasar(), setup=setup, entry_price=float(setup.signal_close))
    assert levels["sl_price"] and abs(levels["sl_price"] - setup.sl_price) < 1e-9
    assert levels["source"] == entry_rule.SL_ANCHOR_ATR
    return "scanner membawa batas zona demand dan level risiko yang sama dengan mesin exit"


def cek_ambang_pump_dinamis() -> str:
    cfg = config_dasar()
    values = [float(value) for value in range(-10, 90)]
    result = scanner.dynamic_market_thresholds(values, cfg)
    assert not result["fallback"] and result["sample"] == 100
    assert -2.0 <= result["minimum"] <= 5.0
    assert 10.0 <= result["maximum"] <= 35.0
    fallback = scanner.dynamic_market_thresholds([1.0, 2.0], cfg)
    assert fallback["fallback"] and fallback["minimum"] == float(cfg["PUMP_FALLBACK_MIN_CHANGE_PCT"]) and fallback["maximum"] == float(cfg["PUMP_FALLBACK_MAX_CHANGE_PCT"])
    return "ambang pump dinamis memakai persentil terjepit dan fallback aman"


def cek_ranking_hybrid() -> str:
    cfg = config_dasar()
    cfg.update({"PUMP_DYNAMIC_MIN_SAMPLE": 5, "MIN_QUOTE_VOLUME_USDT_24H": 0.0})
    tickers = [{"symbol": f"COIN{i}USDT", "quoteVolume": str(1_000_000 * (i + 1)), "lastPrice": "1", "priceChangePercent": str(i * 2)} for i in range(8)]
    ranked = scanner.filter_and_rank_candidates(tickers, cfg)
    assert ranked and all(candidate.market_score >= 0.0 for candidate in ranked)
    assert ranked[0].market_score >= ranked[-1].market_score
    return "shortlist pasar diurutkan dengan skor hybrid perubahan dan turnover"


CHECKS = (cek_konfigurasi, cek_resample, cek_sinyal_lolos, cek_pola_ditolak, cek_ema_ditolak, cek_zona_ditolak, cek_zona_tembus, cek_batas_strict, cek_anti_lookahead, cek_interval, cek_scanner, cek_ambang_pump_dinamis, cek_ranking_hybrid)


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
    checks = CHECKS
    for check in checks:
        print(f"[OK] {check()}")
    print(f"SELFTEST ENTRY OK: {len(checks)} pemeriksaan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
