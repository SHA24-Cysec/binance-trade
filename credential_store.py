"""Penyimpanan kredensial Binance di .env tanpa membocorkan secret."""

from __future__ import annotations

import csv
import os
import re
import stat
import subprocess
from pathlib import Path

from atomic_io import atomic_write_text


ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
_KEYS = ("BINANCE_API_KEY", "BINANCE_API_SECRET")
_ASSIGN_RE = re.compile(r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>BINANCE_API_KEY|BINANCE_API_SECRET)\s*=.*$")


def _validate_secret_value(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} harus berupa teks.")
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        raise ValueError(f"{label} mengandung karakter baris yang tidak diizinkan.")
    if value and (value != value.strip() or any(ord(ch) < 33 or ord(ch) > 126 for ch in value)):
        raise ValueError(f"{label} hanya boleh berisi karakter ASCII tercetak tanpa spasi tepi.")
    if len(value) > 512:
        raise ValueError(f"{label} terlalu panjang.")
    return value


def _quote(value: str) -> str:
    # Format single-quoted didukung python-dotenv. Escape backslash lebih dulu.
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _dominant_newline(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def update_env(*, api_key: str | None = None, api_secret: str | None = None,
               path: os.PathLike | str = ENV_PATH) -> dict:
    """Perbarui dua kunci dan pertahankan semua baris lain serta newline.

    None berarti tidak diubah. String kosong adalah nilai sah untuk operasi
    hapus kredensial yang dilakukan secara eksplisit oleh endpoint.
    """
    target = Path(path)
    replacements: dict[str, str] = {}
    if api_key is not None:
        replacements["BINANCE_API_KEY"] = _validate_secret_value(api_key, "API key")
    if api_secret is not None:
        replacements["BINANCE_API_SECRET"] = _validate_secret_value(api_secret, "API secret")
    if not replacements:
        return permission_status(target)

    try:
        # newline="" mencegah universal-newline mengubah CRLF menjadi LF.
        with open(target, "r", encoding="utf-8", newline="") as handle:
            original = handle.read()
    except FileNotFoundError:
        original = ""
    newline = _dominant_newline(original)
    lines = original.splitlines(keepends=True)
    seen: set[str] = set()
    output: list[str] = []

    for line in lines:
        content = line[:-2] if line.endswith("\r\n") else (line[:-1] if line.endswith("\n") else line)
        ending = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
        match = _ASSIGN_RE.match(content)
        if match and match.group("key") in replacements:
            key = match.group("key")
            if key in seen:
                # Hapus duplikat agar tidak ada perbedaan tafsir first/last value.
                continue
            output.append(f"{match.group('prefix')}{key}={_quote(replacements[key])}{ending or newline}")
            seen.add(key)
        else:
            output.append(line)

    missing = [key for key in _KEYS if key in replacements and key not in seen]
    if missing:
        if output and not output[-1].endswith(("\n", "\r\n")):
            output[-1] += newline
        for key in missing:
            output.append(f"{key}={_quote(replacements[key])}{newline}")

    atomic_write_text(target, "".join(output), mode=0o600 if os.name == "posix" else None)
    return secure_permissions(target)


def read_credentials(path: os.PathLike | str = ENV_PATH) -> tuple[str, str]:
    """Parse .env memakai python-dotenv bila tersedia."""
    target = Path(path)
    try:
        from dotenv import dotenv_values
        values = dotenv_values(dotenv_path=target, encoding="utf-8")
        return str(values.get("BINANCE_API_KEY") or ""), str(values.get("BINANCE_API_SECRET") or "")
    except (ImportError, OSError, ValueError):
        return "", ""


def credential_status(path: os.PathLike | str = ENV_PATH) -> dict:
    key, secret = read_credentials(path)
    return {
        "api_key_set": bool(key),
        "api_secret_set": bool(secret),
        "api_key_last4": key[-4:] if key else None,
        "permissions": permission_status(path),
    }


def _windows_current_sid() -> str | None:  # pragma: no cover - Windows
    try:
        result = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, check=True,
        )
        row = next(csv.reader([result.stdout.strip()]))
        return row[1].strip() if len(row) >= 2 else None
    except Exception:
        return None


def secure_permissions(path: os.PathLike | str) -> dict:
    target = Path(path)
    if not target.exists():
        return {"ok": False, "platform": os.name, "message": "File .env belum ada."}
    if os.name == "posix":
        try:
            os.chmod(target, 0o600)
            mode = stat.S_IMODE(target.stat().st_mode)
            ok = mode == 0o600
            return {
                "ok": ok,
                "platform": "posix",
                "mode": oct(mode),
                "message": "Izin file 0600 terverifikasi." if ok else f"Izin aktual {oct(mode)}, bukan 0600.",
            }
        except OSError as exc:
            return {"ok": False, "platform": "posix", "message": f"Gagal mengatur izin: {exc}"}

    if os.name == "nt":  # pragma: no cover - Windows
        sid = _windows_current_sid()
        if not sid:
            return {
                "ok": False, "platform": "windows",
                "message": "SID pengguna tidak dapat diverifikasi. ACL .env belum diklaim aman.",
            }
        try:
            command = [
                "icacls.exe", str(target), "/inheritance:r",
                "/grant:r", f"*{sid}:(F)", "*S-1-5-18:(F)",
            ]
            result = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15, check=False,
            )
            if result.returncode != 0:
                msg = (result.stderr or result.stdout or "icacls gagal").strip()
                return {"ok": False, "platform": "windows", "message": msg[:300]}
            verify = subprocess.run(
                ["icacls.exe", str(target)], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15, check=False,
            )
            ok = verify.returncode == 0 and sid in verify.stdout
            return {
                "ok": ok,
                "platform": "windows",
                "message": (
                    "ACL dibatasi ke SID pengguna aktif dan SYSTEM, lalu terbaca kembali."
                    if ok else
                    "icacls selesai tetapi ACL tidak dapat diverifikasi. Jangan anggap file aman."
                ),
            }
        except Exception as exc:
            return {
                "ok": False, "platform": "windows",
                "message": f"ACL Windows gagal: {exc}. Jangan anggap file aman.",
            }

    return {"ok": False, "platform": os.name, "message": "Platform izin file tidak dikenali."}


def permission_status(path: os.PathLike | str = ENV_PATH) -> dict:
    target = Path(path)
    if not target.exists():
        return {"ok": False, "platform": os.name, "message": "File .env belum ada."}
    if os.name == "posix":
        try:
            mode = stat.S_IMODE(target.stat().st_mode)
            return {
                "ok": mode == 0o600,
                "platform": "posix",
                "mode": oct(mode),
                "message": "Izin file 0600 terverifikasi." if mode == 0o600 else f"Izin file saat ini {oct(mode)}.",
            }
        except OSError as exc:
            return {"ok": False, "platform": "posix", "message": str(exc)}
    # ACL Windows tidak disimpulkan hanya dari metadata file. Jalankan kembali
    # verifikasi icacls supaya UI tidak menampilkan klaim yang stale.
    if os.name == "nt":  # pragma: no cover - Windows
        sid = _windows_current_sid()
        if not sid:
            return {"ok": False, "platform": "windows", "message": "SID pengguna tidak tersedia."}
        try:
            result = subprocess.run(
                ["icacls.exe", str(target)], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15, check=False,
            )
            ok = result.returncode == 0 and sid in result.stdout
            return {
                "ok": ok, "platform": "windows",
                "message": "ACL pengguna aktif terdeteksi." if ok else "ACL .env tidak dapat diverifikasi.",
            }
        except Exception as exc:
            return {"ok": False, "platform": "windows", "message": str(exc)}
    return {"ok": False, "platform": os.name, "message": "Platform tidak dikenali."}
