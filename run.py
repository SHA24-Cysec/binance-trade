#!/usr/bin/env python3
"""Peluncur satu perintah untuk dashboard dan bot terkelola.

Dashboard berjalan di proses utama dan tetap hidup ketika bot Stop atau
Restart. Bot adalah child process dashboard. Menjalankan dashboard.py atau
pump_scanner_bot.py secara terpisah tetap didukung.

Bot TIDAK dijalankan otomatis saat peluncur ini dipakai. Setelah dashboard
hidup, status bot adalah STOPPED dan bot baru berjalan ketika pengguna
menekan tombol Start di dashboard (alur /api/control/prepare lalu
/api/control/execute dengan action START).
"""

from __future__ import annotations

import sys


def _safe_console() -> None:
    """Hindari UnicodeEncodeError pada console Windows lama."""
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
    from dashboard import main as dashboard_main
    return dashboard_main(auto_start_bot=False)


if __name__ == "__main__":
    raise SystemExit(main())
