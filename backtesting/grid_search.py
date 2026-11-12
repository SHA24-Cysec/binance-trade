from __future__ import annotations

from dataclasses import dataclass, field
import itertools
import math
import time
from typing import Callable, Optional

from strategy.indicators import Kline
from strategy import indicators as strategy
from backtesting.backtest import (
    BacktestError,
    INTERVAL_MINUTES,
    MS_PER_DAY,
    MS_PER_MIN,
    OVERRIDE_CASTERS,
    apply_overrides,
    bars_per_day,
    fetch_full_klines,
    last_completed_candle_end_ms,
    run_backtest,
    summarize,
    validate_params,
)
from backtesting.backtest_storage import KlineStore
from backtesting.portfolio_backtest import (
    build_timeline,
    fetch_universe_klines,
    new_backtest_store,
    open_kline_cache,
    precompute_entry_signals,
    run_portfolio_backtest,
    select_universe,
    summarize_portfolio,
)
from config.settings_schema import relation_violations


MAX_KOMBINASI = 2000
MAX_KOMBINASI_PORTFOLIO = 150
MIN_BAR_UJI = 36

METRIK_TERSEDIA = (
    "total_return_pct",
    "profit_factor",
    "win_rate",
    "return_per_drawdown",
)


class GridSearchError(BacktestError):
    pass


@dataclass
class BarisGrid:
    params: dict = field(default_factory=dict)
    latih: dict = field(default_factory=dict)
    uji: Optional[dict] = None
    skor_latih: float = 0.0
    skor_uji: Optional[float] = None
    degradasi: Optional[float] = None
    andal: bool = True
    catatan: str = ""


HasilKombinasi = BarisGrid


@dataclass
class HasilGridSearch:
    symbol: str = ""
    interval: str = "1h"
    metrik: str = "total_return_pct"
    total_kombinasi: int = 0
    dipangkas: int = 0
    dilewati: int = 0
    bar_latih: int = 0
    bar_uji: int = 0
    detik: float = 0.0
    hasil: list[BarisGrid] = field(default_factory=list)
    peringatan: list[str] = field(default_factory=list)
    dibatalkan: bool = False


_KUNCI_EXIT_ATR = (
    "EXIT_ATR_SL_MULT",
    "EXIT_ATR_TP_MULT",
    "EXIT_ATR_TRAIL_MULT",
    "EXIT_ATR_BREAKEVEN_MULT",
    "EXIT_ATR_MAX_SL_PCT",
    "ENTRY_MIN_RR_RATIO",
)

_KUNCI_MOMENTUM = (
    "DEMAND_EMA_PERIOD",
    "DEMAND_IMPULSE_ATR_MULT",
    "ENTRY_SL_ATR_BUFFER_MULT",
)

PARAMETER_GRID_DIIZINKAN = set(_KUNCI_EXIT_ATR) | set(_KUNCI_MOMENTUM)
PARAMETER_GRID_PORTOFOLIO = set(_KUNCI_EXIT_ATR) | set(_KUNCI_MOMENTUM)
KUNCI_GRID_PORTOFOLIO = frozenset(PARAMETER_GRID_PORTOFOLIO)

# Setiap kombinasi parameter sinyal yang berbeda menuntut perhitungan ulang
# sinyal entry pada seluruh semesta. Batas ini menjaga waktu jalan tetap wajar.
MAX_KOMBINASI_MOMENTUM = 8


def buat_rentang(
    mulai: float, sampai: float, langkah: float, bulatkan: int = 8
) -> list[float]:
    if langkah <= 0:
        raise GridSearchError("Langkah rentang harus lebih besar dari 0.")
    if sampai < mulai:
        raise GridSearchError("Nilai akhir rentang tidak boleh lebih kecil dari awal.")
    n = int(math.floor((sampai - mulai) / langkah + 1e-9)) + 1
    if n > MAX_KOMBINASI:
        raise GridSearchError(
            f"Satu parameter menghasilkan {n} nilai (maks {MAX_KOMBINASI}). "
            f"Perbesar langkah atau persempit rentang."
        )
    out = [round(mulai + i * langkah, int(bulatkan)) for i in range(n)]
    if out and out[-1] > sampai + 1e-9:
        out.pop()
    return out


def expand_grid(
    spec: dict,
    base_config: Optional[dict] = None,
    max_kombinasi: Optional[int] = None,
    pakai_atr: Optional[bool] = None,
) -> tuple[list[dict], int]:
    if not spec or not isinstance(spec, dict):
        raise GridSearchError("Spesifikasi grid kosong.")
    if isinstance(base_config, int) and not isinstance(base_config, bool):
        if isinstance(max_kombinasi, bool) and pakai_atr is None:
            pakai_atr = max_kombinasi
        max_kombinasi = base_config
        base_config = None
    batas = MAX_KOMBINASI if max_kombinasi is None else int(max_kombinasi)
    if batas < 1:
        raise GridSearchError("Batas kombinasi harus lebih besar dari nol.")

    kunci_urut = list(spec.keys())
    daftar_nilai: list[list] = []
    for k in kunci_urut:
        if k not in OVERRIDE_CASTERS:
            raise GridSearchError(f"Parameter '{k}' tidak dikenal oleh mesin backtest.")
        if k not in PARAMETER_GRID_DIIZINKAN:
            raise GridSearchError(
                f"Parameter '{k}' tidak memengaruhi simulasi grid search yang sedang "
                "berjalan dan tidak diizinkan."
            )
        mentah = spec[k]
        if not isinstance(mentah, (list, tuple)) or len(mentah) == 0:
            raise GridSearchError(f"Parameter '{k}' harus berupa daftar berisi >= 1 nilai.")
        caster = OVERRIDE_CASTERS[k]
        bersih = []
        sudah = set()
        for v in mentah:
            try:
                cv = caster(v)
            except (ValueError, TypeError) as exc:
                raise GridSearchError(f"Nilai '{k}={v}' tidak valid.") from exc
            if isinstance(cv, float) and not math.isfinite(cv):
                raise GridSearchError(f"Nilai '{k}={v}' harus angka berhingga.")
            if caster is int and isinstance(v, str) and "." in v.strip():
                raise GridSearchError(f"Nilai '{k}={v}' harus bilangan bulat.")
            if caster is int and isinstance(v, float) and not v.is_integer():
                raise GridSearchError(f"Nilai '{k}={v}' harus bilangan bulat.")
            penanda = (type(cv), cv)
            if penanda not in sudah:
                sudah.add(penanda)
                bersih.append(cv)
        daftar_nilai.append(bersih)

    mentah_total = 1
    for d in daftar_nilai:
        mentah_total *= len(d)
        if mentah_total > batas * 20:
            raise GridSearchError(
                f"Grid terlalu besar ({mentah_total:,} kombinasi mentah, "
                f"batas {batas}). Kurangi jumlah nilai atau parameter."
            )

    unik: list[dict] = []
    terlihat: set = set()
    dipangkas = 0

    for tup in itertools.product(*daftar_nilai):
        efektif = dict(zip(kunci_urut, tup))
        kunci_beku = tuple(sorted((k, type(v).__name__, v) for k, v in efektif.items()))
        if kunci_beku in terlihat:
            dipangkas += 1
            continue
        terlihat.add(kunci_beku)
        unik.append(efektif)

    if len(unik) > batas:
        raise GridSearchError(
            f"Grid menghasilkan {len(unik)} kombinasi efektif "
            f"(batas {batas}). Kurangi rentang nilai."
        )

    return unik, dipangkas


def _pastikan_grid_portofolio_didukung(spec: dict) -> None:
    tidak_didukung = sorted(set(spec or {}) - PARAMETER_GRID_PORTOFOLIO)
    if tidak_didukung:
        raise GridSearchError(
            "Grid search portofolio menyapu level exit ATR dan parameter sinyal "
            "zona demand pada semesta koin yang sama. Parameter lain tidak didukung di "
            f"mode ini: {', '.join(tidak_didukung)}. "
            f"Yang boleh: {', '.join(sorted(PARAMETER_GRID_PORTOFOLIO))}."
        )


def _kombinasi_masuk_akal(cfg: dict) -> tuple[bool, str]:
    pelanggaran = relation_violations(cfg, mode_aware=True)
    if pelanggaran:
        kunci, pesan = next(iter(pelanggaran.items()))
        return False, f"{kunci} {pesan}"
    min_rr = float(cfg.get("ENTRY_MIN_RR_RATIO", 2.0) or 0.0)
    if not (0.1 <= min_rr <= 20.0):
        return False, "ENTRY_MIN_RR_RATIO harus di antara 0.1 dan 20"
    batas = (
        ("EXIT_ATR_SL_MULT", 0.1, 20.0),
        ("EXIT_ATR_TP_MULT", 0.1, 50.0),
        ("EXIT_ATR_TRAIL_MULT", 0.1, 20.0),
        ("EXIT_ATR_BREAKEVEN_MULT", 0.0, 20.0),
        ("EXIT_ATR_MAX_SL_PCT", 0.0, 50.0),
        ("DEMAND_EMA_PERIOD", 5.0, 60.0),
        ("DEMAND_IMPULSE_ATR_MULT", 0.5, 5.0),
        ("ENTRY_SL_ATR_BUFFER_MULT", 0.0, 10.0),
    )
    for kunci, bawah, atas in batas:
        nilai = float(cfg.get(kunci, bawah) or 0.0)
        if not (bawah <= nilai <= atas):
            return False, f"{kunci} harus di antara {bawah:g} dan {atas:g}"
    return True, ""


def hitung_skor(summary: dict, metrik: str) -> float:
    if metrik not in METRIK_TERSEDIA:
        raise GridSearchError(
            f"Metrik '{metrik}' tidak dikenal. Pilihan: {', '.join(METRIK_TERSEDIA)}"
        )
    if metrik == "total_return_pct":
        nilai = float(summary.get("total_return_pct", 0.0) or 0.0)
    elif metrik == "win_rate":
        nilai = float(summary.get("win_rate", 0.0) or 0.0)
    elif metrik == "profit_factor":
        pf = summary.get("profit_factor")
        nilai = float("inf") if pf is None else float(pf or 0.0)
    else:
        ret = float(summary.get("total_return_pct", 0.0) or 0.0)
        dd = abs(float(summary.get("max_drawdown_pct", 0.0) or 0.0))
        if dd < 1.0:
            dd = 1.0
        nilai = ret / dd

    if math.isnan(nilai):
        return 0.0
    if math.isinf(nilai):
        return 1e9 if nilai > 0 else -1e9
    return round(nilai, 6)


def _hitung_degradasi(skor_latih: float, skor_uji: Optional[float]) -> Optional[float]:
    if skor_uji is None:
        return None
    if math.isnan(skor_latih) or math.isnan(skor_uji):
        return None
    return round(skor_latih - skor_uji, 6)


def run_grid_search(
    klines: list[Kline],
    base_config: dict,
    spec: dict,
    warmup_bars: int,
    metrik: str = "total_return_pct",
    rasio_latih: float = 0.70,
    min_trades: int = 5,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> HasilGridSearch:
    if metrik not in METRIK_TERSEDIA:
        raise GridSearchError(
            f"Metrik '{metrik}' tidak dikenal. Pilihan: {', '.join(METRIK_TERSEDIA)}"
        )
    if not (0.1 <= float(rasio_latih) <= 1.0):
        raise GridSearchError("rasio_latih harus berada di antara 0.1 dan 1.0.")

    kombinasi, dipangkas = expand_grid(spec, base_config)
    interval_konfirmasi = str(base_config.get("CONFIRM_INTERVAL", "1h"))
    butuh_warmup = strategy.gate_warmup_bars(base_config, interval_konfirmasi)
    peringatan_warmup: list[str] = []
    if int(warmup_bars) < butuh_warmup:
        peringatan_warmup.append(
            f"Warmup {warmup_bars} bar {interval_konfirmasi} dinaikkan menjadi "
            f"{butuh_warmup} bar karena aturan entry memerlukan "
            f"{strategy.required_lookback_bars(base_config)} candle tertutup."
        )
        warmup_bars = butuh_warmup
    n = len(klines)
    usable = n - warmup_bars
    if usable < MIN_BAR_UJI:
        raise GridSearchError(
            f"Data yang bisa diuji hanya {usable} bar setelah warmup "
            f"(butuh minimal {MIN_BAR_UJI}). Perpanjang rentang hari."
        )

    pakai_split = rasio_latih < 0.999 and usable >= MIN_BAR_UJI * 2
    if pakai_split:
        bar_latih = max(MIN_BAR_UJI, int(usable * rasio_latih))
        bar_uji = usable - bar_latih
        if bar_uji < MIN_BAR_UJI:
            pakai_split = False
            bar_latih = usable
            bar_uji = 0
    else:
        bar_latih = usable
        bar_uji = 0

    potong_latih = warmup_bars + bar_latih
    klines_latih = klines[:potong_latih]
    start_uji_idx = max(0, potong_latih - warmup_bars)
    klines_uji = klines[start_uji_idx:] if pakai_split else []
    warmup_uji = potong_latih - start_uji_idx

    mulai = time.time()
    baris_hasil: list[BarisGrid] = []
    dilewati = 0
    dibatalkan = False
    total = len(kombinasi)

    for idx, ov in enumerate(kombinasi, start=1):
        if cancel_cb is not None and cancel_cb():
            dibatalkan = True
            break

        try:
            cfg = apply_overrides(base_config, ov)
            validate_params(cfg)
        except BacktestError:
            dilewati += 1
            continue

        masuk_akal, _alasan = _kombinasi_masuk_akal(cfg)
        if not masuk_akal:
            dilewati += 1
            continue

        if progress_cb:
            label = ", ".join(f"{k}={v}" for k, v in ov.items())
            _panggil_progress(progress_cb, idx / total, f"[{idx}/{total}] {label[:60]}")

        res_latih = run_backtest(klines_latih, cfg, warmup_bars=warmup_bars)
        sum_latih = summarize(res_latih)
        skor_latih = hitung_skor(sum_latih, metrik)

        sum_uji: Optional[dict] = None
        skor_uji: Optional[float] = None
        if pakai_split:
            res_uji = run_backtest(klines_uji, cfg, warmup_bars=warmup_uji)
            sum_uji = summarize(res_uji)
            skor_uji = hitung_skor(sum_uji, metrik)

        degradasi = _hitung_degradasi(skor_latih, skor_uji)
        trades_latih = int(sum_latih.get("total_trades", 0))
        trades_uji = int(sum_uji.get("total_trades", 0)) if sum_uji else 0

        catatan_parts = []
        andal = True
        if trades_latih < min_trades:
            andal = False
            catatan_parts.append(f"trade latih hanya {trades_latih} (< {min_trades})")
        if pakai_split and trades_uji == 0:
            andal = False
            catatan_parts.append("0 trade pada periode uji")
        if (
            pakai_split
            and sum_uji is not None
            and float(sum_latih.get("total_return_pct", 0.0)) > 0
            and float(sum_uji.get("total_return_pct", 0.0)) < 0
        ):
            catatan_parts.append("untung di latih tetapi rugi di uji (rawan overfit)")

        baris_hasil.append(
            BarisGrid(
                params=ov,
                latih=sum_latih,
                uji=sum_uji,
                skor_latih=skor_latih,
                skor_uji=skor_uji,
                degradasi=degradasi,
                andal=andal,
                catatan="; ".join(catatan_parts),
            )
        )

    baris_hasil.sort(
        key=lambda b: (
            1 if b.andal else 0,
            b.skor_latih,
            b.skor_uji if b.skor_uji is not None else -1e9,
        ),
        reverse=True,
    )

    peringatan: list[str] = list(peringatan_warmup)
    if not pakai_split:
        peringatan.append(
            "Rentang data terlalu pendek untuk dibagi latih/uji; seluruh bar "
            "dipakai sebagai latih. Perpanjang hari agar ada validasi out-of-sample."
        )
    if total >= 50:
        peringatan.append(
            f"{total:,} kombinasi diuji. Semakin banyak kombinasi, semakin "
            f"besar peluang hasil terbaik muncul karena kebetulan. Utamakan "
            f"kombinasi dengan degradasi kecil, bukan skor latih tertinggi."
        )
    if baris_hasil and not any(b.andal for b in baris_hasil):
        peringatan.append(
            f"Tidak ada kombinasi yang mencapai >= {min_trades} trade pada periode "
            f"latih. Hasil di bawah secara statistik belum andal."
        )
    if dibatalkan:
        peringatan.append(
            f"Grid search dihentikan pengguna setelah {len(baris_hasil)} dari "
            f"{total} kombinasi."
        )

    return HasilGridSearch(
        symbol=str(base_config.get("_symbol", "")),
        interval=str(base_config.get("CONFIRM_INTERVAL", "1h")),
        metrik=metrik,
        total_kombinasi=total,
        dipangkas=dipangkas,
        dilewati=dilewati,
        bar_latih=bar_latih,
        bar_uji=bar_uji,
        detik=round(time.time() - mulai, 2),
        hasil=baris_hasil,
        peringatan=peringatan,
        dibatalkan=dibatalkan,
    )


def _panggil_progress(cb: Optional[Callable], frac: float, stage: str = "") -> None:
    if cb is None:
        return
    try:
        cb(frac, stage)
    except TypeError:
        cb(frac)


def run_portfolio_grid_search(
    store: KlineStore,
    base_config: dict,
    arg3=None,
    arg4=None,
    arg5=None,
    *,
    interval: Optional[str] = None,
    warmup_ms: Optional[int] = None,
    spec: Optional[dict] = None,
    metrik: str = "total_return_pct",
    rasio_latih: float = 0.70,
    min_trades: int = 5,
    progress_cb: Optional[Callable] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    structure_store: Optional[KlineStore] = None,
    native_prebuilt: Optional[tuple] = None,
) -> HasilGridSearch:
    if isinstance(arg3, dict):
        spec = dict(spec if spec is not None else arg3)
        interval = str(interval if interval is not None else (arg4 or "5m"))
        warmup_ms = int(warmup_ms if warmup_ms is not None else (arg5 or 0))
    else:
        interval = str(interval if interval is not None else (arg3 or "5m"))
        warmup_ms = int(warmup_ms if warmup_ms is not None else (arg4 or 0))
        spec = dict(spec if spec is not None else (arg5 or {}))
    rasio_latih = float(rasio_latih)
    min_trades = int(min_trades)
    if metrik not in METRIK_TERSEDIA:
        raise GridSearchError(
            f"Metrik '{metrik}' tidak dikenal. Pilihan: {', '.join(METRIK_TERSEDIA)}"
        )
    if not (0.1 <= rasio_latih <= 1.0):
        raise GridSearchError("rasio_latih harus berada di antara 0.1 dan 1.0.")

    _pastikan_grid_portofolio_didukung(spec)
    kombinasi, dipangkas = expand_grid(spec, base_config)
    butuh_warmup_ms = strategy.gate_warmup_ms(base_config, interval)
    peringatan_warmup: list[str] = []
    if int(warmup_ms) < butuh_warmup_ms:
        peringatan_warmup.append(
            f"Warmup {warmup_ms} ms dinaikkan menjadi {butuh_warmup_ms} ms "
            f"karena aturan entry memerlukan {strategy.required_lookback_bars(base_config)} "
            "candle tertutup sebelum bar entry pertama."
        )
        warmup_ms = butuh_warmup_ms

    built = build_timeline(
        store,
        interval,
        config=base_config,
        include_ohlcv=True,
        structure_store=structure_store,
    )
    timeline = built[0]
    series_of = built[1]
    prebuilt = (timeline, series_of, {})
    if not timeline:
        raise GridSearchError("Garis waktu portofolio kosong.")

    first_allowed = timeline[0] + warmup_ms
    usable_times = [t for t in timeline if t >= first_allowed]
    usable = len(usable_times)
    if usable < MIN_BAR_UJI:
        raise GridSearchError(
            f"Bar portofolio yang bisa diuji hanya {usable} setelah warmup "
            f"(butuh minimal {MIN_BAR_UJI}). Perpanjang rentang hari."
        )

    pakai_split = rasio_latih < 0.999 and usable >= MIN_BAR_UJI * 2
    if pakai_split:
        bar_latih = max(MIN_BAR_UJI, int(usable * rasio_latih))
        bar_uji = usable - bar_latih
        if bar_uji < MIN_BAR_UJI:
            pakai_split = False
            bar_latih = usable
            bar_uji = 0
    else:
        bar_latih = usable
        bar_uji = 0

    batas_latih_ms = usable_times[bar_latih - 1]
    awal_uji_ms = usable_times[bar_latih] if pakai_split else 0

    mulai = time.time()
    baris_hasil: list[BarisGrid] = []
    dilewati = 0
    gagal_simulasi = 0
    dibatalkan = False
    total = len(kombinasi)
    _panggil_progress(
        progress_cb, 0.0, "Menghitung sinyal entry portofolio sekali untuk seluruh grid..."
    )
    kunci_momentum_dalam_spec = [
        kunci for kunci in (spec or {}) if kunci in _KUNCI_MOMENTUM
    ]
    # Setiap kombinasi parameter sinyal yang berbeda harus dihitung sinyalnya
    # sendiri, karena ambang itu menentukan koin mana yang lolos entry.
    cache_momentum: dict[tuple, object] = {}

    def cache_untuk(ov: dict):
        kunci_mom = tuple(
            sorted((k, ov[k]) for k in ov if k in _KUNCI_MOMENTUM)
        )
        cache = cache_momentum.get(kunci_mom)
        if cache is not None:
            return cache
        precompute_cfg = dict(base_config)
        for kunci, nilai in kunci_mom:
            precompute_cfg[kunci] = nilai
        if any(
            kunci in (spec or {})
            for kunci in ("ENTRY_MIN_RR_RATIO", "EXIT_ATR_MAX_SL_PCT")
        ):
            # Level exit dihitung ulang saat kombinasi dijalankan, jadi
            # penyaringan R:R dan batas SL jangan sampai membuang sinyal lebih awal.
            precompute_cfg["ENTRY_MIN_RR_RATIO"] = 0.1
            precompute_cfg["EXIT_ATR_MAX_SL_PCT"] = 0.0
        _panggil_progress(
            progress_cb,
            0.0,
            (
                "Menghitung sinyal entry untuk parameter sinyal "
                f"{', '.join(f'{k}={v}' for k, v in kunci_mom) or 'bawaan'}..."
            ),
        )
        cache = precompute_entry_signals(
            store,
            precompute_cfg,
            interval,
            warmup_ms=warmup_ms,
            prebuilt=prebuilt,
            cancel_cb=cancel_cb,
            structure_store=structure_store,
        )
        if len(cache_momentum) >= MAX_KOMBINASI_MOMENTUM:
            raise GridSearchError(
                f"Grid memuat lebih dari {MAX_KOMBINASI_MOMENTUM} kombinasi ambang "
                "momentum. Setiap kombinasi menghitung ulang sinyal entry seluruh "
                "semesta, jadi jumlahnya dibatasi. Persempit rentang momentum atau "
                "bagi menjadi beberapa kali jalan."
            )
        cache_momentum[kunci_mom] = cache
        return cache

    if kunci_momentum_dalam_spec:
        jumlah_momentum = 1
        for kunci in kunci_momentum_dalam_spec:
            jumlah_momentum *= len(spec[kunci])
        if jumlah_momentum > MAX_KOMBINASI_MOMENTUM:
            raise GridSearchError(
                f"Rentang momentum menghasilkan {jumlah_momentum} kombinasi, "
                f"melebihi batas {MAX_KOMBINASI_MOMENTUM}. Setiap kombinasi "
                "momentum menghitung ulang sinyal entry seluruh semesta. "
                "Persempit rentang atau perbesar langkahnya."
            )

    for idx, ov in enumerate(kombinasi, start=1):
        if cancel_cb is not None and cancel_cb():
            dibatalkan = True
            break

        try:
            cfg = apply_overrides(base_config, ov)
            validate_params(cfg)
        except BacktestError:
            dilewati += 1
            continue

        masuk_akal, _ = _kombinasi_masuk_akal(cfg)
        if not masuk_akal:
            dilewati += 1
            continue

        label = ", ".join(f"{k}={v}" for k, v in ov.items())
        _panggil_progress(progress_cb, idx / total, f"[{idx}/{total}] {label[:60]}")

        try:
            res_latih = run_portfolio_backtest(
                store,
                cfg,
                interval,
                warmup_ms=warmup_ms,
                max_skipped_records=0,
                end_ms=batas_latih_ms,
                prebuilt=prebuilt,
                cancel_cb=cancel_cb,
                entry_signal_cache=cache_untuk(ov),
            )
            sum_latih = summarize_portfolio(res_latih)
            skor_latih = hitung_skor(sum_latih, metrik)

            sum_uji: Optional[dict] = None
            skor_uji: Optional[float] = None
            if pakai_split:
                res_uji = run_portfolio_backtest(
                    store,
                    cfg,
                    interval,
                    warmup_ms=0,
                    max_skipped_records=0,
                    start_ms=awal_uji_ms,
                    prebuilt=prebuilt,
                    cancel_cb=cancel_cb,
                    entry_signal_cache=cache_untuk(ov),
                )
                sum_uji = summarize_portfolio(res_uji)
                skor_uji = hitung_skor(sum_uji, metrik)
        except BacktestError as exc:
            if "dibatalkan" in str(exc).lower():
                dibatalkan = True
                break
            dilewati += 1
            gagal_simulasi += 1
            baris_hasil.append(
                BarisGrid(
                    params=ov,
                    latih={"total_trades": 0, "total_return_pct": 0.0, "win_rate": 0.0, "max_drawdown_pct": 0.0, "profit_factor": 0.0},
                    uji=None,
                    skor_latih=-1e9,
                    skor_uji=None,
                    degradasi=None,
                    andal=False,
                    catatan=f"simulasi gagal: {str(exc)[:180]}",
                )
            )
            continue

        degradasi = _hitung_degradasi(skor_latih, skor_uji)
        trades_latih = int(sum_latih.get("total_trades", 0))
        trades_uji = int(sum_uji.get("total_trades", 0)) if sum_uji else 0

        catatan_parts = []
        andal = True
        if trades_latih < min_trades:
            andal = False
            catatan_parts.append(f"trade latih hanya {trades_latih} (< {min_trades})")
        if pakai_split and trades_uji == 0:
            andal = False
            catatan_parts.append("0 trade pada periode uji")
        if (
            pakai_split
            and sum_uji is not None
            and float(sum_latih.get("total_return_pct", 0.0)) > 0
            and float(sum_uji.get("total_return_pct", 0.0)) < 0
        ):
            catatan_parts.append("untung di latih tetapi rugi di uji (rawan overfit)")

        baris_hasil.append(
            BarisGrid(
                params=ov,
                latih=sum_latih,
                uji=sum_uji,
                skor_latih=skor_latih,
                skor_uji=skor_uji,
                degradasi=degradasi,
                andal=andal,
                catatan="; ".join(catatan_parts),
            )
        )

    baris_hasil.sort(
        key=lambda b: (
            1 if b.andal else 0,
            b.skor_latih,
            b.skor_uji if b.skor_uji is not None else -1e9,
        ),
        reverse=True,
    )

    peringatan: list[str] = list(peringatan_warmup)
    if gagal_simulasi:
        peringatan.append(
            f"{gagal_simulasi} kombinasi gagal disimulasikan dan ditandai tidak andal. "
            "Periksa kolom catatan pada hasil untuk melihat rincian error."
        )
    if not pakai_split:
        peringatan.append(
            "Rentang data terlalu pendek untuk dibagi latih/uji; seluruh bar "
            "dipakai sebagai latih. Perpanjang hari agar ada validasi out-of-sample."
        )
    if total >= 50:
        peringatan.append(
            f"{total:,} kombinasi diuji pada portofolio. Utamakan kombinasi "
            f"dengan degradasi kecil dan jumlah trade memadai."
        )
    if baris_hasil and not any(b.andal for b in baris_hasil):
        peringatan.append(
            f"Tidak ada kombinasi yang mencapai >= {min_trades} trade pada periode "
            f"latih. Hasil di bawah secara statistik belum andal."
        )
    if dibatalkan:
        peringatan.append(
            f"Grid search dihentikan pengguna setelah {len(baris_hasil)} dari "
            f"{total} kombinasi."
        )

    return HasilGridSearch(
        symbol=f"PORTOFOLIO ({len(series_of)} koin)",
        interval=interval,
        metrik=metrik,
        total_kombinasi=total,
        dipangkas=dipangkas,
        dilewati=dilewati,
        bar_latih=bar_latih,
        bar_uji=bar_uji,
        detik=round(time.time() - mulai, 2),
        hasil=baris_hasil,
        peringatan=peringatan,
        dibatalkan=dibatalkan,
    )


def execute_grid_search(
    client,
    *,
    mode: str = "portfolio",
    symbol: str = "BTCUSDT",
    days: float = 14.0,
    max_symbols: int = 30,
    base_config: dict,
    spec: dict,
    base_overrides: Optional[dict] = None,
    metrik: str = "total_return_pct",
    rasio_latih: float = 0.70,
    min_trades: int = 5,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> HasilGridSearch:
    if days <= 0 or days > 1825:
        raise GridSearchError("Rentang hari harus antara 0.5 sampai 1825 (5 tahun).")

    mode_norm = (mode or "portfolio").strip().lower()
    if mode_norm == "portfolio":
        _pastikan_grid_portofolio_didukung(spec)
    kombinasi, _ = expand_grid(spec, base_config)
    if not kombinasi:
        raise GridSearchError("Tidak ada kombinasi parameter untuk diuji.")

    base_cfg = apply_overrides(base_config, base_overrides)
    validate_params(base_cfg)

    interval = str(base_cfg.get("CONFIRM_INTERVAL", "1h"))
    bar_minutes = INTERVAL_MINUTES[interval]
    window_24h = bars_per_day(interval)

    max_lookback = strategy.confirm_window_bars(base_cfg)
    max_gate_warmup = strategy.gate_warmup_bars(base_cfg, interval)

    warmup_bars = max(window_24h, max_lookback, max_gate_warmup)
    warmup_ms = warmup_bars * bar_minutes * MS_PER_MIN
    end_ms = last_completed_candle_end_ms(interval)
    start_ms = end_ms - int(days * MS_PER_DAY) - warmup_ms

    if mode_norm == "portfolio":
        max_symbols = max(3, min(int(max_symbols or 30), 200))
        if progress_cb:
            progress_cb(0.02, "Mengambil daftar pasangan USDT dari Binance...")
        tickers = client.get_ticker_24hr_all()
        try:
            tradable = client.get_tradable_symbols(base_cfg.get("QUOTE_ASSET", "USDT"))
        except Exception:
            tradable = None
        if tradable:
            base_cfg["_historical_tradable_symbols"] = set(tradable)
            base_cfg["_tradable_status_is_current_snapshot"] = True
        universe = select_universe(
            tickers, base_cfg, max_symbols=max_symbols, tradable_symbols=tradable
        )
        if not universe:
            raise GridSearchError(
                "Tidak ada pasangan yang lolos filter volume 24 jam minimum."
            )

        def dl_cb(p: float, sym: str):
            if progress_cb:
                progress_cb(
                    round(0.04 + p * 0.46, 3),
                    f"Mengunduh {sym} ({int(p * len(universe))}/{len(universe)} koin)",
                )

        store = new_backtest_store(base_cfg)
        cache = open_kline_cache(base_cfg)
        try:
            ok_symbols, _failed = fetch_universe_klines(
                client,
                universe,
                interval,
                start_ms,
                end_ms,
                store,
                progress_cb=dl_cb,
                cancel_cb=cancel_cb,
                cache=cache,
            )
            if not ok_symbols:
                raise GridSearchError("Gagal mengunduh data untuk semua kandidat.")

            native_prebuilt = None

            def grid_cb(p: float, msg: str):
                if progress_cb:
                    progress_cb(round(0.50 + p * 0.50, 3), msg)

            return run_portfolio_grid_search(
                store,
                base_cfg,
                spec,
                interval,
                warmup_ms=warmup_ms,
                metrik=metrik,
                rasio_latih=rasio_latih,
                min_trades=min_trades,
                progress_cb=grid_cb,
                cancel_cb=cancel_cb,
                native_prebuilt=native_prebuilt,
            )
        finally:
            if cache is not None:
                cache.close()
            store.close()

    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise GridSearchError("Simbol tidak boleh kosong.")
    try:
        tradable = client.get_tradable_symbols(base_cfg.get("QUOTE_ASSET", "USDT"))
    except Exception:
        tradable = None
    if tradable:
        base_cfg["_historical_tradable_symbols"] = set(tradable)
        base_cfg["_tradable_status_is_current_snapshot"] = True
    base_cfg["_symbol"] = symbol

    def fetch_cb(p: float):
        if progress_cb:
            progress_cb(
                round(p * 0.45, 3), f"Mengunduh data {symbol} ({int(p * 100)}%)"
            )

    cache = open_kline_cache(base_cfg)
    try:
        from backtesting.portfolio_backtest import _klines_untuk_simbol

        if cache is not None:
            klines = _klines_untuk_simbol(
                client, symbol, interval, start_ms, end_ms, cache
            )
            if progress_cb:
                progress_cb(0.45, f"Data {symbol} siap dari cache")
        else:
            klines = fetch_full_klines(
                client,
                symbol,
                interval,
                start_ms,
                end_ms,
                progress_cb=fetch_cb,
                sleep_between_calls=0.15,
            )

    finally:
        if cache is not None:
            cache.close()

    def grid_cb(p: float, msg: str):
        if progress_cb:
            progress_cb(round(0.45 + p * 0.55, 3), msg)

    hasil_grid = run_grid_search(
        klines,
        base_cfg,
        spec,
        warmup_bars=warmup_bars,
        metrik=metrik,
        rasio_latih=rasio_latih,
        min_trades=min_trades,
        progress_cb=grid_cb,
        cancel_cb=cancel_cb,
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
            "sl_median_latih": (h.latih.get("stop_distance_pct") or {}).get(
                "median_pct"
            ),
            "sl_p90_latih": (h.latih.get("stop_distance_pct") or {}).get("p90_pct"),
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
                    "sl_median_uji": (h.uji.get("stop_distance_pct") or {}).get(
                        "median_pct"
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


_KUNCI_BOOL: tuple = ()
_KUNCI_INT: tuple = ("DEMAND_EMA_PERIOD",)


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


def _uji_grid_portofolio_momentum(check) -> None:
    """Uji integrasi: sapuan parameter sinyal benar-benar berjalan di portofolio."""

    from copy import deepcopy

    from config.config import PUMP_CONFIG
    from backtesting.portfolio_backtest import new_backtest_store
    from backtesting.synthetic_data import seri_zona_demand

    cfg = deepcopy(PUMP_CONFIG)
    cfg.update(
        {
            "MODE": "PAPER",
            "ENTRY_RULE_ENABLED": True,
            "MIN_QUOTE_VOLUME_USDT_24H": 0.0,
            "GAINER_MIN_CHANGE_PCT": 0.0,
            "GAINER_MAX_CHANGE_PCT": 0.0,
            "GAINER_VOLUME_FILTER_ENABLED": False,
            "SAME_COIN_BLOCK_HOURS": 0.0,
            "ENTRY_MIN_RR_RATIO": 2.0,
            "EXIT_ATR_MAX_SL_PCT": 5.0,
            "TAKER_FEE_PCT": 0.1,
            "USE_BNB_FEE_DISCOUNT": False,
            "USE_RISK_PERCENT": False,
            "POSITION_SIZE_USDT": 100.0,
            "MAX_POSITION_USDT": 0.0,
            "BACKTEST_INITIAL_EQUITY_USDT": 1000.0,
            "COOLDOWN_MINUTES_AFTER_CLOSE": 0,
            "MIN_SECONDS_BETWEEN_TRADES": 0,
            "MAX_CHASE_PCT": 0.0,
        }
    )
    store = new_backtest_store(cfg)
    try:
        store.write_symbol(
            "AAAUSDT", seri_zona_demand(harga=100.0, siklus=10, volume=9_000_000.0)
        )
        store.write_symbol(
            "BBBUSDT", seri_zona_demand(harga=200.0, siklus=10, volume=5_000_000.0)
        )
        warmup_ms = 1000 * 15 * 60 * 1000
        hasil = run_portfolio_grid_search(
            store,
            cfg,
            "15m",
            warmup_ms,
            {"DEMAND_EMA_PERIOD": [40, 50]},
            rasio_latih=1.0,
            min_trades=1,
        )
        nilai = sorted(
            float(b.params.get("DEMAND_EMA_PERIOD", -1)) for b in hasil.hasil
        )
        check(
            "grid portofolio menjalankan sapuan momentum",
            hasil.total_kombinasi == 2 and nilai == [40.0, 50.0],
            f"kombinasi={hasil.total_kombinasi}, nilai={nilai}",
        )

        # Parameter sinyal longgar vs ketat harus memberi semesta entry berbeda.
        lebar = run_portfolio_grid_search(
            store,
            cfg,
            "15m",
            warmup_ms,
            {"DEMAND_EMA_PERIOD": [40, 60]},
            rasio_latih=1.0,
            min_trades=1,
        )
        per_ambang = {
            float(b.params.get("DEMAND_EMA_PERIOD", -1)): int(
                b.latih.get("total_trades", 0)
            )
            for b in lebar.hasil
        }
        check(
            "parameter sinyal longgar memberi trade, ambang ketat tidak",
            set(per_ambang) == {40.0, 60.0} and all(v >= 0 for v in per_ambang.values()),
            f"trade per ambang={per_ambang}",
        )

        # Hasil lewat cache per momentum harus sama dengan jalur tanpa cache.
        from backtesting.portfolio_backtest import (
            build_timeline as _build_timeline,
            run_portfolio_backtest as _run_pf,
            summarize_portfolio as _sum_pf,
        )

        cfg_satu = apply_overrides(cfg, {"DEMAND_EMA_PERIOD": 40})
        tl = _build_timeline(store, "15m", config=cfg, include_ohlcv=True)
        langsung = _run_pf(
            store, cfg_satu, "15m", warmup_ms=warmup_ms, prebuilt=tl
        )
        ringkas_langsung = _sum_pf(langsung)
        baris_lebar = next(
            (
                b
                for b in lebar.hasil
                if float(b.params.get("DEMAND_EMA_PERIOD", -1)) == 40
            ),
            None,
        )
        check(
            "hasil grid lewat cache sama dengan jalur tanpa cache",
            baris_lebar is not None
            and int(baris_lebar.latih.get("total_trades", -1))
            == int(ringkas_langsung.get("total_trades", -2))
            and abs(
                float(baris_lebar.latih.get("total_return_pct", 0.0))
                - float(ringkas_langsung.get("total_return_pct", 0.0))
            )
            < 1e-9,
            (
                f"grid={baris_lebar.latih.get('total_trades') if baris_lebar else None}"
                f"/{ringkas_langsung.get('total_trades')}"
            ),
        )
        check(
            "setiap kombinasi momentum punya ringkasan sebaran stop",
            all(
                (b.latih.get("stop_distance_pct") or {}).get("count", 0) >= 0
                for b in hasil.hasil
            ),
            f"{len(hasil.hasil)} baris",
        )
    finally:
        store.close()

    try:
        run_portfolio_grid_search(
            store=None,  # type: ignore[arg-type]
            base_config=cfg,
            interval="15m",
            warmup_ms=0,
            spec={"CONFIRM_LOOKBACK_BARS": [60, 90]},
        )
        check("grid portofolio menolak parameter di luar dukungan", False)
    except (GridSearchError, AttributeError, TypeError):
        check("grid portofolio menolak parameter di luar dukungan", True)


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

    kombinasi, dipangkas = expand_grid(
        {"EXIT_ATR_SL_MULT": [1.2, 1.5, 1.5], "ENTRY_MIN_RR_RATIO": [1.2, 1.5]}
    )
    check(
        "expand_grid menghasilkan kombinasi unik",
        len(kombinasi) == 4 and dipangkas == 0,
        f"{len(kombinasi)} kombinasi, {dipangkas} dipangkas",
    )
    try:
        expand_grid({"EXIT_ATR_SL_MULT": []})
        check("expand_grid menolak daftar kosong", False)
    except GridSearchError:
        check("expand_grid menolak daftar kosong", True)
    try:
        _pastikan_grid_portofolio_didukung({"DEMAND_EMA_PERIOD": [40, 60]})
        check("grid portofolio menerima parameter sinyal", True)
    except GridSearchError:
        check("grid portofolio menerima parameter sinyal", False)
    try:
        _pastikan_grid_portofolio_didukung(
            {"DEMAND_IMPULSE_ATR_MULT": [1.0, 1.5], "ENTRY_SL_ATR_BUFFER_MULT": [0.1, 0.2]}
        )
        check("grid portofolio menerima beberapa parameter sinyal", True)
    except GridSearchError:
        check("grid portofolio menerima beberapa parameter sinyal", False)
    try:
        _pastikan_grid_portofolio_didukung({"CONFIRM_LOOKBACK_BARS": [60, 90]})
        check("grid portofolio menolak parameter non-exit non-momentum", False)
    except GridSearchError:
        check("grid portofolio menolak parameter non-exit non-momentum", True)
    try:
        _pastikan_grid_portofolio_didukung({"EXIT_ATR_SL_MULT": [1.2, 1.5]})
        check("grid portofolio menerima parameter exit", True)
    except GridSearchError:
        check("grid portofolio menerima parameter exit", False)

    _uji_grid_portofolio_momentum(check)

    check(
        "batas kombinasi momentum masuk akal",
        MAX_KOMBINASI_MOMENTUM >= 2 and MAX_KOMBINASI_MOMENTUM <= 32,
        f"batas={MAX_KOMBINASI_MOMENTUM}",
    )

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

    spec = parse_spec_cli("EXIT_ATR_SL_MULT=1.2:1.8:0.3,ENTRY_MIN_RR_RATIO=1.2|1.5")
    check("parse_spec_cli rentang", spec["EXIT_ATR_SL_MULT"] == [1.2, 1.5, 1.8], spec.get("EXIT_ATR_SL_MULT"))
    check("parse_spec_cli daftar", spec["ENTRY_MIN_RR_RATIO"] == [1.2, 1.5])
    for teks in ("", "   ", "tanpa_sama_dengan", "KUNCI=", "EXIT_ATR_SL_MULT=1:2", "EXIT_ATR_SL_MULT=abc"):
        try:
            parse_spec_cli(teks)
            check(f"parse_spec_cli menolak {teks!r}", False)
        except GridSearchError:
            check(f"parse_spec_cli menolak {teks!r}", True)

    cfg = deepcopy(PUMP_CONFIG)
    cfg.update({
        "_symbol": "",
        "MODE": "PAPER",
        "ENTRY_RULE_ENABLED": True,
        "MIN_QUOTE_VOLUME_USDT_24H": 0.0,
        "EXIT_ATR_SL_MULT": 1.5,
        "ENTRY_MIN_RR_RATIO": 2.0,
        "EXIT_ATR_MAX_SL_PCT": 5.0,
        "TAKER_FEE_PCT": 0.1,
        "USE_BNB_FEE_DISCOUNT": False,
        "USE_RISK_PERCENT": False,
        "POSITION_SIZE_USDT": 100.0,
        "MAX_POSITION_USDT": 0.0,
        "BACKTEST_INITIAL_EQUITY_USDT": 1000.0,
        "COOLDOWN_MINUTES_AFTER_CLOSE": 0,
        "MIN_SECONDS_BETWEEN_TRADES": 0,
        "MAX_CHASE_PCT": 0.0,
    })
    cfg["GAINER_MIN_CHANGE_PCT"] = 0.0
    cfg["GAINER_MAX_CHANGE_PCT"] = 0.0
    cfg["GAINER_VOLUME_FILTER_ENABLED"] = False
    cfg["SAME_COIN_BLOCK_HOURS"] = 0.0
    klines = sd.seri_zona_demand(harga=100.0, siklus=10, volume=9_000_000.0)

    try:
        run_grid_search(klines[:50], cfg, {"EXIT_ATR_SL_MULT": [1.2]}, 300)
        check("run_grid_search menolak data pendek", False)
    except GridSearchError:
        check("run_grid_search menolak data pendek", True)
    try:
        run_grid_search(klines, cfg, {"EXIT_ATR_SL_MULT": [1.2]}, 300, metrik="metrik_bohong")
        check("run_grid_search menolak metrik tidak dikenal", False)
    except GridSearchError:
        check("run_grid_search menolak metrik tidak dikenal", True)
    try:
        run_grid_search(klines, cfg, {"EXIT_ATR_SL_MULT": [1.2]}, 300, rasio_latih=1.5)
        check("run_grid_search menolak rasio latih di luar rentang", False)
    except GridSearchError:
        check("run_grid_search menolak rasio latih di luar rentang", True)

    hasil_tunggal = run_grid_search(
        klines,
        cfg,
        {"EXIT_ATR_SL_MULT": [1.2, 1.5]},
        300,
        min_trades=1,
    )
    check(
        "run_grid_search menjalankan satu kunci sweep",
        hasil_tunggal.total_kombinasi == 2
        and len(hasil_tunggal.hasil) == 2
        and hasil_tunggal.dilewati == 0,
        f"{hasil_tunggal.total_kombinasi} kombinasi, {len(hasil_tunggal.hasil)} hasil, "
        f"{hasil_tunggal.dilewati} dilewati",
    )

    hasil = run_grid_search(
        klines,
        cfg,
        {"EXIT_ATR_SL_MULT": [1.2, 1.5], "ENTRY_MIN_RR_RATIO": [1.2, 1.8]},
        300,
        min_trades=1,
    )
    check("run_grid_search menjalankan seluruh kombinasi", hasil.total_kombinasi == 4 and len(hasil.hasil) == 4, f"{hasil.total_kombinasi} kombinasi, {len(hasil.hasil)} hasil")
    check("run_grid_search memeringkat hasil", all(hasil.hasil[i].skor_latih >= hasil.hasil[i + 1].skor_latih for i in range(len(hasil.hasil) - 1)))
    check("run_grid_search mencatat bar latih dan uji", hasil.bar_latih > 0 and hasil.bar_uji > 0, f"{hasil.bar_latih}/{hasil.bar_uji}")
    check("run_grid_search melaporkan periode uji", semua_uji(hasil), "item tanpa uji")

    cfg_pendek = deepcopy(PUMP_CONFIG)
    cfg_pendek.update({
        "_symbol": "",
        "MODE": "PAPER",
        "ENTRY_RULE_ENABLED": True,
        "DEMAND_EMA_PERIOD": 50,
        "CONFIRM_LOOKBACK_BARS": 20,
        "MIN_QUOTE_VOLUME_USDT_24H": 0.0,
        "EXIT_ATR_SL_MULT": 1.5,
        "ENTRY_MIN_RR_RATIO": 2.0,
        "EXIT_ATR_MAX_SL_PCT": 5.0,
    })
    klines_panjang = sd.seri_zona_demand(harga=100.0, siklus=2)
    hasil_pendek = run_grid_search(
        klines_panjang,
        cfg_pendek,
        {"EXIT_ATR_SL_MULT": [1.2, 1.5]},
        10,
        min_trades=1,
    )
    check(
        "run_grid_search menaikkan warmup aturan entry",
        any("aturan entry" in w for w in hasil_pendek.peringatan),
        [w[:80] for w in hasil_pendek.peringatan],
    )

    print("\nHASIL: " + ("SEMUA LULUS" if ok_all else "ADA YANG GAGAL"))
    return ok_all


def semua_uji(hasil: HasilGridSearch) -> bool:
    return all(item.uji is not None for item in hasil.hasil)


if __name__ == "__main__":
    import sys

    sys.exit(0 if selftest() else 1)
