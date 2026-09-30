"""Scanner pasar dan filter operasional Binance Spot.

Modul ini menyediakan filter semesta, gerbang pump, likuiditas, spread, usia
listing, dan korelasi BTC untuk monitoring pasar. Jalur pembukaan posisi baru
telah dihapus. Modul trading hanya mempertahankan pengelolaan posisi yang
sudah ada dan fungsi exit.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, Optional


from strategy import indicators as strategy
from strategy.indicators import Kline

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
    """Simbol yang lolos filter pasar untuk monitoring."""
    symbol: str
    base_asset: str
    price_change_pct: float
    quote_volume: float
    last_price: float


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
    volume hari-hari SESUDAHNYA tidak pernah ikut terhitung.

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
    yang lebih layak dipilih. Pengurutan volume hanya untuk monitoring.

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
