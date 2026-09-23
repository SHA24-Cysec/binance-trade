"""
Persistensi state bot ke file JSON, supaya kalau bot restart (crash, reboot
VPS, dsb) basket grid yang sedang berjalan, level breakeven/trailing, dan
tracking equity harian tidak hilang.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone

logger = logging.getLogger("state")

DEFAULT_STATE = {
    "layers": [],                 # list of {"price": float, "qty": float, "time": int}
    "avg_price": 0.0,
    "total_qty": 0.0,
    "be_active": False,
    "be_stop_price": 0.0,
    "trailing_active": False,
    "trailing_stop_price": 0.0,
    "last_order_time": 0,
    "last_signal_bar_time": 0,
    "cooldown_until": 0,
    "day_start_equity": None,
    "day_start_date": None,
    "peak_equity": None,
    "dd_stopped": False,
    "dd_stop_until": 0,
    "daily_stopped": False,
}


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        logger.info("File state %s tidak ditemukan, mulai dari state kosong.", path)
        return dict(DEFAULT_STATE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_STATE)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Gagal membaca %s (%s). Membuat state baru dari default.", path, exc)
        return dict(DEFAULT_STATE)


def save_state(path: str, state: dict) -> None:
    """Tulis atomik: tulis ke file sementara dulu baru rename, supaya file
    state tidak pernah setengah-tertulis kalau proses mati di tengah jalan."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    shutil.move(tmp_path, path)


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_ms() -> int:
    return int(time.time() * 1000)
