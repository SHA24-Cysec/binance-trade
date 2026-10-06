from __future__ import annotations

import math
from typing import NamedTuple, Optional


class Kline(NamedTuple):
    open_time: int
    open: float
    high: float
    low: float
    close: float
    close_time: int
    volume: float = 0.0
    quote_volume: float = 0.0


INTERVAL_MINUTES = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "6h": 360,
    "8h": 480,
    "12h": 720,
    "1d": 1440,
}


def interval_to_ms(interval: str, default_minutes: int = 5) -> int:
    return int(INTERVAL_MINUTES.get(str(interval), default_minutes)) * 60_000


def parse_klines(raw: list) -> list[Kline]:
    out = []
    for idx, row in enumerate(raw):
        o = float(row[1])
        h = float(row[2])
        low_ = float(row[3])
        c = float(row[4])

        for nama, nilai in (("open", o), ("high", h), ("low", low_), ("close", c)):
            if nilai != nilai:
                raise ValueError(
                    f"Candle ke-{idx} punya {nama}=NaN. Data bursa rusak. "
                    "Dihentikan karena NaN membuat semua cek Stop Loss/Take "
                    "Profit diam-diam gagal."
                )
            if nilai in (float("inf"), float("-inf")):
                raise ValueError(
                    f"Candle ke-{idx} punya {nama} tak hingga. Data bursa rusak."
                )
            if nilai <= 0:
                raise ValueError(
                    f"Candle ke-{idx} punya {nama}={nilai}. Harga wajib > 0. "
                    "Data bursa rusak atau simbol sudah tidak diperdagangkan."
                )

        if h < low_:
            raise ValueError(
                f"Candle ke-{idx}: high ({h}) lebih kecil dari low ({low_})."
            )

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


def required_lookback_bars(config: dict) -> int:
    rolling_lookback = int(config.get("ROLLING_VOLUME_LOOKBACK_BARS", 20) or 20)
    confirmation_bars = int(config.get("ROLLING_VOLUME_CONFIRMATION_BARS", 1) or 1)
    demand_lookback = (
        int(config.get("DEMAND_LOOKBACK_BARS", 20) or 20) + 1
        if bool(config.get("DEMAND_ZONE_FILTER_ENABLED", False))
        else 0
    )
    atr_period = max(1, int(config.get("ATR_PERIOD", 14) or 14))
    return max(
        atr_period, rolling_lookback + max(1, confirmation_bars), demand_lookback
    )


def confirm_window_bars(config: dict) -> int:
    lookback = int(config.get("CONFIRM_LOOKBACK_BARS", 48) or 48)
    return max(1, min(1000, max(lookback, required_lookback_bars(config))))


def resolve_position_notional(config: dict, quote_free: float) -> dict:
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


def backtest_buy_execution_price(
    open_price: float, spread_pct: float = 0.0, slippage_pct: float = 0.0
) -> float:
    price = float(open_price)
    adverse = (
        max(0.0, float(spread_pct)) / 200.0 + max(0.0, float(slippage_pct)) / 100.0
    )
    return price * (1.0 + adverse)


def backtest_sell_execution_price(
    price: float, spread_pct: float = 0.0, slippage_pct: float = 0.0
) -> float:
    raw = max(0.0, float(price))
    adverse = (
        max(0.0, float(spread_pct)) / 200.0 + max(0.0, float(slippage_pct)) / 100.0
    )
    return raw * max(0.0, 1.0 - adverse)


def atr(klines: list[Kline], period: int = 14) -> float | None:
    period = int(period)
    if period <= 0 or not klines:
        return None
    trs: list[float] = []
    for i, candle in enumerate(klines):
        prev_close = klines[i - 1].close if i else candle.close
        trs.append(
            max(
                candle.high - candle.low,
                abs(candle.high - prev_close),
                abs(candle.low - prev_close),
            )
        )
    if len(trs) < period:
        return None
    result = sum(trs[:period]) / period
    for tr in trs[period:]:
        result = (result * (period - 1) + tr) / period
    return result if math.isfinite(result) and result > 0 else None


def detect_demand_zone(klines: list[Kline], config: dict) -> dict:
    """Konfirmasi posisi harga terhadap zona demand pada candle tertutup.

    Aturan, dipakai identik oleh LIVE, PAPER, dan backtest:

    1. zona_low  = low terendah "rak", yaitu candle di dalam DEMAND_LOOKBACK_BARS
                   candle tertutup terakhir yang menutup di area dasar (area
                   akumulasi; sumbu sesaat dan candle impuls tidak ikut).
    2. zone_high = puncak body terendah di dalam rak, ditebalkan minimal
                   DEMAND_ZONE_BUFFER_PCT di atas zona_low.
    3. close candle sinyal tidak boleh menembus zona_low.
    4. candle sinyal harus hijau terhadap open-nya sendiri dan terhadap close
       candle sebelumnya (reaksi demand, bukan pantulan lemah).
    5. close_pos = (close - low) / (high - low) harus >=
       DEMAND_MIN_CLOSE_POSITION (dorongan pembeli, menolak ekor atas panjang).
    6. jarak harga diukur dari BATAS ATAS zona (zone_high), bukan dari dasar:
       (close / zone_high - 1) * 100 tidak boleh melewati
       DEMAND_MAX_DISTANCE_PCT. Toleransi riil dari dasar zona karena itu
       sekitar DEMAND_ZONE_BUFFER_PCT + DEMAND_MAX_DISTANCE_PCT.

    Semua kegagalan bersifat menolak entry (fail closed), termasuk data kurang
    atau OHLC tidak valid. Saat DEMAND_ZONE_FILTER_ENABLED dimatikan, fungsi
    selalu lolos tanpa membaca candle sama sekali.
    """
    if not bool(config.get("DEMAND_ZONE_FILTER_ENABLED", False)):
        return {
            "ok": True,
            "reason": "filter zona demand nonaktif",
            "zone_low": None,
            "zone_high": None,
            "distance_pct": None,
            "close_position": None,
        }

    lookback = max(3, int(config.get("DEMAND_LOOKBACK_BARS", 20) or 20))
    buffer_pct = max(0.0, float(config.get("DEMAND_ZONE_BUFFER_PCT", 0.8) or 0.0))
    max_dist_pct = max(0.0, float(config.get("DEMAND_MAX_DISTANCE_PCT", 3.5) or 0.0))
    min_close_pos = max(
        0.0, min(1.0, float(config.get("DEMAND_MIN_CLOSE_POSITION", 0.45) or 0.0))
    )

    required = lookback + 1
    if not klines or len(klines) < required:
        return {
            "ok": False,
            "reason": f"data zona demand kurang: {len(klines) if klines else 0} dari minimum {required}",
            "zone_low": None,
            "zone_high": None,
            "distance_pct": None,
            "close_position": None,
        }

    prior = klines[-1 - lookback : -1]
    signal = klines[-1]

    for candle in prior + [signal]:
        if (
            not math.isfinite(candle.open)
            or not math.isfinite(candle.high)
            or not math.isfinite(candle.low)
            or not math.isfinite(candle.close)
            or candle.low <= 0
            or candle.high < candle.low
        ):
            return {
                "ok": False,
                "reason": "data OHLC candle untuk zona demand tidak valid",
                "zone_low": None,
                "zone_high": None,
                "distance_pct": None,
                "close_position": None,
            }

    # --- pemetaan dasar zona demand -------------------------------------
    # `prior` sudah persis DEMAND_LOOKBACK_BARS candle tertutup terakhir
    # (dipotong di baris di atas), jadi seluruh jendela itulah yang memetakan
    # dasar demand. Inilah yang membuat DEMAND_LOOKBACK_BARS benar-benar
    # mengubah geometri zona, bukan hanya syarat jumlah data.
    #
    # Dasarnya dicari lewat "rak" (shelf): candle di dalam jendela yang MENUTUP
    # di area dasar, tidak lebih tinggi dari DEMAND_ZONE_BUFFER_PCT di atas
    # level dasar. Menutup, bukan menyentuh: candle impuls besar yang low-ya
    # masih menyentuh dasar tapi close-ya sudah melayang bukan area akumulasi,
    # dan sumbu panjang sesaat (flash dump) juga bukan. Kalau rak dihitung dari
    # low, dua hal itu ikut masuk ke rak, puncak zona ikut naik, selisih ke
    # harga mengecil, dan filter jadi longgar justru pada saat harga paling
    # rawan dibeli di pucuk.
    #
    # Rak wajib berisi minimal 2 candle. Kalau tidak (jendela terlalu sempit,
    # atau harga sudah lurus naik tanpa konsolidasi), pemetaan jatuh kembali ke
    # konsolidasi 6 candle terbaru supaya filter tidak fail-open tanpa dasar.
    window = prior
    anchor = min(float(k.close) for k in window)
    band = anchor * (1.0 + buffer_pct / 100.0)
    shelf = [k for k in window if float(k.close) <= band]
    if len(shelf) >= 2:
        # Dasar zona tidak boleh lebih dalam dari pita buffer di bawah level
        # dasar: sumbu yang lebih panjang dari itu adalah spike, bukan support.
        zone_low = max(
            min(float(k.low) for k in shelf), anchor * (1.0 - buffer_pct / 100.0)
        )
        base_candles = shelf
    else:
        base_candles = window[-min(len(window), 6) :]
        zone_low = min(float(k.low) for k in base_candles)
    # Atap zona = puncak body TERENDAH di dalam area dasar, bukan tertinggi.
    # Rak bisa miring (base yang merangkak naik); memakai puncak tertinggi akan
    # melebarkan zona ke atas sehingga harga yang sudah lari jauh masih dianggap
    # dekat demand. Terukur di 108 skenario x 300 seri: dengan puncak tertinggi
    # ada 3 skenario yang justru lebih longgar daripada filter versi sebelumnya
    # dan lookback 6 kehilangan status mode kompatibilitasnya (+4,0% entry);
    # dengan puncak terendah tidak ada satu pun skenario yang lebih longgar dan
    # lookback 6 kembali persis seperti perilaku lama (+0,0%).
    base_body = min(max(float(k.open), float(k.close)) for k in base_candles)
    # Tebal minimum zona tetap DIAM-nya, bukan ikut-ikutan melebar. zone_low tidak
    # mungkin melewati base_body karena setiap candle punya open/close >= low-nya.
    zone_high = max(base_body, zone_low * (1.0 + buffer_pct / 100.0))

    sig_open = float(signal.open)
    sig_high = float(signal.high)
    sig_low = float(signal.low)
    sig_close = float(signal.close)
    prev_close = float(prior[-1].close)

    candle_range = sig_high - sig_low
    if candle_range > 0:
        close_pos = (sig_close - sig_low) / candle_range
    else:
        close_pos = 1.0 if sig_close >= prev_close else 0.0

    distance_pct = (
        max(0.0, (sig_close / zone_high - 1.0) * 100.0) if zone_high > 0 else 0.0
    )

    if sig_close < zone_low:
        return {
            "ok": False,
            "reason": f"zona demand ditembus: close {sig_close:.6g} di bawah dasar demand {zone_low:.6g}",
            "zone_low": zone_low,
            "zone_high": zone_high,
            "distance_pct": distance_pct,
            "close_position": close_pos,
        }

    if sig_close < sig_open or sig_close < prev_close:
        return {
            "ok": False,
            "reason": (
                f"reaksi demand lemah: candle sinyal bearish/turun "
                f"(open={sig_open:.6g}, close={sig_close:.6g}, prev={prev_close:.6g})"
            ),
            "zone_low": zone_low,
            "zone_high": zone_high,
            "distance_pct": distance_pct,
            "close_position": close_pos,
        }

    if close_pos + 1e-9 < min_close_pos:
        return {
            "ok": False,
            "reason": (
                f"dorongan demand kurang: posisi close candle {close_pos:.2f} "
                f"di bawah minimum {min_close_pos:.2f}"
            ),
            "zone_low": zone_low,
            "zone_high": zone_high,
            "distance_pct": distance_pct,
            "close_position": close_pos,
        }

    if max_dist_pct > 0 and distance_pct > max_dist_pct:
        return {
            "ok": False,
            "reason": (
                f"harga terlalu jauh di atas zona demand: +{distance_pct:.2f}% "
                f"di atas batas atas zona {zone_high:.6g} (batas {max_dist_pct:g}%)"
            ),
            "zone_low": zone_low,
            "zone_high": zone_high,
            "distance_pct": distance_pct,
            "close_position": close_pos,
        }

    return {
        "ok": True,
        "reason": (
            f"demand zone valid [{zone_low:.6g}..{zone_high:.6g}], "
            f"jarak +{distance_pct:.2f}%, close_pos {close_pos:.2f}"
        ),
        "zone_low": zone_low,
        "zone_high": zone_high,
        "distance_pct": distance_pct,
        "close_position": close_pos,
    }


def resolve_exit_levels(config: dict) -> dict:
    use_atr = bool(config.get("USE_ATR_EXIT", False))
    if use_atr:
        period = max(1, int(config.get("ATR_PERIOD", 14) or 14))
        sl = abs(float(config.get("ATR_MULT_SL", 1.5) or 0.0))
        tp = abs(float(config.get("ATR_MULT_TP", 3.0) or 0.0))
        trail = abs(float(config.get("ATR_MULT_TRAIL", 1.0) or 0.0))
        be_trigger = abs(float(config.get("ATR_MULT_BE_TRIGGER", 1.0) or 0.0))
        be_lock = abs(float(config.get("ATR_MULT_BE_LOCK", 0.1) or 0.0))
        trail_start = abs(float(config.get("ATR_MULT_TRAIL_START", 1.5) or 0.0))
        trail = min(trail, sl)
        be_trigger = min(be_trigger, trail_start)
        be_lock = min(be_lock, be_trigger)
        atr_value = config.get("_atr_value")
        if atr_value is not None:
            atr_value = abs(float(atr_value))
        scale = atr_value if atr_value is not None else 1.0
        return {
            "sl_pct": sl * scale,
            "tp_pct": tp * scale,
            "be_trigger_pct": be_trigger * scale,
            "be_lock_pct": be_lock * scale,
            "trail_start_pct": trail_start * scale,
            "trail_step_pct": trail * scale,
            "atr_period": period,
            "atr_value": atr_value,
            "atr_mult_sl": sl,
            "atr_mult_tp": tp,
            "atr_mult_trail": trail,
            "source": "ATR",
            "note": "Level exit berbasis ATR; invariant diterapkan",
        }

    fixed_sl = abs(float(config.get("SL_PCT", 1.8)))
    fixed_tp = abs(float(config.get("TP_PCT", 4.0)))
    fixed_be_trigger = abs(float(config.get("BE_TRIGGER_PCT", 1.0)))
    fixed_be_lock = abs(float(config.get("BE_LOCK_PCT", 0.15)))
    fixed_trail_start = abs(float(config.get("TRAILING_START_PCT", 1.5)))
    fixed_trail_step = abs(float(config.get("TRAILING_STEP_PCT", 0.6)))
    fixed_trail_step = min(fixed_trail_step, fixed_sl)
    fixed_be_trigger = min(fixed_be_trigger, fixed_trail_start)
    fixed_be_lock = min(fixed_be_lock, fixed_be_trigger)
    return {
        "sl_pct": fixed_sl,
        "tp_pct": fixed_tp,
        "be_trigger_pct": fixed_be_trigger,
        "be_lock_pct": fixed_be_lock,
        "trail_start_pct": fixed_trail_start,
        "trail_step_pct": fixed_trail_step,
        "source": "FIXED",
        "note": "Level exit tetap dari config; invariant diterapkan",
    }


TREND_KLINE_LIMIT = 1000


def trend_interval(config: dict) -> str:
    raw = str(config.get("TREND_INTERVAL", "1h") or "1h").strip().lower()
    if raw not in INTERVAL_MINUTES:
        raise ValueError(
            f"TREND_INTERVAL '{raw}' tidak dikenal. Pilihan: "
            + ", ".join(sorted(INTERVAL_MINUTES, key=INTERVAL_MINUTES.get))
        )
    return raw


def trend_interval_minutes(config: dict) -> int:
    return int(INTERVAL_MINUTES[trend_interval(config)])


def trend_required_bars(config: dict) -> int:
    """Candle trend minimum agar EMA dan ADX benar-benar terdefinisi.

    EMA butuh `periode` candle untuk seed SMA. ADX Wilder butuh `periode` nilai
    TR/DM untuk seed ATR/DM, lalu `periode` nilai DX lagi untuk seed ADX,
    sehingga minimal 2 x periode candle.
    """
    fast = max(2, int(config.get("TREND_EMA_FAST", 20) or 20))
    slow = max(3, int(config.get("TREND_EMA_SLOW", 50) or 50))
    adx_period = max(2, int(config.get("TREND_ADX_PERIOD", 14) or 14))
    return max(fast, slow, 2 * adx_period + 1)


def trend_window_bars(config: dict) -> int:
    """Jumlah candle trend yang dipakai gerbang trend.

    Nilai yang SAMA dipakai bot live (limit fetch dari Binance) dan backtest
    (potongan jendela dari candle yang sudah tutup), supaya keputusan keduanya
    identik. Endpoint klines Binance membatasi 1000 candle per panggilan.
    """
    diminta = int(config.get("TREND_LOOKBACK_BARS", 120) or 120)
    diminta = max(1, min(TREND_KLINE_LIMIT - 1, diminta))
    return max(trend_required_bars(config), diminta)


def trend_warmup_bars(config: dict, interval: str) -> int:
    """Candle interval simulasi yang wajib tersedia sebelum bar entry pertama.

    Backtest membentuk candle trend dengan merangkai candle interval simulasi,
    jadi warmup harus menutupi (jumlah candle trend + 1 bucket) x rasio.
    """
    sim_minutes = int(INTERVAL_MINUTES.get(str(interval), 5))
    trend_minutes = trend_interval_minutes(config)
    if trend_minutes % sim_minutes:
        raise ValueError(
            f"TREND_INTERVAL '{trend_interval(config)}' ({trend_minutes} menit) harus "
            f"kelipatan bulat dari interval simulasi '{interval}' ({sim_minutes} menit) "
            "supaya backtest bisa merangkai candle trend dari data yang sudah diunduh."
        )
    rasio = trend_minutes // sim_minutes
    return trend_window_bars(config) * rasio + rasio


def aggregate_klines(
    klines: list[Kline], target_minutes: int, source_minutes: Optional[int] = None
) -> list[Kline]:
    """Rangkai candle timeframe tinggi dari candle yang lebih kecil.

    Hanya bucket yang LENGKAP yang dipakai: bucket yang kekurangan candle
    (data bolong atau jam terakhir yang belum selesai) dibuang, supaya high/low
    dan volume candle rangkaian tidak pernah setengah matang. Nilai OHLCV hasil
    rangkaian sama dengan candle asli Binance untuk rentang yang sama.
    """
    target_ms = int(target_minutes) * 60_000
    if source_minutes is None:
        if len(klines) >= 2:
            source_minutes = max(
                1, (int(klines[1].open_time) - int(klines[0].open_time)) // 60_000
            )
        else:
            source_minutes = int(target_minutes)
    source_ms = int(source_minutes) * 60_000
    if source_ms <= 0 or target_ms % source_ms:
        raise ValueError(
            f"Interval sumber {source_minutes} menit tidak bisa dirangkai ke "
            f"{target_minutes} menit (harus pembagi bulat)."
        )
    rasio = target_ms // source_ms
    if rasio <= 1:
        return list(klines)

    tersusun: dict[int, list[Kline]] = {}
    for candle in sorted(klines, key=lambda k: int(k.open_time)):
        bucket = (int(candle.open_time) // target_ms) * target_ms
        tersusun.setdefault(bucket, []).append(candle)

    hasil: list[Kline] = []
    for bucket in sorted(tersusun):
        rows = tersusun[bucket]
        if len(rows) != rasio:
            continue
        if any(int(rows[i].open_time) != bucket + i * source_ms for i in range(rasio)):
            continue
        hasil.append(
            Kline(
                open_time=bucket,
                open=float(rows[0].open),
                high=max(float(k.high) for k in rows),
                low=min(float(k.low) for k in rows),
                close=float(rows[-1].close),
                close_time=bucket + target_ms - 1,
                volume=sum(float(k.volume) for k in rows),
                quote_volume=sum(float(k.quote_volume) for k in rows),
            )
        )
    return hasil


def ema_series(values: list[float], period: int) -> list[Optional[float]]:
    """EMA klasik dengan seed SMA pada `period` nilai pertama.

    Panjang hasil selalu sama dengan panjang input; None berarti belum
    terdefinisi (data belum cukup), bukan angka nol yang menyesatkan.
    """
    period = int(period)
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    try:
        seed = sum(float(v) for v in values[:period]) / period
    except (TypeError, ValueError):
        return out
    if not math.isfinite(seed):
        return out
    out[period - 1] = seed
    faktor = 2.0 / (period + 1.0)
    prev = seed
    for i in range(period, n):
        nilai = float(values[i])
        if not math.isfinite(nilai):
            return out
        prev = nilai * faktor + prev * (1.0 - faktor)
        if not math.isfinite(prev):
            return out
        out[i] = prev
    return out


def adx_series(klines: list[Kline], period: int = 14) -> list[Optional[float]]:
    """ADX Wilder (periode default 14), sejajar dengan daftar candle.

    Rumus mengikuti definisi asli Wilder: TR dan directional movement dihaluskan
    dengan smoothing Wilder (bukan EMA biasa), DX = 100 x |DI+ - DI-| / (DI+ + DI-),
    lalu ADX = rata-rata Wilder dari DX. None berarti belum/ tidak bisa dihitung
    (misalnya rentang harga nol total sehingga pembagi nol).
    """
    period = int(period)
    n = len(klines)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < 2 * period:
        return out

    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr[0] = float(klines[0].high) - float(klines[0].low)
    for i in range(1, n):
        high = float(klines[i].high)
        low = float(klines[i].low)
        prev_high = float(klines[i - 1].high)
        prev_low = float(klines[i - 1].low)
        prev_close = float(klines[i - 1].close)
        if not all(
            math.isfinite(v) for v in (high, low, prev_high, prev_low, prev_close)
        ):
            return out
        naik = high - prev_high
        turun = prev_low - low
        if naik > turun and naik > 0:
            plus_dm[i] = naik
        if turun > naik and turun > 0:
            minus_dm[i] = turun
        tr[i] = max(high - low, abs(high - prev_close), abs(low - prev_close))

    def _dx(
        tr_smooth: float, plus_smooth: float, minus_smooth: float
    ) -> Optional[float]:
        if tr_smooth <= 0 or not math.isfinite(tr_smooth):
            return None
        di_plus = 100.0 * plus_smooth / tr_smooth
        di_minus = 100.0 * minus_smooth / tr_smooth
        jumlah = di_plus + di_minus
        if jumlah <= 0:
            return 0.0
        return 100.0 * abs(di_plus - di_minus) / jumlah

    tr_smooth = sum(tr[1 : period + 1])
    plus_smooth = sum(plus_dm[1 : period + 1])
    minus_smooth = sum(minus_dm[1 : period + 1])

    dx: list[Optional[float]] = [None] * n
    dx[period] = _dx(tr_smooth, plus_smooth, minus_smooth)
    for i in range(period + 1, n):
        tr_smooth = tr_smooth - tr_smooth / period + tr[i]
        plus_smooth = plus_smooth - plus_smooth / period + plus_dm[i]
        minus_smooth = minus_smooth - minus_smooth / period + minus_dm[i]
        dx[i] = _dx(tr_smooth, plus_smooth, minus_smooth)

    seed_start = 2 * period - 1
    if n <= seed_start:
        return out
    jendela = [dx[i] for i in range(period, seed_start + 1)]
    if any(v is None for v in jendela):
        return out
    adx = sum(float(v) for v in jendela) / period
    out[seed_start] = adx
    for i in range(seed_start + 1, n):
        nilai = dx[i]
        if nilai is None:
            return out
        adx = (adx * (period - 1) + float(nilai)) / period
        out[i] = adx
    return out


def evaluate_trend_filter(
    klines: list[Kline], config: dict, signal_close_time_ms: Optional[int] = None
) -> dict:
    """Gerbang trend timeframe tinggi (default H1) untuk menyaring entry.

    Aturan, semuanya dari candle yang SUDAH TUTUP (tanpa repaint):
      1. close candle terakhir > EMA cepat,
      2. EMA cepat > EMA lambat,
      3. ADX >= TREND_ADX_MIN (kecuali ambangnya 0, artinya cek kekuatan trend mati).

    Candle yang belum tutup selalu dibuang di sini, walaupun pemanggil lupa
    menyaringnya, sehingga hasil live dan backtest tidak bisa berbeda karena
    candle yang masih berjalan.
    """
    interval = str(config.get("TREND_INTERVAL", "1h") or "1h").strip().lower()
    if interval not in INTERVAL_MINUTES:
        return {
            "ok": False,
            "reason": (
                f"TREND_INTERVAL '{interval}' tidak dikenal; pilihan: "
                + ", ".join(sorted(INTERVAL_MINUTES, key=INTERVAL_MINUTES.get))
            ),
            "interval": interval,
            "bars": 0,
            "required": 0,
            "window": 0,
            "close": None,
            "ema_fast": None,
            "ema_slow": None,
            "adx": None,
            "checks": {},
            "values": {},
        }
    if not bool(config.get("TREND_FILTER_ENABLED", False)):
        return {
            "ok": True,
            "reason": f"filter trend {interval} nonaktif",
            "interval": interval,
            "bars": 0,
            "required": 0,
            "close": None,
            "ema_fast": None,
            "ema_slow": None,
            "adx": None,
            "checks": {},
            "values": {},
        }

    fast = max(2, int(config.get("TREND_EMA_FAST", 20) or 20))
    slow = max(3, int(config.get("TREND_EMA_SLOW", 50) or 50))
    adx_period = max(2, int(config.get("TREND_ADX_PERIOD", 14) or 14))
    adx_min = float(config.get("TREND_ADX_MIN", 20.0) or 0.0)
    if adx_min < 0:
        adx_min = 0.0
    jendela_penuh = trend_window_bars(config)
    minimum = trend_required_bars(config)

    siap = [
        k
        for k in (klines or [])
        if signal_close_time_ms is None
        or int(k.close_time) <= int(signal_close_time_ms)
    ]
    jendela = siap[-jendela_penuh:]

    kosong = {
        "interval": interval,
        "bars": len(jendela),
        "required": minimum,
        "window": jendela_penuh,
        "close": None,
        "ema_fast": None,
        "ema_slow": None,
        "adx": None,
        "checks": {},
        "values": {},
    }
    if len(jendela) < minimum:
        return {
            "ok": False,
            "reason": (
                f"data candle {interval} kurang: {len(jendela)} dari minimum "
                f"{minimum} (jendela {jendela_penuh}; naikkan TREND_LOOKBACK_BARS "
                "atau tunggu riwayat bertambah)"
            ),
            **kosong,
        }

    for candle in jendela:
        if (
            not math.isfinite(float(candle.close))
            or float(candle.close) <= 0
            or float(candle.high) < float(candle.low)
        ):
            return {"ok": False, "reason": f"candle {interval} tidak valid", **kosong}

    closes = [float(k.close) for k in jendela]
    ema_fast_series = ema_series(closes, fast)
    ema_slow_series = ema_series(closes, slow)
    adx_values = (
        adx_series(jendela, adx_period) if adx_min > 0 else [None] * len(jendela)
    )
    ema_fast = ema_fast_series[-1]
    ema_slow = ema_slow_series[-1]
    adx_now = adx_values[-1]
    close_now = closes[-1]

    terisi = {
        "interval": interval,
        "bars": len(jendela),
        "required": minimum,
        "window": jendela_penuh,
        "close": close_now,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "adx": adx_now,
        "checks": {},
        "values": {
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "adx": adx_now,
            "close": close_now,
        },
    }

    if ema_fast is None or ema_slow is None:
        return {
            "ok": False,
            "reason": (
                f"EMA {interval} belum terdefinisi pada jendela "
                f"{len(jendela)} candle (butuh EMA cepat {fast} dan "
                f"EMA lambat {slow})"
            ),
            **terisi,
        }

    if adx_min > 0 and adx_now is None:
        return {
            "ok": False,
            "reason": (
                f"ADX {adx_period} {interval} tidak bisa dihitung dari "
                "jendela ini (rentang harga nol atau data rusak)"
            ),
            **terisi,
        }

    harga_di_atas = close_now > float(ema_fast)
    susunan_naik = float(ema_fast) > float(ema_slow)
    trend_kuat = True if adx_min <= 0 else bool(float(adx_now) >= adx_min)

    checks = {
        "harga_di_atas_ema_cepat": harga_di_atas,
        "ema_cepat_di_atas_ema_lambat": susunan_naik,
        "adx_di_atas_ambang": trend_kuat,
    }
    ringkas = (
        f"close {close_now:.6g} vs EMA{fast} {float(ema_fast):.6g} vs "
        f"EMA{slow} {float(ema_slow):.6g}"
        + (f", ADX{adx_period} {float(adx_now):.1f}" if adx_now is not None else "")
    )

    if not harga_di_atas:
        return {
            "ok": False,
            "reason": (
                f"trend {interval} turun: close {close_now:.6g} di bawah "
                f"EMA{fast} {float(ema_fast):.6g} ({ringkas})"
            ),
            **{**terisi, "checks": checks},
        }
    if not susunan_naik:
        return {
            "ok": False,
            "reason": (
                f"struktur {interval} belum naik: EMA{fast} "
                f"{float(ema_fast):.6g} di bawah EMA{slow} "
                f"{float(ema_slow):.6g} ({ringkas})"
            ),
            **{**terisi, "checks": checks},
        }
    if not trend_kuat:
        return {
            "ok": False,
            "reason": (
                f"trend {interval} lemah: ADX{adx_period} "
                f"{float(adx_now):.1f} di bawah ambang {adx_min:g} ({ringkas})"
            ),
            **{**terisi, "checks": checks},
        }

    return {
        "ok": True,
        "reason": f"trend {interval} naik dan kuat ({ringkas})",
        **{**terisi, "checks": checks},
    }


# ---------------------------------------------------------------------------
# Selftest indikator trend
#
# Nilai referensi EMA dan ADX di bawah diambil dari TA-Lib 0.8.1 (implementasi
# rujukan industri untuk EMA dan ADX Wilder) pada seri deterministik yang sama.
# EMA cocok sampai batas presisi float; ADX TA-Lib memakai cara seed smoothing
# Wilder yang sedikit berbeda sehingga selisihnya menyusut secara geometris
# (0.006 persen setelah 140 candle, nol setelah sekitar 250 candle) dan karena
# itu diuji dengan toleransi kecil yang didokumentasikan.
# ---------------------------------------------------------------------------

_REF_CLOSE_TERAKHIR = 111.10647491522288
_REF_EMA20_TERAKHIR = 109.4661022879455
_REF_EMA50_TERAKHIR = 106.74824666390583
_REF_ADX14_TERAKHIR_SERI_AYUN = 24.90058798601024


def _seri_uji_deterministik(n: int = 60) -> list[Kline]:
    import math as _math

    out: list[Kline] = []
    for i in range(n):
        harga = 100.0 * (1.0 + 0.01 * _math.sin(i / 5.0) + i * 0.002)
        out.append(
            Kline(
                open_time=i * 3_600_000,
                open=harga * 0.999,
                high=harga * 1.004,
                low=harga * 0.996,
                close=harga,
                close_time=i * 3_600_000 + 3_599_999,
                volume=1.0,
                quote_volume=harga,
            )
        )
    return out


def _seri_uji_riak(
    n: int = 130,
    amplitudo: float = 4.0,
    drift: float = 0.08,
    akhir_naik: int = 10,
    tick_akhir: float = 0.0008,
) -> list[Kline]:
    """Seri dengan susunan EMA naik tetapi ADX lemah (pasar beriak).

    Sepuluh candle terakhir dibuat naik halus supaya close berada di atas EMA,
    sementara riak besar sebelumnya menahan ADX di bawah ambang.
    """
    import math as _math

    out: list[Kline] = []
    harga = 100.0
    for i in range(n):
        if i >= n - akhir_naik:
            harga = harga * (1.0 + tick_akhir)
        else:
            harga = 100.0 + amplitudo * _math.sin(i / 2.0) + drift * i
        out.append(
            Kline(
                open_time=i * 3_600_000,
                open=harga * 0.999,
                high=harga * 1.003,
                low=harga * 0.997,
                close=harga,
                close_time=i * 3_600_000 + 3_599_999,
                volume=1.0,
                quote_volume=harga,
            )
        )
    return out


def _seri_uji_ayun(n: int = 140) -> list[Kline]:
    import math as _math

    out: list[Kline] = []
    for i in range(n):
        harga = 100.0 + 4.0 * _math.sin(i / 3.0) + 0.05 * i
        out.append(
            Kline(
                open_time=i * 3_600_000,
                open=harga * 0.998,
                high=harga * 1.005,
                low=harga * 0.995,
                close=harga,
                close_time=i * 3_600_000 + 3_599_999,
                volume=1.0,
                quote_volume=harga,
            )
        )
    return out


def selftest() -> int:
    print("=== SELFTEST indikator trend (EMA, ADX, rangkaian candle, gerbang) ===")
    gagal = 0

    def cek(nama: str, syarat: bool, info: str = "") -> None:
        nonlocal gagal
        if not syarat:
            gagal += 1
        print(
            ("  LULUS " if syarat else "  GAGAL ")
            + nama
            + (f"  -> {info}" if info else "")
        )

    # --- EMA ---
    seri = _seri_uji_deterministik()
    tutup = [k.close for k in seri]
    ema20 = ema_series(tutup, 20)
    ema50 = ema_series(tutup, 50)
    cek(
        "EMA belum terdefinisi sebelum periode terpenuhi",
        all(v is None for v in ema20[:19]) and ema20[19] is not None,
    )
    cek(
        "EMA50 belum terdefinisi pada 49 candle pertama",
        ema50[48] is None and ema50[49] is not None,
    )
    cek(
        "EMA20 akhir sama dengan referensi TA-Lib",
        abs(ema20[-1] - _REF_EMA20_TERAKHIR) < 1e-9,
        f"{ema20[-1]:.10f}",
    )
    cek(
        "EMA50 akhir sama dengan referensi TA-Lib",
        abs(ema50[-1] - _REF_EMA50_TERAKHIR) < 1e-9,
        f"{ema50[-1]:.10f}",
    )
    cek(
        "close akhir sama dengan referensi", abs(tutup[-1] - _REF_CLOSE_TERAKHIR) < 1e-9
    )
    datar = [5.0] * 40
    cek(
        "EMA seri datar sama dengan nilainya sendiri",
        abs(ema_series(datar, 20)[-1] - 5.0) < 1e-12,
    )
    naik_linear = [100.0 + i for i in range(60)]
    e = ema_series(naik_linear, 20)[-1]
    _sma20 = sum(naik_linear[-20:]) / 20
    cek(
        "EMA seri naik berada di antara SMA dan harga terakhir",
        _sma20 <= e <= naik_linear[-1],
        f"{e:.6f}",
    )

    # --- ADX ---
    adx_satur = adx_series(seri, 14)
    cek(
        "ADX terdefinisi mulai candle ke-2x periode + 1",
        all(v is None for v in adx_satur[:27]) and adx_satur[27] is not None,
    )
    cek(
        "ADX seri naik monoton jenuh di 100",
        abs(adx_satur[-1] - 100.0) < 1e-9,
        f"{adx_satur[-1]:.6f}",
    )
    adx_ayun = adx_series(_seri_uji_ayun(), 14)
    cek(
        "ADX seri berayun sama dengan referensi TA-Lib (toleransi 0.01)",
        abs(adx_ayun[-1] - _REF_ADX14_TERAKHIR_SERI_AYUN) < 0.01,
        f"{adx_ayun[-1]:.6f} vs {_REF_ADX14_TERAKHIR_SERI_AYUN:.6f}",
    )
    cek(
        "ADX seri berayun di bawah ambang 30 (tidak jenuh)",
        adx_ayun[-1] < 30.0,
        f"{adx_ayun[-1]:.2f}",
    )
    adx_datar = adx_series(
        [
            Kline(
                i * 3_600_000, 1.0, 1.0, 1.0, 1.0, i * 3_600_000 + 3_599_999, 1.0, 1.0
            )
            for i in range(40)
        ],
        14,
    )
    cek(
        "ADX rentang harga nol tidak dihitung (None, fail closed)",
        all(v is None for v in adx_datar),
    )
    cek("ADX data kurang ditolak", all(v is None for v in adx_series(seri[:20], 14)))

    # --- rangkaian candle ---
    lima = [
        Kline(
            open_time=i * 300_000,
            open=100.0 + i,
            high=100.5 + i,
            low=99.5 + i,
            close=100.2 + i,
            close_time=i * 300_000 + 299_999,
            volume=10.0,
            quote_volume=1000.0,
        )
        for i in range(24)
    ]
    jam = aggregate_klines(lima, 60, 5)
    cek(
        "12 candle 5m menjadi 1 candle 1h",
        len(jam) == 2 and jam[0].open_time == 0 and jam[1].open_time == 3_600_000,
    )
    cek(
        "OHLC hasil rangkaian benar",
        jam[0].open == lima[0].open
        and jam[0].close == lima[11].close
        and jam[0].high == max(k.high for k in lima[:12])
        and jam[0].low == min(k.low for k in lima[:12]),
    )
    cek(
        "volume hasil rangkaian adalah jumlah anaknya",
        abs(jam[0].volume - 120.0) < 1e-9
        and abs(jam[0].quote_volume - 12_000.0) < 1e-9,
    )
    cek(
        "close_time candle rangkaian menutup bucket", jam[0].close_time == 3_600_000 - 1
    )
    bolong = [k for i, k in enumerate(lima) if i not in (3, 4)]
    cek(
        "bucket tidak lengkap dibuang (bukan dipakai setengah matang)",
        len(aggregate_klines(bolong, 60, 5)) == 1,
    )
    cek(
        "rangkaian interval tidak bulat ditolak",
        _gagal_tertangkap(lambda: aggregate_klines(lima, 7, 5)),
    )

    # --- gerbang trend ---
    cfg = {
        "TREND_FILTER_ENABLED": True,
        "TREND_INTERVAL": "1h",
        "TREND_EMA_FAST": 20,
        "TREND_EMA_SLOW": 50,
        "TREND_ADX_PERIOD": 14,
        "TREND_ADX_MIN": 20.0,
        "TREND_LOOKBACK_BARS": 120,
    }

    def jam_deret(a: float, n: int = 130) -> list[Kline]:
        out = []
        p = 100.0
        for i in range(n):
            p *= a
            out.append(
                Kline(
                    i * 3_600_000,
                    p,
                    p * 1.002,
                    p * 0.998,
                    p,
                    i * 3_600_000 + 3_599_999,
                    1.0,
                    p,
                )
            )
        return out

    naik = evaluate_trend_filter(jam_deret(1.004), cfg)
    turun = evaluate_trend_filter(jam_deret(0.996), cfg)
    riak = evaluate_trend_filter(_seri_uji_riak(), cfg)
    cek("trend naik diloloskan", naik["ok"], naik["reason"])
    cek(
        "trend turun ditolak",
        (not turun["ok"]) and "di bawah" in turun["reason"],
        turun["reason"],
    )
    cek(
        "susunan EMA naik tetapi ADX lemah ditolak",
        (not riak["ok"]) and "lemah" in riak["reason"],
        riak["reason"],
    )
    cek(
        "riak memang punya susunan EMA naik (bukan salah tolak karena struktur)",
        bool(riak["checks"]["harga_di_atas_ema_cepat"])
        and bool(riak["checks"]["ema_cepat_di_atas_ema_lambat"]),
    )
    cek(
        "ADX_MIN 0 meloloskan riak (cek kekuatan trend benar-benar dimatikan)",
        evaluate_trend_filter(_seri_uji_riak(), dict(cfg, TREND_ADX_MIN=0.0))["ok"],
    )
    datar_penuh = evaluate_trend_filter(jam_deret(1.0), cfg)
    cek(
        "harga rata sempurna ditolak (close tidak lebih tinggi dari EMA)",
        not datar_penuh["ok"],
        datar_penuh["reason"],
    )
    cek(
        "filter nonaktif selalu lolos",
        evaluate_trend_filter(jam_deret(0.996), dict(cfg, TREND_FILTER_ENABLED=False))[
            "ok"
        ],
    )
    kurang = evaluate_trend_filter(jam_deret(1.004, 40), cfg)
    cek(
        "riwayat kurang ditolak (fail closed)",
        (not kurang["ok"]) and "kurang" in kurang["reason"],
        kurang["reason"],
    )
    cek(
        "jendela dipakai persis TREND_LOOKBACK_BARS",
        evaluate_trend_filter(jam_deret(1.004), cfg)["bars"] == 120,
    )
    cek(
        "candle belum tutup dibuang walau diberikan pemanggil",
        evaluate_trend_filter(
            jam_deret(1.004)
            + [
                Kline(
                    130 * 3_600_000,
                    1.0,
                    1.0,
                    1.0,
                    1.0,
                    130 * 3_600_000 + 3_599_999,
                    1.0,
                    1.0,
                )
            ],
            cfg,
        )["bars"]
        == 120,
    )
    cek(
        "TREND_INTERVAL tidak dikenal ditolak",
        not evaluate_trend_filter(jam_deret(1.004), dict(cfg, TREND_INTERVAL="7h"))[
            "ok"
        ],
    )
    cek(
        "harga tidak wajar ditolak",
        not evaluate_trend_filter(
            [
                Kline(
                    i * 3_600_000,
                    1.0,
                    1.0,
                    1.0,
                    0.0,
                    i * 3_600_000 + 3_599_999,
                    1.0,
                    1.0,
                )
                for i in range(130)
            ],
            cfg,
        )["ok"],
        "harga nol",
    )
    cek(
        "helper kebutuhan warmup menghitung rasio interval",
        trend_warmup_bars(cfg, "5m") == trend_window_bars(cfg) * 12 + 12,
    )
    cek(
        "warmup menolak trend yang lebih pendek dari interval simulasi",
        _gagal_tertangkap(
            lambda: trend_warmup_bars(dict(cfg, TREND_INTERVAL="30m"), "1h")
        ),
    )

    # --- zona demand: knob lookback harus benar-benar mengubah geometri ---
    def _k(o, h, l, c, i=0):
        return Kline(
            i * 300_000, o, h, l, c, i * 300_000 + 299_999, 1000.0, 1000.0 * c
        )

    cfg_dem = dict(
        DEMAND_ZONE_FILTER_ENABLED=True,
        DEMAND_LOOKBACK_BARS=20,
        DEMAND_ZONE_BUFFER_PCT=0.8,
        DEMAND_MAX_DISTANCE_PCT=3.5,
        DEMAND_MIN_CLOSE_POSITION=0.45,
    )

    # Base datar 14 candle di 100, kaki naik 6 candle sampai 103.6, lalu sinyal
    # di 104.15. Sinyal ini +3,43% dari atap dasar versi 6 candle (lolos) tapi
    # +3,53% dari atap dasar versi 20 candle (ditolak): hanya batas 3,5% yang
    # membedakan keduanya, jadi selisihnya murni akibat lookback.
    datar = [_k(99.9, 100.2, 99.8, 100.0, i) for i in range(14)]
    kaki = []
    for j in range(6):
        o = 100.0 + 0.6 * j
        c = o + 0.6
        kaki.append(_k(o, c + 0.1, o - 0.1, c, 14 + j))
    sinyal = _k(103.8, 104.35, 103.7, 104.15, 20)
    seri = datar + kaki + [sinyal]

    ketat = detect_demand_zone(seri, dict(cfg_dem, DEMAND_LOOKBACK_BARS=20))
    longgar = detect_demand_zone(seri, dict(cfg_dem, DEMAND_LOOKBACK_BARS=6))
    cek(
        "DEMAND_LOOKBACK_BARS mengubah atap zona (knob tidak mati)",
        abs(ketat["zone_high"] - 100.5984) < 1e-9
        and abs(longgar["zone_high"] - 100.6992) < 1e-9,
        f"lookback 20 -> atap {ketat['zone_high']:.4f}, "
        f"lookback 6 -> {longgar['zone_high']:.4f}",
    )
    cek(
        "lookback panjang memblokir entry yang sudah di kaki naik",
        (not ketat["ok"]) and "terlalu jauh" in ketat["reason"],
        ketat["reason"],
    )
    cek(
        "lookback pendek memetakan dasar terbaru sehingga entry sama lolos",
        longgar["ok"] and abs(longgar["distance_pct"] - 3.43) < 0.01,
        longgar["reason"],
    )

    # Sumbu panjang sesaat (flash dump) bukan support: dasarnya dipotong ke pita
    # buffer, bukan ditetapkan di dasar jurang.
    sumur = [_k(99.9, 100.2, 99.8, 100.0, i) for i in range(20)]
    sumur[10] = _k(99.9, 100.2, 80.0, 99.9, 10)
    spike = detect_demand_zone(
        sumur + [_k(99.9, 100.2, 99.8, 100.0, 20), _k(100.0, 100.6, 99.9, 100.5, 21)],
        cfg_dem,
    )
    cek(
        "sumbu panjang sendirian tidak dijadikan dasar demand",
        spike["ok"] and spike["zone_low"] is not None and spike["zone_low"] > 99.0,
        f"dasar {spike['zone_low']:.4f} (bukan 80.0) -> {spike['reason'][:52]}",
    )
    cek(
        "data kurang tetap ditolak (fail closed)",
        not detect_demand_zone(sumur[:5], cfg_dem)["ok"],
    )
    cek(
        "filter nonaktif selalu lolos",
        detect_demand_zone([], dict(cfg_dem, DEMAND_ZONE_FILTER_ENABLED=False))["ok"],
    )
    cek(
        "batas atas zona tidak pernah di bawah dasarnya (invarian)",
        all(
            (r["zone_high"] is None) or r["zone_high"] >= r["zone_low"]
            for r in (
                detect_demand_zone(seri, dict(cfg_dem, DEMAND_LOOKBACK_BARS=b))
                for b in range(3, 21)
            )
        )
    )

    print(
        "HASIL SELFTEST indicators: "
        + ("SEMUA LULUS" if not gagal else f"{gagal} GAGAL")
    )
    return 0 if not gagal else 1


def _gagal_tertangkap(fn) -> bool:
    try:
        fn()
    except (ValueError, TypeError):
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(selftest())
