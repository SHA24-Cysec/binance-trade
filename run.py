from __future__ import annotations

import sys


def _safe_console() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main() -> int:
    _safe_console()
    from web.dashboard import main as dashboard_main
    return dashboard_main(auto_start_bot=False)


if __name__ == "__main__":
    raise SystemExit(main())
