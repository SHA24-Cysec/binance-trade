"""Seleksi kandidat dan deteksi setup PULLBACK dan RETEST untuk bot Spot.

Modul ini tidak memprediksi arah harga. Ia mencari STRUKTUR harga yang sudah
terjadi pada candle yang SUDAH tertutup: sebuah breakout di atas swing high,
lalu pullback kembali ke area level itu, lalu candle yang menutup kembali di
atas level. Long only, satu posisi per rotasi.

Modul ini BUKAN lagi pump scanner. Gerbang "naik sekian persen dalam 24 jam"
sudah dihapus, begitu juga pengurutan kandidat berdasarkan kenaikan 24 jam.
Yang tersisa dari seleksi pasar hanyalah saringan struktural (status
perdagangan, stablecoin, leveraged token, blacklist, volume kuotasi minimum),
dan pengurutan akhir memakai kualitas setup, bukan besarnya kenaikan.

Semua fungsi deteksi bersifat MURNI dan TANPA STATE: input daftar candle
tertutup urut kronologis plus config, output keputusan dan alasan. Tidak ada
status setup yang disimpan antar scan, jadi jalur live, backtest.py,
portfolio_backtest.py, dan watchlist_auto.py menghasilkan keputusan yang sama
untuk jendela candle yang sama.

Caller live bertanggung jawab membuang candle yang masih berjalan sebelum
memanggil fungsi di sini. Endpoint klines Binance hampir selalu menyertakan
candle berjalan, dan memakainya berarti keputusan live tidak lagi sebanding
dengan backtest.

Referensi endpoint yang relevan (dicek 2026-09-25):
  klines      https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market#klines
              bobot IP 2, limit maksimum 1000 candle per panggilan.
  ticker 24h  https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market#ticker24hr
              bobot IP 80 bila diambil tanpa parameter symbol (seluruh pasar).
  batas rate  REQUEST_WEIGHT 6000 per menit per IP, dibaca langsung dari
              /api/v3/exchangeInfo (rateLimits) pada 2026-09-25.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import strategy
from strategy import Kline

# Stablecoin/aset yang bukan target trading struktural (kalau jadi base asset,
# pair-nya seperti USDCUSDT yang praktis tidak bergerak).
STABLE_BASE_ASSETS = {
    "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "EUR", "GBP", "TRY",
    "BRL", "AEUR", "USTC", "USDD", "PYUSD", "USDE",
    # Ditambahkan 2026-09-24 setelah pemeriksaan ticker 24 jam Binance Spot:
    # USD1USDT dan RLUSDUSDT aktif diperdagangkan dengan volume besar
    # (219 juta dan 88 juta USDT) namun perubahan 24 jamnya praktis 0,00%,
    # karena keduanya stablecoin yang dipatok ke USD. Breakout struktural
    # pada pair seperti ini hampir selalu noise tick size, bukan pergerakan
    # yang bisa diperdagangkan.
    "USD1", "RLUSD",
}

# CATATAN AUDIT 2026-09-24: Binance telah menghapus SEMUA leveraged token
# (BTCUP, ETHDOWN, dsb) per 3 April 2024, jadi daftar ini kini murni pagar
# pengaman. Kalau suatu hari Binance menghidupkan produk serupa, suffix ini
# kembali relevan.
LEVERAGED_TOKEN_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


@dataclass
class Candidate:
    symbol: str
    base_asset: str
    price_change_pct: float
    quote_volume: float
    last_price: float
    confirmed: bool = False
    confirm_reason: str = ""
    # Diisi setelah konfirmasi setup. Dipakai untuk mengurutkan kandidat dan
    # untuk mengunci level invalidasi di state posisi saat entry.
    setup: "Optional[SetupResult]" = None


@dataclass
class SetupResult:
    """Hasil evaluasi satu jendela candle terhadap aturan pullback retest.

    ``ok`` True hanya kalau candle TERAKHIR yang tertutup adalah candle
    konfirmasi retest. Field lain tetap diisi sebisanya walaupun ok False,
    supaya log bisa menjelaskan setup sampai mana prosesnya.

    ``breakout_level``, ``zone_low``, ``zone_high``, dan ``atr_abs`` disimpan
    supaya pemanggil dapat mengunci level invalidasi di state posisi saat
    entry, tanpa perlu menghitung ulang dari data yang lebih baru.
    """
    ok: bool
    reason: str
    breakout_level: Optional[float] = None
    zone_low: Optional[float] = None
    zone_high: Optional[float] = None
    anchor_index: Optional[int] = None
    anchored_vwap: Optional[float] = None
    atr_pct: Optional[float] = None
    atr_abs: Optional[float] = None
    invalidation_price: Optional[float] = None
    extension_atr: Optional[float] = None
    retest_touches: int = 0


def _looks_leveraged(base_asset: str) -> bool:
    """Deteksi leveraged token Binance lama (BTCUP, ETHDOWN, dsb).

    Binance sudah menghapus SEMUA leveraged token per 3 April 2024, jadi
    fungsi ini kini murni pagar pengaman. Syarat awalan minimal 2 huruf
    mencegah koin SAH seperti JUP (awalan 'J' hanya 1 huruf) ikut tertolak,
    sebelum perbaikan audit 2026-09-24 JUPUSDT salah ditolak oleh heuristik ini.
    """
    for sfx in LEVERAGED_TOKEN_SUFFIXES:
        if base_asset.endswith(sfx) and len(base_asset) - len(sfx) >= 2:
            return True
    return False


def filter_and_rank_candidates(tickers: list, config: dict,
                               tradable_symbols: "set | None" = None) -> list[Candidate]:
    """Saring semesta pair lalu urutkan dari volume kuotasi 24 jam tertinggi.

    KEPUTUSAN DESAIN (bisa diubah, lihat README bagian "Pemilihan kandidat"):
    gerbang kenaikan 24 jam sudah DIHAPUS, dan urutannya bukan lagi dari
    kenaikan tertinggi. Strategi pullback retest tidak membutuhkan koin yang
    sudah naik banyak hari itu, yang dibutuhkan adalah struktur breakout dan
    retest pada candle konfirmasi. Urutan volume di sini hanya menentukan
    simbol mana yang candle-nya diunduh lebih dulu saat anggaran request
    terbatas, BUKAN kandidat mana yang lebih layak dibeli. Pengurutan
    berdasarkan kualitas setup dilakukan di find_best_candidate().

    ``tickers`` adalah hasil mentah GET /api/v3/ticker/24hr untuk seluruh pair
    (bobot IP 80, dicek 2026-09-25).

    ``tradable_symbols`` opsional: himpunan simbol yang statusnya TRADING
    menurut exchangeInfo. Kalau diberikan, simbol di luar himpunan itu dibuang.
    Kalau None, saringan status dilewati (dipakai jalur backtest yang bekerja
    dari data historis dan tidak punya snapshot exchangeInfo saat itu).
    """
    quote_asset = config["QUOTE_ASSET"]
    exclude_symbols = set(config.get("EXTRA_EXCLUDE_SYMBOLS", []))
    min_vol = float(config.get("MIN_QUOTE_VOLUME_USDT_24H", 0) or 0)

    out = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith(quote_asset):
            continue
        if symbol in exclude_symbols:
            continue
        if tradable_symbols is not None and symbol not in tradable_symbols:
            continue

        base_asset = symbol[: -len(quote_asset)]
        if not base_asset or base_asset in STABLE_BASE_ASSETS:
            continue
        if _looks_leveraged(base_asset):
            continue

        try:
            price_change_pct = float(t["priceChangePercent"])
            quote_volume = float(t["quoteVolume"])
            last_price = float(t["lastPrice"])
        except (KeyError, ValueError, TypeError):
            continue

        if last_price <= 0:
            continue
        if quote_volume < min_vol:
            continue

        out.append(Candidate(
            symbol=symbol, base_asset=base_asset, price_change_pct=price_change_pct,
            quote_volume=quote_volume, last_price=last_price,
        ))

    out.sort(key=lambda c: c.quote_volume, reverse=True)
    return out


# ======================================================================
# Deteksi setup: breakout, anchored VWAP, pullback, retest
# ======================================================================

def _pivot_high_indexes(klines: list[Kline], wing: int) -> list[int]:
    """Indeks candle yang menjadi pivot high dengan ``wing`` candle di kiri dan kanan.

    Sebuah pivot high di indeks p berarti high[p] lebih tinggi daripada high
    semua candle pada p-wing..p-1 dan p+1..p+wing. Perbandingan sengaja KETAT
    (>) di kedua sisi supaya deretan high yang sama persis (data datar, koin
    dengan tick size besar) tidak menghasilkan banyak pivot palsu.

    Semua candle sayap kanan harus sudah ada di dalam jendela, jadi pivot
    tidak pernah dihitung dari candle yang belum tertutup.
    """
    out: list[int] = []
    n = len(klines)
    if wing < 1 or n < 2 * wing + 1:
        return out
    for p in range(wing, n - wing):
        h = klines[p].high
        kiri = all(h > klines[j].high for j in range(p - wing, p))
        kanan = all(h > klines[j].high for j in range(p + 1, p + wing + 1))
        if kiri and kanan:
            out.append(p)
    return out


def _breakout_level_for(klines: list[Kline], pivots: list[int], b: int,
                        swing_lookback: int, wing: int) -> Optional[float]:
    """Swing high valid yang menjadi level breakout untuk candle indeks ``b``.

    Pivot yang boleh dipakai hanya yang SELURUH sayap kanannya sudah tertutup
    sebelum candle b (p + wing <= b - 1), dan yang berada dalam
    ``swing_lookback`` candle sebelum b. Syarat sayap kanan itu yang mencegah
    look-ahead: level tidak pernah memakai informasi dari candle b atau
    sesudahnya.
    """
    batas_bawah = max(0, b - swing_lookback)
    kandidat = [klines[p].high for p in pivots if batas_bawah <= p <= b - 1 - wing]
    if not kandidat:
        return None
    return max(kandidat)


def detect_pullback_retest(klines: list[Kline], config: dict) -> SetupResult:
    """Deteksi setup pullback dan retest pada jendela candle TERTUTUP.

    Urutan kejadian yang dicari, semuanya di dalam satu jendela dan tanpa
    menyimpan state antar panggilan:

      1. BREAKOUT. Candle yang close di atas swing high terakhir yang valid
         ditambah BREAKOUT_BUFFER_ATR_MULT x ATR. Sumbu yang menembus tanpa
         close di atas level TIDAK dihitung sebagai breakout.
      2. ANCHOR. Candle breakout itu sendiri menjadi jangkar anchored VWAP
         (candle anchor ikut dihitung).
      3. PULLBACK. Harga turun kembali ke zona di sekitar level, dan anchored
         VWAP berada dalam jarak RETEST_VWAP_CONFLUENCE_ATR_MULT x ATR dari
         level (konfluensi level dan VWAP).
      4. RETEST TERKONFIRMASI. Candle TERAKHIR yang tertutup menyentuh zona,
         close kembali di atas level, close berada di bagian atas range candle
         (MIN_CLOSE_POSITION_IN_RANGE), dan close di atas anchored VWAP.
      5. INVALIDASI. Setup gugur bila ada candle tertutup yang close di bawah
         level dikurangi INVALIDATION_ATR_MULT x ATR sebelum retest, atau bila
         jarak breakout ke candle terakhir melewati MAX_BARS_BREAKOUT_TO_RETEST,
         atau bila sentuhan zona sudah melebihi MAX_RETEST_TOUCHES.
      6. ANTI-KEJAR. Entry ditolak bila close terakhir sudah lebih dari
         MAX_EXTENSION_ATR_MULT x ATR di atas level.

    Penentuan anchor DETERMINISTIK: jendela ditelusuri dari candle tertua ke
    terbaru. Saat ada breakout dan belum ada setup aktif, anchor di-set. Saat
    terjadi invalidasi, setup dihapus lalu pencarian breakout berikutnya
    dilanjutkan. Yang dievaluasi di akhir adalah setup aktif terakhir yang
    bertahan sampai candle terakhir.

    SEMUA nilai default parameter adalah titik awal yang masih harus
    divalidasi lewat backtest repo ini.
    """
    swing_lookback = int(config.get("SWING_LOOKBACK_BARS", 12) or 12)
    wing = int(config.get("SWING_PIVOT_WING_BARS", 2) or 2)
    buffer_mult = float(config.get("BREAKOUT_BUFFER_ATR_MULT", 0.10) or 0.0)
    zone_mult = float(config.get("RETEST_ZONE_ATR_MULT", 0.5) or 0.0)
    konfluensi_mult = float(config.get("RETEST_VWAP_CONFLUENCE_ATR_MULT", 1.0) or 0.0)
    vwap_min_bars = int(config.get("VWAP_MIN_BARS_AFTER_ANCHOR", 2) or 0)
    max_bars = int(config.get("MAX_BARS_BREAKOUT_TO_RETEST", 12) or 1)
    max_touches = int(config.get("MAX_RETEST_TOUCHES", 1) or 1)
    invalid_mult = float(config.get("INVALIDATION_ATR_MULT", 1.0) or 0.0)
    ext_mult = float(config.get("MAX_EXTENSION_ATR_MULT", 1.0) or 0.0)
    min_close_pos = float(config.get("MIN_CLOSE_POSITION_IN_RANGE", 0.35) or 0.0)
    atr_period = int(config.get("ATR_PERIOD", 14) or 14)

    butuh = strategy.required_lookback_bars(config)
    n = len(klines)
    if n < butuh:
        return SetupResult(False, f"data candle kurang: {n} dari minimum {butuh}")

    atr_abs = strategy.atr(klines, atr_period)
    if atr_abs is None or atr_abs <= 0:
        return SetupResult(False, "ATR tidak bisa dihitung atau nol (candle datar)")

    last = klines[-1]
    if last.close <= 0:
        return SetupResult(False, "harga close terakhir tidak valid")
    atr_pct = atr_abs / last.close * 100.0

    pivots = _pivot_high_indexes(klines, wing)
    if not pivots:
        return SetupResult(False, "tidak ada swing high (pivot) di dalam jendela",
                           atr_pct=atr_pct, atr_abs=atr_abs)

    # ---- Telusuri jendela dari candle tertua ke terbaru -------------
    anchor: Optional[int] = None
    level: Optional[float] = None
    touches = 0            # jumlah KUNJUNGAN ke zona, bukan jumlah candle
    sedang_di_zona = False
    alasan_gugur = ""

    for b in range(n):
        candle = klines[b]

        if anchor is not None and level is not None:
            zone_low = level - zone_mult * atr_abs
            zone_high = level + zone_mult * atr_abs
            invalid_price = level - invalid_mult * atr_abs

            # Invalidasi harga: close tertutup di bawah level - k x ATR.
            if candle.close < invalid_price:
                alasan_gugur = (f"setup gugur: close {candle.close:.8g} di bawah batas "
                                f"invalidasi {invalid_price:.8g}")
                anchor = None
                level = None
                touches = 0
                sedang_di_zona = False
                continue

            # Invalidasi waktu: retest tidak kunjung datang.
            if b - anchor > max_bars:
                alasan_gugur = (f"setup gugur: lebih dari {max_bars} candle sejak breakout "
                                "tanpa retest terkonfirmasi")
                anchor = None
                level = None
                touches = 0
                sedang_di_zona = False
                # Candle b masih boleh menjadi breakout baru, jadi jangan
                # continue di sini, biarkan jatuh ke pemeriksaan breakout.
            else:
                # Hitung KUNJUNGAN ke zona, bukan jumlah candle di dalam zona.
                # Retest yang berlangsung tiga candle berturut-turut tetap
                # dihitung satu kunjungan, karena itu memang satu peristiwa
                # retest. Kalau dihitung per candle, MAX_RETEST_TOUCHES=1 akan
                # menolak hampir semua retest normal.
                if b > anchor:
                    di_zona = zone_low <= candle.low <= zone_high
                    if di_zona and not sedang_di_zona:
                        touches += 1
                    sedang_di_zona = di_zona
                continue

        # Belum ada setup aktif: cari breakout pada candle b.
        lvl = _breakout_level_for(klines, pivots, b, swing_lookback, wing)
        if lvl is None:
            continue
        if candle.close > lvl + buffer_mult * atr_abs:
            anchor = b
            level = lvl
            touches = 0
            sedang_di_zona = False

    if anchor is None or level is None:
        alasan = alasan_gugur or "tidak ada breakout dalam jendela"
        return SetupResult(False, alasan, atr_pct=atr_pct, atr_abs=atr_abs)

    zone_low = level - zone_mult * atr_abs
    zone_high = level + zone_mult * atr_abs
    invalid_price = level - invalid_mult * atr_abs
    idx_terakhir = n - 1

    dasar = dict(breakout_level=level, zone_low=zone_low, zone_high=zone_high,
                 anchor_index=anchor, atr_pct=atr_pct, atr_abs=atr_abs,
                 invalidation_price=invalid_price, retest_touches=touches)

    # ---- Syarat candle terakhir ------------------------------------
    if idx_terakhir == anchor:
        return SetupResult(False, "belum retest: breakout baru terjadi pada candle terakhir", **dasar)

    bars_setelah_anchor = idx_terakhir - anchor
    if bars_setelah_anchor < vwap_min_bars:
        return SetupResult(
            False,
            f"anchored VWAP belum layak: baru {bars_setelah_anchor} candle setelah anchor, "
            f"minimum {vwap_min_bars}",
            **dasar)

    vwap = strategy.anchored_vwap(klines, anchor)
    dasar["anchored_vwap"] = vwap
    if vwap is None:
        return SetupResult(False, "anchored VWAP tidak bisa dihitung (volume nol atau tidak tersedia)",
                           **dasar)

    if abs(vwap - level) > konfluensi_mult * atr_abs:
        return SetupResult(
            False,
            f"tanpa konfluensi: anchored VWAP {vwap:.8g} terlalu jauh dari level {level:.8g} "
            f"(batas {konfluensi_mult:g} x ATR)",
            **dasar)

    if touches > max_touches:
        return SetupResult(
            False,
            f"sentuhan zona sudah {touches} kali, batas MAX_RETEST_TOUCHES {max_touches}",
            **dasar)

    if not (zone_low <= last.low <= zone_high):
        return SetupResult(
            False,
            f"belum retest: low candle terakhir {last.low:.8g} berada di luar zona "
            f"{zone_low:.8g}-{zone_high:.8g}",
            **dasar)

    if last.close <= level:
        return SetupResult(
            False,
            f"retest gagal: close {last.close:.8g} tidak kembali di atas level {level:.8g}",
            **dasar)

    candle_range = last.high - last.low
    if candle_range <= 0:
        return SetupResult(False, "candle terakhir tidak punya range (high sama dengan low)", **dasar)
    close_pos = (last.close - last.low) / candle_range
    if close_pos < min_close_pos:
        return SetupResult(
            False,
            f"close candle retest hanya di posisi {close_pos:.2f} dari range, "
            f"minimum {min_close_pos:.2f}",
            **dasar)

    if last.close <= vwap:
        return SetupResult(
            False,
            f"close {last.close:.8g} masih di bawah anchored VWAP {vwap:.8g}",
            **dasar)

    ekstensi = (last.close - level) / atr_abs
    dasar["extension_atr"] = ekstensi
    if ekstensi > ext_mult:
        return SetupResult(
            False,
            f"anti-kejar: close sudah {ekstensi:.2f} x ATR di atas level, batas {ext_mult:g}",
            **dasar)

    return SetupResult(
        True,
        (f"retest sah: level {level:.8g}, zona {zone_low:.8g}-{zone_high:.8g}, "
         f"anchored VWAP {vwap:.8g}, ATR {atr_pct:.2f}%, ekstensi {ekstensi:.2f} x ATR, "
         f"batas invalidasi {invalid_price:.8g}"),
        **dasar)


def confirm_entry(klines: list[Kline], config: dict) -> tuple[bool, str]:
    """Konfirmasi entry dengan alasan yang dapat dicatat ke log.

    Signature-nya sengaja dipertahankan (bool, str) supaya seluruh pemanggil
    lama, yaitu backtest.py, portfolio_backtest.py, dan watchlist_auto.py,
    tidak perlu diubah bentuk panggilannya. Pemanggil yang butuh level dan
    zona memakai detect_pullback_retest() langsung.
    """
    hasil = detect_pullback_retest(klines, config)
    return hasil.ok, hasil.reason


def setup_quality_key(setup: SetupResult, candidate: Candidate) -> tuple:
    """Kunci pengurutan kandidat: retest paling rapat dulu, lalu paling likuid.

    Ukuran utamanya adalah jarak close terakhir terhadap breakout_level dalam
    satuan ATR. Semakin kecil, semakin dekat entry ke level yang menjadi dasar
    stop dan invalidasi, sehingga risiko per trade lebih terdefinisi. Pemutus
    seri adalah volume kuotasi 24 jam (lebih besar lebih dulu).

    Ini keputusan desain, bukan hasil yang sudah tervalidasi.
    """
    ekstensi = setup.extension_atr if setup.extension_atr is not None else float("inf")
    return (ekstensi, -float(candidate.quote_volume))


def find_best_candidate(tickers: list, klines_fetcher, config: dict,
                        tradable_symbols: "set | None" = None) -> Optional[Candidate]:
    """Kembalikan kandidat dengan setup pullback retest TERBAIK pada scan ini.

    Bukan lagi "gainer tertinggi yang lolos konfirmasi". Alurnya sekarang:

      1. Saring semesta (filter_and_rank_candidates), urut volume kuotasi.
      2. Ambil TOP_N_CANDIDATES_TO_CONFIRM teratas saja. Batas ini yang
         menjaga rate limit: setiap simbol butuh satu panggilan klines dengan
         bobot IP 2, sedangkan plafon REQUEST_WEIGHT adalah 6000 per menit per
         IP (dicek 2026-09-25 dari /api/v3/exchangeInfo). Dengan default 10
         simbol per scan, biaya klines hanya 20 bobot per scan ditambah 80
         bobot untuk ticker 24 jam seluruh pasar.
      3. Evaluasi setup untuk setiap simbol, kumpulkan yang lolos, lalu urut
         dengan setup_quality_key().

    ``klines_fetcher`` sengaja diinjeksikan agar fungsi tetap murni dan mudah
    diuji tanpa jaringan. Ia wajib mengembalikan candle TERTUTUP saja.
    """
    ranked = filter_and_rank_candidates(tickers, config, tradable_symbols)
    top_n = ranked[: int(config.get("TOP_N_CANDIDATES_TO_CONFIRM", 10) or 10)]

    lolos: list[Candidate] = []
    for cand in top_n:
        try:
            klines = klines_fetcher(cand.symbol)
        except Exception as exc:  # noqa: BLE001 - satu simbol gagal tidak boleh menghentikan scan
            cand.confirmed = False
            cand.confirm_reason = f"gagal mengambil candle: {exc}"
            continue
        hasil = detect_pullback_retest(klines or [], config)
        cand.confirmed = hasil.ok
        cand.confirm_reason = hasil.reason
        cand.setup = hasil
        if hasil.ok:
            lolos.append(cand)

    if not lolos:
        return None
    lolos.sort(key=lambda c: setup_quality_key(c.setup, c))
    return lolos[0]
