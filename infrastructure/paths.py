"""Path terpusat untuk file aplikasi dan file runtime.

Kode aplikasi dikelompokkan langsung di folder domain repository, sedangkan
artefak runtime dipisah ke dua folder khusus supaya akar repository bersih:

- ``logs/`` : semua file log (log bot per mode dan audit perubahan settings).
- ``data/`` : semua file state dan konfigurasi runtime (settings gabungan,
  state posisi, kontrol, lock proses, akun PAPER, ledger rate limit,
  watchlist otomatis, dan cache backtest).

Modul ini adalah SATU-SATUNYA tempat yang menentukan lokasi folder tersebut.
Dengan begitu perilaku tidak bergantung pada current working directory maupun
lokasi file modul yang sudah dikelompokkan.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

LOGS_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"


def ensure_runtime_dirs() -> None:
    """Pastikan folder runtime logs/ dan data/ ada.

    Idempoten dan aman dipanggil berulang kali dari proses mana pun. Fungsi
    tulis yang memakai ``atomic_write_json``/``atomic_write_text`` sudah membuat
    folder induk secara otomatis, jadi fungsi ini hanya wajib dipanggil oleh
    penulis file yang memakai ``open``/handler biasa (misalnya RotatingFileHandler).
    """
    for folder in (LOGS_DIR, DATA_DIR):
        folder.mkdir(parents=True, exist_ok=True)
