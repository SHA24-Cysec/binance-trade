from __future__ import annotations

import math
from typing import Optional

DEFAULT_ATR_PERIOD = 14
DEFAULT_SL_MULT = 1.5
DEFAULT_TP_MULT = 3.0
DEFAULT_TRAIL_MULT = 2.0
DEFAULT_BREAKEVEN_MULT = 1.0
DEFAULT_MAX_SL_PCT = 5.0
DEFAULT_MIN_RR = 2.0

ATR_PARAM_KEYS = (
    "ENTRY_ATR_PERIOD",
    "EXIT_ATR_SL_MULT",
    "EXIT_ATR_TP_MULT",
    "EXIT_ATR_TRAIL_MULT",
    "EXIT_ATR_BREAKEVEN_MULT",
    "EXIT_ATR_MAX_SL_PCT",
)

EVENT_BREAKEVEN = "BREAKEVEN_AKTIF"
EVENT_TRAILING = "TRAILING_AKTIF"


def _finite(value) -> Optional[float]:
    try:
        angka = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(angka):
        return None
    return angka


def _positive(value) -> Optional[float]:
    angka = _finite(value)
    if angka is None or angka <= 0.0:
        return None
    return angka


def _cfg_float(config: dict, key: str, default: float, low: float, high: float) -> float:
    sumber = config if isinstance(config, dict) else {}
    angka = _finite(sumber.get(key, default))
    if angka is None:
        angka = default
    return max(low, min(high, angka))


def atr_period(config: dict) -> int:
    return int(round(_cfg_float(config, "ENTRY_ATR_PERIOD", DEFAULT_ATR_PERIOD, 2.0, 200.0)))


def atr_params(config: dict) -> dict:
    return {
        "period": atr_period(config),
        "sl_mult": _cfg_float(config, "EXIT_ATR_SL_MULT", DEFAULT_SL_MULT, 0.1, 20.0),
        "tp_mult": _cfg_float(config, "EXIT_ATR_TP_MULT", DEFAULT_TP_MULT, 0.1, 50.0),
        "trail_mult": _cfg_float(config, "EXIT_ATR_TRAIL_MULT", DEFAULT_TRAIL_MULT, 0.1, 20.0),
        "breakeven_mult": _cfg_float(
            config, "EXIT_ATR_BREAKEVEN_MULT", DEFAULT_BREAKEVEN_MULT, 0.0, 20.0
        ),
        "max_sl_pct": _cfg_float(
            config, "EXIT_ATR_MAX_SL_PCT", DEFAULT_MAX_SL_PCT, 0.0, 50.0
        ),
        "min_rr": _cfg_float(config, "ENTRY_MIN_RR_RATIO", DEFAULT_MIN_RR, 0.1, 20.0),
    }


def stop_price(entry_price: float, atr: float, mult: float) -> float:
    entry = _positive(entry_price)
    jarak = _positive(atr)
    kali = _finite(mult)
    if entry is None or jarak is None or kali is None or kali <= 0.0:
        return 0.0
    level = entry - jarak * kali
    return level if level > 0.0 else 0.0


def target_price(entry_price: float, atr: float, mult: float) -> float:
    entry = _positive(entry_price)
    jarak = _positive(atr)
    kali = _finite(mult)
    if entry is None or jarak is None or kali is None or kali <= 0.0:
        return 0.0
    return entry + jarak * kali


def breakeven_trigger_price(entry_price: float, atr: float, mult: float) -> float:
    entry = _positive(entry_price)
    jarak = _positive(atr)
    kali = _finite(mult)
    if entry is None or jarak is None or kali is None or kali <= 0.0:
        return 0.0
    return entry + jarak * kali


def breakeven_price(entry_price: float, lock_pct: float = 0.0) -> float:
    entry = _positive(entry_price)
    if entry is None:
        return 0.0
    kunci = _finite(lock_pct) or 0.0
    return entry * (1.0 + max(0.0, kunci) / 100.0)


def trailing_stop_price(highest_price: float, atr: float, mult: float) -> float:
    puncak = _positive(highest_price)
    jarak = _positive(atr)
    kali = _finite(mult)
    if puncak is None or jarak is None or kali is None or kali <= 0.0:
        return 0.0
    level = puncak - jarak * kali
    return level if level > 0.0 else 0.0


def trailing_trigger_price(entry_price: float, atr: float, sl_mult: float) -> float:
    return breakeven_trigger_price(entry_price, atr, sl_mult)


def plan_levels(entry_price: float, atr: float, config: dict) -> dict:
    params = atr_params(config)
    entry = _positive(entry_price)
    jarak = _positive(atr)
    dasar = {
        "ok": False,
        "reason": "",
        "sl_price": None,
        "tp_price": None,
        "sl_pct": None,
        "tp_pct": None,
        "rr_ratio": None,
        "atr": jarak,
        "sl_mult": params["sl_mult"],
        "tp_mult": params["tp_mult"],
        "trail_mult": params["trail_mult"],
        "breakeven_mult": params["breakeven_mult"],
        "max_sl_pct": params["max_sl_pct"],
        "min_rr_ratio": params["min_rr"],
    }
    if entry is None:
        dasar["reason"] = "harga entry tidak valid untuk menghitung level ATR"
        return dasar
    if jarak is None:
        dasar["reason"] = (
            f"ATR periode {params['period']} tidak tersedia atau tidak valid, "
            "level exit tidak bisa dihitung"
        )
        return dasar

    sl = stop_price(entry, jarak, params["sl_mult"])
    if sl <= 0.0 or sl >= entry:
        dasar["reason"] = (
            f"Stop ATR {params['sl_mult']:g} x ATR menghasilkan level {sl:.6g} "
            f"yang tidak berada di bawah entry {entry:.6g}"
        )
        return dasar
    sl_pct = (entry - sl) / entry * 100.0
    if params["max_sl_pct"] > 0.0 and sl_pct > params["max_sl_pct"] + 1e-9:
        dasar["sl_price"] = sl
        dasar["sl_pct"] = sl_pct
        dasar["reason"] = (
            f"jarak SL ATR {sl_pct:.2f}% melampaui batas {params['max_sl_pct']:.2f}% "
            f"(ATR {jarak:.6g}, entry {entry:.6g}); volatilitas terlalu tinggi untuk ukuran posisi ini"
        )
        return dasar

    tp = target_price(entry, jarak, params["tp_mult"])
    if tp <= entry:
        dasar["reason"] = "target ATR tidak berada di atas harga entry"
        return dasar
    tp_pct = (tp - entry) / entry * 100.0
    rr = params["tp_mult"] / params["sl_mult"]
    if rr + 1e-9 < params["min_rr"]:
        dasar["sl_price"] = sl
        dasar["tp_price"] = tp
        dasar["sl_pct"] = sl_pct
        dasar["tp_pct"] = tp_pct
        dasar["rr_ratio"] = rr
        dasar["reason"] = (
            f"R:R dari kelipatan ATR hanya 1:{rr:.2f}, di bawah minimum 1:{params['min_rr']:.2f} "
            f"(naikkan EXIT_ATR_TP_MULT atau turunkan EXIT_ATR_SL_MULT)"
        )
        return dasar

    return {
        "ok": True,
        "reason": (
            f"SL {sl:.6g} (-{sl_pct:.2f}%, {params['sl_mult']:g} x ATR {jarak:.6g}), "
            f"TP {tp:.6g} (+{tp_pct:.2f}%, {params['tp_mult']:g} x ATR), "
            f"R:R 1:{rr:.2f}, trailing {params['trail_mult']:g} x ATR"
        ),
        "sl_price": sl,
        "tp_price": tp,
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "rr_ratio": rr,
        "atr": jarak,
        "sl_mult": params["sl_mult"],
        "tp_mult": params["tp_mult"],
        "trail_mult": params["trail_mult"],
        "breakeven_mult": params["breakeven_mult"],
        "max_sl_pct": params["max_sl_pct"],
        "min_rr_ratio": params["min_rr"],
    }


def _state_float(state: dict, key: str, fallback: float = 0.0) -> float:
    angka = _finite(state.get(key))
    return fallback if angka is None else angka


def advance(
    state: dict,
    price: float,
    config: dict,
    *,
    atr: Optional[float] = None,
) -> dict:
    params = atr_params(config)
    entry = _state_float(state, "entry_price")
    harga = _finite(price) or 0.0
    jarak = _finite(atr if atr is not None else state.get("atr_at_entry"))
    awal = _state_float(state, "initial_sl_price") or _state_float(state, "sl_price")
    sl_lama = _state_float(state, "sl_price")
    puncak = max(_state_float(state, "peak_price"), harga, entry)
    be_aktif = bool(state.get("be_active"))
    be_stop = _state_float(state, "be_stop_price")
    trail_aktif = bool(state.get("trail_active"))
    trail_stop = _state_float(state, "trail_stop_price")
    sl_baru = sl_lama
    events: list[tuple] = []

    hasil = {
        "peak_price": puncak,
        "be_active": be_aktif,
        "be_stop_price": be_stop,
        "trail_active": trail_aktif,
        "trail_stop_price": trail_stop,
        "sl_price": sl_lama,
        "tp_price": _state_float(state, "tp_price"),
        "events": events,
        "atr": jarak,
    }

    if entry <= 0.0 or harga <= 0.0 or jarak is None or jarak <= 0.0:
        return hasil

    if params["breakeven_mult"] > 0.0 and not be_aktif:
        pemicu = breakeven_trigger_price(entry, jarak, params["breakeven_mult"])
        if pemicu > entry and puncak >= pemicu:
            level = breakeven_price(entry)
            if level > 0.0 and level > sl_baru:
                events.append((EVENT_BREAKEVEN, sl_baru, level))
                be_aktif = True
                be_stop = level
                sl_baru = level

    if params["trail_mult"] > 0.0 and jarak > 0.0:
        pemicu_trail = trailing_trigger_price(entry, jarak, params["sl_mult"])
        if pemicu_trail > 0.0 and puncak >= pemicu_trail:
            kandidat = trailing_stop_price(puncak, jarak, params["trail_mult"])
            if kandidat > 0.0 and kandidat > sl_baru:
                events.append((EVENT_TRAILING, sl_baru, kandidat))
                trail_aktif = True
                trail_stop = kandidat
                sl_baru = kandidat

    if awal > 0.0 and sl_baru < awal:
        sl_baru = awal

    hasil.update(
        {
            "be_active": be_aktif,
            "be_stop_price": be_stop,
            "trail_active": trail_aktif,
            "trail_stop_price": trail_stop,
            "sl_price": sl_baru,
        }
    )
    return hasil


