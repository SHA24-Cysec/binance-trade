#!/usr/bin/env python3
"""
Backtest PORTOFOLIO untuk Pump Scanner Bot.
============================================

Bedanya dengan backtest.py (satu simbol):

    backtest.py       menjawab "KALAU bot kebetulan hanya memantau simbol ini,
                      bagaimana hasil parameter exit saya?"

    modul ini         menjawab "kalau bot memindai SELURUH pasar seperti
                      aslinya, simbol mana yang benar-benar akan dia pilih,
                      dan berapa hasilnya?"

Perbedaan itu penting karena bot asli hanya memegang SATU posisi. Ketika
satu simbol memberi sinyal, bot mungkin sedang sibuk memegang simbol lain,
atau memilih simbol lain yang lebih likuid setelah setupnya lolos.
Backtest satu simbol menghitung SEMUA sinyal sebagai trade, sehingga hasilnya
hampir selalu lebih optimistis daripada yang bisa dicapai bot sungguhan.


CARA KERJA
----------
1. Ambil daftar pair dari ticker 24 jam, saring persis seperti
   market_scanner.filter_and_rank_candidates (buang stablecoin, token
   leveraged, simbol yang dikecualikan, dan yang volumenya di bawah ambang).
2. Unduh candle historis untuk SEMUA simbol yang lolos saringan, lalu tulis
   LANGSUNG ke satu file SQLite temporary per job (backtest_storage.py).
   Candle TIDAK pernah ditumpuk sebagai dict[str, list[Kline]] penuh di RAM:
   hanya satu simbol yang hidup di RAM pada satu waktu saat diunduh, dan
   selama simulasi hanya beberapa simbol aktif (cache LRU) yang dimuat utuh.
3. Bangun "papan kandidat" per bar waktu: untuk setiap titik waktu, saring
   simbol yang volume 24 jam bergulirnya lolos ambang, lalu urutkan dari
   volume terbesar. Urutan volume ini meniru batas anggaran request bot live
   (TOP_N_CANDIDATES_TO_CONFIRM simbol per scan), BUKAN penilaian kualitas.
4. Maju bar demi bar melewati garis waktu gabungan:
     - kalau TIDAK punya posisi: evaluasi top-N kandidat dengan
       scanner.detect_pullback_retest(), kumpulkan yang lolos, lalu pilih
       dengan scanner.setup_quality_key(). Ini meniru find_best_candidate().
     - kalau PUNYA posisi: kelola exit memakai logika yang sama dengan
       backtest satu simbol (SL/TP/BE/Trailing).
5. Hormati cooldown setelah setiap posisi ditutup.

Yang dipakai bersama dengan bot live (BUKAN ditulis ulang):
    market_scanner.detect_pullback_retest() -> aturan entry
    market_scanner.setup_quality_key()      -> urutan kualitas kandidat
    market_scanner.STABLE_BASE_ASSETS  -> saringan pasar
    strategy.resolve_exit_levels()     -> level SL/TP/BE/Trailing
    backtest.compute_rolling_24h_stats -> statistik 24 jam bergulir
    backtest.fetch_full_klines()       -> pengambilan candle
    backtest_storage.KlineStore        -> penyimpanan candle di SQLite temporary
Menyalin logika ini akan menciptakan risiko backtest diam-diam menyimpang
dari bot sungguhan, jenis bug yang baru ketahuan setelah kehilangan uang.


KETERBATASAN YANG TETAP ADA (baca sebelum percaya hasilnya)
-----------------------------------------------------------
1. SURVIVORSHIP BIAS, dan ini tidak bisa diperbaiki. Binance hanya
   menyediakan data historis untuk pair yang MASIH listing hari ini. Koin
   yang sudah didelisting -- sering justru yang kolaps setelah pump -- tidak
   ada dalam data. Hasil backtest portofolio karenanya masih cenderung lebih
   baik daripada kenyataan. Ini batas struktural dari sumber datanya, bukan
   sesuatu yang bisa diakali dengan kode.

2. GRANULARITAS CANDLE. Exit dievaluasi per candle (default 5 menit), bukan
   tiap 15 detik seperti loop bot live. Kalau SL dan TP tersentuh di candle
   yang sama, urutan sebenarnya tidak diketahui. Modul ini memakai prioritas
   konservatif yang sama dengan backtest.py: STOP_LOSS diperiksa paling awal,
   sehingga hasil tidak melebih-lebihkan profit.

3. STATISTIK 24 JAM DIREKONSTRUKSI, BUKAN DIREKAM. Volume dan perubahan 24
   jam dihitung ulang dari candle, bukan diambil dari snapshot ticker/24hr
   historis (Binance tidak menyediakannya). Nilainya sangat dekat tetapi
   tidak identik dengan angka yang dilihat bot pada saat itu, karena
   ticker/24hr adalah jendela bergulir tepat 24 jam sedangkan rekonstruksi
   ini dibulatkan ke batas candle terdekat. Volume itulah yang menentukan
   simbol mana yang masuk top-N kandidat per bar.

4. VOLUME 24 JAM juga direkonstruksi dari penjumlahan quote volume candle.
   Angkanya bisa sedikit berbeda dari field quoteVolume di ticker.

5. LATENSI DAN SLIPPAGE tidak dimodelkan. Entry dianggap terjadi tepat di
   harga penutupan candle sinyal. Fee taker pulang-pergi SUDAH dipotong.

6. Bot live memindai tiap LOOP_INTERVAL_SECONDS (default 15 detik),
   sedangkan simulasi ini memeriksa peluang entry sekali per bar candle.
   Pada praktiknya perbedaan ini kecil karena sinyal entry memang dihitung
   dari candle tertutup, tetapi bot live bisa menangkap peluang beberapa
   menit lebih cepat.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Callable, Optional

from strategy import Kline
import strategy
import market_scanner as scanner
import backtest_storage as storage
import backtest_cache as kcache
from backtest_storage import KlineStore, SymbolSeries
from backtest_cache import KlineCache

logger = logging.getLogger(__name__)

from backtest import (
    BacktestError,
    MS_PER_MIN,
    bars_per_day,
    compute_rolling_24h_stats,
    fetch_full_klines,
    initial_backtest_equity,
)

# Penanda "belum ada di memo". Dipakai sebagai nilai default dict.get() karena
# None adalah nilai memo yang SAH (riwayat harian tidak memenuhi syarat).
_BELUM_DIHITUNG = object()


@dataclass
class PortfolioTrade:
    """Satu trade lengkap hasil simulasi portofolio."""
    symbol: str
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    reason: str
    hold_minutes: float
    pnl_pct: float           # sudah dipotong fee pulang-pergi
    gross_pnl_pct: float
    fee_pct: float
    sl_pct: float
    tp_pct: float
    exit_source: str
    rank_at_entry: int       # posisi simbol di papan kandidat (1 = volume terbesar)
    pct24h_at_entry: float
    candidates_at_entry: int  # berapa simbol lolos saringan pada bar itu
    position_notional: float = 0.0
    equity_before: float = 0.0
    equity_after: float = 0.0
    pnl_quote: float = 0.0


@dataclass
class SkippedSignal:
    """Sinyal yang valid tetapi TIDAK bisa diambil bot.

    Inilah bukti konkret kenapa backtest satu simbol terlalu optimistis:
    setiap baris di sini adalah trade yang akan dihitung oleh backtest satu
    simbol, tetapi tidak akan pernah terjadi di bot sungguhan.
    """
    time: int
    symbol: str
    reason: str       # "SEDANG_PEGANG_POSISI_LAIN" atau "KALAH_KUALITAS_SETUP"
    holding: str      # simbol yang sedang dipegang saat itu


@dataclass
class PortfolioResult:
    interval: str
    symbols_scanned: int
    symbols_with_data: int
    bars_total: int
    start_time: int
    end_time: int
    trades: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    symbols_failed: list = field(default_factory=list)
    initial_equity: float = 0.0
    final_equity: float = 0.0


# ======================================================================
# Tahap 1: pilih semesta simbol
# ======================================================================

def select_universe(tickers: list, config: dict, max_symbols: Optional[int] = None,
                    tradable_symbols: Optional[set] = None) -> list[str]:
    """Tentukan simbol mana yang ikut disimulasikan.

    Memakai filter_and_rank_candidates() milik scanner supaya aturan
    penyaringannya identik dengan bot live (stablecoin dibuang, token
    leveraged dibuang, simbol yang dikecualikan dibuang).

    GERBANG PUMP SENGAJA TIDAK DIPAKAI DI SINI (apply_pump_gate=False).
    Fungsi ini hanya memilih simbol MANA yang datanya diunduh, dan satu-satunya
    ticker yang tersedia saat itu adalah ticker HARI INI. Memakainya untuk
    menyaring periode historis justru menghasilkan bias pemilih (hanya koin
    yang kebetulan pump hari ini yang pernah diuji) sekaligus look-ahead.
    Gerbang pump yang sebenarnya ditegakkan PER BAR di dalam
    run_portfolio_backtest(), memakai kenaikan dan volume pada titik waktu
    yang sedang diuji. Ambang volume minimum juga ditegakkan ulang per-bar
    memakai volume 24 jam bergulir dari candle.
    """
    cfg = dict(config)
    cfg["MIN_QUOTE_VOLUME_USDT_24H"] = float(config.get("MIN_QUOTE_VOLUME_USDT_24H", 0))

    ranked = scanner.filter_and_rank_candidates(tickers, cfg, tradable_symbols,
                                                apply_pump_gate=False)
    # Urutkan berdasarkan likuiditas, bukan kenaikan hari ini. Kalau daftar
    # harus dipotong, yang dipertahankan adalah pair paling likuid, yang juga
    # paling mungkin lolos filter volume bot pada periode mana pun.
    ranked.sort(key=lambda c: c.quote_volume, reverse=True)
    symbols = [c.symbol for c in ranked]
    if max_symbols is not None and max_symbols > 0:
        symbols = symbols[:max_symbols]
    return symbols


# ======================================================================
# Tahap 2: unduh candle semua simbol ke SQLite temporary
# ======================================================================

def new_backtest_store(config: Optional[dict] = None) -> KlineStore:
    """Buat store SQLite temporary untuk SATU job backtest.

    Lokasinya selalu direktori temporary OS lewat ``tempfile.mkdtemp``, jadi
    dua job backtest yang berjalan bersamaan tidak mungkin memakai file yang
    sama. Pemanggil WAJIB menutupnya dengan ``store.cleanup()`` di blok
    ``finally``, termasuk pada jalur error dan pembatalan.
    """
    cfg = config or {}
    cache_size = int(cfg.get("BACKTEST_SYMBOL_CACHE_SIZE",
                             storage.DEFAULT_SYMBOL_CACHE_SIZE)
                     or storage.DEFAULT_SYMBOL_CACHE_SIZE)
    return KlineStore.create_temp(cache_size=max(1, cache_size))


def open_kline_cache(config: Optional[dict] = None) -> Optional[KlineCache]:
    """Buka cache candle lintas job kalau diaktifkan config.

    Kegagalan membuka cache (disk penuh, izin tulis, file rusak) TIDAK boleh
    menggagalkan backtest: fungsi ini mencatat peringatan lalu mengembalikan
    None, dan pemanggil jatuh ke unduh penuh seperti sebelum ada cache.
    Pemanggil wajib menutupnya di blok ``finally`` (tutup saja, jangan
    dihapus -- isinya memang untuk dipakai job berikutnya).
    """
    cfg = config or {}
    if not bool(cfg.get("BACKTEST_CACHE_ENABLED", True)):
        return None
    path = str(cfg.get("BACKTEST_CACHE_FILE") or "").strip()
    if not path:
        return None
    try:
        cache = KlineCache(
            path,
            fresh_hours=float(cfg.get("BACKTEST_CACHE_FRESH_HOURS",
                                      kcache.DEFAULT_FRESH_HOURS) or 0),
            ttl_days=float(cfg.get("BACKTEST_CACHE_TTL_DAYS",
                                   kcache.DEFAULT_TTL_DAYS) or 0),
        )
        cache.prune()
        return cache
    except (sqlite3.Error, OSError, kcache.CacheError) as exc:  # noqa: BLE001
        logger.warning("Cache candle backtest tidak bisa dipakai (%s). Backtest "
                       "tetap berjalan dengan mengunduh penuh.", exc)
        return None


def fetch_universe_klines(
    client,
    symbols: list[str],
    interval: str,
    start_ms: int,
    end_ms: int,
    store: KlineStore,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    sleep_between_symbols: float = 0.0,
    cancel_cb: Optional[Callable[[], bool]] = None,
    cache: Optional[KlineCache] = None,
) -> tuple[list[str], list]:
    """Unduh candle tiap simbol LANGSUNG ke ``store``. Return (berhasil, gagal).

    Beda dengan versi lama yang mengembalikan ``dict[str, list[Kline]]``:
    hasil unduhan satu simbol ditulis ke SQLite lalu referensinya dilepas,
    sehingga hanya SATU simbol yang hidup di RAM pada satu waktu. Yang
    dikembalikan hanya daftar nama simbol yang berhasil (urut sesuai urutan
    unduh) dan daftar kegagalan.

    ``cache`` opsional (backtest_cache.KlineCache): kalau diberikan, hanya
    rentang waktu yang BELUM pernah diunduh yang diminta ke Binance, dan
    jendela segar (bawaan 24 jam terakhir) tetap selalu diunduh ulang.
    Tanpa cache, perilakunya persis seperti sebelumnya yaitu unduh penuh.

    Satu simbol yang gagal TIDAK membatalkan seluruh backtest -- simbol itu
    dicatat di daftar gagal lalu dilewati. Dengan ratusan simbol, memaksa
    semuanya berhasil berarti satu koin bermasalah bisa menggagalkan proses
    yang sudah berjalan sepuluh menit. Kegagalan MENULIS ke SQLite
    diperlakukan sama: dicatat sebagai kegagalan simbol, bukan crash job.
    """
    ok_symbols: list[str] = []
    failed: list = []
    total = max(1, len(symbols))

    for idx, sym in enumerate(symbols):
        if cancel_cb is not None and cancel_cb():
            raise BacktestError("Backtest dibatalkan.")
        try:
            kl = _klines_untuk_simbol(client, sym, interval, start_ms, end_ms,
                                      cache)
            if kl:
                store.write_symbol(sym, kl)
                ok_symbols.append(sym)
            else:
                failed.append({"symbol": sym, "error": "tidak ada data candle pada rentang ini"})
            del kl
        except Exception as exc:  # noqa: BLE001
            failed.append({"symbol": sym, "error": str(exc)[:160]})

        if progress_cb:
            progress_cb((idx + 1) / total, sym)
        if sleep_between_symbols:
            time.sleep(sleep_between_symbols)

    return ok_symbols, failed


def _klines_untuk_simbol(client, symbol: str, interval: str, start_ms: int,
                         end_ms: int,
                         cache: Optional[KlineCache]) -> list[Kline]:
    """Candle satu simbol: dari cache bila ada, sisanya diunduh.

    Tanpa cache, ini sekadar ``fetch_full_klines`` seperti versi sebelumnya.
    Dengan cache, yang diminta ke Binance hanya rentang yang belum tercatat
    di tabel cakupan, ditambah jendela segar yang memang selalu diperbarui.
    """
    if cache is None:
        return fetch_full_klines(client, symbol, interval, start_ms, end_ms,
                                 sleep_between_calls=0.0)

    for awal, akhir in cache.missing_ranges(symbol, interval, start_ms, end_ms):
        bagian = fetch_full_klines(client, symbol, interval, awal, akhir,
                                   sleep_between_calls=0.0)
        # Cakupan dicatat walau hasilnya kosong: rentang yang memang tidak
        # punya candle (koin belum listing) tidak boleh diminta ulang tiap job.
        cache.put(symbol, interval, bagian, awal, akhir)
    return cache.read(symbol, interval, start_ms, end_ms)


def fetch_universe_daily_klines(
    client,
    symbols: list[str],
    start_ms: int,
    end_ms: int,
    store: KlineStore,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> list[str]:
    """Unduh candle 1d tiap simbol ke ``store`` untuk gerbang pump.

    Dipakai supaya rata-rata volume 7 hari di backtest memakai candle harian
    ASLI Binance, sama dengan yang dibaca bot live, bukan hasil penjumlahan
    candle intraday yang hari pertamanya sering tidak lengkap.

    ``start_ms`` sebaiknya sudah dimundurkan minimal 8 hari dari awal periode
    simulasi, supaya bar paling awal pun punya 7 candle harian penuh di
    belakangnya. Simbol yang gagal diunduh tidak menulis baris apa pun;
    ``ensure_daily_series()`` nanti menambalnya dari agregasi candle intraday,
    persis seperti perilaku lama saat ``daily_klines`` tidak memuat simbol itu.

    Candle harian SENGAJA tidak ikut cache lintas job: seluruh jendela 38
    hari muat dalam SATU request per simbol, sedangkan kebijakan kesegaran
    ketat mengharuskan candle harian terakhir selalu diunduh ulang. Jadi
    cache hanya akan menambah kerumitan tanpa mengurangi satu request pun.

    Return daftar simbol yang candle hariannya berhasil disimpan.
    """
    from strategy import parse_klines

    tersimpan: list[str] = []
    total = max(1, len(symbols))
    for idx, sym in enumerate(symbols):
        if cancel_cb is not None and cancel_cb():
            raise BacktestError("Backtest dibatalkan.")
        try:
            raw = client.get_klines(sym, interval="1d", limit=1000,
                                    start_time_ms=start_ms, end_time_ms=end_ms)
            harian = parse_klines(raw)
            if harian:
                store.write_daily(sym, harian)
                tersimpan.append(sym)
        except Exception as exc:  # noqa: BLE001 - satu simbol gagal tidak menghentikan backtest
            logger.warning("Candle harian %s gagal diunduh (%s). Gerbang pump "
                           "akan memakai agregasi candle intraday simbol ini.",
                           sym, str(exc)[:120])
        if progress_cb:
            progress_cb((idx + 1) / total, sym)
    return tersimpan


def ensure_daily_series(store: KlineStore, symbols: list[str]) -> None:
    """Pastikan setiap simbol punya deret harian tersimpan di ``store``.

    Simbol yang candle 1d-nya tidak tersedia ditambal dengan
    ``scanner.aggregate_to_daily()`` dari candle intraday-nya sendiri, sama
    dengan perilaku lama ketika argumen ``daily_klines`` tidak memuat simbol
    tersebut. Agregasi dikerjakan SATU simbol pada satu waktu lalu langsung
    ditulis ke SQLite, jadi tidak ada dict harian penuh yang menumpuk di RAM.
    """
    for sym in symbols:
        if store.daily_count(sym) > 0:
            continue
        harian = scanner.aggregate_to_daily(store.load_klines(sym))
        if harian:
            store.write_daily(sym, harian)


# ======================================================================
# Tahap 3: bangun papan kandidat per bar
# ======================================================================

def build_timeline(store: KlineStore, interval: str,
                   symbols: Optional[list[str]] = None,
                   ) -> tuple[list[int], dict[str, SymbolSeries]]:
    """Siapkan struktur yang bisa ditelusuri per titik waktu, dari SQLite.

    Return:
        timeline  : daftar open_time unik terurut, gabungan semua simbol
        series_of : {symbol: SymbolSeries} berisi array open_time, close_time,
                    dan statistik 24 jam bergulir

    ``SymbolSeries`` menggantikan pasangan ``index_of``/``stats_of`` versi
    lama: pencarian index memakai bisect pada array int64 (bukan dict
    {open_time: index} per simbol), dan statistik disimpan sebagai dua array
    float64 plus penanda kesiapan (bukan satu dict per bar). Statistiknya
    sendiri tetap dihitung oleh ``backtest.compute_rolling_24h_stats``, fungsi
    yang sama dengan backtest satu simbol, supaya angkanya identik.

    Prakomputasi membaca SATU simbol pada satu waktu dari SQLite lalu melepas
    list Kline-nya, jadi puncak RAM tahap ini setara satu simbol, bukan
    seluruh semesta.
    """
    window = bars_per_day(interval)
    daftar = list(symbols) if symbols is not None else store.symbols()
    series_of: dict[str, SymbolSeries] = {}
    all_times: set = set()

    for sym in daftar:
        klines = store.load_klines(sym)
        if not klines:
            continue
        stats = compute_rolling_24h_stats(klines, window)
        series = SymbolSeries.build(sym, klines, stats,
                                    store.daily_close_times(sym))
        series_of[sym] = series
        all_times.update(series.open_times())
        del klines, stats

    timeline = sorted(all_times)
    return timeline, series_of


class _PumpGateAverages:
    """Memo rata-rata volume harian untuk gerbang pump per titik waktu.

    ``scanner.pump_gate_ok_at()`` adalah gabungan dua fungsi bersama:
    ``average_prior_daily_quote_volume()`` lalu ``evaluate_pump_gate()``.
    Bagian pertama hanya berubah hasilnya ketika ada candle harian BARU yang
    tertutup, yaitu sekali per hari per simbol, sedangkan loop utama
    membutuhkannya sekali per bar per simbol (288 kali lebih sering pada
    interval 5 menit). Memo ini menyimpan hasilnya dengan kunci
    (simbol, jumlah candle harian yang sudah tertutup pada waktu acuan),
    sehingga candle harian tidak dibaca ulang dari SQLite tiap bar.

    Keputusan akhirnya tetap diambil ``scanner.evaluate_pump_gate()``, fungsi
    yang sama dengan bot live, jadi tidak ada aturan gerbang yang ditulis
    ulang di sini.
    """

    __slots__ = ("_store", "_memo")

    def __init__(self, store: KlineStore) -> None:
        self._store = store
        self._memo: dict[str, dict[int, Optional[float]]] = {}

    def memo_for(self, symbol: str) -> dict:
        """Kamus memo milik satu simbol (dipakai langsung di hot path)."""
        return self._memo.setdefault(str(symbol), {})

    def compute(self, symbol: str, reference_ms: int, key: int) -> Optional[float]:
        """Hitung dan simpan rata-rata untuk satu kunci memo yang belum ada."""
        rata, _alasan = scanner.average_prior_daily_quote_volume(
            self._store.daily_klines(symbol), reference_ms)
        self.memo_for(symbol)[int(key)] = rata
        return rata

    def average_at(self, symbol: str, series: SymbolSeries,
                   reference_ms: int) -> Optional[float]:
        """Rata-rata volume harian penuh sebelum ``reference_ms`` untuk simbol."""
        key = series.closed_daily_count(reference_ms)
        memo = self.memo_for(symbol)
        if key in memo:
            return memo[key]
        return self.compute(symbol, reference_ms, key)


def _resolve_symbol_cache_size(config: dict, top_n: int) -> int:
    """Berapa simbol yang boleh utuh (list[Kline]) di RAM bersamaan.

    Nilai dasar diambil dari ``BACKTEST_SYMBOL_CACHE_SIZE`` (bawaan 8 simbol,
    lihat backtest_storage.DEFAULT_SYMBOL_CACHE_SIZE) dan boleh dinaikkan
    pengguna yang RAM-nya lega. Lantai minimumnya adalah ``2 x top_n + 4``,
    bukan ``top_n`` saja, karena isi papan kandidat top-N BERGANTI sebagian
    antar bar; cache seukuran satu papan akan saling menendang dan memaksa
    pembacaan ulang satu simbol PENUH dari SQLite ribuan kali, persis pola
    yang harus dihindari di hot path.

    Angka lantai ini bukan tebakan. Pada uji 150 simbol x 30 hari candle 5
    menit (2026-09-26): cache 12 -> 323 kali muat ulang penuh, 25,6 detik;
    cache 24 -> 21 kali muat ulang, 17,5 detik (versi dict lama: 16,7 detik).
    Puncak RSS proses tetap 128 MB melawan 865 MB versi lama.
    """
    diminta = int(config.get("BACKTEST_SYMBOL_CACHE_SIZE",
                             storage.DEFAULT_SYMBOL_CACHE_SIZE)
                  or storage.DEFAULT_SYMBOL_CACHE_SIZE)
    return max(1, diminta, 2 * int(top_n) + 4)


# ======================================================================
# Tahap 4: simulasi portofolio
# ======================================================================

def run_portfolio_backtest(
    store: KlineStore,
    config: dict,
    interval: str,
    warmup_ms: int = 0,
    progress_cb: Optional[Callable[[float], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    max_skipped_records: int = 400,
) -> PortfolioResult:
    """Jalankan simulasi satu-posisi melintasi seluruh semesta simbol.

    ``store`` adalah :class:`backtest_storage.KlineStore`, satu file SQLite
    temporary berisi candle seluruh semesta. Dari sinilah semua candle
    dibaca; tidak ada ``dict[str, list[Kline]]`` penuh yang dipegang fungsi
    ini. Kepemilikan store ada di PEMANGGIL: dia yang membuatnya dan wajib
    memanggil ``store.cleanup()`` di blok ``finally``.

    Candle harian untuk gerbang pump juga diambil dari ``store`` (tabel
    ``daily_klines``). Simbol yang belum punya deret harian ditambal dari
    agregasi candle intraday-nya sendiri lewat ``ensure_daily_series()``,
    jadi gerbang tetap berlaku tanpa request tambahan. Rata-rata 7 hari
    SELALU dihitung dari candle harian yang sudah tertutup pada bar yang
    sedang diuji, jadi tidak ada look-ahead.
    """
    semua_simbol = store.symbols()
    if not semua_simbol:
        raise BacktestError("Tidak ada data candle untuk disimulasikan.")

    # Jalur direct API juga wajib melewati structural policy yang sama dengan
    # scanner, bukan hanya jalur select_universe dashboard.
    tradable_meta = config.get("_historical_tradable_symbols")
    original_count = len(semua_simbol)
    symbols = [sym for sym in semua_simbol
               if scanner.is_structurally_allowed_symbol(sym, config, tradable_meta)]
    if not symbols:
        raise BacktestError("Tidak ada data yang lolos policy semesta bersama.")
    lolos_policy = len(symbols)

    # Deret harian per simbol untuk gerbang pump. Disiapkan SEKALI di depan
    # (satu simbol pada satu waktu, langsung ke SQLite), bukan per bar.
    ensure_daily_series(store, symbols)
    gate_averages = _PumpGateAverages(store)

    timeline, series_of = build_timeline(store, interval, symbols)
    if not timeline:
        raise BacktestError("Garis waktu kosong, tidak ada candle yang bisa diproses.")
    symbols = [sym for sym in symbols if sym in series_of]

    lookback = strategy.confirm_window_bars(config)
    min_vol = float(config["MIN_QUOTE_VOLUME_USDT_24H"])
    top_n = int(config.get("TOP_N_CANDIDATES_TO_CONFIRM", 10))
    # Simbol yang sedang dipegang dan papan kandidat top-N harus muat di
    # cache LRU store, kalau tidak hot path akan membaca ulang satu simbol
    # penuh dari SQLite pada setiap bar.
    store.set_cache_size(_resolve_symbol_cache_size(config, top_n))
    cooldown_ms = int(config["COOLDOWN_MINUTES_AFTER_CLOSE"]) * MS_PER_MIN
    # Jumlah candle minimum untuk satu keputusan entry dihitung oleh fungsi
    # bersama strategy.required_lookback_bars(), bukan angka hard-code seperti
    # sebelumnya. Kalau CONFIRM_LOOKBACK_BARS diset di bawah itu, jendela
    # tetap dinaikkan oleh confirm_window_bars() supaya backtest tidak diam-diam
    # menghasilkan nol trade, tetapi pengguna tetap diberi peringatan.
    _pre_warnings: list = []
    if lolos_policy != original_count:
        _pre_warnings.append("Sebagian data dibuang oleh policy semesta bersama (stablecoin, leveraged token, blacklist, quote, atau status metadata).")
    if tradable_meta is None or config.get("_tradable_status_is_current_snapshot"):
        _pre_warnings.append("Status TRADING historis tidak tersedia dari candle Binance. Policy status hanya dapat diverifikasi dari metadata saat ini bila caller menyediakannya.")
    _butuh = strategy.required_lookback_bars(config)
    if int(config.get("CONFIRM_LOOKBACK_BARS", 0)) < _butuh:
        _pre_warnings.append(
            f"CONFIRM_LOOKBACK_BARS={config.get('CONFIRM_LOOKBACK_BARS')} lebih kecil dari "
            f"{_butuh} candle yang dibutuhkan struktur setup. Simulasi memakai "
            f"{lookback} candle agar deteksi tetap mungkin, tetapi perbaiki config supaya "
            "backtest dan bot live benar-benar memakai angka yang sama."
        )

    try:
        from config import get_taker_fee_pct as _fee_fn
        fee_round_trip = _fee_fn(config) * 2.0
    except ImportError:
        fee_round_trip = float(config.get("TAKER_FEE_PCT", 0.1)) * 2.0

    execution_spread_pct = max(0.0, float(config.get("BACKTEST_ENTRY_SPREAD_PCT", 0.10) or 0.0))
    execution_slippage_pct = max(0.0, float(config.get("BACKTEST_SLIPPAGE_PCT", 0.05) or 0.0))
    entry_delay_bars = max(0, int(config.get("BACKTEST_ENTRY_DELAY_BARS", 0) or 0))

    trades: list = []
    skipped: list = []
    warnings: list = list(_pre_warnings)
    initial_equity = initial_backtest_equity(config)
    equity = initial_equity

    # Status posisi
    holding: Optional[str] = None
    entry_price = 0.0
    entry_time = 0
    position_notional = 0.0
    equity_before_entry = 0.0
    be_active = False
    be_stop = 0.0
    trailing_active = False
    trailing_stop = 0.0
    cur = {}          # level exit dan level setup yang dikunci saat entry
    rank_at_entry = 0
    pct24h_at_entry = 0.0
    cands_at_entry = 0
    next_entry_allowed_at = 0

    first_allowed_time = timeline[0] + warmup_ms
    total_bars = len(timeline)

    # Materialisasi sekali di depan untuk hot path. Papan kandidat dihitung
    # untuk SETIAP simbol pada SETIAP bar, jadi pencarian dict dan pemanggilan
    # method per simbol per bar akan menjadi jutaan operasi tambahan.
    papan_input = []
    for sym in symbols:
        ot, ct, pct_arr, vol_arr, ready_arr, daily_ct = series_of[sym].board_arrays()
        papan_input.append((sym, ot, ct, pct_arr, vol_arr, ready_arr, daily_ct,
                            gate_averages.memo_for(sym), len(ot)))

    for bi, t_now in enumerate(timeline):
        if progress_cb and bi % 50 == 0:
            progress_cb(bi / total_bars)
        if cancel_cb is not None and bi % 200 == 0 and cancel_cb():
            raise BacktestError("Backtest dibatalkan.")

        # --- Papan kandidat pada titik waktu ini --------------------
        # Dihitung sekali per bar, meniru satu snapshot ticker per rotasi
        # pada bot live.
        board = []
        for (sym, ot, ct, pct_arr, vol_arr, ready_arr, daily_ct, memo, n_bar) in papan_input:
            pos = bisect_left(ot, t_now)
            if pos >= n_bar or ot[pos] != t_now:
                continue
            if not ready_arr[pos]:
                continue
            vol24 = vol_arr[pos]
            if vol24 < min_vol:
                continue
            # Gerbang pump, fungsi keputusan yang sama dengan bot live dan
            # backtest satu simbol. Dievaluasi pada TITIK WAKTU bar ini.
            # Rata-rata volume harian di-memo per jumlah candle harian yang
            # sudah tertutup, karena nilainya hanya berubah sekali per hari
            # sementara loop ini berjalan sekali per bar.
            ref_ms = ct[pos]
            kunci_memo = bisect_right(daily_ct, ref_ms)
            rata_harian = memo.get(kunci_memo, _BELUM_DIHITUNG)
            if rata_harian is _BELUM_DIHITUNG:
                rata_harian = gate_averages.compute(sym, ref_ms, kunci_memo)
            pct24 = pct_arr[pos]
            gate_ok, _gate_reason = scanner.evaluate_pump_gate(
                pct24, vol24, rata_harian, config)
            if not gate_ok:
                continue
            board.append((vol24, sym, pos, pct24))
        # Urut dari volume kuotasi 24 jam terbesar, sama seperti urutan
        # pengambilan candle di bot live. Ini BUKAN penilaian kualitas setup.
        board.sort(key=lambda x: -x[0])

        # ============ SUDAH PUNYA POSISI: kelola exit ============
        if holding is not None:
            hi = series_of[holding].index_at(t_now)
            if hi is None:
                # Celah data pada simbol yang sedang dipegang. Posisi
                # dipertahankan; bar ini dilewati untuk simbol tersebut.
                continue

            candle = store.klines(holding)[hi]
            # Sinyal dan entry berada pada bar berbeda saat latency aktif;
            # jangan mengevaluasi exit pada bar yang baru dipakai untuk fill.
            if candle.open_time <= entry_time:
                continue
            pnl_high = (candle.high / entry_price - 1.0) * 100.0
            pnl_low = (candle.low / entry_price - 1.0) * 100.0
            hold_minutes = (candle.close_time - entry_time) / 60000.0

            atr_mode = cur.get("src") == "ATR"
            sl_price = (entry_price - cur["sl"]) if atr_mode else entry_price * (1 - cur["sl"] / 100.0)
            pnl_high_unit = candle.high - entry_price if atr_mode else pnl_high
            if config["USE_BREAKEVEN"] and not be_active and pnl_high_unit >= cur["be_trig"]:
                be_active = True
                be_stop = entry_price + cur["be_lock"] if atr_mode else entry_price * (1 + cur["be_lock"] / 100.0)
            if config["USE_TRAILING"]:
                if not trailing_active and pnl_high_unit >= cur["tr_start"]:
                    trailing_active = True
                    trailing_stop = candle.high - cur["tr_step"] if atr_mode else candle.high * (1 - cur["tr_step"] / 100.0)
                elif trailing_active:
                    cand_stop = candle.high - cur["tr_step"] if atr_mode else candle.high * (1 - cur["tr_step"] / 100.0)
                    if cand_stop > trailing_stop:
                        trailing_stop = cand_stop

            exit_reason = None
            exit_price = None

            # Urutan prioritas SENGAJA konservatif dan identik dengan
            # backtest.py: risiko dianggap terealisasi lebih dulu kalau
            # dalam satu candle harga menyentuh SL maupun TP.
            # Fill gap-aware (perbaikan audit B-06): candle yang DIBUKA sudah
            # menembus level diisi pada harga pembukaan, sama seperti
            # paper_engine mengisi stop pada harga book pasca-gap.
            sl_triggered = (candle.low <= sl_price if atr_mode else pnl_low <= -cur["sl"])
            if config["USE_STOP_LOSS"] and sl_triggered:
                exit_reason = "STOP_LOSS"
                exit_price = min(sl_price, candle.open)
            elif config["USE_TP"] and (candle.high >= entry_price + cur["tp"] if atr_mode else pnl_high >= cur["tp"]):
                exit_reason = "TAKE_PROFIT"
                exit_price = max(entry_price + cur["tp"] if atr_mode else entry_price * (1 + cur["tp"] / 100.0), candle.open)
            elif be_active and candle.low <= be_stop:
                exit_reason = "BREAKEVEN"
                exit_price = min(be_stop, candle.open)
            elif trailing_active and candle.low <= trailing_stop:
                exit_reason = "TRAILING_STOP"
                exit_price = min(trailing_stop, candle.open)

            is_last = (bi == total_bars - 1)
            if exit_reason is None and is_last:
                exit_reason = "END_OF_DATA"
                exit_price = candle.close
                warnings.append(
                    "Posisi terakhir masih terbuka saat data habis (ditutup paksa di harga "
                    "penutupan terakhir demi kelengkapan statistik, bukan exit sungguhan)."
                )

            if exit_reason:
                exit_price = strategy.backtest_sell_execution_price(
                    exit_price, execution_spread_pct, execution_slippage_pct)
                gross = (exit_price / entry_price - 1.0) * 100.0
                pnl_pct = gross - fee_round_trip
                pnl_quote = position_notional * pnl_pct / 100.0
                equity_after = max(0.0, equity + pnl_quote)
                trades.append(PortfolioTrade(
                    symbol=holding,
                    entry_time=entry_time, entry_price=entry_price,
                    exit_time=candle.close_time, exit_price=exit_price,
                    reason=exit_reason, hold_minutes=hold_minutes,
                    pnl_pct=pnl_pct, gross_pnl_pct=gross, fee_pct=fee_round_trip,
                    sl_pct=cur["sl"], tp_pct=cur["tp"],
                    exit_source=cur["src"],
                    rank_at_entry=rank_at_entry, pct24h_at_entry=pct24h_at_entry,
                    candidates_at_entry=cands_at_entry,
                    position_notional=position_notional, equity_before=equity_before_entry,
                    equity_after=equity_after, pnl_quote=pnl_quote,
                ))
                equity = equity_after
                position_notional = 0.0
                holding = None
                be_active = False
                trailing_active = False
                next_entry_allowed_at = candle.close_time + cooldown_ms
            continue

        # ============ TIDAK PUNYA POSISI: cari kandidat ============
        if t_now < first_allowed_time or t_now < next_entry_allowed_at:
            continue

        eligible = board
        if not eligible:
            continue

        # Meniru find_best_candidate(): evaluasi top-N kandidat (urut volume),
        # kumpulkan semua yang setupnya sah, lalu pilih dengan kunci kualitas
        # yang sama dengan scanner.
        lolos = []
        for rank, (vol24, sym, i, pct) in enumerate(eligible[:top_n], start=1):
            kl = store.klines(sym)
            if i + 1 < lookback:
                continue
            window_kl = kl[max(0, i - lookback + 1): i + 1]
            try:
                setup = scanner.detect_pullback_retest(window_kl, config)
            except Exception:  # noqa: BLE001
                continue
            if setup.ok:
                lolos.append((rank, pct, sym, i, vol24, setup))

        if not lolos:
            continue

        lolos.sort(key=lambda row: scanner.setup_quality_key(row[5],
                                                             scanner.Candidate(row[2], "", row[1], row[4],
                                                                               store.klines(row[2])[row[3]].close)))
        rank, pct, sym, i, _vol24, setup_terpilih = lolos[0]
        sizing = strategy.resolve_position_notional(config, equity)
        if sizing["notional"] <= 0 or sizing["notional"] > equity:
            # Live juga tidak boleh membeli nominal fixed yang melebihi saldo.
            # Tidak dipaksa masuk dengan full-equity compounding.
            continue

        # Catat sinyal valid lain pada bar yang sama yang TIDAK terambil
        # karena bot hanya boleh pegang satu posisi. Ini yang membuat
        # backtest satu simbol terlihat lebih bagus dari kenyataan.
        if len(skipped) < max_skipped_records:
            for _r2, _p2, sym2, _i2, _v2, _s2 in lolos:
                if sym2 == sym:
                    continue
                skipped.append(SkippedSignal(
                    time=t_now, symbol=sym2,
                    reason="KALAH_KUALITAS_SETUP", holding=sym,
                ))
                if len(skipped) >= max_skipped_records:
                    break

        kl = store.klines(sym)
        entry_idx = i + entry_delay_bars
        if entry_idx >= len(kl):
            warnings.append(f"Sinyal {sym} terakhir tidak memiliki bar eksekusi setelah latency entry; trade dilewati.")
            continue
        if entry_delay_bars == 0:
            raw_entry_price = kl[i].close
            entry_time_value = kl[i].close_time
        else:
            entry_candle = kl[entry_idx]
            raw_entry_price = entry_candle.open
            entry_time_value = entry_candle.open_time
        holding = sym
        position_notional = sizing["notional"]
        equity_before_entry = equity
        entry_price = strategy.backtest_buy_execution_price(
            raw_entry_price, execution_spread_pct, execution_slippage_pct)
        entry_time = entry_time_value
        be_active = False
        trailing_active = False
        be_stop = 0.0
        trailing_stop = 0.0
        rank_at_entry = rank
        pct24h_at_entry = pct
        cands_at_entry = len(eligible)

        # Level exit dikunci memakai fungsi yang sama dengan bot live.
        level_cfg = dict(config)
        level_cfg["_atr_value"] = strategy.atr(kl[:i + 1], int(config.get("ATR_PERIOD", 14) or 14))
        lv = strategy.resolve_exit_levels(level_cfg)
        cur = {
            "sl": lv["sl_pct"], "tp": lv["tp_pct"],
            "be_trig": lv["be_trigger_pct"], "be_lock": lv["be_lock_pct"],
            "tr_start": lv["trail_start_pct"], "tr_step": lv["trail_step_pct"],
            "src": lv["source"],
        }

    if progress_cb:
        progress_cb(1.0)

    return PortfolioResult(
        interval=interval,
        symbols_scanned=len(symbols),
        symbols_with_data=len(symbols),
        bars_total=total_bars,
        start_time=timeline[0],
        end_time=timeline[-1],
        trades=trades,
        skipped=skipped,
        warnings=list(dict.fromkeys(warnings)),   # buang duplikat, jaga urutan
        initial_equity=initial_equity,
        final_equity=equity,
    )


# ======================================================================
# Ringkasan
# ======================================================================

def summarize_portfolio(result: PortfolioResult) -> dict:
    """Statistik simulasi portofolio dengan equity/notional nyata."""
    trades = result.trades
    total = len(trades)
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]

    initial = result.initial_equity
    equity = initial
    gross_equity = initial
    equity_curve = [0.0]
    for t in trades:
        notional = t.position_notional if t.position_notional > 0 else equity
        if t.equity_after > 0 or t.pnl_quote != 0:
            equity = t.equity_after
        else:
            equity = max(0.0, equity + notional * t.pnl_pct / 100.0)
        gross_equity = max(0.0, gross_equity + notional * t.gross_pnl_pct / 100.0)
        equity_curve.append((equity / initial - 1.0) * 100.0 if initial > 0 else 0.0)

    final_equity = equity if trades else (result.final_equity or initial)
    total_return = ((final_equity / initial) - 1.0) * 100.0 if initial > 0 else 0.0
    gross_return = ((gross_equity / initial) - 1.0) * 100.0 if initial > 0 else 0.0
    peak = initial
    max_dd = 0.0
    for value in [initial] + [initial * (1.0 + v / 100.0) for v in equity_curve[1:]]:
        peak = max(peak, value)
        max_dd = max(max_dd, ((peak - value) / peak * 100.0) if peak > 0 else 0.0)

    gross_win = sum(t.pnl_pct for t in wins)
    gross_loss = abs(sum(t.pnl_pct for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0)

    reason_counts: dict = {}
    for t in trades:
        reason_counts[t.reason] = reason_counts.get(t.reason, 0) + 1

    symbol_counts: dict = {}
    symbol_pnl: dict = {}
    for t in trades:
        symbol_counts[t.symbol] = symbol_counts.get(t.symbol, 0) + 1
        symbol_pnl[t.symbol] = symbol_pnl.get(t.symbol, 0.0) + t.pnl_quote
    top_symbols = sorted(symbol_pnl.items(), key=lambda kv: kv[1], reverse=True)

    span_ms = max(1, result.end_time - result.start_time)
    span_days = span_ms / (24 * 60 * 60 * 1000)
    total_hold = sum(t.hold_minutes for t in trades)
    exposure = (total_hold / (span_days * 24 * 60) * 100.0) if span_days > 0 else 0.0

    return {
        "total_trades": total, "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / total * 100.0) if total else 0.0,
        "total_return_pct": total_return, "gross_return_pct": gross_return,
        "fee_drag_pct": gross_return - total_return,
        "total_fee_pct": sum(t.fee_pct for t in trades),
        "max_drawdown_pct": max_dd,
        "avg_win_pct": (sum(t.pnl_pct for t in wins) / len(wins)) if wins else 0.0,
        "avg_loss_pct": (sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0.0,
        "profit_factor": profit_factor,
        "avg_hold_minutes": (total_hold / total) if total else 0.0,
        "reason_counts": reason_counts, "equity_curve": equity_curve,
        "initial_equity": initial, "final_equity": final_equity,
        "total_pnl_quote": final_equity - initial,
        "unique_symbols": len(symbol_counts), "symbols_in_universe": result.symbols_with_data,
        # pnl_pct di sini adalah kontribusi terhadap modal awal, bukan
        # penjumlahan persentase trade. pnl_quote disertakan untuk pelaporan.
        "top_symbols": [{"symbol": s, "pnl_quote": p,
                         "pnl_pct": (p / initial * 100.0) if initial > 0 else 0.0,
                         "trades": symbol_counts[s]}
                        for s, p in top_symbols[:10]],
        "worst_symbols": [{"symbol": s, "pnl_quote": p,
                            "pnl_pct": (p / initial * 100.0) if initial > 0 else 0.0,
                            "trades": symbol_counts[s]}
                          for s, p in top_symbols[-5:][::-1] if p < 0],
        "skipped_signals": len(result.skipped),
        "avg_rank_at_entry": (sum(t.rank_at_entry for t in trades) / total) if total else 0.0,
        "exposure_pct": exposure, "span_days": span_days,
        "trades_per_day": (total / span_days) if span_days > 0 else 0.0,
    }


# ======================================================================
# Selftest
# ======================================================================

def _mk(t, o, h, l, c, qv=5_000_000.0):
    """Buat candle 5 menit. t dalam indeks bar, bukan milidetik."""
    ms = t * 5 * MS_PER_MIN
    return Kline(open_time=ms, open=o, high=h, low=l, close=c,
                 close_time=ms + 5 * MS_PER_MIN - 1, volume=1000.0, quote_volume=qv)


def selftest() -> bool:
    """Uji mandiri tanpa jaringan. Return True kalau semua lolos."""
    ok_all = True

    def check(name, cond, extra=""):
        nonlocal ok_all
        if not cond:
            ok_all = False
        print(("  LULUS " if cond else "  GAGAL ") + name + (("  -> " + str(extra)) if extra else ""))

    print("Selftest portfolio_backtest")

    # --- build_timeline menggabungkan waktu dari simbol berbeda ---
    a = [_mk(i, 100, 101, 99, 100) for i in range(5)]
    b = [_mk(i, 50, 51, 49, 50) for i in range(3, 9)]
    with KlineStore.from_klines({"AUSDT": a, "BUSDT": b}) as _store_tl:
        tl, seri = build_timeline(_store_tl, "5m")
        check("timeline gabungan unik & terurut", tl == sorted(set(tl)) and len(tl) == 9, len(tl))
        check("SymbolSeries memetakan waktu ke posisi",
              seri["BUSDT"].index_at(b[0].open_time) == 0)
        check("SymbolSeries menolak waktu yang tidak ada",
              seri["BUSDT"].index_at(b[0].open_time - 1) is None)

    # --- satu posisi saja pada satu waktu ---
    cfg = {
        "CONFIRM_LOOKBACK_BARS": 48,
        # Nama kunci yang BENAR (perbaikan audit temuan R-03): sebelumnya
        # tertulis "CONFIRM_MIN_CLOSE_POSITION" -- kunci yang tidak pernah
        # dibaca scanner -- sehingga relaksasi gerbang ini tidak pernah
        # berlaku dan tes diam-diam memakai default 0.35.
        "MIN_CLOSE_POSITION_IN_RANGE": 0.0,
        "MIN_QUOTE_VOLUME_USDT_24H": 0, "TOP_N_CANDIDATES_TO_CONFIRM": 10,
        "COOLDOWN_MINUTES_AFTER_CLOSE": 0,
        "USE_STOP_LOSS": True, "USE_TP": True, "USE_BREAKEVEN": False,
        "USE_TRAILING": False, "SL_PCT": 2.0, "TP_PCT": 3.0,
        "BE_TRIGGER_PCT": 1.0, "BE_LOCK_PCT": 0.1,
        "TRAILING_START_PCT": 1.5, "TRAILING_STEP_PCT": 0.6,
        "TAKER_FEE_PCT": 0.1,
        "QUOTE_ASSET": "USDT",
        # Selftest ini menguji mesin portofolio, bukan gerbang pump. Gerbang
        # tetap berjalan (riwayat harian sintetis tetap wajib disediakan),
        # hanya ambangnya yang dilonggarkan. Gerbang pump diuji sungguhan di
        # tests/test_pump_gate.py.
        "PUMP_MIN_24H_CHANGE_PCT": -1000.0, "PUMP_VOLUME_SURGE_MULT": 0.0,
        # Fokus selftest ini adalah orkestrasi portofolio dan exit. Sinyal
        # rolling volume diuji terpisah pada test sinyal momentum.
        "ROLLING_VOLUME_FILTER_ENABLED": False,
        # Parameter setup dibiarkan default dari config.py lewat PUMP_CONFIG
        # di bawah, kecuali yang sengaja dilonggarkan di atas.
    }
    from config import PUMP_CONFIG as _PC
    for _k in ("SWING_LOOKBACK_BARS", "SWING_PIVOT_WING_BARS",
               "VWAP_MIN_BARS_AFTER_ANCHOR", "MAX_BARS_BREAKOUT_TO_RETEST",
               "MAX_RETEST_TOUCHES"):
        cfg[_k] = _PC[_k]

    # Dua simbol membentuk setup pullback retest bersamaan. Bot hanya boleh
    # memegang satu. 288 bar pertama = jendela 24 jam yang dibutuhkan
    # statistik bergulir, sisanya berisi sepuluh siklus setup.
    from synthetic_data import riwayat_harian, seri_banyak_setup

    def _harian(data_dict: dict) -> dict:
        """Riwayat 1d sintetis untuk tiap simbol, supaya gerbang pump punya
        tujuh candle harian penuh sebelum bar pertama."""
        return {sym: riwayat_harian(kl) for sym, kl in data_dict.items()}

    def _jalankan(data_dict: dict, cfg_uji: dict) -> PortfolioResult:
        """Simulasi dari store SQLite temporary, lalu filenya dihapus.

        Data sintetis selftest memang kecil, jadi boleh masuk lewat
        KlineStore.from_klines(). Jalur produksi menulis per simbol langsung
        dari hasil unduhan, tanpa dict penuh.
        """
        with KlineStore.from_klines(data_dict, _harian(data_dict)) as _st:
            return run_portfolio_backtest(_st, cfg_uji, "5m")

    up_a = seri_banyak_setup(harga=100.0, siklus=10, volume=9_000_000.0)
    up_b = seri_banyak_setup(harga=200.0, siklus=10, volume=5_000_000.0)

    res = _jalankan({"AUSDT": up_a, "BUSDT": up_b}, cfg)
    overlaps = 0
    for i in range(len(res.trades)):
        for j in range(i + 1, len(res.trades)):
            t1, t2 = res.trades[i], res.trades[j]
            if t1.entry_time < t2.exit_time and t2.entry_time < t1.exit_time:
                overlaps += 1
    check("tidak pernah dua posisi bersamaan", overlaps == 0, f"{overlaps} tumpang tindih")
    check("menghasilkan trade", len(res.trades) > 0, len(res.trades))

    # --- fee benar-benar dipotong ---
    if res.trades:
        t = res.trades[0]
        check("fee pulang-pergi dipotong",
              abs((t.gross_pnl_pct - t.pnl_pct) - 0.2) < 1e-9,
              f"selisih {t.gross_pnl_pct - t.pnl_pct:.4f}")

    # --- cooldown dihormati ---
    cfg_cd = dict(cfg)
    cfg_cd["COOLDOWN_MINUTES_AFTER_CLOSE"] = 60
    res_cd = _jalankan({"AUSDT": up_a, "BUSDT": up_b}, cfg_cd)
    viol = 0
    for i in range(1, len(res_cd.trades)):
        gap_min = (res_cd.trades[i].entry_time - res_cd.trades[i - 1].exit_time) / 60000.0
        if gap_min < 59.9:
            viol += 1
    check("cooldown dihormati", viol == 0, f"{viol} pelanggaran")
    check("cooldown mengurangi jumlah trade",
          len(res_cd.trades) <= len(res.trades),
          f"{len(res_cd.trades)} vs {len(res.trades)}")

    # --- prioritas konservatif: SL menang atas TP di candle yang sama ---
    # Dipakai data setup yang sudah terbukti menghasilkan entry, lalu candle
    # TEPAT SESUDAH entry pertama diganti dengan satu candle berayun ekstrem
    # yang menyentuh TP (+3%) DAN SL (-2%) sekaligus. Rentangnya sengaja
    # lebar (-12% s/d +12%) supaya kedua level pasti terlampaui berapa pun
    # harga entry persisnya. Mesin harus memilih yang konservatif, yaitu SL.
    entry_pertama = res.trades[0].entry_time if res.trades else 0
    seq_sl = []
    tandai = False
    for k in up_a:
        if tandai:
            seq_sl.append(Kline(open_time=k.open_time, open=k.open,
                                high=k.open * 1.12, low=k.open * 0.88, close=k.open,
                                close_time=k.close_time, volume=k.volume,
                                quote_volume=k.quote_volume))
            tandai = False
            continue
        seq_sl.append(k)
        if k.close_time == entry_pertama:
            tandai = True

    res_sl = _jalankan({"AUSDT": seq_sl}, cfg)
    check("skenario SL-vs-TP benar-benar menghasilkan trade (tes tidak vakum)",
          len(res_sl.trades) > 0, len(res_sl.trades))
    spanning = [t for t in res_sl.trades if t.entry_time == entry_pertama]
    if spanning:
        check("SL diprioritaskan saat SL & TP kena di satu candle",
              all(t.reason == "STOP_LOSS" for t in spanning),
              [t.reason for t in spanning][:4])
    else:
        check("SL diprioritaskan saat SL & TP kena di satu candle",
              False, "tidak ada trade yang melewati candle ekstrem")

    # --- paritas dengan backtest satu simbol ---
    # Data yang sama, satu simbol saja, harus menghasilkan entry pada bar yang
    # sama di kedua mesin. Kalau berbeda, berarti salah satu mesin memakai
    # jendela atau urutan exit yang tidak sinkron.
    import backtest as _bt
    from synthetic_data import seri_dengan_setup
    cfg_par = dict(cfg)
    par_kl = seri_dengan_setup(harga=100.0, ekor="naik", panjang_ekor=20)
    res_p1 = _bt.run_backtest(par_kl, dict(cfg_par, _symbol="AUSDT"), warmup_bars=0,
                              daily_klines=riwayat_harian(par_kl))
    res_p2 = _jalankan({"AUSDT": par_kl}, cfg_par)
    check("paritas entry satu simbol vs portofolio",
          [t.entry_time for t in res_p1.trades] == [t.entry_time for t in res_p2.trades],
          f"{[t.entry_time for t in res_p1.trades]} vs {[t.entry_time for t in res_p2.trades]}")
    check("paritas alasan exit satu simbol vs portofolio",
          [t.reason for t in res_p1.trades] == [t.reason for t in res_p2.trades],
          f"{[t.reason for t in res_p1.trades]} vs {[t.reason for t in res_p2.trades]}")

    # --- filter volume menyingkirkan simbol ilikuid ---
    cfg_vol = dict(cfg)
    cfg_vol["MIN_QUOTE_VOLUME_USDT_24H"] = 1e15   # tak ada yang lolos
    res_vol = _jalankan({"AUSDT": up_a}, cfg_vol)
    check("filter volume menyaring semua", len(res_vol.trades) == 0, len(res_vol.trades))
    # Kontrol positif: data yang sama tanpa ambang volume mustahil harus
    # tetap menghasilkan trade. Tanpa ini, tes di atas ikut hijau kalau
    # mesinnya rusak dan tidak pernah membuka posisi sama sekali.
    check("kontrol positif: data sama tanpa ambang volume tetap menghasilkan trade",
          len(_jalankan({"AUSDT": up_a}, cfg).trades) > 0)

    # --- ringkasan konsisten ---
    s = summarize_portfolio(res)
    check("total = menang + kalah", s["total_trades"] == s["wins"] + s["losses"])
    check("kurva equity panjangnya benar", len(s["equity_curve"]) == s["total_trades"] + 1)
    check("drawdown tidak negatif", s["max_drawdown_pct"] >= 0, s["max_drawdown_pct"])
    check("return kotor >= bersih",
          s["gross_return_pct"] >= s["total_return_pct"] - 1e-9,
          f'{s["gross_return_pct"]:.3f} vs {s["total_return_pct"]:.3f}')
    check("eksposur masuk akal (0-100%)", 0 <= s["exposure_pct"] <= 100, s["exposure_pct"])

    # --- select_universe menyaring stablecoin & token leveraged ---
    tickers = [
        {"symbol": "BTCUSDT", "priceChangePercent": "1.0", "quoteVolume": "9e9", "lastPrice": "60000"},
        {"symbol": "USDCUSDT", "priceChangePercent": "0.0", "quoteVolume": "9e9", "lastPrice": "1"},
        {"symbol": "BTCUPUSDT", "priceChangePercent": "5.0", "quoteVolume": "9e9", "lastPrice": "10"},
        {"symbol": "ETHBTC", "priceChangePercent": "1.0", "quoteVolume": "9e9", "lastPrice": "0.05"},
        {"symbol": "SOLUSDT", "priceChangePercent": "-3.0", "quoteVolume": "5e8", "lastPrice": "200"},
    ]
    uni = select_universe(tickers, {"QUOTE_ASSET": "USDT",
                                    "MIN_QUOTE_VOLUME_USDT_24H": 0,
                                    "EXTRA_EXCLUDE_SYMBOLS": []})
    check("semesta buang stablecoin/leveraged/non-USDT",
          set(uni) == {"BTCUSDT", "SOLUSDT"}, uni)
    # select_universe hanya memilih simbol yang datanya diunduh, jadi ia
    # SENGAJA tidak memakai gerbang pump berbasis ticker hari ini. Gerbangnya
    # ditegakkan per bar di dalam run_portfolio_backtest().
    check("select_universe tidak memakai ticker hari ini sebagai gerbang pump "
          "(SOL yang turun hari ini tetap ikut diunduh)",
          "SOLUSDT" in uni)

    print("\nHASIL: " + ("SEMUA LULUS" if ok_all else "ADA YANG GAGAL"))
    return ok_all


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)


# ==== RINGKASAN AUDIT (portfolio_backtest.py) =========================
# Perubahan: penyimpanan candle dipindah dari dict[str, list[Kline]] penuh di
#   RAM ke satu file SQLite temporary per job (backtest_storage.KlineStore).
#   fetch_universe_klines(), fetch_universe_daily_klines(), build_timeline(),
#   dan run_portfolio_backtest() DIGANTI isinya, bukan ditambah jalur baru.
#   Tidak ada flag use_sqlite dan tidak ada fungsi _v2; jalur dict lama sudah
#   tidak ada lagi. Satu-satunya pintu masuk dict adalah
#   KlineStore.from_klines() untuk data sintetis selftest/tes.
# Pemanggil (grep seluruh repo, termasuk tests/, 2026-09-26):
#   - dashboard.py::_bt_run_job -> satu-satunya pemanggil produksi, sudah
#     diperbarui (store dibuat + dibersihkan di finally).
#   - selftest() di file ini -> diperbarui memakai KlineStore.from_klines().
#   - tests/test_portfolio_backtest.py -> tes baru untuk migrasi ini.
#   - test_watchlist.py hanya meng-import modulnya untuk cek konsistensi
#     config, tidak memanggil fungsi yang berubah (diperiksa, tidak perlu
#     diubah).
#   Tidak ada modul live (pump_scanner_bot.py, live_client.py, paper_engine.py,
#   market_data.py, market_ws.py, backtest.py) yang menyentuh fungsi ini.
# Sintaks/tipe: type hints lengkap pada seluruh tanda tangan yang berubah;
#   build_timeline sekarang mengembalikan (timeline, dict[str, SymbolSeries]);
#   docstring lama yang menyebut index_of/stats_of dan "dict di RAM" sudah
#   ditulis ulang agar tidak menyesatkan. Tidak ada print()/TODO/kode sisa.
# Keamanan: modul ini tidak merakit SQL sama sekali; seluruh akses lewat
#   KlineStore yang memakai placeholder "?". Nama simbol berasal dari respons
#   Binance publik, tetap diperlakukan sebagai parameter, bukan identifier.
#   Path file dibuat tempfile.mkdtemp() di backtest_storage, bukan hardcode.
# Race condition/thread-safety: _bt_run_job berjalan di thread terpisah,
#   sedangkan progress/cancel dibaca thread HTTP. Store dibuat dan dipakai
#   di thread job yang sama; koneksi sqlite3 dibuka check_same_thread=False
#   dan seluruh aksesnya diserialisasi RLock milik store, jadi cleanup() dari
#   thread lain pun aman. Dua job backtest paralel memakai dua direktori
#   temporary berbeda sehingga tidak ada state bersama. cancel_cb dan
#   progress_cb tetap dipanggil pada titik yang sama persis seperti sebelumnya
#   (per simbol saat unduh, tiap 50 bar untuk progres, tiap 200 bar untuk
#   pembatalan).
# RAM sebelum vs sesudah (diukur 2026-09-26, data sintetis 150 simbol x 30
#   hari candle 5 menit = 1,3 juta candle, puncak RSS proses):
#     versi dict lama : 865 MB  (seluruh Kline + index_of + stats_of hidup
#                                bersamaan; ~1,3 juta objek Kline)
#     versi SQLite    : 129 MB  (6,7x lebih hemat; objek Kline yang hidup
#                                bersamaan maksimal 24 simbol = ~207 ribu,
#                                sisanya array numerik 41 byte per bar)
#   Hasil trade kedua versi identik (hash SHA-256 dari daftar trade+skipped+
#   warnings sama persis: 32843ff6a14a4ec5).
# Performa (mesin dan data yang sama, tanpa jaringan):
#     150 simbol x 30 hari : lama 16,7 detik, baru 16,7 detik.
#      30 simbol x 10 hari : lama 0,49 detik, baru 0,63 detik (selisih 0,14
#       detik ada di prakomputasi yang kini membaca ulang dari SQLite;
#       loop simulasinya sendiri justru lebih cepat, 0,33 vs 0,39 detik,
#       karena gerbang pump dan statistik tidak lagi lewat dict per bar).
#   Angka ini kecil dibanding fase unduh yang untuk 150 simbol memakan menit.
# Pilihan desain hot path: kombinasi (a) prakomputasi statistik per simbol
#   dari pembacaan SQLite yang dialirkan satu simbol pada satu waktu, dan
#   (b) cache LRU array candle untuk simbol aktif (lihat
#   _resolve_symbol_cache_size). Query per baris di dalam loop per-bar
#   SENGAJA dihindari; satu-satunya pembacaan di dalam loop adalah cache-miss
#   simbol yang baru masuk papan kandidat (terukur 21 kali untuk 8.640 bar).
# Kompatibilitas pemanggil: tanda tangan fetch_universe_klines(),
#   fetch_universe_daily_klines(), build_timeline(), dan
#   run_portfolio_backtest() BERUBAH (argumen store, tanpa daily_klines).
#   dashboard.py sudah disesuaikan pada commit yang sama. select_universe()
#   dan summarize_portfolio() tidak berubah sama sekali.
# Cache candle lintas job (tambahan setelah migrasi SQLite): open_kline_cache()
#   membuka backtest_cache.KlineCache bila BACKTEST_CACHE_ENABLED, dan
#   _klines_untuk_simbol() hanya mengunduh rentang yang belum tercatat di
#   cakupan cache. Tanpa cache, jalurnya persis fetch_full_klines() seperti
#   sebelumnya, jadi tidak ada dua implementasi paralel: satu fungsi, satu
#   percabangan tunggal pada argumen cache.
#   Terukur 2026-09-26 (klien palsu, 150 simbol x 30 hari): job pertama 1.350
#   request, job berikutnya 150 request (89 persen lebih sedikit) karena hanya
#   jendela segar 24 jam yang diunduh ulang. Isi candle identik, jadi hasil
#   simulasi tidak berubah (diuji test_hasil_simulasi_sama_dengan_dan_tanpa_cache).
#   Kegagalan cache TIDAK menggagalkan job: open_kline_cache() mengembalikan
#   None sambil logger.warning, lalu backtest kembali ke unduh penuh.
#   Candle harian sengaja TIDAK di-cache; alasannya ditulis di docstring
#   fetch_universe_daily_klines (satu request per simbol, dan kebijakan
#   kesegaran ketat tetap mengharuskan candle harian terakhir diunduh ulang).
# Catatan terbuka: statistik 24 jam tetap dihitung
#   backtest.compute_rolling_24h_stats (fungsi bersama) supaya angkanya tidak
#   bisa menyimpang dari backtest satu simbol, walaupun menghitungnya lewat
#   window function SQL akan sedikit lebih cepat. Konsistensi hasil dinilai
#   lebih mahal daripada selisih ratusan milidetik itu.
# =======================================================================
