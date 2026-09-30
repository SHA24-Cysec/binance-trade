"""Entry point kompatibilitas untuk ``backtesting.portfolio_backtest``."""

from importlib import import_module
import sys

_module = import_module("backtesting.portfolio_backtest")

if __name__ == "__main__":
    raise SystemExit(0 if _module.selftest() else 1)

sys.modules[__name__] = _module
