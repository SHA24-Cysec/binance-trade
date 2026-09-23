"""
Logika penemuan "kandidat pump": filter & ranking dari data ticker 24 jam
seluruh pasar, lalu konfirmasi momentum jangka pendek dari candle terbaru.

PENTING (baca ini): tidak ada bagian dari modul ini yang MEMPREDIKSI pump
sebelum terjadi. Semua deteksi di sini bersifat REAKTIF -- mencari koin yang
harganya SUDAH bergerak naik signifikan dalam 24 jam terakhir dan volumenya
sedang tinggi, lalu mengecek apakah momentumnya kelihatannya masih
berlanjut (belum berbalik arah). Ini adalah pendekatan "momentum chasing",
bukan crystal ball.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Optional

from strategy import Kline

# Stablecoin/aset yang bukan "koin" dalam artian trading pump (kalau jadi
# base asset, pair-nya seperti USDCUSDT bukan target yang relevan).
STABLE_BASE_ASSETS = {
    "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "EUR", "GBP", "TRY",
    "BRL", "AEUR", "USTC", "USDD", "PYUSD", "USDE",
}

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


def _looks_leveraged(base_asset: str) -> bool:
    return base_asset.endswith(LEVERAGED_TOKEN_SUFFIXES)


def filter_and_rank_candidates(tickers: list, config: dict) -> list[Candidate]:
    """tickers = hasil mentah GET /api/v3/ticker/24hr (list of dict, semua
    pair). Mengembalikan daftar Candidate yang lolos filter, terurut dari
    kenaikan % tertinggi ke terendah."""
    quote_asset = config["QUOTE_ASSET"]
    exclude_symbols = set(config.get("EXTRA_EXCLUDE_SYMBOLS", []))
    min_pct = config["MIN_PUMP_PCT_24H"]
    min_vol = config["MIN_QUOTE_VOLUME_USDT_24H"]

    out = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith(quote_asset):
            continue
        if symbol in exclude_symbols:
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
        if price_change_pct < min_pct:
            continue
        if quote_volume < min_vol:
            continue

        out.append(Candidate(
            symbol=symbol, base_asset=base_asset, price_change_pct=price_change_pct,
            quote_volume=quote_volume, last_price=last_price,
        ))

    out.sort(key=lambda c: c.price_change_pct, reverse=True)
    return out


def confirm_momentum(klines: list[Kline], config: dict) -> tuple[bool, str]:
    """Cek apakah momentum jangka pendek (dari candle konfirmasi, mis. 5m)
    masih terlihat berlanjut naik, bukan sudah berbalik/topping.

    Dua syarat sederhana (sengaja dibuat mudah diaudit, bukan indikator
    canggih yang rawan overfit):
    1. Rata-rata 3 close terakhir > rata-rata 3 close sebelumnya (momentum
       jangka pendek masih positif).
    2. Candle terakhir tidak "reversal kuat" -- posisi close tidak berada
       di 35% terbawah dari rentang high-low candle itu (kalau di situ,
       kemungkinan sedang dijual/topping)."""
    min_bars = 8
    if len(klines) < min_bars:
        return False, "data candle konfirmasi tidak cukup"

    closes = [k.close for k in klines]
    recent_avg = mean(closes[-3:])
    prior_avg = mean(closes[-6:-3])
    momentum_ok = recent_avg > prior_avg

    last = klines[-1]
    candle_range = last.high - last.low
    if candle_range <= 0:
        reversal_ok = True
    else:
        close_position = (last.close - last.low) / candle_range
        reversal_ok = close_position >= config.get("MIN_CLOSE_POSITION_IN_RANGE", 0.35)

    if not momentum_ok:
        return False, "momentum jangka pendek melemah (rata-rata close menurun)"
    if not reversal_ok:
        return False, "candle terakhir terlihat seperti reversal/topping"
    return True, "momentum masih naik"


def compute_vwap(klines: list[Kline]) -> Optional[float]:
    """VWAP (Volume Weighted Average Price) BERGULIR dari sejumlah candle
    terakhir yang diberikan -- BUKAN VWAP sesi/harian seperti di bursa saham
    (yang reset tiap buka pasar). Binance Spot buka 24/7 tanpa sesi, jadi
    VWAP bergulir dari window candle konfirmasi (CONFIRM_LOOKBACK_BARS) yang
    dipakai di sini, sesuai praktik umum VWAP untuk pasar tanpa jam reset.

    VWAP = total(quote_volume) / total(volume) pada window klines yang
    diberikan -- ini setara dengan rata-rata harga tertimbang volume,
    memakai quote_volume (dalam USDT) dan volume (dalam koin) yang SUDAH
    tersedia di setiap Kline hasil parse_klines(), tanpa perlu field baru.

    Mengembalikan None kalau tidak ada volume sama sekali di window
    (data tidak valid untuk dihitung)."""
    total_volume = sum(k.volume for k in klines)
    if total_volume <= 0:
        return None
    total_quote = sum(k.quote_volume for k in klines)
    return total_quote / total_volume


def check_vwap_extension(klines: list[Kline], config: dict) -> tuple[bool, str]:
    """Filter tambahan (opsional, USE_VWAP_FILTER): tolak kandidat kalau
    harga saat ini terlalu jauh menyimpang dari VWAP bergulir jangka pendek
    (window sama dengan CONFIRM_LOOKBACK_BARS, konsisten dengan
    confirm_momentum() -- mengukur leg pump yang SEDANG terjadi, bukan
    tercampur histori sebelum pump seperti VWAP 24 jam).

    Dua kondisi yang membuat kandidat DITOLAK:
    1. Harga masih di BAWAH VWAP (tekanan beli di leg ini belum benar-benar
       dominan, VWAP dihitung dari transaksi RIIL jadi ini sinyal netral/
       lemah, bukan pump yang solid).
    2. Harga sudah lebih dari VWAP_MAX_EXTENSION_PCT persen DI ATAS VWAP
       (kandidat sudah terlalu "kepanasan"/ekstrem dibanding rata-rata
       transaksi baru-baru ini -- risiko besar membeli di puncak lokal yang
       segera terkoreksi)."""
    if not config.get("USE_VWAP_FILTER", False):
        return True, "filter VWAP nonaktif"

    min_bars = 8
    if len(klines) < min_bars:
        return False, "data candle VWAP tidak cukup"

    vwap = compute_vwap(klines)
    if vwap is None or vwap <= 0:
        return False, "VWAP tidak bisa dihitung (data volume kosong/tidak valid)"

    last_price = klines[-1].close
    extension_pct = (last_price / vwap - 1.0) * 100.0
    max_ext = config.get("VWAP_MAX_EXTENSION_PCT", 5.0)

    if extension_pct < 0:
        return False, f"harga masih {abs(extension_pct):.2f}% di bawah VWAP (tekanan beli belum dominan)"
    if extension_pct > max_ext:
        return False, f"harga sudah {extension_pct:.2f}% di atas VWAP (melebihi batas {max_ext:.2f}%, terlalu ekstrem/kepanasan)"
    return True, f"harga {extension_pct:.2f}% di atas VWAP, masih wajar (batas {max_ext:.2f}%)"


def find_best_candidate(tickers: list, klines_fetcher, config: dict) -> Optional[Candidate]:
    """klines_fetcher: fungsi(symbol) -> list[Kline] (dipisah supaya fungsi
    ini tetap murni/testable tanpa perlu klien jaringan sungguhan)."""
    ranked = filter_and_rank_candidates(tickers, config)
    top_n = ranked[: config["TOP_N_CANDIDATES_TO_CONFIRM"]]

    for cand in top_n:
        klines = klines_fetcher(cand.symbol)
        ok, reason = confirm_momentum(klines, config)
        if ok:
            vwap_ok, vwap_reason = check_vwap_extension(klines, config)
            if not vwap_ok:
                cand.confirmed = False
                cand.confirm_reason = f"lolos momentum tapi ditolak filter VWAP: {vwap_reason}"
                continue
            reason = f"{reason}; VWAP: {vwap_reason}"
            ok = vwap_ok
        cand.confirmed = ok
        cand.confirm_reason = reason
        if ok:
            return cand
    return None



def is_symbol_still_ranked(symbol: str, tickers: list, config: dict, rank_threshold: int) -> bool:
    """Dipakai untuk exit dini: kalau koin yang sedang dipegang sudah jatuh
    keluar dari top-N gainer (rank_threshold), anggap momentumnya sudah
    pudar."""
    ranked = filter_and_rank_candidates(tickers, config)
    for i, c in enumerate(ranked[:rank_threshold]):
        if c.symbol == symbol:
            return True
    return False
