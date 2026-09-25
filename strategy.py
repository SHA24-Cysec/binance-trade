"""
Struktur data candle (Kline), parser klines Binance, ATR, dan anchored VWAP.

Semua array di sini memakai urutan KRONOLOGIS (index 0 = candle paling lama,
index -1 = candle paling baru), sesuai perilaku default endpoint
GET /api/v3/klines.

Referensi endpoint yang dipakai modul ini:
  https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market#klines
  (dicek 2026-09-25). Respons klines berupa tuple 12 field dengan indeks
  0=openTime, 1=open, 2=high, 3=low, 4=close, 5=volume, 6=closeTime,
  7=quoteAssetVolume. Bobot request 2 per panggilan, limit maksimum 1000
  candle per panggilan.

Catatan: modul ini dulu juga berisi indikator SuperTrend + EMA + ATR untuk bot
grid martingale lama (sudah dihapus). Sekarang isinya struktur candle, parser,
ATR Wilder, anchored VWAP, dan resolusi level exit yang dipakai bersama oleh
jalur live, backtest, dan watchlist.
"""

from __future__ import annotations

import math
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


# Panjang interval candle dalam menit. Dipakai bersama oleh bot live,
# backtest satu simbol, dan backtest portofolio supaya tidak ada dua tabel
# yang bisa berbeda diam-diam. Nilai enum interval mengikuti dokumentasi
# endpoint klines Binance (dicek 2026-09-25, lihat docstring modul).
INTERVAL_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480, "12h": 720,
    "1d": 1440,
}


def interval_to_ms(interval: str, default_minutes: int = 5) -> int:
    """Panjang satu candle dalam milidetik, jatuh ke default bila tidak dikenal."""
    return int(INTERVAL_MINUTES.get(str(interval), default_minutes)) * 60_000


def parse_klines(raw: list) -> list[Kline]:
    """raw = hasil GET /api/v3/klines (list of list), urutan sudah kronologis
    (paling lama -> paling baru) sesuai perilaku default endpoint tsb.

    Indeks array mentah (sesuai dokumentasi resmi Binance):
    0=openTime 1=open 2=high 3=low 4=close 5=volume 6=closeTime
    7=quoteAssetVolume ..."""
    out = []
    for idx, row in enumerate(raw):
        o = float(row[1])
        h = float(row[2])
        low_ = float(row[3])
        c = float(row[4])

        # Penjagaan data rusak. Ini BUKAN paranoia berlebihan: bursa
        # sesekali mengirim nilai aneh saat maintenance atau saat simbol
        # baru didelisting, dan akibatnya senyap tapi fatal.
        #
        # NaN adalah yang paling berbahaya. Setiap perbandingan dengan NaN
        # menghasilkan False, termasuk "pnl_low <= -SL_PCT". Artinya satu
        # candle NaN saja membuat Stop Loss BERHENTI BEKERJA tanpa satu pun
        # pesan error, dan posisi ditahan terus sampai data habis. Lebih
        # baik gagal keras di sini daripada diam-diam kehilangan uang.
        for nama, nilai in (("open", o), ("high", h), ("low", low_), ("close", c)):
            if nilai != nilai:      # True hanya untuk NaN
                raise ValueError(
                    f"Candle ke-{idx} punya {nama}=NaN. Data bursa rusak. "
                    "Dihentikan karena NaN membuat semua cek Stop Loss/Take "
                    "Profit diam-diam gagal."
                )
            if nilai in (float("inf"), float("-inf")):
                raise ValueError(f"Candle ke-{idx} punya {nama} tak hingga. Data bursa rusak.")
            if nilai <= 0:
                raise ValueError(
                    f"Candle ke-{idx} punya {nama}={nilai}. Harga wajib > 0. "
                    "Data bursa rusak atau simbol sudah tidak diperdagangkan."
                )

        # high/low yang terbalik akan mengacaukan deteksi sentuhan SL/TP.
        if h < low_:
            raise ValueError(f"Candle ke-{idx}: high ({h}) lebih kecil dari low ({low_}).")

        out.append(
            Kline(
                open_time=int(row[0]),
                open=o,
                high=h,
                low=low_,
                close=c,
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


# ---------------------------------------------------------------------
# Anchored VWAP
# ---------------------------------------------------------------------
def anchored_vwap(klines: "list[Kline]", anchor_index: int) -> "float | None":
    """VWAP yang dijangkar pada satu candle tertentu (candle anchor IKUT dihitung).

    Rumusnya sama dengan VWAP bergulir yang dulu ada di repo ini, hanya titik
    mulainya berbeda:

        anchored_vwap = sum(quote_volume[anchor..terakhir]) / sum(volume[anchor..terakhir])

    ``quote_volume`` diambil dari indeks 7 respons klines Binance dan
    ``volume`` dari indeks 5 (lihat docstring modul, dicek 2026-09-25), jadi
    pembagian ini benar-benar harga rata-rata tertimbang volume, bukan
    rata-rata harga biasa.

    Kembalikan None, bukan melempar error atau memberi angka palsu, kalau:
      - daftar candle kosong atau anchor_index di luar rentang,
      - total volume nol (sering terjadi pada data sintetis lama yang memakai
        nilai default Kline.volume = 0.0),
      - total quote_volume nol atau salah satu penjumlahan menghasilkan
        nilai tidak finite (NaN/inf).

    Pemanggil WAJIB memperlakukan None sebagai "setup ditolak", bukan sebagai
    nol. VWAP nol akan membuat setiap perbandingan harga lolos secara diam-diam.
    """
    if not klines:
        return None
    n = len(klines)
    if anchor_index < 0 or anchor_index >= n:
        return None

    vol_sum = 0.0
    quote_sum = 0.0
    for k in klines[anchor_index:]:
        vol_sum += float(k.volume)
        quote_sum += float(k.quote_volume)

    if not math.isfinite(vol_sum) or not math.isfinite(quote_sum):
        return None
    if vol_sum <= 0 or quote_sum <= 0:
        return None
    value = quote_sum / vol_sum
    if not math.isfinite(value) or value <= 0:
        return None
    return value


# ---------------------------------------------------------------------
# Kebutuhan jumlah candle untuk satu keputusan entry
# ---------------------------------------------------------------------
def required_lookback_bars(config: dict) -> int:
    """Jumlah candle MINIMUM yang dibutuhkan satu evaluasi setup pullback retest.

    Dihitung di SATU tempat lalu dipakai bersama oleh bot live, backtest satu
    simbol, backtest portofolio, penilaian watchlist, dan validasi
    settings_schema. Sebelumnya angka minimum ini tersebar (ada yang
    hard-code 8 di portfolio_backtest.py), dan itu jenis duplikasi yang
    membuat satu jalur diam-diam berbeda dari jalur lain.

    Dua kebutuhan yang digabung:
      1. ATR Wilder butuh ATR_PERIOD + 1 candle (TR candle pertama dibuang).
      2. Struktur setup butuh ruang untuk swing lookback, sayap pivot kiri dan
         kanan, lalu jarak dari breakout sampai retest:
         SWING_LOOKBACK_BARS + 2 x SWING_PIVOT_WING_BARS + MAX_BARS_BREAKOUT_TO_RETEST.

    Nilai default parameter di config.py adalah TITIK AWAL yang masih harus
    divalidasi lewat backtest repo ini, bukan angka yang sudah terbukti.
    """
    atr_period = int(config.get("ATR_PERIOD", 14) or 14)
    swing_lookback = int(config.get("SWING_LOOKBACK_BARS", 12) or 12)
    wing = int(config.get("SWING_PIVOT_WING_BARS", 2) or 2)
    max_bars_to_retest = int(config.get("MAX_BARS_BREAKOUT_TO_RETEST", 12) or 12)

    need_atr = max(2, atr_period + 1)
    need_struktur = max(3, swing_lookback + 2 * wing + max_bars_to_retest)
    return max(need_atr, need_struktur)


def confirm_window_bars(config: dict) -> int:
    """Jumlah candle tertutup yang harus diambil untuk satu keputusan entry.

    Sama dengan CONFIRM_LOOKBACK_BARS, tetapi tidak pernah lebih kecil dari
    required_lookback_bars(). Ini yang dipakai pengambil klines di bot live,
    dashboard, dan watchlist supaya ketiganya melihat jendela yang identik
    walaupun config diisi terlalu kecil oleh pengguna.

    Batas atas 1000 mengikuti limit maksimum endpoint GET /api/v3/klines
    (dicek 2026-09-25, lihat docstring modul).
    """
    lookback = int(config.get("CONFIRM_LOOKBACK_BARS", 48) or 48)
    return max(1, min(1000, max(lookback, required_lookback_bars(config))))


def resolve_position_notional(config: dict, quote_free: float) -> dict:
    """Tentukan nominal entry dari saldo quote dengan policy yang dipakai live.

    Ini adalah sumber kebenaran tunggal untuk sizing. Bot live memakainya
    sebelum BUY, sedangkan kedua backtest memakainya untuk membangun equity
    curve. Urutannya sengaja eksplisit:

    1. mode persen memakai saldo free setelah BALANCE_BUFFER_PCT,
    2. mode fixed memakai POSITION_SIZE_USDT apa adanya,
    3. MAX_POSITION_USDT, bila positif, membatasi KEDUA mode.

    Return memuat rincian supaya caller tidak perlu menebak apakah plafon
    sedang menimpa parameter sizing yang dipilih user.
    """
    free = max(0.0, float(quote_free or 0.0))
    use_percent = bool(config.get("USE_RISK_PERCENT"))
    buffer_pct = max(0.0, float(config.get("BALANCE_BUFFER_PCT", 0.5) or 0.0))
    buffer_pct = min(buffer_pct, 100.0)

    if use_percent:
        spendable = free * (1.0 - buffer_pct / 100.0)
        requested = spendable * float(config.get("RISK_PERCENT", 25.0) or 0.0) / 100.0
        mode = "PERCENT"
    else:
        spendable = free
        requested = float(config.get("POSITION_SIZE_USDT", 5.0) or 0.0)
        mode = "FIXED"

    requested = max(0.0, requested)
    cap = float(config.get("MAX_POSITION_USDT", 0.0) or 0.0)
    cap_active = cap > 0.0 and requested > cap
    notional = min(requested, cap) if cap > 0.0 else requested
    effective_pct = (notional / free * 100.0) if free > 0 else 0.0

    return {
        "notional": notional,
        "requested_notional": requested,
        "mode": mode,
        "quote_free": free,
        "spendable_quote": spendable,
        "buffer_pct": buffer_pct if use_percent else 0.0,
        "cap": cap if cap > 0.0 else None,
        "cap_active": cap_active,
        "effective_pct_of_free": effective_pct,
    }


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
    fixed_be_trigger = abs(float(config.get("BE_TRIGGER_PCT", 1.0)))
    fixed_be_lock = abs(float(config.get("BE_LOCK_PCT", 0.15)))
    fixed_trail_start = abs(float(config.get("TRAILING_START_PCT", 1.5)))
    fixed_trail_step = abs(float(config.get("TRAILING_STEP_PCT", 0.6)))

    # Invariant ini berlaku untuk mode fixed MAUPUN ATR. Tanpanya BE dapat
    # mengunci harga yang belum pernah disentuh (lock > trigger), sedangkan
    # backtest candle bisa mencatat fill profit yang mustahil.
    fixed_trail_step = min(fixed_trail_step, fixed_sl)
    fixed_be_trigger = min(fixed_be_trigger, fixed_trail_start)
    fixed_be_lock = min(fixed_be_lock, fixed_be_trigger)
    fixed_levels = {
        "sl_pct": fixed_sl,
        "tp_pct": fixed_tp,
        "be_trigger_pct": fixed_be_trigger,
        "be_lock_pct": fixed_be_lock,
        "trail_start_pct": fixed_trail_start,
        "trail_step_pct": fixed_trail_step,
    }

    if not config.get("USE_ATR_EXITS", False):
        return {**fixed_levels, "atr_pct": None,
                "source": "FIXED", "note": "SL/TP/BE/Trailing tetap dari config (invariant BE/trailing diterapkan)"}

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

    # Profit yang dikunci saat BE tidak boleh melebihi profit yang memicunya.
    # Kalau lock > trigger, stop dipindah ke harga di atas harga yang baru
    # saja tersentuh, sehingga backtest bisa mencatat fill yang pada
    # kenyataannya mustahil, dan di mode live stop langsung tidak valid.
    be_lock = min(be_lock, be_trigger)

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
