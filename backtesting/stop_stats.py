"""Sebaran jarak stop (SL) hasil backtest dalam persen.

Modul ini dipakai bersama oleh backtest satu simbol dan simulasi portofolio
supaya laporan memakai definisi yang sama: jarak stop diukur dari harga entry
ke harga SL pada saat posisi dibuka (kolom ``sl_pct`` pada trade).

Semua nilai dalam persen, bukan pecahan. Trade tanpa nilai SL yang sah
(misalnya rekaman lama) dihitung pada kunci ``tanpa_data`` dan tidak
mencemari statistik.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

# Batas atas tiap keranjang, dalam persen. Keranjang terakhir menampung
# nilai di atas batas terakhir.
BATAS_KERANJANG = (0.5, 1.0, 2.0, 3.0, 5.0)

LABEL_KERANJANG = (
    "di bawah 0,5%",
    "0,5% sampai 1%",
    "1% sampai 2%",
    "2% sampai 3%",
    "3% sampai 5%",
    "di atas 5%",
)


def _angka(nilai) -> Optional[float]:
    try:
        angka = float(nilai)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(angka):
        return None
    return angka


def angka_atau_none(nilai) -> Optional[float]:
    """Ubah nilai apa pun menjadi float terbatas, atau ``None`` bila gagal."""

    return _angka(nilai)


def _sl_dari_trade(trade) -> Optional[float]:
    if isinstance(trade, dict):
        nilai = trade.get("sl_pct")
    else:
        nilai = getattr(trade, "sl_pct", None)
    angka = _angka(nilai)
    if angka is None or angka <= 0.0:
        return None
    return angka


def _persentil(deret: Sequence[float], fraksi: float) -> float:
    if not deret:
        return 0.0
    urut = sorted(deret)
    if len(urut) == 1:
        return float(urut[0])
    posisi = fraksi * (len(urut) - 1)
    bawah = int(math.floor(posisi))
    atas = min(bawah + 1, len(urut) - 1)
    bobot = posisi - bawah
    return float(urut[bawah] * (1.0 - bobot) + urut[atas] * bobot)


def distribusi_jarak_stop(
    trades: Iterable,
    *,
    max_sl_pct: Optional[float] = None,
) -> dict:
    """Ringkas jarak stop semua trade ke bentuk keranjang dan persentil."""

    nilai: list[float] = []
    tanpa_data = 0
    for trade in trades or []:
        angka = _sl_dari_trade(trade)
        if angka is None:
            tanpa_data += 1
            continue
        nilai.append(angka)

    jumlah = len(nilai)
    keranjang = []
    for idx, batas in enumerate(BATAS_KERANJANG):
        bawah = 0.0 if idx == 0 else BATAS_KERANJANG[idx - 1]
        isi = [
            v
            for v in nilai
            if (v < batas if idx < len(BATAS_KERANJANG) else False)
            and (v >= bawah if idx > 0 else True)
        ]
        keranjang.append(
            {
                "label": LABEL_KERANJANG[idx],
                "bawah_pct": round(bawah, 4),
                "atas_pct": round(batas, 4),
                "count": len(isi),
            }
        )
    ekor = [v for v in nilai if v >= BATAS_KERANJANG[-1]]
    keranjang.append(
        {
            "label": LABEL_KERANJANG[-1],
            "bawah_pct": round(BATAS_KERANJANG[-1], 4),
            "atas_pct": None,
            "count": len(ekor),
        }
    )
    for isi in keranjang:
        isi["pct"] = round(isi["count"] / jumlah * 100.0, 1) if jumlah else 0.0

    batas_maks = _angka(max_sl_pct)
    di_batas = 0
    if batas_maks is not None and batas_maks > 0.0:
        di_batas = len([v for v in nilai if v >= batas_maks - 1e-9])

    return {
        "count": jumlah,
        "tanpa_data": tanpa_data,
        "buckets": keranjang,
        "min_pct": round(min(nilai), 4) if nilai else None,
        "median_pct": round(_persentil(nilai, 0.5), 4) if nilai else None,
        "mean_pct": round(sum(nilai) / jumlah, 4) if jumlah else None,
        "p90_pct": round(_persentil(nilai, 0.9), 4) if nilai else None,
        "max_pct": round(max(nilai), 4) if nilai else None,
        "at_limit_count": di_batas,
        "max_sl_pct": batas_maks if batas_maks and batas_maks > 0.0 else None,
    }


def ambang_stop_tersentuh(summary: dict, batas_pct: float = 3.0) -> float:
    """Bagian trade yang jarak stop-nya minimal ``batas_pct`` persen."""

    blok = (summary or {}).get("stop_distance_pct") or {}
    jumlah = int(blok.get("count") or 0)
    if jumlah <= 0:
        return 0.0
    kena = 0
    for isi in blok.get("buckets") or []:
        batas_bawah = isi.get("bawah_pct")
        if batas_bawah is None or float(batas_bawah) < float(batas_pct):
            continue
        kena += int(isi.get("count") or 0)
    return round(kena / jumlah * 100.0, 1)


def selftest() -> bool:
    """Uji cepat modul ini tanpa data pasar."""

    hasil: list[tuple[str, bool, str]] = []

    def cek(nama: str, lulus: bool, catatan: str = "") -> None:
        hasil.append((nama, bool(lulus), catatan))

    contoh = [
        {"sl_pct": 0.25},
        {"sl_pct": 0.75},
        {"sl_pct": 1.0},
        {"sl_pct": 1.9},
        {"sl_pct": 2.0},
        {"sl_pct": 4.5},
        {"sl_pct": 7.0},
        {"sl_pct": None},
        {"sl_pct": 0.0},
    ]
    d = distribusi_jarak_stop(contoh, max_sl_pct=5.0)
    cek("jumlah trade sah", d["count"] == 7, f"count={d['count']}")
    cek("trade tanpa data", d["tanpa_data"] == 2, f"tanpa_data={d['tanpa_data']}")
    jumlah_keranjang = [b["count"] for b in d["buckets"]]
    cek(
        "keranjang menjumlah tepat",
        sum(jumlah_keranjang) == d["count"],
        f"keranjang={jumlah_keranjang}",
    )
    cek(
        "pembagian keranjang benar",
        jumlah_keranjang == [1, 1, 2, 1, 1, 1],
        f"keranjang={jumlah_keranjang}",
    )
    cek("median", abs(d["median_pct"] - 1.9) < 1e-6, f"median={d['median_pct']}")
    cek("maksimum", abs(d["max_pct"] - 7.0) < 1e-6, f"max={d['max_pct']}")
    cek("minimum", abs(d["min_pct"] - 0.25) < 1e-6, f"min={d['min_pct']}")
    cek(
        "trade tepat di batas SL dihitung",
        d["at_limit_count"] == 1,
        f"at_limit={d['at_limit_count']}",
    )
    kosong = distribusi_jarak_stop([])
    cek(
        "tanpa trade tidak error",
        kosong["count"] == 0 and kosong["median_pct"] is None,
        f"count={kosong['count']}",
    )
    cek(
        "persentase keranjang dijumlahkan",
        abs(sum(b["pct"] for b in d["buckets"]) - 100.0) < 0.6,
        f"total={sum(b['pct'] for b in d['buckets'])}",
    )
    cek(
        "ambang 3 persen",
        abs(ambang_stop_tersentuh({"stop_distance_pct": d}, 3.0) - 28.6) < 0.2,
        f"nilai={ambang_stop_tersentuh({'stop_distance_pct': d}, 3.0)}",
    )

    lulus = sum(1 for _, ok, _ in hasil if ok)
    for nama, ok, catatan in hasil:
        print(f"  [{'LULUS' if ok else 'GAGAL'}] {nama}" + (f" ({catatan})" if catatan else ""))
    print(f"HASIL: {lulus}/{len(hasil)} lulus")
    return lulus == len(hasil)


if __name__ == "__main__":
    import sys

    sys.exit(0 if selftest() else 1)
