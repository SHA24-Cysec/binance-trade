from __future__ import annotations

from copy import deepcopy

from backtesting import backtest as bt
from backtesting import synthetic_data as sd
from config.config import PUMP_CONFIG
from strategy import entry_rule, exit_atr


def config_dasar() -> dict:
    cfg = deepcopy(PUMP_CONFIG)
    cfg.update({"CONFIRM_INTERVAL": "5m", "GAINER_VOLUME_FILTER_ENABLED": False, "GAINER_MIN_CHANGE_PCT": 0.0, "MIN_QUOTE_VOLUME_USDT_24H": 0.0, "ENTRY_MIN_RR_RATIO": 3.0, "ENTRY_SL_ATR_BUFFER_MULT": 0.1, "EXIT_ATR_MAX_SL_PCT": 10.0, "USE_RISK_PERCENT": False, "POSITION_SIZE_USDT": 100.0, "MAX_POSITION_USDT": 100.0, "BACKTEST_INITIAL_EQUITY_USDT": 1000.0, "COOLDOWN_MINUTES_AFTER_CLOSE": 0, "MIN_SECONDS_BETWEEN_TRADES": 0})
    return cfg


def main() -> int:
    checks: list[tuple[str, bool, str]] = []
    cfg = config_dasar()
    plan = entry_rule.exit_levels(105.0, 100.0, cfg, atr=2.0)
    checks.append(("level awal memakai low pola dan buffer ATR", plan["ok"] and abs(plan["sl_price"] - 99.8) < 1e-12, str(plan)))
    checks.append(("target awal memakai R:R 1:3", plan["ok"] and abs(plan["tp_price"] - 120.6) < 1e-12, str(plan)))

    state = {"entry_price": 105.0, "initial_sl_price": plan["sl_price"], "sl_price": plan["sl_price"], "tp_price": plan["tp_price"], "peak_price": 105.0, "atr_at_entry": 2.0}
    first = exit_atr.advance(state, 110.3, cfg, atr=2.0)
    checks.append(("breakeven mengangkat stop tanpa menurunkannya", first["be_active"] and first["sl_price"] >= 105.0, str(first)))
    state.update(first)
    second = exit_atr.advance(state, 113.0, cfg, atr=2.0)
    checks.append(("trailing aktif setelah harga bergerak cukup jauh", second["trail_active"] and second["sl_price"] >= first["sl_price"], str(second)))
    state.update(second)
    third = exit_atr.advance(state, 109.0, cfg, atr=2.0)
    checks.append(("trailing tidak turun saat harga retrace", third["sl_price"] >= second["sl_price"], str(third)))

    result = bt.run_backtest(sd.seri_banyak_setup(siklus=4), cfg, warmup_bars=120)
    checks.append(("jalur backtest menghasilkan posisi dari sinyal baru", bool(result.trades), f"trades={len(result.trades)}"))
    checks.append(("trade backtest menyimpan rasio level 1:3", bool(result.trades) and all(abs(t.tp_pct / t.sl_pct - 3.0) < 0.05 for t in result.trades if t.sl_pct > 0), f"trades={len(result.trades)}"))

    print("Selftest paritas exit pola, breakeven, dan trailing:")
    passed = 0
    for name, ok, detail in checks:
        print(f"  [{'LULUS' if ok else 'GAGAL'}] {name}" + ("" if ok else f" ({detail})"))
        passed += int(ok)
    print(f"HASIL: {passed}/{len(checks)} lulus")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
