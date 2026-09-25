"""Skema, validasi, dan penyimpanan override konfigurasi dashboard.

Modul ini tidak meng-import config.py agar config.py dapat memakainya saat
proses import tanpa circular import. Semua path runtime berakar di folder repo.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from atomic_io import (
    append_json_line,
    archive_corrupt,
    atomic_write_json,
    read_json,
)


ROOT = Path(__file__).resolve().parent
RUNTIME_FILE = ROOT / "pump_bot_runtime.json"
RUNTIME_ERROR_FILE = ROOT / "pump_bot_runtime.error.json"
AUDIT_FILE = ROOT / "pump_bot_settings_audit.log"
VALID_MODES = ("PAPER", "LIVE")
VALID_WATCHLIST_TIERS = ("INTI", "AKTIF", "SPEKULATIF")
# Tier lama yang masih mungkin ada di file watchlist atau settings override
# yang sudah tersimpan. Dipetakan diam-diam ke nama baru supaya file lama
# tidak ditolak validasi dan datanya tidak hilang.
LEGACY_WATCHLIST_TIER_MAP = {"MOMENTUM": "AKTIF"}

# Kunci config yang SUDAH DIHAPUS bersama strategi lama. Kalau masih ada di
# file override milik pengguna, kunci itu dibuang saat dimuat, bukan dianggap
# file rusak. Tanpa daftar ini, load_mode_override() akan mengarsipkan seluruh
# file override sebagai korup dan pengguna kehilangan semua setelannya.
REMOVED_CONFIG_KEYS = {
    "MIN_PUMP_PCT_24H",          # gerbang kenaikan 24 jam, dihapus bersama seleksi top gainer
    "MOMENTUM_FADE_EXIT",        # diganti SETUP_INVALIDATION_EXIT
    "MOMENTUM_FADE_RANK_THRESHOLD",
}
_WRITE_LOCK = threading.RLock()
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,40}$")
_ASSET_RE = re.compile(r"^[A-Z0-9]{2,12}$")


def _required_lookback_bars(candidate: dict) -> int:
    """Bungkus strategy.required_lookback_bars() dengan import lokal.

    Import dilakukan di dalam fungsi supaya modul ini tetap bisa diimpor oleh
    config.py tanpa menyeret dependensi lain saat proses import awal.
    strategy.py sendiri tidak mengimpor modul repo mana pun, jadi tidak ada
    risiko import melingkar.
    """
    from strategy import required_lookback_bars
    return required_lookback_bars(candidate)


def settings_file(mode: str) -> Path:
    return ROOT / f"pump_bot_settings_{str(mode).lower()}.json"


def settings_error_file(mode: str) -> Path:
    return ROOT / f"pump_bot_settings_{str(mode).lower()}.error.json"


def _field(group: str, label: str, description: str, kind: str,
           *, minimum: float | None = None, maximum: float | None = None,
           unit: str = "", dangerous: bool = False, read_only: bool = False,
           editor: str | None = None, options: list | None = None,
           managed_by: str | None = None) -> dict:
    return {
        "group": group,
        "label": label,
        "description": description,
        "type": kind,
        "min": minimum,
        "max": maximum,
        "unit": unit,
        "dangerous": dangerous,
        "read_only": read_only,
        "editor": editor or kind,
        "options": options,
        "managed_by": managed_by,
    }


# Satu entri untuk setiap kunci PUMP_CONFIG final, termasuk BASE_URL.
PARAMETER_SCHEMA: dict[str, dict] = {
    "QUOTE_ASSET": _field("Sistem", "Aset kuotasi", "Aset modal dan kuotasi pasangan.", "str", editor="asset"),
    "MODE": _field("Sistem", "Mode aktif", "Diubah melalui panel Mode.", "str", read_only=True, managed_by="mode"),
    "SHOW_BACKTEST_IN_LIVE": _field("Sistem", "Tampilkan backtest di LIVE", "Mengizinkan beban backtest saat bot LIVE.", "bool", dangerous=True),
    "LIVE_BASE_URL": _field("Sistem", "URL REST Binance", "Endpoint produksi yang dikunci oleh aplikasi.", "str", read_only=True),
    "API_KEY": _field("Sistem", "API key", "Dikelola melalui panel Kredensial.", "str", read_only=True, managed_by="credentials"),
    "API_SECRET": _field("Sistem", "API secret", "Write-only melalui panel Kredensial.", "str", read_only=True, managed_by="credentials"),
    "PAPER_INITIAL_BALANCES": _field("Akun PAPER", "Saldo awal PAPER", "Diubah hanya saat reset akun PAPER.", "dict", read_only=True, editor="balances", managed_by="paper_reset"),
    "PAPER_ACCOUNT_STATE_FILE": _field("Sistem", "File akun PAPER", "Path runtime internal.", "str", read_only=True),
    "PAPER_DEPTH_LIMIT": _field("Akun PAPER", "Kedalaman order book", "Jumlah level order book untuk simulasi fill.", "int", minimum=5, maximum=5000, unit="level"),
    "PAPER_LIMIT_ORDER_TIMEOUT_SECONDS": _field("Akun PAPER", "Timeout order limit", "Batas tunggu order limit simulasi.", "int", minimum=1, maximum=86400, unit="detik"),
    "USE_WEBSOCKET": _field("Sistem", "Gunakan WebSocket", "WebSocket sebagai sumber data pasar primer.", "bool"),
    "WS_BASE_URL": _field("Sistem", "URL WebSocket", "Endpoint WebSocket produksi yang dikunci.", "str", read_only=True),
    "MAX_MARKET_DATA_AGE_SECONDS": _field("Sistem", "Usia maksimum data pasar", "Data lebih tua akan dianggap basi.", "float", minimum=0.1, maximum=300, unit="detik"),

    "MARKET_SCAN_INTERVAL_SECONDS": _field("Scan", "Interval scan pasar", "Jarak waktu pemindaian seluruh pasar.", "int", minimum=10, maximum=86400, unit="detik"),
    "LOOP_INTERVAL_SECONDS": _field("Scan", "Interval loop", "Jarak evaluasi posisi dan kontrol.", "int", minimum=1, maximum=300, unit="detik"),
    "MIN_QUOTE_VOLUME_USDT_24H": _field("Scan", "Minimum volume kuotasi", "Volume 24 jam minimum.", "float", minimum=0, maximum=1e15, unit="USDT"),
    "TOP_N_CANDIDATES_TO_CONFIRM": _field("Scan", "Jumlah kandidat konfirmasi", "Berapa kandidat teratas yang diperiksa.", "int", minimum=1, maximum=1000),
    "CONFIRM_INTERVAL": _field("Scan", "Interval konfirmasi", "Interval candle konfirmasi setup.", "str", editor="select", options=["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"]),
    "CONFIRM_LOOKBACK_BARS": _field("Scan", "Jumlah candle konfirmasi", "Jumlah candle tertutup untuk deteksi setup dan ATR. Limit endpoint klines 1000 per panggilan.", "int", minimum=3, maximum=1000, unit="candle"),
    "MIN_CLOSE_POSITION_IN_RANGE": _field("Scan", "Minimum posisi close", "Posisi close candle retest di dalam rentang high-low.", "float", minimum=0, maximum=1),

    # Parameter strategi pullback dan retest. Semua default di config.py masih
    # harus divalidasi lewat backtest repo ini.
    "SWING_LOOKBACK_BARS": _field("Setup Pullback", "Lookback swing high", "Berapa candle ke belakang dipindai untuk mencari level breakout.", "int", minimum=3, maximum=500, unit="candle"),
    "SWING_PIVOT_WING_BARS": _field("Setup Pullback", "Sayap pivot", "Candle di kiri dan kanan yang harus lebih rendah agar sebuah candle menjadi pivot high.", "int", minimum=1, maximum=50, unit="candle"),
    "BREAKOUT_BUFFER_ATR_MULT": _field("Setup Pullback", "Buffer breakout", "Jarak di atas level yang wajib dilewati close agar dianggap breakout.", "float", minimum=0, maximum=10),
    "RETEST_ZONE_ATR_MULT": _field("Setup Pullback", "Lebar zona retest", "Setengah lebar zona di atas dan di bawah level, dalam satuan ATR.", "float", minimum=0.01, maximum=10),
    "RETEST_VWAP_CONFLUENCE_ATR_MULT": _field("Setup Pullback", "Konfluensi VWAP", "Jarak maksimum anchored VWAP terhadap level.", "float", minimum=0.01, maximum=20),
    "VWAP_MIN_BARS_AFTER_ANCHOR": _field("Setup Pullback", "Minimum candle setelah anchor", "Candle minimum setelah breakout sebelum anchored VWAP dipercaya.", "int", minimum=1, maximum=200, unit="candle"),
    "MAX_BARS_BREAKOUT_TO_RETEST": _field("Setup Pullback", "Umur maksimum setup", "Batas jarak candle dari breakout ke retest.", "int", minimum=1, maximum=500, unit="candle"),
    "MAX_RETEST_TOUCHES": _field("Setup Pullback", "Maksimum kunjungan zona", "Berapa kali harga boleh kembali ke zona sebelum setup dianggap lemah.", "int", minimum=1, maximum=20),
    "INVALIDATION_ATR_MULT": _field("Setup Pullback", "Jarak invalidasi", "Jarak di bawah level yang membatalkan setup dan memicu exit SETUP_INVALIDATED.", "float", minimum=0.01, maximum=20),
    "MAX_EXTENSION_ATR_MULT": _field("Setup Pullback", "Batas anti-kejar", "Jarak maksimum close di atas level agar entry masih diizinkan.", "float", minimum=0.01, maximum=20),
    "EXTRA_EXCLUDE_SYMBOLS": _field("Scan", "Blacklist simbol", "Simbol tambahan yang tidak boleh dipilih.", "list", editor="symbols"),
    "MIN_LISTING_AGE_DAYS": _field("Scan", "Usia listing minimum", "Pasangan lebih muda akan ditolak.", "int", minimum=0, maximum=36500, unit="hari"),

    "WATCHLIST_ENABLED": _field("Watchlist", "Aktifkan watchlist", "Menampilkan panel pemantauan watchlist.", "bool"),
    "WATCHLIST_AUTO_REFRESH": _field("Watchlist", "Penyegaran otomatis", "Susun ulang daftar dari data terbaru.", "bool"),
    "WATCHLIST_AUTO_INTERVAL_HOURS": _field("Watchlist", "Interval penyegaran", "Jarak penyegaran otomatis.", "int", minimum=1, maximum=720, unit="jam"),
    "WATCHLIST_AUTO_MAX_SYMBOLS": _field("Watchlist", "Maksimum simbol dinilai", "Batas kandidat yang diunduh saat refresh.", "int", minimum=1, maximum=600),
    "WATCHLIST_AUTO_DAYS": _field("Watchlist", "Riwayat penilaian", "Jumlah hari candle untuk penilaian.", "int", minimum=1, maximum=365, unit="hari"),
    "WATCHLIST_AUTO_KEEP": _field("Watchlist", "Jumlah simbol disimpan", "Jumlah hasil akhir watchlist.", "int", minimum=1, maximum=600),
    "WATCHLIST_AUTO_MAX_WEIGHT": _field("Watchlist", "Anggaran request weight", "Plafon weight setiap refresh.", "int", minimum=1, maximum=6000),
    "WATCHLIST_AUTO_PACE_SECONDS": _field("Watchlist", "Jeda request", "Jeda antarpanggilan saat refresh.", "float", minimum=0, maximum=60, unit="detik"),
    "WATCHLIST_AUTO_MIN_HEADROOM": _field("Watchlist", "Sisa kuota minimum", "Refresh berhenti bila headroom kurang.", "float", minimum=0, maximum=1),
    "WATCHLIST_AUTO_STARTUP_DELAY_SECONDS": _field("Watchlist", "Jeda awal", "Tunda refresh setelah dashboard start.", "int", minimum=0, maximum=86400, unit="detik"),
    "WATCHLIST": _field("Watchlist", "Daftar watchlist", "Editor simbol dan tier pemantauan.", "list", editor="watchlist"),

    "USE_RISK_PERCENT": _field("Ukuran Posisi", "Gunakan persen risiko", "Ukuran posisi dihitung dari saldo bebas.", "bool", dangerous=True),
    "RISK_PERCENT": _field("Ukuran Posisi", "Persen saldo per entry", "Persentase saldo bebas yang digunakan.", "float", minimum=0.01, maximum=100, unit="%", dangerous=True),
    "POSITION_SIZE_USDT": _field("Ukuran Posisi", "Ukuran posisi tetap", "Nominal saat mode persen dimatikan.", "float", minimum=0.01, maximum=1e9, unit="USDT", dangerous=True),
    "BACKTEST_INITIAL_EQUITY_USDT": _field("Ukuran Posisi", "Modal awal backtest", "Saldo USDT awal yang dipakai model sizing pada backtest.", "float", minimum=0.01, maximum=1e12, unit="USDT"),
    "MAX_POSITION_USDT": _field("Ukuran Posisi", "Plafon posisi", "Nol berarti tanpa plafon di PAPER, tetapi dilarang di LIVE.", "float", minimum=0, maximum=1e9, unit="USDT", dangerous=True),
    "BALANCE_BUFFER_PCT": _field("Ukuran Posisi", "Bantalan saldo", "Saldo yang tidak dibelanjakan untuk fee dan pergerakan harga.", "float", minimum=0, maximum=50, unit="%"),

    "USE_TP": _field("SL dan TP", "Aktifkan Take Profit", "Menutup posisi saat target tercapai.", "bool", dangerous=True),
    "TP_PCT": _field("SL dan TP", "Take Profit", "Target profit tetap saat ATR tidak aktif.", "float", minimum=0.01, maximum=1000, unit="%"),
    "USE_STOP_LOSS": _field("SL dan TP", "Aktifkan Stop Loss", "Jaring pengaman kerugian per trade.", "bool", dangerous=True),
    "SL_PCT": _field("SL dan TP", "Stop Loss", "Batas rugi tetap saat ATR tidak aktif.", "float", minimum=0.01, maximum=100, unit="%", dangerous=True),
    "USE_ATR_EXITS": _field("ATR", "Gunakan exit ATR", "Skalakan level exit dengan volatilitas.", "bool"),
    "ATR_PERIOD": _field("ATR", "Periode ATR", "Periode Wilder ATR.", "int", minimum=2, maximum=500, unit="candle"),
    "ATR_MULTIPLIER_SL": _field("ATR", "Pengali ATR untuk SL", "Pengali ATR sebelum batas min dan max.", "float", minimum=0.01, maximum=100),
    "ATR_SL_MIN_PCT": _field("ATR", "Batas bawah SL ATR", "Jarak SL ATR minimum.", "float", minimum=0.01, maximum=100, unit="%"),
    "ATR_SL_MAX_PCT": _field("ATR", "Batas atas SL ATR", "Jarak SL ATR maksimum.", "float", minimum=0.01, maximum=100, unit="%", dangerous=True),
    "ATR_TP_RR_RATIO": _field("ATR", "Rasio TP terhadap SL", "Target TP sebagai kelipatan SL.", "float", minimum=0.01, maximum=100),
    "ATR_BE_TRIGGER_MULT": _field("ATR", "Pengali trigger BE", "Trigger breakeven dalam satuan ATR.", "float", minimum=0, maximum=100),
    "ATR_BE_LOCK_MULT": _field("ATR", "Pengali kunci BE", "Profit yang dikunci dalam satuan ATR.", "float", minimum=0, maximum=100),
    "ATR_TRAILING_START_MULT": _field("ATR", "Pengali mulai trailing", "Mulai trailing dalam satuan ATR.", "float", minimum=0, maximum=100),
    "ATR_TRAILING_STEP_MULT": _field("ATR", "Pengali langkah trailing", "Jarak trailing dalam satuan ATR.", "float", minimum=0.01, maximum=100),

    "USE_BREAKEVEN": _field("Breakeven dan Trailing", "Aktifkan breakeven", "Mengunci posisi setelah profit minimum.", "bool"),
    "BE_TRIGGER_PCT": _field("Breakeven dan Trailing", "Trigger breakeven", "Profit untuk mengaktifkan breakeven tetap.", "float", minimum=0, maximum=1000, unit="%"),
    "BE_LOCK_PCT": _field("Breakeven dan Trailing", "Profit terkunci BE", "Profit minimum setelah BE aktif.", "float", minimum=0, maximum=1000, unit="%"),
    "USE_TRAILING": _field("Breakeven dan Trailing", "Aktifkan trailing", "Mengikuti kenaikan harga dengan stop dinamis.", "bool"),
    "TRAILING_START_PCT": _field("Breakeven dan Trailing", "Mulai trailing", "Profit untuk mengaktifkan trailing tetap.", "float", minimum=0, maximum=1000, unit="%"),
    "TRAILING_STEP_PCT": _field("Breakeven dan Trailing", "Jarak trailing", "Jarak stop dari harga tertinggi.", "float", minimum=0.01, maximum=100, unit="%"),
    "MAX_HOLD_MINUTES": _field("Breakeven dan Trailing", "Maksimum waktu hold", "Paksa keluar setelah durasi ini.", "int", minimum=1, maximum=525600, unit="menit"),
    "SETUP_INVALIDATION_EXIT": _field("Breakeven dan Trailing", "Exit saat setup batal", "Keluar saat candle tertutup menembus batas invalidasi yang dikunci saat entry.", "bool"),

    "MAX_SPREAD_PCT": _field("Fee dan Filter", "Spread maksimum", "Spread bid-ask maksimum untuk entry.", "float", minimum=0, maximum=100, unit="%", dangerous=True),
    "TAKER_FEE_PCT": _field("Fee dan Filter", "Fee taker", "Asumsi fee order market.", "float", minimum=0, maximum=10, unit="%"),
    "MAKER_FEE_PCT": _field("Fee dan Filter", "Fee maker", "Asumsi fee order limit maker.", "float", minimum=0, maximum=10, unit="%"),
    "USE_BNB_FEE_DISCOUNT": _field("Fee dan Filter", "Diskon fee BNB", "Gunakan asumsi diskon pembayaran fee dengan BNB.", "bool"),
    "COOLDOWN_MINUTES_AFTER_CLOSE": _field("Fee dan Filter", "Cooldown setelah close", "Jeda entry setelah posisi ditutup.", "int", minimum=0, maximum=525600, unit="menit"),
    "MIN_SECONDS_BETWEEN_TRADES": _field("Fee dan Filter", "Jarak minimum trade", "Jeda keras antartrade.", "int", minimum=0, maximum=31536000, unit="detik"),

    "USE_EQUITY_STOP": _field("Drawdown dan Daily Stop", "Aktifkan equity stop", "Menghentikan entry setelah drawdown maksimum.", "bool", dangerous=True),
    "MAX_DRAWDOWN_PERCENT": _field("Drawdown dan Daily Stop", "Drawdown maksimum", "Penurunan dari peak equity sebelum stop.", "float", minimum=0.01, maximum=100, unit="%", dangerous=True),
    "USE_DAILY_STOP": _field("Drawdown dan Daily Stop", "Aktifkan daily stop", "Menghentikan entry pada limit harian.", "bool", dangerous=True),
    "MAX_DAILY_LOSS_PERCENT": _field("Drawdown dan Daily Stop", "Rugi harian maksimum", "Kerugian harian sebelum stop.", "float", minimum=0.01, maximum=100, unit="%", dangerous=True),
    "DAILY_PROFIT_TARGET_PERCENT": _field("Drawdown dan Daily Stop", "Target profit harian", "Profit harian sebelum entry dihentikan.", "float", minimum=0.01, maximum=10000, unit="%"),
    "CLOSE_ALL_AT_LIMIT": _field("Drawdown dan Daily Stop", "Tutup posisi saat limit", "Tutup posisi saat kill switch aktif.", "bool", dangerous=True),
    "DD_COOLDOWN_HOURS": _field("Drawdown dan Daily Stop", "Cooldown drawdown", "Durasi jeda setelah drawdown stop.", "int", minimum=1, maximum=87600, unit="jam"),
    "MAX_CONSECUTIVE_ERRORS": _field("Sistem", "Maksimum error beruntun", "Bot berhenti setelah error API beruntun.", "int", minimum=1, maximum=100000),

    "STATE_FILE": _field("Sistem", "File state posisi", "Path runtime internal per mode.", "str", read_only=True),
    "LOG_FILE": _field("Sistem", "File log", "Path runtime internal per mode.", "str", read_only=True),
    "HEARTBEAT_INTERVAL_SECONDS": _field("Sistem", "Interval heartbeat log", "Jarak heartbeat di log bot.", "int", minimum=5, maximum=86400, unit="detik"),
    "CONTROL_FILE": _field("Sistem", "File kontrol", "Path komunikasi dashboard ke bot.", "str", read_only=True),
    "USE_DUST_SWEEP": _field("Sistem", "Konversi dust ke BNB", "Konversi dust base asset setelah close di LIVE.", "bool"),
    "BASE_URL": _field("Sistem", "Base URL aktif", "Alias turunan dari LIVE_BASE_URL.", "str", read_only=True),
}


def _error_marker(path: Path, message: str, backup: Path | None = None) -> None:
    atomic_write_json(path, {
        "error": message,
        "backup": str(backup) if backup else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


def load_runtime_mode(default: str = "PAPER") -> tuple[str, list[str]]:
    errors: list[str] = []
    if RUNTIME_ERROR_FILE.exists():
        marker = read_json(RUNTIME_ERROR_FILE, {}) or {}
        errors.append(str(marker.get("error") or "File runtime mode sebelumnya rusak."))
    if not RUNTIME_FILE.exists():
        return default, errors
    try:
        with open(RUNTIME_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or "active_mode" not in data:
            raise ValueError("root harus object dan memiliki active_mode")
        return str(data["active_mode"]), errors
    except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
        backup = archive_corrupt(RUNTIME_FILE)
        message = f"File mode runtime rusak: {exc}. Cadangan: {backup}"
        _error_marker(RUNTIME_ERROR_FILE, message, backup)
        errors.append(message)
        return default, errors


def save_runtime_mode(mode: str) -> None:
    raw = str(mode).strip().upper()
    if raw not in VALID_MODES:
        raise ValueError(f"Mode tidak valid: {mode!r}")
    with _WRITE_LOCK:
        atomic_write_json(RUNTIME_FILE, {"active_mode": raw})
        try:
            RUNTIME_ERROR_FILE.unlink(missing_ok=True)
        except OSError:
            pass


def load_mode_override(mode: str) -> tuple[dict, list[str]]:
    raw_mode = str(mode).strip().upper()
    path = settings_file(raw_mode)
    marker_path = settings_error_file(raw_mode)
    errors: list[str] = []
    if marker_path.exists():
        marker = read_json(marker_path, {}) or {}
        errors.append(str(marker.get("error") or f"Override {raw_mode} sebelumnya rusak."))
    if not path.exists():
        return {}, errors
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("root override harus object JSON")
        # MIGRASI: kunci strategi lama dibuang, bukan dianggap file rusak.
        dibuang = sorted(set(data) & REMOVED_CONFIG_KEYS)
        for key in dibuang:
            data.pop(key, None)
        if dibuang:
            errors.append(
                f"Override {raw_mode} memuat kunci strategi lama yang sudah dihapus dan "
                "diabaikan: " + ", ".join(dibuang))
        unknown = sorted(set(data) - set(PARAMETER_SCHEMA))
        if unknown:
            raise ValueError("kunci override tidak dikenal: " + ", ".join(unknown))
        forbidden = [k for k in data if PARAMETER_SCHEMA[k]["read_only"]]
        # PAPER_INITIAL_BALANCES adalah pengecualian terkelola oleh reset.
        forbidden = [k for k in forbidden if k != "PAPER_INITIAL_BALANCES"]
        if forbidden:
            raise ValueError("override memuat kunci read-only: " + ", ".join(forbidden))
        return data, errors
    except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
        backup = archive_corrupt(path)
        message = f"Override {raw_mode} rusak: {exc}. Cadangan: {backup}"
        _error_marker(marker_path, message, backup)
        errors.append(message)
        return {}, errors


def save_mode_override(mode: str, overrides: dict) -> None:
    raw_mode = str(mode).strip().upper()
    if raw_mode not in VALID_MODES:
        raise ValueError("Mode override tidak valid.")
    with _WRITE_LOCK:
        atomic_write_json(settings_file(raw_mode), overrides)
        try:
            settings_error_file(raw_mode).unlink(missing_ok=True)
        except OSError:
            pass


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lower = value.strip().lower()
        if lower in ("true", "1", "yes", "ya", "on"):
            return True
        if lower in ("false", "0", "no", "tidak", "off"):
            return False
    raise ValueError("harus bernilai boolean")


def _coerce_number(value: Any, integer: bool) -> int | float:
    if isinstance(value, bool):
        raise ValueError("boolean bukan angka")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("harus berupa angka") from None
    if not math.isfinite(number):
        raise ValueError("angka harus finite")
    if integer:
        if not number.is_integer():
            raise ValueError("harus berupa bilangan bulat")
        return int(number)
    return number


def _validate_symbol_list(value: Any, quote: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("harus berupa daftar simbol")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        symbol = str(item).strip().upper()
        if not _SYMBOL_RE.fullmatch(symbol) or not symbol.endswith(quote):
            raise ValueError(f"simbol tidak valid: {symbol!r}")
        if symbol in seen:
            raise ValueError(f"simbol duplikat: {symbol}")
        seen.add(symbol)
        result.append(symbol)
    return result


def _validate_watchlist(value: Any, quote: str) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("watchlist harus berupa daftar")
    result: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"baris watchlist {index + 1} harus object")
        symbol = str(item.get("symbol", "")).strip().upper()
        tier = LEGACY_WATCHLIST_TIER_MAP.get(
            str(item.get("tier", "")).strip().upper(),
            str(item.get("tier", "")).strip().upper())
        if not _SYMBOL_RE.fullmatch(symbol) or not symbol.endswith(quote):
            raise ValueError(f"simbol watchlist tidak valid: {symbol!r}")
        if tier not in VALID_WATCHLIST_TIERS:
            raise ValueError(f"tier {symbol} tidak valid")
        if symbol in seen:
            raise ValueError(f"simbol watchlist duplikat: {symbol}")
        seen.add(symbol)
        row = {"symbol": symbol, "tier": tier}
        if item.get("score") not in (None, ""):
            score = _coerce_number(item.get("score"), False)
            if score < 0 or score > 100:
                raise ValueError(f"score {symbol} harus 0 sampai 100")
            row["score"] = score
        if item.get("note") not in (None, ""):
            note = str(item.get("note", "")).strip()
            if len(note) > 300:
                raise ValueError(f"catatan {symbol} maksimal 300 karakter")
            row["note"] = note
        result.append(row)
    return result


def validate_balances(value: Any) -> dict[str, float]:
    if not isinstance(value, dict) or not value:
        raise ValueError("saldo awal harus object yang tidak kosong")
    result: dict[str, float] = {}
    for asset, amount in value.items():
        name = str(asset).strip().upper()
        if not _ASSET_RE.fullmatch(name):
            raise ValueError(f"nama aset tidak valid: {name!r}")
        number = _coerce_number(amount, False)
        if number <= 0:
            raise ValueError(f"saldo {name} harus lebih besar dari nol")
        if number > 1e18:
            raise ValueError(f"saldo {name} terlalu besar")
        result[name] = number
    return result


def coerce_field(key: str, value: Any, candidate: dict) -> Any:
    spec = PARAMETER_SCHEMA[key]
    kind = spec["type"]
    if kind == "bool":
        result = _coerce_bool(value)
    elif kind == "int":
        result = _coerce_number(value, True)
    elif kind == "float":
        result = _coerce_number(value, False)
    elif kind == "str":
        if not isinstance(value, str):
            raise ValueError("harus berupa teks")
        result = value.strip()
        if not result:
            raise ValueError("tidak boleh kosong")
        if len(result) > 2048:
            raise ValueError("teks terlalu panjang")
        if spec.get("editor") == "asset":
            result = result.upper()
            if not _ASSET_RE.fullmatch(result):
                raise ValueError("kode aset tidak valid")
        options = spec.get("options")
        if options and result not in options:
            raise ValueError("nilai tidak termasuk pilihan yang diizinkan")
    elif key == "WATCHLIST":
        result = _validate_watchlist(value, str(candidate.get("QUOTE_ASSET", "USDT")).upper())
    elif key == "EXTRA_EXCLUDE_SYMBOLS":
        result = _validate_symbol_list(value, str(candidate.get("QUOTE_ASSET", "USDT")).upper())
    elif key == "PAPER_INITIAL_BALANCES":
        result = validate_balances(value)
    else:
        raise ValueError("tipe field tidak didukung")

    if isinstance(result, (int, float)) and not isinstance(result, bool):
        minimum = spec.get("min")
        maximum = spec.get("max")
        if minimum is not None and result < minimum:
            raise ValueError(f"minimal {minimum:g}")
        if maximum is not None and result > maximum:
            raise ValueError(f"maksimal {maximum:g}")
    return result


def validate_candidate(candidate: dict, mode: str) -> tuple[dict, dict[str, str], list[str]]:
    """Validasi semua nilai editable dan relasi lintas-field."""
    raw_mode = str(mode).strip().upper()
    cleaned = deepcopy(candidate)
    errors: dict[str, str] = {}
    warnings: list[str] = []

    # QUOTE_ASSET divalidasi lebih dulu karena dipakai editor simbol.
    keys = ["QUOTE_ASSET"] + [k for k in PARAMETER_SCHEMA if k != "QUOTE_ASSET"]
    for key in keys:
        spec = PARAMETER_SCHEMA[key]
        if spec["read_only"] and key != "PAPER_INITIAL_BALANCES":
            continue
        if key not in candidate:
            errors[key] = "nilai tidak tersedia"
            continue
        try:
            cleaned[key] = coerce_field(key, candidate[key], cleaned)
        except ValueError as exc:
            errors[key] = str(exc)

    def relation(key: str, condition: bool, message: str) -> None:
        if not condition and key not in errors:
            errors[key] = message

    if not errors:
        relation("ATR_SL_MIN_PCT", cleaned["ATR_SL_MIN_PCT"] <= cleaned["ATR_SL_MAX_PCT"],
                 "harus lebih kecil atau sama dengan batas atas ATR")
        if cleaned["USE_ATR_EXITS"]:
            relation("CONFIRM_LOOKBACK_BARS",
                     cleaned["CONFIRM_LOOKBACK_BARS"] >= cleaned["ATR_PERIOD"] + 1,
                     "harus minimal ATR_PERIOD + 1 saat ATR aktif")
        # Relasi jendela konfirmasi terhadap struktur setup. Dihitung lewat
        # SATU fungsi bersama supaya angka minimum tidak pernah berbeda antara
        # validasi, bot live, dan backtest.
        butuh_bars = _required_lookback_bars(cleaned)
        relation("CONFIRM_LOOKBACK_BARS",
                 cleaned["CONFIRM_LOOKBACK_BARS"] >= butuh_bars,
                 f"harus minimal {butuh_bars} candle untuk ATR dan struktur setup "
                 "(SWING_LOOKBACK_BARS + 2 x SWING_PIVOT_WING_BARS + MAX_BARS_BREAKOUT_TO_RETEST)")
        # Zona retest tidak boleh lebih dalam dari batas invalidasi. Kalau
        # lebih dalam, candle yang baru menyentuh dasar zona sudah otomatis
        # membatalkan setup, sehingga retest tidak akan pernah sah.
        relation("INVALIDATION_ATR_MULT",
                 cleaned["INVALIDATION_ATR_MULT"] >= cleaned["RETEST_ZONE_ATR_MULT"],
                 "harus lebih besar atau sama dengan RETEST_ZONE_ATR_MULT")
        # Batas anti-kejar harus di atas buffer breakout, kalau tidak setiap
        # breakout yang sah langsung dianggap terlalu jauh.
        relation("MAX_EXTENSION_ATR_MULT",
                 cleaned["MAX_EXTENSION_ATR_MULT"] >= cleaned["BREAKOUT_BUFFER_ATR_MULT"],
                 "harus lebih besar atau sama dengan BREAKOUT_BUFFER_ATR_MULT")
        if cleaned["USE_STOP_LOSS"]:
            relation("SL_PCT", cleaned["SL_PCT"] > 0, "harus lebih besar dari nol saat Stop Loss aktif")
        if cleaned["USE_TP"]:
            relation("TP_PCT", cleaned["TP_PCT"] > 0, "harus lebih besar dari nol saat Take Profit aktif")
        if cleaned["USE_RISK_PERCENT"]:
            relation("RISK_PERCENT", cleaned["RISK_PERCENT"] > 0, "harus lebih besar dari nol")
        else:
            relation("POSITION_SIZE_USDT", cleaned["POSITION_SIZE_USDT"] > 0,
                     "harus lebih besar dari nol saat sizing tetap")
        relation("WATCHLIST_AUTO_KEEP",
                 cleaned["WATCHLIST_AUTO_KEEP"] <= cleaned["WATCHLIST_AUTO_MAX_SYMBOLS"],
                 "tidak boleh melebihi jumlah simbol yang dinilai")
        if raw_mode == "LIVE":
            relation("MAX_POSITION_USDT", cleaned["MAX_POSITION_USDT"] > 0,
                     "mode LIVE wajib memiliki plafon posisi lebih besar dari nol")
        elif cleaned["MAX_POSITION_USDT"] == 0:
            warnings.append("PAPER berjalan tanpa plafon posisi nominal.")

    return cleaned, errors, warnings


def editable_keys() -> set[str]:
    return {k for k, v in PARAMETER_SCHEMA.items() if not v["read_only"]}


def compute_overrides(defaults: dict, candidate: dict, *, include_balances: bool = False) -> dict:
    allowed = editable_keys()
    if include_balances:
        allowed.add("PAPER_INITIAL_BALANCES")
    result = {}
    for key in allowed:
        if key in candidate and candidate.get(key) != defaults.get(key):
            result[key] = deepcopy(candidate[key])
    return result


def diff_values(old: dict, new: dict) -> list[dict]:
    result = []
    for key in PARAMETER_SCHEMA:
        if old.get(key) != new.get(key):
            result.append({
                "key": key,
                "label": PARAMETER_SCHEMA[key]["label"],
                "old": deepcopy(old.get(key)),
                "new": deepcopy(new.get(key)),
                "dangerous": bool(PARAMETER_SCHEMA[key]["dangerous"]),
            })
    return result


def dangerous_relaxations(old: dict, new: dict) -> list[str]:
    """Kembalikan perubahan LIVE yang menambah eksposur atau melepas guard."""
    relaxed: list[str] = []
    if float(new.get("RISK_PERCENT", 0)) > float(old.get("RISK_PERCENT", 0)):
        relaxed.append("RISK_PERCENT dinaikkan")
    if (not bool(new.get("USE_RISK_PERCENT")) and
            (bool(old.get("USE_RISK_PERCENT")) or
             float(new.get("POSITION_SIZE_USDT", 0)) > float(old.get("POSITION_SIZE_USDT", 0)))):
        relaxed.append("sizing tetap diaktifkan atau POSITION_SIZE_USDT dinaikkan")
    old_max = float(old.get("MAX_POSITION_USDT", 0) or 0)
    new_max = float(new.get("MAX_POSITION_USDT", 0) or 0)
    if new_max == 0 or (old_max > 0 and new_max > old_max):
        relaxed.append("MAX_POSITION_USDT dilonggarkan")
    for key, label in (
        ("USE_TP", "Take Profit dimatikan"),
        ("USE_STOP_LOSS", "Stop Loss dimatikan"),
        ("USE_EQUITY_STOP", "Equity Stop dimatikan"),
        ("USE_DAILY_STOP", "Daily Stop dimatikan"),
        ("CLOSE_ALL_AT_LIMIT", "penutupan posisi pada limit dimatikan"),
    ):
        if bool(old.get(key)) and not bool(new.get(key)):
            relaxed.append(label)
    if not bool(old.get("SHOW_BACKTEST_IN_LIVE")) and bool(new.get("SHOW_BACKTEST_IN_LIVE")):
        relaxed.append("Backtest diaktifkan saat LIVE dan dapat memakai rate limit IP")
    for key, label in (
        ("MAX_DRAWDOWN_PERCENT", "MAX_DRAWDOWN_PERCENT dinaikkan"),
        ("MAX_DAILY_LOSS_PERCENT", "MAX_DAILY_LOSS_PERCENT dinaikkan"),
        ("SL_PCT", "jarak Stop Loss diperlebar"),
        ("ATR_SL_MAX_PCT", "batas atas Stop Loss ATR diperlebar"),
        ("MAX_SPREAD_PCT", "batas spread entry diperlebar"),
    ):
        if float(new.get(key, 0)) > float(old.get(key, 0)):
            relaxed.append(label)
    return relaxed


def audit_change(event: dict) -> None:
    safe = deepcopy(event)
    for forbidden in ("API_SECRET", "BINANCE_API_SECRET", "secret"):
        safe.pop(forbidden, None)
    safe.setdefault("time", datetime.now(timezone.utc).isoformat())
    with _WRITE_LOCK:
        append_json_line(AUDIT_FILE, safe)


def read_audit(limit: int = 200) -> list[dict]:
    try:
        with open(AUDIT_FILE, "r", encoding="utf-8") as handle:
            lines = handle.readlines()[-max(1, min(int(limit), 1000)):]
    except OSError:
        return []
    result = []
    for line in reversed(lines):
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                result.append(item)
        except json.JSONDecodeError:
            continue
    return result


def public_schema(defaults: dict, current: dict) -> list[dict]:
    fields = []
    for key, spec in PARAMETER_SCHEMA.items():
        row = {"key": key, **deepcopy(spec)}
        # Kredensial tidak pernah dikirim, bahkan bila defaults berasal dari env.
        if key in ("API_KEY", "API_SECRET"):
            row["default"] = None
            row["value"] = None
            row["modified"] = False
        else:
            row["default"] = deepcopy(defaults.get(key))
            row["value"] = deepcopy(current.get(key))
            row["modified"] = defaults.get(key) != current.get(key)
        fields.append(row)
    return fields
