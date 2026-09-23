"""
Konfigurasi Bot Grid Martingale (adaptasi dari Gold_Grid_Martingale_Pro.mq5)
=============================================================================

CATATAN ADAPTASI DARI MT5 KE BINANCE SPOT (WAJIB DIBACA):

1. Binance Spot TIDAK BISA short-sell. Karena itu bot ini HANYA membuka posisi
   BUY (mengikuti pilihan Anda). Saat sinyal tren berbalik turun, bot TIDAK
   membuka posisi apa pun (tidak short, tidak market-sell paksa) -- basket
   yang sudah terbuka tetap dikelola lewat Take Profit / Breakeven / Trailing
   seperti EA aslinya (mirror dari InpCloseOnOppositeTrend = false).

2. EA asli memakai satuan "points" (mis. GridMinPoints=300 untuk XAUUSD).
   Satuan itu tidak relevan untuk BTCUSDT, jadi semua jarak grid, TP,
   breakeven, dan trailing di sini memakai PERSENTASE dari harga rata-rata
   basket. Nilai default di bawah HANYALAH titik awal yang wajar, BUKAN hasil
   backtest -- volatilitas BTC berbeda jauh dari XAUUSD. Anda WAJIB
   menguji/menyesuaikan sebelum menaikkan modal.

3. "Lot" pada MT5 diganti dengan nominal USDT (quote asset) per entry.

4. Tidak ada floating "Stop Loss" bawaan bursa di Spot (karena tidak ada
   leverage/likuidasi) -- breakeven & trailing di sini dikelola bot dengan
   cara memantau harga terus-menerus dan mengeksekusi MARKET SELL saat level
   tersentuh. Ini artinya bot HARUS berjalan tanpa henti (24/7) di VPS Anda.
   Jika bot mati, level BE/trailing tidak akan tereksekusi.
"""

import os

CONFIG = {
    # ------------------------------------------------------------------
    # 01. UMUM
    # ------------------------------------------------------------------
    "SYMBOL": "BTCUSDT",
    "BASE_ASSET": "BTC",
    "QUOTE_ASSET": "USDT",
    "INTERVAL": "5m",                    # setara PERIOD_M5 di EA asli
    "DRY_RUN": False,                     # WAJIB: set False manual kalau sudah yakin mau live
    "LOOP_INTERVAL_SECONDS": 15,         # jeda antar-iterasi pengecekan TP/BE/trailing
    "HEARTBEAT_INTERVAL_SECONDS": 300,   # log "masih hidup" tiap 5 menit meski tidak ada kejadian
    "MIN_SECONDS_BETWEEN_ORDERS": 60,
    "COOLDOWN_MINUTES_AFTER_CLOSE": 5,

    # Base URL REST. Untuk uji coba tanpa uang sungguhan, ganti ke:
    # "https://testnet.binance.vision"
    "BASE_URL": "https://api.binance.com",

    # Kredensial diambil dari environment variable, JANGAN taruh langsung di sini.
    # Set lewat: export BINANCE_API_KEY=...  &&  export BINANCE_API_SECRET=...
    # $env:BINANCE_API_SECRET=... (Windows PowewrShell)
    # $env:BINANCE_API_KEY=... (Windows PowewrShell)
    "API_KEY": os.environ.get("BINANCE_API_KEY", ""),
    "API_SECRET": os.environ.get("BINANCE_API_SECRET", ""),

    # ------------------------------------------------------------------
    # 02. MANAJEMEN RISIKO
    # ------------------------------------------------------------------
    "USE_RISK_PERCENT": False,           # True = order awal % dari saldo USDT free
    "RISK_PERCENT": 3.0,                 # dipakai jika USE_RISK_PERCENT = True
    "INITIAL_ORDER_USDT": 5.0,          # dipakai jika USE_RISK_PERCENT = False
    "MAX_ORDER_USDT": 10.0,              # batas aman nominal per satu entry
    "MAX_TOTAL_EXPOSURE_USDT": 50.0,    # batas total nominal seluruh grid (safety cap)

    "USE_EQUITY_STOP": False,              # matikan (False) utk nonaktifkan DD Stop (drawdown dari puncak)
    "MAX_DRAWDOWN_PERCENT": 20.0,        # stop dari peak equity
    "USE_DAILY_STOP": False,               # matikan (False) utk nonaktifkan Daily Stop (rugi/profit harian)
    "MAX_DAILY_LOSS_PERCENT": 5.0,
    "DAILY_PROFIT_TARGET_PERCENT": 10.0,
    "CLOSE_ALL_AT_LIMIT": True,
    "DD_COOLDOWN_HOURS": 24,

    # ------------------------------------------------------------------
    # 03. ENTRY: SuperTrend + EMA (identik konsep dengan EA asli)
    # ------------------------------------------------------------------
    "ST_ATR_PERIOD": 10,
    "ST_MULTIPLIER": 3.0,
    "EMA_PERIOD": 200,
    "USE_CLOSED_BAR_SIGNAL": True,
    "CLOSE_ON_OPPOSITE_TREND": False,    # spot: tidak relevan (tidak ada short)

    # ------------------------------------------------------------------
    # 04. GRID MARTINGALE (konservatif, persentase dari harga)
    # ------------------------------------------------------------------
    "USE_ATR_GRID": True,
    "GRID_ATR_PERIOD": 10,
    "GRID_ATR_MULTIPLIER": 1.0,
    "GRID_MIN_PCT": 0.6,                 # jarak minimum antar layer grid (%)
    "GRID_MAX_PCT": 3.0,                 # jarak maksimum antar layer grid (%)
    "FIXED_GRID_PCT": 1.0,               # dipakai jika USE_ATR_GRID = False
    "LOT_MULTIPLIER": 1.3,               # pengali martingale, sama dgn EA asli
    "MAX_GRID_LAYERS": 5,
    "ONLY_ADD_IF_TREND_VALID": True,

    # ------------------------------------------------------------------
    # 05. EXIT: TP Basket, Breakeven, Trailing (persentase dari avg price)
    # ------------------------------------------------------------------
    "USE_BASKET_TP": True,
    "BASKET_TP_PCT": 1.2,
    "USE_BASKET_BREAKEVEN": True,
    "BE_TRIGGER_PCT": 0.7,
    "BE_LOCK_PCT": 0.1,
    "USE_BASKET_TRAILING": True,
    "TRAILING_START_PCT": 1.5,
    "TRAILING_STEP_PCT": 0.4,

    # ------------------------------------------------------------------
    # 06. FILTER
    # ------------------------------------------------------------------
    "MAX_SPREAD_PCT": 0.15,              # tolak entry jika spread bid-ask > ini (%)

    # ------------------------------------------------------------------
    # 07. FILE STATE & LOG
    # ------------------------------------------------------------------
    "STATE_FILE": "bot_state.json",
    "LOG_FILE": "bot.log",
}


# =========================================================================
# KONFIGURASI MODE KEDUA: PUMP SCANNER (pump_scanner_bot.py)
# =========================================================================
# Mode ini TIDAK memprediksi pump sebelum terjadi -- ia mendeteksi koin yang
# harganya SUDAH naik signifikan + volume tinggi dalam 24 jam terakhir, lalu
# mengkonfirmasi lewat candle 5 menit apakah momentumnya kelihatan masih
# berlanjut, sebelum ikut masuk. Ini reaktif (momentum chasing), bukan
# prediktif. Berbeda dari grid martingale, mode ini TIDAK averaging-down --
# hanya satu entry per rotasi, dengan TP/Breakeven/Trailing untuk keluar.
PUMP_CONFIG = {
    "QUOTE_ASSET": "USDT",
    "DRY_RUN": False,
    "BASE_URL": "https://api.binance.com",
    "API_KEY": os.environ.get("BINANCE_API_KEY", ""),
    "API_SECRET": os.environ.get("BINANCE_API_SECRET", ""),

    # --- Scan & seleksi kandidat ---
    "MARKET_SCAN_INTERVAL_SECONDS": 300,     # scan seluruh pasar tiap 5 menit
    "LOOP_INTERVAL_SECONDS": 15,             # cek TP/BE/trailing tiap 15 detik
    "MIN_PUMP_PCT_24H": 8.0,                 # minimal naik 8% dalam 24 jam utk dianggap kandidat
    "MIN_QUOTE_VOLUME_USDT_24H": 2_000_000,  # minimal volume 24 jam (hindari koin ilikuid/rawan manipulasi)
    "TOP_N_CANDIDATES_TO_CONFIRM": 10,       # dari hasil ranking, cek candle utk N teratas
    "CONFIRM_INTERVAL": "5m",
    "CONFIRM_LOOKBACK_BARS": 20,
    "MIN_CLOSE_POSITION_IN_RANGE": 0.35,     # lihat market_scanner.confirm_momentum()
    "EXTRA_EXCLUDE_SYMBOLS": [],             # mis. ["SOMEUSDT"] kalau mau blacklist manual

    # --- Ukuran posisi (tanpa martingale -- sekali entry per rotasi) ---
    "USE_RISK_PERCENT": True,               # True = ukuran posisi % dari saldo USDT free
    "RISK_PERCENT": 95.0,                     # dipakai jika USE_RISK_PERCENT = True
    "POSITION_SIZE_USDT": 5.0,              # dipakai jika USE_RISK_PERCENT = False
    "MAX_POSITION_USDT": 10.0,

    # --- Exit ---
    "USE_TP": True,
    "TP_PCT": 6.0,
    "USE_BREAKEVEN": True,
    "BE_TRIGGER_PCT": 3.0,
    "BE_LOCK_PCT": 0.3,
    "USE_TRAILING": True,
    "TRAILING_START_PCT": 4.0,
    "TRAILING_STEP_PCT": 1.5,
    "MAX_HOLD_MINUTES": 240,                 # paksa keluar kalau kelamaan hold (hindari nyangkut di pump mati)
    "MOMENTUM_FADE_EXIT": True,
    "MOMENTUM_FADE_RANK_THRESHOLD": 30,      # keluar dini kalau sudah tidak masuk top-30 gainer lagi

    # --- Filter & jarak antar-trade ---
    "MAX_SPREAD_PCT": 0.5,                   # altcoin biasanya spread lebih lebar dari BTCUSDT
    "COOLDOWN_MINUTES_AFTER_CLOSE": 10,
    "MIN_SECONDS_BETWEEN_TRADES": 60,

    # --- Kontrol risiko (konsep sama dengan mode grid) ---
    "USE_EQUITY_STOP": False,              # matikan (False) utk nonaktifkan DD Stop
    "MAX_DRAWDOWN_PERCENT": 20.0,
    "USE_DAILY_STOP": False,               # matikan (False) utk nonaktifkan Daily Stop
    "MAX_DAILY_LOSS_PERCENT": 5.0,
    "DAILY_PROFIT_TARGET_PERCENT": 10.0,
    "CLOSE_ALL_AT_LIMIT": True,
    "DD_COOLDOWN_HOURS": 24,

    # --- File state & log (SENGAJA beda nama dari mode grid, supaya bisa
    #     dijalankan berdua tanpa saling menimpa) ---
    "STATE_FILE": "pump_bot_state.json",
    "LOG_FILE": "pump_bot.log",
    "HEARTBEAT_INTERVAL_SECONDS": 300,
}