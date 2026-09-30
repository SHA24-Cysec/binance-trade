"""Entry point kompatibilitas untuk ``trading.pump_scanner_bot``."""

from importlib import import_module
import sys

_module = import_module("trading.pump_scanner_bot")

if __name__ == "__main__":
    raise SystemExit(_module.main())

sys.modules[__name__] = _module
