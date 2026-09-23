"""
Struktur data candle (Kline) dan parser hasil endpoint klines Binance.

Semua array di sini memakai urutan KRONOLOGIS (index 0 = candle paling lama,
index -1 = candle paling baru), sesuai perilaku default endpoint
GET /api/v3/klines.

Catatan: modul ini dulu juga berisi indikator SuperTrend + EMA + ATR untuk bot
grid martingale. Bot grid sudah dihapus, jadi modul ini kini hanya menyisakan
struktur candle dan parser yang masih dipakai oleh pump scanner.
"""

from __future__ import annotations

from typing import NamedTuple


class Kline(NamedTuple):
    open_time: int
    open: float
    high: float
    low: float
    close: float
    close_time: int
    # Dua field di bawah ditambahkan untuk kebutuhan fitur backtest (menghitung
    # volume 24 jam bergulir tanpa perlu memanggil ulang ticker/24hr per-bar).
    # Diberi nilai default 0.0 supaya SEMUA pemanggilan lama (mis. selftest di
    # pump_scanner_bot.py yang membuat Kline manual tanpa argumen ini) tetap
    # berjalan tanpa perlu diubah.
    volume: float = 0.0
    quote_volume: float = 0.0


def parse_klines(raw: list) -> list[Kline]:
    """raw = hasil GET /api/v3/klines (list of list), urutan sudah kronologis
    (paling lama -> paling baru) sesuai perilaku default endpoint tsb.

    Indeks array mentah (sesuai dokumentasi resmi Binance):
    0=openTime 1=open 2=high 3=low 4=close 5=volume 6=closeTime
    7=quoteAssetVolume ..."""
    out = []
    for row in raw:
        out.append(
            Kline(
                open_time=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                close_time=int(row[6]),
                volume=float(row[5]) if len(row) > 5 else 0.0,
                quote_volume=float(row[7]) if len(row) > 7 else 0.0,
            )
        )
    return out


# ---------------------------------------------------------------------
# ATR (Average True Range) -- Wilder, 1978
# ---------------------------------------------------------------------
def true_ranges(klines: "list[Kline]") -> "list[float]":
    """True Range per candle, definisi asli J. Welles Wilder (1978):

        TR = max(high - low,
                 |high - close_sebelumnya|,
                 |low  - close_sebelumnya|)

    Dua suku terakhir yang membuat TR berbeda dari sekadar (high - low):
    keduanya menangkap GAP antar candle. Di crypto spot yang buka 24 jam gap
    memang jarang, tapi pada koin yang sedang pump, lompatan harga antar
    candle 5 menit itu justru sering terjadi -- dan itulah volatilitas yang
    paling penting untuk diukur di bot ini.

    Candle pertama tidak punya close sebelumnya, jadi TR-nya = high - low.
    Return list dengan panjang SAMA dengan input.
    """
    out = []
    prev_close = None
    for k in klines:
        if prev_close is None:
            tr = k.high - k.low
        else:
            tr = max(k.high - k.low, abs(k.high - prev_close), abs(k.low - prev_close))
        out.append(float(tr))
        prev_close = k.close
    return out


def atr(klines: "list[Kline]", period: int = 14) -> "float | None":
    """ATR memakai Wilder's smoothing (RMA), BUKAN rata-rata sederhana.

    Kenapa penting dibedakan: Wilder's smoothing setara EMA dengan
    alpha = 1/period, sehingga lebih lambat bereaksi daripada SMA dengan
    period yang sama. Nilai yang dihasilkan berbeda nyata, dan hampir semua
    referensi/backtest ATR (termasuk platform charting) memakai versi Wilder.
    Kalau di sini dipakai SMA, hasil bot tidak akan cocok dengan angka ATR
    yang Anda lihat di TradingView maupun dengan literatur multiplier
    (1.5x/2x/2.5x) yang jadi dasar kalibrasi.

    Rumusnya:
        ATR_pertama = rata-rata TR dari `period` candle pertama
        ATR_t       = (ATR_sebelumnya * (period - 1) + TR_t) / period

    Return None kalau data candle kurang dari `period + 1` (butuh satu candle
    ekstra karena TR candle pertama tidak memakai close sebelumnya, jadi
    kurang akurat dan sebaiknya tidak ikut dihitung).
    """
    if period < 1:
        return None
    if len(klines) < period + 1:
        return None

    trs = true_ranges(klines)[1:]  # buang TR candle pertama (tanpa prev_close)
    if len(trs) < period:
        return None

    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return float(value)


def atr_percent(klines: "list[Kline]", period: int = 14,
                 reference_price: "float | None" = None) -> "float | None":
    """ATR dinyatakan sebagai PERSEN dari harga acuan.

    Bot ini bekerja dalam satuan persen di mana-mana (SL_PCT, TP_PCT, dst) dan
    memperdagangkan koin dengan rentang harga sangat lebar (dari 0.000001 USDT
    sampai ribuan USDT). ATR absolut tidak bisa dibandingkan antar koin, ATR
    persen bisa.

    reference_price default = close candle terakhir.
    """
    value = atr(klines, period)
    if value is None:
        return None
    price = reference_price if reference_price is not None else (klines[-1].close if klines else 0.0)
    if not price or price <= 0:
        return None
    return value / price * 100.0


def resolve_exit_levels(config: dict, klines: "list[Kline] | None" = None,
                         entry_price: "float | None" = None) -> dict:
    """Tentukan SL% dan TP% untuk SATU posisi, mode tetap maupun ATR.

    Fungsi ini sengaja dipakai BERSAMA oleh bot live (pump_scanner_bot.py) dan
    backtest (backtest.py). Kalau keduanya menghitung level exit sendiri-sendiri,
    hasil backtest bisa diam-diam tidak mewakili perilaku bot sungguhan -- dan
    itu jenis bug yang paling mahal, karena baru ketahuan setelah rugi uang.

    Return dict:
        sl_pct           : float, jarak stop loss dalam persen (selalu positif)
        tp_pct           : float, jarak take profit dalam persen
        be_trigger_pct   : float, profit yang memicu Breakeven
        be_lock_pct      : float, profit yang dikunci saat Breakeven aktif
        trail_start_pct  : float, profit yang mengaktifkan Trailing
        trail_step_pct   : float, jarak Trailing di bawah harga tertinggi
        atr_pct          : float | None, ATR sebagai persen harga
        source           : "FIXED" | "ATR" | "ATR_FALLBACK_FIXED"
        note             : penjelasan singkat untuk log

    SEMUA level exit dihitung di satu tempat ini, termasuk Breakeven dan
    Trailing. Kalau hanya SL/TP yang mengikuti ATR sementara BE/Trailing
    memakai angka tetap, hasilnya pincang: trailing yang jauh lebih sempit
    dari ATR akan menutup posisi sebelum TP sempat tercapai, sehingga
    risk:reward jadi terbalik.

    Perilaku FALLBACK yang disengaja: kalau USE_ATR_EXITS=True tapi ATR tidak
    bisa dihitung (candle kurang dari ATR_PERIOD+1, atau harga nol), fungsi ini
    TIDAK melempar error dan TIDAK membiarkan posisi tanpa stop. Ia jatuh
    kembali ke SL_PCT/TP_PCT tetap dan menandainya lewat source, supaya
    kegagalan indikator tidak pernah berubah jadi posisi tanpa proteksi.
    """
    fixed_sl = abs(float(config.get("SL_PCT", 1.8)))
    fixed_tp = abs(float(config.get("TP_PCT", 4.0)))
    fixed_levels = {
        "sl_pct": fixed_sl,
        "tp_pct": fixed_tp,
        "be_trigger_pct": abs(float(config.get("BE_TRIGGER_PCT", 1.0))),
        "be_lock_pct": abs(float(config.get("BE_LOCK_PCT", 0.15))),
        "trail_start_pct": abs(float(config.get("TRAILING_START_PCT", 1.5))),
        "trail_step_pct": abs(float(config.get("TRAILING_STEP_PCT", 0.6))),
    }

    if not config.get("USE_ATR_EXITS", False):
        return {**fixed_levels, "atr_pct": None,
                "source": "FIXED", "note": "SL/TP/BE/Trailing tetap dari config"}

    period = int(config.get("ATR_PERIOD", 14))
    a_pct = atr_percent(klines or [], period, reference_price=entry_price)

    if a_pct is None or a_pct <= 0:
        return {**fixed_levels, "atr_pct": None,
                "source": "ATR_FALLBACK_FIXED",
                "note": (f"ATR tidak bisa dihitung (butuh minimal {period + 1} candle, "
                          f"tersedia {len(klines or [])}); pakai SL/TP tetap")}

    mult = float(config.get("ATR_MULTIPLIER_SL", 2.0))
    lo = abs(float(config.get("ATR_SL_MIN_PCT", 1.2)))
    hi = abs(float(config.get("ATR_SL_MAX_PCT", 4.0)))
    if lo > hi:
        # Config salah isi -- jangan diam-diam menghasilkan rentang mustahil.
        lo, hi = hi, lo

    raw_sl = a_pct * mult
    sl_pct = min(max(raw_sl, lo), hi)
    rr = abs(float(config.get("ATR_TP_RR_RATIO", 2.0)))
    tp_pct = sl_pct * rr

    if raw_sl < lo:
        clamp = f" (dibatasi lantai {lo:.2f}%)"
    elif raw_sl > hi:
        clamp = f" (dibatasi plafon {hi:.2f}%)"
    else:
        clamp = ""

    # Breakeven & Trailing memakai ATR sebagai satuan juga, supaya seluruh
    # komponen exit berada pada skala yang sama.
    be_trigger = a_pct * abs(float(config.get("ATR_BE_TRIGGER_MULT", 0.5)))
    be_lock = a_pct * abs(float(config.get("ATR_BE_LOCK_MULT", 0.1)))
    trail_start = a_pct * abs(float(config.get("ATR_TRAILING_START_MULT", 1.0)))
    trail_step = a_pct * abs(float(config.get("ATR_TRAILING_STEP_MULT", 1.5)))

    # Trailing tidak boleh lebih longgar dari Stop Loss. Kalau itu terjadi,
    # trailing tidak pernah punya kesempatan bekerja karena SL selalu kena
    # lebih dulu, jadi fiturnya cuma ilusi.
    trail_step = min(trail_step, sl_pct)

    # Breakeven harus terpicu sebelum Trailing, kalau tidak urutannya kacau.
    be_trigger = min(be_trigger, trail_start)

    return {
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "be_trigger_pct": be_trigger,
        "be_lock_pct": be_lock,
        "trail_start_pct": trail_start,
        "trail_step_pct": trail_step,
        "atr_pct": a_pct,
        "source": "ATR",
        "note": (f"ATR({period})={a_pct:.2f}% x {mult:g} = {raw_sl:.2f}% -> "
                  f"SL {sl_pct:.2f}%{clamp}, TP {tp_pct:.2f}% (RR {rr:g}:1), "
                  f"BE@{be_trigger:.2f}% kunci {be_lock:.2f}%, "
                  f"Trail@{trail_start:.2f}% jarak {trail_step:.2f}%"),
    }
