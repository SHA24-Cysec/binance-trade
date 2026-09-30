#!/usr/bin/env python3
"""
Backtest portofolio data-only.

Modul ini mengunduh, menyimpan, dan memvalidasi candle multi-simbol untuk
monitoring. Ia tidak memiliki generator sinyal atau simulasi pembukaan posisi.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from strategy.indicators import Kline
from market import market_scanner as scanner
from backtesting import backtest_storage as storage
from backtesting import backtest_cache as kcache
from backtesting.backtest_storage import KlineStore, SymbolSeries
from backtesting.backtest_cache import KlineCache

logger = logging.getLogger(__name__)

from backtesting.backtest import (
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
    from strategy.indicators import parse_klines

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
    """Kembalikan hasil data-only tanpa pembukaan posisi.

    Store dan parameter API tetap dipertahankan agar dashboard dapat membaca
    cakupan data. Tidak ada simulasi perdagangan yang dijalankan.
    """
    symbols = store.symbols()
    if not symbols:
        raise BacktestError("Tidak ada data candle untuk disimulasikan.")
    allowed = [sym for sym in symbols
               if scanner.is_structurally_allowed_symbol(
                   sym, config, config.get("_historical_tradable_symbols"))]
    if not allowed:
        raise BacktestError("Tidak ada data yang lolos policy semesta bersama.")
    if progress_cb:
        progress_cb(1.0)
    initial_equity = initial_backtest_equity(config)
    return PortfolioResult(
        interval=interval,
        symbols_scanned=len(allowed),
        symbols_with_data=len(allowed),
        bars_total=0,
        start_time=0,
        end_time=0,
        trades=[],
        skipped=[],
        warnings=["Backtest portofolio hanya memuat data dan tidak membuka posisi."],
        initial_equity=initial_equity,
        final_equity=initial_equity,
    )

def summarize_portfolio(result: PortfolioResult) -> dict:
    """Ringkasan portofolio dengan invariant tanpa trade."""
    initial = result.initial_equity or initial_backtest_equity({})
    final = result.final_equity if result.final_equity else initial
    return {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "total_return_pct": 0.0,
        "gross_return_pct": 0.0,
        "fee_drag_pct": 0.0,
        "total_fee_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "avg_win_pct": 0.0,
        "avg_loss_pct": 0.0,
        "profit_factor": 0.0,
        "avg_hold_minutes": 0.0,
        "reason_counts": {},
        "equity_curve": [0.0],
        "initial_equity": initial,
        "final_equity": final,
        "total_pnl_quote": 0.0,
        "unique_symbols": 0,
        "symbols_in_universe": result.symbols_with_data,
        "top_symbols": [],
        "worst_symbols": [],
        "exposure_pct": 0.0,
        "span_days": 0.0,
        "trades_per_day": 0.0,
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
    a = [_mk(i, 100, 101, 99, 100) for i in range(5)]
    b = [_mk(i, 50, 51, 49, 50) for i in range(3, 9)]
    with KlineStore.from_klines({"AUSDT": a, "BUSDT": b}) as store:
        timeline, series = build_timeline(store, "5m")
        check("timeline gabungan unik dan terurut",
              timeline == sorted(set(timeline)) and len(timeline) == 9)
        check("SymbolSeries memetakan waktu", series["BUSDT"].index_at(b[0].open_time) == 0)
        result = run_portfolio_backtest(store, {
            "QUOTE_ASSET": "USDT",
            "EXTRA_EXCLUDE_SYMBOLS": [],
            "BACKTEST_INITIAL_EQUITY_USDT": 10_000.0,
        }, "5m")
        check("portofolio tanpa trade", result.trades == [])
        check("peringatan pembukaan posisi tercatat",
              any("tidak membuka posisi" in w for w in result.warnings))
        summary = summarize_portfolio(result)
        check("ringkasan tanpa trade konsisten",
              summary["total_trades"] == 0 and summary["equity_curve"] == [0.0])

    tickers = [
        {"symbol": "BTCUSDT", "priceChangePercent": "1.0", "quoteVolume": "9e9", "lastPrice": "60000"},
        {"symbol": "USDCUSDT", "priceChangePercent": "0.0", "quoteVolume": "9e9", "lastPrice": "1"},
        {"symbol": "BTCUPUSDT", "priceChangePercent": "5.0", "quoteVolume": "9e9", "lastPrice": "10"},
        {"symbol": "ETHBTC", "priceChangePercent": "1.0", "quoteVolume": "9e9", "lastPrice": "0.05"},
        {"symbol": "SOLUSDT", "priceChangePercent": "-3.0", "quoteVolume": "5e8", "lastPrice": "200"},
    ]
    universe = select_universe(tickers, {
        "QUOTE_ASSET": "USDT", "MIN_QUOTE_VOLUME_USDT_24H": 0,
        "EXTRA_EXCLUDE_SYMBOLS": [],
    })
    check("semesta buang stablecoin, leveraged, dan non-USDT",
          set(universe) == {"BTCUSDT", "SOLUSDT"}, universe)
    print("\nHASIL: " + ("SEMUA LULUS" if ok_all else "ADA YANG GAGAL"))
    return ok_all


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)


# Catatan: penyimpanan SQLite, pengunduhan candle, dan filter semesta tetap
# dipertahankan untuk kompatibilitas dashboard. Jalur simulasi perdagangan
# tidak tersedia di modul ini.
