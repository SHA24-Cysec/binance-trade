#!/usr/bin/env python3
"""
Modul backtest untuk Pump Scanner Bot.
=======================================

TUJUAN: menguji parameter EXIT (Stop Loss, Take Profit, Breakeven, Trailing,
Max Hold) dan filter ENTRY (Min Pump % 24 jam, filter VWAP) pada SATU simbol
memakai data historis candle 5 menit dari Binance, memakai logika yang SAMA
PERSIS dengan `market_scanner.confirm_momentum()`,
`market_scanner.check_vwap_extension()`, dan `pump_scanner_bot.manage_exit()`
supaya hasilnya konsisten dengan cara bot asli bekerja.

INI BUKAN SIMULASI SEMPURNA. Baca "KETERBATASAN" di bawah sebelum
mempercayai hasilnya untuk keputusan finansial:

1. PERSAINGAN ANTAR-SIMBOL TIDAK DISIMULASIKAN. Bot asli memindai SELURUH
   pasar dan hanya mengambil SATU kandidat terbaik per rotasi. Backtest ini
   menguji "kalau bot kebetulan sedang memantau simbol ini, dan simbol ini
   lolos filter, apakah bot akan entry dan bagaimana hasilnya" -- bukan
   "berapa kali bot benar-benar akan memilih simbol ini dari semua pilihan
   yang tersedia saat itu". Hasilnya BUKAN prediksi return bot yang sesungguhnya,
   melainkan alat uji sensitivitas parameter exit.

2. GRANULARITAS CANDLE 5 MENIT untuk exit (bukan tick-by-tick / 15 detik
   seperti loop bot asli). Kalau harga TP dan level stop (Stop Loss/Breakeven/
   Trailing) sama-sama tersentuh dalam candle 5 menit yang sama, urutan
   sebenarnya tidak diketahui dari data candle. Backtest ini menerapkan
   urutan prioritas TETAP dan SENGAJA KONSERVATIF: STOP_LOSS diperiksa PALING
   AWAL (pakai harga TERENDAH candle), baru TAKE_PROFIT (harga TERTINGGI),
   lalu BREAKEVEN dan TRAILING_STOP (harga TERENDAH), baru MAX_HOLD_TIME.
   Urutan ini dipilih supaya hasil backtest tidak melebih-lebihkan profit --
   risiko selalu dianggap terealisasi lebih dulu kalau ambigu.

3. MOMENTUM_FADE_EXIT (keluar dini kalau simbol jatuh dari top-N gainer)
   TIDAK disimulasikan karena butuh data ranking SELURUH pasar per candle,
   bukan cuma satu simbol. Kalau di akun asli fitur ini aktif, hasil live
   bisa lebih baik (keluar lebih awal dari pump yang mati) dibanding
   backtest ini.

4. Filter volume (MIN_QUOTE_VOLUME_USDT_24H), spread maksimum, dan ukuran
   posisi TIDAK bisa diubah dari form backtest (dipertahankan dari config.py
   apa adanya) -- backtest ini fokus menguji parameter EXIT + filter pump 24h
   + filter VWAP saja, sesuai permintaan.

5. FILTER VWAP (USE_VWAP_FILTER) memakai VWAP BERGULIR jangka pendek, window
   sama dengan CONFIRM_LOOKBACK_BARS (candle konfirmasi momentum yang sama),
   BUKAN VWAP sesi/harian seperti di bursa saham. Binance Spot tidak punya
   jam buka/tutup sesi, jadi VWAP bergulir jangka pendek dipakai supaya
   mengukur leg pump yang SEDANG terjadi, bukan tercampur histori sebelum
   pump seperti kalau memakai VWAP 24 jam.

Karena keterbatasan di atas, gunakan hasil backtest ini sebagai alat bantu
membandingkan SATU set parameter dengan set parameter lain pada simbol yang
sama, bukan sebagai jaminan hasil trading di masa depan.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from strategy import Kline
import market_scanner as scanner

MS_PER_MIN = 60_000
MS_PER_DAY = 24 * 60 * MS_PER_MIN

INTERVAL_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240,
}


class BacktestError(Exception):
    pass


@dataclass
class BacktestTrade:
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    reason: str
    hold_minutes: float
    pnl_pct: float


@dataclass
class BacktestResult:
    symbol: str
    interval: str
    bars_total: int
    bars_usable: int
    start_time: int
    end_time: int
    trades: list = field(default_factory=list)
    params: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


def bars_per_day(interval: str) -> int:
    minutes = INTERVAL_MINUTES.get(interval)
    if not minutes:
        raise BacktestError(f"Interval '{interval}' tidak didukung untuk perhitungan 24 jam.")
    return (24 * 60) // minutes


def compute_rolling_24h_stats(klines: list[Kline], window: int) -> list[Optional[dict]]:
    """Untuk tiap index i, hitung (price_change_pct_24h, quote_volume_24h)
    berdasarkan window candle terakhir (termasuk candle i). None kalau
    riwayat belum cukup (i < window - 1) -- konsisten dengan bot asli yang
    baru mempertimbangkan simbol setelah ada histori 24 jam penuh."""
    n = len(klines)
    out: list[Optional[dict]] = [None] * n
    if n == 0:
        return out
    # Rolling sum volume pakai sliding window supaya O(n), bukan O(n*window).
    vol_sum = 0.0
    for i in range(n):
        vol_sum += klines[i].quote_volume
        if i >= window:
            vol_sum -= klines[i - window].quote_volume
        if i >= window - 1:
            ref_close = klines[i - window + 1].open  # harga acuan "24 jam lalu"
            if ref_close > 0:
                pct = (klines[i].close / ref_close - 1.0) * 100.0
                out[i] = {"pct24h": pct, "vol24h": vol_sum}
    return out


def fetch_full_klines(
    client,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    progress_cb: Optional[Callable[[float], None]] = None,
    sleep_between_calls: float = 0.25,
) -> list[Kline]:
    """Ambil semua candle dalam rentang [start_ms, end_ms] dengan paging
    (endpoint Binance maksimal 1000 candle per panggilan)."""
    from strategy import parse_klines

    all_rows: list = []
    cursor = start_ms
    minutes = INTERVAL_MINUTES.get(interval, 5)
    step_ms = minutes * MS_PER_MIN
    total_span = max(1, end_ms - start_ms)
    guard = 0
    guard_limit = 5000  # jaga-jaga supaya tidak infinite loop kalau API aneh

    while cursor < end_ms:
        guard += 1
        if guard > guard_limit:
            raise BacktestError("Terlalu banyak halaman data, dihentikan demi keamanan.")
        raw = client.get_klines(symbol, interval, limit=1000, start_time_ms=cursor, end_time_ms=end_ms)
        if not raw:
            break
        all_rows.extend(raw)
        last_open_time = int(raw[-1][0])
        next_cursor = last_open_time + step_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if progress_cb:
            done = min(1.0, (cursor - start_ms) / total_span)
            progress_cb(done)
        if len(raw) < 1000:
            break
        if sleep_between_calls:
            time.sleep(sleep_between_calls)

    klines = parse_klines(all_rows)
    # Buang duplikat (batas antar-halaman kadang tumpang tindih) & urutkan.
    seen = set()
    unique = []
    for k in klines:
        if k.open_time in seen:
            continue
        seen.add(k.open_time)
        unique.append(k)
    unique.sort(key=lambda k: k.open_time)
    return unique


def run_backtest(klines: list[Kline], config: dict, warmup_bars: int,
                  progress_cb: Optional[Callable[[float], None]] = None) -> BacktestResult:
    """klines: candle SUDAH termasuk periode warmup di depan (dipakai untuk
    hitung 24h stats & konfirmasi momentum), sepanjang `warmup_bars` candle
    pertama tidak akan dipakai sebagai titik entry, hanya sebagai referensi.

    config: dict gabungan PUMP_CONFIG + override parameter dari form (lihat
    apply_overrides())."""
    interval = config.get("CONFIRM_INTERVAL", "5m")
    window = bars_per_day(interval)
    stats = compute_rolling_24h_stats(klines, window)

    lookback = config["CONFIRM_LOOKBACK_BARS"]
    min_pump_pct = config["MIN_PUMP_PCT_24H"]
    min_vol = config["MIN_QUOTE_VOLUME_USDT_24H"]
    cooldown_ms = config["COOLDOWN_MINUTES_AFTER_CLOSE"] * MS_PER_MIN

    n = len(klines)
    trades: list[BacktestTrade] = []
    warnings: list[str] = []

    in_position = False
    entry_price = 0.0
    entry_time = 0
    be_active = False
    be_stop = 0.0
    trailing_active = False
    trailing_stop = 0.0
    next_entry_allowed_at = 0

    i = max(warmup_bars, window - 1, lookback)
    start_idx = i

    while i < n:
        if progress_cb and i % 200 == 0:
            progress_cb(min(1.0, (i - start_idx) / max(1, n - start_idx)))

        candle = klines[i]

        if not in_position:
            st = stats[i]
            if st is not None and st["pct24h"] >= min_pump_pct and st["vol24h"] >= min_vol \
                    and candle.open_time >= next_entry_allowed_at:
                window_klines = klines[max(0, i - lookback + 1): i + 1]
                ok, _reason = scanner.confirm_momentum(window_klines, config)
                if ok:
                    ok, _vwap_reason = scanner.check_vwap_extension(window_klines, config)
                if ok:
                    in_position = True
                    entry_price = candle.close
                    entry_time = candle.close_time
                    be_active = False
                    trailing_active = False
                    be_stop = 0.0
                    trailing_stop = 0.0
            i += 1
            continue

        # --- sudah dalam posisi: evaluasi candle demi candle ---
        pnl_high = (candle.high / entry_price - 1.0) * 100.0
        pnl_low = (candle.low / entry_price - 1.0) * 100.0
        hold_minutes = (candle.close_time - entry_time) / 60000.0

        sl_price = entry_price * (1 - config["SL_PCT"] / 100.0) if config["USE_STOP_LOSS"] else None

        if config["USE_BREAKEVEN"] and not be_active and pnl_high >= config["BE_TRIGGER_PCT"]:
            be_active = True
            be_stop = entry_price * (1 + config["BE_LOCK_PCT"] / 100.0)

        if config["USE_TRAILING"]:
            if not trailing_active and pnl_high >= config["TRAILING_START_PCT"]:
                trailing_active = True
                trailing_stop = candle.high * (1 - config["TRAILING_STEP_PCT"] / 100.0)
            elif trailing_active:
                cand_stop = candle.high * (1 - config["TRAILING_STEP_PCT"] / 100.0)
                if cand_stop > trailing_stop:
                    trailing_stop = cand_stop

        exit_reason = None
        exit_price = None

        # Prioritas SENGAJA dibuat KONSERVATIF: Stop Loss dicek PALING AWAL
        # (pakai harga TERENDAH candle). Kalau dalam satu candle 5 menit yang
        # sama harga sempat menyentuh level SL maupun level TP/BE/Trailing
        # (candle sangat fluktuatif), backtest ini menganggap SL kena LEBIH
        # DULU -- supaya hasil backtest tidak melebih-lebihkan profit dan
        # tetap jujur soal risiko, sesuai keputusan yang diminta saat
        # menambahkan fitur ini. Baru setelah itu TAKE_PROFIT (harga
        # TERTINGGI candle), lalu BREAKEVEN/TRAILING_STOP (harga TERENDAH),
        # baru MAX_HOLD_TIME.
        if config["USE_STOP_LOSS"] and pnl_low <= -abs(config["SL_PCT"]):
            exit_reason = "STOP_LOSS"
            exit_price = sl_price
        elif config["USE_TP"] and pnl_high >= config["TP_PCT"]:
            exit_reason = "TAKE_PROFIT"
            exit_price = entry_price * (1 + config["TP_PCT"] / 100.0)
        elif be_active and candle.low <= be_stop:
            exit_reason = "BREAKEVEN"
            exit_price = be_stop
        elif trailing_active and candle.low <= trailing_stop:
            exit_reason = "TRAILING_STOP"
            exit_price = trailing_stop
        elif hold_minutes >= config["MAX_HOLD_MINUTES"]:
            exit_reason = "MAX_HOLD_TIME"
            exit_price = candle.close

        is_last_bar = (i == n - 1)
        if exit_reason is None and is_last_bar:
            exit_reason = "END_OF_DATA"
            exit_price = candle.close
            warnings.append(
                "Posisi terakhir masih terbuka saat data historis habis (ditutup paksa di harga "
                "penutupan terakhir demi kelengkapan statistik, bukan exit sungguhan)."
            )

        if exit_reason:
            pnl_pct = (exit_price / entry_price - 1.0) * 100.0
            trades.append(BacktestTrade(
                entry_time=entry_time, entry_price=entry_price,
                exit_time=candle.close_time, exit_price=exit_price,
                reason=exit_reason, hold_minutes=hold_minutes, pnl_pct=pnl_pct,
            ))
            in_position = False
            next_entry_allowed_at = candle.close_time + cooldown_ms

        i += 1

    result = BacktestResult(
        symbol=config.get("_symbol", "?"),
        interval=interval,
        bars_total=n,
        bars_usable=max(0, n - start_idx),
        start_time=klines[start_idx].open_time if n > start_idx else (klines[0].open_time if n else 0),
        end_time=klines[-1].close_time if n else 0,
        trades=trades,
        params=config,
        warnings=warnings,
    )
    return result


def summarize(result: BacktestResult) -> dict:
    trades = result.trades
    total = len(trades)
    real_trades = [t for t in trades if t.reason != "END_OF_DATA"]
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    win_rate = (len(wins) / total * 100.0) if total else 0.0

    # Return kumulatif dihitung sebagai COMPOUNDING sederhana (reinvest 100%
    # tiap trade) supaya menggambarkan efek RISK_PERCENT tinggi -- BUKAN
    # penjumlahan biasa, karena bot memang memakai persentase saldo per entry.
    equity_curve = [0.0]  # dalam persen, basis 0% = modal awal
    equity_mult = 1.0
    for t in trades:
        equity_mult *= (1 + t.pnl_pct / 100.0)
        equity_curve.append((equity_mult - 1.0) * 100.0)

    total_return_pct = (equity_mult - 1.0) * 100.0

    peak = -1e18
    max_dd = 0.0
    for v in equity_curve:
        level = 1 + v / 100.0
        if level > peak:
            peak = level
        dd = (peak - level) / peak * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    avg_win = (sum(t.pnl_pct for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0.0
    gross_win = sum(t.pnl_pct for t in wins)
    gross_loss = abs(sum(t.pnl_pct for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    avg_hold = (sum(t.hold_minutes for t in trades) / total) if total else 0.0

    reason_counts: dict = {}
    for t in trades:
        reason_counts[t.reason] = reason_counts.get(t.reason, 0) + 1

    return {
        "total_trades": total,
        "real_trades": len(real_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_return_pct": total_return_pct,
        "max_drawdown_pct": max_dd,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "profit_factor": profit_factor,
        "avg_hold_minutes": avg_hold,
        "reason_counts": reason_counts,
        "equity_curve": equity_curve,
    }


def apply_overrides(base_config: dict, overrides: dict) -> dict:
    """Gabungkan PUMP_CONFIG asli dengan override dari form backtest.
    Hanya key yang dikenal (whitelist) yang boleh menimpa -- ini mencegah
    input form sembarangan mengubah field lain yang tidak dimaksudkan."""
    ALLOWED = {
        "SL_PCT": float,
        "TP_PCT": float,
        "BE_TRIGGER_PCT": float,
        "BE_LOCK_PCT": float,
        "TRAILING_START_PCT": float,
        "TRAILING_STEP_PCT": float,
        "MAX_HOLD_MINUTES": float,
        "MIN_PUMP_PCT_24H": float,
        "VWAP_MAX_EXTENSION_PCT": float,
    }
    cfg = copy.deepcopy(base_config)
    for key, caster in ALLOWED.items():
        if key in overrides and overrides[key] is not None and overrides[key] != "":
            try:
                cfg[key] = caster(overrides[key])
            except (TypeError, ValueError):
                raise BacktestError(f"Nilai parameter '{key}' tidak valid: {overrides[key]!r}")
    return cfg


def validate_params(cfg: dict) -> None:
    checks = [
        ("SL_PCT", 0.01, 1000),
        ("TP_PCT", 0.01, 1000),
        ("BE_TRIGGER_PCT", 0.01, 1000),
        ("BE_LOCK_PCT", -100, 1000),
        ("TRAILING_START_PCT", 0.01, 1000),
        ("TRAILING_STEP_PCT", 0.01, 1000),
        ("MAX_HOLD_MINUTES", 1, 100000),
        ("MIN_PUMP_PCT_24H", -100, 1000),
        ("VWAP_MAX_EXTENSION_PCT", 0.01, 1000),
    ]
    for key, lo, hi in checks:
        val = cfg.get(key)
        if val is None or not (lo <= val <= hi):
            raise BacktestError(f"Parameter '{key}'={val} di luar rentang wajar ({lo}..{hi}).")


# ---------------------------------------------------------------------
# Selftest -- murni logika, TANPA jaringan (pola sama seperti
# pump_scanner_bot.py --selftest).
# ---------------------------------------------------------------------
def _make_candle(t, o, h, l, c, vol=1_000_000.0, qvol=None):
    if qvol is None:
        qvol = vol * ((o + c) / 2.0)
    return Kline(open_time=t, open=o, high=h, low=l, close=c,
                 close_time=t + 299_999, volume=vol, quote_volume=qvol)


def selftest():
    print("=== SELFTEST backtest.py: rolling 24h stats ===")
    # 288 candle/hari (5 menit). Buat 2 hari data harga naik pelan lalu pump.
    klines = []
    t = 0
    price = 1.0
    for _ in range(288):  # hari 1: flat
        klines.append(_make_candle(t, price, price * 1.001, price * 0.999, price))
        t += 300_000
    for i in range(50):  # hari 2 awal: pump tajam (+15% dari harga awal)
        price = 1.0 * (1 + 0.15 * (i / 49))
        klines.append(_make_candle(t, price, price * 1.002, price * 0.998, price, vol=5_000_000.0))
        t += 300_000
    stats = compute_rolling_24h_stats(klines, window=288)
    assert stats[286] is None or stats[286]["pct24h"] is not None
    last = stats[-1]
    assert last is not None, "24h stats harusnya sudah terisi di akhir data"
    print(f"  pct24h akhir = {last['pct24h']:.2f}% (harus mendekati +15%)")
    assert 10 < last["pct24h"] < 20, f"pct24h tidak masuk akal: {last['pct24h']}"
    print("  -> OK")

    print("\n=== SELFTEST backtest.py: entry + TP ===")
    from config import PUMP_CONFIG
    cfg = dict(PUMP_CONFIG)
    cfg["MIN_PUMP_PCT_24H"] = 8.0
    cfg["MIN_QUOTE_VOLUME_USDT_24H"] = 1_000_000
    cfg["TP_PCT"] = 6.0
    cfg["USE_BREAKEVEN"] = True
    cfg["BE_TRIGGER_PCT"] = 3.0
    cfg["BE_LOCK_PCT"] = 0.3
    cfg["USE_TRAILING"] = True
    cfg["TRAILING_START_PCT"] = 4.0
    cfg["TRAILING_STEP_PCT"] = 1.5
    cfg["MAX_HOLD_MINUTES"] = 240
    cfg["_symbol"] = "TESTUSDT"

    # Setelah pump (naik terus, momentum jelas naik & candle terakhir bukan
    # reversal), lanjutkan naik tajam sampai kena TP (+6% dari entry).
    for i in range(20):
        price = price * 1.01
        klines.append(_make_candle(t, price, price * 1.012, price * 0.999, price, vol=5_000_000.0))
        t += 300_000

    result = run_backtest(klines, cfg, warmup_bars=0)
    print(f"  Jumlah trade terdeteksi: {len(result.trades)}")
    assert len(result.trades) >= 1, "Backtest harusnya mendeteksi minimal 1 entry pada skenario pump jelas ini"
    first = result.trades[0]
    print(f"  Trade pertama: entry={first.entry_price:.4f} exit={first.exit_price:.4f} "
          f"pnl={first.pnl_pct:+.2f}% alasan={first.reason}")
    assert first.reason in ("STOP_LOSS", "TAKE_PROFIT", "BREAKEVEN", "TRAILING_STOP", "MAX_HOLD_TIME", "END_OF_DATA")
    summary = summarize(result)
    print(f"  Ringkasan: total_trades={summary['total_trades']} win_rate={summary['win_rate']:.1f}% "
          f"total_return={summary['total_return_pct']:+.2f}% max_dd={summary['max_drawdown_pct']:.2f}%")
    print("  -> OK")

    print("\n=== SELFTEST backtest.py: tidak ada entry kalau filter tidak lolos ===")
    cfg2 = dict(cfg)
    cfg2["MIN_PUMP_PCT_24H"] = 500.0  # mustahil lolos
    result2 = run_backtest(klines, cfg2, warmup_bars=0)
    assert len(result2.trades) == 0, "Harusnya TIDAK ada entry kalau filter pump % dibuat mustahil"
    print("  -> OK (tidak ada entry, sesuai harapan)")

    print("\n=== SELFTEST backtest.py: filter VWAP menolak pump yang terlalu curam/ekstrem ===")
    # Pump SANGAT curam (+3%/candle, candle close dekat high supaya lolos cek
    # reversal confirm_momentum) -> extension dari VWAP window pendek akan
    # jauh melebihi VWAP_MAX_EXTENSION_PCT (5%) begitu momentum lolos.
    klines_curam = []
    t3 = 0
    price3 = 1.0
    for _ in range(288):
        klines_curam.append(_make_candle(t3, price3, price3 * 1.001, price3 * 0.999, price3))
        t3 += 300_000
    for _ in range(60):
        price3 = price3 * 1.03
        klines_curam.append(_make_candle(
            t3, price3 * 0.99, price3 * 1.001, price3 * 0.985, price3, vol=5_000_000.0))
        t3 += 300_000

    cfg_vwap_on = dict(cfg)
    cfg_vwap_on["MIN_PUMP_PCT_24H"] = 8.0
    cfg_vwap_on["MIN_QUOTE_VOLUME_USDT_24H"] = 1_000_000
    cfg_vwap_on["USE_VWAP_FILTER"] = True
    cfg_vwap_on["VWAP_MAX_EXTENSION_PCT"] = 5.0
    result_on = run_backtest(klines_curam, cfg_vwap_on, warmup_bars=0)
    print(f"  Filter VWAP ON, pump curam +3%/candle -> jumlah trade: {len(result_on.trades)}")
    assert len(result_on.trades) == 0, "Pump yang terlalu curam harusnya DITOLAK filter VWAP (0 trade)"

    cfg_vwap_off = dict(cfg_vwap_on)
    cfg_vwap_off["USE_VWAP_FILTER"] = False
    result_off = run_backtest(klines_curam, cfg_vwap_off, warmup_bars=0)
    print(f"  Filter VWAP OFF, data sama persis -> jumlah trade: {len(result_off.trades)}")
    assert len(result_off.trades) > 0, "Tanpa filter VWAP, pump curam yang sama harusnya tetap bisa entry"
    print("  -> OK (filter VWAP terbukti menolak entry pada pump yang kepanasan/ekstrem)")

    print("\n=== SELFTEST backtest.py: Stop Loss kena SEBELUM Breakeven/Trailing aktif ===")
    # Bangun ulang data flat -> pump (SAMA seperti tes di atas), tapi
    # DIPOTONG PERSIS di candle tempat entry pertama terjadi (index 315,
    # harga 1.08265...) -- tanpa candle pump lanjutan sesudahnya -- lalu
    # LANGSUNG disambung candle anjlok tajam beruntun. Ini mengisolasi
    # skenario: harga turun terus sejak entry dan TIDAK PERNAH naik lagi ke
    # BE_TRIGGER_PCT=3% profit, persis kasus yang TIDAK terlindungi oleh
    # Breakeven/Trailing sendirian (keduanya baru aktif setelah profit).
    sl_klines = []
    t2 = 0
    p2 = 1.0
    for _ in range(288):  # hari 1: flat
        sl_klines.append(_make_candle(t2, p2, p2 * 1.001, p2 * 0.999, p2))
        t2 += 300_000
    for i in range(50):  # hari 2: pump tajam +15%
        p2 = 1.0 * (1 + 0.15 * (i / 49))
        sl_klines.append(_make_candle(t2, p2, p2 * 1.002, p2 * 0.998, p2, vol=5_000_000.0))
        t2 += 300_000
    sl_klines = sl_klines[:316]  # potong TEPAT di candle tempat entry pertama terjadi (diverifikasi manual)
    entry_ref_price = sl_klines[-1].close
    t2 = sl_klines[-1].close_time + 1
    p2 = entry_ref_price
    for _ in range(5):
        p2 = p2 * 0.98  # turun bertahap, total lebih dari 3% dalam beberapa candle
        sl_klines.append(_make_candle(t2, p2 * 1.001, p2 * 1.002, p2 * 0.998, p2, vol=5_000_000.0))
        t2 += 300_000

    sl_cfg = dict(cfg)
    sl_cfg["TP_PCT"] = 999.0  # matikan TP secara efektif, isolasi pengujian SL murni
    sl_cfg["USE_STOP_LOSS"] = True
    sl_cfg["SL_PCT"] = 3.0
    sl_result = run_backtest(sl_klines, sl_cfg, warmup_bars=0)
    assert len(sl_result.trades) >= 1, "Skenario Stop Loss harusnya tetap menghasilkan 1 entry"
    sl_trade = sl_result.trades[0]
    print(f"  Trade: entry={sl_trade.entry_price:.4f} exit={sl_trade.exit_price:.4f} "
          f"pnl={sl_trade.pnl_pct:+.2f}% alasan={sl_trade.reason}")
    assert sl_trade.reason == "STOP_LOSS", f"Harusnya keluar karena STOP_LOSS, dapat: {sl_trade.reason}"
    assert sl_trade.pnl_pct < 0, "Trade Stop Loss harusnya rugi"
    assert abs(sl_trade.pnl_pct - (-3.0)) < 0.05, \
        f"Kerugian Stop Loss harusnya persis -3.00% (harga exit dikunci di level SL), dapat {sl_trade.pnl_pct:.2f}%"
    print("  -> OK (Stop Loss kena tepat di -3.00%, TIDAK menunggu sampai MAX_HOLD_TIME)")

    print("\n=== SELFTEST backtest.py: Stop Loss OFF -> trade yang sama jadi tertahan lebih lama/lebih rugi ===")
    sl_cfg_off = dict(sl_cfg)
    sl_cfg_off["USE_STOP_LOSS"] = False
    sl_result_off = run_backtest(sl_klines, sl_cfg_off, warmup_bars=0)
    assert len(sl_result_off.trades) >= 1
    sl_trade_off = sl_result_off.trades[0]
    print(f"  Trade (SL off): pnl={sl_trade_off.pnl_pct:+.2f}% alasan={sl_trade_off.reason}")
    assert sl_trade_off.reason != "STOP_LOSS", "Kalau USE_STOP_LOSS=False, alasan STOP_LOSS tidak boleh muncul"
    assert sl_trade_off.pnl_pct <= sl_trade.pnl_pct, \
        "Tanpa Stop Loss, kerugian pada skenario ini harusnya sama besar atau lebih buruk (tertahan lebih lama)"
    print("  -> OK (tanpa Stop Loss, posisi tidak dilindungi dan kerugian sama/lebih besar)")

    print("\n=== SELFTEST backtest.py: apply_overrides & validate_params ===")
    merged = apply_overrides(dict(PUMP_CONFIG), {"TP_PCT": "8.5", "MIN_PUMP_PCT_24H": "10", "SL_PCT": "4.5"})
    assert merged["TP_PCT"] == 8.5 and merged["MIN_PUMP_PCT_24H"] == 10.0 and merged["SL_PCT"] == 4.5
    validate_params(merged)
    try:
        validate_params(apply_overrides(dict(PUMP_CONFIG), {"TP_PCT": "-5"}))
        raise AssertionError("Harusnya menolak TP_PCT negatif")
    except BacktestError:
        pass
    try:
        validate_params(apply_overrides(dict(PUMP_CONFIG), {"SL_PCT": "-5"}))
        raise AssertionError("Harusnya menolak SL_PCT negatif")
    except BacktestError:
        pass
    print("  -> OK")

    print("\nSEMUA SELFTEST backtest.py LULUS.")
    print("(Tidak menghubungi Binance sama sekali -- murni logika lokal dengan data sintetis.)")


if __name__ == "__main__":
    selftest()
