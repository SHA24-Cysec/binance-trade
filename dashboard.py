"""Entry point kompatibilitas untuk ``web.dashboard``."""

from importlib import import_module
import sys

_module = import_module("web.dashboard")

if __name__ == "__main__":
    raise SystemExit(_module.main(auto_start_bot=False))

sys.modules[__name__] = _module
