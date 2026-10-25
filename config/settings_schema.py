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
        "File dasar ledger REQUEST_WEIGHT API dan SAPI per endpoint lintas proses.",
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
    "CONFLUENCE_SL_BUFFER_PCT": _field(
        "SL dan TP",
        "Buffer SL Confluence",
        "Buffer tambahan di bawah struktur Order Block / POI / Swing Low / Sweep Low untuk mengunci level Stop Loss struktural.",
        "float",
        minimum=0.0,
        maximum=10.0,
        unit="%",
    ),
    "CONFLUENCE_MIN_RR_RATIO": _field(
        "SL dan TP",
        "Minimum Risk:Reward Confluence",
        "Rasio imbal hasil terhadap risiko minimum (target BSL $$$ dibagi jarak SL struktural) agar setup diizinkan entry.",
        "float",
        minimum=0.1,
        maximum=20.0,
        unit="x",
    ),
    "MAX_HOLD_HOURS": _field(
        "SL dan TP",
        "Batas waktu tahan (time stop)",
        "Tutup posisi otomatis saat umurnya mencapai batas ini. 0 = nonaktif. "
        "Exit harga (Stop Loss struktural dan Take Profit BSL $$$) tetap diprioritaskan "
        "pada bar yang sama, jadi time stop hanya menutup posisi yang mengendap tanpa arah.",
        "float",
        minimum=0,
        maximum=720,
        unit="jam",
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
    "USE_NATIVE_OCO": _field(
        "SL dan TP",
        "OCO exchange-side LIVE",
        "Pasang OCO SELL native berisi TP limit (BSL $$$) dan SL limit struktural pada posisi yang terdeteksi.",
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
    "TRADE_LEDGER_FILE": _field(
        "Sistem",
        "Ledger trade",
        "Riwayat fill dan PnL per mode yang digunakan dashboard.",
        "str",
        read_only=True,
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
        "Interval candle konfirmasi untuk pemicu eksekusi entry M5. CONFLUENCE_STRUCTURE_INTERVAL wajib kelipatan bulat dari nilai ini dan tidak boleh lebih pendek agar candle struktur POI dapat dirangkai tanpa look-ahead.",
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
        "Jumlah candle tertutup untuk pemicu eksekusi The Confluence Code. Limit endpoint klines 1000 per panggilan.",
        "int",
        minimum=15,
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
        "Nominal posisi sebagai persentase saldo bebas, BUKAN risiko per trade. "
        "Kerugian rupiah saat stop loss kena = nominal x jarak stop loss. Nilai 100 "
        "berarti satu posisi memakai hampir seluruh saldo bebas.",
        "float",
        minimum=0.01,
        maximum=100,
        unit="%",
        dangerous=True,
    ),
    "TOP_N_CANDIDATES_TO_CONFIRM": _field(
        "Scan",
        "Jumlah kandidat konfirmasi",
        "Berapa kandidat teratas yang diperiksa.",
        "int",
        minimum=1,
        maximum=1000,
    ),
    "CONFLUENCE_FILTER_ENABLED": _field(
        "Confluence Code",
        "Filter The Confluence Code",
        "Aktifkan gerbang entry The Confluence Code (Volume Profile -> Fibonacci -> Liquidity -> FVG -> Order Block). Jika nonaktif, kandidat yang lolos seleksi 24 jam dan proteksi orderbook langsung memenuhi syarat entry.",
        "bool",
    ),
    "CONFLUENCE_STRUCTURE_INTERVAL": _field(
        "Confluence Code",
        "Interval struktur POI Confluence",
        "Timeframe candle tertutup untuk memetakan struktur impuls BoS, Volume Profile, Fibonacci, FVG, dan Order Block (default 15m).",
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
    "CONFLUENCE_LOOKBACK_BARS": _field(
        "Confluence Code",
        "Lookback struktur Confluence",
        "Jumlah candle tertutup yang dianalisis untuk mencari struktur impuls BoS dan zona POI.",
        "int",
        minimum=15,
        maximum=500,
        unit="candle",
    ),
    "CONFLUENCE_MIN_IMPULSE_PCT": _field(
        "Confluence Code",
        "Impuls minimum BoS",
        "Kenaikan persentase minimum dari Swing Low (1) ke Swing High (0) yang menembus struktur tertinggi sebelumnya (Break of Structure).",
        "float",
        minimum=0.1,
        maximum=100.0,
        unit="%",
    ),
    "CONFLUENCE_ZONE_TOLERANCE_RATIO": _field(
        "Confluence Code",
        "Toleransi klaster POI",
        "Toleransi lebar klaster terhadap rentang impuls untuk menyatukan irisan Volume Profile, Fibonacci, FVG, dan Order Block.",
        "float",
        minimum=0.0,
        maximum=0.50,
        unit="rasio",
    ),
    "VP_FILTER_ENABLED": _field(
        "Confluence Code",
        "1. Volume Profile (HVN/POC)",
        "Wajibkan terdapat High Volume Node (HVN) atau Point of Control (POC) dari Fixed Range Volume Profile di zona diskon retracement.",
        "bool",
    ),
    "VP_BINS": _field(
        "Confluence Code",
        "Jumlah bin Volume Profile",
        "Jumlah pembagian level harga (price bins) pada Fixed Range Volume Profile di rentang impuls.",
        "int",
        minimum=5,
        maximum=100,
        unit="bin",
    ),
    "VP_HVN_TOP_RATIO": _field(
        "Confluence Code",
        "Rasio HVN Volume Profile",
        "Proporsi bin ber-volume tertinggi yang dikategorikan sebagai High Volume Node (misal 0.40 = 40% teratas).",
        "float",
        minimum=0.05,
        maximum=1.0,
        unit="rasio",
    ),
    "FIB_FILTER_ENABLED": _field(
        "Confluence Code",
        "2. Fibonacci Retracement",
        "Wajibkan harga retracement dan zona POI berada di area diskon Fibonacci dari kaki impuls BoS.",
        "bool",
    ),
    "FIB_RETRACEMENT_MIN": _field(
        "Confluence Code",
        "Batas atas Fibonacci (Min)",
        "Level retracement Fibonacci minimum untuk zona diskon (default 0.50).",
        "float",
        minimum=0.10,
        maximum=0.95,
        unit="rasio",
    ),
    "FIB_RETRACEMENT_MAX": _field(
        "Confluence Code",
        "Batas bawah Fibonacci (Max)",
        "Level retracement Fibonacci maksimum untuk zona diskon (default 0.786).",
        "float",
        minimum=0.15,
        maximum=0.99,
        unit="rasio",
    ),
    "LIQUIDITY_FILTER_ENABLED": _field(
        "Confluence Code",
        "3. Liquidity Sweep (SSL/BSL)",
        "Wajibkan terbentuk Buy-Side Liquidity (BSL) di puncak impuls serta Sell-Side Liquidity (SSL) internal yang tersapu menuju zona POI.",
        "bool",
    ),
    "LIQUIDITY_SWEEP_LOOKBACK_BARS": _field(
        "Confluence Code",
        "Lookback sapuan likuiditas",
        "Jumlah candle terakhir pada timeframe konfirmasi untuk mendeteksi sapuan SSL dan sentuhan zona POI.",
        "int",
        minimum=1,
        maximum=30,
        unit="candle",
    ),
    "FVG_FILTER_ENABLED": _field(
        "Confluence Code",
        "4. Bullish Fair Value Gap (FVG)",
        "Wajibkan terdapat celah ketidakseimbangan harga bullish (candle[i-1].high < candle[i+1].low) yang belum tertembus penuh di zona diskon.",
        "bool",
    ),
    "OB_FILTER_ENABLED": _field(
        "Confluence Code",
        "5. Bullish Order Block (OB)",
        "Wajibkan terdapat Order Block institusional di pangkal dorongan impuls yang beririsan dengan zona diskon.",
        "bool",
    ),
    "CONFLUENCE_REQUIRE_BULLISH_REACTION": _field(
        "Confluence Code",
        "Wajib reaksi candle bullish",
        "Wajibkan candle pemicu menutup bullish (close > open) di atas posisi penutupan minimum setelah menyentuh zona POI.",
        "bool",
    ),
    "CONFLUENCE_MIN_CLOSE_POSITION": _field(
        "Confluence Code",
        "Posisi close reaksi minimum",
        "Posisi penutupan minimum di dalam rentang high-low candle pemicu (0.55 = menutup di 45% bagian atas).",
        "float",
        minimum=0.0,
        maximum=1.0,
        unit="rasio",
    ),
    "USE_RISK_PERCENT": _field(
        "Ukuran Posisi",
        "Gunakan persen saldo",
        "Ukuran posisi dihitung dari saldo bebas. Nonaktifkan untuk memakai "
        "POSITION_SIZE_USDT sebagai nominal tetap.",
        "bool",
        dangerous=True,
    ),
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
        "PUMP_MIN_24H_CHANGE_PCT",
        "PUMP_MAX_24H_CHANGE_PCT",
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
        "DAILY_DEMAND_FILTER_ENABLED",
        "DAILY_DEMAND_INTERVAL",
        "DAILY_DEMAND_LOOKBACK_BARS",
        "DAILY_DEMAND_ZONE_BUFFER_PCT",
        "DAILY_DEMAND_MAX_DISTANCE_PCT",
        "DAILY_DEMAND_MIN_CLOSE_POSITION",
        "DAILY_TREND_FILTER_ENABLED",
        "DAILY_TREND_INTERVAL",
        "DAILY_TREND_EMA_FAST",
        "DAILY_TREND_EMA_SLOW",
        "DAILY_TREND_ADX_PERIOD",
        "DAILY_TREND_ADX_MIN",
        "DAILY_TREND_LOOKBACK_BARS",
        "USE_DAILY_STOP",
        "MAX_DAILY_LOSS_PERCENT",
        "DAILY_PROFIT_TARGET_PERCENT",
        "DETECTOR_ENABLED",
        "DETECTOR_WEIGHT_CHANGE",
        "DETECTOR_WEIGHT_VOLUME5M",
        "DETECTOR_WEIGHT_ORDERBOOK",
        "DETECTOR_WEIGHT_ATR",
        "DETECTOR_ATR_MIN_PCT",
        "DETECTOR_ATR_MAX_PCT",
        "TREND_FILTER_ENABLED",
        "TREND_INTERVAL",
        "TREND_EMA_FAST",
        "TREND_EMA_SLOW",
        "TREND_ADX_PERIOD",
        "TREND_ADX_MIN",
        "TREND_LOOKBACK_BARS",
        "HTF_DEMAND_FILTER_ENABLED",
        "HTF_DEMAND_LOOKBACK_BARS",
        "HTF_DEMAND_ZONE_BUFFER_PCT",
        "HTF_DEMAND_MAX_DISTANCE_PCT",
        "HTF_DEMAND_MIN_CLOSE_POSITION",
        "M15_DEMAND_FILTER_ENABLED",
        "M15_DEMAND_LOOKBACK_BARS",
        "M15_DEMAND_ZONE_BUFFER_PCT",
        "M15_DEMAND_MAX_DISTANCE_PCT",
        "M15_DEMAND_MIN_CLOSE_POSITION",
        "M15_TREND_FILTER_ENABLED",
        "M15_TREND_EMA_FAST",
        "M15_TREND_EMA_SLOW",
        "M15_TREND_ADX_PERIOD",
        "M15_TREND_ADX_MIN",
        "M15_TREND_LOOKBACK_BARS",
        "ZONE_FILTER_ENABLED",
        "ZONE_INTERVAL",
        "ZONE_LOOKBACK_BARS",
        "ZONE_BUFFER_PCT",
        "ZONE_MAX_DISTANCE_PCT",
        "ZONE_REQUIRE_INSIDE_ZONE",
        "ZONE_MIN_CLOSE_POSITION",
        "ZONE_EMA_FAST",
        "ZONE_EMA_SLOW",
        "ZONE_ADX_PERIOD",
        "ZONE_ADX_MIN",
        "ZONE_TREND_LOOKBACK_BARS",
        "ROLLING_VOLUME_CONFIRMATION_BARS",
        "ROLLING_VOLUME_FILTER_ENABLED",
        "ROLLING_VOLUME_LOOKBACK_BARS",
        "ROLLING_VOLUME_SURGE_MULT",
        "DEMAND_ZONE_FILTER_ENABLED",
        "DEMAND_LOOKBACK_BARS",
        "DEMAND_ZONE_BUFFER_PCT",
        "DEMAND_REQUIRE_INSIDE_ZONE",
        "DEMAND_MAX_DISTANCE_PCT",
        "DEMAND_MIN_CLOSE_POSITION",
        "HTF_FILTER_ENABLED",
        "HTF_INTERVAL",
        "HTF_EMA_FAST",
        "HTF_EMA_SLOW",
        "HTF_ADX_PERIOD",
        "HTF_ADX_MIN",
        "HTF_LOOKBACK_BARS",
        "HTF_RESISTANCE_FILTER_ENABLED",
        "HTF_RESISTANCE_LOOKBACK_BARS",
        "HTF_RESISTANCE_PIVOT_WING_BARS",
        "HTF_RESISTANCE_MIN_ROOM_PCT",
        "DIRECTION_FILTER_ENABLED",
        "DIRECTION_INTERVAL",
        "DIRECTION_EMA_FAST",
        "DIRECTION_EMA_SLOW",
        "DIRECTION_ADX_PERIOD",
        "DIRECTION_ADX_MIN",
        "DIRECTION_LOOKBACK_BARS",
        "DIRECTION_ZONE_FILTER_ENABLED",
        "DIRECTION_ZONE_LOOKBACK_BARS",
        "DIRECTION_ZONE_BUFFER_PCT",
        "DIRECTION_ZONE_MAX_DISTANCE_PCT",
        "DIRECTION_ZONE_MIN_CLOSE_POSITION",
        "DIRECTION_ZONE_REQUIRE_INSIDE_ZONE",
        "TIMING_FILTER_ENABLED",
        "TIMING_EMA_FAST",
        "TIMING_EMA_SLOW",
        "TIMING_ADX_PERIOD",
        "TIMING_ADX_MIN",
        "TIMING_LOOKBACK_BARS",
        "BREAKOUT_RETEST_FILTER_ENABLED",
        "BREAKOUT_LOOKBACK_BARS",
        "BREAKOUT_PIVOT_WING_BARS",
        "BREAKOUT_ATR_PERIOD",
        "BREAKOUT_RETEST_TOLERANCE_ATR",
        "BREAKOUT_RETEST_MAX_BARS",
        "BREAKOUT_RETEST_MAX_TOUCHES",
        "BREAKOUT_RETEST_REQUIRE_BULLISH",
        "USE_ATR_EXIT",
        "ATR_PERIOD",
        "ATR_MULT_SL",
        "ATR_MULT_TP",
        "ATR_MULT_TRAIL",
        "ATR_MULT_BE_TRIGGER",
        "ATR_MULT_BE_LOCK",
        "ATR_MULT_TRAIL_START",
        "USE_TP",
        "TP_PCT",
        "USE_STOP_LOSS",
        "SL_PCT",
        "USE_BREAKEVEN",
        "BE_TRIGGER_PCT",
        "BE_LOCK_PCT",
        "USE_TRAILING",
        "TRAILING_START_PCT",
        "TRAILING_STEP_PCT",
    }
)

_LEGACY_KEY_MIGRATION = {
    "TREND_FILTER_ENABLED": "DIRECTION_FILTER_ENABLED",
    "TREND_EMA_FAST": "DIRECTION_EMA_FAST",
    "TREND_EMA_SLOW": "DIRECTION_EMA_SLOW",
    "TREND_ADX_PERIOD": "DIRECTION_ADX_PERIOD",
    "TREND_ADX_MIN": "DIRECTION_ADX_MIN",
    "TREND_LOOKBACK_BARS": "DIRECTION_LOOKBACK_BARS",
    "M15_TREND_FILTER_ENABLED": "TIMING_FILTER_ENABLED",
    "M15_TREND_EMA_FAST": "TIMING_EMA_FAST",
    "M15_TREND_EMA_SLOW": "TIMING_EMA_SLOW",
    "M15_TREND_ADX_PERIOD": "TIMING_ADX_PERIOD",
    "M15_TREND_ADX_MIN": "TIMING_ADX_MIN",
    "M15_TREND_LOOKBACK_BARS": "TIMING_LOOKBACK_BARS",
    "SWING_LOOKBACK_BARS": "BREAKOUT_LOOKBACK_BARS",
    "SWING_PIVOT_WING_BARS": "BREAKOUT_PIVOT_WING_BARS",
    "MAX_BARS_BREAKOUT_TO_RETEST": "BREAKOUT_RETEST_MAX_BARS",
    "MAX_RETEST_TOUCHES": "BREAKOUT_RETEST_MAX_TOUCHES",
}


def _migrate_legacy_keys(payload: dict) -> dict:
    hasil = dict(payload)
    for lama, baru in _LEGACY_KEY_MIGRATION.items():
        if lama not in hasil:
            continue
        nilai = hasil.pop(lama)
        if baru not in hasil:
            hasil[baru] = nilai
    for kunci in list(hasil):
        if kunci in REMOVED_PARAMETERS:
            hasil.pop(kunci)
    return hasil


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
        payload = _migrate_legacy_keys(payload)
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

    def ambil(*keys: str) -> "list[float] | None":
        nilai = [_angka_untuk_relasi(cfg, k) for k in keys]
        return None if any(v is None for v in nilai) else nilai

    def pasangan(kiri: str, kanan: str) -> "tuple[float, float] | None":
        nilai = ambil(kiri, kanan)
        return None if nilai is None else (nilai[0], nilai[1])

    keluar: dict[str, str] = {}

    if "IDR_RATE_MODE" in cfg and "IDR_RATE_MANUAL" in cfg:
        mode_kurs = str(cfg.get("IDR_RATE_MODE", "AUTO") or "AUTO").strip().upper()
        nilai_kurs = _angka_untuk_relasi(cfg, "IDR_RATE_MANUAL")
        if mode_kurs == "MANUAL" and nilai_kurs is not None and nilai_kurs <= 0:
            keluar["IDR_RATE_MANUAL"] = (
                "wajib diisi lebih besar dari nol saat IDR_RATE_MODE = MANUAL"
            )

    confluence_aktif = _flag_untuk_relasi(cfg, "CONFLUENCE_FILTER_ENABLED", True)
    cek_confluence = mode_aware is False or confluence_aktif

    if cek_confluence:
        fib_pair = pasangan("FIB_RETRACEMENT_MIN", "FIB_RETRACEMENT_MAX")
        if fib_pair is not None and fib_pair[0] >= fib_pair[1]:
            keluar["FIB_RETRACEMENT_MIN"] = (
                "harus lebih kecil dari FIB_RETRACEMENT_MAX agar rentang zona diskon valid"
            )

        liq_pair = pasangan("LIQUIDITY_SWEEP_LOOKBACK_BARS", "CONFIRM_LOOKBACK_BARS")
        if liq_pair is not None and liq_pair[0] >= liq_pair[1]:
            keluar["LIQUIDITY_SWEEP_LOOKBACK_BARS"] = (
                "harus lebih kecil dari CONFIRM_LOOKBACK_BARS"
            )

        try:
            from strategy.indicators import INTERVAL_MINUTES as _INTERVAL_MINUTES

            menit_konfirmasi = _INTERVAL_MINUTES.get(
                str(cfg.get("CONFIRM_INTERVAL", "")).lower()
            )
            menit_struktur = _INTERVAL_MINUTES.get(
                str(cfg.get("CONFLUENCE_STRUCTURE_INTERVAL", "")).lower()
            )
        except ImportError:
            menit_konfirmasi = menit_struktur = None

        if menit_struktur and menit_konfirmasi:
            if menit_struktur % menit_konfirmasi != 0 or menit_struktur < menit_konfirmasi:
                keluar["CONFLUENCE_STRUCTURE_INTERVAL"] = (
                    "harus kelipatan bulat dari CONFIRM_INTERVAL dan tidak lebih pendek, "
                    "agar backtest dapat merangkai candle struktur Confluence tanpa look-ahead"
                )

    batas_tahan = _angka_untuk_relasi(cfg, "MAX_HOLD_HOURS")
    if batas_tahan is not None and batas_tahan > 0:
        try:
            from strategy.indicators import INTERVAL_MINUTES as _INTERVAL_MINUTES_TAHAN

            menit_bar = _INTERVAL_MINUTES_TAHAN.get(
                str(cfg.get("CONFIRM_INTERVAL", "")).lower()
            )
        except ImportError:
            menit_bar = None
        if menit_bar and batas_tahan * 60.0 < 2.0 * float(menit_bar):
            keluar["MAX_HOLD_HOURS"] = (
                "minimal 2 bar CONFIRM_INTERVAL "
                f"({2.0 * float(menit_bar) / 60.0:g} jam) supaya posisi tidak ditutup "
                "pada bar entry-nya sendiri; isi 0 untuk menonaktifkan time stop"
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
        for kunci_relasi, pesan_relasi in relation_violations(cleaned).items():
            relation(kunci_relasi, False, pesan_relasi)

        if cleaned.get("CONFLUENCE_FILTER_ENABLED"):
            active_components = [
                k
                for k in (
                    "VP_FILTER_ENABLED",
                    "FIB_FILTER_ENABLED",
                    "LIQUIDITY_FILTER_ENABLED",
                    "FVG_FILTER_ENABLED",
                    "OB_FILTER_ENABLED",
                )
                if cleaned.get(k)
            ]
            if not active_components:
                warnings.append(
                    "CONFLUENCE_FILTER_ENABLED aktif tetapi kelima komponen (Volume Profile, "
                    "Fibonacci, Liquidity, FVG, Order Block) dinonaktifkan; hanya struktur "
                    "impuls BoS yang diperiksa."
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
            native_stop_available = bool(cleaned["USE_NATIVE_STOP_LOSS"])
            native_oco_available = bool(cleaned["USE_NATIVE_OCO"])
            relation(
                "USE_NATIVE_STOP_LOSS",
                native_stop_available or native_oco_available,
                "mode LIVE wajib memiliki minimal satu proteksi exchange-side: native stop atau OCO",
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

    return cleaned, errors, warnings
