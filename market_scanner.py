"""Seleksi kandidat dan deteksi setup PULLBACK dan RETEST untuk bot Spot.

Modul ini tidak memprediksi arah harga. Ia mencari STRUKTUR harga yang sudah
terjadi pada candle yang SUDAH tertutup: sebuah breakout di atas swing high,
lalu pullback kembali ke area level itu, lalu candle yang menutup kembali di
atas level. Long only, satu posisi per rotasi.

Seleksi pasar punya DUA lapis. Lapis pertama saringan struktural (status
perdagangan, stablecoin, leveraged token, blacklist, volume kuotasi minimum).
Lapis kedua GERBANG PUMP yang bersifat WAJIB: simbol hanya boleh menjadi
kandidat kalau harganya naik minimal PUMP_MIN_24H_CHANGE_PCT dalam 24 jam DAN
volume kuotasi 24 jamnya minimal PUMP_VOLUME_SURGE_MULT kali rata-rata volume
kuotasi 7 hari penuh sebelumnya. Gerbang ini menggantikan keputusan desain
lama yang meniadakan syarat kenaikan 24 jam, jadi koin yang sedang turun 24
jam TIDAK lagi bisa menjadi kandidat.

Pengurutan kandidat tetap memakai kualitas setup pullback retest, bukan
besarnya kenaikan. Gerbang pump hanya menentukan siapa yang boleh ikut
dievaluasi, bukan siapa yang lebih layak dibeli.

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

import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, Optional

import strategy
from strategy import Kline

logger = logging.getLogger(__name__)

# Jumlah candle harian PENUH yang wajib tersedia sebelum sebuah simbol boleh
# dinilai oleh gerbang volume. Koin yang baru listing dan belum punya tujuh
# hari riwayat DITOLAK (fail closed), bukan diloloskan diam-diam.
PUMP_GATE_DAILY_CANDLES = 7

MS_PER_DAY = 86_400_000

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

    ``ok`` True hanya kalau candle terakhir yang tertutup adalah candle
    konfirmasi retest. Field lain tetap diisi sebisanya walaupun ok False,
    supaya log bisa menjelaskan setup sampai mana prosesnya.
    """
    ok: bool
    reason: str
    breakout_level: Optional[float] = None
    zone_low: Optional[float] = None
    zone_high: Optional[float] = None
    anchor_index: Optional[int] = None
    retest_touches: int = 0
    atr_value: Optional[float] = None


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


def is_structurally_allowed_symbol(symbol: str, config: dict,
                                   tradable_symbols: "set | None" = None) -> bool:
    """Gerbang statis semesta yang dapat dipakai ulang oleh semua jalur.

    Tidak memerlukan ticker historis, sehingga backtest satu simbol dapat
    menolak stablecoin, leveraged token, blacklist, quote salah, dan simbol
    non-TRADING dengan policy yang sama seperti scanner live.
    """
    quote_asset = str(config.get("QUOTE_ASSET", ""))
    if not quote_asset or not symbol.endswith(quote_asset):
        return False
    if symbol in set(config.get("EXTRA_EXCLUDE_SYMBOLS", [])):
        return False
    if tradable_symbols is not None and symbol not in tradable_symbols:
        return False
    base_asset = symbol[: -len(quote_asset)]
    return bool(base_asset and base_asset not in STABLE_BASE_ASSETS
                and not _looks_leveraged(base_asset))


def spread_pct_from_book(bid: float, ask: float) -> float:
    """Spread persen canonical terhadap mid-price untuk seluruh pemakai."""
    mid = (float(bid) + float(ask)) / 2.0
    return ((float(ask) - float(bid)) / mid * 100.0) if mid > 0 else float("inf")


# ======================================================================
# Gerbang pump: naik 24 jam DAN volume sedang naik
# ======================================================================

def _angka_wajar(nilai) -> bool:
    """True hanya untuk angka hingga dan tidak negatif.

    Mengikuti pola penjagaan di strategy.parse_klines(): NaN tidak boleh
    lolos diam-diam sampai ke perbandingan numerik, karena SETIAP
    perbandingan dengan NaN menghasilkan False. Kalau NaN dibiarkan, sebuah
    simbol bisa lolos atau gagal gerbang tanpa alasan yang bisa dijelaskan.
    """
    try:
        angka = float(nilai)
    except (TypeError, ValueError):
        return False
    if math.isnan(angka) or math.isinf(angka):
        return False
    return angka >= 0.0


def aggregate_to_daily(klines: list[Kline]) -> list[Kline]:
    """Gabungkan candle intraday menjadi candle harian UTC.

    Dipakai jalur backtest sebagai sumber cadangan ketika pemanggil tidak
    menyediakan candle 1d hasil unduhan terpisah. Pengelompokan memakai
    open_time // 86.400.000 sehingga batas harinya sama persis dengan candle
    1d Binance (UTC). Field quote_volume dijumlahkan, itulah satu-satunya
    field yang dipakai gerbang volume.
    """
    ember: dict[int, list[Kline]] = {}
    for k in klines:
        ember.setdefault(int(k.open_time) // MS_PER_DAY, []).append(k)

    harian: list[Kline] = []
    for hari in sorted(ember):
        isi = ember[hari]
        harian.append(Kline(
            open_time=hari * MS_PER_DAY,
            open=isi[0].open,
            high=max(x.high for x in isi),
            low=min(x.low for x in isi),
            close=isi[-1].close,
            close_time=hari * MS_PER_DAY + MS_PER_DAY - 1,
            volume=sum(x.volume for x in isi),
            quote_volume=sum(x.quote_volume for x in isi),
        ))
    return harian


def average_prior_daily_quote_volume(
    daily_klines: "list[Kline] | None",
    reference_ms: int,
    need: int = PUMP_GATE_DAILY_CANDLES,
) -> tuple[Optional[float], str]:
    """Rata-rata quote_volume dari ``need`` candle harian PENUH terakhir.

    Yang diperhitungkan hanya candle harian yang close_time-nya sudah lewat
    pada ``reference_ms``. Inilah yang membuat perhitungan identik di live dan
    di backtest sekaligus bebas look-ahead: di live ``reference_ms`` adalah
    waktu sekarang sehingga candle hari ini yang belum tertutup terbuang, di
    backtest ``reference_ms`` adalah waktu bar yang sedang diuji sehingga
    volume hari-hari SESUDAHNYA tidak pernah ikut terhitung. Pola ini sama
    dengan penjagaan sayap kanan pivot di _breakout_level_for().

    Return (rata_rata, alasan). rata_rata None berarti data tidak memenuhi
    syarat, dan ``alasan`` menjelaskan kenapa.
    """
    if not daily_klines:
        return None, "tidak ada candle harian"

    tertutup = [k for k in daily_klines if int(k.close_time) <= int(reference_ms)]
    if len(tertutup) < need:
        return None, (f"riwayat harian kurang: {len(tertutup)} candle tertutup, "
                      f"minimum {need} (kemungkinan koin baru listing)")

    dipakai = tertutup[-need:]
    volumes: list[float] = []
    for k in dipakai:
        if not _angka_wajar(k.quote_volume):
            return None, ("quote_volume candle harian tidak wajar "
                          f"({k.quote_volume!r}), data bursa rusak")
        volumes.append(float(k.quote_volume))

    return sum(volumes) / float(need), f"rata-rata {need} hari penuh terakhir"


def evaluate_pump_gate(price_change_pct, quote_volume,
                       avg_daily_quote_volume: Optional[float],
                       config: dict, btc_drop_pct: float | None = None) -> tuple[bool, str]:
    """Inti gerbang pump, MURNI dan tanpa jaringan.

    Dipakai bersama oleh jalur live dan seluruh jalur backtest supaya tidak
    pernah ada dua definisi "sedang pump" yang bisa berbeda diam-diam.

      Syarat 1: price_change_pct >= PUMP_MIN_24H_CHANGE_PCT
      Syarat 2: quote_volume >= PUMP_VOLUME_SURGE_MULT x rata-rata volume
                kuotasi 7 hari penuh sebelumnya

    ``avg_daily_quote_volume`` None berarti riwayat harian tidak memenuhi
    syarat, dan hasilnya DITOLAK (fail closed).
    """
    min_change = float(config.get("PUMP_MIN_24H_CHANGE_PCT", 10.0) or 0.0)
    surge_mult = float(config.get("PUMP_VOLUME_SURGE_MULT", 1.5) or 0.0)

    if btc_drop_pct is None:
        btc_drop_pct = config.get("_btc_drop_pct")
    if config.get("BTC_FILTER_ENABLED", False):
        if btc_drop_pct is None:
            if config.get("_btc_filter_fail_closed", False):
                return False, "data filter BTC tidak tersedia, simbol ditolak (fail closed)"
            # Pemanggil murni/backtest lama boleh tidak mengaktifkan filter
            # BTC karena tidak membawa deret BTC historis. Jalur LIVE selalu
            # mengatur _btc_filter_fail_closed=True secara eksplisit.
        else:
            max_drop = abs(float(config.get("BTC_MAX_DROP_PCT", 5.0) or 0.0))
            if float(btc_drop_pct) <= -max_drop:
                return False, (f"BTC turun {float(btc_drop_pct):.2f}% dalam "
                               f"{int(config.get("BTC_LOOKBACK_BARS", 3) or 3)} candle")

    if not _angka_wajar(quote_volume):
        return False, f"quote_volume 24 jam tidak wajar ({quote_volume!r})"
    try:
        change = float(price_change_pct)
    except (TypeError, ValueError):
        return False, f"priceChangePercent tidak bisa dibaca ({price_change_pct!r})"
    if math.isnan(change) or math.isinf(change):
        return False, f"priceChangePercent tidak wajar ({price_change_pct!r})"

    if change < min_change:
        return False, (f"kenaikan 24 jam {change:.2f}% di bawah ambang "
                       f"{min_change:g}%")

    if avg_daily_quote_volume is None:
        return False, "rata-rata volume harian tidak tersedia"
    if not _angka_wajar(avg_daily_quote_volume):
        return False, f"rata-rata volume harian tidak wajar ({avg_daily_quote_volume!r})"
    if avg_daily_quote_volume <= 0:
        return False, "rata-rata volume harian nol, perbandingan volume tidak bermakna"

    butuh = surge_mult * float(avg_daily_quote_volume)
    rasio = float(quote_volume) / float(avg_daily_quote_volume)
    if float(quote_volume) < butuh:
        return False, (f"volume 24 jam {float(quote_volume):.0f} hanya {rasio:.2f}x "
                       f"rata-rata 7 hari {float(avg_daily_quote_volume):.0f}, "
                       f"minimum {surge_mult:g}x")

    return True, (f"pump sah: naik {change:.2f}% (ambang {min_change:g}%), "
                  f"volume {rasio:.2f}x rata-rata 7 hari (ambang {surge_mult:g}x)")


def is_pumping_today(symbol: str, price_change_pct, quote_volume,
                     get_daily_klines_fn: "Optional[Callable[[str], list[Kline]]]",
                     config: dict,
                     reference_ms: "int | None" = None) -> tuple[bool, str]:
    """Gerbang pump untuk SATU simbol, termasuk pengambilan candle harian.

    ``get_daily_klines_fn`` diinjeksikan supaya fungsi ini tetap mudah diuji
    tanpa jaringan, dan supaya jalur backtest dapat memberi candle harian
    historis. Fungsi itu harus mengembalikan list[Kline] harian kronologis;
    candle hari berjalan boleh ikut, nanti dibuang oleh
    average_prior_daily_quote_volume() berdasarkan ``reference_ms``.

    Syarat 1 diperiksa LEBIH DULU karena datanya sudah ada di ticker 24 jam
    yang diambil sekali untuk seluruh pasar. Request candle harian (bobot IP
    2) hanya terjadi untuk simbol yang sudah lolos syarat 1, yaitu subset
    kecil, sehingga beban rate limit tetap ringan.

    Kegagalan request untuk satu simbol (timeout, simbol didelisting di tengah
    scan) TIDAK melempar keluar: simbol itu ditolak dan scan lanjut.
    """
    min_change = float(config.get("PUMP_MIN_24H_CHANGE_PCT", 10.0) or 0.0)
    try:
        change = float(price_change_pct)
    except (TypeError, ValueError):
        return False, f"priceChangePercent tidak bisa dibaca ({price_change_pct!r})"
    if math.isnan(change) or math.isinf(change):
        return False, f"priceChangePercent tidak wajar ({price_change_pct!r})"
    if change < min_change:
        return False, f"kenaikan 24 jam {change:.2f}% di bawah ambang {min_change:g}%"

    if get_daily_klines_fn is None:
        # FAIL CLOSED. Tanpa sumber candle harian, syarat volume tidak bisa
        # dibuktikan, dan gerbang ini WAJIB. Meloloskan simbol di sini sama
        # saja menghidupkan kembali perilaku lama tanpa gerbang.
        return False, "sumber candle harian tidak tersedia, simbol ditolak (fail closed)"

    ref = int(reference_ms) if reference_ms is not None else int(time.time() * 1000)

    try:
        harian = get_daily_klines_fn(symbol)
    except Exception as exc:  # noqa: BLE001 - satu simbol gagal tidak boleh menghentikan scan
        return False, f"gagal mengambil candle harian: {str(exc)[:160]}"

    rata, alasan = average_prior_daily_quote_volume(harian, ref)
    if rata is None:
        return False, alasan
    return evaluate_pump_gate(change, quote_volume, rata, config)


def pump_gate_ok_at(daily_klines: "list[Kline] | None", reference_ms: int,
                    price_change_pct, quote_volume, config: dict) -> bool:
    """Gerbang pump untuk satu TITIK WAKTU historis (jalur backtest).

    Bentuk ringkas dari is_pumping_today() untuk pemanggil yang sudah punya
    candle harian di tangan dan tidak perlu request apa pun. Logika keputusan
    tetap satu, yaitu evaluate_pump_gate(), supaya live dan backtest tidak
    bisa berbeda diam-diam.
    """
    rata, _alasan = average_prior_daily_quote_volume(daily_klines, reference_ms)
    ok, _r = evaluate_pump_gate(price_change_pct, quote_volume, rata, config)
    return ok


def make_daily_klines_fetcher(client, *, limit: int = PUMP_GATE_DAILY_CANDLES + 1,
                              end_time_ms: "int | None" = None,
                              cache: "dict | None" = None):
    """Pembuat fungsi pengambil candle 1d untuk gerbang pump.

    ``limit`` default 8 = 7 candle harian penuh + kemungkinan candle hari
    berjalan yang nanti dibuang berdasarkan waktu acuan.

    ``cache`` opsional: dict yang dipakai ulang selama SATU siklus scan supaya
    simbol yang sama tidak diminta dua kali (misalnya saat dipanggil dari
    filter_and_rank_candidates lalu dari jalur lain di siklus yang sama).
    Jangan dipakai lintas siklus, datanya akan basi.
    """
    def _fetch(symbol: str) -> list[Kline]:
        if cache is not None and symbol in cache:
            return cache[symbol]
        raw = client.get_klines(symbol, interval="1d", limit=limit,
                                end_time_ms=end_time_ms)
        parsed = strategy.parse_klines(raw)
        if cache is not None:
            cache[symbol] = parsed
        return parsed

    return _fetch


def filter_and_rank_candidates(tickers: list, config: dict,
                               tradable_symbols: "set | None" = None,
                               *,
                               get_daily_klines_fn: "Optional[Callable[[str], list[Kline]]]" = None,
                               reference_ms: "int | None" = None,
                               apply_pump_gate: bool = True) -> list[Candidate]:
    """Saring semesta pair lalu urutkan dari volume kuotasi 24 jam tertinggi.

    Ada DUA lapis saringan.

      1. Struktural dan likuiditas: quote asset, stablecoin, leveraged token,
         blacklist, status TRADING, dan MIN_QUOTE_VOLUME_USDT_24H.
      2. GERBANG PUMP (wajib, lihat is_pumping_today): naik minimal
         PUMP_MIN_24H_CHANGE_PCT dalam 24 jam DAN volume kuotasi 24 jam
         minimal PUMP_VOLUME_SURGE_MULT kali rata-rata volume kuotasi 7 hari
         penuh sebelumnya.

    CATATAN REVISI: sebelumnya di sini tertulis bahwa gerbang kenaikan 24 jam
    "sudah DIHAPUS". Keputusan itu TIDAK berlaku lagi. Koin yang sedang turun
    24 jam, atau yang volumenya tidak sedang naik, tidak akan pernah muncul
    sebagai kandidat.

    Urutan volume di sini hanya menentukan simbol mana yang candle-nya
    diunduh lebih dulu saat anggaran request terbatas, BUKAN kandidat mana
    yang lebih layak dibeli. Pengurutan berdasarkan kualitas setup dilakukan
    di find_best_candidate().

    ``tickers`` adalah hasil mentah GET /api/v3/ticker/24hr untuk seluruh pair
    (bobot IP 80, dicek 2026-09-25). Field priceChangePercent dan quoteVolume
    dari respons yang SAMA itu yang dipakai gerbang pump, jadi syarat pertama
    tidak menambah satu pun request.

    ``tradable_symbols`` opsional: himpunan simbol yang statusnya TRADING
    menurut exchangeInfo. Kalau diberikan, simbol di luar himpunan itu dibuang.
    Kalau None, saringan status dilewati (dipakai jalur backtest yang bekerja
    dari data historis dan tidak punya snapshot exchangeInfo saat itu).

    ``get_daily_klines_fn`` wajib diisi selama ``apply_pump_gate`` True. Kalau
    tidak diisi, SEMUA simbol ditolak (fail closed), karena syarat volume
    tidak bisa dibuktikan.

    ``apply_pump_gate=False`` HANYA untuk pemilihan semesta unduhan backtest
    portofolio, di mana gerbang harus dievaluasi per titik waktu historis di
    dalam simulasi, bukan dari ticker hari ini. Jangan dipakai di jalur live.
    """
    quote_asset = config["QUOTE_ASSET"]
    min_vol = float(config.get("MIN_QUOTE_VOLUME_USDT_24H", 0) or 0)

    if apply_pump_gate and get_daily_klines_fn is None:
        logger.warning(
            "Gerbang pump aktif tetapi sumber candle harian tidak diberikan. "
            "Semua simbol ditolak (fail closed).")

    lolos_struktural = 0
    out = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if not is_structurally_allowed_symbol(symbol, config, tradable_symbols):
            continue

        base_asset = symbol[: -len(quote_asset)]

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

        lolos_struktural += 1

        if apply_pump_gate:
            ok_pump, alasan = is_pumping_today(
                symbol, price_change_pct, quote_volume,
                get_daily_klines_fn, config, reference_ms=reference_ms)
            if not ok_pump:
                logger.debug("Gerbang pump menolak %s: %s", symbol, alasan)
                continue

        out.append(Candidate(
            symbol=symbol, base_asset=base_asset, price_change_pct=price_change_pct,
            quote_volume=quote_volume, last_price=last_price,
        ))

    out.sort(key=lambda c: c.quote_volume, reverse=True)

    if apply_pump_gate:
        if out:
            logger.info("Gerbang pump: %d kandidat lolos dari %d simbol yang lolos "
                        "saringan likuiditas.", len(out), lolos_struktural)
        else:
            # Sengaja eksplisit: dengan ambang +%s dan volume naik, pasar sepi
            # bisa membuat hasilnya nol untuk waktu lama. Diam di log membuat
            # kondisi ini tampak seperti bot macet.
            logger.info("Gerbang pump: 0 kandidat lolos gerbang pump (dari %d simbol "
                        "yang lolos saringan likuiditas). Ambang: naik >= %g%% "
                        "dan volume >= %gx rata-rata 7 hari.",
                        lolos_struktural,
                        float(config.get("PUMP_MIN_24H_CHANGE_PCT", 10.0) or 0.0),
                        float(config.get("PUMP_VOLUME_SURGE_MULT", 1.5) or 0.0))
    return out


# ======================================================================
# Deteksi setup: momentum pump dan konfirmasi volume rolling
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


def _pivot_low_indexes(klines: list[Kline], wing: int) -> list[int]:
    """Cari pivot low dengan sayap kanan yang sudah tertutup."""
    if wing < 1 or len(klines) < 2 * wing + 1:
        return []
    return [p for p in range(wing, len(klines) - wing)
            if all(klines[p].low < klines[j].low for j in range(p-wing, p))
            and all(klines[p].low < klines[j].low for j in range(p+1, p+wing+1))]


def _higher_low_confirmed(klines: list[Kline], wing: int) -> bool:
    """True bila pivot low terakhir lebih tinggi dari pivot low sebelumnya."""
    pivots = _pivot_low_indexes(klines, wing)
    return len(pivots) >= 2 and klines[pivots[-1]].low > klines[pivots[-2]].low


def _rolling_volume_confirmation(klines: list[Kline], config: dict) -> tuple[bool, str]:
    """Validasi lonjakan volume pada candle konfirmasi yang sudah close.

    Candle keputusan terakhir tidak pernah masuk ke rata-rata pembandingnya.
    Dengan begitu, sinyal tidak dapat meloloskan diri hanya karena volume
    candle yang sedang diuji ikut menaikkan rata-rata. Quote volume dipakai
    bila tersedia karena satuannya langsung mengikuti asset kuotasi; volume
    dasar menjadi fallback untuk fixture lama yang belum memilikinya.
    """
    if not bool(config.get("ROLLING_VOLUME_FILTER_ENABLED", True)):
        return True, "rolling volume nonaktif"

    lookback = max(1, int(config.get("ROLLING_VOLUME_LOOKBACK_BARS", 20) or 20))
    confirmations = max(1, int(config.get("ROLLING_VOLUME_CONFIRMATION_BARS", 1) or 1))
    multiplier = float(config.get("ROLLING_VOLUME_SURGE_MULT", 2.0) or 0.0)
    required = lookback + confirmations
    if len(klines) < required:
        return False, f"data volume rolling kurang: {len(klines)} dari minimum {required}"
    if not math.isfinite(multiplier) or multiplier <= 0:
        return False, "ROLLING_VOLUME_SURGE_MULT tidak valid"

    values = []
    for k in klines:
        quote = float(getattr(k, "quote_volume", 0.0) or 0.0)
        base = float(getattr(k, "volume", 0.0) or 0.0)
        value = quote if quote > 0 else base
        if not math.isfinite(value) or value < 0:
            return False, "volume candle tidak valid"
        values.append(value)

    passed = 0
    ratios = []
    for offset in range(confirmations):
        idx = len(values) - confirmations + offset
        prior = values[idx - lookback:idx]
        average = sum(prior) / lookback
        current = values[idx]
        if average <= 0:
            return False, "rata-rata volume rolling nol"
        ratio = current / average
        ratios.append(ratio)
        if ratio >= multiplier:
            passed += 1

    ok = passed == confirmations
    detail = (f"rolling volume {min(ratios):.2f}x, minimum {multiplier:g}x, "
              f"{passed}/{confirmations} candle konfirmasi")
    return ok, detail


def detect_pullback_retest(klines: list[Kline], config: dict) -> SetupResult:
    """Deteksi momentum pump dengan minimal tiga dari empat konfirmasi.

    Semua indikator hanya membaca candle dalam ``klines`` yang diasumsikan
    sudah close dan kronologis. Konfirmasi terdiri dari EMA cross, RSI sehat,
    MACD histogram menguat, dan higher low setelah pump awal. Tidak ada candle
    yang belum close atau data masa depan yang digunakan.
    """
    n = len(klines)
    period = 14
    wing = max(1, int(config.get("SWING_PIVOT_WING_BARS", 2) or 2))
    minimum = max(30, strategy.required_lookback_bars(config))
    if n < minimum:
        return SetupResult(False, f"data candle kurang: {n} dari minimum {minimum}")
    closes = [float(k.close) for k in klines]
    if any(x <= 0 or not math.isfinite(x) for x in closes):
        return SetupResult(False, "close candle tidak valid")
    volume_ok, volume_detail = _rolling_volume_confirmation(klines, config)
    if not volume_ok:
        return SetupResult(False, f"rolling volume ditolak: {volume_detail}")
    ema9, ema21 = strategy.ema(closes, 9), strategy.ema(closes, 21)
    if len(ema9) < 2:
        return SetupResult(False, "data EMA kurang")
    ema_cross = ema9[-2] <= ema21[-2] and ema9[-1] > ema21[-1]
    rsi_values = strategy.rsi(closes, period)
    rsi_ok = 50.0 <= rsi_values[-1] <= 75.0
    _macd, _signal, histogram = strategy.macd(closes)
    macd_ok = len(histogram) >= 2 and (histogram[-1] > histogram[-2] or
                                       (histogram[-2] <= 0 < histogram[-1]))
    higher_low = _higher_low_confirmed(klines, wing)
    confirmations = sum((ema_cross, rsi_ok, macd_ok, higher_low))
    details = (f"EMA={'ya' if ema_cross else 'tidak'}, RSI={rsi_values[-1]:.2f}, "
               f"MACD={'naik' if macd_ok else 'tidak'}, HL={'ya' if higher_low else 'tidak'} "
               f"({confirmations}/4), {volume_detail}")
    if confirmations < 3:
        return SetupResult(False, f"konfirmasi entry kurang dari 3/4: {details}")
    return SetupResult(True, f"momentum pump sah: {details}",
                       breakout_level=klines[-1].close,
                       zone_low=klines[-1].low, zone_high=klines[-1].high,
                       anchor_index=max(0, n - 1), retest_touches=0,
                       atr_value=strategy.atr(klines, int(config.get("ATR_PERIOD", 14) or 14)))

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
    """Kunci pengurutan kandidat setelah setup lolos.

    Setelah filter berbasis volatilitas dihapus, pemutus urutan yang tersisa
    adalah likuiditas 24 jam. Volume kuotasi lebih besar didahulukan.
    """
    return (-float(candidate.quote_volume),)


def find_best_candidate(tickers: list, klines_fetcher, config: dict,
                        tradable_symbols: "set | None" = None,
                        daily_klines_fetcher=None,
                        reference_ms: "int | None" = None) -> Optional[Candidate]:
    """Kembalikan kandidat dengan setup pullback retest TERBAIK pada scan ini.

    Bukan lagi "gainer tertinggi yang lolos konfirmasi". Alurnya sekarang:

      1. Saring semesta (filter_and_rank_candidates), termasuk GERBANG PUMP
         yang wajib, lalu urut volume kuotasi.
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
    ranked = filter_and_rank_candidates(
        tickers, config, tradable_symbols,
        get_daily_klines_fn=daily_klines_fetcher, reference_ms=reference_ms)
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
