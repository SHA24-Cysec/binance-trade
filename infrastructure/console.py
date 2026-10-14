from __future__ import annotations

import sys

MIN_PYTHON = (3, 10)


def configure_utf8_console() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def python_version_error() -> str | None:
    if sys.version_info >= MIN_PYTHON:
        return None
    current = ".".join(str(part) for part in sys.version_info[:3])
    minimum = ".".join(str(part) for part in MIN_PYTHON)
    return (
        f"Python {minimum} atau lebih baru dibutuhkan, versi yang dipakai {current} "
        f"({sys.executable}). Pasang Python terbaru dari python.org atau "
        "manajer paket distro, lalu buat ulang virtual environment."
    )
