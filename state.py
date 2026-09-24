"""
Persistensi state bot ke file JSON, supaya kalau bot restart (crash, reboot
VPS, dsb) posisi yang sedang berjalan, level breakeven/trailing, dan
tracking equity harian tidak hilang.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from atomic_io import archive_corrupt, atomic_write_json

logger = logging.getLogger("state")

# Dirampingkan pada audit 2026-09-24 (temuan R-02): kunci sisa bot grid
# martingale lama ("layers", "avg_price", "total_qty", "last_signal_bar_time")
# dan "last_order_time" tidak dibaca satu baris pun di kode aktif, jadi
# dihapus supaya tidak menyesatkan. Skema posisi lengkap milik pump scanner
# ada di DEFAULT_STATE pump_scanner_bot.py (yang digabung di atas hasil
# load_state di sini); dashboard membaca file state dengan .get() yang aman,
# jadi kunci yang hilang tidak merusak apa pun.
DEFAULT_STATE = {
    "be_active": False,
    "be_stop_price": 0.0,
    "trailing_active": False,
    "trailing_stop_price": 0.0,
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
    except json.JSONDecodeError as exc:
        try:
            backup = archive_corrupt(path)
        except OSError as backup_exc:
            backup = None
            logger.error("State %s rusak dan gagal diarsipkan: %s", path, backup_exc)
        logger.error("Gagal membaca %s (%s). Cadangan: %s. Membuat state default.",
                     path, exc, backup)
        return dict(DEFAULT_STATE)
    except OSError as exc:
        logger.error("Gagal membaca %s (%s). Membuat state baru dari default.", path, exc)
        return dict(DEFAULT_STATE)


def save_state(path: str, state: dict) -> None:
    """Tulis JSON atomik dengan retry sharing violation Windows."""
    atomic_write_json(path, state)


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------
# "Control file" -- jalur sinyal SEARAH dari dashboard.py (proses lain)
# ke pump_scanner_bot.py, dipakai untuk perintah manual dari dashboard
# (mis. tombol "Jual Sekarang"). Dipisah dari file state utama supaya
# TIDAK ada risiko tabrakan tulis dengan state.json yang aktif ditulis
# terus-menerus oleh loop utama bot -- dashboard hanya menulis file kecil
# ini, bot yang membaca & memprosesnya, lalu menghapusnya.
# ---------------------------------------------------------------------

def load_control(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as exc:
        try:
            backup = archive_corrupt(path)
            logger.error("File kontrol %s rusak (%s), diarsipkan ke %s.", path, exc, backup)
        except OSError as backup_exc:
            logger.error("File kontrol %s rusak dan gagal diarsipkan: %s", path, backup_exc)
        return {}
    except OSError:
        return {}


def save_control(path: str, data: dict) -> None:
    """Tulis kontrol atomik dengan temporary unik dan retry Windows."""
    atomic_write_json(path, data)


def clear_control(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def get_stop_control_file(control_path: str) -> str:
    """Path khusus perintah STOP, terpisah dari CLOSE_POSITION.

    Pemisahan mencegah klik Stop menimpa perintah Jual Sekarang yang sedang
    menunggu diproses bot. Keduanya tetap memakai kanal file kontrol state.py.
    """
    path = Path(control_path)
    return str(path.with_name(f"{path.stem}.stop{path.suffix or '.json'}"))


def request_stop(control_path: str, *, requested_by: str = "dashboard") -> str:
    stop_path = get_stop_control_file(control_path)
    atomic_write_json(stop_path, {
        "action": "STOP_BOT",
        "requested_at": now_ms(),
        "requested_by": str(requested_by)[:80],
    })
    return stop_path


def clear_stop_request(control_path: str) -> None:
    clear_control(get_stop_control_file(control_path))


def consume_stop_request(control_path: str, max_age_seconds: int = 300) -> bool:
    """Ambil dan hapus permintaan stop satu kali.

    Permintaan stale dibuang agar bot yang dinyalakan berjam-jam kemudian
    tidak langsung berhenti karena file lama.
    """
    stop_path = get_stop_control_file(control_path)
    cmd = load_control(stop_path)
    if not cmd:
        return False
    clear_control(stop_path)
    if cmd.get("action") != "STOP_BOT":
        logger.warning("Perintah stop tidak dikenal di %s: %s", stop_path, cmd)
        return False
    try:
        requested_at = int(cmd.get("requested_at", 0) or 0)
    except (TypeError, ValueError):
        return False
    age = (now_ms() - requested_at) / 1000.0
    if requested_at <= 0 or age < -30 or age > max_age_seconds:
        logger.warning("Perintah stop diabaikan karena stale/tidak valid (umur %.1f detik).", age)
        return False
    return True

