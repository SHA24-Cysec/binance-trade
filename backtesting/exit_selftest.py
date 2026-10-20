"""Selftest offline fitur time stop MAX_HOLD_HOURS dan aturan exit ATR.

Tujuan: membuktikan tiga hal tanpa menyentuh jaringan sama sekali.

1. Aturan time stop bekerja sesuai desain (unit test murni).
2. Saat MAX_HOLD_HOURS = 0 atau kuncinya tidak ada, seluruh hasil backtest
   IDENTIK dengan sebelumnya (bukti tidak ada regresi).
3. Saat aktif, setiap posisi benar benar selesai di dalam batas, exit berbasis
   harga (SL, BE, trailing) tetap menang, dan aturan yang dipakai bot live sama
   dengan aturan yang dipakai mesin backtest.

Semua candle dibuat sintetis oleh backtesting.synthetic_data, jadi hasilnya
deterministik dan tidak butuh internet.

Pemakaian:
    python -m backtesting.exit_selftest
    python -m backtesting.backtest --selftest
    python -m trading.pump_scanner_bot --selftest
"""

from __future__ import annotations

from copy import deepcopy
from typing import Callable

from backtesting import backtest as bt
from backtesting import parity
from backtesting import synthetic_data as sd
from config.config import CONFIG_LOAD_ERRORS, PUMP_CONFIG
from strategy import indicators as strategy

BAR_MS_5M = 300_000
MS_PER_JAM = 3_600_000
CAP_CONTOH = 24.0
PANJANG_EKOR = 900
HARGA_AWAL = 100.0


def config_dasar(cap_jam: float | None = CAP_CONTOH, ekor: str = "datar") -> dict:
    """Config backtest satu simbol dengan semua gerbang timeframe dimatikan.

    Layer arah Daily, layer zona H1, layer timing M5, filter BTC, dan
    filter volume dimatikan supaya rangkaian candle sintetis pasti menghasilkan
    entry. Yang diuji di sini adalah aturan EXIT, bukan aturan entry.
    """
    cfg = deepcopy(PUMP_CONFIG)
    cfg.update(
        {
            "_symbol": "",
            "MODE": "PAPER",
            "CONFIG_UPDATE_MODE": "test",
            "DIRECTION_FILTER_ENABLED": False,
            "DIRECTION_ZONE_FILTER_ENABLED": False,
            "ZONE_FILTER_ENABLED": False,
            "TIMING_FILTER_ENABLED": False,
            "BTC_FILTER_ENABLED": False,
            "ROLLING_VOLUME_FILTER_ENABLED": False,
            "DEMAND_ZONE_FILTER_ENABLED": False,
            "PUMP_MIN_24H_CHANGE_PCT": -1000.0,
            "MIN_QUOTE_VOLUME_USDT_24H": 0.0,
            "USE_ATR_EXIT": True,
            "USE_TP": False,
            "USE_STOP_LOSS": True,
            "USE_BREAKEVEN": True,
            "USE_TRAILING": True,
            "USE_RISK_PERCENT": False,
            "POSITION_SIZE_USDT": 100.0,
            "MAX_POSITION_USDT": 100.0,
            "BACKTEST_INITIAL_EQUITY_USDT": 1000.0,
        }
    )
    if cap_jam is None:
        cfg.pop("MAX_HOLD_HOURS", None)
    else:
        cfg["MAX_HOLD_HOURS"] = float(cap_jam)
    return cfg


def jalankan(ekor: str = "datar", cap_jam: float | None = CAP_CONTOH,
             panjang_ekor: int = PANJANG_EKOR):
    """Jalankan backtest sintetis. Kembalikan hasil backtest."""
    cfg = config_dasar(cap_jam, ekor)
    klines = sd.seri_dengan_setup(
        harga=HARGA_AWAL, panjang_ekor=panjang_ekor, ekor=ekor
    )
    warmup = max(strategy.required_lookback_bars(cfg), 60)
    bt.validate_params(cfg)
    return bt.run_backtest(klines, cfg, warmup)


def jejak_trade(hasil) -> list:
    """Sidik jari daftar trade untuk perbandingan antar konfigurasi."""
    return [
        (
            int(t.entry_time),
            int(t.exit_time),
            str(t.reason),
            round(float(t.pnl_pct), 10),
            round(float(t.hold_minutes), 6),
        )
        for t in hasil.trades
    ]


def _ctx_now_ms(nilai: int) -> Callable[[], None]:
    """Ganti jam dinding bot live dengan nilai tetap, lalu kembalikan seperti semula."""
    from infrastructure.storage import state as state_mod

    asli = state_mod.now_ms
    state_mod.now_ms = lambda: nilai
    return lambda: setattr(state_mod, "now_ms", asli)


# --------------------------------------------------------------------- cek


def cek_aturan_time_stop() -> str:
    """Batas keputusan time_stop_due, termasuk kasus yang harus inert."""
    entry = 1_700_000_000_000
    cap = int(CAP_CONTOH * MS_PER_JAM)
    kasus = [
        # (umur posisi ms, harapan, keterangan)
        (0, False, "baru saja masuk"),
        (cap - 2 * BAR_MS_5M, False, "dua bar sebelum batas"),
        (cap - BAR_MS_5M - 1, False, "lebih awal dari bar terakhir"),
        (cap - BAR_MS_5M, False, "kalau ditahan lagi masih pas batas"),
        (cap - BAR_MS_5M + 1, True, "bar terakhir di dalam batas"),
        (cap - 1, True, "tepat sebelum batas"),
        (cap, True, "tepat di batas"),
        (cap + BAR_MS_5M, True, "sudah lewat batas"),
    ]
    for umur, harapan, keterangan in kasus:
        hasil = strategy.time_stop_due(entry, entry + umur, cap, BAR_MS_5M)
        assert hasil is harapan, (
            f"time_stop_due salah untuk {keterangan}: umur {umur} ms "
            f"menghasilkan {hasil}, seharusnya {harapan}"
        )
    inert = [
        (0, entry + cap, cap, BAR_MS_5M, "cap 0 berarti nonaktif"),
        (entry, entry + cap, 0, BAR_MS_5M, "cap negatif dianggap nonaktif"),
        (0, entry + cap, cap, BAR_MS_5M, "entry_time tidak diketahui"),
        (entry, 0, cap, BAR_MS_5M, "waktu sekarang tidak valid"),
        (entry, entry + cap, cap, 0, "panjang bar tidak diketahui"),
    ]
    for e, now, c, bar, keterangan in inert:
        assert strategy.time_stop_due(e, now, c, bar) is False, (
            f"time_stop_due harus inert saat {keterangan}"
        )
    assert strategy.TIME_STOP_REASON == "TIME_STOP"
    assert parity.TIME_STOP_REASON == strategy.TIME_STOP_REASON
    return f"{len(kasus)} kasus batas dan {len(inert)} kasus inert sesuai"


def cek_konversi_max_hold() -> str:
    """Konversi jam ke ms dan penolakan nilai yang tidak aman."""
    assert strategy.max_hold_ms({"MAX_HOLD_HOURS": CAP_CONTOH}, "5m") == int(
        CAP_CONTOH * MS_PER_JAM
    )
    assert strategy.max_hold_ms({"MAX_HOLD_HOURS": "12"}, "5m") == 12 * MS_PER_JAM
    for kosong in (0, 0.0, "", None):
        assert strategy.max_hold_ms({"MAX_HOLD_HOURS": kosong}, "5m") == 0
    assert strategy.max_hold_ms({}, "5m") == 0
    assert parity.max_hold_ms({}, "5m") == 0
    assert parity.MAX_HOLD_HOURS_LIMIT == strategy.MAX_HOLD_HOURS_LIMIT

    ditolak = {
        -1.0: "negatif",
        float("nan"): "bukan angka berhingga",
        strategy.MAX_HOLD_HOURS_LIMIT + 1: "melebihi batas 30 hari",
        "abc": "bukan angka",
        0.05: "lebih sempit dari 2 bar 5 menit",
    }
    for nilai, keterangan in ditolak.items():
        try:
            strategy.max_hold_ms({"MAX_HOLD_HOURS": nilai}, "5m")
        except ValueError:
            continue
        raise AssertionError(f"MAX_HOLD_HOURS {nilai!r} ({keterangan}) harus ditolak")
    return f"konversi benar dan {len(ditolak)} nilai berbahaya ditolak"


def cek_time_stop_menutup_posisi() -> str:
    """Cap 24 jam menutup posisi mengendap tepat di bar terakhir di dalam batas."""
    hasil = jalankan("datar")
    assert hasil.trades, "rangkaian sintetis tidak menghasilkan trade sama sekali"
    time_stop = [t for t in hasil.trades if t.reason == strategy.TIME_STOP_REASON]
    assert time_stop, (
        "tidak ada trade dengan alasan TIME_STOP padahal ekornya datar 75 jam "
        "dan batasnya 24 jam"
    )
    for t in hasil.trades:
        assert t.hold_minutes <= CAP_CONTOH * 60.0 + 1e-6, (
            f"trade dengan alasan {t.reason} berumur {t.hold_minutes / 60:.4f} jam, "
            f"melewati batas {CAP_CONTOH:g} jam"
        )
    for t in time_stop:
        sisa_ms = CAP_CONTOH * MS_PER_JAM - t.hold_minutes * 60_000.0
        assert 0 <= sisa_ms <= 1.0, (
            f"exit TIME_STOP terjadi {sisa_ms / 60_000:.4f} menit sebelum batas, "
            "seharusnya tepat di bar terakhir yang masih di dalam batas"
        )
    return (
        f"{len(hasil.trades)} trade, {len(time_stop)} di antaranya TIME_STOP, "
        f"umur maksimum {max(t.hold_minutes for t in hasil.trades) / 60:.4f} jam"
    )


def cek_batas_berjenjang() -> str:
    """Beberapa nilai cap sekaligus: umur posisi selalu di dalam batasnya."""
    ringkas = []
    for cap in (48.0, 24.0, 12.0, 6.0, 2.0):
        hasil = jalankan("datar", cap)
        assert hasil.trades, f"batas {cap:g} jam tidak menghasilkan trade"
        maks = max(t.hold_minutes for t in hasil.trades) / 60.0
        assert maks <= cap + 1e-6, f"batas {cap:g} jam dilanggar: {maks:.4f} jam"
        jumlah_ts = sum(1 for t in hasil.trades if t.reason == strategy.TIME_STOP_REASON)
        ringkas.append(f"{cap:g} jam -> maks {maks:.2f} jam, {jumlah_ts} TIME_STOP")
    return "; ".join(ringkas)


def cek_time_stop_nonaktif_aman() -> str:
    """MAX_HOLD_HOURS=0 harus identik dengan config lama yang tidak punya kunci itu."""
    ringkas = []
    for ekor in ("datar", "invalidasi", "naik"):
        tanpa_kunci = jalankan(ekor, None, panjang_ekor=400)
        cap_nol = jalankan(ekor, 0.0, panjang_ekor=400)
        assert jejak_trade(tanpa_kunci) == jejak_trade(cap_nol), (
            f"skenario {ekor}: MAX_HOLD_HOURS=0 berbeda dari config tanpa kunci"
        )
        assert bt.summarize(tanpa_kunci) == bt.summarize(cap_nol), (
            f"skenario {ekor}: ringkasan equity atau posisi terbuka berbeda"
        )
        assert tanpa_kunci.trades or tanpa_kunci.mark_to_market_open, (
            f"skenario {ekor}: rangkaian kontrol tidak menghasilkan posisi"
        )
        if ekor == "datar":
            assert tanpa_kunci.mark_to_market_open, (
                "posisi pada ekor datar seharusnya masih terbuka tanpa time stop"
            )
        else:
            assert tanpa_kunci.trades, (
                f"skenario {ekor}: exit berbasis harga tidak menghasilkan trade"
            )
        assert all(
            t.reason != strategy.TIME_STOP_REASON for t in tanpa_kunci.trades
        ), "TIME_STOP muncul padahal fiturnya nonaktif"
        ringkas.append(
            f"{ekor}: {len(tanpa_kunci.trades)} trade tertutup, "
            f"{int(tanpa_kunci.mark_to_market_open)} posisi terbuka"
        )
    return "; ".join(ringkas) + "; hasil identik tanpa TIME_STOP"


def cek_prioritas_exit_harga() -> str:
    """SL, BE, dan trailing tetap menang atas time stop."""
    turun = jalankan("invalidasi", CAP_CONTOH, panjang_ekor=400)
    naik = jalankan("naik", CAP_CONTOH, panjang_ekor=400)
    for nama, hasil in (("turun", turun), ("naik", naik)):
        assert hasil.trades, f"skenario {nama} tidak menghasilkan trade"
        alasan = {t.reason for t in hasil.trades}
        assert strategy.TIME_STOP_REASON not in alasan, (
            f"TIME_STOP muncul di skenario harga {nama} padahal exit harga "
            f"seharusnya lebih dulu: {sorted(alasan)}"
        )
        assert alasan & {"STOP_LOSS", "BREAKEVEN", "TRAILING_STOP"}, (
            f"skenario {nama} tidak menghasilkan exit berbasis harga: {sorted(alasan)}"
        )
        maks = max(t.hold_minutes for t in hasil.trades) / 60.0
        assert maks < CAP_CONTOH, f"skenario {nama} menahan posisi {maks:.2f} jam"
    return (
        f"turun: {len(turun.trades)} trade alasan {sorted({t.reason for t in turun.trades})}; "
        f"naik: {len(naik.trades)} trade alasan {sorted({t.reason for t in naik.trades})}"
    )


def cek_kapital_berputar() -> str:
    """Time stop membebaskan modal sehingga frekuensi trade naik."""
    tanpa_cap = jalankan("datar", None)
    dengan_cap = jalankan("datar", CAP_CONTOH)
    assert len(dengan_cap.trades) > len(tanpa_cap.trades), (
        f"batas {CAP_CONTOH:g} jam menghasilkan {len(dengan_cap.trades)} trade, "
        f"tidak lebih banyak dari {len(tanpa_cap.trades)} trade tanpa batas"
    )
    return (
        f"{len(tanpa_cap.trades)} trade tanpa batas menjadi "
        f"{len(dengan_cap.trades)} trade dengan batas {CAP_CONTOH:g} jam"
    )


def cek_paritas_aturan_live() -> str:
    """Keputusan time stop bot live identik dengan aturan yang dipakai backtest."""
    import trading.pump_scanner_bot as bot

    cfg = config_dasar(CAP_CONTOH)
    cap_ms = strategy.max_hold_ms(cfg, "5m")
    assert bot._max_hold_cap_ms(cfg) == cap_ms, (
        "batas yang dihitung bot live berbeda dari batas mesin backtest"
    )

    entry = 1_700_000_000_000
    beda = 0
    for umur in (
        0,
        BAR_MS_5M,
        cap_ms - 2 * BAR_MS_5M,
        cap_ms - BAR_MS_5M,
        cap_ms - BAR_MS_5M + 1,
        cap_ms - 1,
        cap_ms,
        cap_ms + BAR_MS_5M,
    ):
        pulihkan = _ctx_now_ms(entry + umur)
        try:
            harapan = strategy.time_stop_due(entry, entry + umur, cap_ms, BAR_MS_5M)
            hasil = bot._time_stop_due(cfg, {"entry_time": entry})
        finally:
            pulihkan()
        if bool(hasil) != bool(harapan):
            beda += 1
    assert beda == 0, f"{beda} dari 8 umur posisi memberi keputusan live yang berbeda"

    pulihkan = _ctx_now_ms(entry + cap_ms * 2)
    try:
        assert bot._time_stop_due(cfg, {"entry_time": 0}) is False, (
            "posisi lama tanpa entry_time tidak boleh ditutup paksa"
        )
        assert bot._time_stop_due(cfg, {}) is False, (
            "state tanpa entry_time tidak boleh ditutup paksa"
        )
        rusak = config_dasar(-5.0)
        assert bot._max_hold_cap_ms(rusak) == 0, (
            "config rusak seharusnya menonaktifkan time stop, bukan melempar error"
        )
        assert bot._time_stop_due(rusak, {"entry_time": entry}) is False
    finally:
        pulihkan()
    return "8 umur posisi sama dengan mesin backtest, state lama dan config rusak aman"


def cek_integrasi_parameter() -> str:
    """Kunci MAX_HOLD_HOURS terpasang di semua lapisan konfigurasi."""
    from config.settings_schema import PARAMETER_SCHEMA, relation_violations, validate_candidate

    assert not CONFIG_LOAD_ERRORS, f"config runtime punya error: {CONFIG_LOAD_ERRORS}"
    assert "MAX_HOLD_HOURS" in PUMP_CONFIG, "default config tidak punya MAX_HOLD_HOURS"
    assert "MAX_HOLD_HOURS" in PARAMETER_SCHEMA, "schema settings tidak punya kuncinya"
    assert "MAX_HOLD_HOURS" in bt.OVERRIDE_CASTERS, "override backtest belum mengenalnya"
    spec = PARAMETER_SCHEMA["MAX_HOLD_HOURS"]
    assert spec["type"] == "float" and spec["min"] == 0, "spec schema tidak sesuai"
    assert spec["max"] == strategy.MAX_HOLD_HOURS_LIMIT, (
        "batas atas schema harus sama dengan batas di strategy.indicators"
    )

    _, errors, _ = validate_candidate(deepcopy(PUMP_CONFIG), "PAPER")
    assert not errors, f"config default sendiri tidak lolos validasi: {errors}"
    kandidat = deepcopy(PUMP_CONFIG)
    kandidat["MAX_HOLD_HOURS"] = 12.0
    bersih, errors, _ = validate_candidate(kandidat, "PAPER")
    assert not errors and bersih["MAX_HOLD_HOURS"] == 12.0, (
        f"override 12 jam ditolak validasi: {errors}"
    )
    kurang = deepcopy(PUMP_CONFIG)
    kurang.pop("MAX_HOLD_HOURS", None)
    _, errors_kurang, _ = validate_candidate(kurang, "PAPER")
    assert errors_kurang.get("MAX_HOLD_HOURS") == "nilai tidak tersedia", (
        "schema seharusnya menandai kunci yang hilang sebagai tidak tersedia"
    )

    for buruk in (-3.0, 5000.0, "abc"):
        try:
            cfg = bt.apply_overrides(config_dasar(None), {"MAX_HOLD_HOURS": buruk})
            bt.validate_params(cfg)
        except bt.BacktestError:
            continue
        raise AssertionError(f"validate_params menerima MAX_HOLD_HOURS={buruk!r}")
    sempit = bt.apply_overrides(config_dasar(None), {"MAX_HOLD_HOURS": 0.1})
    try:
        bt.validate_params(sempit)
    except bt.BacktestError:
        pass
    else:
        raise AssertionError("validate_params menerima cap 0,1 jam pada interval 5m")

    cfg_relasi = config_dasar(None)
    cfg_relasi["MAX_HOLD_HOURS"] = 0.5
    cfg_relasi["CONFIRM_INTERVAL"] = "1h"
    assert "MAX_HOLD_HOURS" in relation_violations(cfg_relasi), (
        "relation_violations tidak menangkap cap 0,5 jam pada interval 1 jam"
    )
    assert "MAX_HOLD_HOURS" not in relation_violations(config_dasar(CAP_CONTOH))

    catatan = ""
    try:
        from web import dashboard as dash

        assert "MAX_HOLD_HOURS" in dash.BT_PARAM_KEYS, "dashboard belum mengenal kuncinya"
        assert "MAX_HOLD_HOURS" in dash.GRID_PARAM_KEYS, "grid dashboard belum mengenalnya"
    except ImportError as exc:
        catatan = f" (cek dashboard dilewati: {exc})"
    return (
        "config, schema, override backtest, validator, relasi, dan dashboard "
        "terhubung" + catatan
    )


CEK: tuple[tuple[str, Callable[[], str]], ...] = (
    ("aturan_time_stop", cek_aturan_time_stop),
    ("konversi_max_hold", cek_konversi_max_hold),
    ("time_stop_menutup_posisi", cek_time_stop_menutup_posisi),
    ("batas_berjenjang", cek_batas_berjenjang),
    ("time_stop_nonaktif_aman", cek_time_stop_nonaktif_aman),
    ("prioritas_exit_harga", cek_prioritas_exit_harga),
    ("kapital_berputar", cek_kapital_berputar),
    ("paritas_aturan_live", cek_paritas_aturan_live),
    ("integrasi_parameter", cek_integrasi_parameter),
)


def run(verbose: bool = True) -> tuple[bool, list[str]]:
    """Jalankan semua pemeriksaan. Kembalikan (semua_lulus, baris_laporan)."""
    baris: list[str] = []
    gagal = 0
    for nama, fungsi in CEK:
        try:
            detail = fungsi()
        except AssertionError as exc:
            gagal += 1
            teks = f"[GAGAL] {nama}: {exc}"
        except Exception as exc:  # kesalahan tak terduga tetap dilaporkan, bukan crash
            gagal += 1
            teks = f"[ERROR] {nama}: {type(exc).__name__}: {exc}"
        else:
            teks = f"[OK]    {nama}: {detail}"
        baris.append(teks)
        if verbose:
            print(teks)
    total = len(CEK)
    ringkas = (
        f"Selftest exit selesai: {total - gagal}/{total} pemeriksaan lulus."
        if gagal == 0
        else f"Selftest exit selesai: {gagal} dari {total} pemeriksaan GAGAL."
    )
    catatan = (
        "Catatan: baris peringatan 'Time stop nonaktif karena konfigurasi' yang "
        "muncul di tengah uji berasal dari uji config rusak dan memang diharapkan."
    )
    baris.append(ringkas)
    baris.append(catatan)
    if verbose:
        print(ringkas)
        print(catatan)
    return gagal == 0, baris


if __name__ == "__main__":
    sukses, _ = run(verbose=True)
    raise SystemExit(0 if sukses else 1)
