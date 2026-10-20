from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from backtesting import backtest as bt
from backtesting import parity
from strategy.indicators import Kline, direction_window_bars, zone_window_bars


def parity_trend_window(config: dict) -> int:
    return direction_window_bars(config)


KUNCI_ATR = (
    "ATR_PERIOD",
    "ATR_MULT_SL",
    "ATR_MULT_TP",
    "ATR_MULT_BE_TRIGGER",
    "ATR_MULT_BE_LOCK",
    "ATR_MULT_TRAIL_START",
    "ATR_MULT_TRAIL",
)
KUNCI_PERSEN = (
    "SL_PCT",
    "TP_PCT",
    "BE_TRIGGER_PCT",
    "BE_LOCK_PCT",
    "TRAILING_START_PCT",
    "TRAILING_STEP_PCT",
)

MAX_KOMBINASI = 2000

MAX_KOMBINASI_PORTFOLIO = 150

METRIK_TERSEDIA = (
    "total_return_pct",
    "profit_factor",
    "return_per_drawdown",
    "win_rate",
)


class GridSearchError(Exception):
    pass


@dataclass
class HasilKombinasi:

    params: dict
    latih: dict
    uji: Optional[dict] = None
    skor_latih: float = 0.0
    skor_uji: Optional[float] = None
    andal: bool = True
    catatan: str = ""

    @property
    def degradasi(self) -> Optional[float]:
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


def buat_rentang(
    mulai: float, sampai: float, langkah: float, bulatkan: int = 6
) -> list[float]:
    if langkah <= 0:
        raise GridSearchError("Langkah rentang harus lebih besar dari nol.")
    if sampai < mulai:
        raise GridSearchError(
            "Nilai akhir rentang tidak boleh lebih kecil dari nilai awal."
        )
    n = int(math.floor((sampai - mulai) / langkah + 1e-9)) + 1
    return [round(mulai + i * langkah, bulatkan) for i in range(n)]


def _pastikan_kunci_berefek(spec: dict) -> None:
    tidak_dikenal = [k for k in spec if k not in bt.OVERRIDE_CASTERS]
    if tidak_dikenal:
        raise GridSearchError(
            "Parameter berikut tidak bisa dioptimasi oleh backtest sehingga "
            "nilainya tidak akan berpengaruh: "
            + ", ".join(sorted(tidak_dikenal))
            + ". Parameter yang didukung: "
            + ", ".join(sorted(bt.OVERRIDE_CASTERS))
            + "."
        )


def _normalkan_spec(spec: dict) -> dict[str, list]:
    _pastikan_kunci_berefek(spec)
    keluar: dict[str, list] = {}
    for kunci, nilai in spec.items():
        if isinstance(nilai, dict):
            wajib = ("mulai", "sampai", "langkah")
            if not all(k in nilai for k in wajib):
                raise GridSearchError(
                    f"Rentang '{kunci}' harus memuat {', '.join(wajib)}."
                )
            keluar[kunci] = buat_rentang(
                float(nilai["mulai"]), float(nilai["sampai"]), float(nilai["langkah"])
            )
        elif isinstance(nilai, (list, tuple)):
            if not nilai:
                raise GridSearchError(f"Daftar nilai '{kunci}' kosong.")
            keluar[kunci] = list(nilai)
        else:
            keluar[kunci] = [nilai]
    if not keluar:
        raise GridSearchError("Grid parameter kosong, tidak ada yang bisa diuji.")
    return keluar


def expand_grid(
    spec: dict, max_kombinasi: Optional[int] = None, pakai_atr: Optional[bool] = None
) -> tuple[list[dict], int]:
    batas = MAX_KOMBINASI if max_kombinasi is None else int(max_kombinasi)
    if batas < 1:
        raise GridSearchError("Batas kombinasi harus lebih besar dari nol.")
    dinormalkan = _normalkan_spec(spec)
    kunci = list(dinormalkan)
    total_mentah = 1
    for k in kunci:
        total_mentah *= len(dinormalkan[k])
    if total_mentah > batas * 20:
        raise GridSearchError(
            f"Grid menghasilkan {total_mentah:,} kombinasi mentah. "
            f"Terlalu besar untuk diproses. Persempit rentang atau perbesar langkah."
        )

    terlihat: set[tuple] = set()
    kombinasi: list[dict] = []
    dipangkas = 0

    for nilai in itertools.product(*(dinormalkan[k] for k in kunci)):
        calon = dict(zip(kunci, nilai, strict=True))
        if "USE_ATR_EXIT" in calon:
            aktif = bool(calon["USE_ATR_EXIT"])
        else:
            aktif = pakai_atr
        if aktif is None:
            relevan = dict(calon)
        else:
            relevan = {
                k: v
                for k, v in calon.items()
                if not (aktif and k in KUNCI_PERSEN)
                and not (not aktif and k in KUNCI_ATR)
            }
        sidik = tuple(sorted(relevan.items(), key=lambda kv: kv[0]))
        if sidik in terlihat:
            dipangkas += 1
            continue
        terlihat.add(sidik)
        kombinasi.append(calon)

    if len(kombinasi) > batas:
        raise GridSearchError(
            f"Grid menghasilkan {len(kombinasi):,} kombinasi efektif, "
            f"melebihi batas {batas:,}. Persempit rentang atau perbesar langkah."
        )
    return kombinasi, dipangkas


def hitung_skor(ringkas: dict, metrik: str) -> float:
    if metrik == "total_return_pct":
        nilai = float(ringkas.get("total_return_pct", 0.0))
    elif metrik == "profit_factor":
        nilai = float(ringkas.get("profit_factor", 0.0))
    elif metrik == "win_rate":
        nilai = float(ringkas.get("win_rate", 0.0))
    elif metrik == "return_per_drawdown":
        dd = abs(float(ringkas.get("max_drawdown_pct", 0.0)))
        ret = float(ringkas.get("total_return_pct", 0.0))
        nilai = ret / max(dd, 1.0)
    else:
        raise GridSearchError(
            f"Metrik '{metrik}' tidak dikenal. Pilihan: {', '.join(METRIK_TERSEDIA)}."
        )
    if math.isnan(nilai):
        return 0.0
    if math.isinf(nilai):
        return 1e9 if nilai > 0 else -1e9
    return nilai


def run_grid_search(
    klines: Sequence[Kline],
    base_config: dict,
    spec: dict,
    warmup_bars: int,
    metrik: str = "total_return_pct",
    rasio_latih: float = 0.7,
    min_trades: int = 10,
    progress_cb: Optional[Callable[[float], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    btc_klines: Optional[Sequence[Kline]] = None,
    native_prebuilt: Optional[tuple] = None,
) -> HasilGridSearch:
    if metrik not in METRIK_TERSEDIA:
        raise GridSearchError(
            f"Metrik '{metrik}' tidak dikenal. Pilihan: {', '.join(METRIK_TERSEDIA)}."
        )
    if not 0.1 <= rasio_latih <= 1.0:
        raise GridSearchError("rasio_latih harus di antara 0.1 dan 1.0.")

    klines = list(klines)
    n = len(klines)
    if n <= warmup_bars + 10:
        raise GridSearchError(
            f"Data terlalu pendek: {n} candle dengan warmup {warmup_bars}."
        )

    kombinasi, dipangkas = expand_grid(
        spec, pakai_atr=bool(base_config.get("USE_ATR_EXIT", False))
    )
    hasil_grid = HasilGridSearch(
        total_kombinasi=len(kombinasi), dipangkas=dipangkas, metrik=metrik
    )

    interval_sim = str(base_config.get("CONFIRM_INTERVAL", "5m") or "5m")
    butuh_arah = bool(base_config.get("DIRECTION_FILTER_ENABLED", False))
    butuh_zona = bool(base_config.get("ZONE_FILTER_ENABLED", False))
    try:
        butuh_warmup = parity.gate_warmup_bars(base_config, interval_sim)
    except ValueError as exc:
        raise GridSearchError(str(exc)) from exc
    if int(warmup_bars) < butuh_warmup:
        bagian = []
        if butuh_arah:
            bagian.append(
                f"layer arah {base_config.get('DIRECTION_INTERVAL', '1d')} "
                f"butuh {parity_trend_window(base_config)} candle tertutup"
            )
        if butuh_zona:
            bagian.append(
                f"layer zona {base_config.get('ZONE_INTERVAL', '1h')} "
                f"butuh {zone_window_bars(base_config)} candle "
                f"{base_config.get('ZONE_INTERVAL', '1h')} tertutup"
            )
        hasil_grid.peringatan.append(
            f"Warmup {warmup_bars} bar dinaikkan menjadi {butuh_warmup} bar karena "
            f"{' dan '.join(bagian)}. Unduhan data harus mencakup rentang warmup ini."
        )
        warmup_bars = butuh_warmup

    if rasio_latih >= 1.0:
        potong = n
        kl_latih, kl_uji = klines, []
        hasil_grid.peringatan.append(
            "Pemisahan periode uji dimatikan (rasio_latih=1.0). Hasil peringkat "
            "TIDAK tervalidasi pada data baru dan sangat rentan overfitting."
        )
    else:
        potong = warmup_bars + int((n - warmup_bars) * rasio_latih)
        kl_latih = klines[:potong]
        awal_uji = max(0, potong - warmup_bars)
        kl_uji = klines[awal_uji:]

    hasil_grid.bar_latih = max(0, len(kl_latih) - warmup_bars)
    hasil_grid.bar_uji = max(0, len(kl_uji) - warmup_bars) if kl_uji else 0

    arah_latih = arah_uji = None
    if butuh_arah:
        arah_latih = parity.build_direction_klines(kl_latih, base_config, interval_sim)
        arah_uji = (
            parity.build_direction_klines(kl_uji, base_config, interval_sim)
            if kl_uji
            else []
        )
    zona_latih = zona_uji = None
    if butuh_zona:
        zona_latih = parity.build_zone_klines(kl_latih, base_config, interval_sim)
        zona_uji = (
            parity.build_zone_klines(kl_uji, base_config, interval_sim) if kl_uji else []
        )

    if kl_uji and hasil_grid.bar_uji < 50:
        hasil_grid.peringatan.append(
            f"Periode uji hanya {hasil_grid.bar_uji} bar. Terlalu pendek untuk "
            f"memvalidasi apa pun. Perpanjang rentang hari atau turunkan rasio_latih."
        )

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
        except (bt.BacktestError, GridSearchError):
            dilewati += 1
            continue

        cfg["_symbol"] = base_config.get("_symbol", "")

        try:
            r_latih = bt.run_backtest(
                kl_latih,
                cfg,
                warmup_bars,
                btc_klines=btc_klines,
                direction_klines=arah_latih,
                zone_klines=zona_latih,
            )
            s_latih = bt.summarize(r_latih)
        except Exception as exc:
            dilewati += 1
            hasil_grid.hasil.append(
                HasilKombinasi(
                    params=dict(params),
                    latih={},
                    andal=False,
                    catatan=(
                        f"simulasi latih gagal: {type(exc).__name__}: "
                        f"{str(exc)[:180]}"
                    ),
                )
            )
            continue

        item = HasilKombinasi(
            params=dict(params),
            latih=s_latih,
            skor_latih=hitung_skor(s_latih, metrik),
        )

        if int(s_latih.get("total_trades", 0)) < min_trades:
            item.andal = False
            item.catatan = (
                f"hanya {s_latih.get('total_trades', 0)} trade pada periode "
                f"latih, di bawah minimum {min_trades}"
            )

        if kl_uji:
            try:
                r_uji = bt.run_backtest(
                    kl_uji,
                    cfg,
                    warmup_bars,
                    btc_klines=btc_klines,
                    direction_klines=arah_uji,
                    zone_klines=zona_uji,
                )
                s_uji = bt.summarize(r_uji)
                item.uji = s_uji
                item.skor_uji = hitung_skor(s_uji, metrik)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {str(exc)[:180]}"
                item.catatan = (
                    (item.catatan + "; ") if item.catatan else ""
                ) + f"periode uji gagal dijalankan: {detail}"

        hasil_grid.hasil.append(item)

    if progress_cb:
        progress_cb(1.0)

    hasil_grid.hasil.sort(key=lambda h: (h.andal, h.skor_latih), reverse=True)
    hasil_grid.dilewati = dilewati
    hasil_grid.detik = time.time() - mulai

    if dilewati:
        hasil_grid.peringatan.append(
            f"{dilewati} kombinasi tidak diperingkat karena parameter tidak valid "
            "atau simulasi gagal."
        )

    andal = [h for h in hasil_grid.hasil if h.andal]
    if not andal and hasil_grid.hasil:
        hasil_grid.peringatan.append(
            f"Tidak satu pun kombinasi mencapai {min_trades} trade. Seluruh hasil "
            f"berasal dari sampel yang terlalu kecil untuk disimpulkan."
        )
    if len(kombinasi) > 100:
        hasil_grid.peringatan.append(
            f"{len(kombinasi):,} kombinasi diuji. Semakin banyak kombinasi, semakin "
            f"besar peluang hasil terbaik muncul karena kebetulan. Utamakan kombinasi "
            f"dengan degradasi kecil, bukan skor latih tertinggi."
        )
    return hasil_grid


def run_portfolio_grid_search(
    store,
    base_config: dict,
    interval: str,
    warmup_ms: int,
    spec: dict,
    *,
    metrik: str = "total_return_pct",
    rasio_latih: float = 0.7,
    min_trades: int = 5,
    progress_cb: Optional[Callable[[float], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    btc_klines: Optional[Sequence[Kline]] = None,
    native_prebuilt: Optional[tuple] = None,
) -> HasilGridSearch:
    from backtesting import portfolio_backtest as pbt

    mulai = time.time()
    if not 0.1 <= float(rasio_latih) <= 1.0:
        raise GridSearchError("rasio_latih harus di antara 0.1 dan 1.0.")
    min_trades = int(min_trades)
    if min_trades < 1:
        raise GridSearchError("min_trades minimal 1.")

    kombinasi, dipangkas = expand_grid(
        spec,
        max_kombinasi=MAX_KOMBINASI_PORTFOLIO,
        pakai_atr=bool(base_config.get("USE_ATR_EXIT", False)),
    )
    hasil_grid = HasilGridSearch(
        total_kombinasi=len(kombinasi), dipangkas=dipangkas, metrik=metrik
    )

    timeline, series_of = pbt.build_timeline(store, interval)
    if not timeline:
        raise GridSearchError(
            "Garis waktu kosong, tidak ada candle yang bisa diproses."
        )
    awal_data, akhir_data = timeline[0], timeline[-1]

    if float(rasio_latih) >= 1.0:
        potong_ms = akhir_data
        hasil_grid.peringatan.append(
            "Pemisahan periode uji dimatikan (rasio_latih=1.0). Hasil peringkat "
            "TIDAK tervalidasi pada data baru dan sangat rentan overfitting."
        )
    else:
        potong_ms = (
            awal_data
            + warmup_ms
            + int((akhir_data - awal_data - warmup_ms) * float(rasio_latih))
        )
        if potong_ms <= awal_data + warmup_ms:
            potong_ms = awal_data + warmup_ms + 1

    hasil_grid.bar_latih = sum(1 for t in timeline if t <= potong_ms)
    hasil_grid.bar_uji = (
        len(timeline) - hasil_grid.bar_latih if float(rasio_latih) < 1.0 else 0
    )
    if float(rasio_latih) < 1.0 and hasil_grid.bar_uji < 50:
        hasil_grid.peringatan.append(
            f"Periode uji hanya {hasil_grid.bar_uji} bar (di bawah 50). "
            f"Skor uji pada sampel sepersis ini sulit diandalkan."
        )

    native_arah = None
    native_zona = None
    if native_prebuilt is not None:
        _, _, native_arah, native_zona, _ = pbt.unpack_prebuilt(native_prebuilt)
    native_htf = native_arah is not None or native_zona is not None
    arah_of = (
        native_arah
        if native_arah is not None
        else pbt.build_direction_lookups(store, list(series_of), base_config, interval)
    )
    zona_of = (
        native_zona
        if native_zona is not None
        else pbt.build_zone_lookups(store, list(series_of), base_config, interval)
    )
    timing_of = pbt.build_timing_lookups(store, list(series_of), base_config, interval)
    if (arah_of or zona_of) and not native_htf:
        butuh_warmup = parity.gate_warmup_ms(base_config, interval)
        if int(warmup_ms) < butuh_warmup:
            hasil_grid.peringatan.append(
                f"Warmup {warmup_ms} ms dinaikkan menjadi {butuh_warmup} ms karena layer "
                f"timeframe tinggi yang aktif (arah + zona) butuh paling sedikit "
                f"{parity.gate_warmup_bars(base_config, interval)} "
                f"candle {interval} sebagai pemanasan."
            )
            warmup_ms = butuh_warmup
    prebuilt = (timeline, series_of, arah_of, zona_of, timing_of)
    dilewati = 0
    total = len(kombinasi)

    for idx, params in enumerate(kombinasi):
        if cancel_cb is not None and cancel_cb():
            hasil_grid.dibatalkan = True
            break
        item = HasilKombinasi(params=dict(params), latih={})
        try:
            cfg = bt.apply_overrides(base_config, params)
            bt.validate_params(cfg)
        except (bt.BacktestError, GridSearchError) as exc:
            dilewati += 1
            item.andal = False
            item.catatan = f"parameter tidak valid: {exc}"
            hasil_grid.hasil.append(item)
            continue
        try:

            def _prog_latih(frac, _idx=idx):
                if progress_cb:
                    progress_cb((_idx + max(0.0, min(1.0, float(frac))) * 0.45) / total)

            r_latih = pbt.run_portfolio_backtest(
                store,
                cfg,
                interval,
                warmup_ms=warmup_ms,
                end_ms=potong_ms,
                prebuilt=prebuilt,
                progress_cb=_prog_latih,
                cancel_cb=cancel_cb,
                btc_klines=btc_klines,
            )
            s_latih = pbt.summarize_portfolio(r_latih)
            item.latih = s_latih
            item.skor_latih = hitung_skor(s_latih, metrik)
            if int(s_latih.get("total_trades", 0)) < min_trades:
                item.andal = False
                item.catatan = (
                    f"hanya {s_latih.get('total_trades', 0)} trade pada "
                    f"periode latih, di bawah minimum {min_trades}"
                )

            if float(rasio_latih) < 1.0:
                try:

                    def _prog_uji(frac, _idx=idx):
                        if progress_cb:
                            progress_cb(
                                (_idx + 0.45 + max(0.0, min(1.0, float(frac))) * 0.55)
                                / total
                            )

                    r_uji = pbt.run_portfolio_backtest(
                        store,
                        cfg,
                        interval,
                        warmup_ms=warmup_ms,
                        start_ms=potong_ms - warmup_ms,
                        prebuilt=prebuilt,
                        progress_cb=_prog_uji,
                        cancel_cb=cancel_cb,
                        btc_klines=btc_klines,
                    )
                    s_uji = pbt.summarize_portfolio(r_uji)
                    item.uji = s_uji
                    item.skor_uji = hitung_skor(s_uji, metrik)
                except pbt.BacktestError as exc:
                    item.catatan = (
                        (item.catatan + "; ") if item.catatan else ""
                    ) + f"periode uji gagal dijalankan: {exc}"
        except pbt.BacktestError as exc:
            if "dibatalkan" in str(exc).lower():
                hasil_grid.dibatalkan = True
                break
            dilewati += 1
            item.andal = False
            item.catatan = f"simulasi latih gagal: {exc}"

        hasil_grid.hasil.append(item)
        if progress_cb:
            progress_cb((idx + 1) / total)

    if progress_cb:
        progress_cb(1.0)

    hasil_grid.hasil.sort(key=lambda h: (h.andal, h.skor_latih), reverse=True)
    hasil_grid.dilewati = dilewati
    hasil_grid.detik = time.time() - mulai

    if dilewati:
        hasil_grid.peringatan.append(
            f"{dilewati} kombinasi tidak diperingkat karena parameter tidak valid "
            "atau simulasi gagal."
        )

    andal = [h for h in hasil_grid.hasil if h.andal]
    if not andal and hasil_grid.hasil:
        hasil_grid.peringatan.append(
            f"Tidak satu pun kombinasi mencapai {min_trades} trade. Seluruh hasil "
            f"berasal dari sampel yang terlalu kecil untuk disimpulkan."
        )
    if len(kombinasi) > 100:
        hasil_grid.peringatan.append(
            f"{len(kombinasi):,} kombinasi diuji. Semakin banyak kombinasi, semakin "
            f"besar peluang hasil terbaik muncul karena kebetulan. Utamakan kombinasi "
            f"dengan degradasi kecil, bukan skor latih tertinggi."
        )
    return hasil_grid


def _pf_aman(nilai) -> Optional[float]:
    if nilai is None:
        return None
    v = float(nilai)
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, 2)


def ringkas_untuk_tabel(hasil: HasilGridSearch, top_n: int = 20) -> list[dict]:
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
            "pf_latih": _pf_aman(h.latih.get("profit_factor")),
            "skor_latih": round(h.skor_latih, 4),
        }
        if h.uji is not None:
            b.update(
                {
                    "trades_uji": h.uji.get("total_trades", 0),
                    "return_uji": round(float(h.uji.get("total_return_pct", 0.0)), 3),
                    "winrate_uji": round(float(h.uji.get("win_rate", 0.0)), 2),
                    "maxdd_uji": round(float(h.uji.get("max_drawdown_pct", 0.0)), 3),
                    "pf_uji": _pf_aman(h.uji.get("profit_factor")),
                    "skor_uji": (
                        round(h.skor_uji, 4) if h.skor_uji is not None else None
                    ),
                    "degradasi": (
                        round(h.degradasi, 4) if h.degradasi is not None else None
                    ),
                }
            )
        baris.append(b)
    return baris


def cetak_tabel(hasil: HasilGridSearch, top_n: int = 15) -> None:
    print(f"\n{'='*104}")
    print(f"HASIL GRID SEARCH  (metrik: {hasil.metrik})")
    print(f"{'='*104}")
    print(f"Kombinasi diuji     : {hasil.total_kombinasi:,}")
    print(f"Dipangkas (mubazir) : {hasil.dipangkas:,}")
    print(f"Dilewati (tidak valid/gagal): {hasil.dilewati:,}")
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
        print(
            f"{i:>3} {p:44} {h.latih.get('total_trades', 0):>6} "
            f"{h.latih.get('total_return_pct', 0.0):>9.2f}%",
            end="",
        )
        if ada_uji:
            ru = h.uji.get("total_return_pct", 0.0) if h.uji else 0.0
            dg = h.degradasi
            print(
                (
                    f" {ru:>8.2f}% {dg:>10.3f}"
                    if dg is not None
                    else f" {ru:>8.2f}% {'-':>10}"
                ),
                end="",
            )
        print(f" {'ya' if h.andal else 'TIDAK':>6}")

    error_rows = [
        h for h in hasil.hasil if h.catatan and "gagal" in h.catatan.lower()
    ]
    if error_rows:
        print("\n  Rincian kegagalan simulasi:")
        for h in error_rows[:5]:
            params = ", ".join(f"{k}={v}" for k, v in sorted(h.params.items()))
            print(f"  - {params}: {h.catatan}")
        if len(error_rows) > 5:
            print(f"  - dan {len(error_rows) - 5} kegagalan lainnya")

    for w in hasil.peringatan:
        print(f"\n  PERINGATAN: {w}")

    if ada_uji:
        print("\n  Cara membaca: kolom ret.UJI berasal dari periode yang TIDAK dipakai")
        print("  saat memilih. Degradasi besar berarti hasil latih tidak bertahan pada")
        print(
            "  data baru, yaitu tanda overfitting. Pilih yang ret.UJI-nya tetap wajar."
        )


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
    except ValueError as exc:
        raise GridSearchError(f"Nilai '{teks}' untuk {kunci} bukan angka.") from exc


def parse_spec_cli(teks: str) -> dict:
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
                f"Format yang benar: NAMA=nilai."
            )
        kunci, nilai = bagian.split("=", 1)
        kunci, nilai = kunci.strip(), nilai.strip()
        if not kunci:
            raise GridSearchError(f"Nama parameter kosong pada '{bagian}'.")

        if ":" in nilai:
            potong = nilai.split(":")
            if len(potong) != 3:
                raise GridSearchError(
                    f"Rentang '{nilai}' untuk {kunci} harus berbentuk "
                    f"mulai:sampai:langkah."
                )
            if kunci in _KUNCI_BOOL:
                raise GridSearchError(
                    f"{kunci} bertipe boolean, tidak bisa memakai rentang."
                )
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


def selftest() -> bool:
    from copy import deepcopy

    from config.config import PUMP_CONFIG
    from backtesting import synthetic_data as sd

    ok_all = True

    def check(nama, kondisi, ekstra=""):
        nonlocal ok_all
        if not kondisi:
            ok_all = False
        print(("  LULUS " if kondisi else "  GAGAL ") + nama + (("  -> " + str(ekstra)) if ekstra else ""))

    print("Selftest grid_search")

    check("buat_rentang dasar", buat_rentang(1.0, 2.0, 0.5) == [1.0, 1.5, 2.0])
    try:
        buat_rentang(2.0, 1.0, 0.5)
        check("buat_rentang menolak akhir < awal", False)
    except GridSearchError:
        check("buat_rentang menolak akhir < awal", True)
    try:
        buat_rentang(1.0, 2.0, 0.0)
        check("buat_rentang menolak langkah nol", False)
    except GridSearchError:
        check("buat_rentang menolak langkah nol", True)

    try:
        expand_grid({"BOGUS_KEY": [1.0]})
        check("expand_grid menolak kunci tanpa efek", False)
    except GridSearchError:
        check("expand_grid menolak kunci tanpa efek", True)

    kombinasi, dipangkas = expand_grid({"USE_ATR_EXIT": [True], "SL_PCT": [1.0, 2.0], "ATR_MULT_SL": [6.0, 7.0]})
    check("expand_grid memangkas parameter persen saat ATR aktif", len(kombinasi) == 2 and dipangkas == 2, f"{len(kombinasi)} kombinasi, {dipangkas} dipangkas")
    kombinasi, dipangkas = expand_grid({"USE_ATR_EXIT": [False], "ATR_MULT_SL": [6.0, 7.0], "SL_PCT": [1.0, 2.0]})
    check("expand_grid memangkas parameter ATR saat ATR mati", len(kombinasi) == 2 and dipangkas == 2, f"{len(kombinasi)} kombinasi, {dipangkas} dipangkas")
    try:
        expand_grid({"SL_PCT": []})
        check("expand_grid menolak daftar kosong", False)
    except GridSearchError:
        check("expand_grid menolak daftar kosong", True)

    check("hitung_skor total_return_pct", hitung_skor({"total_return_pct": 5.0}, "total_return_pct") == 5.0)
    check("hitung_skor profit_factor", hitung_skor({"profit_factor": 2.5}, "profit_factor") == 2.5)
    check("hitung_skor win_rate", hitung_skor({"win_rate": 42.0}, "win_rate") == 42.0)
    check("hitung_skor return_per_drawdown", abs(hitung_skor({"total_return_pct": 10.0, "max_drawdown_pct": -5.0}, "return_per_drawdown") - 2.0) < 1e-12)
    check("hitung_skor NaN jadi nol", hitung_skor({"total_return_pct": float("nan")}, "total_return_pct") == 0.0)
    check("hitung_skor inf positif", hitung_skor({"total_return_pct": float("inf")}, "total_return_pct") == 1e9)
    check("hitung_skor inf negatif", hitung_skor({"total_return_pct": float("-inf")}, "total_return_pct") == -1e9)
    try:
        hitung_skor({}, "metrik_bohong")
        check("hitung_skor menolak metrik tidak dikenal", False)
    except GridSearchError:
        check("hitung_skor menolak metrik tidak dikenal", True)

    spec = parse_spec_cli("ATR_MULT_SL=1:2:0.5,TP_PCT=3|4,USE_ATR_EXIT=true")
    check("parse_spec_cli rentang", spec["ATR_MULT_SL"] == [1.0, 1.5, 2.0], spec.get("ATR_MULT_SL"))
    check("parse_spec_cli daftar", spec["TP_PCT"] == [3.0, 4.0])
    check("parse_spec_cli boolean", spec["USE_ATR_EXIT"] == [True], spec.get("USE_ATR_EXIT"))
    for teks in ("", "   ", "tanpa_sama_dengan", "KUNCI=", "ATR_MULT_SL=1:2", "ATR_MULT_SL=abc", "USE_ATR_EXIT=1:2:0.5"):
        try:
            parse_spec_cli(teks)
            check(f"parse_spec_cli menolak {teks!r}", False)
        except GridSearchError:
            check(f"parse_spec_cli menolak {teks!r}", True)

    cfg = deepcopy(PUMP_CONFIG)
    cfg.update({
        "_symbol": "",
        "MODE": "PAPER",
        "DIRECTION_FILTER_ENABLED": False,
        "DIRECTION_ZONE_FILTER_ENABLED": False,
        "ZONE_FILTER_ENABLED": False,
        "TIMING_FILTER_ENABLED": False,
        "DEMAND_ZONE_FILTER_ENABLED": False,
        "BTC_FILTER_ENABLED": False,
        "ROLLING_VOLUME_FILTER_ENABLED": False,
        "PUMP_MIN_24H_CHANGE_PCT": -1000.0,
        "MIN_QUOTE_VOLUME_USDT_24H": 0.0,
        "USE_ATR_EXIT": False,
        "USE_TP": True,
        "USE_STOP_LOSS": True,
        "USE_BREAKEVEN": False,
        "USE_TRAILING": False,
        "SL_PCT": 2.0,
        "TP_PCT": 4.0,
        "TAKER_FEE_PCT": 0.1,
        "USE_BNB_FEE_DISCOUNT": False,
        "USE_RISK_PERCENT": False,
        "POSITION_SIZE_USDT": 100.0,
        "MAX_POSITION_USDT": 0.0,
        "BACKTEST_INITIAL_EQUITY_USDT": 1000.0,
        "MAX_HOLD_HOURS": 0.0,
        "COOLDOWN_MINUTES_AFTER_CLOSE": 0,
        "MIN_SECONDS_BETWEEN_TRADES": 0,
        "MAX_CHASE_PCT": 0.0,
    })
    klines = sd.seri_banyak_setup(harga=100.0, siklus=10, volume=9_000_000.0)

    try:
        run_grid_search(klines[:50], cfg, {"SL_PCT": [1.5]}, 300)
        check("run_grid_search menolak data pendek", False)
    except GridSearchError:
        check("run_grid_search menolak data pendek", True)
    try:
        run_grid_search(klines, cfg, {"SL_PCT": [1.5]}, 300, metrik="metrik_bohong")
        check("run_grid_search menolak metrik tidak dikenal", False)
    except GridSearchError:
        check("run_grid_search menolak metrik tidak dikenal", True)
    try:
        run_grid_search(klines, cfg, {"SL_PCT": [1.5]}, 300, rasio_latih=1.5)
        check("run_grid_search menolak rasio latih di luar rentang", False)
    except GridSearchError:
        check("run_grid_search menolak rasio latih di luar rentang", True)
    cfg["USE_TP"] = True
    hasil_tolak = run_grid_search(klines, cfg, {"SL_PCT": [1.5, 2.5], "TP_PCT": [1.0, 4.0]}, 300, min_trades=1)
    check("run_grid_search melewati kombinasi TP <= SL", hasil_tolak.total_kombinasi == 4 and len(hasil_tolak.hasil) == 2 and hasil_tolak.dilewati == 2, f"{hasil_tolak.total_kombinasi} kombinasi, {len(hasil_tolak.hasil)} hasil, {hasil_tolak.dilewati} dilewati")
    cfg["USE_TP"] = False

    hasil = run_grid_search(klines, cfg, {"SL_PCT": [1.5, 2.5], "TP_PCT": [3.0, 4.0]}, 300, min_trades=1)
    check("run_grid_search menjalankan seluruh kombinasi", hasil.total_kombinasi == 4 and len(hasil.hasil) == 4, f"{hasil.total_kombinasi} kombinasi, {len(hasil.hasil)} hasil")
    check("run_grid_search memeringkat hasil", all(hasil.hasil[i].skor_latih >= hasil.hasil[i + 1].skor_latih for i in range(len(hasil.hasil) - 1)))
    check("run_grid_search mencatat bar latih dan uji", hasil.bar_latih > 0 and hasil.bar_uji > 0, f"{hasil.bar_latih}/{hasil.bar_uji}")
    check("run_grid_search melaporkan periode uji", semua_uji(hasil), "item tanpa uji")

    cfg_htf = deepcopy(PUMP_CONFIG)
    cfg_htf.update({
        "_symbol": "",
        "MODE": "PAPER",
        "DIRECTION_FILTER_ENABLED": True,
        "DIRECTION_ZONE_FILTER_ENABLED": True,
        "ZONE_FILTER_ENABLED": True,
        "TIMING_FILTER_ENABLED": False,
        "DEMAND_ZONE_FILTER_ENABLED": False,
        "BTC_FILTER_ENABLED": False,
        "ROLLING_VOLUME_FILTER_ENABLED": False,
        "PUMP_MIN_24H_CHANGE_PCT": -1000.0,
        "MIN_QUOTE_VOLUME_USDT_24H": 0.0,
        "USE_ATR_EXIT": False,
        "SL_PCT": 2.0,
        "TP_PCT": 4.0,
    })
    klines_panjang = sd.seri_5m_trend(harga=100.0, jam_trend=1560)
    hasil_htf = run_grid_search(klines_panjang, cfg_htf, {"SL_PCT": [1.5, 2.5]}, 300, min_trades=1)
    check("run_grid_search menaikkan warmup gerbang timeframe tinggi", any("layer arah" in w and "layer zona" in w for w in hasil_htf.peringatan), [w[:80] for w in hasil_htf.peringatan])

    print("\nHASIL: " + ("SEMUA LULUS" if ok_all else "ADA YANG GAGAL"))
    return ok_all


def semua_uji(hasil: HasilGridSearch) -> bool:
    return all(item.uji is not None for item in hasil.hasil)


if __name__ == "__main__":
    import sys

    sys.exit(0 if selftest() else 1)
