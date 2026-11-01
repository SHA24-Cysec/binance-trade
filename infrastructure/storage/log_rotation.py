from __future__ import annotations

import logging.handlers
import os
import sys
import time

from infrastructure.storage.atomic_io import (
    ensure_private_directory,
    harden_runtime_file,
    replace_with_retry,
)

_NOTICE_INTERVAL_SECONDS = 60.0


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):

    def __init__(self, *args, **kwargs) -> None:
        filename = kwargs.get("filename", args[0] if args else None)
        if filename is not None:
            ensure_private_directory(os.path.dirname(os.fspath(filename)) or ".")
        super().__init__(*args, **kwargs)
        harden_runtime_file(self.baseFilename)
        self.last_rotation_error: str | None = None
        self._last_notice_at = 0.0

    def _open(self):
        stream = super()._open()
        harden_runtime_file(self.baseFilename)
        return stream

    def rotate(self, source: str, dest: str) -> None:
        if callable(self.rotator):
            self.rotator(source, dest)
            return
        if os.path.exists(source):
            replace_with_retry(source, dest)

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError as exc:
            self.last_rotation_error = f"{type(exc).__name__}: {exc}"
            if self.stream is None and not self.delay:
                self.stream = self._open()
            self._notify(exc)
            return
        self.last_rotation_error = None

    def _notify(self, exc: OSError) -> None:
        now = time.monotonic()
        if now - self._last_notice_at < _NOTICE_INTERVAL_SECONDS:
            return
        self._last_notice_at = now
        stream = getattr(sys, "stderr", None)
        if stream is None:
            return
        try:
            stream.write(
                f"Rotasi log {self.baseFilename} ditunda karena file sedang dipakai "
                f"proses lain ({exc}). Log tetap ditulis ke file aktif.\n"
            )
            stream.flush()
        except (OSError, ValueError):
            pass
