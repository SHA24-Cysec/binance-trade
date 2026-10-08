from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from infrastructure.storage.atomic_io import (
    archive_corrupt,
    atomic_write_json,
    interprocess_lock,
)

logger = logging.getLogger("state")

DEFAULT_STATE = {
    "be_active": False,
    "be_stop_price": 0.0,
    "trailing_active": False,
    "trailing_stop_price": 0.0,
    "cooldown_until": 0,
    "peak_equity": None,
    "dd_stopped": False,
    "dd_stop_until": 0,
}


def _load_state_checked_unlocked(path: str) -> tuple[dict, bool, str]:
    if not os.path.exists(path):
        logger.info("File state %s tidak ditemukan, mulai dari state kosong.", path)
        return dict(DEFAULT_STATE), True, ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("root state harus object JSON")
        merged = dict(DEFAULT_STATE)
        merged.update(data)
        return merged, True, ""
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        try:
            backup = archive_corrupt(path)
        except OSError as backup_exc:
            backup = None
            logger.error("State %s rusak dan gagal diarsipkan: %s", path, backup_exc)
        message = f"Gagal membaca state valid {path}: {exc}. Cadangan: {backup}"
        logger.error("%s. Membuat state default fail-closed.", message)
        return dict(DEFAULT_STATE), False, message
    except OSError as exc:
        message = f"Gagal membaca state {path}: {exc}"
        logger.error("%s. Membuat state default fail-closed.", message)
        return dict(DEFAULT_STATE), False, message


def load_state_checked(path: str) -> tuple[dict, bool, str]:
    with interprocess_lock(path):
        return _load_state_checked_unlocked(path)


def load_state(path: str) -> dict:
    state, _valid, _error = load_state_checked(path)
    return state


def save_state(path: str, state: dict) -> None:
    with interprocess_lock(path):
        atomic_write_json(path, state)


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_ms() -> int:
    return int(time.time() * 1000)


def _load_control_unlocked(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as exc:
        try:
            backup = archive_corrupt(path)
            logger.error(
                "File kontrol %s rusak (%s), diarsipkan ke %s.", path, exc, backup
            )
        except OSError as backup_exc:
            logger.error(
                "File kontrol %s rusak dan gagal diarsipkan: %s", path, backup_exc
            )
        return {}
    except OSError:
        return {}


def load_control(path: str) -> dict:
    with interprocess_lock(path):
        return _load_control_unlocked(path)


def save_control(path: str, data: dict) -> None:
    with interprocess_lock(path):
        atomic_write_json(path, data)


def _clear_control_unlocked(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def clear_control(path: str) -> None:
    with interprocess_lock(path):
        _clear_control_unlocked(path)


def get_stop_control_file(control_path: str) -> str:
    path = Path(control_path)
    return str(path.with_name(f"{path.stem}.stop{path.suffix or '.json'}"))


def request_stop(control_path: str, *, requested_by: str = "dashboard") -> str:
    stop_path = get_stop_control_file(control_path)
    with interprocess_lock(stop_path):
        atomic_write_json(
            stop_path,
            {
                "action": "STOP_BOT",
                "requested_at": now_ms(),
                "requested_by": str(requested_by)[:80],
            },
        )
    return stop_path


def clear_stop_request(control_path: str) -> None:
    clear_control(get_stop_control_file(control_path))


def consume_stop_request(control_path: str, max_age_seconds: int = 300) -> bool:
    stop_path = get_stop_control_file(control_path)
    with interprocess_lock(stop_path):
        cmd = _load_control_unlocked(stop_path)
        if not cmd:
            return False
        _clear_control_unlocked(stop_path)
        if cmd.get("action") != "STOP_BOT":
            logger.warning("Perintah stop tidak dikenal di %s: %s", stop_path, cmd)
            return False
        try:
            requested_at = int(cmd.get("requested_at", 0) or 0)
        except (TypeError, ValueError):
            return False
        age = (now_ms() - requested_at) / 1000.0
        if requested_at <= 0 or age < -30 or age > max_age_seconds:
            logger.warning(
                "Perintah stop diabaikan karena stale/tidak valid (umur %.1f detik).",
                age,
            )
            return False
        return True
