from __future__ import annotations

import sys


def main() -> int:
    from infrastructure.console import configure_utf8_console, python_version_error

    configure_utf8_console()
    masalah_versi = python_version_error()
    if masalah_versi:
        print(masalah_versi, file=sys.stderr)
        return 2
    from web.dashboard import main as dashboard_main

    return dashboard_main(auto_start_bot=False)


if __name__ == "__main__":
    raise SystemExit(main())
