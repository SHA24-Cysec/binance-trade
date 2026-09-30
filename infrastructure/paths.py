"""Path terpusat untuk file aplikasi dan file runtime.

Kode aplikasi dikelompokkan langsung di folder domain repository, sedangkan
konfigurasi, template, dan artefak runtime tetap berakar di folder repository.
Dengan begitu perilaku tidak bergantung pada current working directory maupun
lokasi file modul yang sudah dikelompokkan.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent


def project_path(path: str | Path) -> Path:
    """Kembalikan path relatif terhadap root repository sebagai path absolut."""
    value = Path(path)
    return value if value.is_absolute() else PROJECT_ROOT / value
