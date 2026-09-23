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
  dipakai bot ini (Enable Spot Trading, karena bot mengirim order sungguhan
  baik di mode TESTNET maupun LIVE). JANGAN
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

    # --- Pemilihan lingkungan: TESTNET atau LIVE ---
    # "TESTNET" = order SUNGGUHAN dikirim, tapi ke Binance Spot Test Network
    #             (https://testnet.binance.vision) memakai dana virtual. Alur
    #             kodenya sama persis dengan LIVE (tidak ada jalur simulasi
    #             terpisah), jadi yang Anda uji benar-benar perilaku bot live.
    # "LIVE"    = order sungguhan ke Binance produksi memakai uang asli.
    #
    # API key testnet BERBEDA dengan key produksi dan dibuat gratis di
    # https://testnet.binance.vision (login pakai akun GitHub). Karena kedua
    # mode membaca variabel .env yang sama (BINANCE_API_KEY/SECRET), isi .env
    # harus diganti sesuai mode yang sedang dipakai.
    "MODE": "TESTNET",                        # "TESTNET" (default, aman) atau "LIVE"

    # Tampilkan fitur Backtest di dashboard saat MODE="LIVE"?
    #
    # False (default) = tab Backtest DISEMBUNYIKAN saat mode LIVE, dan
    #                   endpoint /api/backtest/* menolak permintaan dengan
    #                   HTTP 403. Di mode TESTNET backtest tetap tersedia
    #                   seperti biasa.
    # True            = backtest tetap tersedia di kedua mode.
    #
    # Alasan defaultnya False: backtest menarik data historis dalam jumlah
    # besar dari endpoint publik Binance (paging /api/v3/klines). Saat bot
    # sedang jalan dengan uang asli, beban itu ikut menghabiskan jatah
    # rate-limit IP yang sama dengan yang dipakai bot untuk memindai pasar
    # dan mengirim order. Kalau jatah habis, Binance membalas HTTP 429 dan
    # dapat berlanjut ke blokir IP sementara (HTTP 418), yang berarti bot
    # bisa gagal menutup posisi tepat waktu. Di testnet risikonya tidak ada
    # karena tidak ada uang asli yang dipertaruhkan.
    #
    # Ini murni soal pemisahan alat analisis dari operasional live. Kalau
    # Anda memang perlu backtest sambil live (misalnya dashboard berjalan
    # di mesin terpisah dengan IP berbeda dari bot), ubah saja ke True.
    "SHOW_BACKTEST_IN_LIVE": False,

    "LIVE_BASE_URL": "https://api.binance.com",
    "TESTNET_BASE_URL": "https://testnet.binance.vision",
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
    "VWAP_MAX_EXTENSION_PCT": 3.5,            # tolak kalau harga > batas ini di atas VWAP; harga di BAWAH VWAP juga ditolak

    # --- Model entry (EKSPERIMENTAL: wajib backtest out-of-sample dulu) ---
    #
    # LEGACY_MOMENTUM: perilaku lama, membeli saat momentum pendek naik dan
    #                  harga masih berada di area VWAP yang diizinkan.
    # VWAP_RETEST_RVOL: setelah pump 24 jam lolos, TUNGGU pullback/retest VWAP,
    #                  lalu beli hanya jika candle 5m berikutnya bullish,
    #                  reclaim VWAP, dan quote volume relatifnya menguat.
    #
    # Hasil 730 hari sebelumnya menunjukkan entry momentum langsung tidak
    # punya edge kotor. Karena itu mode retest ini adalah DESAIN HIPOTESIS,
    # bukan set parameter terbukti dan bukan izin untuk live trading.
    "ENTRY_MODEL": "VWAP_RETEST_RVOL",
    "VWAP_RETEST_LOOKBACK_BARS": 3,           # retest harus terjadi dalam 3 candle SEBELUM candle sinyal
    "VWAP_RETEST_TOUCH_TOLERANCE_PCT": 0.20,  # low retest boleh sampai 0,20% di atas VWAP
    "VWAP_RETEST_MAX_BREAKDOWN_PCT": 0.75,    # low retest tidak boleh breakdown >0,75% di bawah VWAP
    "VWAP_RETEST_MIN_RECLAIM_PCT": 0.10,      # close sinyal minimal 0,10% di atas VWAP
    "VWAP_RETEST_SIGNAL_MIN_CLOSE_POSITION": 0.60,  # close sinyal minimal di 60% range candle
    "RVOL_LOOKBACK_BARS": 10,                 # pembanding volume = 10 candle sebelum candle sinyal
    "MIN_RELATIVE_QUOTE_VOLUME": 1.50,        # quote volume sinyal minimal 1,5x rata-rata pembanding
    # Proteksi keras: mode entry baru tidak boleh mengirim order LIVE sebelum
    # lulus validasi out-of-sample yang disepakati. TESTNET dan backtest tetap
    # diizinkan. Jangan ubah ke True hanya karena satu hasil backtest bagus.
    "ALLOW_EXPERIMENTAL_ENTRY_LIVE": True,

    "EXTRA_EXCLUDE_SYMBOLS": [],             # mis. ["SOMEUSDT"] kalau mau blacklist manual

    # --- Ukuran posisi (tanpa martingale -- sekali entry per rotasi) ---
    "USE_RISK_PERCENT": True,               # True = ukuran posisi % dari saldo USDT free
    "RISK_PERCENT": 95.0,                     # dipakai jika USE_RISK_PERCENT = True
    "POSITION_SIZE_USDT": 5.0,              # dipakai jika USE_RISK_PERCENT = False

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
    "MAX_POSITION_USDT": 0,                  # 0 = tanpa plafon (ikuti RISK_PERCENT sepenuhnya)

    # Bantalan saldo (persen) yang TIDAK ikut dibelanjakan, dipotong dari
    # saldo USDT free sebelum RISK_PERCENT dihitung. Gunanya teknis, bukan
    # filosofi risiko: order MARKET BUY diisi pada harga yang bergerak, dan
    # fee taker 0,1% dipotong dari saldo yang sama. Kalau bot mencoba
    # membelanjakan 100% saldo persis, order sering ditolak bursa dengan
    # error -2010 "Account has insufficient balance".
    # Dengan RISK_PERCENT 95 bantalan ini praktis tidak terasa; ia baru
    # penting kalau Anda menaikkan RISK_PERCENT mendekati 100.
    "BALANCE_BUFFER_PCT": 0.5,

    # --- Exit ---
    "USE_TP": True,
    "TP_PCT": 4.0,
    "USE_STOP_LOSS": True,                   # kerugian maksimum per-trade dari harga entry, exit paksa di harga pasar
    "SL_PCT": 1.8,                            # contoh: 3.0 = keluar kalau rugi >= 3% dari entry (SEBELUM Breakeven/Trailing aktif)

    # --- Stop Loss & Take Profit adaptif berbasis ATR (opsional) ---
    #
    # Kalau USE_ATR_EXITS = False (default), bot memakai SL_PCT/TP_PCT tetap
    # persis seperti sebelumnya -- tidak ada perubahan perilaku sama sekali.
    #
    # Kalau True, jarak SL dihitung dari volatilitas koin yang sedang dipegang:
    #     SL% = batasi(ATR_MULTIPLIER_SL x ATR%, antara ATR_SL_MIN_PCT dan ATR_SL_MAX_PCT)
    #     TP% = SL% x ATR_TP_RR_RATIO
    #
    # ALASAN pakai bentuk HIBRIDA (ATR dengan batas bawah & atas), bukan ATR
    # murni: pengujian lintas banyak strategi/pasar oleh Kevin Davey
    # (kjtradingsystems.com) menemukan ATR menang hanya ~66% kasus, dan ATR
    # murni bisa menghasilkan jarak stop ekstrem saat volatilitas meledak
    # (contohnya 3x ATR di Crude Oil berkisar dari $240 sampai $15.000+).
    # Batas min/max menahan itu tanpa membuang sifat adaptif ATR.
    #
    # ALASAN fitur ini relevan untuk bot pump scanner: bot ini memperdagangkan
    # BANYAK koin berbeda (semua pair USDT), dan filter MIN_PUMP_PCT_24H
    # memastikan koin yang dipilih SEDANG dalam volatilitas tinggi. SL tetap
    # 1.8% bisa berarti 3x ATR di satu koin tapi hanya 0.8x ATR di koin lain.
    # Data yang dikutip Volatility Box (595+ simbol, 2018-2025) menyebut stop
    # di bawah 1.0x ATR terpicu noise >65% dalam 3 bar pertama, sedangkan di
    # 1.5x ATR turun ke 38%.
    #
    # PERINGATAN JUJUR: angka-angka di atas berasal dari publikasi pihak
    # ketiga (sebagian milik vendor), BUKAN dari data trading Anda sendiri.
    # Bukti akademik yang lebih kuat (Barroso & Santa-Clara 2015) mendukung
    # penyesuaian terhadap volatilitas, tapi studi momentum crypto di
    # Financial Markets and Portfolio Management (2025) menegaskan volatility
    # management TIDAK menghilangkan tail risk. Jadi JANGAN aktifkan ini
    # begitu saja -- bandingkan dulu lewat backtest.py pada koin yang
    # benar-benar lolos filter Anda:
    #     python backtest.py --compare-atr --symbol <KOIN>USDT --days 30
    "USE_ATR_EXITS": True,                  # default False = perilaku lama (SL/TP tetap) tidak berubah
    "ATR_PERIOD": 14,                        # standar Wilder; dihitung pada CONFIRM_INTERVAL (default 5m)
    "ATR_MULTIPLIER_SL": 2.0,                # 2.0x = nilai yang paling sering optimal di literatur
    "ATR_SL_MIN_PCT": 1.2,                   # lantai: jangan pernah pasang stop lebih sempit dari ini
    "ATR_SL_MAX_PCT": 4.0,                   # plafon: lindungi dari ATR yang meledak
    "ATR_TP_RR_RATIO": 2.0,                  # TP = SL x rasio ini (2:1, sesuai backtest yang dikutip di atas)

    # Breakeven & Trailing juga ikut skala ATR saat USE_ATR_EXITS = True.
    #
    # KENAPA INI WAJIB, bukan sekadar pelengkap: kalau hanya SL/TP yang ikut
    # ATR sementara BE/Trailing tetap memakai angka tetap, hasilnya PINCANG.
    # Contoh nyata pada koin dengan ATR 3%:
    #     SL  -> 4.0%  (lebar, ikut ATR)
    #     TP  -> 8.0%  (lebar, ikut ATR)
    #     Trailing step -> 0.6% tetap = hanya 0,20x ATR
    # Pullback normal yang masih jauh di dalam 1x ATR langsung menyentuh
    # trailing, jadi posisi tertutup di sekitar +0,9% padahal TP 8% belum
    # tersentuh. Risk:reward jadi TERBALIK: risiko 4%, imbalan 0,9%.
    # Pada backtest data sintetis, kondisi pincang ini membuat TAKE_PROFIT
    # hanya tercapai 2,9% dari trade, sementara 91,2% ditutup BE/Trailing.
    #
    # Angka pengali di bawah memakai ATR sebagai satuan, bukan persen tetap:
    #   BE_TRIGGER  = 0.5x ATR -> amankan modal setelah gerakan setengah ATR
    #   BE_LOCK     = 0.1x ATR -> kunci profit tipis di atas entry
    #   TRAIL_START = 1.0x ATR -> mulai trailing setelah gerakan satu ATR penuh
    #   TRAIL_STEP  = 1.5x ATR -> jarak trailing di ATAS 1x ATR, supaya tidak
    #                             terpicu noise biasa (data yang dikutip di
    #                             atas: stop < 1.0x ATR terpicu noise >65%
    #                             dalam 3 bar pertama; di 1.5x turun ke 38%)
    "ATR_BE_TRIGGER_MULT": 0.5,
    "ATR_BE_LOCK_MULT": 0.1,
    "ATR_TRAILING_START_MULT": 1.0,
    "ATR_TRAILING_STEP_MULT": 1.5,
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
    "TAKER_FEE_PCT": 0.1,                    # ubah ke 0.075 kalau Anda membayar fee dengan BNB
    "USE_BNB_FEE_DISCOUNT": True,           # True = otomatis pakai 0,075% (diskon 25%)
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
    # dimatikan lewat config). Di mode TESTNET fitur ini otomatis DILEWATI
    # karena Binance Spot Test Network tidak menyediakan endpoint /sapi/*
    # sama sekali (sumber: developers.binance.com/docs/binance-spot-api-docs/
    # testnet/general-info, dicek 2026-09-23) -- jadi tidak ada gunanya
    # dipanggil di sana dan kegagalannya bukan bug.
    "USE_DUST_SWEEP": True,
}


# ---------------------------------------------------------------------
# Helper mode TESTNET / LIVE
# ---------------------------------------------------------------------
VALID_MODES = ("TESTNET", "LIVE")


def get_mode(config: dict = None) -> str:
    """Kembalikan mode yang dinormalisasi ("TESTNET" atau "LIVE").

    Nilai yang tidak dikenal TIDAK pernah diam-diam dianggap LIVE -- selalu
    jatuh ke TESTNET, supaya salah ketik di config tidak berujung order
    memakai uang asli.
    """
    cfg = PUMP_CONFIG if config is None else config
    mode = str(cfg.get("MODE", "TESTNET")).strip().upper()
    return mode if mode in VALID_MODES else "TESTNET"


def is_testnet(config: dict = None) -> bool:
    return get_mode(config) == "TESTNET"


def backtest_enabled(config: dict = None) -> bool:
    """Apakah fitur backtest boleh dipakai pada mode yang sedang aktif.

    Di TESTNET selalu boleh. Di LIVE hanya boleh kalau
    SHOW_BACKTEST_IN_LIVE diset True secara eksplisit.

    Nilai config dibaca longgar (menerima True/False, "true"/"false",
    1/0) supaya tidak gampang salah pasang, tetapi apa pun yang tidak
    jelas berarti "ya" akan dianggap False -- sikap default yang aman,
    konsisten dengan get_mode() yang juga tidak pernah diam-diam
    menganggap nilai asing sebagai LIVE.
    """
    if is_testnet(config):
        return True
    cfg = PUMP_CONFIG if config is None else config
    raw = cfg.get("SHOW_BACKTEST_IN_LIVE", False)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "1", "yes", "ya", "on")


def get_base_url(config: dict = None) -> str:
    """Base URL REST sesuai mode.

    Testnet: https://testnet.binance.vision (hanya endpoint /api/* tersedia).
    Live   : https://api.binance.com
    Sumber: developers.binance.com/docs/binance-spot-api-docs/testnet/general-info
    (dicek 2026-09-23).
    """
    cfg = PUMP_CONFIG if config is None else config
    if is_testnet(cfg):
        return cfg.get("TESTNET_BASE_URL", "https://testnet.binance.vision")
    return cfg.get("LIVE_BASE_URL", "https://api.binance.com")


# Disediakan supaya kode lama yang membaca PUMP_CONFIG["BASE_URL"] tetap jalan.
PUMP_CONFIG["BASE_URL"] = get_base_url(PUMP_CONFIG)


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
