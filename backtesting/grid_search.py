from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from backtesting import backtest as bt
from backtesting import parity
from strategy.indicators import Kline


def parity_trend_window(config: dict) -> int:
    from strategy import indicators as strategy_mod

    return strategy_mod.trend_window_bars(config)


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


def _normalkan_spec(spec: dict) -> dict[str, list]:
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


def cek_relasi_exit(cfg: dict) -> Optional[str]:
    if bool(cfg.get("USE_ATR_EXIT", False)):
        sl = float(cfg.get("ATR_MULT_SL", 0.0))
        tp = float(cfg.get("ATR_MULT_TP", 0.0))
        trail = float(cfg.get("ATR_MULT_TRAIL", 0.0))
        be_trigger = float(cfg.get("ATR_MULT_BE_TRIGGER", 0.0))
        be_lock = float(cfg.get("ATR_MULT_BE_LOCK", 0.0))
        trail_start = float(cfg.get("ATR_MULT_TRAIL_START", 0.0))
        if trail > sl:
            return (
                f"ATR_MULT_TRAIL ({trail:g}) melebihi ATR_MULT_SL ({sl:g}); "
                "trailing tidak boleh lebih lebar dari stop loss"
            )
        if be_trigger > trail_start:
            return (
                f"ATR_MULT_BE_TRIGGER ({be_trigger:g}) melebihi "
                f"ATR_MULT_TRAIL_START ({trail_start:g})"
            )
        if be_lock > be_trigger:
            return (
                f"ATR_MULT_BE_LOCK ({be_lock:g}) melebihi "
                f"ATR_MULT_BE_TRIGGER ({be_trigger:g})"
            )
        if tp <= sl:
            return (
                f"ATR_MULT_TP ({tp:g}) harus lebih besar dari "
                f"ATR_MULT_SL ({sl:g}) agar rasio risk-reward tidak terbalik"
            )
    else:
        if bool(cfg.get("USE_TP", True)) and bool(cfg.get("USE_STOP_LOSS", True)):
            tp = float(cfg.get("TP_PCT", 0.0))
            sl = float(cfg.get("SL_PCT", 0.0))
            if tp <= sl:
                return (
                    f"TP_PCT ({tp:g}) harus lebih besar dari SL_PCT ({sl:g}) "
                    "agar rasio risk-reward tidak terbalik"
                )
    return None


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
    trend_latih = trend_uji = None
    if bool(base_config.get("TREND_FILTER_ENABLED", False)):
        try:
            butuh_warmup = parity.trend_warmup_bars(base_config, interval_sim)
        except ValueError as exc:
            raise GridSearchError(str(exc)) from exc
        if int(warmup_bars) < butuh_warmup:
            hasil_grid.peringatan.append(
                f"Warmup {warmup_bars} bar dinaikkan menjadi {butuh_warmup} bar karena "
                f"gerbang trend {base_config.get('TREND_INTERVAL', '1h')} butuh "
                f"{parity_trend_window(base_config)} candle trend tertutup. Unduhan data "
                "harus mencakup rentang warmup ini."
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

    if bool(base_config.get("TREND_FILTER_ENABLED", False)):
        trend_latih = parity.build_trend_klines(kl_latih, base_config, interval_sim)
        trend_uji = (
            parity.build_trend_klines(kl_uji, base_config, interval_sim)
            if kl_uji
            else []
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
            pesan_relasi = cek_relasi_exit(cfg)
            if pesan_relasi:
                raise GridSearchError(pesan_relasi)
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
                trend_klines=trend_latih,
            )
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
                    trend_klines=trend_uji,
                )
                s_uji = bt.summarize(r_uji)
                item.uji = s_uji
                item.skor_uji = hitung_skor(s_uji, metrik)
            except Exception:
                item.catatan = (
                    item.catatan + "; " if item.catatan else ""
                ) + "periode uji gagal dijalankan"

        hasil_grid.hasil.append(item)

    if progress_cb:
        progress_cb(1.0)

    hasil_grid.hasil.sort(key=lambda h: (h.andal, h.skor_latih), reverse=True)
    hasil_grid.dilewati = dilewati
    hasil_grid.detik = time.time() - mulai

    if dilewati:
        hasil_grid.peringatan.append(
            f"{dilewati} kombinasi dilewati karena parameter tidak valid atau "
            "melanggar relasi wajib (mis. trailing melebihi SL). Kombinasi "
            "seperti itu tidak bisa disimpan ke Pengaturan, jadi tidak ikut "
            "diperingkat."
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

    trend_of = pbt.build_trend_lookups(store, list(series_of), base_config, interval)
    if trend_of:
        butuh_warmup = parity.trend_warmup_ms(base_config, interval)
        if int(warmup_ms) < butuh_warmup:
            hasil_grid.peringatan.append(
                f"Warmup {warmup_ms} ms dinaikkan menjadi {butuh_warmup} ms karena gerbang "
                f"trend {base_config.get('TREND_INTERVAL', '1h')} butuh "
                f"{parity_trend_window(base_config)} candle trend tertutup."
            )
            warmup_ms = butuh_warmup
    prebuilt = (timeline, series_of, trend_of)
    dilewati = 0
    total = len(kombinasi)

    for idx, params in enumerate(kombinasi):
        if cancel_cb is not None and cancel_cb():
            hasil_grid.dibatalkan = True
            break
        item = HasilKombinasi(params=dict(params), latih={})
        try:
            cfg = bt.apply_overrides(base_config, params)
            pesan_relasi = cek_relasi_exit(cfg)
            if pesan_relasi:
                raise GridSearchError(pesan_relasi)
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
            f"{dilewati} kombinasi dilewati karena parameter tidak valid atau "
            "melanggar relasi wajib (mis. trailing melebihi SL). Kombinasi "
            "seperti itu tidak bisa disimpan ke Pengaturan, jadi tidak ikut "
            "diperingkat."
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


def selftest() -> int:
    """Uji lokal pencarian grid dengan gerbang trend aktif.

    Menjaga dua hal yang pernah salah dan mudah terulang:
      * candle trend yang sudah dirangkai tidak boleh dirangkai dua kali,
      * penolakan gerbang trend harus benar-benar terlihat di hasil grid.
    Tidak menghubungi Binance sama sekali.
    """
    print("=== SELFTEST grid_search.py: gerbang trend di jalur grid ===")
    from backtesting.backtest_storage import KlineStore
    from backtesting.synthetic_data import (
        blok_setup_volume,
        cfg_gerbang_pump_nonaktif,
        seri_5m_trend,
    )
    from config.config import PUMP_CONFIG

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

    def seri_drift(
        arah: float,
        siklus: int = 12,
        jam_drift: int = 24,
        tick: float = 0.0006,
        volume: float = 5_000_000.0,
    ):
        """5m: drift searah selama sehari, ditutup satu blok setup, diulang."""
        out, i, harga = [], 0, 100.0
        for _ in range(siklus):
            seg = seri_5m_trend(
                harga=harga,
                jam_trend=jam_drift,
                arah=arah,
                tick=tick,
                volume=volume,
                mulai_index=i,
            )
            out.extend(seg)
            i += len(seg)
            harga = seg[-1].close
            blok, harga, i = blok_setup_volume(harga, i, volume)
            out.extend(blok)
        return out

    def konfig(trend: bool) -> dict:
        cfg = cfg_gerbang_pump_nonaktif(dict(PUMP_CONFIG))
        cfg.update(
            {
                "MIN_QUOTE_VOLUME_USDT_24H": 1_000_000,
                "BACKTEST_ENTRY_DELAY_BARS": 0,
                "BACKTEST_ENTRY_SPREAD_PCT": 0.0,
                "BACKTEST_SLIPPAGE_PCT": 0.0,
                "_symbol": "TESTUSDT",
                "USE_ATR_EXIT": False,
                "TP_PCT": 2.0,
                "SL_PCT": 1.0,
                "MAX_CHASE_PCT": 0.0,
                "MIN_SECONDS_BETWEEN_ENTRIES": 0,
                "MAX_OPEN_POSITIONS": 1,
                "BACKTEST_INITIAL_EQUITY_USDT": 1000.0,
                "POSITION_SIZE_PCT": 10.0,
                "TREND_FILTER_ENABLED": trend,
                "TREND_INTERVAL": "1h",
                "TREND_EMA_FAST": 20,
                "TREND_EMA_SLOW": 50,
                "TREND_ADX_PERIOD": 14,
                "TREND_ADX_MIN": 20.0,
                "TREND_LOOKBACK_BARS": 120,
            }
        )
        return cfg

    spec = {"TP_PCT": [2.0, 3.0], "SL_PCT": [1.0, 1.5]}

    def total_trade(hasil, kunci: str) -> int:
        return sum(
            int(r.get(kunci, 0) or 0) for r in ringkas_untuk_tabel(hasil, top_n=99)
        )

    for nama, arah, harus_lolos in (
        ("drift naik", 1.0, True),
        ("drift turun", -1.0, False),
    ):
        kl = seri_drift(arah)
        on = run_grid_search(
            kl, konfig(True), spec, warmup_bars=0, min_trades=1, rasio_latih=0.6
        )
        off = run_grid_search(
            kl, konfig(False), spec, warmup_bars=0, min_trades=1, rasio_latih=0.6
        )
        tr_on, tr_off = total_trade(on, "trades_latih"), total_trade(
            off, "trades_latih"
        )
        cek(
            f"[{nama}] warmup trend dinaikkan dan dilaporkan di peringatan",
            any("Warmup" in w for w in on.peringatan),
            len(on.peringatan),
        )
        cek(
            f"[{nama}] empat kombinasi exit dinilai",
            on.total_kombinasi == 4,
            on.total_kombinasi,
        )
        cek(f"[{nama}] periode uji tetap tersedia", on.bar_uji >= 50, on.bar_uji)
        if harus_lolos:
            cek(
                f"[{nama}] gerbang trend meloloskan sinyal (data trend dirangkai sekali)",
                tr_on >= 1,
                f"trend_on={tr_on} trend_off={tr_off}",
            )
        else:
            cek(
                f"[{nama}] gerbang trend memblokir sinyal latih",
                tr_on == 0 and tr_off >= 1,
                f"trend_on={tr_on} trend_off={tr_off}",
            )

    data = {"NAIKUSDT": seri_drift(1.0), "TURUNUSDT": seri_drift(-1.0)}
    with KlineStore.from_klines(data) as store:
        on_pf = run_portfolio_grid_search(
            store, konfig(True), "5m", 0, spec, min_trades=1, rasio_latih=0.6
        )
        off_pf = run_portfolio_grid_search(
            store, konfig(False), "5m", 0, spec, min_trades=1, rasio_latih=0.6
        )
    total_on = total_trade(on_pf, "trades_latih") + total_trade(on_pf, "trades_uji")
    total_off = total_trade(off_pf, "trades_latih") + total_trade(off_pf, "trades_uji")
    cek(
        "grid portofolio memakai gerbang trend yang sama",
        any("Warmup" in w for w in on_pf.peringatan) and 0 < total_on < total_off,
        f"trend_on={total_on} trend_off={total_off}",
    )

    print(
        "HASIL SELFTEST grid_search: "
        + ("SEMUA LULUS" if not gagal else f"{gagal} GAGAL")
    )
    return 0 if not gagal else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
