"""Penyimpanan kredensial Binance di .env tanpa membocorkan secret."""

from __future__ import annotations

import csv
import os
import re
import stat
import subprocess
from pathlib import Path

from infrastructure.storage.atomic_io import atomic_write_text, interprocess_lock
from infrastructure.paths import PROJECT_ROOT


ROOT = PROJECT_ROOT
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
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _dominant_newline(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _update_env_unlocked(*, api_key: str | None = None, api_secret: str | None = None,
                         path: os.PathLike | str = ENV_PATH) -> dict:
    target = Path(path)
    replacements: dict[str, str] = {}
    if api_key is not None:
        replacements["BINANCE_API_KEY"] = _validate_secret_value(api_key, "API key")
    if api_secret is not None:
        replacements["BINANCE_API_SECRET"] = _validate_secret_value(api_secret, "API secret")
    if not replacements:
        return permission_status(target)

    try:
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


def update_env(*, api_key: str | None = None, api_secret: str | None = None,
               path: os.PathLike | str = ENV_PATH) -> dict:
    with interprocess_lock(path):
        return _update_env_unlocked(api_key=api_key, api_secret=api_secret, path=path)


def read_credentials(path: os.PathLike | str = ENV_PATH) -> tuple[str, str]:
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


def _windows_current_identity() -> tuple[str | None, str | None]:
    """Baca (SID, nama akun) pengguna aktif lewat whoami.

    Keduanya wajib karena output icacls menampilkan NAMA akun (contoh:
    DESKTOP-ABC\\john) dan hanya menampilkan SID mentah bila akun tidak bisa
    diselesaikan Windows. Mengembalikan (None, None) bila whoami gagal.
    """
    try:
        result = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, check=True,
        )
        baris = [b for b in result.stdout.strip().splitlines() if b.strip()]
        if not baris:
            return None, None
        row = next(csv.reader([baris[0]]))
        sid = row[1].strip() if len(row) >= 2 and row[1].strip() else None
        name = row[0].strip() if row and row[0].strip() else None
        return sid, name
    except Exception:
        return None, None


def _windows_read_sddl(target: Path) -> str | None:
    """Baca deskriptor keamanan file (pemilik + DACL) dalam bentuk SDDL.

    SDDL selalu memuat SID mentah (contoh: O:S-1-5-21-...D:AI(A;;FA;;;S-1-5-21-...))
    sehingga verifikasi tidak terpengaruh nama tampilan akun maupun bahasa
    tampilan Windows. Mengembalikan None bila gagal dibaca; pemanggil jatuh ke
    fallback icacls. Di luar Windows selalu None.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.GetFileSecurityW.restype = wintypes.BOOL
        advapi32.GetFileSecurityW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
        advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.LPVOID),
        ]

        OWNER_SECURITY_INFORMATION = 0x00000001
        DACL_SECURITY_INFORMATION = 0x00000004
        info = OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION

        needed = wintypes.DWORD(0)
        advapi32.GetFileSecurityW(str(target), info, None, 0, ctypes.byref(needed))
        if needed.value == 0:
            return None
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetFileSecurityW(
            str(target), info, buffer, needed.value, ctypes.byref(needed)
        ):
            return None
        sddl_ptr = wintypes.LPWSTR()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            buffer, 1, info, ctypes.byref(sddl_ptr), None
        ):
            return None
        try:
            return sddl_ptr.value
        finally:
            try:
                ctypes.windll.kernel32.LocalFree(ctypes.cast(sddl_ptr, wintypes.HANDLE))
            except Exception:
                pass
    except Exception:
        return None


def _identity_in_text(text: str, sid: str | None, name: str | None) -> bool:
    """True bila SID atau nama akun ada di teks output ACL.

    Pencocokan nama tidak peka huruf besar/kecil karena bentuk tampilan akun
    bisa berbeda antara whoami dan icacls (contoh MicrosoftAccount vs
    microsoftaccount). SID dicocokkan persis.
    """
    if not text:
        return False
    if sid and sid in text:
        return True
    if name and name.lower() in text.lower():
        return True
    return False


def _windows_acl_check(target: Path, sid: str | None, name: str | None) -> dict:
    """Periksa apakah pengguna aktif ada di ACL file.

    Lapis 1: deskriptor keamanan SDDL (SID mentah, paling andal).
    Lapis 2: output icacls, mencocokkan SID atau nama akun.

    "verified": True berarti ACL berhasil dibaca sehingga ketiadaan pengguna
    adalah hasil pasti, bukan kegagalan alat.
    """
    sddl = _windows_read_sddl(target)
    if sddl is not None:
        return {
            "ok": _identity_in_text(sddl, sid, name),
            "via": "sddl",
            "verified": True,
        }
    try:
        result = subprocess.run(
            ["icacls.exe", str(target)], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15, check=False,
        )
    except Exception as exc:
        return {"ok": False, "via": "icacls", "verified": False, "error": str(exc)}
    if result.returncode != 0:
        raw = (result.stderr or result.stdout or "icacls gagal tanpa pesan").strip()
        return {"ok": False, "via": "icacls", "verified": False, "error": raw[:300]}
    return {
        "ok": _identity_in_text(result.stdout or "", sid, name),
        "via": "icacls",
        "verified": True,
    }


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

    if os.name == "nt":
        sid, name = _windows_current_identity()
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
            check = _windows_acl_check(target, sid, name)
            if check["ok"]:
                return {
                    "ok": True, "platform": "windows",
                    "message": "ACL dibatasi ke SID pengguna aktif dan SYSTEM, lalu terbaca kembali.",
                }
            if check.get("verified"):
                return {
                    "ok": False, "platform": "windows",
                    "message": "icacls selesai tetapi ACL tidak memuat akun Anda. Jangan anggap file aman.",
                }
            return {
                "ok": False, "platform": "windows",
                "message": (
                    "icacls selesai tetapi ACL tidak dapat diverifikasi: "
                    f"{check.get('error') or 'icacls gagal'}. Jangan anggap file aman."
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
    if os.name == "nt":
        sid, name = _windows_current_identity()
        if not sid and not name:
            return {"ok": False, "platform": "windows", "message": "SID pengguna tidak tersedia."}
        check = _windows_acl_check(target, sid, name)
        if check["ok"]:
            return {"ok": True, "platform": "windows", "message": "ACL pengguna aktif terdeteksi."}
        if check.get("verified"):
            return {
                "ok": False, "platform": "windows",
                "message": (
                    "ACL .env tidak memuat akun Anda. Simpan ulang kredensial dari panel "
                    "Kredensial Binance agar izin file dikunci ulang ke akun Anda dan SYSTEM."
                ),
            }
        return {
            "ok": False, "platform": "windows",
            "message": (
                "ACL .env tidak dapat diverifikasi: "
                f"{check.get('error') or 'icacls gagal'}. Jangan anggap file aman."
            ),
        }
    return {"ok": False, "platform": os.name, "message": "Platform tidak dikenali."}
