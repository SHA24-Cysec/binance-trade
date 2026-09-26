"""
Konfigurasi Bot Pump Scanner (Binance Spot)
============================================

Catatan: file ini dulu juga memuat blok CONFIG untuk bot grid martingale.
Bot grid sudah dihapus, jadi kini hanya tersisa PUMP_CONFIG.

Strategi bot ini adalah PUMP MOMENTUM, long only, di pasar Spot. Bot
hanya memakai candle yang SUDAH tertutup dan kronologis. Entry membutuhkan
minimal 3 dari 4 konfirmasi: EMA9 cross EMA21, RSI sehat, MACD histogram
menguat, dan higher low. Volume harian serta volume rolling menjadi gerbang
pump berlapis. Hanya satu entry per rotasi (tanpa averaging-down dan tanpa
martingale), dengan Stop Loss/TP/Breakeven/Trailing berbasis ATR.

Kredensial diambil dari file .env, JANGAN taruh langsung di file config.py ini
(kalau ditulis di sini, risiko ke-commit ke Git atau ke-share tanpa sengaja
jadi besar). Baris "API_KEY" dan "API_SECRET" di bawah otomatis membaca dari
file .env di folder yang sama dengan config.py ini, lewat library
python-dotenv (sudah ada di requirements.txt). Kalau file .env belum ada atau
isinya kosong, nilainya menjadi string kosong dan bot LIVE akan menolak Start.
Dashboard tetap bisa berjalan dan mode PAPER tetap tidak memerlukan key.

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
- Environment variable OS tetap didukung bila `.env` belum berisi nilai.
  Jika dashboard menyimpan kredensial ke `.env`, nilai file itu sengaja
  mengalahkan environment proses lama (`override=True`) agar perubahan UI
  benar-benar berlaku pada restart bot.

============================================================
PERINGATAN KEAMANAN
============================================================
- JANGAN pernah menulis API key/secret langsung di file config.py ini,
  file .py lain, atau commit ke Git -- siapa pun yang bisa baca file/repo
  otomatis bisa pakai akun Binance Anda.
- JANGAN commit file ".env" (yang berisi kredensial asli) ke Git atau
  bagikan ke siapa pun. Yang boleh dibagikan/di-commit hanya
  ".env.example".
- Kalau bikin API key di Binance (hanya diperlukan untuk mode LIVE),
  aktifkan HANYA permission yang benar-benar dipakai bot ini (Enable Spot
  Trading, karena di LIVE bot mengirim order sungguhan). JANGAN
  aktifkan permission "Enable Withdrawals" sama sekali. Permission yang sama
  ini juga sudah cukup untuk fitur dust sweep (USE_DUST_SWEEP di bawah) --
  tidak perlu permission tambahan apa pun.
- Kalau API key/secret pernah tidak sengaja bocor (ke-screenshot, ke-share,
  ke-commit ke repo publik, dsb), langsung hapus/revoke key itu di halaman
  Binance API Management dan buat key baru.
"""

import os
from copy import deepcopy

try:
    from dotenv import load_dotenv
    # Cari file .env di folder yang sama dengan config.py ini, apapun dari
    # mana skrip dijalankan (run.py, dashboard.py, backtest.py, dll).
    _ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    # Kredensial yang disimpan dashboard di .env adalah sumber aktif. Ini
    # sengaja override environment proses supaya perubahan dari UI benar-benar
    # berlaku setelah restart bot, termasuk bila terminal lama masih memiliki
    # BINANCE_API_KEY/BINANCE_API_SECRET.
    load_dotenv(dotenv_path=_ENV_PATH, override=True)
except ImportError:
    # python-dotenv belum terpasang (mis. requirements.txt belum di-install).
    # Bot tetap bisa jalan kalau BINANCE_API_KEY/SECRET sudah di-set manual
    # sebagai environment variable OS -- cuma file .env tidak akan terbaca.
    pass

PUMP_CONFIG = {
    "QUOTE_ASSET": "USDT",

    # --- Pemilihan mode: PAPER atau LIVE ---
    # "PAPER" = SIMULASI penuh lokal. Data pasar (harga, order book, kline,
    #           exchangeInfo) diambil ASLI dari Binance produksi lewat endpoint
    #           publik (REST + WebSocket) TANPA API key dan TANPA tanda tangan,
    #           tetapi eksekusi order, fee, dan saldo disimulasikan lokal dan
    #           disimpan ke file. Jalur LOGIKA STRATEGI sama persis dengan LIVE;
    #           yang berbeda hanya lapisan eksekusi dan sumber saldo.
    # "LIVE"  = order sungguhan ke Binance produksi memakai UANG ASLI.
    #
    # PAPER TIDAK memerlukan API key (semua data dari endpoint publik).
    # LIVE memerlukan BINANCE_API_KEY/BINANCE_API_SECRET produksi di file .env.
    #
    # Pengaman: nilai MODE yang tidak dikenal / typo / kosong TIDAK pernah
    # diam-diam dianggap LIVE. Nilai di sini adalah default immutable. Pilihan
    # dashboard disimpan terpisah di pump_bot_runtime.json dan digabung saat
    # config dimuat. Default aman tetap PAPER.
    "MODE": "PAPER",                          # "PAPER" (default, aman) atau "LIVE"

    # Tampilkan fitur Backtest di dashboard saat MODE="LIVE"?
    #
    # False (default) = tab Backtest DISEMBUNYIKAN saat mode LIVE, dan
    #                   endpoint /api/backtest/* menolak permintaan dengan
    #                   HTTP 403. Di mode PAPER backtest tetap tersedia
    #                   seperti biasa.
    # True            = backtest tetap tersedia di kedua mode.
    #
    # Alasan defaultnya False: backtest menarik data historis dalam jumlah
    # besar dari endpoint publik Binance (paging /api/v3/klines). Saat bot
    # sedang jalan dengan uang asli (LIVE), beban itu ikut menghabiskan jatah
    # rate-limit IP yang sama dengan yang dipakai bot untuk memindai pasar
    # dan mengirim order. Kalau jatah habis, Binance membalas HTTP 429 dan
    # dapat berlanjut ke blokir IP sementara (HTTP 418), yang berarti bot
    # bisa gagal menutup posisi tepat waktu. Di PAPER risiko uang asli tidak
    # ada, jadi backtest dibiarkan tersedia.
    #
    # Ini murni soal pemisahan alat analisis dari operasional live. Kalau
    # Anda memang perlu backtest sambil live (misalnya dashboard berjalan
    # di mesin terpisah dengan IP berbeda dari bot), ubah saja ke True.
    "SHOW_BACKTEST_IN_LIVE": False,

    # Base URL REST produksi publik. Dipakai KEDUA mode untuk DATA PASAR
    # (PAPER: hanya data; LIVE: data + order bertanda tangan).
    "LIVE_BASE_URL": "https://api.binance.com",
    # REQUEST_WEIGHT Binance dihitung per IP. Semua client proses produksi
    # berbagi ledger file ini agar bot, dashboard, dan backtest tidak berebut
    # kuota secara buta. File runtime diabaikan Git.
    "RATE_LIMIT_STATE_FILE": "binance_rate_limit_state.json",
    "RATE_LIMIT_WEIGHT_LIMIT": 6000,
    "RATE_LIMIT_SAFETY_MARGIN": 100,
    "API_KEY": os.environ.get("BINANCE_API_KEY", ""),    # hanya WAJIB untuk LIVE; PAPER mengabaikannya
    "API_SECRET": os.environ.get("BINANCE_API_SECRET", ""),  # hanya WAJIB untuk LIVE; PAPER mengabaikannya

    # ============================================================
    # --- Pengaturan mode PAPER (simulasi eksekusi lokal) ---
    # Semua kunci di bawah HANYA berpengaruh saat MODE="PAPER". Di LIVE
    # diabaikan. Tarif fee memakai TAKER_FEE_PCT + USE_BNB_FEE_DISCOUNT yang
    # sudah ada di bawah (satu sumber kebenaran, dipakai backtest juga).
    # ============================================================
    # Saldo virtual awal per aset. Bot memakai QUOTE_ASSET (USDT) sebagai modal.
    "PAPER_INITIAL_BALANCES": {"USDT": 10000.0},
    # File state akun simulasi (saldo, order, riwayat trade, total fee).
    # OTOMATIS diberi akhiran mode -> pump_paper_account_paper.json. Hanya
    # ditulis di PAPER; LIVE tidak pernah menyentuh file ini.
    "PAPER_ACCOUNT_STATE_FILE": "pump_paper_account.json",
    # Kedalaman order book (level) yang diambil untuk "berjalan" saat mengisi
    # market order. Semakin dalam, semakin realistis slippage-nya.
    "PAPER_DEPTH_LIMIT": 100,
    # Timeout default (detik) untuk order LIMIT yang tak kunjung terisi
    # (didukung mesin simulasi; bot pump sendiri hanya memakai MARKET).
    "PAPER_LIMIT_ORDER_TIMEOUT_SECONDS": 60,

    # --- Data pasar: WebSocket (primer) + REST (wajib untuk yang tak ada WS) ---
    # True (default) = HYBRID. WebSocket jadi sumber utama harga/bookTicker/
    #                  depth/kline real-time; REST hanya dipakai untuk
    #                  exchangeInfo, kline historis, depth snapshot awal, dan
    #                  fallback saat WS basi/putus. Loop utama berhenti nge-poll
    #                  REST berulang.
    # False          = REST polling penuh (perilaku lama), berguna untuk debug
    #                  atau lingkungan yang memblokir WebSocket.
    "USE_WEBSOCKET": True,
    # Base endpoint WebSocket market data produksi (dicek 2026-09-24 dari
    # developers.binance.com/docs/binance-spot-api-docs/web-socket-streams).
    # Mirror khusus market data: wss://data-stream.binance.vision
    "WS_BASE_URL": "wss://stream.binance.com:9443",
    # Usia MAKSIMUM data pasar (detik) yang boleh dipakai untuk mengisi order
    # simulasi. Data yang lebih tua dari ini memicu fallback REST; kalau REST
    # juga gagal, order simulasi DITOLAK agar tidak jalan di atas data basi.
    "MAX_MARKET_DATA_AGE_SECONDS": 10.0,

    # --- Scan & seleksi kandidat ---
    "MARKET_SCAN_INTERVAL_SECONDS": 300,     # scan seluruh pasar tiap 5 menit
    "LOOP_INTERVAL_SECONDS": 15,             # cek TP/BE/trailing tiap 15 detik
    "MIN_QUOTE_VOLUME_USDT_24H": 3099455.404470322,  # kandidat walk-forward; dibatasi untuk pair yang lebih likuid
    # Berapa simbol teratas (urut volume kuotasi 24 jam) yang candle-nya
    # diunduh tiap siklus scan. Angka ini yang menjaga rate limit: satu
    # panggilan klines berbobot IP 2 sedangkan plafon REQUEST_WEIGHT adalah
    # 6000 per menit per IP (dibaca dari /api/v3/exchangeInfo, dicek
    # 2026-09-25), ditambah 80 bobot untuk ticker 24 jam seluruh pasar.
    # OPTIMASI MANUAL (return-focused, DD dijaga): dinaikkan 10 -> 15 supaya
    # lebih banyak kandidat dikonfirmasi tiap scan = lebih banyak peluang entry.
    # Beban IP tetap kecil: 15 x weight 2 = 30 dari plafon 6000/menit.
    "TOP_N_CANDIDATES_TO_CONFIRM": 15,
    "CONFIRM_INTERVAL": "5m",
    # Jendela candle tertutup untuk satu keputusan entry. Nilai minimum
    # dihitung oleh strategy.required_lookback_bars() dari parameter struktur
    # di bawah, dan divalidasi di settings_schema.py. Limit endpoint klines
    # adalah 1000 candle per panggilan (dicek 2026-09-25).
    # OPTIMASI MANUAL: 53 -> 60. Wajib >= SWING_LOOKBACK_BARS(20) +
    # 2*SWING_PIVOT_WING_BARS(3) + MAX_BARS_BREAKOUT_TO_RETEST(24) = 50.
    # Diberi margin ke 60 agar indikator momentum dan volume rolling memiliki data cukup.
    "CONFIRM_LOOKBACK_BARS": 60,
    # Posisi close di dalam range candle retest (0 = di low, 1 = di high).
    # Kunci lama ini dipakai ulang oleh market_scanner.detect_pullback_retest().
    # OPTIMASI MANUAL: 0.273 -> 0.35. Menuntut candle retest menutup di bagian
    # atas rentangnya = reclaim lebih meyakinkan = kualitas entry naik (menekan
    # retest gagal). Ini penyeimbang dari gerbang pump yang dilonggarkan di
    # bawah, supaya jumlah trade naik tanpa menurunkan kualitas drastis.
    "MIN_CLOSE_POSITION_IN_RANGE": 0.35,

    # ------------------------------------------------------------------
    # PARAMETER STRATEGI PULLBACK DAN RETEST
    # ------------------------------------------------------------------
    # SEMUA angka di blok ini adalah TITIK AWAL yang BELUM divalidasi. Nilai
    # ini dipilih konservatif berdasarkan struktur aturannya saja, bukan dari
    # hasil backtest. Validasi dulu lewat backtest.py dan portfolio_backtest.py
    # pada periode pengembangan dan periode uji yang terpisah sebelum dipakai
    # dengan uang sungguhan.
    #
    # KANDIDAT WALK-FORWARD DIIMPLEMENTASIKAN 2026-09-25:
    # Nilai setup dan gerbang pump di bawah berasal dari kandidat OOS yang
    # lolos minimum trade pada laporan optimization_report.md. Kandidat ini
    # hanya tervalidasi pada pilot 180 hari dan 29 pair; hasilnya belum
    # menjadi alasan untuk LIVE. Tetap gunakan PAPER dan jangan mematikan
    # USE_EQUITY_STOP/USE_DAILY_STOP.
    #
    # Berapa candle ke belakang yang dipindai untuk mencari swing high yang
    # menjadi level breakout.
    # OPTIMASI MANUAL: 23 -> 20. Swing high yang lebih baru lebih responsif
    # terhadap breakout terkini = lebih banyak setup.
    "SWING_LOOKBACK_BARS": 20,
    # Jumlah candle di kiri dan kanan yang harus lebih rendah agar sebuah
    # candle dianggap pivot high. Sayap kanan wajib sudah tertutup, itulah
    # yang mencegah level breakout memakai data masa depan.
    # OPTIMASI MANUAL: 4 -> 3. Sayap pivot lebih pendek mendeteksi lebih banyak
    # pivot high valid = lebih banyak kandidat breakout. Tetap >=3 agar pivot
    # tidak jadi noise satu-dua candle.
    "SWING_PIVOT_WING_BARS": 3,
    # Kunci legacy untuk kompatibilitas konfigurasi lama. Tidak dipakai oleh
    # strategi momentum baru.
    "VWAP_MIN_BARS_AFTER_ANCHOR": 4,
    # Umur maksimum setup: kalau retest tidak datang dalam sekian candle,
    # setup dianggap gugur dan bot mencari breakout berikutnya.
    # OPTIMASI MANUAL: 22 -> 24. Jendela sedikit lebih panjang agar lebih banyak
    # breakout sempat menghasilkan retest sebelum setup gugur.
    "MAX_BARS_BREAKOUT_TO_RETEST": 24,
    # Berapa kali harga boleh berkunjung ke zona sebelum setup dianggap lemah.
    # Kunjungan dihitung per peristiwa, bukan per candle.
    # OPTIMASI MANUAL: 1 -> 2. Banyak retest valid menyentuh zona dua kali
    # sebelum reclaim; mengizinkan 2 kunjungan menaikkan jumlah entry tanpa
    # menerima zona yang sudah terlalu sering diuji (lemah).
    "MAX_RETEST_TOUCHES": 2,

    # ------------------------------------------------------------------
    # GERBANG PUMP (saringan semesta, WAJIB, bukan sekadar prioritas urutan)
    # ------------------------------------------------------------------
    # Sebuah simbol hanya boleh masuk semesta kandidat kalau KEDUA syarat di
    # bawah terpenuhi. Pemeriksaan terjadi SEBELUM deteksi pullback retest,
    # jadi struktur setup hanya dicari pada koin yang memang sedang bergerak.
    #
    # Kenapa gerbang ini dihidupkan kembali: tanpa syarat kenaikan, semesta
    # kandidat didominasi pair besar yang sedang sideways atau turun. Setup
    # pullback retest pada koin yang tren harian dan volumenya tidak mendukung
    # lebih sering berakhir sebagai retest yang gagal.
    #
    # Sumber data syarat 1 dan 2 (bagian volume 24 jam berjalan) adalah
    # respons GET /api/v3/ticker/24hr yang SUDAH diambil sekali untuk seluruh
    # pasar tiap siklus scan, jadi keduanya tidak menambah request.
    #
    # Minimal kenaikan harga 24 jam, dalam persen, dari field
    # priceChangePercent. Koin yang turun 24 jam otomatis gugur.
    # OPTIMASI MANUAL: 9.22 -> 6.0. Gerbang kenaikan dilonggarkan agar lebih
    # banyak koin bermomentum masuk semesta kandidat = lebih banyak peluang.
    # Tetap 6% (bukan 0) supaya strategi pullback-retest tetap membeli pullback
    # DI DALAM tren naik, bukan di koin datar/turun.
    "PUMP_MIN_24H_CHANGE_PCT": 6.0,
    # Volume dianggap "sedang naik" bila quoteVolume 24 jam berjalan minimal
    # sekian kali rata-rata volume kuotasi 7 hari PENUH sebelumnya (candle 1d
    # yang sudah tertutup). Candle harian diminta HANYA untuk simbol yang
    # sudah lolos syarat kenaikan, yaitu subset kecil, sehingga bobot IP
    # tambahannya kecil (2 per simbol). Perbandingan terhadap rata-rata 7 hari
    # dipilih daripada menyimpan riwayat volume di file JSON baru, supaya
    # tidak ada masalah cold start dan supaya angka yang sama bisa dihitung
    # ulang untuk titik waktu historis mana pun di backtest.
    # OPTIMASI MANUAL: 2.71 -> 2.0. Sedikit lebih longgar untuk menerima koin
    # dengan volume yang sedang naik nyata (2x rata-rata 7 hari) tanpa menuntut
    # lonjakan ekstrem yang jarang. Tetap >1 (syarat validasi).
    "PUMP_VOLUME_SURGE_MULT": 2.0,
    # Filter korelasi BTC. Nilai drop dihitung dari candle tertutup pada
    # jendela BTC_LOOKBACK_BARS oleh pemanggil data pasar.
    "BTC_FILTER_ENABLED": True,
    "BTC_MAX_DROP_PCT": 3.0,
    "BTC_LOOKBACK_BARS": 3,

    # Konfirmasi momentum volume pada candle timeframe entry. Volume candle
    # terakhir yang sudah close harus melebihi rata-rata candle sebelumnya.
    "ROLLING_VOLUME_FILTER_ENABLED": True,
    "ROLLING_VOLUME_LOOKBACK_BARS": 20,
    "ROLLING_VOLUME_SURGE_MULT": 2.0,
    "ROLLING_VOLUME_CONFIRMATION_BARS": 1,

    # Exit adaptif untuk volatilitas scalping. False mempertahankan perilaku
    # persen lama agar state dan konfigurasi lama tetap kompatibel.
    "USE_ATR_EXIT": True,
    "ATR_PERIOD": 14,
    "ATR_MULT_SL": 1.5,
    "ATR_MULT_TP": 3.0,
    "ATR_MULT_TRAIL": 1.0,
    "ATR_MULT_BE_TRIGGER": 1.0,
    "ATR_MULT_BE_LOCK": 0.1,
    "ATR_MULT_TRAIL_START": 1.5,

    "EXTRA_EXCLUDE_SYMBOLS": [],             # mis. ["SOMEUSDT"] kalau mau blacklist manual

    # --- Filter usia listing (proteksi koin baru) ---
    # Koin yang baru listing beberapa hari punya riwayat tipis, spread lebar,
    # dan sering menjadi pump artifisial "hari listing" yang langsung kolaps.
    # Bot menolak entry ke pair yang usianya di bawah ambang ini, dicek dari
    # candle harian pertamanya (1 panggilan klines weight 2 per kandidat,
    # di-cache permanen). 0 = nonaktifkan filter ini.
    "MIN_LISTING_AGE_DAYS": 7,

    # ==================================================================
    # WATCHLIST PEMANTAUAN (READ-ONLY, TIDAK MEMENGARUHI KEPUTUSAN TRADE)
    # ==================================================================
    #
    # PENTING, BACA DULU: daftar ini MURNI UNTUK DITAMPILKAN DI DASHBOARD.
    # Bot TETAP memindai SELURUH pair USDT seperti sebelumnya. Tidak ada satu
    # baris pun di market_scanner.py / pump_scanner_bot.py yang membaca
    # daftar ini, jadi menambah atau menghapus simbol di sini TIDAK mengubah
    # koin apa yang dibeli bot, tidak mengubah ranking kandidat, dan tidak
    # mengubah hasil backtest. Kalau suatu hari Anda ingin watchlist ikut
    # menyaring entry, itu perubahan terpisah yang harus dilakukan sadar.
    #
    # Gunanya: saat memantau dashboard Anda tidak perlu menebak koin mana
    # yang sedang "dekat" dengan kondisi masuk bot. Panel watchlist
    # menampilkan harga, perubahan 24 jam, volume, dan status tiap koin
    # terhadap gerbang semesta scanner yang masih berlaku, yaitu
    # MIN_QUOTE_VOLUME_USDT_24H dan MAX_SPREAD_PCT. Gerbang pump scanner
    # yang memerlukan kenaikan 24 jam serta volume harian TIDAK ditampilkan
    # sebagai status watchlist karena panel ini sengaja tidak mengunduh candle
    # harian tambahan dan tidak memengaruhi keputusan entry.
    #
    # ------------------------------------------------------------------
    # DARI MANA DAFTAR INI BERASAL (metodologi, bukan tebakan)
    # ------------------------------------------------------------------
    # Disusun 2026-09-24 dari data pasar Binance Spot yang sesungguhnya,
    # bukan dari daftar "koin populer" atau opini. Datanya:
    #
    #   - 3.710 simbol exchangeInfo + ticker 24 jam + bookTicker, ditarik dari
    #     endpoint data publik resmi Binance (data-api.binance.vision).
    #   - 487 pair USDT lolos aturan struktural bot (status TRADING, spot
    #     diizinkan, bukan stablecoin, bukan leveraged token).
    #   - 182 pair lolos MIN_QUOTE_VOLUME_USDT_24H, ditarik candle 1 jam
    #     selama 120 hari untuk mengukur frekuensi pump.
    #   - 110 pair shortlist ditarik candle 5 menit selama 45 hari
    #     (= CONFIRM_INTERVAL bot, 12.960 candle per simbol).
    #   - Pada tiap candle 5 menit itu dijalankan confirm_entry() asli dari
    #     market_scanner.py. Jadi angka "berapa kali koin ini memicu sinyal"
    #     adalah hasil menjalankan logika keputusan bot itu sendiri, bukan
    #     perkiraan.
    #
    # Skor 0-100 menimbang empat hal yang benar-benar menentukan apakah
    # sebuah koin cocok dengan mesin ini:
    #   35 poin  frekuensi sinyal entry nyata per 30 hari
    #   25 poin  likuiditas: berapa persen waktu volume 24 jam koin itu
    #            berada di atas MIN_QUOTE_VOLUME_USDT_24H, plus volume median
    #   20 poin  spread bid-ask sekarang dibanding MAX_SPREAD_PCT
    #   20 poin  kestabilan operasional dan kelengkapan riwayat pasar
    #
    # Yang SENGAJA dibuang dari daftar:
    #   - 9 saham tokenisasi Binance (bStocks, mis. MSTRB, CRCLB, SOXLB).
    #     Terdeteksi dari data: porsi volume akhir pekan hanya 4-14%,
    #     sementara median crypto 24/7 adalah 25,6%. Harganya ditambatkan ke
    #     bursa saham AS yang tutup akhir pekan, sehingga asumsi pasar
    #     24/7 milik bot ini tidak berlaku untuk mereka.
    #   - Pair dengan spread saat ini melewati MAX_SPREAD_PCT.
    #   - Pair dengan riwayat kurang dari 90 hari (belum cukup bukti).
    #   - Pair dengan kurang dari 3 sinyal dalam 45 hari (terlalu jarang).
    #
    # BATAS KEJUJURAN DATA INI: frekuensi sinyal TIDAK sama dengan
    # profitabilitas. Yang diukur adalah seberapa sering koin memicu kondisi
    # masuk bot, bukan seberapa sering trade-nya berakhir untung. Pasar juga
    # berputar; koin yang aktif hari ini bisa sepi dalam dua bulan. Tinjau
    # ulang daftar ini secara berkala.
    "WATCHLIST_ENABLED": True,               # False = panel watchlist disembunyikan dari dashboard

    # ------------------------------------------------------------------
    # PENYEGARAN DAFTAR OTOMATIS (opsional)
    # ------------------------------------------------------------------
    # Kalau True, dashboard menyusun ULANG daftar di bawah secara berkala
    # dari data Binance terbaru, memakai metodologi yang sama. Hasilnya
    # ditulis ke file terpisah (watchlist_auto_<mode>.json) dan TIDAK
    # PERNAH menimpa config.py -- daftar manual di bawah tetap utuh sebagai
    # cadangan kalau penyegaran gagal atau dimatikan.
    #
    # Tetap tidak memengaruhi keputusan trading apa pun.
    #
    # SOAL BEBAN KE BINANCE (alasan angka-angka di bawah dipilih):
    # Batas resmi 6000 request weight per menit, dihitung PER IP bukan per
    # API key (developers.binance.com, General REST API Information/LIMITS,
    # dicek 2026-09-24). Jadi dashboard dan bot berbagi jatah yang sama.
    # Anggaran default di bawah menghabiskan sekitar 684 weight per siklus,
    # disebar ~15 menit = 0,76% anggaran. Sisanya tetap milik bot.
    #
    # Tiga rem keamanan TIDAK bisa dimatikan lewat config karena menyangkut
    # keselamatan posisi Anda:
    #   1. penyegaran dilewati selama bot memegang posisi terbuka
    #   2. berhenti sendiri kalau sisa kuota weight menipis
    #   3. berhenti total kalau kena 429/418, tidak mencoba ulang
    "WATCHLIST_AUTO_REFRESH": True,          # False = daftar statis, hanya dari WATCHLIST di bawah
    "WATCHLIST_AUTO_INTERVAL_HOURS": 6,      # jarak antar penyegaran
    "WATCHLIST_AUTO_MAX_SYMBOLS": 60,        # kandidat teratas (by likuiditas) yang dinilai
    "WATCHLIST_AUTO_DAYS": 14,               # panjang riwayat candle 5m untuk menilai
    "WATCHLIST_AUTO_KEEP": 26,               # berapa simbol dipertahankan di daftar akhir
    "WATCHLIST_AUTO_MAX_WEIGHT": 900,        # plafon keras weight per siklus
    "WATCHLIST_AUTO_PACE_SECONDS": 2.0,      # jeda antar panggilan (menyebar beban)
    "WATCHLIST_AUTO_MIN_HEADROOM": 0.5,      # berhenti kalau sisa kuota menit ini < 50%
    "WATCHLIST_AUTO_STARTUP_DELAY_SECONDS": 60,  # jangan menyegarkan tepat saat start
    "WATCHLIST": [
        # --- INTI: likuiditas di atas ambang bot >= 90% waktu ---
        # Sinyal di sini paling mungkin benar-benar bisa dieksekusi karena
        # koinnya hampir selalu memenuhi filter volume bot.
        {"symbol": "ZECUSDT",     "tier": "INTI",      "score": 93.0, "note": "33 sinyal/45h, spread 0,001% (tersempit), volume median 109 juta"},
        {"symbol": "ENAUSDT",     "tier": "INTI",      "score": 92.5, "note": "34 sinyal/45h"},
        {"symbol": "ARBUSDT",     "tier": "INTI",      "score": 89.6, "note": "44 sinyal/45h, terbanyak di tier ini"},
        {"symbol": "NEARUSDT",    "tier": "INTI",      "score": 87.1, "note": "28 sinyal/45h, volume median 32 juta"},
        {"symbol": "UNIUSDT",     "tier": "INTI",      "score": 86.5, "note": "27 sinyal/45h, spread 0,011%"},
        {"symbol": "PENGUUSDT",   "tier": "INTI",      "score": 82.3, "note": "26 sinyal/45h, likuiditas 100% waktu"},
        {"symbol": "DASHUSDT",    "tier": "INTI",      "score": 80.3, "note": "27 sinyal/45h"},
        {"symbol": "PUMPUSDT",    "tier": "INTI",      "score": 79.1, "note": "21 sinyal/45h, volume median 11,5 juta"},
        {"symbol": "FILUSDT",     "tier": "INTI",      "score": 78.5, "note": "22 sinyal/45h, spread 0,011%"},
        {"symbol": "INJUSDT",     "tier": "INTI",      "score": 78.4, "note": "23 sinyal/45h"},
        {"symbol": "AVAXUSDT",    "tier": "INTI",      "score": 76.9, "note": "17 sinyal/45h, likuiditas sangat stabil"},
        {"symbol": "SUIUSDT",     "tier": "INTI",      "score": 74.5, "note": "13 sinyal/45h, volume median 25 juta"},

        # --- AKTIF: likuiditas di atas ambang 60-90% waktu ---
        # Aktif berkala. Sinyal cukup sering, tapi ada periode koin ini
        # tidak memenuhi filter volume sehingga bot mengabaikannya.
        {"symbol": "CHIPUSDT",    "tier": "AKTIF",  "score": 76.5, "note": "32 sinyal/45h, tapi likuiditas cukup hanya 61% waktu"},
        {"symbol": "ZAMAUSDT",    "tier": "AKTIF",  "score": 71.7, "note": "18 sinyal/45h, pump tertinggi 51% dalam 120 hari"},
        {"symbol": "CRVUSDT",     "tier": "AKTIF",  "score": 69.2, "note": "24 sinyal/45h"},
        {"symbol": "TIAUSDT",     "tier": "AKTIF",  "score": 65.2, "note": "16 sinyal/45h, likuiditas cukup 72% waktu"},
        {"symbol": "ETHFIUSDT",   "tier": "AKTIF",  "score": 64.4, "note": "16 sinyal/45h"},
        {"symbol": "ZROUSDT",     "tier": "AKTIF",  "score": 60.8, "note": "19 sinyal/45h, spread 0,133% relatif lebar"},
        {"symbol": "POLUSDT",     "tier": "AKTIF",  "score": 59.6, "note": "hanya 7 sinyal/45h, tapi spread 0,010% dan likuid"},
        {"symbol": "SEIUSDT",     "tier": "AKTIF",  "score": 59.5, "note": "11 sinyal/45h, likuiditas cukup 64% waktu"},

        # --- SPEKULATIF: likuiditas di atas ambang < 60% waktu ---
        # PERHATIAN: koin di tier ini paling sering memicu sinyal, tapi
        # justru karena volumenya naik-turun ekstrem. Volume median mereka
        # ADA DI BAWAH MIN_QUOTE_VOLUME_USDT_24H, artinya di hari biasa bot
        # memang tidak akan menyentuhnya; mereka hanya lolos saat sedang
        # ramai. Risiko slippage pada order MARKET di sini nyata.
        {"symbol": "MUBARAKUSDT", "tier": "SPEKULATIF", "score": 79.8, "note": "39 sinyal/45h tapi likuiditas cukup hanya 26% waktu"},
        {"symbol": "NILUSDT",     "tier": "SPEKULATIF", "score": 77.6, "note": "35 sinyal/45h, likuiditas cukup 34% waktu"},
        {"symbol": "WIFUSDT",     "tier": "SPEKULATIF", "score": 74.3, "note": "33 sinyal/45h, likuiditas cukup 42% waktu"},
        {"symbol": "ARUSDT",      "tier": "SPEKULATIF", "score": 68.6, "note": "24 sinyal/45h, pump tertinggi 58%"},
        {"symbol": "PROMUSDT",    "tier": "SPEKULATIF", "score": 66.8, "note": "26 sinyal/45h, volume median hanya 0,9 juta"},
        {"symbol": "FFUSDT",      "tier": "SPEKULATIF", "score": 65.8, "note": "21 sinyal/45h, likuiditas cukup 45% waktu"},
    ],

    # --- Ukuran posisi (tanpa martingale -- sekali entry per rotasi) ---
    #
    # PERINGATAN HASIL AUDIT 2026-09-24 (temuan K-01): RISK_PERCENT=100.0
    # berarti SELURUH modal dipertaruhkan di setiap trade. Stop Loss beberapa
    # persen saja dapat menggerus total akun secara cepat saat rugi beruntun.
    # Default di bawah (25%) adalah titik awal yang lebih masuk akal untuk
    # LIVE; naikkan bertahap HANYA dari data hasil nyata, bukan karena satu
    # backtest terlihat bagus.
    "USE_RISK_PERCENT": True,               # True = ukuran posisi % dari saldo USDT free
    # OPTIMASI MANUAL: 25 -> 30. Karena hanya 1 posisi per rotasi, risiko
    # konkuren = satu posisi. Dengan SL 1.8%, rugi per trade ~ 30% x 1.8% =
    # 0,54% dari equity -- masih moderat dan jauh dari MAX_DRAWDOWN 12%.
    # PENTING untuk LIVE: turunkan lagi & pasang MAX_POSITION_USDT nyata.
    "RISK_PERCENT": 30.0,                      # dipakai jika USE_RISK_PERCENT = True
    "POSITION_SIZE_USDT": 5.0,              # dipakai jika USE_RISK_PERCENT = False
    # Modal awal simulasi backtest. Ini BUKAN saldo LIVE yang dibaca otomatis;
    # ubah sesuai akun yang ingin dimodelkan. Backtest memakai policy sizing
    # yang sama dengan live terhadap angka ini.
    "BACKTEST_INITIAL_EQUITY_USDT": 10_000.0,
    # Asumsi execution backtest. Entry live memakai ask setelah sinyal,
    # sehingga simulasi tidak boleh otomatis membeli di close sinyal tanpa
    # spread, slippage, dan latency.
    "BACKTEST_ENTRY_SPREAD_PCT": 0.10,
    "BACKTEST_SLIPPAGE_PCT": 0.05,
    "BACKTEST_ENTRY_DELAY_BARS": 1,

    # --- Plafon nominal per posisi ---
    #
    # 0 (atau negatif) = TIDAK ADA PLAFON. Ukuran posisi murni mengikuti
    # RISK_PERCENT dari saldo USDT free, jadi persentase yang Anda set
    # benar-benar terpakai berapa pun besar saldo Anda.
    #
    # PERINGATAN SEJARAH (penting, pernah jadi bug diam-diam di config ini):
    # sebelumnya nilai ini 10.0 sementara RISK_PERCENT 95.0. Karena kode
    # memakai min(nominal_dari_persen, MAX_POSITION_USDT), plafon 10 USDT
    # SELALU menang dan RISK_PERCENT praktis tidak pernah terpakai:
    #     saldo   100 USDT -> niat 95 USDT  -> nyatanya 10 USDT (10% saldo)
    #     saldo 1.000 USDT -> niat 950 USDT -> nyatanya 10 USDT (1% saldo)
    #     saldo 5.000 USDT -> niat 4.750    -> nyatanya 10 USDT (0,2% saldo)
    # Makin besar saldo, makin kecil persentase sesungguhnya. Kalau Anda
    # mengisi ulang plafon ini dengan angka > 0, PASTIKAN itu memang yang
    # Anda maksud, dan bot akan memperingatkan di log kalau plafon
    # membatalkan RISK_PERCENT Anda.
    # 0 = tanpa plafon (ikuti RISK_PERCENT sepenuhnya). Default 100: untuk
    # hari-hari pertama LIVE, plafon keras ini membatasi nominal maksimum
    # yang dipertaruhkan per posisi berapa pun saldo Anda. Naikkan/0-kan
    # hanya setelah bot terbukti berperilaku benar dengan uang asli.
    # CATATAN OPTIMASI (PENTING soal return PAPER):
    # Plafon 100 pada modal 10.000 USDT membuat tiap posisi hanya ~1% modal,
    # sehingga RISK_PERCENT praktis TIDAK PERNAH terpakai -- ini pengerem
    # return terbesar. Untuk melepas rem itu KHUSUS di PAPER, "tanpa plafon"
    # (MAX_POSITION_USDT = 0) diterapkan lewat file per-mode
    # pump_bot_settings_paper.json (0 hanya sah di PAPER). Nilai DEFAULT di
    # config.py ini sengaja DIPERTAHANKAN 100 supaya default LIVE tetap
    # konservatif dan tetap LOLOS validasi (LIVE melarang plafon 0).
    # Sebelum LIVE: isi plafon nominal nyata sesuai toleransi Anda.
    "MAX_POSITION_USDT": 100,

    # Bantalan saldo (persen) yang TIDAK ikut dibelanjakan, dipotong dari
    # saldo USDT free sebelum RISK_PERCENT dihitung. Gunanya teknis, bukan
    # filosofi risiko: order MARKET BUY diisi pada harga yang bergerak, dan
    # fee taker 0,1% dipotong dari saldo yang sama. Kalau bot mencoba
    # membelanjakan 100% saldo persis, order sering ditolak bursa dengan
    # error -2010 "Account has insufficient balance".
    # Makin dekat RISK_PERCENT ke 100, makin penting bantalan ini.
    "BALANCE_BUFFER_PCT": 0.5,

    # --- Exit ---
    "USE_TP": True,
    # OPTIMASI MANUAL: 4.0 -> 5.0. Karena trailing aktif, TP berfungsi sebagai
    # plafon; menaikkannya memberi ruang bagi pemenang untuk lari lebih jauh.
    # R:R jadi 5.0/1.8 ~ 2.8:1.
    "TP_PCT": 5.0,
    "USE_STOP_LOSS": True,                   # guard lokal, tetap dipakai sebagai fallback
    "USE_NATIVE_OCO": True,                  # LIVE: OCO SELL native, TP limit + SL limit
    "USE_NATIVE_STOP_LOSS": True,            # fallback LIVE bila client OCO tidak tersedia
    "NATIVE_OCO_LIMIT_BUFFER_PCT": 0.10,     # buffer limit dari trigger agar ada peluang fill
    # SL dipertahankan 1.8: cukup ketat untuk DD stabil, tapi tidak terlalu
    # sempit sehingga posisi ke-stop oleh noise sebelum setup sempat bekerja.
    "SL_PCT": 1.8,                            # keluar paksa kalau rugi >= nilai ini (%) dari entry (SEBELUM Breakeven/Trailing aktif)

    "USE_BREAKEVEN": True,
    # OPTIMASI MANUAL: BE trigger 1.0 -> 1.2, lock 0.15 -> 0.2. BE aktif sedikit
    # lebih lambat agar pullback normal tidak buru-buru menendang ke BE
    # (memberi ruang pemenang berkembang), tapi mengunci profit lebih tegas.
    "BE_TRIGGER_PCT": 1.2,
    "BE_LOCK_PCT": 0.2,
    "USE_TRAILING": True,
    # OPTIMASI MANUAL: start 1.5 -> 1.8, step 0.6 -> 0.9. Trailing mulai setelah
    # momentum terkonfirmasi, dan step lebih lebar memberi napas agar pemenang
    # ikut tren lebih jauh (menangkap gerakan besar = return naik), bukan
    # ke-trail keluar oleh pullback kecil. Invariant: step(0.9) <= SL(1.8).
    "TRAILING_START_PCT": 1.8,
    "TRAILING_STEP_PCT": 0.9,
    # CATATAN: MAX_HOLD_MINUTES (paksa keluar setelah sekian menit) sudah
    # DIHAPUS TOTAL, bukan dinonaktifkan. Alasannya: batas waktu memaksa exit
    # pada harga pasar apa pun tanpa melihat struktur, sehingga posisi yang
    # masih valid secara setup bisa ditutup hanya karena jam dinding.

    # --- Filter & jarak antar-trade ---
    # Spread maksimum (bid-ask) yang masih boleh dimasuki. Ini biaya NYATA
    # yang langsung dibayar setiap kali masuk lewat order MARKET, dan
    # dampaknya berlipat kalau Anda memutar porsi saldo yang besar tiap trade.
    # Nilai 0.5 sebelumnya terlalu longgar: dengan TP 4%, spread 0,5% saja
    # sudah memakan 12,5% dari target profit, ditambah fee 0,2% pulang-pergi.
    # 0.25 lebih realistis untuk altcoin likuid yang lolos filter volume bot ini.
    "MAX_SPREAD_PCT": 0.25,

    # --- Biaya trading (dipakai backtest agar hasilnya jujur) ---
    # Binance Spot VIP0 per 2026: 0,1% maker maupun taker; diskon 25% kalau
    # fee dibayar memakai BNB, sehingga jadi 0,075%.
    # (Sumber: halaman fee resmi Binance & beberapa ringkasan independen,
    # dicek 2026-09-23.)
    # Bot SELALU memakai order MARKET, jadi yang relevan adalah TAKER.
    "TAKER_FEE_PCT": 0.1,                    # taker (order MARKET) Spot VIP0 = 0,1%
    "MAKER_FEE_PCT": 0.1,                    # maker (limit yang mengendap) Spot VIP0 = 0,1%
    "USE_BNB_FEE_DISCOUNT": True,           # True = diskon 25% (0,1% -> 0,075%)
    # OPTIMASI MANUAL: 10 -> 5 (satu candle 5m). Cooldown lebih pendek memberi
    # lebih banyak peluang re-entry setelah posisi ditutup, tanpa memicu
    # entry beruntun dalam satu candle yang sama.
    "COOLDOWN_MINUTES_AFTER_CLOSE": 5,
    "MIN_SECONDS_BETWEEN_TRADES": 60,

    # --- Kontrol risiko ---
    #
    # JARING PENGAMAN MODAL. Sebelum audit 2026-09-24 semua saklar ini MATI
    # (False) sehingga tidak ada satu pun mekanisme yang menghentikan bot
    # saat kerugian menumpuk (temuan K-01), dan CLOSE_ALL_AT_LIMIT tidak punya
    # implementasi di kode (temuan T-06) -- sekarang parameter itu benar-benar
    # bekerja: saat DD stop / daily stop memicu, posisi terbuka ditutup paksa
    # satu kali per episode.
    "USE_EQUITY_STOP": False,               # matikan (False) utk nonaktifkan DD Stop
    # OPTIMASI MANUAL: 15 -> 12. Jaring DD diperketat agar penurunan dari peak
    # equity berhenti lebih awal = DD lebih stabil (inti permintaan Anda).
    "MAX_DRAWDOWN_PERCENT": 12.0,
    "USE_DAILY_STOP": False,                # matikan (False) utk nonaktifkan Daily Stop
    # OPTIMASI MANUAL: 3 -> 5. Sedikit lebih lega agar mesin return punya ruang
    # dalam satu hari (dengan ~0,54% risiko/trade, ini ~9 trade rugi baru
    # menghentikan hari), tetap terbatas untuk menjaga DD.
    "MAX_DAILY_LOSS_PERCENT": 5.0,
    # OPTIMASI MANUAL: 10 -> 15. Target profit harian dinaikkan supaya hari
    # yang bagus tidak terlalu cepat menghentikan entry (prioritas return).
    "DAILY_PROFIT_TARGET_PERCENT": 15.0,
    "CLOSE_ALL_AT_LIMIT": True,
    "DD_COOLDOWN_HOURS": 24,

    # Berapa error API berturut-turut sebelum bot berhenti total. Posisi yang
    # sedang terbuka saat itu berhenti dikelola (SL/TP bot ini pengecekan
    # lokal tiap 15 detik), jadi WAJIB jalankan bot di bawah supervisor yang
    # menyalakannya kembali otomatis (contoh unit systemd ada di
    # pump-bot.service di repo ini). Dengan supervisor aktif, berhenti total
    # hanya berarti jeda singkat, bukan posisi telanjang berjam-jam.
    "MAX_CONSECUTIVE_ERRORS": 20,
    # Supervisor dashboard boleh menghidupkan kembali bot yang crash, tetapi
    # dibatasi agar exception deterministik tidak menjadi restart loop tanpa
    # akhir. Stop manual/dashboard selalu menonaktifkan auto-restart episode itu.
    "SUPERVISOR_AUTO_RESTART": True,
    "SUPERVISOR_MAX_RESTARTS": 5,
    "SUPERVISOR_RESTART_WINDOW_SECONDS": 300,
    "SUPERVISOR_RESTART_BACKOFF_SECONDS": 5,

    # --- File state & log (OTOMATIS dipisah per mode, lihat catatan) ---
    # Nilai di bawah adalah NAMA DASAR. Saat config.py di-import, nama final
    # otomatis disisipkan akhiran mode aktif SEBELUM ekstensinya:
    #   MODE="PAPER" -> pump_bot_state_paper.json, pump_bot_paper.log,
    #                   pump_bot_control_paper.json
    #   MODE="LIVE"  -> pump_bot_state_live.json, pump_bot_live.log,
    #                   pump_bot_control_live.json
    # Tujuannya: data PAPER dan LIVE tidak pernah tertukar/tercampur.
    # Posisi paper yang sedang terbuka tidak mungkin "dilanjutkan" bot
    # saat pindah ke LIVE (atau sebaliknya), dan riwayat trade di dashboard
    # hanya berasal dari mode yang sedang aktif.
    # File versi LAMA tanpa akhiran (pump_bot_state.json, pump_bot.log,
    # pump_bot_control.json) TIDAK dipindahkan/dihapus otomatis; dibiarkan
    # apa adanya, dan mode yang aktif memulai dengan file barunya sendiri.
    # Perhitungan nama final: get_state_file() / get_log_file() /
    # get_control_file() di bagian bawah file ini.
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
    # dimatikan lewat config). Di mode PAPER fitur ini otomatis DILEWATI
    # karena dust convert memakai endpoint /sapi/* yang BERTANDA TANGAN,
    # sedangkan PAPER dilarang keras mengirim request bertanda tangan apa pun
    # (lihat guard di paper_client.py). Konversi dust bukan bagian dari
    # simulasi eksekusi, jadi ketiadaannya di PAPER bukan bug.
    "USE_DUST_SWEEP": True,
}

# Salinan default tidak pernah ditulis ulang oleh dashboard. Override per mode
# dimuat dari file JSON terpisah sebelum helper mode menghitung path final.
PUMP_DEFAULTS = deepcopy(PUMP_CONFIG)
# BASE_URL adalah alias read-only yang biasanya dihitung saat finalisasi, tetapi
# harus tersedia juga ketika validator memeriksa layer pada import pertama.
PUMP_DEFAULTS["BASE_URL"] = PUMP_DEFAULTS["LIVE_BASE_URL"]
CONFIG_LOAD_ERRORS: list[str] = []


def _load_runtime_layers(explicit_mode: str | None = None) -> None:
    """Bangun ulang PUMP_CONFIG dari default + mode runtime + override.

    Dictionary global dimutasi in-place agar modul yang sudah melakukan
    ``from config import PUMP_CONFIG`` tetap melihat nilai baru setelah
    perpindahan mode dashboard.
    """
    from settings_schema import load_mode_override, load_runtime_mode, validate_candidate

    cfg = deepcopy(PUMP_DEFAULTS)
    # Kredensial dapat berubah dari dashboard saat proses masih hidup.
    cfg["API_KEY"] = os.environ.get("BINANCE_API_KEY", "")
    cfg["API_SECRET"] = os.environ.get("BINANCE_API_SECRET", "")
    errors: list[str] = []
    if explicit_mode is None:
        active_mode, runtime_errors = load_runtime_mode(str(cfg.get("MODE", "PAPER")))
        errors.extend(runtime_errors)
    else:
        active_mode = explicit_mode
    cfg["MODE"] = active_mode

    # Mode invalid tetap dipertahankan supaya require_valid_mode() menghentikan
    # bot. Jangan menormalkannya diam-diam ke LIVE atau PAPER di sini.
    normalized = str(active_mode).strip().upper()
    if normalized in ("PAPER", "LIVE"):
        override, override_errors = load_mode_override(normalized)
        errors.extend(override_errors)
        cfg.update(override)
        cfg["MODE"] = normalized
        cleaned, validation_errors, _ = validate_candidate(cfg, normalized)
        if validation_errors:
            errors.extend(
                f"{key}: {message}" for key, message in validation_errors.items()
            )
        else:
            cfg = cleaned
    else:
        errors.append(
            f"Mode runtime tidak valid: {active_mode!r}. Hanya PAPER atau LIVE yang diizinkan."
        )

    PUMP_CONFIG.clear()
    PUMP_CONFIG.update(cfg)
    CONFIG_LOAD_ERRORS.clear()
    CONFIG_LOAD_ERRORS.extend(errors)


_load_runtime_layers()


# ---------------------------------------------------------------------
# Helper mode PAPER / LIVE
# ---------------------------------------------------------------------
VALID_MODES = ("PAPER", "LIVE")


class InvalidModeError(ValueError):
    """MODE di config tidak dikenal / kosong / typo.

    Dilempar oleh require_valid_mode() supaya bot berhenti dengan pesan jelas,
    BUKAN jatuh diam-diam ke LIVE (yang berisiko uang asli) atau ke mode acak.
    """


def get_mode(config: dict = None) -> str:
    """Kembalikan mode yang dinormalisasi ("PAPER" atau "LIVE").

    Nilai yang tidak dikenal TIDAK pernah diam-diam dianggap LIVE -- selalu
    jatuh ke default aman "PAPER", supaya salah ketik di config tidak berujung
    order memakai uang asli. Untuk MENGHENTIKAN bot pada MODE tak valid (bukan
    diam-diam jatuh ke PAPER), pakai require_valid_mode() di titik start.
    """
    cfg = PUMP_CONFIG if config is None else config
    mode = str(cfg.get("MODE", "PAPER")).strip().upper()
    return mode if mode in VALID_MODES else "PAPER"


def require_valid_mode(config: dict = None) -> str:
    """Validasi MODE secara ketat dan kembalikan nilainya, atau lempar
    InvalidModeError kalau tidak dikenal/kosong.

    Dipanggil di awal start bot & dashboard. Berbeda dari get_mode() yang
    'memaafkan' (default aman ke PAPER untuk pembacaan biasa), fungsi ini
    SENGAJA berhenti keras supaya typo pada MODE tidak lewat begitu saja.
    """
    cfg = PUMP_CONFIG if config is None else config
    raw = cfg.get("MODE", None)
    mode = str(raw).strip().upper() if raw is not None else ""
    if mode not in VALID_MODES:
        raise InvalidModeError(
            f"MODE tidak valid: {raw!r}. Nilai yang diizinkan hanya "
            f"{', '.join(VALID_MODES)}. Perbaiki pump_bot_runtime.json atau "
            "hapus file itu agar default PAPER dipakai. Bot TIDAK akan berjalan "
            "dengan mode yang tidak dikenal demi keamanan."
        )
    return mode


def is_paper(config: dict = None) -> bool:
    return get_mode(config) == "PAPER"


def backtest_enabled(config: dict = None) -> bool:
    """Apakah fitur backtest boleh dipakai pada mode yang sedang aktif.

    Di PAPER selalu boleh. Di LIVE hanya boleh kalau SHOW_BACKTEST_IN_LIVE
    diset True secara eksplisit.

    Nilai config dibaca longgar (menerima True/False, "true"/"false",
    1/0) supaya tidak gampang salah pasang, tetapi apa pun yang tidak
    jelas berarti "ya" akan dianggap False -- sikap default yang aman,
    konsisten dengan get_mode() yang juga tidak pernah diam-diam
    menganggap nilai asing sebagai LIVE.
    """
    if is_paper(config):
        return True
    cfg = PUMP_CONFIG if config is None else config
    raw = cfg.get("SHOW_BACKTEST_IN_LIVE", False)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "1", "yes", "ya", "on")


def get_base_url(config: dict = None) -> str:
    """Base URL REST produksi publik.

    Dipakai KEDUA mode. Data pasar (harga, order book, kline, exchangeInfo)
    selalu berasal dari Binance produksi publik https://api.binance.com, baik
    di PAPER (hanya data, keyless/unsigned) maupun LIVE (data + order signed).
    Sumber: developers.binance.com/docs/binance-spot-api-docs/rest-api
    (dicek 2026-09-24).
    """
    cfg = PUMP_CONFIG if config is None else config
    return cfg.get("LIVE_BASE_URL", "https://api.binance.com")


def use_websocket(config: dict = None) -> bool:
    """Apakah lapisan data pasar memakai WebSocket sebagai sumber primer
    (mode hybrid). False = REST polling penuh (perilaku lama)."""
    cfg = PUMP_CONFIG if config is None else config
    raw = cfg.get("USE_WEBSOCKET", True)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "1", "yes", "ya", "on")


def get_paper_account_file(config: dict = None) -> str:
    """Nama file state akun simulasi PAPER (sudah berakhiran mode)."""
    cfg = PUMP_CONFIG if config is None else config
    return _mode_filename(
        str(cfg.get("PAPER_ACCOUNT_STATE_FILE", "pump_paper_account.json")),
        get_mode(cfg),
    )


# ---------------------------------------------------------------------
# Nama file state/log/kontrol TERPISAH per mode
# ---------------------------------------------------------------------
def _mode_filename(base: str, mode: str) -> str:
    """Sisipkan akhiran mode ('_paper' / '_live') sebelum ekstensi file.

    Contoh:
        pump_bot_state.json + PAPER -> pump_bot_state_paper.json
        pump_bot.log        + LIVE  -> pump_bot_live.log

    Idempoten DAN mengganti akhiran mode lama: kalau nama dasar sudah
    mengandung akhiran mode (mis. 'pump_bot_state_paper.json' lalu mode
    diganti LIVE), akhiran lama dibuang dulu sebelum akhiran baru disisipkan,
    sehingga hasilnya 'pump_bot_state_live.json' -- bukan menumpuk jadi
    '..._paper_live.json'. Ini penting kalau PUMP_CONFIG (yang nama
    file-nya SUDAH final berakhiran mode) disalin lalu MODE-nya diubah,
    misalnya oleh kode pengujian.

    Akhiran '_testnet' yang lama juga ikut dikenali dan dibuang agar file
    yang tercatat dari versi lama tidak menumpuk saat migrasi ke PAPER.
    """
    if not base:
        return base
    tag = mode.lower()  # "paper" atau "live"
    root, ext = os.path.splitext(base)
    for old_tag in ("_paper", "_live", "_testnet"):
        if root.lower().endswith(old_tag):
            root = root[: -len(old_tag)]
            break
    return f"{root}_{tag}{ext}"


def get_state_file(config: dict = None) -> str:
    """Nama file state posisi sesuai mode aktif (pump_bot_state_paper.json
    atau pump_bot_state_live.json)."""
    cfg = PUMP_CONFIG if config is None else config
    return _mode_filename(str(cfg.get("STATE_FILE", "pump_bot_state.json")), get_mode(cfg))


def get_log_file(config: dict = None) -> str:
    """Nama file log sesuai mode aktif (pump_bot_paper.log atau
    pump_bot_live.log)."""
    cfg = PUMP_CONFIG if config is None else config
    return _mode_filename(str(cfg.get("LOG_FILE", "pump_bot.log")), get_mode(cfg))


def get_control_file(config: dict = None) -> str:
    """Nama file kontrol (perintah manual dari dashboard) sesuai mode aktif
    (pump_bot_control_paper.json atau pump_bot_control_live.json)."""
    cfg = PUMP_CONFIG if config is None else config
    return _mode_filename(str(cfg.get("CONTROL_FILE", "pump_bot_control.json")), get_mode(cfg))


_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def _runtime_path(path: str) -> str:
    """Jadikan path runtime absolut terhadap folder repo.

    Path custom absolut dari pengujian tetap dipertahankan. Ini mencegah bot
    langsung menulis state ke folder acak hanya karena dijalankan dari cwd
    yang berbeda.
    """
    return path if os.path.isabs(path) else os.path.join(_PROJECT_ROOT, path)


def _finalize_config_dict(cfg: dict) -> dict:
    cfg["BASE_URL"] = get_base_url(cfg)
    cfg["STATE_FILE"] = _runtime_path(get_state_file(cfg))
    cfg["LOG_FILE"] = _runtime_path(get_log_file(cfg))
    cfg["CONTROL_FILE"] = _runtime_path(get_control_file(cfg))
    cfg["PAPER_ACCOUNT_STATE_FILE"] = _runtime_path(get_paper_account_file(cfg))
    cfg["RATE_LIMIT_STATE_FILE"] = _runtime_path(
        str(cfg.get("RATE_LIMIT_STATE_FILE", "binance_rate_limit_state.json"))
    )
    return cfg


def reload_config(explicit_mode: str | None = None) -> dict:
    """Muat ulang mode dan override, lalu mutasi PUMP_CONFIG in-place."""
    _load_runtime_layers(explicit_mode)
    _finalize_config_dict(PUMP_CONFIG)
    return PUMP_CONFIG


def default_config_for_mode(mode: str) -> dict:
    cfg = deepcopy(PUMP_DEFAULTS)
    cfg["MODE"] = str(mode).strip().upper()
    cfg["API_KEY"] = os.environ.get("BINANCE_API_KEY", "")
    cfg["API_SECRET"] = os.environ.get("BINANCE_API_SECRET", "")
    return _finalize_config_dict(cfg)


def build_config_for_mode(mode: str, *, validate: bool = True) -> tuple[dict, list[str]]:
    """Bangun config suatu mode tanpa mengubah mode dashboard aktif.

    ``validate=False`` hanya dipakai editor agar konfigurasi lama yang invalid
    masih dapat dibuka dan diperbaiki. Start dan checklist selalu memvalidasi.
    """
    from settings_schema import load_mode_override, validate_candidate

    raw = str(mode).strip().upper()
    cfg = default_config_for_mode(raw)
    errors: list[str] = []
    if raw in VALID_MODES:
        override, errors = load_mode_override(raw)
        cfg.update(override)
        cfg["MODE"] = raw
        if validate:
            cleaned, validation_errors, _ = validate_candidate(cfg, raw)
            if validation_errors:
                errors.extend(
                    f"{key}: {message}" for key, message in validation_errors.items()
                )
            else:
                cfg = cleaned
        _finalize_config_dict(cfg)
    return cfg, errors


# Finalisasi import pertama.
_finalize_config_dict(PUMP_CONFIG)
# Alias juga perlu ada pada salinan default agar tes kelengkapan skema dan UI
# dapat menampilkan nilai default semua kunci final.
PUMP_DEFAULTS["BASE_URL"] = PUMP_DEFAULTS["LIVE_BASE_URL"]


# ---------------------------------------------------------------------
# Helper watchlist pemantauan (READ-ONLY)
# ---------------------------------------------------------------------
# Tier watchlist berbasis UPTIME LIKUIDITAS, bukan strategi. "AKTIF" dulu
# bernama "MOMENTUM", dan nama lama itu menyesatkan karena tidak ada
# hubungannya dengan strategi entry. File watchlist atau settings override
# lama yang masih menyimpan "MOMENTUM" otomatis dibaca sebagai "AKTIF"
# lewat migrate_watchlist_tier() di bawah.
VALID_WATCHLIST_TIERS = ("INTI", "AKTIF", "SPEKULATIF")
LEGACY_WATCHLIST_TIER_MAP = {"MOMENTUM": "AKTIF"}


def migrate_watchlist_tier(tier: str) -> str:
    """Ubah nama tier lama menjadi nama baru.

    Dipakai config.get_watchlist() dan settings_schema agar file lama tetap
    terbaca tanpa error validasi dan tanpa kehilangan data.
    """
    raw = str(tier or "").strip().upper()
    return LEGACY_WATCHLIST_TIER_MAP.get(raw, raw)


def watchlist_enabled(config: dict = None) -> bool:
    """Apakah panel watchlist ditampilkan di dashboard.

    Dibaca longgar (True/False, "true"/"1"/"ya") supaya tidak gampang salah
    pasang. Nilai yang tidak jelas berarti "ya" dianggap False, konsisten
    dengan sikap default aman di helper lain pada file ini.
    """
    cfg = PUMP_CONFIG if config is None else config
    raw = cfg.get("WATCHLIST_ENABLED", False)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "1", "yes", "ya", "on")


def get_watchlist(config: dict = None) -> list:
    """Kembalikan watchlist yang sudah dibersihkan dan divalidasi.

    Fungsi ini TIDAK memengaruhi keputusan trading apa pun. Ia hanya
    menyiapkan data untuk ditampilkan dashboard.

    Toleran terhadap isi yang tidak rapi, karena daftar ini memang untuk
    diedit manusia:
      - entry boleh berupa string ("ARBUSDT") atau dict lengkap
      - simbol dinormalisasi jadi huruf besar tanpa spasi
      - entry kosong, duplikat, dan tipe yang salah dibuang diam-diam
      - tier yang tidak dikenal jatuh ke "LAINNYA" (bukan bikin error)
      - score yang tidak bisa dibaca jadi None (bukan bikin error)

    Sikap ini disengaja: satu baris yang salah ketik tidak boleh membuat
    dashboard gagal dimuat, apalagi mengganggu proses bot.
    """
    cfg = PUMP_CONFIG if config is None else config
    raw = cfg.get("WATCHLIST", [])
    if not isinstance(raw, (list, tuple)):
        return []

    out = []
    seen = set()
    for item in raw:
        if isinstance(item, str):
            item = {"symbol": item}
        if not isinstance(item, dict):
            continue

        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)

        tier = migrate_watchlist_tier(item.get("tier", ""))
        if tier not in VALID_WATCHLIST_TIERS:
            tier = "LAINNYA"

        try:
            score = float(item["score"]) if item.get("score") is not None else None
        except (TypeError, ValueError):
            score = None

        out.append({
            "symbol": symbol,
            "tier": tier,
            "score": score,
            "note": str(item.get("note", "")).strip(),
        })
    return out


def watchlist_auto_enabled(config: dict = None) -> bool:
    """Apakah penyegaran daftar otomatis aktif.

    Hanya berlaku kalau panel watchlist sendiri aktif. Dibaca longgar
    dengan sikap default aman, sama seperti helper lain di file ini.
    """
    if not watchlist_enabled(config):
        return False
    cfg = PUMP_CONFIG if config is None else config
    raw = cfg.get("WATCHLIST_AUTO_REFRESH", False)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "1", "yes", "ya", "on")


def get_taker_fee_pct(config: dict = None) -> float:
    """Fee taker efektif dalam persen, sudah memperhitungkan diskon BNB.

    Binance Spot VIP0 = 0,1%; membayar fee dengan BNB memberi diskon 25%
    sehingga menjadi 0,075% (dicek 2026-09-23).
    """
    cfg = PUMP_CONFIG if config is None else config
    fee = float(cfg.get("TAKER_FEE_PCT", 0.1))
    if cfg.get("USE_BNB_FEE_DISCOUNT"):
        fee *= 0.75
    return fee


def get_maker_fee_pct(config: dict = None) -> float:
    """Fee maker efektif dalam persen (order limit yang mengendap), sudah
    memperhitungkan diskon BNB. Spot VIP0 = 0,1%; diskon BNB 25% -> 0,075%
    (dicek 2026-09-24). Bot pump SELALU market (taker), maker dipakai mesin
    simulasi hanya untuk order limit yang terisi sebagai maker."""
    cfg = PUMP_CONFIG if config is None else config
    fee = float(cfg.get("MAKER_FEE_PCT", cfg.get("TAKER_FEE_PCT", 0.1)))
    if cfg.get("USE_BNB_FEE_DISCOUNT"):
        fee *= 0.75
    return fee
