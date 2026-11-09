from __future__ import annotations

from copy import deepcopy

from backtesting import backtest as bt
from backtesting import parity
from backtesting import synthetic_data as sd

BAR_MS_5M = 300_000
from config.config import PUMP_CONFIG
from strategy import entry_rule, exit_atr


def config_dasar() -> dict:
    cfg = deepcopy(PUMP_CONFIG)
    cfg.update({"GAINER_VOLUME_FILTER_ENABLED": False, "GAINER_MIN_CHANGE_PCT": 0.0, "MIN_QUOTE_VOLUME_USDT_24H": 0.0, "ENTRY_MIN_RR_RATIO": 3.0, "ENTRY_SL_ATR_BUFFER_MULT": 0.1, "EXIT_ATR_MAX_SL_PCT": 10.0, "CONFIRM_INTERVAL": "5m", "USE_RISK_PERCENT": False, "POSITION_SIZE_USDT": 100.0, "MAX_POSITION_USDT": 100.0, "BACKTEST_INITIAL_EQUITY_USDT": 1000.0, "COOLDOWN_MINUTES_AFTER_CLOSE": 0, "MIN_SECONDS_BETWEEN_TRADES": 0})
    return cfg


def _posisi(levels: dict) -> parity.PositionState:
    data = {"source": "ENGULFING_LOW_ATR_BUFFER", "sl_pct": 2.0, "tp_pct": 8.0, "sl_price": 98.0, "tp_price": 108.0}
    data.update(levels)
    return parity.PositionState(100.0, data)


def _candle(high: float, low: float, close: float, detik: int = 0, open_price: float | None = None) -> parity.Kline:
    return parity.Kline(open_time=BAR_MS_5M * detik, open=close if open_price is None else open_price, high=high, low=low, close=close, volume=1.0, close_time=BAR_MS_5M * (detik + 1) - 1)


def run(verbose: bool = False) -> tuple[bool, list[str]]:
    messages: list[str] = []
    tests = []

    def test(name, fn):
        tests.append((name, fn))

    def level_pola():
        cfg = config_dasar()
        plan = entry_rule.exit_levels(105.0, 100.0, cfg, atr=2.0)
        assert plan["ok"], plan
        assert abs(plan["sl_price"] - 99.8) < 1e-12
        assert abs(plan["tp_price"] - 120.6) < 1e-12
        assert abs(plan["rr_ratio"] - 3.0) < 1e-12

    def batas_stop():
        cfg = config_dasar()
        cfg["EXIT_ATR_MAX_SL_PCT"] = 1.0
        plan = entry_rule.exit_levels(105.0, 100.0, cfg, atr=2.0)
        assert not plan["ok"] and "melampaui" in plan["reason"]

    def breakeven():
        cfg = config_dasar()
        state = {"entry_price": 100.0, "initial_sl_price": 98.0, "sl_price": 98.0, "tp_price": 106.0, "peak_price": 100.0, "atr_at_entry": 1.0}
        result = exit_atr.advance(state, 102.1, cfg, atr=1.0)
        assert result["be_active"] and result["sl_price"] >= 100.0

    def trailing_tidak_turun():
        cfg = config_dasar()
        state = {"entry_price": 100.0, "initial_sl_price": 98.0, "sl_price": 101.0, "tp_price": 120.0, "peak_price": 104.0, "trail_active": True, "trail_stop_price": 102.0, "atr_at_entry": 1.0}
        result = exit_atr.advance(state, 103.1, cfg, atr=1.0)
        assert result["sl_price"] >= 101.0

    def target():
        assert entry_rule.take_profit_due(100.0, 115.0, 115.0)
        assert not entry_rule.take_profit_due(100.0, 115.0, 114.99)

    def backtest_trade():
        cfg = config_dasar()
        result = bt.run_backtest(sd.seri_banyak_setup(siklus=4), cfg, warmup_bars=120)
        assert result.trades, "fixture engulfing tidak menghasilkan trade"
        for trade in result.trades:
            assert trade.sl_pct > 0.0 and trade.tp_pct > 0.0
            assert abs(trade.tp_pct / trade.sl_pct - 3.0) < 0.05

    test("SL low pola dengan buffer ATR dan TP R:R 1:3", level_pola)
    test("batas jarak stop pola", batas_stop)
    test("breakeven ATR tetap aktif", breakeven)
    test("trailing stop tidak pernah turun", trailing_tidak_turun)
    test("pemicu take profit", target)
    test("integrasi backtest engulfing", backtest_trade)

    ok = True
    for name, fn in tests:
        try:
            fn()
            messages.append(f"LULUS: {name}")
            if verbose:
                print(f"  [LULUS] {name}")
        except Exception as exc:
            ok = False
            messages.append(f"GAGAL: {name}: {exc}")
            if verbose:
                print(f"  [GAGAL] {name}: {exc}")
    return ok, messages


def main() -> int:
    print("Selftest exit pola + ATR:")
    ok, _ = run(verbose=True)
    print("HASIL: SEMUA LULUS" if ok else "HASIL: ADA YANG GAGAL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
