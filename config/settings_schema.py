from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from infrastructure.storage.atomic_io import (
    archive_corrupt,
    atomic_write_json,
    interprocess_lock,
    read_json,
)
from infrastructure.paths import DATA_DIR

SETTINGS_FILE = DATA_DIR / "settings.json"
SETTINGS_ERROR_FILE = DATA_DIR / "settings.error.json"
VALID_MODES = ("PAPER", "LIVE")


_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,40}$")
_ASSET_RE = re.compile(r"^[A-Z0-9]{2,12}$")


def _field(
    group: str,
    label: str,
    description: str,
    kind: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    unit: str = "",
    dangerous: bool = False,
    read_only: bool = False,
    editor: str | None = None,
    options: list | None = None,
    managed_by: str | None = None,
) -> dict:
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


PARAMETER_SCHEMA: dict[str, dict] = {
    "QUOTE_ASSET": _field(
        "Sistem",
        "Aset kuotasi",
        "Aset modal dan kuotasi pasangan.",
        "str",
        editor="asset",
    ),
    "MODE": _field(
        "Sistem",
        "Mode aktif",
        "Dibaca dari BOT_MODE di file .env.",
        "str",
        read_only=True,
        managed_by="environment",
    ),
    "SHOW_BACKTEST_IN_LIVE": _field(
        "Sistem",
        "Tampilkan backtest di LIVE",
        "Mengizinkan beban backtest saat bot LIVE.",
        "bool",
        dangerous=True,
    ),
    "LIVE_BASE_URL": _field(
        "Sistem",
        "URL REST Binance",
        "Endpoint produksi yang dikunci oleh aplikasi.",
        "str",
        read_only=True,
    ),
    "RATE_LIMIT_STATE_FILE": _field(
        "Sistem",
        "Ledger rate limit",
        "File runtime bersama untuk koordinasi REQUEST_WEIGHT lintas proses.",
        "str",
        read_only=True,
    ),
    "RATE_LIMIT_WEIGHT_LIMIT": _field(
        "Sistem",
        "Batas REQUEST_WEIGHT",
        "Batas weight Binance per menit dan IP.",
        "int",
        read_only=True,
    ),
    "RATE_LIMIT_SAFETY_MARGIN": _field(
        "Sistem",
        "Cadangan REQUEST_WEIGHT",
        "Cadangan agar request tidak mendekati batas IP.",
        "int",
        read_only=True,
    ),
    "API_KEY": _field(
        "Sistem",
        "API key",
        "Dibaca dari BINANCE_API_KEY di file .env.",
        "str",
        read_only=True,
        managed_by="credentials",
    ),
    "API_SECRET": _field(
        "Sistem",
        "API secret",
        "Dibaca dari BINANCE_API_SECRET di file .env.",
        "str",
        read_only=True,
        managed_by="credentials",
    ),
    "PAPER_INITIAL_BALANCES": _field(
        "Akun PAPER",
        "Saldo awal PAPER",
        "Dibaca dari override PAPER di data/settings.json.",
        "dict",
        read_only=True,
        editor="balances",
        managed_by="paper_reset",
    ),
    "PAPER_ACCOUNT_STATE_FILE": _field(
        "Sistem", "File akun PAPER", "Path runtime internal.", "str", read_only=True
    ),
    "PAPER_DEPTH_LIMIT": _field(
        "Akun PAPER",
        "Kedalaman order book",
        "Jumlah level order book untuk simulasi fill.",
        "int",
        minimum=5,
        maximum=5000,
        unit="level",
    ),
    "PAPER_LIMIT_ORDER_TIMEOUT_SECONDS": _field(
        "Akun PAPER",
        "Timeout order limit",
        "Batas tunggu order limit simulasi.",
        "int",
        minimum=1,
        maximum=86400,
        unit="detik",
    ),
    "USE_WEBSOCKET": _field(
        "Sistem",
        "Gunakan WebSocket",
        "WebSocket sebagai sumber data pasar primer.",
        "bool",
    ),
    "WS_BASE_URL": _field(
        "Sistem",
        "URL WebSocket",
        "Endpoint WebSocket produksi yang dikunci.",
        "str",
        read_only=True,
    ),
    "MAX_MARKET_DATA_AGE_SECONDS": _field(
        "Sistem",
        "Usia maksimum data pasar",
        "Data lebih tua akan dianggap basi.",
        "float",
        minimum=0.1,
        maximum=300,
        unit="detik",
    ),
    "TICKER_SNAPSHOT_TTL_SECONDS": _field(
        "Sistem",
        "Usia snapshot ticker 24 jam",
        "Daftar ticker 24 jam disegarkan di latar belakang oleh thread khusus, sehingga scan tidak perlu menunggu unduhan 1,9 MB itu. 0 berarti selalu menunggu unduhan segar seperti versi lama.",
        "int",
        minimum=0,
        maximum=3600,
        unit="detik",
    ),
    "WS_LAST_PRICE_OVERLAY_ENABLED": _field(
        "Sistem",
        "Timpa harga terakhir dari WebSocket",
        "Harga terakhir pada snapshot ticker ditimpa dengan harga terbaru dari stream !miniTicker@arr. Hanya memengaruhi harga acuan cadangan, tidak pernah mengubah lolos atau tidaknya suatu kandidat.",
        "bool",
    ),
    "MARKET_SCAN_INTERVAL_SECONDS": _field(
        "Scan",
        "Interval scan pasar",
        "Jarak waktu pemindaian seluruh pasar.",
        "int",
        minimum=10,
        maximum=86400,
        unit="detik",
    ),
    "LOOP_INTERVAL_SECONDS": _field(
        "Scan",
        "Interval loop",
        "Jarak evaluasi posisi dan kontrol.",
        "int",
        minimum=1,
        maximum=300,
        unit="detik",
    ),
    "MARKET_DATA_WORKERS": _field(
        "Scan",
        "Worker pengambilan data pasar",
        "Jumlah thread paralel untuk mengambil candle konfirmasi saat scan. 1 berarti serial seperti versi lama.",
        "int",
        minimum=1,
        maximum=32,
        unit="thread",
    ),
    "MIN_QUOTE_VOLUME_USDT_24H": _field(
        "Scan",
        "Minimum volume kuotasi",
        "Volume 24 jam minimum.",
        "float",
        minimum=0,
        maximum=1e15,
        unit="USDT",
    ),
    "MARKET_DATA_INTERVAL": _field(
        "Scan",
        "Interval data pasar",
        "Interval candle yang digunakan untuk monitoring pasar.",
        "str",
        editor="select",
        options=[
            "1m",
            "3m",
            "5m",
            "15m",
            "30m",
            "1h",
            "2h",
            "4h",
            "6h",
            "8h",
            "12h",
            "1d",
        ],
    ),
    "PUMP_MIN_24H_CHANGE_PCT": _field(
        "Monitoring Pasar",
        "Minimum perubahan 24 jam",
        "Filter monitoring perubahan harga 24 jam.",
        "float",
        minimum=0,
        maximum=1000,
        unit="%",
    ),
    "PUMP_MAX_24H_CHANGE_PCT": _field(
        "Monitoring Pasar",
        "Maksimum perubahan 24 jam",
        "Koin yang sudah naik lebih dari persen ini dalam 24 jam ditolak agar bot tidak membeli di pucuk. Harus lebih besar dari minimum. 0 = nonaktif.",
        "float",
        minimum=0,
        maximum=1000,
        unit="%",
        dangerous=True,
    ),
    "BTC_FILTER_ENABLED": _field(
        "Monitoring Pasar",
        "Filter kondisi BTC",
        "Filter monitoring kondisi BTC pada jendela candle tertutup.",
        "bool",
    ),
    "BTC_MAX_DROP_PCT": _field(
        "Monitoring Pasar",
        "Penurunan BTC maksimum",
        "Batas penurunan BTC untuk filter monitoring.",
        "float",
        minimum=0.1,
        maximum=50,
        unit="%",
    ),
    "BTC_LOOKBACK_BARS": _field(
        "Monitoring Pasar",
        "Lookback BTC",
        "Jumlah candle tertutup untuk mengukur penurunan BTC.",
        "int",
        minimum=1,
        maximum=1000,
        unit="candle",
    ),
    "USE_ATR_EXIT": _field(
        "SL dan TP",
        "Gunakan exit ATR",
        "Gunakan jarak exit adaptif berdasarkan ATR; jika mati, gunakan persen lama.",
        "bool",
        dangerous=True,
    ),
    "ATR_PERIOD": _field(
        "SL dan TP",
        "Periode ATR",
        "Periode ATR Wilder.",
        "int",
        minimum=2,
        maximum=200,
        unit="candle",
    ),
    "ATR_MULT_SL": _field(
        "SL dan TP",
        "Pengali ATR Stop Loss",
        "Jarak Stop Loss dalam ATR.",
        "float",
        minimum=0.1,
        maximum=20,
        unit="x",
        dangerous=True,
    ),
    "ATR_MULT_TP": _field(
        "SL dan TP",
        "Pengali ATR Take Profit",
        "Jarak Take Profit dalam ATR.",
        "float",
        minimum=0.1,
        maximum=50,
        unit="x",
    ),
    "ATR_MULT_TRAIL": _field(
        "SL dan TP",
        "Pengali ATR trailing",
        "Jarak trailing dalam ATR.",
        "float",
        minimum=0.1,
        maximum=20,
        unit="x",
    ),
    "ATR_MULT_BE_TRIGGER": _field(
        "Breakeven dan Trailing",
        "Pengali ATR trigger BE",
        "Profit ATR untuk mengaktifkan breakeven.",
        "float",
        minimum=0,
        maximum=20,
        unit="x",
    ),
    "ATR_MULT_BE_LOCK": _field(
        "Breakeven dan Trailing",
        "Pengali ATR lock BE",
        "Profit ATR yang dikunci.",
        "float",
        minimum=0,
        maximum=20,
        unit="x",
    ),
    "ATR_MULT_TRAIL_START": _field(
        "Breakeven dan Trailing",
        "Pengali ATR mulai trailing",
        "Profit ATR untuk mengaktifkan trailing.",
        "float",
        minimum=0,
        maximum=50,
        unit="x",
    ),
    "EXTRA_EXCLUDE_SYMBOLS": _field(
        "Scan",
        "Blacklist simbol",
        "Simbol tambahan yang tidak boleh dipilih.",
        "list",
        editor="symbols",
    ),
    "BACKTEST_INITIAL_EQUITY_USDT": _field(
        "Data Backtest",
        "Modal awal backtest",
        "Saldo USDT awal simulasi backtest. 0 berarti mengikuti saldo awal PAPER (PAPER_INITIAL_BALANCES) supaya persen return dan drawdown sebanding dengan bot.",
        "float",
        minimum=0,
        maximum=1e12,
        unit="USDT",
    ),
    "BACKTEST_CACHE_ENABLED": _field(
        "Sistem",
        "Cache candle backtest",
        "Pakai ulang candle yang sudah pernah diunduh supaya backtest ulang tidak mengunduh dari nol.",
        "bool",
    ),
    "BACKTEST_CACHE_FILE": _field(
        "Sistem",
        "File cache backtest",
        "Path runtime internal cache candle backtest.",
        "str",
        read_only=True,
    ),
    "BACKTEST_CACHE_FRESH_HOURS": _field(
        "Sistem",
        "Jendela segar cache",
        "Rentang jam terakhir yang selalu diunduh ulang karena candle belum tertutup.",
        "int",
        minimum=0,
        maximum=168,
        unit="jam",
    ),
    "BACKTEST_CACHE_TTL_DAYS": _field(
        "Sistem",
        "Umur cache backtest",
        "Data simbol yang tidak dipakai selama sekian hari dibuang. Nol berarti tidak pernah dipangkas.",
        "int",
        minimum=0,
        maximum=3650,
        unit="hari",
    ),
    "USE_TP": _field(
        "SL dan TP",
        "Aktifkan Take Profit",
        "Menutup posisi saat target tercapai.",
        "bool",
        dangerous=True,
    ),
    "TP_PCT": _field(
        "SL dan TP",
        "Take Profit",
        "Target profit tetap.",
        "float",
        minimum=0.01,
        maximum=1000,
        unit="%",
    ),
    "USE_STOP_LOSS": _field(
        "SL dan TP",
        "Aktifkan Stop Loss",
        "Jaring pengaman kerugian per trade.",
        "bool",
        dangerous=True,
    ),
    "USE_NATIVE_OCO": _field(
        "SL dan TP",
        "OCO exchange-side LIVE",
        "Pasang OCO SELL native berisi TP limit dan SL limit pada posisi yang terdeteksi.",
        "bool",
        dangerous=True,
    ),
    "USE_NATIVE_STOP_LOSS": _field(
        "SL dan TP",
        "Stop Loss exchange-side fallback",
        "Fallback STOP_LOSS market native bila pemasangan OCO tidak didukung.",
        "bool",
        dangerous=True,
    ),
    "NATIVE_OCO_LIMIT_BUFFER_PCT": _field(
        "SL dan TP",
        "Buffer limit OCO",
        "Jarak limit order dari trigger OCO agar ada peluang fill setelah trigger.",
        "float",
        minimum=0.01,
        maximum=5,
        unit="%",
        dangerous=True,
    ),
    "SL_PCT": _field(
        "SL dan TP",
        "Stop Loss",
        "Batas rugi tetap.",
        "float",
        minimum=0.01,
        maximum=100,
        unit="%",
        dangerous=True,
    ),
    "USE_BREAKEVEN": _field(
        "Breakeven dan Trailing",
        "Aktifkan breakeven",
        "Mengunci posisi setelah profit minimum.",
        "bool",
    ),
    "BE_TRIGGER_PCT": _field(
        "Breakeven dan Trailing",
        "Trigger breakeven",
        "Profit untuk mengaktifkan breakeven tetap.",
        "float",
        minimum=0,
        maximum=1000,
        unit="%",
    ),
    "BE_LOCK_PCT": _field(
        "Breakeven dan Trailing",
        "Profit terkunci BE",
        "Profit minimum setelah BE aktif.",
        "float",
        minimum=0,
        maximum=1000,
        unit="%",
    ),
    "USE_TRAILING": _field(
        "Breakeven dan Trailing",
        "Aktifkan trailing",
        "Mengikuti kenaikan harga dengan stop dinamis.",
        "bool",
    ),
    "TRAILING_START_PCT": _field(
        "Breakeven dan Trailing",
        "Mulai trailing",
        "Profit untuk mengaktifkan trailing tetap.",
        "float",
        minimum=0,
        maximum=1000,
        unit="%",
    ),
    "TRAILING_STEP_PCT": _field(
        "Breakeven dan Trailing",
        "Jarak trailing",
        "Jarak stop dari harga tertinggi.",
        "float",
        minimum=0.01,
        maximum=100,
        unit="%",
    ),
    "TAKER_FEE_PCT": _field(
        "Fee dan Filter",
        "Fee taker",
        "Asumsi fee order market.",
        "float",
        minimum=0,
        maximum=10,
        unit="%",
    ),
    "MAKER_FEE_PCT": _field(
        "Fee dan Filter",
        "Fee maker",
        "Asumsi fee order limit maker.",
        "float",
        minimum=0,
        maximum=10,
        unit="%",
    ),
    "USE_BNB_FEE_DISCOUNT": _field(
        "Fee dan Filter",
        "Diskon fee BNB",
        "Gunakan asumsi diskon pembayaran fee dengan BNB.",
        "bool",
    ),
    "USE_EQUITY_STOP": _field(
        "Drawdown",
        "Aktifkan equity stop",
        "Menghentikan operasi ketika drawdown maksimum tercapai.",
        "bool",
        dangerous=True,
    ),
    "MAX_DRAWDOWN_PERCENT": _field(
        "Drawdown",
        "Drawdown maksimum",
        "Penurunan dari peak equity sebelum stop.",
        "float",
        minimum=0.01,
        maximum=100,
        unit="%",
        dangerous=True,
    ),
    "CLOSE_ALL_AT_LIMIT": _field(
        "Drawdown",
        "Tutup posisi saat limit",
        "Tutup posisi saat kill switch aktif.",
        "bool",
        dangerous=True,
    ),
    "DD_COOLDOWN_HOURS": _field(
        "Drawdown",
        "Cooldown drawdown",
        "Durasi jeda setelah drawdown stop.",
        "int",
        minimum=1,
        maximum=87600,
        unit="jam",
    ),
    "MAX_CONSECUTIVE_ERRORS": _field(
        "Sistem",
        "Maksimum error beruntun",
        "Bot berhenti setelah error API beruntun.",
        "int",
        minimum=1,
        maximum=100000,
    ),
    "SUPERVISOR_AUTO_RESTART": _field(
        "Sistem",
        "Supervisor auto-restart",
        "Hidupkan kembali bot setelah crash dengan batas percobaan.",
        "bool",
        read_only=True,
    ),
    "SUPERVISOR_MAX_RESTARTS": _field(
        "Sistem",
        "Batas restart supervisor",
        "Maksimum restart dalam satu jendela waktu.",
        "int",
        minimum=0,
        maximum=100,
        read_only=True,
    ),
    "SUPERVISOR_RESTART_WINDOW_SECONDS": _field(
        "Sistem",
        "Jendela restart supervisor",
        "Jendela penghitungan restart berulang.",
        "int",
        minimum=60,
        maximum=86400,
        read_only=True,
    ),
    "SUPERVISOR_RESTART_BACKOFF_SECONDS": _field(
        "Sistem",
        "Backoff restart supervisor",
        "Jeda minimum sebelum bot dihidupkan kembali.",
        "int",
        minimum=1,
        maximum=3600,
        read_only=True,
    ),
    "STATE_FILE": _field(
        "Sistem",
        "File state posisi",
        "Path runtime internal per mode.",
        "str",
        read_only=True,
    ),
    "LOG_FILE": _field(
        "Sistem", "File log", "Path runtime internal per mode.", "str", read_only=True
    ),
    "HEARTBEAT_INTERVAL_SECONDS": _field(
        "Sistem",
        "Interval heartbeat log",
        "Jarak heartbeat di log bot.",
        "int",
        minimum=5,
        maximum=86400,
        unit="detik",
    ),
    "CONTROL_FILE": _field(
        "Sistem",
        "File kontrol",
        "Path komunikasi dashboard ke bot.",
        "str",
        read_only=True,
    ),
    "USE_DUST_SWEEP": _field(
        "Sistem",
        "Konversi dust ke BNB",
        "Konversi dust base asset setelah close di LIVE.",
        "bool",
    ),
    "BASE_URL": _field(
        "Sistem",
        "Base URL aktif",
        "Alias turunan dari LIVE_BASE_URL.",
        "str",
        read_only=True,
    ),
    "BACKTEST_ENTRY_DELAY_BARS": _field(
        "Ukuran Posisi",
        "Latency entry backtest",
        "Jumlah bar tunggu setelah sinyal sebelum simulasi entry.",
        "int",
        minimum=0,
        maximum=10,
        unit="bar",
    ),
    "BACKTEST_ENTRY_SPREAD_PCT": _field(
        "Ukuran Posisi",
        "Spread entry backtest",
        "Total spread bid-ask yang dibebankan pada simulasi entry.",
        "float",
        minimum=0,
        maximum=10,
        unit="%",
    ),
    "BACKTEST_SLIPPAGE_PCT": _field(
        "Ukuran Posisi",
        "Slippage backtest",
        "Slippage adverse per eksekusi backtest.",
        "float",
        minimum=0,
        maximum=10,
        unit="%",
    ),
    "BALANCE_BUFFER_PCT": _field(
        "Ukuran Posisi",
        "Bantalan saldo",
        "Saldo yang tidak dibelanjakan untuk fee dan pergerakan harga.",
        "float",
        minimum=0,
        maximum=50,
        unit="%",
    ),
    "CONFIRM_INTERVAL": _field(
        "Scan",
        "Interval konfirmasi",
        "Interval candle konfirmasi volume rolling.",
        "str",
        editor="select",
        options=[
            "1m",
            "3m",
            "5m",
            "15m",
            "30m",
            "1h",
            "2h",
            "4h",
            "6h",
            "8h",
            "12h",
            "1d",
        ],
    ),
    "CONFIRM_LOOKBACK_BARS": _field(
        "Scan",
        "Jumlah candle konfirmasi",
        "Jumlah candle tertutup untuk konfirmasi volume dan ATR. Limit endpoint klines 1000 per panggilan.",
        "int",
        minimum=3,
        maximum=1000,
        unit="candle",
    ),
    "COOLDOWN_MINUTES_AFTER_CLOSE": _field(
        "Fee dan Filter",
        "Cooldown setelah close",
        "Jeda entry setelah posisi ditutup.",
        "int",
        minimum=0,
        maximum=525600,
        unit="menit",
    ),
    "SAME_COIN_BLOCK_HOURS": _field(
        "Fee dan Filter",
        "Blokir koin sama (jam)",
        "Setelah trade di satu koin ditutup, koin itu tidak boleh di-entry lagi selama sekian jam (default 24 jam = 1 hari). 0 = nonaktif. Berlaku mulai trade berikutnya dan bertahan walau bot di-restart.",
        "float",
        minimum=0,
        maximum=8760,
        unit="jam",
    ),
    "SAME_COIN_BLOCK_LOSS_ONLY": _field(
        "Fee dan Filter",
        "Blokir koin sama hanya saat loss",
        "Jika aktif (default), hanya trade yang rugi yang memicu blokir, sehingga koin yang sudah menghasilkan loss tidak di-entry lagi. Jika nonaktif, semua trade yang ditutup (profit maupun loss) memicu blokir.",
        "bool",
    ),
    "MAX_CHASE_PCT": _field(
        "Fee dan Filter",
        "Batas chase entry",
        "Entry dilewati bila ask sudah melebihi close candle sinyal sebesar persen ini. 0 = nonaktif.",
        "float",
        minimum=0,
        maximum=100,
        unit="%",
        dangerous=True,
    ),
    "MAX_POSITION_USDT": _field(
        "Ukuran Posisi",
        "Plafon posisi",
        "Nol berarti tanpa plafon di PAPER, tetapi dilarang di LIVE.",
        "float",
        minimum=0,
        maximum=1e9,
        unit="USDT",
        dangerous=True,
    ),
    "DEPTH_FILTER_ENABLED": _field(
        "Fee dan Filter",
        "Filter kedalaman order book",
        "Entry ditolak bila total nilai ask dalam rentang harga di bawah terlalu tipis dibanding nilai order. Hanya berlaku di PAPER dan LIVE, tidak di backtest. Data gagal diambil = entry dibatalkan.",
        "bool",
        dangerous=True,
    ),
    "DEPTH_RANGE_PCT": _field(
        "Fee dan Filter",
        "Rentang kedalaman",
        "Rentang harga di atas ask terbaik yang dihitung sebagai kedalaman beli.",
        "float",
        minimum=0.01,
        maximum=10,
        unit="%",
    ),
    "DEPTH_MIN_ASK_NOTIONAL_MULT": _field(
        "Fee dan Filter",
        "Kedalaman minimum (kali nilai order)",
        "Total nilai ask dalam rentang kedalaman minimal sekian kali nilai order.",
        "float",
        minimum=1,
        maximum=1000,
        unit="x",
    ),
    "ORDERBOOK_FILTER_ENABLED": _field(
        "Fee dan Filter",
        "Filter ketimpangan dan sell wall",
        "Entry ditolak bila bid jauh lebih tipis dari ask (tekanan jual) atau ada dinding ask besar di atas harga. Hanya berlaku di PAPER dan LIVE. Data gagal diambil = entry dibatalkan.",
        "bool",
        dangerous=True,
    ),
    "ORDERBOOK_LEVELS": _field(
        "Fee dan Filter",
        "Jumlah level ketimpangan",
        "Jumlah level teratas di sisi bid dan ask untuk menghitung rasio bid banding ask.",
        "int",
        minimum=1,
        maximum=100,
        unit="level",
    ),
    "ORDERBOOK_MIN_BID_ASK_RATIO": _field(
        "Fee dan Filter",
        "Rasio bid banding ask minimum",
        "Entry ditolak bila total nilai bid di level teratas kurang dari rasio ini dikali total nilai ask.",
        "float",
        minimum=0,
        maximum=100,
        unit="x",
    ),
    "SELL_WALL_RANGE_PCT": _field(
        "Fee dan Filter",
        "Rentang deteksi sell wall",
        "Rentang harga di atas ask terbaik untuk mencari dinding ask.",
        "float",
        minimum=0.01,
        maximum=10,
        unit="%",
    ),
    "SELL_WALL_MAX_SHARE_PCT": _field(
        "Fee dan Filter",
        "Porsi maksimum satu level ask",
        "Entry ditolak bila satu level ask di rentang sell wall bernilai lebih dari persen ini dari total ask di rentang itu (minimal 3 level).",
        "float",
        minimum=1,
        maximum=100,
        unit="%",
    ),
    "ORDERBOOK_DEPTH_LIMIT": _field(
        "Fee dan Filter",
        "Jumlah level snapshot order book",
        "Level yang diminta dari Binance saat cek order book. Nilai dibulatkan ke atas ke 100, 500, atau 1000.",
        "int",
        minimum=100,
        maximum=1000,
        unit="level",
    ),
    "MAX_SPREAD_PCT": _field(
        "Fee dan Filter",
        "Spread maksimum",
        "Spread bid-ask maksimum untuk entry.",
        "float",
        minimum=0,
        maximum=100,
        unit="%",
        dangerous=True,
    ),
    "MIN_LISTING_AGE_DAYS": _field(
        "Scan",
        "Usia listing minimum",
        "Pasangan lebih muda akan ditolak.",
        "int",
        minimum=0,
        maximum=36500,
        unit="hari",
    ),
    "MIN_SECONDS_BETWEEN_TRADES": _field(
        "Fee dan Filter",
        "Jarak minimum trade",
        "Jeda keras antartrade.",
        "int",
        minimum=0,
        maximum=31536000,
        unit="detik",
    ),
    "POSITION_SIZE_USDT": _field(
        "Ukuran Posisi",
        "Ukuran posisi tetap",
        "Nominal saat mode persen dimatikan.",
        "float",
        minimum=0.01,
        maximum=1e9,
        unit="USDT",
        dangerous=True,
    ),
    "RISK_PERCENT": _field(
        "Ukuran Posisi",
        "Persen saldo per entry",
        "Persentase saldo bebas yang digunakan.",
        "float",
        minimum=0.01,
        maximum=100,
        unit="%",
        dangerous=True,
    ),
    "ROLLING_VOLUME_CONFIRMATION_BARS": _field(
        "Konfirmasi Volume",
        "Candle volume konfirmasi",
        "Jumlah candle terakhir yang wajib memenuhi lonjakan volume.",
        "int",
        minimum=1,
        maximum=20,
        unit="candle",
    ),
    "ROLLING_VOLUME_FILTER_ENABLED": _field(
        "Konfirmasi Volume",
        "Filter volume rolling",
        "Wajibkan volume candle konfirmasi melampaui rata-rata candle sebelumnya.",
        "bool",
    ),
    "ROLLING_VOLUME_LOOKBACK_BARS": _field(
        "Konfirmasi Volume",
        "Lookback volume rolling",
        "Jumlah candle sebelumnya untuk menghitung rata-rata volume.",
        "int",
        minimum=2,
        maximum=500,
        unit="candle",
    ),
    "ROLLING_VOLUME_SURGE_MULT": _field(
        "Konfirmasi Volume",
        "Pengali volume rolling",
        "Volume candle konfirmasi minimal sekian kali rata-rata sebelumnya.",
        "float",
        minimum=0.1,
        maximum=100,
        unit="x",
    ),
    "DEMAND_ZONE_FILTER_ENABLED": _field(
        "Konfirmasi Demand",
        "Filter zona demand chart",
        "Wajibkan harga berada di area demand (support atau base akumulasi) dengan reaksi dorongan beli yang sah pada candle konfirmasi. Berlaku di LIVE, PAPER, dan backtest.",
        "bool",
    ),
    "DEMAND_LOOKBACK_BARS": _field(
        "Konfirmasi Demand",
        "Lookback zona demand",
        "Jumlah candle tertutup yang memetakan dasar zona demand. Dasarnya dicari di "
        "seluruh jendela ini (swing support), dan hanya candle yang menutup di area "
        "dasar itu yang dihitung sebagai akumulasi; candle yang close-nya sudah "
        "melayang dianggap kaki naik dan tidak ikut jadi dasar. Makin besar nilai ini, "
        "makin jauh ke belakang bot mencari dasarnya, sehingga makin banyak entry yang "
        "dianggap sudah terlalu jauh dari demand (lebih ketat).",
        "int",
        minimum=3,
        maximum=500,
        unit="candle",
    ),
    "DEMAND_ZONE_BUFFER_PCT": _field(
        "Konfirmasi Demand",
        "Lebar zona demand",
        "Tebal zona demand di atas level dasarnya. Berfungsi ganda: menentukan candle "
        "mana yang masih dianggap bagian dari area dasar (candle yang menutup tidak "
        "lebih tinggi dari buffer di atas level dasar) dan menentukan "
        "ketebalan minimum zona bila area dasarnya sendiri sempit.",
        "float",
        minimum=0.05,
        maximum=20,
        unit="%",
    ),
    "DEMAND_MAX_DISTANCE_PCT": _field(
        "Konfirmasi Demand",
        "Jarak maksimum dari zona demand",
        "Batas jarak harga penutupan sinyal di atas BATAS ATAS zona demand "
        "(zone_high), bukan dari dasarnya, agar bot tidak membeli terlalu jauh dari "
        "area demand (anti-pucuk). Toleransi riil dari dasar zona jadi sekitar "
        "DEMAND_ZONE_BUFFER_PCT + DEMAND_MAX_DISTANCE_PCT.",
        "float",
        minimum=0.1,
        maximum=50,
        unit="%",
    ),
    "DEMAND_MIN_CLOSE_POSITION": _field(
        "Konfirmasi Demand",
        "Posisi close minimum pada candle",
        "Posisi penutupan minimum di dalam rentang high-low candle sinyal (0.0 di low, 1.0 di high) sebagai bukti dorongan demand pembeli.",
        "float",
        minimum=0.0,
        maximum=1.0,
    ),
    "HTF_DEMAND_FILTER_ENABLED": _field(
        "Demand H1",
        "Filter zona demand timeframe tinggi",
        "Wajibkan candle timeframe tinggi (TREND_INTERVAL, default 1h) terakhir yang sudah tutup juga bereaksi di dekat zona demand H1, supaya entry M5 tidak terjadi saat harga sedang melayang jauh di atas dasar timeframe tinggi. Memakai candle H1 yang sama dengan gerbang trend (tanpa unduhan tambahan). Berlaku di LIVE, PAPER, dan backtest; gagal ambil data berarti kandidat ditolak (fail closed).",
        "bool",
    ),
    "HTF_DEMAND_LOOKBACK_BARS": _field(
        "Demand H1",
        "Lookback zona demand H1",
        "Jumlah candle H1 tertutup yang memetakan dasar zona demand timeframe tinggi. Default 72 candle = 3 hari struktur harga. Makin besar, makin jauh ke belakang dasarnya dicari dan makin ketat menolak entry yang sudah jauh dari dasar.",
        "int",
        minimum=3,
        maximum=500,
        unit="candle",
    ),
    "HTF_DEMAND_ZONE_BUFFER_PCT": _field(
        "Demand H1",
        "Tebal zona demand H1",
        "Tebal minimum zona demand H1 dalam persen di atas dasar zona. Candle H1 lebih lebar daripada M5, jadi defaultnya lebih tebal (1.5% vs 0.8%).",
        "float",
        minimum=0.0,
        maximum=20.0,
        unit="%",
    ),
    "HTF_DEMAND_MAX_DISTANCE_PCT": _field(
        "Demand H1",
        "Jarak maksimum dari zona H1",
        "Jarak maksimum close candle H1 di atas ATAP zona demand H1, dalam persen. Default 10% (bandingkan 3.5% di M5): longgar untuk pump awal dari dasar H1, tapi menolak entry yang sudah terbang jauh. Harus lebih besar atau sama dengan tebal zona.",
        "float",
        minimum=0.0,
        maximum=50.0,
        unit="%",
    ),
    "HTF_DEMAND_MIN_CLOSE_POSITION": _field(
        "Demand H1",
        "Posisi close minimum candle H1",
        "Posisi penutupan minimum di dalam rentang high-low candle H1 sinyal (0.0 di low, 1.0 di high) sebagai bukti dorongan demand pembeli. Default 0.40, sedikit lebih longgar dari M5 (0.45) karena ekor candle H1 lebih panjang.",
        "float",
        minimum=0.0,
        maximum=1.0,
    ),
    "DAILY_DEMAND_FILTER_ENABLED": _field(
        "Demand Daily",
        "Filter zona demand harian",
        "Wajibkan candle harian (DAILY_DEMAND_INTERVAL, default 1d) terakhir yang sudah tutup juga bereaksi di dekat zona demand harian, supaya entry M5 tidak terjadi saat harga sedang melayang jauh di atas dasar akumulasi harian. Ini lapisan KETIGA setelah zona demand M5 dan zona demand H1. Candle harian diambil langsung dari Binance dan disimpan sehari sekali per simbol; gagal ambil data berarti kandidat ditolak (fail closed).",
        "bool",
    ),
    "DAILY_DEMAND_INTERVAL": _field(
        "Demand Daily",
        "Interval demand harian",
        "Timeframe untuk gerbang demand harian. Wajib kelipatan bulat dari interval konfirmasi dan tidak lebih pendek, supaya backtest bisa merangkai candle ini dari data yang sama. Pilihan dibatasi pada interval yang dikenali aplikasi (12h atau 1d).",
        "str",
        editor="select",
        options=["12h", "1d"],
    ),
    "DAILY_DEMAND_LOOKBACK_BARS": _field(
        "Demand Daily",
        "Lookback zona demand harian",
        "Jumlah candle harian tertutup yang memetakan dasar zona demand harian. Default 20 hari struktur harga. Makin besar, makin jauh ke belakang dasarnya dicari dan makin ketat menolak entry yang sudah jauh dari dasar. Perlu diingat: backtest merangkai candle harian dari candle konfirmasi, jadi lookback besar menambah kebutuhan warmup sekitar (lookback + 1) hari data.",
        "int",
        minimum=3,
        maximum=500,
        unit="candle",
    ),
    "DAILY_DEMAND_ZONE_BUFFER_PCT": _field(
        "Demand Daily",
        "Tebal zona demand harian",
        "Tebal minimum zona demand harian dalam persen di atas dasar zona. Candle harian paling lebar dari semua timeframe, jadi defaultnya paling tebal (2.0%, bandingkan 1.5% di H1 dan 0.8% di M5).",
        "float",
        minimum=0.0,
        maximum=20.0,
        unit="%",
    ),
    "DAILY_DEMAND_MAX_DISTANCE_PCT": _field(
        "Demand Daily",
        "Jarak maksimum dari zona harian",
        "Jarak maksimum close candle harian di atas ATAP zona demand harian, dalam persen. Default 12%: memberi ruang untuk pump yang baru mulai dari dasar harian, tapi menolak entry yang sudah terbang jauh dari area demand hari-hari terakhir. Harus lebih besar atau sama dengan tebal zona.",
        "float",
        minimum=0.0,
        maximum=50.0,
        unit="%",
    ),
    "DAILY_DEMAND_MIN_CLOSE_POSITION": _field(
        "Demand Daily",
        "Posisi close minimum candle harian",
        "Posisi penutupan minimum di dalam rentang high-low candle harian sinyal (0.0 di low, 1.0 di high) sebagai bukti dorongan demand pembeli pada hari itu. Default 0.40, sama seperti H1.",
        "float",
        minimum=0.0,
        maximum=1.0,
    ),
    "DAILY_TREND_FILTER_ENABLED": _field(
        "Trend Harian",
        "Filter EMA + ADX harian",
        "Wajibkan candle harian (DAILY_TREND_INTERVAL, default 1d) terakhir yang sudah tutup berada dalam trend naik dan kuat: close di atas EMA cepat, EMA cepat di atas EMA lambat, dan ADX minimum. Lapisan TAMBAHAN di atas gerbang trend H1 dan gerbang demand. Default nonaktif. Berlaku di LIVE, PAPER, dan backtest; data harian gagal diambil atau riwayatnya kurang berarti kandidat ditolak (fail closed).",
        "bool",
    ),
    "DAILY_TREND_INTERVAL": _field(
        "Trend Harian",
        "Interval trend harian",
        "Timeframe gerbang EMA + ADX harian. Pilihan dibatasi pada 12h atau 1d. Wajib kelipatan bulat dari interval konfirmasi supaya backtest bisa merangkai candle ini dari data yang sama.",
        "str",
        editor="select",
        options=["12h", "1d"],
    ),
    "DAILY_TREND_EMA_FAST": _field(
        "Trend Harian",
        "Periode EMA cepat harian",
        "EMA cepat pada candle harian. Default 20. Wajib lebih kecil dari EMA lambat harian.",
        "int",
        minimum=2,
        maximum=500,
        unit="candle",
    ),
    "DAILY_TREND_EMA_SLOW": _field(
        "Trend Harian",
        "Periode EMA lambat harian",
        "EMA lambat pada candle harian. Default 50. Dipakai juga sebagai penentu panjang riwayat minimum; koin yang riwayat hariannya lebih pendek dari ini ditolak.",
        "int",
        minimum=3,
        maximum=1000,
        unit="candle",
    ),
    "DAILY_TREND_ADX_PERIOD": _field(
        "Trend Harian",
        "Periode ADX harian",
        "Periode ADX Wilder pada candle harian. Default 14. Butuh sekitar dua kali periode ini candle agar nilainya terdefinisi.",
        "int",
        minimum=2,
        maximum=200,
        unit="candle",
    ),
    "DAILY_TREND_ADX_MIN": _field(
        "Trend Harian",
        "Ambang ADX harian",
        "ADX minimum agar trend harian dianggap kuat. Default 20. Isi 0 untuk mematikan cek kekuatan trend dan hanya memakai susunan EMA.",
        "float",
        minimum=0,
        maximum=100,
    ),
    "DAILY_TREND_LOOKBACK_BARS": _field(
        "Trend Harian",
        "Jendela candle trend harian",
        "Jumlah candle harian tertutup yang dipakai menghitung EMA dan ADX harian. Default 120. Nilai ini dipakai sama persis oleh bot live dan backtest; endpoint klines Binance membatasi 1000 candle per panggilan.",
        "int",
        minimum=20,
        maximum=999,
        unit="candle",
    ),
    "TOP_N_CANDIDATES_TO_CONFIRM": _field(
        "Scan",
        "Jumlah kandidat konfirmasi",
        "Berapa kandidat teratas yang diperiksa.",
        "int",
        minimum=1,
        maximum=1000,
    ),
    "TREND_FILTER_ENABLED": _field(
        "Trend H1",
        "Filter trend timeframe tinggi",
        "Wajibkan trend timeframe tinggi (default H1) naik sebelum entry. Data candle trend gagal diambil berarti kandidat ditolak (fail closed).",
        "bool",
    ),
    "TREND_INTERVAL": _field(
        "Trend H1",
        "Interval trend",
        "Timeframe untuk gerbang trend. Wajib kelipatan bulat dari interval konfirmasi supaya backtest bisa merangkai candle ini dari data yang sama.",
        "str",
        editor="select",
        options=["1h", "2h", "4h", "6h", "8h", "12h", "1d"],
    ),
    "TREND_EMA_FAST": _field(
        "Trend H1",
        "Periode EMA cepat",
        "EMA cepat pada timeframe trend. Wajib lebih kecil dari EMA lambat.",
        "int",
        minimum=2,
        maximum=500,
        unit="candle",
    ),
    "TREND_EMA_SLOW": _field(
        "Trend H1",
        "Periode EMA lambat",
        "EMA lambat pada timeframe trend; dipakai juga sebagai penentu panjang riwayat minimum.",
        "int",
        minimum=3,
        maximum=1000,
        unit="candle",
    ),
    "TREND_ADX_PERIOD": _field(
        "Trend H1",
        "Periode ADX",
        "Periode ADX Wilder pada timeframe trend. Butuh sekitar dua kali periode ini candle agar nilainya terdefinisi.",
        "int",
        minimum=2,
        maximum=200,
        unit="candle",
    ),
    "TREND_ADX_MIN": _field(
        "Trend H1",
        "Ambang ADX minimum",
        "ADX minimum agar trend dianggap kuat. Isi 0 untuk mematikan cek kekuatan trend dan hanya memakai susunan EMA.",
        "float",
        minimum=0,
        maximum=100,
    ),
    "TREND_LOOKBACK_BARS": _field(
        "Trend H1",
        "Jendela candle trend",
        "Jumlah candle tertutup timeframe trend yang dipakai menghitung EMA dan ADX. Nilai ini dipakai sama persis oleh bot live dan backtest; endpoint klines Binance membatasi 1000 candle per panggilan.",
        "int",
        minimum=20,
        maximum=999,
        unit="candle",
    ),
    "USE_RISK_PERCENT": _field(
        "Ukuran Posisi",
        "Gunakan persen risiko",
        "Ukuran posisi dihitung dari saldo bebas.",
        "bool",
        dangerous=True,
    ),
    # ------------------------------------------------------------ Tampilan IDR
    # Satu grup khusus untuk lapisan tampilan rupiah. Semua field di grup ini
    # hanya dibaca dashboard; bot, sizing, order, dan backtest tidak memakainya.
    # Nilai kurs selalu ditampilkan sebagai pelengkap: angka USDT tetap menjadi
    # angka utama, rupiah menyusul di baris sub judul.
    "IDR_DISPLAY_ENABLED": _field(
        "Tampilan IDR",
        "Tampilkan nilai rupiah",
        "Menampilkan sub judul rupiah (kurs USDT/IDR) di kartu equity, PnL, dan tabel riwayat trade. Tidak memengaruhi trading.",
        "bool",
    ),
    "IDR_RATE_MODE": _field(
        "Tampilan IDR",
        "Sumber kurs",
        "AUTO mengambil kurs dari pair spot Binance (bawaan USDTIDR, aktif sejak November 2025). MANUAL memakai angka tetap pada IDR_RATE_MANUAL.",
        "str",
        editor="select",
        options=["AUTO", "MANUAL"],
    ),
    "IDR_RATE_MANUAL": _field(
        "Tampilan IDR",
        "Kurs manual",
        "Jumlah rupiah untuk 1 USDT saat sumber kurs MANUAL. Dipakai juga sebagai cadangan terakhir bila mode AUTO sedang tidak bisa menghubungi bursa. Isi 0 untuk menonaktifkan cadangan manual.",
        "float",
        minimum=0,
        maximum=1_000_000_000,
        unit="IDR/USDT",
    ),
    "IDR_RATE_SYMBOL": _field(
        "Tampilan IDR",
        "Pair sumber kurs",
        "Pair Binance yang harganya dipakai sebagai kurs. USDTIDR adalah pasangan resmi rupiah untuk Tether; ganti hanya bila user memakai pair lain seperti USDCIDR.",
        "str",
        editor="select",
        options=["USDTIDR", "USDCIDR"],
    ),
    "IDR_RATE_REFRESH_SECONDS": _field(
        "Tampilan IDR",
        "Interval segarkan kurs",
        "Jeda minimum antar pengambilan kurs dari bursa. Satu request ticker berbobot kecil, tetapi nilai di bawah 15 detik tidak diizinkan agar limit IP bersama bot tetap aman.",
        "int",
        minimum=15,
        maximum=86400,
        unit="detik",
    ),
    "IDR_RATE_MAX_AGE_SECONDS": _field(
        "Tampilan IDR",
        "Ambang kurs basi",
        "Umur kurs saat penanda basi mulai muncul di dashboard. Kurs lama tetap ditampilkan agar tidak ada angka menyesatkan, tetapi diberi tanda.",
        "int",
        minimum=60,
        maximum=604800,
        unit="detik",
    ),
    "IDR_RATE_STATE_FILE": _field(
        "Tampilan IDR",
        "Berkas cache kurs",
        "Berkas JSON tempat kurs terakhir disimpan, supaya rupiah tetap tampil saat bursa tidak bisa dihubungi atau setelah dashboard dijalankan ulang.",
        "str",
    ),
}


def _error_marker(path: Path, message: str, backup: Path | None = None) -> None:
    atomic_write_json(
        path,
        {
            "error": message,
            "backup": str(backup) if backup else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _clear_error_marker() -> bool:
    try:
        SETTINGS_ERROR_FILE.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _read_doc_unlocked() -> tuple[dict, list[str]]:
    errors: list[str] = []
    if SETTINGS_ERROR_FILE.exists():
        try:
            marker = read_json(SETTINGS_ERROR_FILE, {}) or {}
            errors.append(
                str(marker.get("error") or "File settings runtime sebelumnya rusak.")
            )
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            errors.append(f"Marker error settings tidak dapat dibaca: {exc}")
    if not SETTINGS_FILE.exists():
        return {}, errors
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("root settings harus object JSON")
        unknown_root = sorted(set(data) - {"active_mode", "overrides"})
        if unknown_root:
            raise ValueError("kunci root tidak dikenal: " + ", ".join(unknown_root))
        if "active_mode" in data and not isinstance(data["active_mode"], str):
            raise ValueError("active_mode lama harus berupa string bila masih ada")
        overrides = data.get("overrides")
        if overrides is None:
            overrides = {}
        if not isinstance(overrides, dict):
            raise ValueError("overrides harus object")
        bad_modes = sorted(str(m) for m in overrides if m not in VALID_MODES)
        if bad_modes:
            raise ValueError("mode override tidak dikenal: " + ", ".join(bad_modes))
        for mode, payload in overrides.items():
            if not isinstance(payload, dict):
                raise ValueError(f"override {mode} harus object")
        data["overrides"] = overrides
        if errors and _clear_error_marker():
            errors = []
        return data, errors
    except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
        backup = archive_corrupt(SETTINGS_FILE)
        message = f"File settings runtime rusak: {exc}. Cadangan: {backup}"
        _error_marker(SETTINGS_ERROR_FILE, message, backup)
        return {}, errors + [message]


REMOVED_PARAMETERS = frozenset(
    {
        "MIN_CLOSE_POSITION_IN_RANGE",
        "SWING_LOOKBACK_BARS",
        "SWING_PIVOT_WING_BARS",
        "VWAP_MIN_BARS_AFTER_ANCHOR",
        "MAX_BARS_BREAKOUT_TO_RETEST",
        "MAX_RETEST_TOUCHES",
        "WATCHLIST_ENTRY_WEIGHT_EMA",
        "WATCHLIST_ENTRY_WEIGHT_RSI",
        "WATCHLIST_ENTRY_WEIGHT_MACD",
        "WATCHLIST_ENTRY_WEIGHT_HL",
        "WATCHLIST_ENTRY_EMA_GAP_PCT",
        "WATCHLIST_ENTRY_RSI_DECAY_PTS",
        "WATCHLIST_ENTRY_SCORE_TTL_SECONDS",
        "WATCHLIST_ENTRY_MIN_HEADROOM",
        "WATCHLIST_ENABLED",
        "WATCHLIST_TOP_N",
        "PUMP_VOLUME_SURGE_MULT",
        "DETECTOR_WEIGHT_VOLUME24",
        "DETECTOR_TOP_N",
        "DAILY_KLINE_CACHE_TTL_SECONDS",
        # Stop harian dihapus total: kunci lama di settings.json dibuang diam-diam.
        "USE_DAILY_STOP",
        "MAX_DAILY_LOSS_PERCENT",
        "DAILY_PROFIT_TARGET_PERCENT",
        # Remove obsolete detector-score settings from older mode overrides.
        "DETECTOR_ENABLED",
        "DETECTOR_WEIGHT_CHANGE",
        "DETECTOR_WEIGHT_VOLUME5M",
        "DETECTOR_WEIGHT_ORDERBOOK",
        "DETECTOR_WEIGHT_ATR",
        "DETECTOR_ATR_MIN_PCT",
        "DETECTOR_ATR_MAX_PCT",
    }
)


def _validate_override_payload(payload: dict) -> None:
    unknown = sorted(set(payload) - set(PARAMETER_SCHEMA))
    if unknown:
        raise ValueError("kunci override tidak dikenal: " + ", ".join(unknown))
    forbidden = [k for k in payload if PARAMETER_SCHEMA[k]["read_only"]]
    forbidden = [k for k in forbidden if k != "PAPER_INITIAL_BALANCES"]
    if forbidden:
        raise ValueError("override memuat kunci read-only: " + ", ".join(forbidden))


def load_mode_override(mode: str) -> tuple[dict, list[str]]:
    raw_mode = str(mode).strip().upper()
    if raw_mode not in VALID_MODES:
        raise ValueError("Mode override tidak valid.")
    with interprocess_lock(SETTINGS_FILE):
        doc, errors = _read_doc_unlocked()
        payload = doc.get("overrides", {}).get(raw_mode)
        if payload is None:
            return {}, errors
        payload = {k: v for k, v in payload.items() if k not in REMOVED_PARAMETERS}
        try:
            _validate_override_payload(payload)
        except ValueError as exc:
            backup = archive_corrupt(SETTINGS_FILE)
            message = f"Override {raw_mode} rusak: {exc}. Cadangan: {backup}"
            _error_marker(SETTINGS_ERROR_FILE, message, backup)
            return {}, errors + [message]
        return dict(payload), errors


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
    elif key == "EXTRA_EXCLUDE_SYMBOLS":
        result = _validate_symbol_list(
            value, str(candidate.get("QUOTE_ASSET", "USDT")).upper()
        )
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


def _angka_untuk_relasi(cfg: dict, key: str) -> float | None:
    """Ambil angka untuk aturan silang. None berarti kuncinya tidak bisa dipakai."""
    if key not in cfg or cfg[key] is None:
        return None
    try:
        nilai = float(cfg[key])
    except (TypeError, ValueError):
        return None
    if nilai != nilai or nilai in (float("inf"), float("-inf")):
        return None
    return nilai


def _flag_untuk_relasi(cfg: dict, key: str, default: bool) -> bool:
    if key not in cfg or cfg[key] is None:
        return default
    return bool(cfg[key])


def relation_violations(cfg: dict, *, mode_aware: bool = False) -> dict[str, str]:
    """Satu sumber kebenaran untuk aturan silang antar parameter.

    Semua pemanggil (penyimpanan setting di dashboard, validasi parameter
    backtest, dan penyaring kombinasi grid search) wajib lewat sini supaya
    ketiganya tidak bisa berbeda pendapat lagi tentang konfigurasi yang sama.

    mode_aware=False : dipakai penyimpanan setting. Relasi exit dan demand dicek
        apa pun mode yang sedang aktif, karena mode bisa dipindah kapan saja dan
        kombinasi yang terbalik harus ketahuan sebelum disimpan.
    mode_aware=True  : dipakai jalur simulasi. Hanya relasi dari mode aktif yang
        dicek, supaya kombinasi grid yang sedang menyetel parameter nonaktif tidak
        dibatalkan tanpa sebab.

    Kunci yang tidak ada di `cfg` membuat aturannya dilewati, jadi fungsi ini aman
    untuk config sebagian (misalnya cfg milik test atau grid).
    """

    def ambil(*keys: str) -> "list[float] | None":
        nilai = [_angka_untuk_relasi(cfg, k) for k in keys]
        return None if any(v is None for v in nilai) else nilai

    def pasangan(kiri: str, kanan: str) -> "tuple[float, float] | None":
        nilai = ambil(kiri, kanan)
        return None if nilai is None else (nilai[0], nilai[1])

    keluar: dict[str, str] = {}
    atr_aktif = _flag_untuk_relasi(cfg, "USE_ATR_EXIT", False)
    cek_exit_atr = mode_aware is False or atr_aktif
    cek_pct = mode_aware is False or not atr_aktif

    if cek_exit_atr:
        for kunci, pesan in (
            (
                ("ATR_MULT_TRAIL", "ATR_MULT_SL"),
                "tidak boleh melebihi ATR_MULT_SL agar invariant trailing <= SL terjaga",
            ),
            (
                ("ATR_MULT_BE_TRIGGER", "ATR_MULT_TRAIL_START"),
                "tidak boleh melebihi trigger trailing",
            ),
            (
                ("ATR_MULT_BE_LOCK", "ATR_MULT_BE_TRIGGER"),
                "tidak boleh melebihi trigger breakeven",
            ),
        ):
            nilai = pasangan(*kunci)
            if nilai is not None and nilai[0] > nilai[1]:
                keluar[kunci[0]] = pesan
        nilai = pasangan("ATR_MULT_TP", "ATR_MULT_SL")
        if nilai is not None and nilai[0] <= nilai[1]:
            keluar["ATR_MULT_TP"] = (
                "harus lebih besar dari ATR_MULT_SL agar rasio risk-reward tidak terbalik"
            )

    if cek_pct:
        tp_on = _flag_untuk_relasi(cfg, "USE_TP", True)
        sl_on = _flag_untuk_relasi(cfg, "USE_STOP_LOSS", True)
        if tp_on and sl_on:
            nilai = pasangan("TP_PCT", "SL_PCT")
            if nilai is not None and nilai[0] <= nilai[1]:
                keluar["TP_PCT"] = (
                    "harus lebih besar dari SL_PCT agar rasio risk-reward tidak terbalik"
                )
        if sl_on:
            nilai = _angka_untuk_relasi(cfg, "SL_PCT")
            if nilai is not None and nilai <= 0:
                keluar["SL_PCT"] = "harus lebih besar dari nol saat Stop Loss aktif"
        if tp_on:
            nilai = _angka_untuk_relasi(cfg, "TP_PCT")
            if nilai is not None and nilai <= 0:
                keluar["TP_PCT"] = "harus lebih besar dari nol saat Take Profit aktif"

    nilai = pasangan("PUMP_MAX_24H_CHANGE_PCT", "PUMP_MIN_24H_CHANGE_PCT")
    if nilai is not None and nilai[0] != 0 and nilai[0] <= nilai[1]:
        keluar["PUMP_MAX_24H_CHANGE_PCT"] = (
            "harus lebih besar dari PUMP_MIN_24H_CHANGE_PCT (atau 0 untuk menonaktifkan)"
        )

    # Tampilan IDR: mode MANUAL tanpa angka kurs hanya menghasilkan dashboard
    # tanpa elemen rupiah sama sekali, jadi lebih baik ditolak saat disimpan
    # daripada baru ketahuan saat halaman dibuka. Relasi ini dicek di semua
    # jalur karena sifatnya statis (tidak bergantung mode PAPER/LIVE).
    if "IDR_RATE_MODE" in cfg and "IDR_RATE_MANUAL" in cfg:
        mode_kurs = str(cfg.get("IDR_RATE_MODE", "AUTO") or "AUTO").strip().upper()
        nilai_kurs = _angka_untuk_relasi(cfg, "IDR_RATE_MANUAL")
        if mode_kurs == "MANUAL" and nilai_kurs is not None and nilai_kurs <= 0:
            keluar["IDR_RATE_MANUAL"] = (
                "wajib diisi lebih besar dari nol saat IDR_RATE_MODE = MANUAL"
            )

    saring_trend = mode_aware is False or _flag_untuk_relasi(
        cfg, "TREND_FILTER_ENABLED", False
    )
    if saring_trend:
        nilai = pasangan("TREND_EMA_SLOW", "TREND_EMA_FAST")
        if nilai is not None and nilai[0] <= nilai[1]:
            keluar["TREND_EMA_SLOW"] = (
                "harus lebih besar dari TREND_EMA_FAST agar susunan EMA tidak terbalik"
            )

    try:
        from strategy.indicators import INTERVAL_MINUTES as _INTERVAL_MINUTES

        menit_trend = _INTERVAL_MINUTES.get(str(cfg.get("TREND_INTERVAL", "")).lower())
        menit_konfirmasi = _INTERVAL_MINUTES.get(
            str(cfg.get("CONFIRM_INTERVAL", "")).lower()
        )
    except ImportError:  # strategi belum tersedia saat skema dimuat sendiri
        menit_trend = menit_konfirmasi = None
    if menit_trend and menit_konfirmasi:
        if menit_trend % menit_konfirmasi != 0 or menit_trend < menit_konfirmasi:
            keluar["TREND_INTERVAL"] = (
                "harus kelipatan bulat dari CONFIRM_INTERVAL dan tidak lebih pendek, "
                "supaya backtest bisa merangkai candle trend dari candle konfirmasi"
            )

    try:
        from strategy.indicators import INTERVAL_MINUTES as _INTERVAL_MINUTES_HARIAN

        menit_harian = _INTERVAL_MINUTES_HARIAN.get(
            str(cfg.get("DAILY_DEMAND_INTERVAL", "")).lower()
        )
        menit_konfirmasi_harian = _INTERVAL_MINUTES_HARIAN.get(
            str(cfg.get("CONFIRM_INTERVAL", "")).lower()
        )
    except ImportError:  # strategi belum tersedia saat skema dimuat sendiri
        menit_harian = menit_konfirmasi_harian = None
    if menit_harian and menit_konfirmasi_harian:
        if menit_harian % menit_konfirmasi_harian != 0 or (
            menit_harian < menit_konfirmasi_harian
        ):
            keluar["DAILY_DEMAND_INTERVAL"] = (
                "harus kelipatan bulat dari CONFIRM_INTERVAL dan tidak lebih pendek, "
                "supaya backtest bisa merangkai candle harian dari candle konfirmasi"
            )

    saring_demand = mode_aware is False or _flag_untuk_relasi(
        cfg, "DEMAND_ZONE_FILTER_ENABLED", False
    )
    if saring_demand:
        nilai = pasangan("DEMAND_MAX_DISTANCE_PCT", "DEMAND_ZONE_BUFFER_PCT")
        if nilai is not None and nilai[0] < nilai[1]:
            keluar["DEMAND_MAX_DISTANCE_PCT"] = (
                "harus lebih besar atau sama dengan DEMAND_ZONE_BUFFER_PCT"
            )

    saring_htf_demand = mode_aware is False or _flag_untuk_relasi(
        cfg, "HTF_DEMAND_FILTER_ENABLED", False
    )
    if saring_htf_demand:
        nilai = pasangan("HTF_DEMAND_MAX_DISTANCE_PCT", "HTF_DEMAND_ZONE_BUFFER_PCT")
        if nilai is not None and nilai[0] < nilai[1]:
            keluar["HTF_DEMAND_MAX_DISTANCE_PCT"] = (
                "harus lebih besar atau sama dengan HTF_DEMAND_ZONE_BUFFER_PCT"
            )

    saring_daily_trend = mode_aware is False or _flag_untuk_relasi(
        cfg, "DAILY_TREND_FILTER_ENABLED", False
    )
    if saring_daily_trend:
        nilai = pasangan("DAILY_TREND_EMA_SLOW", "DAILY_TREND_EMA_FAST")
        if nilai is not None and nilai[0] <= nilai[1]:
            keluar["DAILY_TREND_EMA_SLOW"] = (
                "harus lebih besar dari DAILY_TREND_EMA_FAST agar susunan EMA harian tidak terbalik"
            )
        try:
            from strategy.indicators import INTERVAL_MINUTES as _INTERVAL_MINUTES_TREN_HARIAN

            menit_tren_harian = _INTERVAL_MINUTES_TREN_HARIAN.get(
                str(cfg.get("DAILY_TREND_INTERVAL", "")).lower()
            )
            menit_konfirmasi_tren_harian = _INTERVAL_MINUTES_TREN_HARIAN.get(
                str(cfg.get("CONFIRM_INTERVAL", "")).lower()
            )
        except ImportError:  # strategi belum tersedia saat skema dimuat sendiri
            menit_tren_harian = menit_konfirmasi_tren_harian = None
        if menit_tren_harian and menit_konfirmasi_tren_harian:
            if menit_tren_harian % menit_konfirmasi_tren_harian != 0 or (
                menit_tren_harian < menit_konfirmasi_tren_harian
            ):
                keluar["DAILY_TREND_INTERVAL"] = (
                    "harus kelipatan bulat dari CONFIRM_INTERVAL dan tidak lebih pendek, "
                    "supaya backtest bisa merangkai candle harian dari candle konfirmasi"
                )

    saring_daily_demand = mode_aware is False or _flag_untuk_relasi(
        cfg, "DAILY_DEMAND_FILTER_ENABLED", False
    )
    if saring_daily_demand:
        nilai = pasangan("DAILY_DEMAND_MAX_DISTANCE_PCT", "DAILY_DEMAND_ZONE_BUFFER_PCT")
        if nilai is not None and nilai[0] < nilai[1]:
            keluar["DAILY_DEMAND_MAX_DISTANCE_PCT"] = (
                "harus lebih besar atau sama dengan DAILY_DEMAND_ZONE_BUFFER_PCT"
            )

    return keluar


def validate_candidate(
    candidate: dict, mode: str
) -> tuple[dict, dict[str, str], list[str]]:
    cleaned = deepcopy(candidate)
    errors: dict[str, str] = {}
    warnings: list[str] = []
    normalized_mode = str(mode).strip().upper()
    if normalized_mode not in VALID_MODES:
        errors["MODE"] = "mode valid hanya PAPER atau LIVE"

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
        relation(
            "MODE",
            str(cleaned.get("MODE", "")).strip().upper() == normalized_mode,
            "MODE pada konfigurasi tidak sama dengan mode yang sedang divalidasi",
        )
        # Semua aturan silang antar parameter diambil dari satu sumber yang sama
        # dengan validasi parameter backtest dan penyaring kombinasi grid search.
        # Di jalur penyimpanan setting, mode_aware dimatikan sehingga kombinasi
        # exit yang terbalik tetap ditolak walau mode exit-nya sedang tidak aktif:
        # mode bisa dipindah kapan saja dan settings.json tidak boleh menyimpan
        # nilai yang begitu dipindah langsung rusak.
        for kunci_relasi, pesan_relasi in relation_violations(cleaned).items():
            relation(kunci_relasi, False, pesan_relasi)

        if cleaned["TREND_FILTER_ENABLED"]:
            _minimal = max(
                int(cleaned["TREND_EMA_SLOW"]), 2 * int(cleaned["TREND_ADX_PERIOD"]) + 1
            )
            if int(cleaned["TREND_LOOKBACK_BARS"]) < _minimal:
                warnings.append(
                    f"TREND_LOOKBACK_BARS {cleaned['TREND_LOOKBACK_BARS']} lebih kecil dari "
                    f"{_minimal} candle yang dibutuhkan EMA dan ADX. Gerbang trend akan "
                    "memakai nilai minimum itu dan menolak entry selama riwayat belum cukup."
                )
            if cleaned["TREND_ADX_MIN"] == 0:
                warnings.append(
                    "TREND_ADX_MIN nol: cek kekuatan trend mati, hanya susunan EMA yang "
                    "menyaring entry sehingga pasar sideways lebih mudah diloloskan."
                )
        if cleaned.get("DAILY_TREND_FILTER_ENABLED"):
            _minimal_harian = max(
                int(cleaned["DAILY_TREND_EMA_SLOW"]),
                2 * int(cleaned["DAILY_TREND_ADX_PERIOD"]) + 1,
            )
            if int(cleaned["DAILY_TREND_LOOKBACK_BARS"]) < _minimal_harian:
                warnings.append(
                    f"DAILY_TREND_LOOKBACK_BARS {cleaned['DAILY_TREND_LOOKBACK_BARS']} lebih "
                    f"kecil dari {_minimal_harian} candle yang dibutuhkan EMA dan ADX harian. "
                    "Gerbang akan memakai nilai minimum itu dan menolak entry selama riwayat belum cukup."
                )
            warnings.append(
                f"Filter EMA + ADX harian aktif: koin yang riwayat harian tertutupnya kurang dari "
                f"{max(int(cleaned['DAILY_TREND_EMA_SLOW']), 2 * int(cleaned['DAILY_TREND_ADX_PERIOD']) + 1)} "
                "hari (termasuk koin baru listing) akan ditolak sampai riwayatnya cukup."
            )
            if cleaned["DAILY_TREND_ADX_MIN"] == 0:
                warnings.append(
                    "DAILY_TREND_ADX_MIN nol: cek kekuatan trend harian mati, hanya susunan EMA "
                    "yang menyaring entry."
                )
        if cleaned["PUMP_MIN_24H_CHANGE_PCT"] <= 0:
            warnings.append(
                "PUMP_MIN_24H_CHANGE_PCT nol atau kurang: gerbang kenaikan 24 jam "
                "praktis mati dan koin yang turun ikut menjadi kandidat."
            )
        if cleaned["PUMP_MIN_24H_CHANGE_PCT"] >= 50:
            warnings.append(
                "PUMP_MIN_24H_CHANGE_PCT sangat tinggi, kandidat bisa nol "
                "untuk waktu yang lama."
            )

        relation(
            "RATE_LIMIT_SAFETY_MARGIN",
            int(cleaned.get("RATE_LIMIT_SAFETY_MARGIN", 0))
            < int(cleaned.get("RATE_LIMIT_WEIGHT_LIMIT", 0)),
            "harus lebih kecil dari RATE_LIMIT_WEIGHT_LIMIT",
        )

        if normalized_mode == "LIVE":
            relation(
                "LIVE_BASE_URL",
                str(cleaned.get("LIVE_BASE_URL", "")).rstrip("/")
                == "https://api.binance.com",
                "mode LIVE hanya boleh memakai endpoint produksi resmi https://api.binance.com",
            )
            relation(
                "MAX_POSITION_USDT",
                float(cleaned["MAX_POSITION_USDT"]) > 0,
                "mode LIVE wajib memiliki plafon posisi lebih besar dari nol",
            )
            relation(
                "USE_STOP_LOSS",
                bool(cleaned["USE_STOP_LOSS"]),
                "mode LIVE wajib memakai Stop Loss",
            )
            native_stop_available = bool(cleaned["USE_NATIVE_STOP_LOSS"])
            native_oco_available = bool(cleaned["USE_NATIVE_OCO"] and cleaned["USE_TP"])
            relation(
                "USE_NATIVE_STOP_LOSS",
                native_stop_available or native_oco_available,
                "mode LIVE wajib memiliki minimal satu proteksi exchange-side: native stop atau OCO",
            )
            if cleaned["USE_NATIVE_OCO"]:
                relation(
                    "USE_TP",
                    bool(cleaned["USE_TP"]),
                    "USE_NATIVE_OCO memerlukan Take Profit aktif",
                )
                relation(
                    "USE_STOP_LOSS",
                    bool(cleaned["USE_STOP_LOSS"]),
                    "USE_NATIVE_OCO memerlukan Stop Loss aktif",
                )
            if cleaned["USE_NATIVE_STOP_LOSS"]:
                relation(
                    "USE_STOP_LOSS",
                    bool(cleaned["USE_STOP_LOSS"]),
                    "USE_NATIVE_STOP_LOSS memerlukan Stop Loss aktif",
                )

        if not cleaned["USE_EQUITY_STOP"]:
            pesan = (
                "USE_EQUITY_STOP nonaktif: "
                "tidak ada rem kerugian tingkat akun sama sekali."
            )
            if cleaned.get("CLOSE_ALL_AT_LIMIT"):
                pesan += (
                    " CLOSE_ALL_AT_LIMIT bernilai aktif tetapi TIDAK AKAN "
                    "PERNAH terpicu karena tidak ada limit yang bisa tercapai."
                )
            pesan += " Mode LIVE akan menolak Resume Bot dengan kombinasi ini."
            warnings.append(pesan)

        if cleaned["USE_STOP_LOSS"] and cleaned["SL_PCT"] >= 20:
            warnings.append(
                f"SL_PCT {cleaned['SL_PCT']:g}% sangat lebar: satu posisi dapat rugi "
                f"sekitar {cleaned['SL_PCT']:g}% dari nilai posisi sebelum stop bekerja. "
                "Pastikan ukuran posisi memang sekecil itu relatif terhadap modal."
            )

    return cleaned, errors, warnings
