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
_INTERVAL_MENIT = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "6h": 360,
    "8h": 480,
    "12h": 720,
    "1d": 1440,
}


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
    allow_empty: bool = False,
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
        "allow_empty": allow_empty,
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
    "PROXY_URL": _field(
        "Sistem",
        "Proxy HTTP/SOCKS",
        "Proxy opsional untuk seluruh koneksi REST ke Binance. Isi http://host:port "
        "atau socks5h://host:port, kosongkan bila koneksi langsung. Bisa juga lewat "
        "PROXY_URL di file .env.",
        "str",
        editor="proxy",
        allow_empty=True,
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
        "Ambang likuiditas 24 jam. Koin yang turnover-nya di bawah nilai ini tidak masuk daftar pemantauan walau kenaikannya besar, supaya bot tidak masuk ke pasar yang sepi dan licin.",
        "float",
        minimum=0,
        maximum=1e15,
        unit="USDT",
    ),
    "GAINER_MIN_CHANGE_PCT": _field(
        "Pemilihan Koin",
        "Kenaikan 24 jam minimum",
        "Batas bawah perubahan harga 24 jam agar koin masuk peringkat top gainer. Isi 0 bila semua koin boleh masuk.",
        "float",
        minimum=-100.0,
        maximum=100000.0,
        unit="%",
    ),
    "GAINER_MAX_CHANGE_PCT": _field(
        "Pemilihan Koin",
        "Kenaikan 24 jam maksimum",
        "Batas atas perubahan harga 24 jam. Berguna untuk menghindari koin yang sudah terbang jauh sebelum bot memeriksa sinyal. 0 berarti tanpa batas.",
        "float",
        minimum=0.0,
        maximum=100000.0,
        unit="%",
    ),
    "PUMP_DYNAMIC_FILTER_ENABLED": _field("Pump Dinamis 24 Jam", "Aktifkan ambang dinamis", "Menghitung batas minimum dan maksimum perubahan 24 jam dari distribusi koin likuid saat pemindaian.", "bool"),
    "PUMP_DYNAMIC_MIN_PERCENTILE": _field("Pump Dinamis 24 Jam", "Persentil minimum", "Persentil perubahan 24 jam untuk batas bawah dinamis.", "float", minimum=0.0, maximum=100.0, unit="persentil"),
    "PUMP_DYNAMIC_MIN_FLOOR_PCT": _field("Pump Dinamis 24 Jam", "Lantai minimum", "Nilai terendah yang boleh dipakai sebagai batas bawah dinamis.", "float", minimum=-100.0, maximum=100.0, unit="%"),
    "PUMP_DYNAMIC_MIN_CAP_PCT": _field("Pump Dinamis 24 Jam", "Plafon minimum", "Nilai tertinggi yang boleh dipakai sebagai batas bawah dinamis.", "float", minimum=-100.0, maximum=100.0, unit="%"),
    "PUMP_DYNAMIC_MAX_PERCENTILE": _field("Pump Dinamis 24 Jam", "Persentil maksimum", "Persentil perubahan 24 jam untuk batas atas dinamis.", "float", minimum=0.0, maximum=100.0, unit="persentil"),
    "PUMP_DYNAMIC_MAX_FLOOR_PCT": _field("Pump Dinamis 24 Jam", "Lantai maksimum", "Nilai terendah yang boleh dipakai sebagai batas atas dinamis.", "float", minimum=0.0, maximum=1000.0, unit="%"),
    "PUMP_DYNAMIC_MAX_CAP_PCT": _field("Pump Dinamis 24 Jam", "Plafon maksimum", "Nilai tertinggi yang boleh dipakai sebagai batas atas dinamis.", "float", minimum=0.0, maximum=1000.0, unit="%"),
    "PUMP_DYNAMIC_MIN_SAMPLE": _field("Pump Dinamis 24 Jam", "Minimum sampel", "Jumlah ticker valid minimum sebelum persentil dinamis digunakan.", "int", minimum=5, maximum=5000, unit="ticker"),
    "PUMP_FALLBACK_MIN_CHANGE_PCT": _field("Pump Dinamis 24 Jam", "Minimum fallback", "Batas bawah cadangan saat jumlah sampel tidak cukup.", "float", minimum=-100.0, maximum=100.0, unit="%"),
    "PUMP_FALLBACK_MAX_CHANGE_PCT": _field("Pump Dinamis 24 Jam", "Maksimum fallback", "Batas atas cadangan saat jumlah sampel tidak cukup.", "float", minimum=0.0, maximum=1000.0, unit="%"),
    "PUMP_RANK_CHANGE_WEIGHT": _field("Ranking Hybrid", "Bobot perubahan 24 jam", "Bobot persentil perubahan harga pada skor hybrid.", "float", minimum=0.0, maximum=100.0, unit="bobot"),
    "PUMP_RANK_TURNOVER_WEIGHT": _field("Ranking Hybrid", "Bobot turnover", "Bobot persentil log turnover pada skor hybrid.", "float", minimum=0.0, maximum=100.0, unit="bobot"),
    "PUMP_RANK_ENGULFING_WEIGHT": _field("Ranking Hybrid", "Bobot engulfing", "Bobot kekuatan body engulfing pada skor akhir.", "float", minimum=0.0, maximum=100.0, unit="bobot"),
    "PUMP_RANK_PRICE_EMA_WEIGHT": _field("Ranking Hybrid", "Bobot jarak harga dari EMA", "Bobot jarak close di atas EMA lambat yang dinormalisasi ATR.", "float", minimum=0.0, maximum=100.0, unit="bobot"),
    "PUMP_RANK_EMA_TREND_WEIGHT": _field("Ranking Hybrid", "Bobot tren EMA", "Bobot jarak EMA cepat di atas EMA lambat yang dinormalisasi ATR.", "float", minimum=0.0, maximum=100.0, unit="bobot"),
    "GAINER_VOLUME_FILTER_ENABLED": _field(
        "Pemilihan Koin",
        "Syarat kenaikan volume 24 jam",
        "Bila aktif, koin hanya masuk peringkat kalau turnover 24 jam terakhir lebih tinggi dari rata-rata beberapa hari sebelumnya. Ini yang menyingkirkan koin yang naik harga tetapi minat pasarnya sedang mengering.",
        "bool",
    ),
    "GAINER_VOLUME_RISE_MULT": _field(
        "Pemilihan Koin",
        "Kenaikan turnover minimum",
        "Turnover 24 jam terakhir minimal sekian kali rata-rata turnover 24 jam beberapa hari sebelumnya. Nilai 1.2 berarti volume harus naik minimal dua puluh persen dari kebiasaannya.",
        "float",
        minimum=0.0,
        maximum=20.0,
        unit="x",
    ),
    "GAINER_VOLUME_AVG_DAYS": _field(
        "Pemilihan Koin",
        "Rata-rata turnover (hari)",
        "Berapa hari sebelumnya dipakai sebagai pembanding turnover 24 jam. Nilai 3 berarti volume hari ini dibanding rata-rata tiga hari sebelumnya.",
        "int",
        minimum=1,
        maximum=14,
        unit="hari",
    ),
    "EXIT_MAX_HOLD_HOURS": _field(
        "Exit ATR",
        "Batas tahan posisi",
        "Umur maksimum posisi sebelum ditutup paksa. 0 berarti tanpa batas, sehingga posisi hanya ditutup oleh stop, target, breakeven, atau trailing ATR.",
        "float",
        minimum=0.0,
        maximum=720.0,
        unit="jam",
    ),
    "ENTRY_MIN_RR_RATIO": _field(
        "Exit ATR",
        "Minimum Risk:Reward",
        "Target profit dihitung dari jarak entry ke Stop Loss pola. Nilai 3 berarti target berada tiga kali jarak risiko, yaitu R:R 1:3.",
        "float",
        minimum=0.1,
        maximum=20.0,
        unit="x",
    ),
    "ENTRY_ATR_PERIOD": _field(
        "Exit ATR",
        "Periode ATR",
        "Jumlah candle untuk menghitung Average True Range pada timeframe sinyal. ATR dipakai untuk buffer Stop Loss, breakeven, dan trailing.",
        "int",
        minimum=2,
        maximum=200,
        unit="candle",
    ),
    "EXIT_ATR_SL_MULT": _field(
        "Exit ATR",
        "Stop Loss (x ATR)",
        "Jarak stop awal dari harga entry, dihitung sebagai sekian kali ATR. Nilai 1.5 berarti stop berada 1.5 ATR di bawah entry.",
        "float",
        minimum=0.1,
        maximum=20.0,
        unit="x ATR",
    ),
    "EXIT_ATR_TP_MULT": _field(
        "Exit ATR",
        "Take Profit (x ATR)",
        "Jarak target profit dari harga entry dalam kelipatan ATR. Rasio TP dibagi SL menjadi R:R efektif strategi.",
        "float",
        minimum=0.1,
        maximum=50.0,
        unit="x ATR",
    ),
    "EXIT_ATR_TRAIL_MULT": _field(
        "Exit ATR",
        "Trailing stop (x ATR)",
        "Setelah profit mencapai satu kali jarak stop, stop dinaikkan mengikuti harga tertinggi dan selalu berjarak sekian kali ATR di bawah puncak. Stop tidak pernah diturunkan.",
        "float",
        minimum=0.1,
        maximum=20.0,
        unit="x ATR",
    ),
    "EXIT_ATR_BREAKEVEN_MULT": _field(
        "Exit ATR",
        "Kompatibilitas BE lama",
        "Parameter kompatibilitas lama. Pemicu aktif sekarang memakai kelipatan risiko awal.",
        "float",
        minimum=0.0,
        maximum=20.0,
        unit="x ATR",
    ),
    "EXIT_BREAKEVEN_TRIGGER_R": _field(
        "Exit ATR",
        "Pemicu breakeven",
        "Profit dalam kelipatan risiko awal sebelum stop dinaikkan ke entry ditambah estimasi biaya beli dan jual.",
        "float",
        minimum=0.1,
        maximum=20.0,
        unit="R",
    ),
    "EXIT_TRAILING_START_R": _field(
        "Exit ATR",
        "Mulai trailing",
        "Profit dalam kelipatan risiko awal sebelum trailing ATR mulai mengikuti harga tertinggi.",
        "float",
        minimum=0.1,
        maximum=20.0,
        unit="R",
    ),
    "EXIT_ATR_MAX_SL_PCT": _field(
        "Exit ATR",
        "Batas jarak stop",
        "Setup ditolak bila 1.5 ATR ternyata lebih lebar dari persen ini terhadap harga entry, tanda volatilitas terlalu besar untuk ukuran posisi saat ini. 0 berarti tanpa batas.",
        "float",
        minimum=0.0,
        maximum=50.0,
        unit="%",
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
        "Pasang OCO SELL native berisi TP limit dan SL limit hasil hitungan ATR pada posisi yang terdeteksi.",
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
        "Penurunan dari puncak equity kumulatif sebelum stop. Puncak tidak direset setelah trigger atau cooldown; entry tetap diblokir selama drawdown kumulatif masih di atas batas.",
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
        "Bullish Engulfing + EMA",
        "Interval sinyal",
        "Timeframe candle untuk pola bullish engulfing dan EMA. Default 1h. Timeframe lebih rendah menghasilkan sinyal lebih sering, tetapi umumnya lebih berisik.",
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
        "Bullish Engulfing + EMA",
        "Jumlah candle yang diunduh",
        "Banyaknya candle terakhir yang diambil untuk menghitung pola, EMA, dan ATR. Limit endpoint klines Binance 1000 per panggilan.",
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
    "DUST_VALUE_THRESHOLD_USDT": _field(
        "Sistem",
        "Ambang debu aset",
        "Nilai aset non-quote yang dianggap debu dan tidak memblokir entry. Aset di atas ambang ini dianggap posisi tidak terkelola dan memblokir entry sampai dijual atau dikonversi.",
        "float",
        minimum=0.0,
        maximum=100.0,
        unit="USDT",
    ),
    "TOP_N_CANDIDATES_TO_CONFIRM": _field(
        "Scan",
        "Jumlah kandidat konfirmasi",
        "Berapa kandidat teratas yang diperiksa.",
        "int",
        minimum=1,
        maximum=1000,
    ),
    "ENTRY_RULE_ENABLED": _field(
        "Bullish Engulfing + EMA",
        "Aktifkan filter entry",
        "Bot hanya entry pada bullish engulfing body ketat, close di atas EMA cepat dan lambat, serta EMA cepat di atas EMA lambat.",
        "bool",
    ),
    "EMA_FAST_PERIOD": _field(
        "Bullish Engulfing + EMA",
        "Periode EMA cepat",
        "Periode EMA cepat untuk menyaring arah tren. Nilai default 20.",
        "int",
        minimum=2,
        maximum=500,
        unit="candle",
    ),
    "EMA_SLOW_PERIOD": _field(
        "Bullish Engulfing + EMA",
        "Periode EMA lambat",
        "Periode EMA lambat untuk menyaring arah tren. Nilai default 50 dan harus lebih besar dari EMA cepat.",
        "int",
        minimum=3,
        maximum=1000,
        unit="candle",
    ),
    "ENTRY_SL_ATR_BUFFER_MULT": _field(
        "Bullish Engulfing + EMA",
        "Buffer SL di bawah pola",
        "Stop Loss ditempatkan di bawah low terendah dari dua candle pola, dikurangi buffer ATR ini.",
        "float",
        minimum=0.0,
        maximum=10.0,
        unit="x ATR",
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
        "ENTRY_BULL_CANDLES",
        "MOMENTUM_BREAKOUT_LOOKBACK",
        "MOMENTUM_BODY_ATR_MULT",
        "MOMENTUM_VOLUME_MULT",
        "MOMENTUM_VOLUME_LOOKBACK",
        "ENTRY_SL_ANCHOR",
        "MARKET_DATA_INTERVAL",
        "ENTRY_SL_BUFFER_PCT",
        "ENTRY_MAX_SL_PCT",
        "EXIT_TRAILING_PCT",
        "BTC_FILTER_ENABLED",
        "BTC_MAX_DROP_PCT",
        "BTC_LOOKBACK_BARS",
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
        "CONFLUENCE_PARTIAL_TP_ENABLED",
        "CONFLUENCE_PARTIAL_TP_FRACTION",
        "CONFLUENCE_PARTIAL_TP_R_MULTIPLE",
        "CONFLUENCE_PARTIAL_TP_BREAKEVEN",
        "CONFLUENCE_FILTER_ENABLED",
        "CONFLUENCE_STRUCTURE_INTERVAL",
        "CONFLUENCE_LOOKBACK_BARS",
        "CONFLUENCE_MIN_IMPULSE_PCT",
        "CONFLUENCE_IMPULSE_SELECTION",
        "CONFLUENCE_ZONE_TOLERANCE_RATIO",
        "CONFLUENCE_REQUIRE_ALL_FIVE",
        "CONFLUENCE_MIN_SCORE",
        "CONFLUENCE_REQUIRE_BULLISH_REACTION",
        "CONFLUENCE_MIN_CLOSE_POSITION",
        "CONFLUENCE_SL_BUFFER_PCT",
        "CONFLUENCE_MIN_RR_RATIO",
        "CONFLUENCE_BREAKEVEN_ENABLED",
        "CONFLUENCE_BREAKEVEN_R_MULTIPLE",
        "CONFLUENCE_BREAKEVEN_LOCK_PCT",
        "VP_FILTER_ENABLED",
        "VP_BINS",
        "VP_HVN_TOP_RATIO",
        "FIB_FILTER_ENABLED",
        "FIB_RETRACEMENT_MIN",
        "FIB_RETRACEMENT_MAX",
        "LIQUIDITY_FILTER_ENABLED",
        "LIQUIDITY_EQUAL_LEVEL_TOL_RATIO",
        "LIQUIDITY_SWEEP_LOOKBACK_BARS",
        "FVG_FILTER_ENABLED",
        "OB_FILTER_ENABLED",
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
        sebelum_migrasi = dict(payload)
        payload = _migrate_legacy_keys(payload)
        migrated = payload != sebelum_migrasi
        try:
            _validate_override_payload(payload)
        except ValueError as exc:
            backup = archive_corrupt(SETTINGS_FILE)
            message = f"Override {raw_mode} rusak: {exc}. Cadangan: {backup}"
            _error_marker(SETTINGS_ERROR_FILE, message, backup)
            return {}, errors + [message]
        if migrated:
            doc.setdefault("overrides", {})[raw_mode] = dict(payload)
            try:
                atomic_write_json(SETTINGS_FILE, doc)
            except (OSError, TypeError, ValueError):
                pass
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
            if not spec.get("allow_empty"):
                raise ValueError("tidak boleh kosong")
        elif len(result) > 2048:
            raise ValueError("teks terlalu panjang")
        elif spec.get("editor") == "asset":
            result = result.upper()
            if not _ASSET_RE.fullmatch(result):
                raise ValueError("kode aset tidak valid")
        elif spec.get("editor") == "proxy":
            skema = result.split("://", 1)[0].lower() if "://" in result else ""
            if skema not in {"http", "https", "socks4", "socks5", "socks5h"}:
                raise ValueError(
                    "skema proxy harus http://, https://, socks4://, socks5://, "
                    "atau socks5h://"
                )
            if any(karakter.isspace() for karakter in result):
                raise ValueError("URL proxy tidak boleh memuat spasi")
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

    if _flag_untuk_relasi(cfg, "PUMP_DYNAMIC_FILTER_ENABLED", True):
        min_floor = _angka_untuk_relasi(cfg, "PUMP_DYNAMIC_MIN_FLOOR_PCT")
        min_cap = _angka_untuk_relasi(cfg, "PUMP_DYNAMIC_MIN_CAP_PCT")
        max_floor = _angka_untuk_relasi(cfg, "PUMP_DYNAMIC_MAX_FLOOR_PCT")
        max_cap = _angka_untuk_relasi(cfg, "PUMP_DYNAMIC_MAX_CAP_PCT")
        fallback_min = _angka_untuk_relasi(cfg, "PUMP_FALLBACK_MIN_CHANGE_PCT")
        fallback_max = _angka_untuk_relasi(cfg, "PUMP_FALLBACK_MAX_CHANGE_PCT")
        if min_floor is not None and min_cap is not None and min_floor > min_cap:
            keluar["PUMP_DYNAMIC_MIN_CAP_PCT"] = "harus sama dengan atau lebih besar dari lantai minimum"
        if max_floor is not None and max_cap is not None and max_floor > max_cap:
            keluar["PUMP_DYNAMIC_MAX_CAP_PCT"] = "harus sama dengan atau lebih besar dari lantai maksimum"
        if fallback_min is not None and fallback_max is not None and fallback_min > fallback_max:
            keluar["PUMP_FALLBACK_MAX_CHANGE_PCT"] = "harus lebih besar dari minimum fallback"
        weight_keys = ("PUMP_RANK_CHANGE_WEIGHT", "PUMP_RANK_TURNOVER_WEIGHT", "PUMP_RANK_ENGULFING_WEIGHT", "PUMP_RANK_PRICE_EMA_WEIGHT", "PUMP_RANK_EMA_TREND_WEIGHT")
        weights = [_angka_untuk_relasi(cfg, key) for key in weight_keys]
        if all(value is not None for value in weights) and sum(weights) <= 0:
            keluar["PUMP_RANK_CHANGE_WEIGHT"] = "jumlah seluruh bobot ranking harus lebih besar dari nol"

    if _flag_untuk_relasi(cfg, "ENTRY_RULE_ENABLED", True):
        perlu = _angka_untuk_relasi(cfg, "ENTRY_ATR_PERIOD")
        ema_cepat = _angka_untuk_relasi(cfg, "EMA_FAST_PERIOD")
        ema_lambat = _angka_untuk_relasi(cfg, "EMA_SLOW_PERIOD")
        lookback_kandidat = [int(nilai) for nilai in (perlu, ema_lambat) if nilai is not None]
        butuh = max([3] + [int(nilai) + 1 for nilai in lookback_kandidat])
        if ema_cepat is not None and ema_lambat is not None and ema_cepat >= ema_lambat:
            keluar["EMA_SLOW_PERIOD"] = "harus lebih besar dari EMA_FAST_PERIOD"
        lookback = _angka_untuk_relasi(cfg, "CONFIRM_LOOKBACK_BARS")
        if lookback is not None and lookback < butuh:
            keluar["CONFIRM_LOOKBACK_BARS"] = (
                f"minimal {butuh} candle agar ATR dan EMA lambat punya cukup riwayat"
            )
        if _flag_untuk_relasi(cfg, "GAINER_VOLUME_FILTER_ENABLED", True):
            menit_interval = _INTERVAL_MENIT.get(
                str(cfg.get("CONFIRM_INTERVAL", "1h") or "1h").strip().lower()
            )
            hari = _angka_untuk_relasi(cfg, "GAINER_VOLUME_AVG_DAYS")
            if menit_interval and hari is not None:
                per_hari = max(1, 1440 // int(menit_interval))
                butuh_volume = per_hari * 2
                if lookback is not None and lookback < butuh_volume:
                    keluar["CONFIRM_LOOKBACK_BARS"] = (
                        f"minimal {butuh_volume} candle agar syarat kenaikan "
                        f"turnover bisa dibandingkan dengan periode sebelumnya "
                        f"pada interval {cfg.get('CONFIRM_INTERVAL', '1h')}"
                    )
        batas_sl = _angka_untuk_relasi(cfg, "EXIT_ATR_MAX_SL_PCT")
        if batas_sl is not None and batas_sl > 0:
            if batas_sl < 0.2:
                keluar["EXIT_ATR_MAX_SL_PCT"] = (
                    "terlalu sempit, hampir semua setup akan ditolak; isi 0 untuk "
                    "mematikan batas atau pakai minimal 0.2 persen"
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

        if not cleaned.get("ENTRY_RULE_ENABLED"):
            warnings.append(
                "ENTRY_RULE_ENABLED nonaktif: bot masuk tanpa menunggu candle "
                "bullish engulfing + EMA, hanya mengandalkan seleksi gainer 24 jam dan "
                "proteksi orderbook."
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
