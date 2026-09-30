"""
Konfigurasi bot Binance Spot.

Bot hanya mengelola posisi yang sudah ada. Jalur pembukaan posisi baru telah
dihapus, sementara filter monitoring pasar, proteksi risiko, dan logika exit
tetap tersedia.
"""

import os
from copy import deepcopy

from infrastructure.paths import PROJECT_ROOT

try:
    from dotenv import load_dotenv
    # Cari file .env di root repository, apapun dari mana skrip dijalankan.
    _ENV_PATH = os.path.join(str(PROJECT_ROOT), ".env")
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
    "PAPER_INITIAL_BALANCES": {"USDT": 1000.0},
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
    "MIN_QUOTE_VOLUME_USDT_24H": 10000000,  # kandidat walk-forward; dibatasi untuk pair yang lebih likuid
    "MARKET_DATA_INTERVAL": "5m",

    # ------------------------------------------------------------------
    # GERBANG PUMP (saringan semesta, WAJIB, bukan sekadar prioritas urutan)
    # ------------------------------------------------------------------
    # Sebuah simbol hanya boleh masuk semesta kandidat kalau KEDUA syarat di
    # bawah terpenuhi. Pemeriksaan dilakukan sebagai filter monitoring pasar sebelum data dipakai.
    #
    # Kenapa gerbang ini dihidupkan kembali: tanpa syarat kenaikan, semesta
    # kandidat didominasi pair besar yang sedang sideways atau turun. Setup
    # filter monitoring pada koin yang tren harian dan volumenya tidak mendukung
    # lebih sering menghasilkan data pasar yang kurang relevan.
    #
    # Sumber data syarat 1 dan 2 (bagian volume 24 jam berjalan) adalah
    # respons GET /api/v3/ticker/24hr yang SUDAH diambil sekali untuk seluruh
    # pasar tiap siklus scan, jadi keduanya tidak menambah request.
    #
    # Minimal kenaikan harga 24 jam, dalam persen, dari field
    # priceChangePercent. Koin yang turun 24 jam otomatis gugur.
    # OPTIMASI MANUAL: 9.22 -> 6.0. Gerbang kenaikan dilonggarkan agar lebih
    # banyak koin bermomentum masuk semesta kandidat = lebih banyak peluang.
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


    # Exit adaptif untuk volatilitas scalping. False mempertahankan perilaku
    # persen lama agar state dan konfigurasi lama tetap kompatibel.
    "USE_ATR_EXIT": True,
    "ATR_PERIOD": 14,
    # Jarak exit adaptif berdasarkan volatilitas posisi terbuka.
    "ATR_MULT_SL": 12.0,
    "ATR_MULT_TP": 24.0,
    "ATR_MULT_TRAIL": 8.0,
    "ATR_MULT_BE_TRIGGER": 8.0,
    "ATR_MULT_BE_LOCK": 0.8,
    "ATR_MULT_TRAIL_START": 12.0,

    "EXTRA_EXCLUDE_SYMBOLS": [],             # mis. ["SOMEUSDT"] kalau mau blacklist manual


    # ==================================================================
    # PANEL WATCHLIST (READ-ONLY, TIDAK MEMENGARUHI KEPUTUSAN TRADE)
    # ==================================================================
    #
    # Penyederhanaan 2026-09-27: daftar simbol MANUAL (tier INTI/AKTIF/
    # SPEKULATIF + skor/note statis hasil analisis offline) DIHAPUS.
    # Panel kini menyusun daftarnya sendiri secara OTOMATIS dari semesta
    # scanner yang sama dengan yang dipakai bot:
    #
    #   - simbol diambil dari ticker 24 jam Binance,
    #   - disaring dengan is_structurally_allowed_symbol() milik
    #     market_scanner (quote asset benar, bukan stablecoin, bukan
    #     leveraged token, tidak di-blacklist),
    #   - wajib lolos gerbang volume MIN_QUOTE_VOLUME_USDT_24H,
    #   - diurutkan KENAIKAN 24 JAM terbesar (volume sebagai pemecah seri),
    #     dipotong WATCHLIST_TOP_N teratas,
    #   - lalu tiap simbol ditampilkan sebagai data monitoring pasar.
    #
    # Panel tetap MURNI TAMPILAN dan hanya menampilkan data monitoring pasar.
    "WATCHLIST_ENABLED": True,               # False = panel watchlist disembunyikan dari dashboard
    # Berapa pair teratas berdasarkan data pasar yang dipantau panel.
    "WATCHLIST_TOP_N": 15,

    # --- Backtest data dan cache ---
    "BACKTEST_INITIAL_EQUITY_USDT": 10_000.0,
    "BACKTEST_CACHE_ENABLED": True,
    "BACKTEST_CACHE_FILE": "Data/backtest_cache.sqlite3",
    "BACKTEST_CACHE_FRESH_HOURS": 24,
    "BACKTEST_CACHE_TTL_DAYS": 30,

    # --- Exit ---
    "USE_TP": True,
    # NILAI SENGAJA (dikonfirmasi operator, audit 2026-09-27): TP 80% adalah
    # plafon longgar; exit pemenang praktis dikerjakan trailing/BE, bukan TP.
    # Fallback persen ini dipakai bila level ATR tidak tersedia.
    "TP_PCT": 80.0,
    "USE_STOP_LOSS": True,                   # guard lokal, tetap dipakai sebagai fallback
    "USE_NATIVE_OCO": True,                  # LIVE: OCO SELL native, TP limit + SL limit
    "USE_NATIVE_STOP_LOSS": True,            # fallback LIVE bila client OCO tidak tersedia
    "NATIVE_OCO_LIMIT_BUFFER_PCT": 0.10,     # buffer limit dari trigger agar ada peluang fill
    # Stop loss fallback untuk posisi yang sedang dikelola.
    "SL_PCT": 28.8,                            # keluar paksa kalau rugi melewati nilai ini

    "USE_BREAKEVEN": True,
    # NILAI SENGAJA (dikonfirmasi operator, audit 2026-09-27): BE aktif pada
    # +19.2% dan mengunci +3.2%. Hanya dipakai jalur fallback persen.
    "BE_TRIGGER_PCT": 19.2,
    "BE_LOCK_PCT": 3.2,
    "USE_TRAILING": True,
    # NILAI SENGAJA (dikonfirmasi operator, audit 2026-09-27): trailing mulai
    # +28.8% dengan jarak 14.4%. Invariant step <= SL tetap dijaga kode
    # (strategy.resolve_exit_levels). Hanya dipakai jalur fallback persen.
    "TRAILING_START_PCT": 28.8,
    "TRAILING_STEP_PCT": 14.4,

    # --- Biaya trading (dipakai backtest agar hasilnya jujur) ---
    # Binance Spot VIP0 per 2026: 0,1% maker maupun taker; diskon 25% kalau
    # fee dibayar memakai BNB, sehingga jadi 0,075%.
    # (Sumber: halaman fee resmi Binance & beberapa ringkasan independen,
    # dicek 2026-09-23.)
    # Bot SELALU memakai order MARKET, jadi yang relevan adalah TAKER.
    "TAKER_FEE_PCT": 0.1,                    # taker (order MARKET) Spot VIP0 = 0,1%
    "MAKER_FEE_PCT": 0.1,                    # maker (limit yang mengendap) Spot VIP0 = 0,1%
    "USE_BNB_FEE_DISCOUNT": True,           # True = diskon 25% (0,1% -> 0,075%)
    # --- Kontrol risiko ---
    #
    # Proteksi akun menutup posisi terbuka ketika batas kerugian tercapai.
    "USE_EQUITY_STOP": False,                  # matikan (False) utk nonaktifkan DD Stop
    # OPTIMASI MANUAL: 15 -> 12. Jaring DD diperketat agar penurunan dari peak
    # equity berhenti lebih awal = DD lebih stabil (inti permintaan Anda).
    "MAX_DRAWDOWN_PERCENT": 12.0,
    "USE_DAILY_STOP": False,                   # matikan (False) utk nonaktifkan Daily Stop
    "MAX_DAILY_LOSS_PERCENT": 5.0,
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
    ``from config.config import PUMP_CONFIG`` tetap melihat nilai baru setelah
    perpindahan mode dashboard.
    """
    from config.settings_schema import load_mode_override, load_runtime_mode, validate_candidate

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


_PROJECT_ROOT = str(PROJECT_ROOT)


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
    cfg["BACKTEST_CACHE_FILE"] = _runtime_path(
        str(cfg.get("BACKTEST_CACHE_FILE", "Data/backtest_cache.sqlite3"))
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
    from config.settings_schema import load_mode_override, validate_candidate

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
VALID_WATCHLIST_TIERS = ("INTI", "AKTIF", "SPEKULATIF")

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


# CATATAN AUDIT 2026-09-27: get_watchlist() dihapus bersama kunci config
# "WATCHLIST". Panel dashboard kini memilih simbolnya sendiri dari semesta
# scanner di dashboard.build_watchlist(); tidak ada lagi daftar manual.


def watchlist_auto_enabled(config: dict = None) -> bool:
    """Apakah penyegaran daftar otomatis aktif (selalu False karena dihapus)."""
    return False


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


# CATATAN AUDIT 2026-09-27: get_maker_fee_pct() DIHAPUS (dead code, temuan
# audit). Tidak ada satu pun pemanggil di seluruh repo; paper_engine
# menghitung tarif maker sendiri secara eksak via Decimal dari MAKER_FEE_PCT.


# ==== RINGKASAN AUDIT (config.py, bagian cache backtest) ==============
# Lingkup perubahan: HANYA penambahan empat kunci cache backtest
#   (BACKTEST_CACHE_ENABLED, BACKTEST_CACHE_FILE, BACKTEST_CACHE_FRESH_HOURS,
#   BACKTEST_CACHE_TTL_DAYS) plus satu baris _runtime_path() untuk path cache.
#   Tidak ada kunci strategi, risiko, mode, atau kredensial yang disentuh.
# Pemanggil: kunci ini hanya dibaca portfolio_backtest.open_kline_cache().
#   Modul live (pump_scanner_bot.py, paper_engine.py, live_client.py) tidak
#   membacanya sama sekali, jadi perilaku trading tidak berubah.
# Sintaks/tipe: nilai default bertipe bool/str/int, sama dengan tipe yang
#   dideklarasikan di settings_schema.PARAMETER_SCHEMA. Tes
#   tests/test_config_settings.py::test_schema_covers_every_final_config_key
#   memverifikasi tidak ada kunci yang lupa didaftarkan ke skema.
# Keamanan: BACKTEST_CACHE_FILE di-absolutkan _runtime_path() ke folder repo
#   sehingga tidak bisa menulis ke direktori acak hanya karena cwd berbeda,
#   dan pola Data/ serta *.sqlite3 sudah ditambahkan ke .gitignore supaya
#   file cache tidak pernah ter-commit.
# Race condition: tidak ada state proses baru di sini; koordinasi akses file
#   cache ditangani backtest_cache.py (WAL + busy_timeout + lock).
# =======================================================================
