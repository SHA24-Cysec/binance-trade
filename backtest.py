"""Entry point kompatibilitas untuk ``backtesting.backtest``."""

from importlib import import_module
import sys

_module = import_module("backtesting.backtest")

if __name__ == "__main__":
    raise SystemExit(_module.main())

sys.modules[__name__] = _module
