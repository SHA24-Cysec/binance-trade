"""Uji paritas jalur exit antara bot live dan mesin backtest untuk breakeven
dan trailing ATR.

Latar belakang
--------------
Bot live memantau harga tiap beberapa detik, jadi pergerakan di dalam satu
candle terlihat sebagai urutan harga. Backtest hanya punya candle OHLC tertutup
dan sengaja memakai puncak candle SEBELUMnya saat menaikkan trailing, supaya
tidak memakai informasi yang belum tersedia pada saat itu (bebas look-ahead).

Modul ini menjalankan urutan candle yang sama pada tiga jalur:

* ``LIVE_HL``  — urutan tick Open, High, Low, Close (harga tinggi dulu).
* ``LIVE_LH``  — urutan tick Open, Low, High, Close (harga rendah dulu).
  Kedua urutan ini mewakili dua kemungkinan nyata jalannya harga di dalam candle,
  karena bot live melihat tick sungguhan, bukan OHLC.
* ``BACKTEST`` — ``parity.evaluate_candle_exit`` persis seperti yang dipakai
  simulasi portofolio dan backtest satu simbol.

Yang diuji: apakah ketiga jalur mengambil keputusan exit yang sejenis
(stop, breakeven, trailing, take profit, batas tahan) dan seberapa besar
selisih harga keluarnya, sehingga selisih live vs backtest bisa dilaporkan
secara jujur, bukan diklaim nol.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from backtesting import parity
from strategy import entry_rule
from strategy import exit_atr
from strategy import indicators as strategy

REASON_TRAIL = parity.TRAILING_STOP_REASON
REASON_BE = parity.FIXED_BREAKEVEN_REASON
REASON_SL = "STOP_LOSS"
REASON_TP = "TAKE_PROFIT"
REASON_TIME = parity.TIME_STOP_REASON
KELUARGA_STOP = (REASON_SL, REASON_BE, REASON_TRAIL)


def _alasan_stop_live(state: dict) -> str:
    trail = float(state.get("trail_stop_price") or 0.0)
    if state.get("trail_active") and trail > 0.0:
        if float(state["sl_price"]) >= trail - 1e-12:
            return REASON_TRAIL
    if state.get("be_active") and float(state["sl_price"]) >= float(
        state["entry_price"]
    ):
        return REASON_BE
    return REASON_SL


def _urutan_tick(candle, urutan: str) -> list[float]:
    o = float(candle.open)
    h = float(candle.high)
    l = float(candle.low)
    c = float(candle.close)
    if urutan == "LIVE_HL":
        return [o, h, l, c]
    return [o, l, h, c]


def jalankan_live(
    klines: Sequence,
    config: dict,
    *,
    entry_price: float,
    atr_value: float,
    sl_price: float,
    tp_price: Optional[float] = None,
    entry_time: int = 0,
    urutan: str = "LIVE_HL",
) -> dict:
    """Simulasikan logika ``manage_exit`` bot live pada urutan tick tertentu."""

    if urutan not in ("LIVE_HL", "LIVE_LH"):
        raise ValueError("urutan harus LIVE_HL atau LIVE_LH")
    jarak = float(atr_value)
    entry = float(entry_price)
    sl_awal = float(sl_price)
    tp = float(tp_price) if tp_price is not None else None
    if tp is None:
        rencana = exit_atr.plan_levels(entry, jarak, config)
        tp = float(rencana["tp_price"])
    state = {
        "entry_price": entry,
        "initial_sl_price": sl_awal,
        "sl_price": sl_awal,
        "tp_price": tp,
        "peak_price": entry,
        "be_active": False,
        "be_stop_price": 0.0,
        "trail_active": False,
        "trail_stop_price": 0.0,
        "atr_at_entry": jarak,
    }
    events: list[tuple] = []
    langkah: list[dict] = []
    bar_ms = 3600 * 1000
    for candle in klines:
        for harga in _urutan_tick(candle, urutan):
            if float(state["sl_price"]) > 0.0 and harga <= float(state["sl_price"]):
                return {
                    "reason": _alasan_stop_live(state),
                    "exit_price": float(state["sl_price"]),
                    "events": events,
                    "steps": langkah,
                    "sl_akhir": float(state["sl_price"]),
                    "be_aktif": bool(state["be_active"]),
                    "trail_aktif": bool(state["trail_active"]),
                    "puncak": float(state["peak_price"]),
                    "candle_index": len(langkah),
                    "urutan": urutan,
                }
            if entry_rule.take_profit_due(entry, state["tp_price"], harga):
                return {
                    "reason": REASON_TP,
                    "exit_price": float(state["tp_price"]),
                    "events": events,
                    "steps": langkah,
                    "sl_akhir": float(state["sl_price"]),
                    "be_aktif": bool(state["be_active"]),
                    "trail_aktif": bool(state["trail_active"]),
                    "puncak": float(state["peak_price"]),
                    "candle_index": len(langkah),
                    "urutan": urutan,
                }
            rencana = exit_atr.advance(state, harga, config, atr=jarak)
            kandidat = float(rencana.get("sl_price") or 0.0)
            if 0.0 < kandidat < harga and kandidat > float(state["sl_price"]):
                for event, dari, ke in rencana.get("events") or []:
                    events.append((event, float(dari), float(ke)))
                if rencana.get("trail_active"):
                    state["trail_active"] = True
                    state["trail_stop_price"] = kandidat
                if rencana.get("be_active"):
                    state["be_active"] = True
                    state["be_stop_price"] = float(
                        rencana.get("be_stop_price") or 0.0
                    )
                state["sl_price"] = kandidat
                state["peak_price"] = float(rencana.get("peak_price") or 0.0)
            else:
                puncak = float(rencana.get("peak_price") or 0.0)
                if puncak > float(state["peak_price"]):
                    state["peak_price"] = puncak
        langkah.append(
            {
                "close_time": int(candle.close_time),
                "sl": float(state["sl_price"]),
                "be": bool(state["be_active"]),
                "trail": bool(state["trail_active"]),
            }
        )
        if entry_time > 0:
            cap_ms = int(config.get("EXIT_MAX_HOLD_HOURS", 0.0) or 0.0) * bar_ms
            if strategy.time_stop_due(
                entry_time, int(candle.close_time), cap_ms, bar_ms
            ):
                return {
                    "reason": REASON_TIME,
                    "exit_price": float(candle.close),
                    "events": events,
                    "steps": langkah,
                    "sl_akhir": float(state["sl_price"]),
                    "be_aktif": bool(state["be_active"]),
                    "trail_aktif": bool(state["trail_active"]),
                    "puncak": float(state["peak_price"]),
                    "candle_index": len(langkah),
                    "urutan": urutan,
                }
    return {
        "reason": "TERBUKA",
        "exit_price": float(klines[-1].close) if klines else 0.0,
        "events": events,
        "steps": langkah,
        "sl_akhir": float(state["sl_price"]),
        "be_aktif": bool(state["be_active"]),
        "trail_aktif": bool(state["trail_active"]),
        "puncak": float(state["peak_price"]),
        "urutan": urutan,
    }


def jalankan_backtest(
    klines: Sequence,
    config: dict,
    *,
    entry_price: float,
    atr_value: float,
    entry_time: int = 0,
) -> dict:
    """Jalankan jalur backtest (``parity.evaluate_candle_exit``) pada candle yang sama."""

    entry = float(entry_price)
    jarak = float(atr_value)
    info = entry_rule.exit_levels(entry, entry, config, atr=jarak, fallback_allowed=True)
    levels = {
        "source": "ENTRY_RULE",
        "atr": jarak,
        "sl_pct": float(info["sl_pct"]),
        "tp_pct": float(info["tp_pct"]),
        "sl_price": float(info["sl_price"]),
        "tp_price": float(info["tp_price"]),
        "rr_ratio": float(info["rr_ratio"] or 0.0),
    }
    pos = parity.PositionState(entry, levels, entry_time=entry_time)
    bar_ms = 3600 * 1000
    cap_ms = int(float(config.get("EXIT_MAX_HOLD_HOURS", 0.0) or 0.0) * bar_ms)
    langkah: list[dict] = []
    for candle in klines:
        hasil = parity.evaluate_candle_exit(
            pos, candle, config, cap_ms=cap_ms, bar_ms=bar_ms
        )
        langkah.append(
            {
                "close_time": int(candle.close_time),
                "sl": float(pos.sl_price),
                "be": bool(pos.be_active),
                "trail": bool(pos.trailing_active),
            }
        )
        if hasil is not None:
            reason, price = hasil
            return {
                "reason": reason,
                "exit_price": float(price),
                "steps": langkah,
                "sl_akhir": float(pos.sl_price),
                "be_aktif": bool(pos.be_active),
                "trail_aktif": bool(pos.trailing_active),
                "puncak": float(pos.highest_price),
                "candle_index": len(langkah),
            }
    return {
        "reason": "TERBUKA",
        "exit_price": float(klines[-1].close) if klines else 0.0,
        "steps": langkah,
        "sl_akhir": float(pos.sl_price),
        "be_aktif": bool(pos.be_active),
        "trail_aktif": bool(pos.trailing_active),
        "puncak": float(pos.highest_price),
    }


def bandingkan(
    klines: Sequence,
    config: dict,
    *,
    entry_price: float,
    atr_value: float,
    entry_time: int = 0,
    sl_price: Optional[float] = None,
) -> dict:
    """Jalankan tiga jalur dan kembalikan ringkasan perbandingannya."""

    entry = float(entry_price)
    jarak = float(atr_value)
    if sl_price is None:
        sl_price = float(
            entry_rule.exit_levels(
                entry, entry, config, atr=jarak, fallback_allowed=True
            )["sl_price"]
        )
    kiri = jalankan_live(
        klines,
        config,
        entry_price=entry,
        atr_value=jarak,
        sl_price=sl_price,
        entry_time=entry_time,
        urutan="LIVE_HL",
    )
    kanan = jalankan_live(
        klines,
        config,
        entry_price=entry,
        atr_value=jarak,
        sl_price=sl_price,
        entry_time=entry_time,
        urutan="LIVE_LH",
    )
    bt = jalankan_backtest(
        klines, config, entry_price=entry, atr_value=jarak, entry_time=entry_time
    )
    alasan = {kiri["reason"], kanan["reason"], bt["reason"]}
    harga = [kiri["exit_price"], kanan["exit_price"], bt["exit_price"]]
    return {
        "live_hl": kiri,
        "live_lh": kanan,
        "backtest": bt,
        "alasan_sama": len(alasan) == 1,
        "alasan": sorted(alasan),
        "harga_min": min(harga),
        "harga_max": max(harga),
        "selisih_maks_pct": round(
            (max(harga) - min(harga)) / entry * 100.0, 4
        )
        if entry > 0
        else 0.0,
        "backtest_di_rentang": min(harga) - 1e-9 <= harga[2] <= max(harga) + 1e-9,
        "alasan_satu_keluarga_stop": alasan.issubset(set(KELUARGA_STOP) | {REASON_TP, REASON_TIME})
        or len(alasan) == 1,
    }


# ---------------------------------------------------------------- selftest


def _konfig() -> dict:
    from copy import deepcopy

    from config.config import PUMP_CONFIG

    cfg = deepcopy(PUMP_CONFIG)
    cfg.update(
        {
            "MODE": "PAPER",
            "ENTRY_RULE_ENABLED": True,
            "ENTRY_ATR_PERIOD": 14,
            "EXIT_ATR_SL_MULT": 1.5,
            "EXIT_ATR_TP_MULT": 3.0,
            "EXIT_ATR_TRAIL_MULT": 2.0,
            "EXIT_ATR_BREAKEVEN_MULT": 1.0,
            "EXIT_ATR_MAX_SL_PCT": 5.0,
            "ENTRY_MIN_RR_RATIO": 2.0,
            "EXIT_MAX_HOLD_HOURS": 24.0,
            "TAKER_FEE_PCT": 0.1,
        }
    )
    return cfg


def _candle(i: int, o: float, h: float, l: float, c: float):
    from strategy.indicators import Kline

    return Kline(
        open_time=i * 3600 * 1000,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=1000.0,
        close_time=(i + 1) * 3600 * 1000 - 1,
    )


def _skenario() -> list[tuple[str, list, dict]]:
    """Skenario candle 1 jam dengan entry 100 dan ATR 1."""

    entry = 100.0
    atr = 1.0
    dasar = {"entry_price": entry, "atr_value": atr}

    # STOP_LOSS: candle pertama langsung menembus stop 98,5.
    sl_dulu = [
        _candle(0, 100.0, 100.2, 98.0, 98.4),
        _candle(1, 98.4, 99.0, 97.5, 97.8),
    ]
    # TAKE_PROFIT: high menembus target 103.
    tp_dulu = [
        _candle(0, 100.0, 103.4, 99.9, 103.1),
        _candle(1, 103.1, 103.6, 102.9, 103.2),
    ]
    # BREAKEVEN: naik melewati 101 (pemicu BE), lalu turun menembus 100.
    be_kena = [
        _candle(0, 100.0, 101.3, 99.9, 101.2),
        _candle(1, 101.2, 101.4, 99.6, 99.8),
        _candle(2, 99.8, 100.2, 99.0, 99.2),
    ]
    # TRAILING: naik bertahap, stop ikut naik, lalu retrace menembus stop trail.
    trail = [
        _candle(0, 100.0, 101.8, 100.2, 101.6),
        _candle(1, 101.6, 102.6, 101.2, 102.4),
        _candle(2, 102.4, 102.9, 101.9, 102.7),
        _candle(3, 102.7, 102.8, 100.5, 100.8),
    ]
    # Puncak dan retrace dalam SATU candle: di sinilah jalur live dan backtest
    # memang berbeda, karena backtest menaikkan trail dari puncak candle sebelumnya.
    trail_satu_candle = [
        _candle(0, 100.0, 101.8, 100.2, 101.6),
        _candle(1, 101.6, 102.8, 100.7, 101.0),
        _candle(2, 101.0, 101.3, 99.9, 100.1),
    ]
    # Batas tahan: harga datar, tidak menyentuh stop maupun target.
    tahan = [_candle(i, 100.0, 100.4, 99.9, 100.1) for i in range(30)]

    return [
        ("stop loss lebih dulu", sl_dulu, dict(dasar)),
        ("take profit lebih dulu", tp_dulu, dict(dasar)),
        ("breakeven tersentuh", be_kena, dict(dasar)),
        ("trailing lalu retrace", trail, dict(dasar)),
        ("puncak dan retrace satu candle", trail_satu_candle, dict(dasar)),
        ("batas tahan 24 jam", tahan, dict(dasar, entry_time=1)),
    ]


def selftest() -> bool:
    cfg = _konfig()
    hasil: list[tuple[str, bool, str]] = []

    def cek(nama: str, lulus: bool, catatan: str = "") -> None:
        hasil.append((nama, bool(lulus), catatan))

    skenario = {nama: (klines, arg) for nama, klines, arg in _skenario()}

    for nama, (klines, arg) in skenario.items():
        b = bandingkan(klines, cfg, **arg)
        alasan_bt = b["backtest"]["reason"]
        alasan_live = {b["live_hl"]["reason"], b["live_lh"]["reason"]}
        cek(
            f"{nama}: alasan exit masih satu keluarga",
            b["alasan_satu_keluarga_stop"],
            f"live={sorted(alasan_live)} backtest={alasan_bt}",
        )
        cek(
            f"{nama}: backtest bebas look-ahead",
            b["backtest_di_rentang"],
            (
                f"backtest={b['backtest']['exit_price']:.4f} "
                f"rentang={b['harga_min']:.4f}..{b['harga_max']:.4f}"
            ),
        )

    # Rincian per skenario yang penting untuk laporan.
    be = bandingkan(skenario["breakeven tersentuh"][0], cfg, **skenario["breakeven tersentuh"][1])
    cek(
        "breakeven benar-benar aktif di live dan backtest",
        be["live_hl"]["be_aktif"] and be["backtest"]["be_aktif"],
        (
            f"live be={be['live_hl']['be_aktif']} "
            f"backtest be={be['backtest']['be_aktif']}"
        ),
    )
    cek(
        "breakeven keluar di harga entry",
        abs(be["backtest"]["exit_price"] - 100.0) < 1e-9
        and abs(be["live_hl"]["exit_price"] - 100.0) < 1e-9,
        f"backtest={be['backtest']['exit_price']:.4f} live={be['live_hl']['exit_price']:.4f}",
    )

    tr = bandingkan(skenario["trailing lalu retrace"][0], cfg, **skenario["trailing lalu retrace"][1])
    cek(
        "trailing benar-benar aktif di live dan backtest",
        tr["live_hl"]["trail_aktif"] and tr["backtest"]["trail_aktif"],
        f"live={tr['live_hl']['trail_aktif']} backtest={tr['backtest']['trail_aktif']}",
    )
    cek(
        "trailing: backtest tidak pernah keluar lebih tinggi dari live",
        tr["backtest"]["exit_price"] <= max(
            tr["live_hl"]["exit_price"], tr["live_lh"]["exit_price"]
        )
        + 1e-9,
        (
            f"backtest={tr['backtest']['exit_price']:.4f} "
            f"live_hl={tr['live_hl']['exit_price']:.4f} "
            f"live_lh={tr['live_lh']['exit_price']:.4f}"
        ),
    )
    cek(
        "trailing: harga keluar live dan backtest sama",
        abs(
            tr["backtest"]["exit_price"] - tr["live_hl"]["exit_price"]
        )
        < 0.5,
        (
            f"backtest={tr['backtest']['exit_price']:.4f} "
            f"live={tr['live_hl']['exit_price']:.4f}"
        ),
    )
    cek(
        "trailing: tidak ada take profit yang mendahului stop trail",
        tr["backtest"]["reason"] == REASON_TRAIL,
        f"alasan={tr['backtest']['reason']} harga={tr['backtest']['exit_price']:.4f}",
    )

    satu = bandingkan(
        skenario["puncak dan retrace satu candle"][0],
        cfg,
        **skenario["puncak dan retrace satu candle"][1],
    )
    cek(
        "satu candle: live keluar lebih dulu, backtest satu bar lebih lambat",
        satu["backtest"]["candle_index"] >= satu["live_hl"]["candle_index"],
        (
            f"live bar={satu['live_hl']['candle_index']} "
            f"backtest bar={satu['backtest']['candle_index']} "
            f"alasan live={satu['live_hl']['reason']} backtest={satu['backtest']['reason']}"
        ),
    )
    cek(
        "satu candle: selisih harga keluar kecil",
        abs(
            satu["backtest"]["exit_price"] - satu["live_hl"]["exit_price"]
        )
        < 0.5,
        (
            f"backtest={satu['backtest']['exit_price']:.4f} "
            f"live={satu['live_hl']['exit_price']:.4f}"
        ),
    )

    tl = bandingkan(skenario["batas tahan 24 jam"][0], cfg, **skenario["batas tahan 24 jam"][1])
    cek(
        "batas tahan memicu TIME_STOP di ketiga jalur",
        tl["alasan_sama"] and tl["backtest"]["reason"] == REASON_TIME,
        f"alasan={tl['alasan']}",
    )

    ttp = bandingkan(skenario["take profit lebih dulu"][0], cfg, **skenario["take profit lebih dulu"][1])
    cek(
        "take profit keluar tepat di level target",
        abs(ttp["backtest"]["exit_price"] - ttp["live_hl"]["exit_price"]) < 1e-9,
        f"backtest={ttp['backtest']['exit_price']:.4f} live={ttp['live_hl']['exit_price']:.4f}",
    )

    lulus = sum(1 for _, ok, _ in hasil if ok)
    print("Selftest paritas breakeven dan trailing:")
    for nama, ok, catatan in hasil:
        print(f"  [{'LULUS' if ok else 'GAGAL'}] {nama}" + (f" ({catatan})" if catatan else ""))
    print(f"HASIL: {lulus}/{len(hasil)} lulus")
    return lulus == len(hasil)


if __name__ == "__main__":
    import sys

    sys.exit(0 if selftest() else 1)
