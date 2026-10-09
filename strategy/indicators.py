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


HTF_DEMAND_DEFAULTS = {
    "HTF_DEMAND_LOOKBACK_BARS": 72,
    "HTF_DEMAND_ZONE_BUFFER_PCT": 1.5,
    "HTF_DEMAND_MAX_DISTANCE_PCT": 10.0,
    "HTF_DEMAND_MIN_CLOSE_POSITION": 0.40,
}


def htf_demand_enabled(config: dict) -> bool:
    return bool(config.get("HTF_DEMAND_FILTER_ENABLED", False))


def htf_demand_lookback_bars(config: dict) -> int:
    """Lookback gerbang demand HTF, dijepit ke rentang yang bisa diunduh."""
    lookback = int(
        config.get(
            "HTF_DEMAND_LOOKBACK_BARS",
            HTF_DEMAND_DEFAULTS["HTF_DEMAND_LOOKBACK_BARS"],
        )
        or HTF_DEMAND_DEFAULTS["HTF_DEMAND_LOOKBACK_BARS"]
    )
    return max(3, min(TREND_KLINE_LIMIT - 2, lookback))


def htf_demand_window_bars(config: dict) -> int:
    """Candle HTF minimum untuk gerbang zona demand (0 kalau nonaktif)."""
    if not htf_demand_enabled(config):
        return 0
    return htf_demand_lookback_bars(config) + 1


def htf_window_bars(config: dict) -> int:
    """Jendela gabungan penyedia candle HTF (gerbang trend + gerbang demand).

    evaluate_trend_filter memotong sendiri jendelanya (TREND_LOOKBACK_BARS),
    jadi melebarkan unduhan/cache demi gerbang demand TIDAK mengubah verdict
    EMA/ADX. Bot live (TrendCache) dan backtest (TrendLookup) sama-sama
    memakai fungsi ini supaya jendela keduanya selalu identik.
    """
    return max(trend_window_bars(config), htf_demand_window_bars(config))


def htf_demand_gate_config(config: dict) -> dict:
    """Terjemahkan kunci HTF_DEMAND_* ke kunci DEMAND_* milik detect_demand_zone.

    Logika zona tidak diduplikasi: gerbang timeframe tinggi memanggil fungsi
    yang sama persis dengan gerbang chart (M5), hanya parameternya yang
    berbeda, sehingga keputusan LIVE, PAPER, dan backtest otomatis identik.
    """
    return {
        "DEMAND_ZONE_FILTER_ENABLED": htf_demand_enabled(config),
        "DEMAND_LOOKBACK_BARS": htf_demand_lookback_bars(config),
        "DEMAND_ZONE_BUFFER_PCT": max(
            0.0,
            float(
                config.get(
                    "HTF_DEMAND_ZONE_BUFFER_PCT",
                    HTF_DEMAND_DEFAULTS["HTF_DEMAND_ZONE_BUFFER_PCT"],
                )
                or 0.0
            ),
        ),
        "DEMAND_MAX_DISTANCE_PCT": max(
            0.0,
            float(
                config.get(
                    "HTF_DEMAND_MAX_DISTANCE_PCT",
                    HTF_DEMAND_DEFAULTS["HTF_DEMAND_MAX_DISTANCE_PCT"],
                )
                or 0.0
            ),
        ),
        "DEMAND_MIN_CLOSE_POSITION": float(
            config.get(
                "HTF_DEMAND_MIN_CLOSE_POSITION",
                HTF_DEMAND_DEFAULTS["HTF_DEMAND_MIN_CLOSE_POSITION"],
            )
            or 0.0
        ),
    }


def evaluate_htf_demand(
    klines: list[Kline], config: dict, signal_close_time_ms: Optional[int] = None
) -> dict:
    """Gerbang zona demand timeframe tinggi (default H1) untuk menyaring entry.

    Melengkapi gerbang demand chart (M5): entry hanya lolos kalau candle HTF
    terakhir yang SUDAH TUTUP juga bereaksi di dekat zona demand HTF (dasar
    akumulasi timeframe tinggi), bukan sedang melayang jauh di atasnya.
    Aturan zonanya identik dengan detect_demand_zone (satu sumber kode),
    hanya parameternya yang diambil dari kunci HTF_DEMAND_*:

      * HTF_DEMAND_LOOKBACK_BARS      (default 72 candle H1 = 3 hari struktur)
      * HTF_DEMAND_ZONE_BUFFER_PCT    (default 1.5%)
      * HTF_DEMAND_MAX_DISTANCE_PCT   (default 10%, diukur dari atap zona)
      * HTF_DEMAND_MIN_CLOSE_POSITION (default 0.40)

    Fail closed seperti gerbang trend: data kurang atau OHLC tidak valid
    menolak entry, bukan meloloskannya. Candle yang belum tutup dibuang di
    sini walaupun pemanggil lupa menyaringnya (anti repaint), sama seperti
    evaluate_trend_filter. Saat HTF_DEMAND_FILTER_ENABLED dimatikan, fungsi
    selalu lolos tanpa membaca candle sama sekali.
    """
    interval = str(config.get("TREND_INTERVAL", "1h") or "1h").strip().lower()
    wajib = htf_demand_window_bars(config)
    if not htf_demand_enabled(config):
        return {
            "ok": True,
            "reason": f"filter demand {interval} nonaktif",
            "interval": interval,
            "bars": 0,
            "required": 0,
            "zone_low": None,
            "zone_high": None,
            "distance_pct": None,
            "close_position": None,
        }
    siap = [
        k
        for k in (klines or [])
        if signal_close_time_ms is None
        or int(k.close_time) <= int(signal_close_time_ms)
    ]
    hasil = detect_demand_zone(siap, htf_demand_gate_config(config))
    hasil["interval"] = interval
    hasil["bars"] = len(siap)
    hasil["required"] = wajib
    return hasil


DAILY_DEMAND_DEFAULTS = {
    "DAILY_DEMAND_INTERVAL": "1d",
    "DAILY_DEMAND_LOOKBACK_BARS": 20,
    "DAILY_DEMAND_ZONE_BUFFER_PCT": 2.0,
    "DAILY_DEMAND_MAX_DISTANCE_PCT": 12.0,
    "DAILY_DEMAND_MIN_CLOSE_POSITION": 0.40,
}


def daily_demand_interval(config: dict) -> str:
    """Interval gerbang demand harian (default 1d)."""
    raw = str(
        config.get("DAILY_DEMAND_INTERVAL", DAILY_DEMAND_DEFAULTS["DAILY_DEMAND_INTERVAL"])
        or DAILY_DEMAND_DEFAULTS["DAILY_DEMAND_INTERVAL"]
    ).strip().lower()
    if raw not in INTERVAL_MINUTES:
        raise ValueError(
            f"DAILY_DEMAND_INTERVAL '{raw}' tidak dikenal. Pilihan: "
            + ", ".join(sorted(INTERVAL_MINUTES, key=INTERVAL_MINUTES.get))
        )
    return raw


def daily_demand_interval_minutes(config: dict) -> int:
    return int(INTERVAL_MINUTES[daily_demand_interval(config)])


def daily_demand_enabled(config: dict) -> bool:
    return bool(config.get("DAILY_DEMAND_FILTER_ENABLED", False))


def daily_demand_lookback_bars(config: dict) -> int:
    """Lookback gerbang demand harian, dijepit ke rentang yang bisa diunduh."""
    lookback = int(
        config.get(
            "DAILY_DEMAND_LOOKBACK_BARS",
            DAILY_DEMAND_DEFAULTS["DAILY_DEMAND_LOOKBACK_BARS"],
        )
        or DAILY_DEMAND_DEFAULTS["DAILY_DEMAND_LOOKBACK_BARS"]
    )
    return max(3, min(TREND_KLINE_LIMIT - 2, lookback))


def daily_demand_window_bars(config: dict) -> int:
    """Candle harian minimum untuk gerbang zona demand (0 kalau nonaktif)."""
    if not daily_demand_enabled(config):
        return 0
    return daily_demand_lookback_bars(config) + 1


def daily_demand_gate_config(config: dict) -> dict:
    """Terjemahkan kunci DAILY_DEMAND_* ke kunci DEMAND_* milik detect_demand_zone.

    Sama seperti gerbang demand H1: logika zona tidak diduplikasi, fungsinya
    yang dipanggil ulang dengan parameter berbeda. Itu yang menjaga keputusan
    LIVE, PAPER, dan backtest tetap identik tanpa tiga salinan aturan.
    """
    return {
        "DEMAND_ZONE_FILTER_ENABLED": daily_demand_enabled(config),
        "DEMAND_LOOKBACK_BARS": daily_demand_lookback_bars(config),
        "DEMAND_ZONE_BUFFER_PCT": max(
            0.0,
            float(
                config.get(
                    "DAILY_DEMAND_ZONE_BUFFER_PCT",
                    DAILY_DEMAND_DEFAULTS["DAILY_DEMAND_ZONE_BUFFER_PCT"],
                )
                or 0.0
            ),
        ),
        "DEMAND_MAX_DISTANCE_PCT": max(
            0.0,
            float(
                config.get(
                    "DAILY_DEMAND_MAX_DISTANCE_PCT",
                    DAILY_DEMAND_DEFAULTS["DAILY_DEMAND_MAX_DISTANCE_PCT"],
                )
                or 0.0
            ),
        ),
        "DEMAND_MIN_CLOSE_POSITION": float(
            config.get(
                "DAILY_DEMAND_MIN_CLOSE_POSITION",
                DAILY_DEMAND_DEFAULTS["DAILY_DEMAND_MIN_CLOSE_POSITION"],
            )
            or 0.0
        ),
    }


def evaluate_daily_demand(
    klines: list[Kline], config: dict, signal_close_time_ms: Optional[int] = None
) -> dict:
    """Gerbang zona demand harian (default 1d) sebagai lapisan ketiga konfirmasi.

    Melengkapi gerbang demand chart (M5) dan gerbang demand H1: entry hanya lolos
    kalau candle harian terakhir yang SUDAH TUTUP juga bereaksi di dekat dasar
    akumulasi harian, bukan sedang melayang jauh di atasnya. Aturan zonanya
    identik dengan detect_demand_zone (satu sumber kode), hanya parameternya yang
    diambil dari kunci DAILY_DEMAND_*:

      * DAILY_DEMAND_INTERVAL          (default 1d)
      * DAILY_DEMAND_LOOKBACK_BARS     (default 20 hari struktur harga)
      * DAILY_DEMAND_ZONE_BUFFER_PCT   (default 2.0%)
      * DAILY_DEMAND_MAX_DISTANCE_PCT  (default 12%, diukur dari atap zona)
      * DAILY_DEMAND_MIN_CLOSE_POSITION (default 0.40)

    Candle harian jauh lebih lebar daripada H1, jadi ambang jarak dan tebal zona
    memang lebih longgar; itu sebabnya parameter harian dipisahkan dari H1
    walau mesin zonanya sama.

    Fail closed seperti gerbang lain: data kurang, OHLC tidak valid, atau candle
    gagal diambil berarti entry DITOLAK. Candle yang belum tutup dibuang di sini
    walaupun pemanggil lupa menyaringnya (anti repaint). Saat
    DAILY_DEMAND_FILTER_ENABLED dimatikan, fungsi selalu lolos tanpa membaca
    candle sama sekali.
    """
    interval = daily_demand_interval(config)
    wajib = daily_demand_window_bars(config)
    if not daily_demand_enabled(config):
        return {
            "ok": True,
            "reason": f"filter demand {interval} nonaktif",
            "interval": interval,
            "bars": 0,
            "required": 0,
            "zone_low": None,
            "zone_high": None,
            "distance_pct": None,
            "close_position": None,
        }
    siap = [
        k
        for k in (klines or [])
        if signal_close_time_ms is None
        or int(k.close_time) <= int(signal_close_time_ms)
    ]
    hasil = detect_demand_zone(siap, daily_demand_gate_config(config))
    hasil["interval"] = interval
    hasil["bars"] = len(siap)
    hasil["required"] = wajib
    return hasil


def daily_warmup_bars(config: dict, interval: str) -> int:
    """Candle interval simulasi yang wajib ada untuk gerbang demand harian (0 kalau nonaktif).

    Candle harian dirangkai dari candle interval simulasi (default 288 candle 5m
    per hari), jadi warmup backtest harus menutupi
    (lookback harian + 1 candle sinyal) x rasio + satu bucket cadangan.
    """
    if not daily_demand_enabled(config):
        return 0
    sim_minutes = int(INTERVAL_MINUTES.get(str(interval), 5))
    daily_minutes = daily_demand_interval_minutes(config)
    if daily_minutes % sim_minutes:
        raise ValueError(
            f"DAILY_DEMAND_INTERVAL '{daily_demand_interval(config)}' ({daily_minutes} "
            f"menit) harus kelipatan bulat dari interval simulasi '{interval}' "
            f"({sim_minutes} menit) supaya backtest bisa merangkai candle harian dari "
            "data yang sudah diunduh."
        )
    rasio = daily_minutes // sim_minutes
    return daily_demand_window_bars(config) * rasio + rasio


def htf_gate_warmup_bars(config: dict, interval: str) -> int:
    """Warmup gabungan SEMUA gerbang timeframe tinggi yang aktif (0 kalau tidak ada).

    Gerbang trend dan gerbang demand H1 memakai interval TREND_INTERVAL yang
    sama, sedangkan gerbang demand harian memakai interval sendiri. Ketiganya
    membaca dari awal rentang data yang sama, jadi kebutuhan yang dipakai adalah
    yang TERBESAR, bukan jumlahnya. Tanpa ini, backtest bisa mulai mengevaluasi
    bar sebelum candle harian cukup, dan hasilnya nol trade tanpa penjelasan.
    """
    butuh = 0
    if bool(config.get("TREND_FILTER_ENABLED", False)) or htf_demand_enabled(config):
        butuh = max(butuh, trend_warmup_bars(config, interval))
    if daily_demand_enabled(config):
        butuh = max(butuh, daily_warmup_bars(config, interval))
    if daily_trend_enabled(config):
        butuh = max(butuh, daily_trend_warmup_bars(config, interval))
    return butuh


def trend_warmup_bars(config: dict, interval: str) -> int:
    """Candle interval simulasi yang wajib tersedia sebelum bar entry pertama.

    Backtest membentuk candle timeframe tinggi dengan merangkai candle
    interval simulasi, jadi warmup harus menutupi (jumlah candle HTF pada
    jendela gabungan trend + demand + 1 bucket) x rasio.
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
    return htf_window_bars(config) * rasio + rasio


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
    klines: list[Kline],
    config: dict,
    signal_close_time_ms: Optional[int] = None,
    nama_kunci_jendela: str = "TREND_LOOKBACK_BARS",
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
                f"{minimum} (jendela {jendela_penuh}; naikkan {nama_kunci_jendela} "
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
# Gerbang EMA + ADX timeframe harian (DAILY_TREND_*)
#
# Lapisan TAMBAHAN di atas gerbang trend H1 dan gerbang demand. Aturannya
# identik dengan gerbang trend H1 (close > EMA cepat, EMA cepat > EMA lambat,
# ADX >= ambang), hanya saja candlenya harian dan parameternya dipisah.
#
# Rumus EMA dan ADX TIDAK diduplikasi: kunci DAILY_TREND_* diterjemahkan ke
# kunci TREND_* lalu memanggil evaluate_trend_filter yang sama persis. Dengan
# begitu bot live, PAPER, backtest, dan grid search memakai angka yang sama.
#
# Default NONAKTIF (DAILY_TREND_FILTER_ENABLED=False).
# ---------------------------------------------------------------------------

DAILY_TREND_DEFAULTS = {
    "DAILY_TREND_FILTER_ENABLED": False,
    "DAILY_TREND_INTERVAL": "1d",
    "DAILY_TREND_EMA_FAST": 20,
    "DAILY_TREND_EMA_SLOW": 50,
    "DAILY_TREND_ADX_PERIOD": 14,
    "DAILY_TREND_ADX_MIN": 20.0,
    "DAILY_TREND_LOOKBACK_BARS": 120,
}


def daily_trend_enabled(config: dict) -> bool:
    return bool(config.get("DAILY_TREND_FILTER_ENABLED", False))


def daily_trend_interval(config: dict) -> str:
    """Interval gerbang EMA+ADX harian (default 1d). Melempar ValueError kalau tidak dikenal."""
    raw = str(
        config.get("DAILY_TREND_INTERVAL", DAILY_TREND_DEFAULTS["DAILY_TREND_INTERVAL"])
        or DAILY_TREND_DEFAULTS["DAILY_TREND_INTERVAL"]
    ).strip().lower()
    if raw not in INTERVAL_MINUTES:
        raise ValueError(
            f"DAILY_TREND_INTERVAL '{raw}' tidak dikenal. Pilihan: "
            + ", ".join(sorted(INTERVAL_MINUTES, key=INTERVAL_MINUTES.get))
        )
    return raw


def daily_trend_interval_minutes(config: dict) -> int:
    return int(INTERVAL_MINUTES[daily_trend_interval(config)])


def daily_trend_gate_config(config: dict) -> dict:
    """Terjemahkan kunci DAILY_TREND_* ke kunci TREND_* milik evaluate_trend_filter.

    Hanya dipanggil saat gerbang aktif (lihat evaluate_daily_trend), jadi
    DAILY_TREND_INTERVAL yang salah tidak mengganggu bot saat gerbang mati.
    """
    d = DAILY_TREND_DEFAULTS
    return {
        "TREND_FILTER_ENABLED": daily_trend_enabled(config),
        "TREND_INTERVAL": daily_trend_interval(config),
        "TREND_EMA_FAST": int(config.get("DAILY_TREND_EMA_FAST", d["DAILY_TREND_EMA_FAST"]) or d["DAILY_TREND_EMA_FAST"]),
        "TREND_EMA_SLOW": int(config.get("DAILY_TREND_EMA_SLOW", d["DAILY_TREND_EMA_SLOW"]) or d["DAILY_TREND_EMA_SLOW"]),
        "TREND_ADX_PERIOD": int(config.get("DAILY_TREND_ADX_PERIOD", d["DAILY_TREND_ADX_PERIOD"]) or d["DAILY_TREND_ADX_PERIOD"]),
        "TREND_ADX_MIN": float(config.get("DAILY_TREND_ADX_MIN", d["DAILY_TREND_ADX_MIN"]) or 0.0),
        "TREND_LOOKBACK_BARS": int(config.get("DAILY_TREND_LOOKBACK_BARS", d["DAILY_TREND_LOOKBACK_BARS"]) or d["DAILY_TREND_LOOKBACK_BARS"]),
    }


def daily_trend_window_bars(config: dict) -> int:
    """Jumlah candle harian tertutup yang dipakai gerbang EMA+ADX (0 kalau nonaktif)."""
    if not daily_trend_enabled(config):
        return 0
    return trend_window_bars(daily_trend_gate_config(config))


def daily_trend_required_bars(config: dict) -> int:
    """Candle harian minimum agar EMA dan ADX harian terdefinisi (0 kalau nonaktif)."""
    if not daily_trend_enabled(config):
        return 0
    return trend_required_bars(daily_trend_gate_config(config))


def daily_trend_warmup_bars(config: dict, interval: str) -> int:
    """Candle interval simulasi yang wajib ada sebelum bar entry pertama (0 kalau nonaktif).

    Candle harian dirangkai dari candle interval simulasi (mis. 288 candle 5m
    per hari), jadi warmup menutupi (jendela harian + 1 bucket) x rasio.
    """
    if not daily_trend_enabled(config):
        return 0
    sim_minutes = int(INTERVAL_MINUTES.get(str(interval), 5))
    daily_minutes = daily_trend_interval_minutes(config)
    if daily_minutes % sim_minutes:
        raise ValueError(
            f"DAILY_TREND_INTERVAL '{daily_trend_interval(config)}' ({daily_minutes} "
            f"menit) harus kelipatan bulat dari interval simulasi '{interval}' "
            f"({sim_minutes} menit) supaya backtest bisa merangkai candle harian dari "
            "data yang sudah diunduh."
        )
    rasio = daily_minutes // sim_minutes
    return daily_trend_window_bars(config) * rasio + rasio


def evaluate_daily_trend(
    klines: list[Kline], config: dict, signal_close_time_ms: Optional[int] = None
) -> dict:
    """Gerbang EMA+ADX timeframe harian sebagai lapisan konfirmasi tambahan.

    Aturan (dari candle harian yang SUDAH TUTUP, tanpa repaint):
      1. close candle harian terakhir > EMA cepat (default 20),
      2. EMA cepat > EMA lambat (default 50),
      3. ADX(14) >= DAILY_TREND_ADX_MIN (default 20; 0 mematikan cek kekuatan).

    Semua logika dan fail-closed-nya dipakai ulang dari evaluate_trend_filter.
    Saat DAILY_TREND_FILTER_ENABLED dimatikan, fungsi langsung lolos tanpa
    membaca candle sama sekali.
    """
    if not daily_trend_enabled(config):
        raw = str(config.get("DAILY_TREND_INTERVAL", "1d") or "1d").strip().lower()
        return {
            "ok": True,
            "reason": f"filter trend {raw} nonaktif",
            "interval": raw,
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
    return evaluate_trend_filter(
        klines,
        daily_trend_gate_config(config),
        signal_close_time_ms,
        nama_kunci_jendela="DAILY_TREND_LOOKBACK_BARS",
    )


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

    # --- Gerbang demand timeframe tinggi (H1): logika zona sama, parameter sendiri ---
    cfg_htf = dict(
        HTF_DEMAND_FILTER_ENABLED=True,
        HTF_DEMAND_LOOKBACK_BARS=6,
        HTF_DEMAND_ZONE_BUFFER_PCT=1.0,
        HTF_DEMAND_MAX_DISTANCE_PCT=8.0,
        HTF_DEMAND_MIN_CLOSE_POSITION=0.4,
        TREND_INTERVAL="1h",
    )

    def _k_jam(o, h, l, c, i=0):
        return Kline(
            i * 3_600_000, o, h, l, c, i * 3_600_000 + 3_599_999, 1000.0, 1000.0 * c
        )

    dasar_jam = [_k_jam(99.9, 100.2, 99.8, 100.0, i) for i in range(10)]
    reaksi_jam = dasar_jam + [_k_jam(100.0, 101.2, 99.9, 101.0, 10)]
    lolos_htf = evaluate_htf_demand(reaksi_jam, cfg_htf)
    cek(
        "reaksi hijau segar di dekat zona demand H1 lolos",
        lolos_htf["ok"] and lolos_htf["interval"] == "1h",
        lolos_htf["reason"],
    )
    banding = detect_demand_zone(reaksi_jam, htf_demand_gate_config(cfg_htf))
    cek(
        "gerbang H1 identik dengan detect_demand_zone berparameter terpetakan",
        lolos_htf["ok"] == banding["ok"]
        and lolos_htf["zone_low"] == banding["zone_low"]
        and lolos_htf["zone_high"] == banding["zone_high"]
        and lolos_htf["distance_pct"] == banding["distance_pct"],
    )
    melambung = list(dasar_jam)
    for j in range(3):
        o = 100.0 + 5.0 * (j + 1)
        melambung.append(_k_jam(o, o + 1.0, o - 0.5, o + 0.5, 10 + j))
    jauh_htf = evaluate_htf_demand(melambung, cfg_htf)
    cek(
        "harga yang sudah terbang jauh di atas dasar H1 ditolak (anti pucuk)",
        (not jauh_htf["ok"]) and "terlalu jauh" in jauh_htf["reason"],
        jauh_htf["reason"],
    )
    cek(
        "data H1 kurang tetap ditolak (fail closed)",
        not evaluate_htf_demand(dasar_jam[:3], cfg_htf)["ok"],
    )
    mati_htf = evaluate_htf_demand([], dict(cfg_htf, HTF_DEMAND_FILTER_ENABLED=False))
    cek(
        "gerbang H1 nonaktif selalu lolos tanpa membaca candle",
        mati_htf["ok"] and "nonaktif" in mati_htf["reason"],
        mati_htf["reason"],
    )
    belum_tutup = reaksi_jam + [_k_jam(101.0, 120.0, 100.9, 119.0, 11)]
    anti_repaint = evaluate_htf_demand(
        belum_tutup, cfg_htf, signal_close_time_ms=reaksi_jam[-1].close_time
    )
    cek(
        "candle H1 yang belum tutup tidak dipakai (anti repaint)",
        anti_repaint["ok"] and anti_repaint["bars"] == len(reaksi_jam),
        f"bars={anti_repaint['bars']}",
    )
    cek(
        "jendela HTF gabungan mengikuti lookback terbesar (trend vs demand)",
        htf_window_bars(dict(cfg_htf, TREND_LOOKBACK_BARS=120)) == 120
        and htf_window_bars(dict(cfg_htf, HTF_DEMAND_LOOKBACK_BARS=200)) == 201,
        f"{htf_window_bars(dict(cfg_htf, TREND_LOOKBACK_BARS=120))} / "
        f"{htf_window_bars(dict(cfg_htf, HTF_DEMAND_LOOKBACK_BARS=200))}",
    )


    # --- Gerbang demand harian (D1): mesin zona sama, parameter sendiri ---
    cfg_daily = dict(
        DAILY_DEMAND_FILTER_ENABLED=True,
        DAILY_DEMAND_INTERVAL="1d",
        DAILY_DEMAND_LOOKBACK_BARS=3,
        DAILY_DEMAND_ZONE_BUFFER_PCT=1.0,
        DAILY_DEMAND_MAX_DISTANCE_PCT=8.0,
        DAILY_DEMAND_MIN_CLOSE_POSITION=0.4,
    )

    def _k_hari(o, h, l, c, i=0):
        return Kline(
            i * 86_400_000, o, h, l, c, i * 86_400_000 + 86_399_999, 1000.0, 1000.0 * c
        )

    cek(
        "interval harian default 1d dan jendelanya lookback + 1 candle sinyal",
        daily_demand_interval(cfg_daily) == "1d"
        and daily_demand_interval_minutes(cfg_daily) == 1440
        and daily_demand_window_bars(cfg_daily) == 4,
    )
    cek(
        "warmup harian = (lookback + 1) hari x 288 candle 5m + 1 bucket",
        daily_warmup_bars(cfg_daily, "5m") == 4 * 288 + 288
        and htf_gate_warmup_bars(cfg_daily, "5m") == 4 * 288 + 288,
    )
    cek(
        "warmup gabungan mengambil kebutuhan TERBESAR, bukan jumlahnya",
        htf_gate_warmup_bars(
            dict(cfg_daily, TREND_FILTER_ENABLED=True, TREND_INTERVAL="1h", TREND_LOOKBACK_BARS=120),
            "5m",
        )
        == max(4 * 288 + 288, 120 * 12 + 12),
    )
    cek(
        "gerbang harian nonaktif selalu lolos dan warmupnya nol",
        evaluate_daily_demand([], dict(cfg_daily, DAILY_DEMAND_FILTER_ENABLED=False))["ok"]
        and daily_warmup_bars(dict(cfg_daily, DAILY_DEMAND_FILTER_ENABLED=False), "5m") == 0
        and htf_gate_warmup_bars(
            dict(cfg_daily, DAILY_DEMAND_FILTER_ENABLED=False), "5m"
        )
        == 0,
    )
    cek(
        "data harian kurang tetap ditolak (fail closed)",
        not evaluate_daily_demand(
            [_k_hari(100.0, 100.5, 99.5, 100.0, i) for i in range(3)], cfg_daily
        )["ok"],
    )
    cek(
        "interval harian tak dikenal ditolak",
        _gagal_tertangkap(lambda: daily_demand_interval(dict(cfg_daily, DAILY_DEMAND_INTERVAL="2d"))),
    )
    cek(
        "interval harian yang bukan kelipatan interval simulasi ditolak",
        _gagal_tertangkap(
            lambda: daily_warmup_bars(dict(cfg_daily, DAILY_DEMAND_INTERVAL="12h"), "8h")
        ),
    )
    cek(
        "warmup harian mengikuti interval yang dipilih (12h dengan simulasi 1h)",
        daily_warmup_bars(dict(cfg_daily, DAILY_DEMAND_INTERVAL="12h"), "1h") == 4 * 12 + 12,
    )

    dasar_hari = [_k_hari(100.0, 100.2, 99.8, 100.0, i) for i in range(3)]
    reaksi_hari = dasar_hari + [_k_hari(100.0, 100.8, 99.9, 100.7, 3)]
    lolos_harian = evaluate_daily_demand(reaksi_hari, cfg_daily)
    cek(
        "reaksi hijau segar di dekat dasar harian lolos",
        lolos_harian["ok"] and lolos_harian["interval"] == "1d",
        lolos_harian["reason"],
    )
    banding_harian = detect_demand_zone(reaksi_hari, daily_demand_gate_config(cfg_daily))
    cek(
        "gerbang harian identik dengan detect_demand_zone berparameter terpetakan",
        lolos_harian["ok"] == banding_harian["ok"]
        and lolos_harian["zone_low"] == banding_harian["zone_low"]
        and lolos_harian["zone_high"] == banding_harian["zone_high"]
        and lolos_harian["distance_pct"] == banding_harian["distance_pct"],
    )
    jauh_harian = evaluate_daily_demand(
        dasar_hari + [_k_hari(100.0, 110.5, 99.9, 110.0, 3)], cfg_daily
    )
    cek(
        "harga yang sudah terbang jauh di atas dasar harian ditolak (anti pucuk)",
        (not jauh_harian["ok"]) and "terlalu jauh" in jauh_harian["reason"],
        jauh_harian["reason"],
    )
    belum_tutup_harian = reaksi_hari + [_k_hari(100.7, 120.0, 100.6, 119.0, 4)]
    anti_repaint_harian = evaluate_daily_demand(
        belum_tutup_harian, cfg_daily, signal_close_time_ms=reaksi_hari[-1].close_time
    )
    cek(
        "candle harian yang belum tutup tidak dipakai (anti repaint)",
        anti_repaint_harian["ok"] and anti_repaint_harian["bars"] == len(reaksi_hari),
        f"bars={anti_repaint_harian['bars']}",
    )
    cek(
        "jendela harian dipakai persis lookback + 1 candle",
        evaluate_daily_demand(
            [_k_hari(100.0, 100.2, 99.8, 100.0, i) for i in range(10)], cfg_daily
        )["required"]
        == 4,
    )

    # --- gerbang EMA + ADX harian (DAILY_TREND_*) ---
    def _hari_deret(a: float, n: int = 130) -> list[Kline]:
        out = []
        p = 100.0
        for i in range(n):
            p *= a
            out.append(
                Kline(
                    i * 86_400_000,
                    p,
                    p * 1.005,
                    p * 0.995,
                    p,
                    i * 86_400_000 + 86_399_999,
                    1000.0,
                    1000.0 * p,
                )
            )
        return out

    cfg_dtrend = dict(
        DAILY_TREND_FILTER_ENABLED=True,
        **{k: v for k, v in DAILY_TREND_DEFAULTS.items() if k != "DAILY_TREND_FILTER_ENABLED"},
    )
    cfg_dtrend_off = dict(cfg_dtrend, DAILY_TREND_FILTER_ENABLED=False)
    cek(
        "default harian: interval 1d, EMA 20/50, ADX 14 min 20, jendela 120",
        daily_trend_interval(cfg_dtrend) == "1d"
        and daily_trend_gate_config(cfg_dtrend)["TREND_EMA_FAST"] == 20
        and daily_trend_gate_config(cfg_dtrend)["TREND_EMA_SLOW"] == 50
        and daily_trend_gate_config(cfg_dtrend)["TREND_ADX_PERIOD"] == 14
        and daily_trend_gate_config(cfg_dtrend)["TREND_ADX_MIN"] == 20.0
        and daily_trend_window_bars(cfg_dtrend) == 120,
    )
    lolos_nonaktif = evaluate_daily_trend([], cfg_dtrend_off)
    cek(
        "gerbang harian nonaktif lolos tanpa membaca candle",
        lolos_nonaktif["ok"] and lolos_nonaktif["bars"] == 0,
        lolos_nonaktif["reason"],
    )
    naik_harian = evaluate_daily_trend(_hari_deret(1.01), cfg_dtrend)
    cek(
        "trend harian naik dan kuat diloloskan",
        naik_harian["ok"] and naik_harian["interval"] == "1d",
        naik_harian["reason"],
    )
    turun_harian = evaluate_daily_trend(_hari_deret(0.99), cfg_dtrend)
    cek(
        "trend harian turun ditolak",
        (not turun_harian["ok"]) and "di bawah" in turun_harian["reason"],
        turun_harian["reason"],
    )
    # Deret tren murni menghasilkan ADX tepat 100, jadi deret berombak dipakai
    # supaya ADX berada di bawah 100 dan ambangnya benar-benar bisa menolak.
    def _hari_berombak(n: int = 130) -> list[Kline]:
        out = []
        p = 100.0
        for i in range(n):
            p *= 1.03 if i % 2 == 0 else 0.99
            out.append(
                Kline(
                    i * 86_400_000,
                    p,
                    p * 1.01,
                    p * 0.99,
                    p,
                    i * 86_400_000 + 86_399_999,
                    1000.0,
                    1000.0 * p,
                )
            )
        return out

    # Ambang 0 sengaja melewati perhitungan ADX, jadi ambang kecil di atas nol dipakai.
    adx_berombak = evaluate_daily_trend(
        _hari_berombak(), dict(cfg_dtrend, DAILY_TREND_ADX_MIN=0.0001)
    )["adx"]
    lemah_harian = evaluate_daily_trend(
        _hari_berombak(), dict(cfg_dtrend, DAILY_TREND_ADX_MIN=(adx_berombak or 0.0) + 1.0)
    )
    cek(
        "ADX harian di bawah ambang ditolak",
        adx_berombak is not None
        and adx_berombak < 99.0
        and (not lemah_harian["ok"])
        and "lemah" in lemah_harian["reason"],
        f"ADX={adx_berombak} | {lemah_harian['reason']}",
    )
    kuat_harian = evaluate_daily_trend(
        _hari_berombak(), dict(cfg_dtrend, DAILY_TREND_ADX_MIN=(adx_berombak or 0.0) - 1.0)
    )
    cek(
        "ADX harian tepat di atas ambang tidak ditolak oleh cek ADX",
        "lemah" not in kuat_harian["reason"],
        kuat_harian["reason"],
    )
    cek(
        "ADX_MIN 0 mematikan cek kekuatan, susunan EMA saja dipakai",
        evaluate_daily_trend(
            _hari_deret(1.01), dict(cfg_dtrend, DAILY_TREND_ADX_MIN=0.0)
        )["ok"],
    )
    kurang_harian = evaluate_daily_trend(_hari_deret(1.01, n=30), cfg_dtrend)
    cek(
        "riwayat harian kurang dari minimum EMA50 ditolak (fail closed)",
        (not kurang_harian["ok"]) and "kurang" in kurang_harian["reason"],
        kurang_harian["reason"],
    )
    seri_belum_tutup = _hari_deret(1.01, n=130)
    tutup_terakhir = seri_belum_tutup[-2].close_time
    # Candle terakhir sengaja dibuat ekstrem (turun tajam). Kalau ikut terbaca,
    # close dan EMA akan berubah. Hasilnya harus identik dengan deret tanpa candle itu.
    seri_belum_tutup[-1] = Kline(
        seri_belum_tutup[-1].open_time,
        seri_belum_tutup[-1].open,
        seri_belum_tutup[-1].high,
        seri_belum_tutup[-1].low,
        seri_belum_tutup[-1].low * 0.5,
        seri_belum_tutup[-1].close_time,
        1000.0,
        1000.0,
    )
    anti_repaint_tren = evaluate_daily_trend(
        seri_belum_tutup, cfg_dtrend, signal_close_time_ms=tutup_terakhir
    )
    tanpa_candle_berjalan = evaluate_daily_trend(
        seri_belum_tutup[:-1], cfg_dtrend
    )
    cek(
        "candle harian yang belum tutup diabaikan (anti repaint)",
        anti_repaint_tren["ok"] == tanpa_candle_berjalan["ok"]
        and anti_repaint_tren["values"] == tanpa_candle_berjalan["values"]
        and anti_repaint_tren["values"]["close"] == seri_belum_tutup[-2].close,
        f"close={anti_repaint_tren['values'].get('close')}",
    )
    cek(
        "hasil harian identik dengan evaluate_trend_filter berparameter terpetakan",
        naik_harian["ok"]
        == evaluate_trend_filter(_hari_deret(1.01), daily_trend_gate_config(cfg_dtrend))["ok"]
        and naik_harian["values"] == evaluate_trend_filter(
            _hari_deret(1.01), daily_trend_gate_config(cfg_dtrend)
        )["values"],
    )
    gagal_interval = _gagal_tertangkap(
        lambda: daily_trend_interval(dict(cfg_dtrend, DAILY_TREND_INTERVAL="7d"))
    )
    cek("DAILY_TREND_INTERVAL tidak dikenal ditolak saat gerbang aktif", gagal_interval)
    cek(
        "warmup harian = (jendela 120 hari + 1) x 288 candle 5m",
        daily_trend_warmup_bars(cfg_dtrend, "5m") == 121 * 288
        and daily_trend_warmup_bars(cfg_dtrend_off, "5m") == 0,
        f"{daily_trend_warmup_bars(cfg_dtrend, '5m')}",
    )
    cek(
        "warmup gabungan memasukkan gerbang EMA+ADX harian (kebutuhan terbesar)",
        htf_gate_warmup_bars(cfg_dtrend, "5m") >= 121 * 288,
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
