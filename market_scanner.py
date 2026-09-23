"""Seleksi kandidat pump dan konfirmasi entry untuk Pump Scanner.

Modul ini tidak memprediksi pump. Ia menyaring koin yang SUDAH naik dan
bervolume tinggi dalam 24 jam, lalu menunggu pola entry pada candle 5 menit
yang SUDAH selesai. Dua mode tersedia:

- ``LEGACY_MOMENTUM``: momentum pendek positif + filter VWAP lama.
- ``VWAP_RETEST_RVOL``: setelah pump, tunggu retest VWAP lalu candle bullish
  yang merebut kembali VWAP dengan volume relatif tinggi. Ini adalah hipotesis
  entry baru untuk diuji, bukan parameter yang sudah terbukti atau siap live.

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
}

LEVERAGED_TOKEN_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")

ENTRY_MODEL_LEGACY = "LEGACY_MOMENTUM"
ENTRY_MODEL_VWAP_RETEST_RVOL = "VWAP_RETEST_RVOL"
VALID_ENTRY_MODELS = (ENTRY_MODEL_LEGACY, ENTRY_MODEL_VWAP_RETEST_RVOL)


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


def _entry_model(config: dict) -> str:
    """Normalisasi mode tanpa pernah mengubah default secara diam-diam.

    Nilai yang tidak dikenal sengaja dikembalikan apa adanya. ``confirm_entry``
    lalu menolaknya, bukan diam-diam kembali ke strategi lama. Ini penting
    supaya salah ketik config tidak mengaktifkan entry yang tidak dimaksud.
    """
    return str(config.get("ENTRY_MODEL", ENTRY_MODEL_LEGACY)).strip().upper()


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

    Ini dipakai oleh mode lama dan tetap menjadi syarat tambahan pada mode
    retest, supaya retest yang terjadi dalam momentum masih menurun tidak
    dianggap sebagai sinyal beli.
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


def compute_vwap(klines: list[Kline]) -> Optional[float]:
    """Hitung VWAP bergulir dari candle yang diberikan.

    Binance Spot buka 24/7, sehingga yang dipakai adalah window bergulir
    ``CONFIRM_LOOKBACK_BARS`` dan bukan VWAP sesi saham. Volume quote dan base
    sudah tersedia pada ``Kline`` hasil parser Binance.
    """
    total_volume = sum(k.volume for k in klines)
    if total_volume <= 0:
        return None
    total_quote = sum(k.quote_volume for k in klines)
    return total_quote / total_volume


def check_vwap_extension(klines: list[Kline], config: dict) -> tuple[bool, str]:
    """Tolak harga di bawah VWAP atau terlalu jauh di atas VWAP.

    Saat filter tidak aktif fungsi ini selalu lolos agar mode lama tetap bisa
    direproduksi. Mode retest pada praktiknya membutuhkan VWAP, jadi
    ``confirm_vwap_retest_rvol`` menolak konfigurasi jika filter ini dimatikan.
    """
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
    max_ext = float(config.get("VWAP_MAX_EXTENSION_PCT", 5.0))

    if extension_pct < 0:
        return False, f"harga masih {abs(extension_pct):.2f}% di bawah VWAP"
    if extension_pct > max_ext:
        return False, (
            f"harga sudah {extension_pct:.2f}% di atas VWAP "
            f"(melewati batas {max_ext:.2f}%, terlalu ekstrem)"
        )
    return True, f"harga {extension_pct:.2f}% di atas VWAP, masih dalam batas"


def confirm_vwap_retest_rvol(klines: list[Kline], config: dict) -> tuple[bool, str]:
    """Konfirmasi entry retest VWAP dengan volume relatif, tanpa look-ahead.

    Syaratnya seluruhnya memakai candle yang sudah tersedia saat sinyal:
    1. Momentum pendek tetap positif dan harga berada di atas VWAP bergulir,
       tetapi tidak melebihi batas ekstensi.
    2. Setidaknya satu candle *sebelum candle sinyal* menyentuh area VWAP
       dalam ``VWAP_RETEST_LOOKBACK_BARS`` candle terakhir. Low retest tidak
       boleh breakdown terlalu jauh di bawah VWAP.
    3. Candle sinyal adalah bullish, menutup cukup dekat ke high, menutup di
       atas VWAP dengan reclaim minimum, dan melampaui close sebelumnya.
    4. Quote volume candle sinyal >= rata-rata quote volume sejumlah candle
       sebelumnya x ``MIN_RELATIVE_QUOTE_VOLUME``.

    Parameter awal ini adalah hipotesis uji. Tidak ada nilai di sini yang
    diklaim optimum maupun layak dipakai live sebelum validasi out-of-sample.
    """
    if not config.get("USE_VWAP_FILTER", False):
        return False, "mode VWAP_RETEST_RVOL membutuhkan USE_VWAP_FILTER=True"

    try:
        retest_bars = int(config.get("VWAP_RETEST_LOOKBACK_BARS", 3))
        rvol_bars = int(config.get("RVOL_LOOKBACK_BARS", 10))
    except (TypeError, ValueError):
        return False, "parameter lookback retest/RVOL tidak valid"
    if retest_bars < 1 or rvol_bars < 1:
        return False, "lookback retest dan RVOL harus minimal 1 candle"

    min_bars = max(8, retest_bars + 1, rvol_bars + 1)
    if len(klines) < min_bars:
        return False, f"data candle retest/RVOL tidak cukup (butuh minimal {min_bars})"

    momentum_ok, momentum_reason = confirm_momentum(klines, config)
    if not momentum_ok:
        return False, momentum_reason

    vwap_ok, vwap_reason = check_vwap_extension(klines, config)
    if not vwap_ok:
        return False, f"ditolak VWAP: {vwap_reason}"

    vwap = compute_vwap(klines)
    if vwap is None or vwap <= 0:
        return False, "VWAP tidak bisa dihitung"

    try:
        touch_tol = abs(float(config.get("VWAP_RETEST_TOUCH_TOLERANCE_PCT", 0.20)))
        max_breakdown = abs(float(config.get("VWAP_RETEST_MAX_BREAKDOWN_PCT", 0.75)))
        min_reclaim = abs(float(config.get("VWAP_RETEST_MIN_RECLAIM_PCT", 0.10)))
        signal_close_position_min = float(config.get("VWAP_RETEST_SIGNAL_MIN_CLOSE_POSITION", 0.60))
        min_rvol = float(config.get("MIN_RELATIVE_QUOTE_VOLUME", 1.50))
    except (TypeError, ValueError):
        return False, "parameter retest/RVOL tidak valid"

    if not (0.0 <= signal_close_position_min <= 1.0):
        return False, "VWAP_RETEST_SIGNAL_MIN_CLOSE_POSITION harus di antara 0 dan 1"
    if min_rvol <= 0:
        return False, "MIN_RELATIVE_QUOTE_VOLUME harus lebih dari 0"

    # Candle retest harus mendahului candle sinyal. Ini mencegah bot menyebut
    # candle pembelian yang sama sebagai "retest" dan kemudian membeli saat
    # candle tersebut belum memberi bukti reclaim.
    prior_retest_bars = klines[-(retest_bars + 1):-1]
    touch_ceiling = vwap * (1.0 + touch_tol / 100.0)
    breakdown_floor = vwap * (1.0 - max_breakdown / 100.0)
    touched = [
        k for k in prior_retest_bars
        if breakdown_floor <= k.low <= touch_ceiling
    ]
    if not touched:
        return False, (
            f"belum ada retest VWAP dalam {retest_bars} candle sebelumnya "
            f"(area {breakdown_floor:.8g} s.d. {touch_ceiling:.8g})"
        )

    signal = klines[-1]
    previous = klines[-2]
    signal_range = signal.high - signal.low
    signal_close_position = ((signal.close - signal.low) / signal_range) if signal_range > 0 else 1.0
    reclaim_level = vwap * (1.0 + min_reclaim / 100.0)

    if signal.close <= signal.open:
        return False, "candle sinyal tidak bullish"
    if signal.close <= previous.close:
        return False, "candle sinyal belum menembus close candle sebelumnya"
    if signal.close < reclaim_level:
        return False, f"close sinyal belum reclaim VWAP +{min_reclaim:.2f}%"
    if signal_close_position < signal_close_position_min:
        return False, (
            f"close candle sinyal hanya di {signal_close_position:.2f} range, "
            f"di bawah minimum {signal_close_position_min:.2f}"
        )

    prior_volumes = [k.quote_volume for k in klines[-(rvol_bars + 1):-1]]
    avg_prior_volume = mean(prior_volumes) if prior_volumes else 0.0
    if avg_prior_volume <= 0:
        return False, "rata-rata quote volume pembanding tidak valid"
    if signal.quote_volume <= 0:
        return False, "quote volume candle sinyal tidak valid"
    rvol = signal.quote_volume / avg_prior_volume
    if rvol < min_rvol:
        return False, f"RVOL {rvol:.2f}x di bawah minimum {min_rvol:.2f}x"

    extension_pct = (signal.close / vwap - 1.0) * 100.0
    return True, (
        f"retest VWAP terkonfirmasi; {momentum_reason}; close {extension_pct:.2f}% "
        f"di atas VWAP; RVOL {rvol:.2f}x"
    )


def confirm_entry(klines: list[Kline], config: dict) -> tuple[bool, str]:
    """Jalankan satu mode entry dengan alasan yang dapat dicatat ke log."""
    mode = _entry_model(config)
    if mode == ENTRY_MODEL_LEGACY:
        ok, reason = confirm_momentum(klines, config)
        if not ok:
            return False, reason
        vwap_ok, vwap_reason = check_vwap_extension(klines, config)
        if not vwap_ok:
            return False, f"lolos momentum tapi ditolak VWAP: {vwap_reason}"
        return True, f"mode legacy: {reason}; VWAP: {vwap_reason}"
    if mode == ENTRY_MODEL_VWAP_RETEST_RVOL:
        return confirm_vwap_retest_rvol(klines, config)
    return False, (
        f"ENTRY_MODEL '{mode}' tidak dikenal. Pilih salah satu: "
        f"{', '.join(VALID_ENTRY_MODELS)}"
    )


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
