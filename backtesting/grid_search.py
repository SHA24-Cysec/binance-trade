#!/usr/bin/env python3
"""
Grid search parameter untuk backtest Pump Scanner Bot.
======================================================

Ditambahkan 1 Oktober 2026 atas permintaan pemilik repositori.

APA YANG DILAKUKAN MODUL INI
----------------------------
Menjalankan `backtest.run_backtest()` berkali kali pada DATA CANDLE YANG SAMA
memakai banyak kombinasi parameter exit, lalu memeringkat hasilnya. Data hanya
diunduh sekali oleh pemanggil dan dipakai ulang untuk semua kombinasi, jadi
tidak ada tambahan beban request ke Binance sama sekali.

PERINGATAN PALING PENTING: OVERFITTING
--------------------------------------
Mencari parameter terbaik pada satu potong data historis hampir selalu
menghasilkan angka yang terlalu bagus untuk dipercaya. Semakin banyak
kombinasi yang dicoba, semakin besar peluang satu di antaranya terlihat hebat
murni karena kebetulan. Ini bukan pendapat, ini konsekuensi statistik dari
pengujian berganda.

Karena itu modul ini SENGAJA tidak pernah menyodorkan satu "pemenang" tunggal
tanpa konteks. Yang dilakukan:

1. Data dibelah menjadi periode LATIH (in-sample) dan periode UJI
   (out-of-sample) yang tidak pernah dipakai saat memilih.
2. Peringkat disusun berdasarkan hasil periode LATIH.
3. Setiap baris hasil JUGA menampilkan hasil periode UJI.
4. Selisih keduanya dilaporkan sebagai `degradasi`. Kombinasi dengan
   degradasi besar berarti hasil latihnya tidak bertahan pada data baru,
   dan itu tanda overfitting.
5. Kombinasi dengan jumlah trade di bawah `min_trades` ditandai tidak andal,
   karena rata rata dari 3 trade tidak berarti apa apa.

Cara membaca hasilnya secara jujur: kombinasi yang layak dipertimbangkan
adalah yang hasil UJI-nya tetap wajar DAN degradasinya kecil, bukan yang
hasil LATIH-nya paling tinggi.

PEMANGKASAN KOMBINASI MUBAZIR
-----------------------------
Bot memakai dua mode exit yang saling meniadakan. Saat `USE_ATR_EXIT` bernilai
True, seluruh parameter persen (SL_PCT, TP_PCT, dan kawan kawan) tidak dipakai
sama sekali, begitu pula sebaliknya. Tanpa pemangkasan, grid akan menjalankan
ratusan kombinasi yang hasilnya identik. `expand_grid()` membuang duplikat itu
lebih dulu, sehingga waktu komputasi tidak terbuang.
"""

from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from backtesting import backtest as bt
from strategy.indicators import Kline

# Parameter yang hanya berlaku pada mode ATR.
_KUNCI_ATR = (
    "ATR_PERIOD", "ATR_MULT_SL", "ATR_MULT_TP", "ATR_MULT_BE_TRIGGER",
    "ATR_MULT_BE_LOCK", "ATR_MULT_TRAIL_START", "ATR_MULT_TRAIL",
)
# Parameter yang hanya berlaku pada mode persen tetap.
_KUNCI_PERSEN = (
    "SL_PCT", "TP_PCT", "BE_TRIGGER_PCT", "BE_LOCK_PCT",
    "TRAILING_START_PCT", "TRAILING_STEP_PCT",
)

# Batas keras jumlah kombinasi. Setiap kombinasi berarti satu kali penelusuran
# penuh seluruh candle, jadi grid yang tidak dibatasi bisa berjalan berjam jam
# dan membuat dashboard tampak menggantung.
MAX_KOMBINASI = 2000

METRIK_TERSEDIA = (
    "total_return_pct",     # return bersih setelah fee
    "profit_factor",        # total profit dibagi total loss
    "return_per_drawdown",  # return dibagi max drawdown, ukuran ketahanan
    "win_rate",
)


class GridSearchError(Exception):
    """Kesalahan yang layak ditampilkan langsung ke pengguna."""


@dataclass
class HasilKombinasi:
    """Satu kombinasi parameter beserta hasilnya di dua periode."""

    params: dict
    latih: dict                      # ringkasan periode in-sample
    uji: Optional[dict] = None       # ringkasan periode out-of-sample
    skor_latih: float = 0.0
    skor_uji: Optional[float] = None
    andal: bool = True               # False bila jumlah trade terlalu sedikit
    catatan: str = ""

    @property
    def degradasi(self) -> Optional[float]:
        """Selisih skor latih dan skor uji.

        Nilai positif besar berarti performa jatuh pada data yang belum
        pernah dilihat, yaitu gejala khas overfitting. None bila tidak ada
        periode uji.
        """
        if self.skor_uji is None:
            return None
        return self.skor_latih - self.skor_uji


@dataclass
class HasilGridSearch:
    hasil: list[HasilKombinasi] = field(default_factory=list)
    total_kombinasi: int = 0
    dilewati: int = 0
    dipangkas: int = 0
    detik: float = 0.0
    bar_latih: int = 0
    bar_uji: int = 0
    metrik: str = "total_return_pct"
    peringatan: list[str] = field(default_factory=list)
    dibatalkan: bool = False


# ---------------------------------------------------------------------
# Penyusunan grid
# ---------------------------------------------------------------------
def buat_rentang(mulai: float, sampai: float, langkah: float,
                 bulatkan: int = 6) -> list[float]:
    """Deret nilai inklusif dari `mulai` sampai `sampai`.

    Memakai perkalian indeks, bukan penjumlahan berulang, supaya galat
    pembulatan float tidak menumpuk. Penjumlahan berulang membuat
    0.1 + 0.1 + 0.1 menjadi 0.30000000000000004 dan nilai terakhir bisa
    hilang dari deret.
    """
    if langkah <= 0:
        raise GridSearchError("Langkah rentang harus lebih besar dari nol.")
    if sampai < mulai:
        raise GridSearchError("Nilai akhir rentang tidak boleh lebih kecil dari nilai awal.")
    n = int(math.floor((sampai - mulai) / langkah + 1e-9)) + 1
    return [round(mulai + i * langkah, bulatkan) for i in range(n)]


def _normalkan_spec(spec: dict) -> dict[str, list]:
    """Ubah spesifikasi grid menjadi dict kunci -> daftar nilai.

    Dua bentuk diterima untuk tiap kunci:
      daftar eksplisit : {"SL_PCT": [1.0, 2.0, 3.0]}
      rentang          : {"SL_PCT": {"mulai": 1.0, "sampai": 3.0, "langkah": 0.5}}
    """
    keluar: dict[str, list] = {}
    for kunci, nilai in spec.items():
        if isinstance(nilai, dict):
            wajib = ("mulai", "sampai", "langkah")
            if not all(k in nilai for k in wajib):
                raise GridSearchError(
                    f"Rentang '{kunci}' harus memuat {', '.join(wajib)}.")
            keluar[kunci] = buat_rentang(
                float(nilai["mulai"]), float(nilai["sampai"]), float(nilai["langkah"]))
        elif isinstance(nilai, (list, tuple)):
            if not nilai:
                raise GridSearchError(f"Daftar nilai '{kunci}' kosong.")
            keluar[kunci] = list(nilai)
        else:
            keluar[kunci] = [nilai]
    if not keluar:
        raise GridSearchError("Grid parameter kosong, tidak ada yang bisa diuji.")
    return keluar


def expand_grid(spec: dict) -> tuple[list[dict], int]:
    """Kembalikan (daftar kombinasi unik, jumlah yang dipangkas).

    Pemangkasan membuang kombinasi yang secara perilaku identik. Contoh:
    bila USE_ATR_EXIT bernilai True, nilai SL_PCT tidak pernah dibaca oleh
    mesin backtest, jadi (ATR, SL_PCT=1) dan (ATR, SL_PCT=9) akan memberi
    hasil yang sama persis. Cukup satu yang dijalankan.
    """
    dinormalkan = _normalkan_spec(spec)
    kunci = list(dinormalkan)
    total_mentah = 1
    for k in kunci:
        total_mentah *= len(dinormalkan[k])
    if total_mentah > MAX_KOMBINASI * 20:
        raise GridSearchError(
            f"Grid menghasilkan {total_mentah:,} kombinasi mentah. "
            f"Terlalu besar untuk diproses. Persempit rentang atau perbesar langkah.")

    terlihat: set[tuple] = set()
    kombinasi: list[dict] = []
    dipangkas = 0

    for nilai in itertools.product(*(dinormalkan[k] for k in kunci)):
        calon = dict(zip(kunci, nilai))
        pakai_atr = bool(calon.get("USE_ATR_EXIT", False))
        # Sidik jari hanya memuat parameter yang benar benar berpengaruh.
        relevan = {k: v for k, v in calon.items()
                   if not (pakai_atr and k in _KUNCI_PERSEN)
                   and not (not pakai_atr and k in _KUNCI_ATR)}
        sidik = tuple(sorted(relevan.items(), key=lambda kv: kv[0]))
        if sidik in terlihat:
            dipangkas += 1
            continue
        terlihat.add(sidik)
        kombinasi.append(calon)

    if len(kombinasi) > MAX_KOMBINASI:
        raise GridSearchError(
            f"Grid menghasilkan {len(kombinasi):,} kombinasi efektif, "
            f"melebihi batas {MAX_KOMBINASI:,}. Persempit rentang atau perbesar langkah.")
    return kombinasi, dipangkas


# ---------------------------------------------------------------------
# Skor
# ---------------------------------------------------------------------
def hitung_skor(ringkas: dict, metrik: str) -> float:
    """Ubah ringkasan backtest menjadi satu angka untuk diperingkat.

    Nilai tak hingga dinetralkan supaya pengurutan tidak didominasi oleh
    kombinasi yang kebetulan tidak pernah rugi (profit factor tak hingga).
    """
    if metrik == "total_return_pct":
        nilai = float(ringkas.get("total_return_pct", 0.0))
    elif metrik == "profit_factor":
        nilai = float(ringkas.get("profit_factor", 0.0))
    elif metrik == "win_rate":
        nilai = float(ringkas.get("win_rate", 0.0))
    elif metrik == "return_per_drawdown":
        dd = abs(float(ringkas.get("max_drawdown_pct", 0.0)))
        ret = float(ringkas.get("total_return_pct", 0.0))
        # Drawdown nol biasanya berarti sampel terlalu kecil, bukan strategi
        # sempurna. Dipakai lantai 1 persen supaya tidak meledak jadi tak hingga.
        nilai = ret / max(dd, 1.0)
    else:
        raise GridSearchError(
            f"Metrik '{metrik}' tidak dikenal. Pilihan: {', '.join(METRIK_TERSEDIA)}.")
    if math.isnan(nilai):
        return 0.0
    if math.isinf(nilai):
        return 1e9 if nilai > 0 else -1e9
    return nilai


# ---------------------------------------------------------------------
# Eksekusi
# ---------------------------------------------------------------------
def run_grid_search(
    klines: Sequence[Kline],
    base_config: dict,
    spec: dict,
    warmup_bars: int,
    daily_klines: Optional[Sequence[Kline]] = None,
    metrik: str = "total_return_pct",
    rasio_latih: float = 0.7,
    min_trades: int = 10,
    progress_cb: Optional[Callable[[float], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> HasilGridSearch:
    """Jalankan grid search dengan pemisahan periode latih dan uji.

    rasio_latih : porsi data untuk periode latih. 0.7 berarti 70 persen awal
                  dipakai memilih parameter, 30 persen sisanya hanya dipakai
                  memverifikasi. Nilai 1.0 mematikan pemisahan, dan modul akan
                  menambahkan peringatan eksplisit karena hasilnya tidak bisa
                  divalidasi.
    min_trades  : di bawah jumlah ini hasil ditandai tidak andal.
    """
    if metrik not in METRIK_TERSEDIA:
        raise GridSearchError(
            f"Metrik '{metrik}' tidak dikenal. Pilihan: {', '.join(METRIK_TERSEDIA)}.")
    if not 0.1 <= rasio_latih <= 1.0:
        raise GridSearchError("rasio_latih harus di antara 0.1 dan 1.0.")

    klines = list(klines)
    n = len(klines)
    if n <= warmup_bars + 10:
        raise GridSearchError(
            f"Data terlalu pendek: {n} candle dengan warmup {warmup_bars}.")

    kombinasi, dipangkas = expand_grid(spec)
    hasil_grid = HasilGridSearch(
        total_kombinasi=len(kombinasi), dipangkas=dipangkas, metrik=metrik)

    # Pemisahan dilakukan pada sumbu waktu, bukan acak. Data harga bersifat
    # berurutan, jadi pengacakan akan membocorkan masa depan ke periode latih.
    if rasio_latih >= 1.0:
        potong = n
        kl_latih, kl_uji = klines, []
        hasil_grid.peringatan.append(
            "Pemisahan periode uji dimatikan (rasio_latih=1.0). Hasil peringkat "
            "TIDAK tervalidasi pada data baru dan sangat rentan overfitting.")
    else:
        potong = warmup_bars + int((n - warmup_bars) * rasio_latih)
        kl_latih = klines[:potong]
        # Periode uji ikut membawa warmup di depannya supaya statistik 24 jam
        # dan ATR tetap punya sejarah yang cukup. Tanpa ini, bar bar awal
        # periode uji akan ditolak karena warmup belum terpenuhi.
        awal_uji = max(0, potong - warmup_bars)
        kl_uji = klines[awal_uji:]

    hasil_grid.bar_latih = max(0, len(kl_latih) - warmup_bars)
    hasil_grid.bar_uji = max(0, len(kl_uji) - warmup_bars) if kl_uji else 0

    if kl_uji and hasil_grid.bar_uji < 50:
        hasil_grid.peringatan.append(
            f"Periode uji hanya {hasil_grid.bar_uji} bar. Terlalu pendek untuk "
            f"memvalidasi apa pun. Perpanjang rentang hari atau turunkan rasio_latih.")

    def _harian_untuk(potongan: Sequence[Kline]) -> Optional[list[Kline]]:
        """Saring candle harian agar tidak melampaui periode yang diuji.

        Tanpa penyaringan ini, periode latih akan melihat candle harian dari
        masa depan dan gerbang pump menjadi bocor (look-ahead bias).
        """
        if not daily_klines or not potongan:
            return None
        batas = potongan[-1].close_time
        return [d for d in daily_klines if d.close_time <= batas]

    mulai = time.time()
    dilewati = 0

    for idx, params in enumerate(kombinasi):
        if cancel_cb and cancel_cb():
            hasil_grid.dibatalkan = True
            break
        if progress_cb:
            progress_cb(min(1.0, idx / max(1, len(kombinasi))))

        try:
            cfg = bt.apply_overrides(base_config, params)
            bt.validate_params(cfg)
        except bt.BacktestError:
            # Kombinasi tidak valid, misalnya nilai di luar rentang wajar.
            # Dilewati diam diam, bukan menggagalkan seluruh grid.
            dilewati += 1
            continue

        cfg["_symbol"] = base_config.get("_symbol", "")

        try:
            r_latih = bt.run_backtest(kl_latih, cfg, warmup_bars,
                                      daily_klines=_harian_untuk(kl_latih))
            s_latih = bt.summarize(r_latih)
        except Exception:
            dilewati += 1
            continue

        item = HasilKombinasi(
            params=dict(params),
            latih=s_latih,
            skor_latih=hitung_skor(s_latih, metrik),
        )

        if int(s_latih.get("total_trades", 0)) < min_trades:
            item.andal = False
            item.catatan = (f"hanya {s_latih.get('total_trades', 0)} trade pada periode "
                            f"latih, di bawah minimum {min_trades}")

        if kl_uji:
            try:
                r_uji = bt.run_backtest(kl_uji, cfg, warmup_bars,
                                        daily_klines=_harian_untuk(kl_uji))
                s_uji = bt.summarize(r_uji)
                item.uji = s_uji
                item.skor_uji = hitung_skor(s_uji, metrik)
            except Exception:
                item.catatan = (item.catatan + "; " if item.catatan else "") + \
                               "periode uji gagal dijalankan"

        hasil_grid.hasil.append(item)

    if progress_cb:
        progress_cb(1.0)

    # Diurutkan berdasarkan skor LATIH, karena periode uji tidak boleh ikut
    # menentukan pilihan. Kalau periode uji dipakai untuk memilih, ia berhenti
    # menjadi data yang belum pernah dilihat dan validasinya kehilangan makna.
    # Hasil yang tidak andal selalu ditaruh di bawah.
    hasil_grid.hasil.sort(key=lambda h: (h.andal, h.skor_latih), reverse=True)
    hasil_grid.dilewati = dilewati
    hasil_grid.detik = time.time() - mulai

    andal = [h for h in hasil_grid.hasil if h.andal]
    if not andal and hasil_grid.hasil:
        hasil_grid.peringatan.append(
            f"Tidak satu pun kombinasi mencapai {min_trades} trade. Seluruh hasil "
            f"berasal dari sampel yang terlalu kecil untuk disimpulkan.")
    if len(kombinasi) > 100:
        hasil_grid.peringatan.append(
            f"{len(kombinasi):,} kombinasi diuji. Semakin banyak kombinasi, semakin "
            f"besar peluang hasil terbaik muncul karena kebetulan. Utamakan kombinasi "
            f"dengan degradasi kecil, bukan skor latih tertinggi.")
    return hasil_grid


def ringkas_untuk_tabel(hasil: HasilGridSearch, top_n: int = 20) -> list[dict]:
    """Bentuk data siap tampil untuk dashboard atau terminal."""
    baris = []
    for h in hasil.hasil[:top_n]:
        b = {
            "params": h.params,
            "andal": h.andal,
            "catatan": h.catatan,
            "trades_latih": h.latih.get("total_trades", 0),
            "return_latih": round(float(h.latih.get("total_return_pct", 0.0)), 3),
            "winrate_latih": round(float(h.latih.get("win_rate", 0.0)), 2),
            "maxdd_latih": round(float(h.latih.get("max_drawdown_pct", 0.0)), 3),
            "skor_latih": round(h.skor_latih, 4),
        }
        if h.uji is not None:
            b.update({
                "trades_uji": h.uji.get("total_trades", 0),
                "return_uji": round(float(h.uji.get("total_return_pct", 0.0)), 3),
                "winrate_uji": round(float(h.uji.get("win_rate", 0.0)), 2),
                "maxdd_uji": round(float(h.uji.get("max_drawdown_pct", 0.0)), 3),
                "skor_uji": round(h.skor_uji, 4) if h.skor_uji is not None else None,
                "degradasi": round(h.degradasi, 4) if h.degradasi is not None else None,
            })
        baris.append(b)
    return baris


def cetak_tabel(hasil: HasilGridSearch, top_n: int = 15) -> None:
    """Cetak hasil ke terminal dengan format yang mudah dibaca."""
    print(f"\n{'='*104}")
    print(f"HASIL GRID SEARCH  (metrik: {hasil.metrik})")
    print(f"{'='*104}")
    print(f"Kombinasi diuji     : {hasil.total_kombinasi:,}")
    print(f"Dipangkas (mubazir) : {hasil.dipangkas:,}")
    print(f"Dilewati (invalid)  : {hasil.dilewati:,}")
    print(f"Bar latih / uji     : {hasil.bar_latih:,} / {hasil.bar_uji:,}")
    print(f"Waktu               : {hasil.detik:.1f} detik")
    if hasil.dibatalkan:
        print("STATUS              : DIBATALKAN sebelum selesai")

    if not hasil.hasil:
        print("\nTidak ada hasil.")
        return

    ada_uji = hasil.hasil[0].uji is not None
    print(f"\n{'#':>3} {'parameter':44} {'trade':>6} {'ret.latih':>10}", end="")
    if ada_uji:
        print(f" {'ret.UJI':>9} {'degradasi':>10}", end="")
    print(f" {'andal':>6}")
    print("-" * 104)

    for i, h in enumerate(hasil.hasil[:top_n], 1):
        p = ", ".join(f"{k}={v}" for k, v in sorted(h.params.items()))
        if len(p) > 44:
            p = p[:41] + "..."
        print(f"{i:>3} {p:44} {h.latih.get('total_trades', 0):>6} "
              f"{h.latih.get('total_return_pct', 0.0):>9.2f}%", end="")
        if ada_uji:
            ru = h.uji.get("total_return_pct", 0.0) if h.uji else 0.0
            dg = h.degradasi
            print(f" {ru:>8.2f}% {dg:>10.3f}" if dg is not None else f" {ru:>8.2f}% {'-':>10}", end="")
        print(f" {'ya' if h.andal else 'TIDAK':>6}")

    for w in hasil.peringatan:
        print(f"\n  PERINGATAN: {w}")

    if ada_uji:
        print("\n  Cara membaca: kolom ret.UJI berasal dari periode yang TIDAK dipakai")
        print("  saat memilih. Degradasi besar berarti hasil latih tidak bertahan pada")
        print("  data baru, yaitu tanda overfitting. Pilih yang ret.UJI-nya tetap wajar.")


# ---------------------------------------------------------------------
# Parser spesifikasi untuk baris perintah
# ---------------------------------------------------------------------
# Kunci boolean perlu penanganan khusus: string "False" bernilai True kalau
# hanya dicek kebenarannya, dan kesalahan seperti ini sulit terlihat karena
# grid tetap berjalan dan hanya memberi hasil yang salah diam diam.
_KUNCI_BOOL = ("USE_ATR_EXIT",)
_KUNCI_INT = ("ATR_PERIOD",)


def _konversi_nilai(kunci: str, teks: str):
    teks = teks.strip()
    if kunci in _KUNCI_BOOL:
        rendah = teks.lower()
        if rendah in ("true", "1", "on", "yes", "ya"):
            return True
        if rendah in ("false", "0", "off", "no", "tidak"):
            return False
        raise GridSearchError(f"Nilai boolean '{teks}' untuk {kunci} tidak dikenal.")
    try:
        return int(teks) if kunci in _KUNCI_INT else float(teks)
    except ValueError:
        raise GridSearchError(f"Nilai '{teks}' untuk {kunci} bukan angka.")


def parse_spec_cli(teks: str) -> dict:
    """Ubah spesifikasi ringkas dari baris perintah menjadi dict grid.

    Dua bentuk yang diterima, dipisah koma antar parameter:

        SL_PCT=1:4:0.5      rentang mulai:sampai:langkah
        TP_PCT=2|4|6        daftar nilai eksplisit
        USE_ATR_EXIT=false  nilai tunggal

    Contoh lengkap:
        "USE_ATR_EXIT=false,SL_PCT=1:4:0.5,TP_PCT=2|4|6"
    """
    if not teks or not teks.strip():
        raise GridSearchError("Spesifikasi grid kosong.")

    spec: dict = {}
    for bagian in teks.split(","):
        bagian = bagian.strip()
        if not bagian:
            continue
        if "=" not in bagian:
            raise GridSearchError(
                f"Bagian '{bagian}' tidak memuat tanda sama dengan. "
                f"Format yang benar: NAMA=nilai.")
        kunci, nilai = bagian.split("=", 1)
        kunci, nilai = kunci.strip(), nilai.strip()
        if not kunci:
            raise GridSearchError(f"Nama parameter kosong pada '{bagian}'.")

        if ":" in nilai:
            potong = nilai.split(":")
            if len(potong) != 3:
                raise GridSearchError(
                    f"Rentang '{nilai}' untuk {kunci} harus berbentuk "
                    f"mulai:sampai:langkah.")
            if kunci in _KUNCI_BOOL:
                raise GridSearchError(f"{kunci} bertipe boolean, tidak bisa memakai rentang.")
            mulai, sampai, langkah = (_konversi_nilai(kunci, x) for x in potong)
            deret = buat_rentang(float(mulai), float(sampai), float(langkah))
            spec[kunci] = [int(x) for x in deret] if kunci in _KUNCI_INT else deret
        elif "|" in nilai:
            spec[kunci] = [_konversi_nilai(kunci, x) for x in nilai.split("|")]
        else:
            spec[kunci] = [_konversi_nilai(kunci, nilai)]
    if not spec:
        raise GridSearchError("Spesifikasi grid tidak menghasilkan parameter apa pun.")
    return spec
