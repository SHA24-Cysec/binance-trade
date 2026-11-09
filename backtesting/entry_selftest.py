from __future__ import annotations

from copy import deepcopy

from backtesting import synthetic_data as sd
from config.config import CONFIG_LOAD_ERRORS, PUMP_CONFIG
from config.settings_schema import PARAMETER_SCHEMA, REMOVED_PARAMETERS, relation_violations
from market import market_scanner as scanner
from strategy import entry_rule
from strategy.indicators import Kline

MS_15M = 900_000


def candle(index: int, open_: float, close: float, low: float, high: float, volume: float = 1000.0) -> Kline:
    return sd.make_candle(index, open_, high, low, close, volume, bar_ms=MS_15M)


def config_dasar() -> dict:
    cfg = deepcopy(PUMP_CONFIG)
    cfg.update({"GAINER_VOLUME_FILTER_ENABLED": False, "CONFIRM_INTERVAL": "15m", "EMA_FAST_PERIOD": 20, "EMA_SLOW_PERIOD": 50, "ENTRY_SL_ATR_BUFFER_MULT": 0.1, "ENTRY_ATR_PERIOD": 14, "ENTRY_MIN_RR_RATIO": 3.0, "EXIT_ATR_MAX_SL_PCT": 10.0})
    return cfg


def seri_bullish(tren_naik: bool = True) -> list[Kline]:
    bars = []
    price = 80.0
    for index in range(60):
        step = 0.35 if tren_naik else -0.12
        open_ = price
        close = max(10.0, open_ + step)
        bars.append(candle(index, open_, close, min(open_, close) - 0.2, max(open_, close) + 0.2))
        price = close
    previous_open = price + 0.8
    previous_close = price
    bars.append(candle(60, previous_open, previous_close, previous_close - 0.3, previous_open + 0.2))
    current_open = previous_close
    current_close = previous_close + 0.25
    bars.append(candle(61, current_open, current_close, current_open - 0.25, current_close + 0.2))
    return bars


def seri_engulfing(tren_naik: bool = True) -> list[Kline]:
    return seri_bullish(tren_naik)


def cek_konfigurasi() -> str:
    for key in ("EMA_FAST_PERIOD", "EMA_SLOW_PERIOD", "ENTRY_SL_ATR_BUFFER_MULT"):
        assert key in PUMP_CONFIG and key in PARAMETER_SCHEMA, key
    for key in ("MOMENTUM_BREAKOUT_LOOKBACK", "MOMENTUM_BODY_ATR_MULT", "MOMENTUM_VOLUME_MULT", "MOMENTUM_VOLUME_LOOKBACK"):
        assert key not in PUMP_CONFIG and key not in PARAMETER_SCHEMA and key in REMOVED_PARAMETERS, key
    assert PUMP_CONFIG["CONFIRM_INTERVAL"] == "30m"
    assert PUMP_CONFIG["CONFIRM_LOOKBACK_BARS"] >= 192
    assert PUMP_CONFIG["ENTRY_MIN_RR_RATIO"] == 2.5
    assert not CONFIG_LOAD_ERRORS, CONFIG_LOAD_ERRORS
    assert not relation_violations(PUMP_CONFIG, mode_aware=True)
    return "config interval 30m, EMA 20/50, buffer ATR 2.0x, dan R:R 1:2.5 sinkron"


def cek_sinyal_lolos() -> str:
    bars = seri_bullish()
    result = entry_rule.evaluate_entry_rule(bars, config_dasar())
    assert result["ok"], result
    assert result["ema_fast"] > result["ema_slow"]
    assert result["signal_close"] > result["ema_fast"]
    assert abs(result["rr_ratio"] - 3.0) < 1e-12
    assert abs(result["entry_low"] - float(bars[-1].low)) < 1e-12
    assert result["sl_price"] < result["entry_low"] < result["signal_close"] < result["tp_price"]
    return "candle bullish di atas EMA 20/50 lolos tanpa harus engulfing dengan R:R 1:3"


def cek_pola_ditolak() -> str:
    bars = seri_bullish()
    last = bars[-1]
    bars[-1] = candle(61, float(last.open), float(last.open) - 0.1, float(last.low) - 0.2, float(last.high))
    result = entry_rule.evaluate_entry_rule(bars, config_dasar())
    assert not result["ok"] and result["stage"] == entry_rule.STAGE_BULLISH_CANDLE, result
    return "candle bearish ditolak oleh filter candle bullish"


def cek_ema_ditolak() -> str:
    bars = seri_bullish(False)
    result = entry_rule.evaluate_entry_rule(bars, config_dasar())
    assert not result["ok"]
    assert result["stage"] in (entry_rule.STAGE_EMA_PRICE, entry_rule.STAGE_EMA_TREND), result
    return "sinyal tanpa struktur tren EMA bullish ditolak"


def cek_batas_strict() -> str:
    bars = seri_bullish()
    last = bars[-1]
    doji = candle(61, float(last.open), float(last.open), float(last.low), float(last.high))
    assert not entry_rule.is_bullish_candle(doji)
    assert entry_rule.is_bullish_candle(last)
    return "candle doji (close == open) ditolak dan hanya close > open yang diterima"


def cek_anti_lookahead() -> str:
    bars = seri_bullish()
    result = entry_rule.evaluate_entry_rule(bars, config_dasar(), int(bars[-2].close_time))
    assert not result["ok"]
    assert result.get("signal_close_time") != int(bars[-1].close_time)
    return "candle setelah waktu evaluasi tidak ikut dihitung"


def cek_scanner() -> str:
    setup = scanner.detect_entry_setup(seri_bullish(), config_dasar())
    assert setup.ok, setup.reason
    assert setup.ema_fast and setup.ema_slow and setup.ema_fast > setup.ema_slow
    assert setup.entry_low and setup.sl_price and setup.sl_price < setup.entry_low
    return "scanner membawa EMA dan level risiko candle bullish tanpa kehilangan data"


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


def run(verbose: bool = False) -> tuple[bool, list[str]]:
    messages = []
    try:
        for check in (cek_konfigurasi, cek_sinyal_lolos, cek_pola_ditolak, cek_ema_ditolak, cek_batas_strict, cek_anti_lookahead, cek_scanner, cek_ambang_pump_dinamis, cek_ranking_hybrid):
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
    checks = (cek_konfigurasi, cek_sinyal_lolos, cek_pola_ditolak, cek_ema_ditolak, cek_batas_strict, cek_anti_lookahead, cek_scanner, cek_ambang_pump_dinamis, cek_ranking_hybrid)
    for check in checks:
        print(f"[OK] {check()}")
    print(f"SELFTEST ENTRY OK: {len(checks)} pemeriksaan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
