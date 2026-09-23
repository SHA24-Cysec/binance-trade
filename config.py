"""
Konfigurasi Bot Pump Scanner (Binance Spot)
============================================

Catatan: file ini dulu juga memuat blok CONFIG untuk bot grid martingale.
Bot grid sudah dihapus, jadi kini hanya tersisa PUMP_CONFIG.

Mode pump scanner TIDAK memprediksi pump sebelum terjadi -- ia mendeteksi koin
yang harganya SUDAH naik signifikan + volume tinggi dalam 24 jam terakhir, lalu
mengkonfirmasi lewat candle 5 menit apakah momentumnya kelihatan masih
berlanjut, DAN apakah harga saat ini masih wajar dibanding VWAP bergulir
jangka pendek (tidak kepanasan/ekstrem), sebelum ikut masuk. Ini reaktif
(momentum chasing), bukan prediktif. Hanya satu entry per rotasi (tanpa
averaging-down), dengan Stop Loss/TP/Breakeven/Trailing untuk keluar.

Kredensial diambil dari file .env, JANGAN taruh langsung di file config.py ini
(kalau ditulis di sini, risiko ke-commit ke Git atau ke-share tanpa sengaja
jadi besar). Baris "API_KEY" dan "API_SECRET" di bawah otomatis membaca dari
file .env di folder yang sama dengan config.py ini, lewat library
python-dotenv (sudah ada di requirements.txt). Kalau file .env belum ada atau
isinya kosong, nilainya jadi string kosong dan bot akan gagal autentikasi ke
Binance (tapi dashboard tetap bisa jalan mode read-only).

============================================================
CARA MENAMBAHKAN API KEY LEWAT FILE .env (Windows maupun Linux/macOS)
============================================================

Sudah disediakan file contoh bernama ".env.example" di folder yang sama
dengan config.py ini. Langkahnya sama persis di Windows maupun Linux,
cuma beda perintah salin file:

1. Salin ".env.example" jadi file baru bernama ".env" (tanpa akhiran
   .example):
       Linux / macOS  :  cp .env.example .env
       Windows (PowerShell atau cmd) :  copy .env.example .env
   (bisa juga cukup duplikat filenya lewat File Explorer / file manager,
   lalu ganti namanya jadi ".env")

2. Buka file ".env" yang baru dibuat itu dengan editor teks apa saja
   (Notepad, VS Code, nano, dll), lalu ganti isinya jadi API key/secret
   Binance Anda yang sebenarnya, contoh:
       BINANCE_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
       BINANCE_API_SECRET=yyyyyyyyyyyyyyyyyyyyyyyyyyyyyy
   Tidak perlu tanda kutip di sekeliling nilainya, dan tidak perlu spasi
   di sekitar tanda "=".

3. Simpan file ".env", lalu jalankan bot seperti biasa:
       python3 run.py        (Linux/macOS)
       python run.py         (Windows)
   Library python-dotenv otomatis membaca file ".env" ini setiap kali
   config.py di-import, tidak perlu restart terminal atau set apa pun
   secara manual di sistem operasi.

4. File ".env" WAJIB ditempatkan di folder yang sama dengan config.py
   (folder utama proyek ini), supaya otomatis terbaca.

Catatan penting:
- File ".env" sudah didaftarkan di .gitignore, jadi TIDAK akan pernah
  ter-commit ke Git secara tidak sengaja. Yang aman di-commit hanya
  ".env.example" (isinya cuma contoh format, bukan kredensial asli).
- Kalau butuh cara lama (lewat environment variable OS, tanpa file .env),
  itu tetap didukung sebagai cadangan -- kalau BINANCE_API_KEY /
  BINANCE_API_SECRET sudah ada sebagai environment variable OS, python-dotenv
  TIDAK akan menimpanya; nilai dari file .env hanya dipakai untuk variabel
  yang belum di-set di level OS.

============================================================
PERINGATAN KEAMANAN
============================================================
- JANGAN pernah menulis API key/secret langsung di file config.py ini,
  file .py lain, atau commit ke Git -- siapa pun yang bisa baca file/repo
  otomatis bisa pakai akun Binance Anda.
- JANGAN commit file ".env" (yang berisi kredensial asli) ke Git atau
  bagikan ke siapa pun. Yang boleh dibagikan/di-commit hanya
  ".env.example".
- Kalau bikin API key di Binance, aktifkan HANYA permission yang benar-benar
  dipakai bot ini (Enable Spot Trading kalau DRY_RUN mau dimatikan). JANGAN
  aktifkan permission "Enable Withdrawals" sama sekali. Permission yang sama
  ini juga sudah cukup untuk fitur dust sweep (USE_DUST_SWEEP di bawah) --
  tidak perlu permission tambahan apa pun.
- Kalau API key/secret pernah tidak sengaja bocor (ke-screenshot, ke-share,
  ke-commit ke repo publik, dsb), langsung hapus/revoke key itu di halaman
  Binance API Management dan buat key baru.
"""

import os

try:
    from dotenv import load_dotenv
    # Cari file .env di folder yang sama dengan config.py ini, apapun dari
    # mana skrip dijalankan (run.py, dashboard.py, backtest.py, dll).
    _ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(dotenv_path=_ENV_PATH, override=False)
except ImportError:
    # python-dotenv belum terpasang (mis. requirements.txt belum di-install).
    # Bot tetap bisa jalan kalau BINANCE_API_KEY/SECRET sudah di-set manual
    # sebagai environment variable OS -- cuma file .env tidak akan terbaca.
    pass

PUMP_CONFIG = {
    "QUOTE_ASSET": "USDT",
    "DRY_RUN": True,                          # default AMAN: simulasi tanpa order sungguhan. Ubah ke False kalau sudah yakin mau live
    "BASE_URL": "https://api.binance.com",
    "API_KEY": os.environ.get("BINANCE_API_KEY", ""),    # diisi otomatis dari file .env (lihat panduan di atas)
    "API_SECRET": os.environ.get("BINANCE_API_SECRET", ""),  # diisi otomatis dari file .env (lihat panduan di atas)

    # --- Scan & seleksi kandidat ---
    "MARKET_SCAN_INTERVAL_SECONDS": 300,     # scan seluruh pasar tiap 5 menit
    "LOOP_INTERVAL_SECONDS": 15,             # cek TP/BE/trailing tiap 15 detik
    "MIN_PUMP_PCT_24H": 13.0,                 # minimal naik 8% dalam 24 jam utk dianggap kandidat
    "MIN_QUOTE_VOLUME_USDT_24H": 2_000_000,  # minimal volume 24 jam (hindari koin ilikuid/rawan manipulasi)
    "TOP_N_CANDIDATES_TO_CONFIRM": 10,       # dari hasil ranking, cek candle utk N teratas
    "CONFIRM_INTERVAL": "5m",
    "CONFIRM_LOOKBACK_BARS": 20,
    "MIN_CLOSE_POSITION_IN_RANGE": 0.35,     # lihat market_scanner.confirm_momentum()
    "USE_VWAP_FILTER": True,                 # tolak kandidat yang terlalu jauh dari VWAP bergulir jangka pendek
    "VWAP_MAX_EXTENSION_PCT": 3.5,            # tolak kalau harga > 5% di atas VWAP (window = CONFIRM_LOOKBACK_BARS); harga di BAWAH VWAP juga selalu ditolak
    "EXTRA_EXCLUDE_SYMBOLS": [],             # mis. ["SOMEUSDT"] kalau mau blacklist manual

    # --- Ukuran posisi (tanpa martingale -- sekali entry per rotasi) ---
    "USE_RISK_PERCENT": True,               # True = ukuran posisi % dari saldo USDT free
    "RISK_PERCENT": 95.0,                     # dipakai jika USE_RISK_PERCENT = True
    "POSITION_SIZE_USDT": 5.0,              # dipakai jika USE_RISK_PERCENT = False
    "MAX_POSITION_USDT": 10.0,

    # --- Exit ---
    "USE_TP": True,
    "TP_PCT": 4.0,
    "USE_STOP_LOSS": True,                   # kerugian maksimum per-trade dari harga entry, exit paksa di harga pasar
    "SL_PCT": 1.8,                            # contoh: 3.0 = keluar kalau rugi >= 3% dari entry (SEBELUM Breakeven/Trailing aktif)
    "USE_BREAKEVEN": True,
    "BE_TRIGGER_PCT": 1.0,
    "BE_LOCK_PCT": 0.15,
    "USE_TRAILING": True,
    "TRAILING_START_PCT": 1.5,
    "TRAILING_STEP_PCT": 0.6,
    "MAX_HOLD_MINUTES": 45,                 # paksa keluar kalau kelamaan hold (hindari nyangkut di pump mati)
    "MOMENTUM_FADE_EXIT": True,
    "MOMENTUM_FADE_RANK_THRESHOLD": 30,      # keluar dini kalau sudah tidak masuk top-30 gainer lagi

    # --- Filter & jarak antar-trade ---
    "MAX_SPREAD_PCT": 0.5,                   # altcoin biasanya spread lebih lebar dari BTCUSDT
    "COOLDOWN_MINUTES_AFTER_CLOSE": 10,
    "MIN_SECONDS_BETWEEN_TRADES": 60,

    # --- Kontrol risiko ---
    "USE_EQUITY_STOP": False,              # matikan (False) utk nonaktifkan DD Stop
    "MAX_DRAWDOWN_PERCENT": 20.0,
    "USE_DAILY_STOP": False,               # matikan (False) utk nonaktifkan Daily Stop
    "MAX_DAILY_LOSS_PERCENT": 5.0,
    "DAILY_PROFIT_TARGET_PERCENT": 10.0,
    "CLOSE_ALL_AT_LIMIT": True,
    "DD_COOLDOWN_HOURS": 24,

    # --- File state & log ---
    "STATE_FILE": "pump_bot_state.json",
    "LOG_FILE": "pump_bot.log",
    "HEARTBEAT_INTERVAL_SECONDS": 300,
    "CONTROL_FILE": "pump_bot_control.json",  # perintah manual dari dashboard (mis. "Jual Sekarang")

    # --- Dust sweep ke BNB ---
    # Setelah SEBUAH posisi ditutup (SL/TP/BE/Trailing/manual/dsb), kalau
    # masih ada sisa saldo KECIL (dust) dari koin itu di akun -- biasanya
    # dari pembulatan qty ke LOT_SIZE bursa -- bot mencoba mengonversinya ke
    # BNB lewat endpoint resmi Binance (POST /sapi/v1/asset/dust). HANYA
    # menyentuh base asset dari simbol yang baru saja ditutup, TIDAK PERNAH
    # "menyapu semua aset kecil di akun" -- modal USDT/BNB Anda tidak pernah
    # ikut disentuh fitur ini (proteksi ini di kode, bukan bisa
    # dimatikan lewat config). Di mode DRY_RUN, fitur ini tidak pernah
    # memanggil API sungguhan (hanya simulasi/log).
    "USE_DUST_SWEEP": True,
}
