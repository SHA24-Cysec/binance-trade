"""Seleksi kandidat pump dan konfirmasi entry untuk Pump Scanner.

Modul ini tidak memprediksi pump. Ia menyaring koin yang SUDAH naik dan
dengan volume tinggi dalam 24 jam, lalu menunggu momentum jangka pendek pada
candle 5 menit yang SUDAH selesai.

Semua fungsi konfirmasi menerima candle dalam urutan kronologis dan menganggap
candle terakhir sudah selesai. Caller live bertanggung jawab membuang candle
yang masih berjalan agar keputusan live dan backtest konsisten.
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
    # Ditambahkan 2026-09-24 setelah pemeriksaan ticker 24 jam Binance Spot:
    # USD1USDT dan RLUSDUSDT aktif diperdagangkan dengan volume besar
    # (219 juta dan 88 juta USDT) namun perubahan 24 jamnya persis 0,00%,
    # karena keduanya stablecoin yang dipatok ke USD. Pair seperti ini
    # tidak akan pernah lolos MIN_PUMP_PCT_24H, jadi membuangnya lebih awal
    # hanya menghemat pekerjaan; ini bukan perubahan perilaku trading.
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


def _looks_leveraged(base_asset: str) -> bool:
    """Deteksi leveraged token Binance lama (BTCUP, ETHDOWN, dsb).

    Binance sudah menghapus SEMUA leveraged token per 3 April 2024, jadi
    fungsi ini kini murni pagar pengaman. Syarat awalan minimal 2 huruf
    mencegah koin SAH seperti JUP (awalan 'J' hanya 1 huruf) ikut tertolak
    -- sebelum perbaikan audit 2026-09-24, JUPUSDT salah ditolak oleh
    heuristik ini.
    """
    for sfx in LEVERAGED_TOKEN_SUFFIXES:
        if base_asset.endswith(sfx) and len(base_asset) - len(sfx) >= 2:
            return True
    return False


def filter_and_rank_candidates(tickers: list, config: dict) -> list[Candidate]:
    """Filter ticker 24 jam lalu urutkan kandidat dari kenaikan tertinggi.

    ``tickers`` adalah hasil mentah GET ticker/24hr untuk seluruh pair.
    Fungsi ini hanya melakukan seleksi pasar. Konfirmasi bentuk candle entry
    dilakukan terpisah oleh ``confirm_entry``.
    """
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
    """Cek momentum pendek dan bentuk candle terakhir.

    Dua syarat yang sengaja sederhana supaya mudah diaudit:
    1. Rata-rata tiga close terakhir di atas rata-rata tiga close sebelumnya.
    2. Close candle terakhir tidak berada di bagian bawah range candle.
    """
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
        reversal_ok = close_position >= float(config.get("MIN_CLOSE_POSITION_IN_RANGE", 0.35))

    if not momentum_ok:
        return False, "momentum jangka pendek melemah (rata-rata close menurun)"
    if not reversal_ok:
        return False, "candle terakhir terlihat seperti reversal/topping"
    return True, "momentum pendek masih naik"


def confirm_entry(klines: list[Kline], config: dict) -> tuple[bool, str]:
    """Jalankan konfirmasi entry dengan alasan yang dapat dicatat ke log."""
    return confirm_momentum(klines, config)


def find_best_candidate(tickers: list, klines_fetcher, config: dict) -> Optional[Candidate]:
    """Kembalikan kandidat ranking tertinggi yang lolos konfirmasi entry.

    ``klines_fetcher`` sengaja diinjeksikan agar fungsi tetap murni dan mudah
    diuji tanpa jaringan. Ia wajib mengembalikan candle tertutup saja.
    """
    ranked = filter_and_rank_candidates(tickers, config)
    top_n = ranked[: config["TOP_N_CANDIDATES_TO_CONFIRM"]]

    for cand in top_n:
        klines = klines_fetcher(cand.symbol)
        ok, reason = confirm_entry(klines, config)
        cand.confirmed = ok
        cand.confirm_reason = reason
        if ok:
            return cand
    return None


def is_symbol_still_ranked(symbol: str, tickers: list, config: dict, rank_threshold: int) -> bool:
    """Dipakai untuk exit dini kalau simbol keluar dari top-N gainer."""
    ranked = filter_and_rank_candidates(tickers, config)
    for i, candidate in enumerate(ranked[:rank_threshold]):
        if candidate.symbol == symbol:
            return True
    return False
