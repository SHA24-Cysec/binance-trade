#!/usr/bin/env python3
"""
Modul backtest untuk Pump Scanner Bot.
=======================================

TUJUAN: menguji parameter exit (Stop Loss, Take Profit, Breakeven, Trailing)
dan parameter setup pullback retest pada SATU simbol memakai data historis
candle dari Binance, memakai logika yang
SAMA PERSIS dengan `market_scanner.detect_pullback_retest()` dan
`pump_scanner_bot.manage_exit()` supaya hasilnya konsisten dengan cara bot
asli bekerja.

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
   lalu BREAKEVEN dan TRAILING_STOP (harga TERENDAH). Urutan ini dipilih
   supaya hasil backtest tidak melebih-lebihkan profit, risiko selalu dianggap
   terealisasi lebih dulu kalau ambigu.

3. Spread maksimum dan ukuran posisi TIDAK bisa diubah dari form backtest
   (dipertahankan dari config.py apa adanya). Slippage market order juga
   TIDAK dimodelkan, entry dan exit dianggap terjadi tepat di harga level
   atau harga penutupan candle. Fee taker beli dan jual SUDAH dipotong.

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
import strategy
import market_scanner as scanner

MS_PER_MIN = 60_000
MS_PER_DAY = 24 * 60 * MS_PER_MIN

# Satu sumber kebenaran ada di strategy.py supaya bot live, backtest satu
# simbol, dan backtest portofolio tidak pernah memakai tabel yang berbeda.
INTERVAL_MINUTES = strategy.INTERVAL_MINUTES


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
    sl_pct: float = 0.0
    tp_pct: float = 0.0
    exit_source: str = "FIXED"
    gross_pnl_pct: float = 0.0
    fee_pct: float = 0.0
    position_notional: float = 0.0
    equity_before: float = 0.0
    equity_after: float = 0.0
    pnl_quote: float = 0.0


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
    initial_equity: float = 0.0
    final_equity: float = 0.0


def bars_per_day(interval: str) -> int:
    minutes = INTERVAL_MINUTES.get(interval)
    if not minutes:
        raise BacktestError(f"Interval '{interval}' tidak didukung untuk perhitungan 24 jam.")
    return (24 * 60) // minutes


def initial_backtest_equity(config: dict) -> float:
    """Equity quote awal untuk simulasi sizing sequential.

    Nilai eksplisit BACKTEST_INITIAL_EQUITY_USDT diutamakan. Fallback menjaga
    kompatibilitas config lama dengan saldo PAPER awal, lalu 10.000 USDT.
    """
    fallback = (config.get("PAPER_INITIAL_BALANCES", {}) or {}).get(
        config.get("QUOTE_ASSET", "USDT"), 10_000.0)
    value = float(config.get("BACKTEST_INITIAL_EQUITY_USDT", fallback) or 0.0)
    return max(0.0, value)


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
                  progress_cb: Optional[Callable[[float], None]] = None,
                  daily_klines: Optional[list[Kline]] = None) -> BacktestResult:
    """klines: candle SUDAH termasuk periode warmup di depan (dipakai untuk
    hitung 24h stats & deteksi setup), sepanjang `warmup_bars` candle
    pertama tidak akan dipakai sebagai titik entry, hanya sebagai referensi.

    config: dict gabungan PUMP_CONFIG + override parameter dari form (lihat
    apply_overrides()).

    daily_klines: candle 1d simbol yang sama, dipakai gerbang pump untuk
    menghitung rata-rata volume kuotasi 7 hari penuh SEBELUM tiap bar yang
    diuji. Kalau None, deret harian dibangun dengan menjumlahkan candle
    intraday yang sudah ada (scanner.aggregate_to_daily), jadi gerbangnya
    tetap berlaku tanpa request tambahan. Perlu dicatat: hari pertama pada
    data intraday biasanya tidak lengkap sehingga volumenya lebih kecil dari
    volume harian sebenarnya. Pemanggil yang butuh presisi penuh (dashboard)
    sebaiknya mengunduh candle 1d asli dan mengoperkannya lewat parameter ini.

    Gerbang pump dievaluasi PER BAR memakai fungsi yang sama dengan bot live
    (market_scanner.evaluate_pump_gate), dan hanya memakai candle harian yang
    sudah tertutup pada bar tersebut, jadi tidak ada look-ahead."""
    interval = config.get("CONFIRM_INTERVAL", "5m")
    window = bars_per_day(interval)
    stats = compute_rolling_24h_stats(klines, window)
    daily_series = daily_klines if daily_klines else scanner.aggregate_to_daily(klines)

    # Jendela konfirmasi memakai fungsi bersama, sama dengan bot live.
    lookback = strategy.confirm_window_bars(config)
    min_vol = config["MIN_QUOTE_VOLUME_USDT_24H"]
    cooldown_ms = config["COOLDOWN_MINUTES_AFTER_CLOSE"] * MS_PER_MIN

    n = len(klines)
    trades: list[BacktestTrade] = []
    warnings: list[str] = []

    initial_equity = initial_backtest_equity(config)
    symbol = str(config.get("_symbol", "") or "")
    historical_tradable = config.get("_historical_tradable_symbols")
    if symbol and not scanner.is_structurally_allowed_symbol(symbol, config, historical_tradable):
        warnings.append("Simbol ditolak oleh policy semesta bersama (quote, stablecoin, leveraged token, blacklist, atau status historis).")
        return BacktestResult(symbol=symbol, interval=interval, bars_total=n, bars_usable=0,
                              start_time=klines[0].open_time if klines else 0,
                              end_time=klines[-1].close_time if klines else 0,
                              trades=[], params=config, warnings=warnings,
                              initial_equity=initial_equity, final_equity=initial_equity)
    equity = initial_equity
    in_position = False
    entry_price = 0.0
    entry_time = 0
    position_notional = 0.0
    equity_before_entry = 0.0
    be_active = False
    be_stop = 0.0
    trailing_active = False
    trailing_stop = 0.0
    next_entry_allowed_at = 0

    # Biaya per putaran: fee taker dibayar saat BUY dan saat SELL.
    try:
        from config import get_taker_fee_pct as _fee_fn
        fee_round_trip_pct = _fee_fn(config) * 2.0
    except ImportError:
        fee_round_trip_pct = float(config.get("TAKER_FEE_PCT", 0.1)) * 2.0

    cur_sl = abs(float(config.get("SL_PCT", 1.8)))
    cur_tp = abs(float(config.get("TP_PCT", 4.0)))
    cur_be_trig = abs(float(config.get("BE_TRIGGER_PCT", 1.0)))
    cur_be_lock = abs(float(config.get("BE_LOCK_PCT", 0.15)))
    cur_tr_start = abs(float(config.get("TRAILING_START_PCT", 1.5)))
    cur_tr_step = abs(float(config.get("TRAILING_STEP_PCT", 0.6)))
    cur_src = "FIXED"

    i = max(warmup_bars, window - 1, lookback)
    start_idx = i

    while i < n:
        if progress_cb and i % 200 == 0:
            progress_cb(min(1.0, (i - start_idx) / max(1, n - start_idx)))

        candle = klines[i]

        if not in_position:
            st = stats[i]
            # Dua gerbang semesta, sama persis dengan bot live: likuiditas
            # lebih dulu (murah), lalu gerbang pump (naik 24 jam dan volume
            # naik). Rata-rata 7 hari dihitung hanya dari candle harian yang
            # sudah tertutup pada bar ini, bukan dari data hari ini.
            if st is not None and st["vol24h"] >= min_vol \
                    and candle.open_time >= next_entry_allowed_at \
                    and scanner.pump_gate_ok_at(daily_series, candle.close_time,
                                                st["pct24h"], st["vol24h"], config):
                # Pada index i candle sudah dianggap selesai. Fungsi yang sama
                # dipakai bot live setelah ia membuang candle yang masih
                # berjalan, jadi aturan entry tidak berbeda antara live dan
                # backtest.
                window_klines = klines[max(0, i - lookback + 1): i + 1]
                setup = scanner.detect_pullback_retest(window_klines, config)
                if setup.ok:
                    sizing = strategy.resolve_position_notional(config, equity)
                    # Sama seperti live: posisi fixed yang lebih besar dari
                    # saldo tidak boleh "terisi" secara ajaib di backtest.
                    if sizing["notional"] <= 0 or sizing["notional"] > equity:
                        i += 1
                        continue
                    in_position = True
                    position_notional = sizing["notional"]
                    equity_before_entry = equity
                    entry_price = candle.close
                    entry_time = candle.close_time
                    be_active = False
                    trailing_active = False
                    be_stop = 0.0
                    trailing_stop = 0.0
                    # Level exit dikunci saat entry memakai fungsi yang sama
                    # dengan bot live supaya hasil backtest mewakili perilaku bot.
                    level_cfg = dict(config)
                    level_cfg["_atr_value"] = strategy.atr(klines[:i + 1], int(config.get("ATR_PERIOD", 14) or 14))
                    lv = strategy.resolve_exit_levels(level_cfg)
                    cur_sl = lv["sl_pct"]
                    cur_tp = lv["tp_pct"]
                    cur_be_trig = lv["be_trigger_pct"]
                    cur_be_lock = lv["be_lock_pct"]
                    cur_tr_start = lv["trail_start_pct"]
                    cur_tr_step = lv["trail_step_pct"]
                    cur_src = lv["source"]
            i += 1
            continue

        # --- sudah dalam posisi: evaluasi candle demi candle ---
        pnl_high = (candle.high / entry_price - 1.0) * 100.0
        pnl_low = (candle.low / entry_price - 1.0) * 100.0
        hold_minutes = (candle.close_time - entry_time) / 60000.0

        atr_mode = cur_src == "ATR"
        sl_price = (entry_price - cur_sl) if atr_mode else entry_price * (1 - cur_sl / 100.0)
        pnl_high_unit = candle.high - entry_price if atr_mode else pnl_high
        if config["USE_BREAKEVEN"] and not be_active and pnl_high_unit >= cur_be_trig:
            be_active = True
            be_stop = entry_price + cur_be_lock if atr_mode else entry_price * (1 + cur_be_lock / 100.0)
        if config["USE_TRAILING"]:
            if not trailing_active and pnl_high_unit >= cur_tr_start:
                trailing_active = True
                trailing_stop = candle.high - cur_tr_step if atr_mode else candle.high * (1 - cur_tr_step / 100.0)
            elif trailing_active:
                cand_stop = candle.high - cur_tr_step if atr_mode else candle.high * (1 - cur_tr_step / 100.0)
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
        # TERTINGGI candle), lalu BREAKEVEN/TRAILING_STOP (harga TERENDAH).
        #
        # Fill GAP-AWARE (perbaikan audit B-06): kalau candle DIBUKA sudah
        # menembus level (gap), harga exit realistis adalah harga pembukaan,
        # bukan level stopnya -- konsisten dengan paper_engine yang mengisi
        # stop pada harga book pasca-gap. Tanpa ini backtest terlalu optimis
        # pada pair yang sering gap.
        sl_triggered = (candle.low <= sl_price if atr_mode else pnl_low <= -cur_sl)
        if config["USE_STOP_LOSS"] and sl_triggered:
            exit_reason = "STOP_LOSS"
            exit_price = min(sl_price, candle.open)
        elif config["USE_TP"] and (candle.high >= entry_price + cur_tp if atr_mode else pnl_high >= cur_tp):
            exit_reason = "TAKE_PROFIT"
            exit_price = max(entry_price + cur_tp if atr_mode else entry_price * (1 + cur_tp / 100.0), candle.open)
        elif be_active and candle.low <= be_stop:
            exit_reason = "BREAKEVEN"
            exit_price = min(be_stop, candle.open)
        elif trailing_active and candle.low <= trailing_stop:
            exit_reason = "TRAILING_STOP"
            exit_price = min(trailing_stop, candle.open)

        is_last_bar = (i == n - 1)
        if exit_reason is None and is_last_bar:
            exit_reason = "END_OF_DATA"
            exit_price = candle.close
            warnings.append(
                "Posisi terakhir masih terbuka saat data historis habis (ditutup paksa di harga "
                "penutupan terakhir demi kelengkapan statistik, bukan exit sungguhan)."
            )

        if exit_reason:
            # PnL KOTOR (belum dipotong biaya)
            gross_pct = (exit_price / entry_price - 1.0) * 100.0
            # PnL BERSIH: fee taker dibayar DUA KALI (saat beli dan saat jual).
            # Ini bukan detail kosmetik -- pada strategi dengan TP 4% dan
            # banyak trade, biaya 0,2% pulang-pergi memakan bagian nyata dari
            # hasil. Backtest yang mengabaikannya akan terlihat jauh lebih
            # bagus daripada kenyataan.
            pnl_pct = gross_pct - fee_round_trip_pct
            pnl_quote = position_notional * pnl_pct / 100.0
            equity_after = max(0.0, equity + pnl_quote)
            trades.append(BacktestTrade(
                entry_time=entry_time, entry_price=entry_price,
                exit_time=candle.close_time, exit_price=exit_price,
                reason=exit_reason, hold_minutes=hold_minutes, pnl_pct=pnl_pct,
                sl_pct=cur_sl, tp_pct=cur_tp, exit_source=cur_src,
                gross_pnl_pct=gross_pct, fee_pct=fee_round_trip_pct,
                position_notional=position_notional, equity_before=equity_before_entry,
                equity_after=equity_after, pnl_quote=pnl_quote,
            ))
            equity = equity_after
            in_position = False
            position_notional = 0.0
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
        initial_equity=initial_equity,
        final_equity=equity,
    )
    return result


def summarize(result: BacktestResult) -> dict:
    trades = result.trades
    total = len(trades)
    real_trades = [t for t in trades if t.reason != "END_OF_DATA"]
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    win_rate = (len(wins) / total * 100.0) if total else 0.0

    # Equity curve memakai hasil nominal yang benar-benar disimulasikan oleh
    # run_backtest, bukan mengompound pnl% seolah setiap entry memakai 100%
    # akun. Untuk objek lama/manual tanpa nominal, fallback menjaga fungsi
    # tetap dapat dipakai dengan modal konfigurasi saat ini.
    initial = result.initial_equity or initial_backtest_equity(result.params)
    equity = initial
    gross_equity = initial
    equity_curve = [0.0]
    for t in trades:
        notional = t.position_notional if t.position_notional > 0 else equity
        if t.equity_after > 0 or t.pnl_quote != 0:
            equity = t.equity_after
        else:
            equity = max(0.0, equity + notional * t.pnl_pct / 100.0)
        gross_equity = max(0.0, gross_equity + notional * t.gross_pnl_pct / 100.0)
        equity_curve.append((equity / initial - 1.0) * 100.0 if initial > 0 else 0.0)

    final_equity = equity if trades else (result.final_equity or initial)
    total_return_pct = ((final_equity / initial) - 1.0) * 100.0 if initial > 0 else 0.0
    gross_return_pct = ((gross_equity / initial) - 1.0) * 100.0 if initial > 0 else 0.0

    peak = initial
    max_dd = 0.0
    for value in [initial] + [initial * (1.0 + v / 100.0) for v in equity_curve[1:]]:
        peak = max(peak, value)
        dd = (peak - value) / peak * 100.0 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

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
        "initial_equity": initial,
        "final_equity": final_equity,
        "total_pnl_quote": final_equity - initial,
        "gross_return_pct": gross_return_pct,
        "fee_drag_pct": gross_return_pct - total_return_pct,
        "total_fee_pct": sum(t.fee_pct for t in trades),
    }


def apply_overrides(base_config: dict, overrides: dict) -> dict:
    """Gabungkan PUMP_CONFIG asli dengan override dari form backtest.
    Hanya key yang dikenal (whitelist) yang boleh menimpa -- ini mencegah
    input form sembarangan mengubah field lain yang tidak dimaksudkan."""
    def _as_bool(v):
        """Terima True/False asli, juga string 'true'/'1'/'on' dari form web."""
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        s = str(v).strip().lower()
        if s in ("true", "1", "on", "yes", "ya"):
            return True
        if s in ("false", "0", "off", "no", "tidak"):
            return False
        raise ValueError(f"bukan boolean: {v!r}")

    ALLOWED = {
        "SL_PCT": float,
        "TP_PCT": float,
        "BE_TRIGGER_PCT": float,
        "BE_LOCK_PCT": float,
        "TRAILING_START_PCT": float,
        "TRAILING_STEP_PCT": float,
        # Parameter setup pullback retest yang boleh diuji dari form backtest.
        "MAX_BARS_BREAKOUT_TO_RETEST": int,
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
        ("MAX_BARS_BREAKOUT_TO_RETEST", 1, 500),
    ]
    for key, lo, hi in checks:
        val = cfg.get(key)
        if val is None or not (lo <= val <= hi):
            raise BacktestError(f"Parameter '{key}'={val} di luar rentang wajar ({lo}..{hi}).")

    butuh = strategy.required_lookback_bars(cfg)
    if int(cfg.get("CONFIRM_LOOKBACK_BARS", 0)) < butuh:
        raise BacktestError(
            f"CONFIRM_LOOKBACK_BARS={cfg.get('CONFIRM_LOOKBACK_BARS')} lebih kecil dari {butuh} "
            "candle yang dibutuhkan struktur setup. Naikkan nilainya supaya backtest "
            "dan bot live memakai jendela yang sama."
        )


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
    klines = []
    t = 0
    price = 1.0
    for _ in range(288):
        klines.append(_make_candle(t, price, price * 1.001, price * 0.999, price))
        t += 300_000
    for i in range(50):
        price = 1.0 * (1 + 0.15 * (i / 49))
        klines.append(_make_candle(t, price, price * 1.002, price * 0.998, price, vol=5_000_000.0))
        t += 300_000
    stats = compute_rolling_24h_stats(klines, window=288)
    last = stats[-1]
    assert last is not None, "24h stats harusnya sudah terisi di akhir data"
    assert 10 < last["pct24h"] < 20, f"pct24h tidak masuk akal: {last['pct24h']}"
    print(f"  pct24h akhir = {last['pct24h']:.2f}% -> OK")

    print("\n=== SELFTEST backtest.py: entry setup pullback retest + TP ===")
    from config import PUMP_CONFIG
    from synthetic_data import (
        cfg_gerbang_pump_nonaktif, riwayat_harian, seri_dengan_setup,
    )
    cfg = cfg_gerbang_pump_nonaktif(PUMP_CONFIG)
    cfg["MIN_QUOTE_VOLUME_USDT_24H"] = 1_000_000
    cfg["TP_PCT"] = 6.0
    cfg["USE_BREAKEVEN"] = True
    cfg["BE_TRIGGER_PCT"] = 3.0
    cfg["BE_LOCK_PCT"] = 0.3
    cfg["USE_TRAILING"] = True
    cfg["TRAILING_START_PCT"] = 4.0
    cfg["TRAILING_STEP_PCT"] = 1.5
    cfg["_symbol"] = "TESTUSDT"

    vals = [100.0] * 288 + [100.2, 100.4, 99.4, 98.4, 97.4, 97.6,
                            98.6, 98.1, 98.3, 97.8, 98.8, 99.8, 98.8,
                            99.8, 99.3, 99.5, 98.5, 99.5, 98.5, 100.0]
    kl_setup = [_make_candle(i * 300_000, v, v + 1, max(0.01, v - 1), v,
                             vol=5_000_000.0) for i, v in enumerate(vals)]
    result = run_backtest(kl_setup, cfg, warmup_bars=0, daily_klines=riwayat_harian(kl_setup))
    assert len(result.trades) >= 1, "Backtest harus mendeteksi minimal 1 entry pada skenario sah"
    first = result.trades[0]
    assert first.reason in ("STOP_LOSS", "TAKE_PROFIT", "BREAKEVEN", "TRAILING_STOP", "END_OF_DATA")
    summary = summarize(result)
    print(f"  Trade pertama: alasan={first.reason}, pnl={first.pnl_pct:+.2f}%")
    print(f"  Ringkasan: total_trades={summary['total_trades']} win_rate={summary['win_rate']:.1f}% -> OK")

    print("\n=== SELFTEST backtest.py: tidak ada entry kalau gerbang likuiditas tidak lolos ===")
    cfg2 = dict(cfg)
    cfg2["MIN_QUOTE_VOLUME_USDT_24H"] = 1e18
    result2 = run_backtest(kl_setup, cfg2, warmup_bars=0, daily_klines=riwayat_harian(kl_setup))
    assert len(result2.trades) == 0, "Harusnya tidak ada entry kalau gerbang volume mustahil"
    assert len(result.trades) > 0, "Kontrol positif gagal: data wajar harus menghasilkan trade"
    print("  -> OK")

    print("\n=== SELFTEST backtest.py: data candle rusak ditolak ===")
    kasus_rusak = [
        ("close NaN", [[0, "1", "2", "0.5", "NaN", 10, 300_000, 1000]]),
        ("close nol", [[0, "1", "2", "0.5", "0", 10, 300_000, 1000]]),
        ("close negatif", [[0, "1", "2", "0.5", "-5", 10, 300_000, 1000]]),
        ("close tak hingga", [[0, "1", "2", "0.5", "inf", 10, 300_000, 1000]]),
        ("high < low", [[0, "1", "0.5", "2", "1", 10, 300_000, 1000]]),
    ]
    for nama, raw in kasus_rusak:
        try:
            strategy.parse_klines(raw)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Candle rusak ({nama}) tidak ditolak parse_klines().")
    ok = strategy.parse_klines([[0, "1", "2", "0.5", "1.5", 10, 300_000, 1000]])
    assert len(ok) == 1 and ok[0].close == 1.5, "Candle normal malah ditolak"
    print("  -> OK")

    print("\n=== SELFTEST backtest.py: Stop Loss kena sebelum proteksi profit aktif ===")
    sl_klines = list(kl_setup)
    entry_ref_price = sl_klines[-1].close
    t2 = sl_klines[-1].close_time + 1
    p2 = entry_ref_price
    for _ in range(5):
        prev = p2
        p2 = p2 * 0.98
        sl_klines.append(_make_candle(t2, prev, prev, p2 * 0.998, p2, vol=5_000_000.0))
        t2 += 300_000

    sl_cfg = dict(cfg)
    sl_cfg["TP_PCT"] = 999.0
    sl_cfg["USE_STOP_LOSS"] = True
    sl_cfg["SL_PCT"] = 3.0
    sl_result = run_backtest(sl_klines, sl_cfg, warmup_bars=0, daily_klines=riwayat_harian(sl_klines))
    assert len(sl_result.trades) >= 1, "Skenario Stop Loss harus menghasilkan entry"
    sl_trade = sl_result.trades[0]
    assert sl_trade.reason == "STOP_LOSS", f"Harusnya keluar karena STOP_LOSS, dapat: {sl_trade.reason}"
    assert sl_trade.pnl_pct < 0, "Trade Stop Loss harus rugi"
    from config import get_taker_fee_pct as fee_now
    expected_fee = fee_now(cfg) * 2
    assert abs(sl_trade.fee_pct - expected_fee) < 1e-9
    print(f"  -> OK (gross {sl_trade.gross_pnl_pct:.2f}%, fee {sl_trade.fee_pct:.2f}%)")

    print("\n=== SELFTEST backtest.py: apply_overrides, validate_params, dan exit tetap ===")
    merged = apply_overrides(dict(PUMP_CONFIG), {"TP_PCT": "8.5", "SL_PCT": "4.5"})
    assert merged["TP_PCT"] == 8.5 and merged["SL_PCT"] == 4.5
    validate_params(merged)
    try:
        validate_params(apply_overrides(dict(PUMP_CONFIG), {"TP_PCT": "-5"}))
        raise AssertionError("Harusnya menolak TP_PCT negatif")
    except BacktestError:
        pass
    levels = strategy.resolve_exit_levels(dict(PUMP_CONFIG, USE_ATR_EXIT=False, SL_PCT=3.0, TP_PCT=6.0))
    assert levels["source"] == "FIXED" and levels["sl_pct"] == 3.0 and levels["tp_pct"] == 6.0
    print("  -> OK")

    print("\nSEMUA SELFTEST backtest.py LULUS.")
    print("(Tidak menghubungi Binance sama sekali, murni logika lokal dengan data sintetis.)")


def print_single_result(result: BacktestResult) -> None:
    summary = summarize(result)
    print("\n" + "=" * 72)
    print("HASIL BACKTEST")
    print("=" * 72)
    print(f"Simbol             : {result.symbol}")
    print(f"Interval           : {result.interval}")
    print(f"Candle total       : {result.bars_total}")
    print(f"Trade nyata        : {summary['real_trades']}")
    print(f"Return bersih      : {summary['total_return_pct']:+.2f}%")
    print(f"Return kotor       : {summary['gross_return_pct']:+.2f}%")
    print(f"Win rate           : {summary['win_rate']:.1f}%")
    print(f"Max drawdown       : {summary['max_drawdown_pct']:.2f}%")
    print(f"Profit factor      : {summary['profit_factor']:.2f}")
    print(f"Alasan exit        : {dict(sorted(summary['reason_counts'].items()))}")
    if result.warnings:
        print("Peringatan:")
        for item in result.warnings:
            print(f"  - {item}")
    print("=" * 72 + "\n")


def main():
    import argparse as _argparse
    import sys as _sys

    parser = _argparse.ArgumentParser(description="Backtest pump scanner")
    parser.add_argument("--selftest", action="store_true",
                         help="Jalankan audit logika lokal tanpa jaringan lalu keluar.")
    parser.add_argument("--symbol", default="BTCUSDT", help="Simbol, mis. SOLUSDT")
    parser.add_argument("--days", type=int, default=30, help="Jumlah hari data historis")
    parser.add_argument("--interval", default=None, help="Interval candle, default dari config")
    args = parser.parse_args()

    if args.selftest or len(_sys.argv) == 1:
        selftest()
        return

    from config import PUMP_CONFIG
    from binance_client import BinanceSpotClient

    cfg = copy.deepcopy(PUMP_CONFIG)
    cfg["_symbol"] = args.symbol
    if args.interval:
        cfg["CONFIRM_INTERVAL"] = args.interval
    validate_params(cfg)

    interval = cfg["CONFIRM_INTERVAL"]
    warmup = bars_per_day(interval)
    total_bars = warmup + bars_per_day(interval) * args.days

    print(f"Mengambil {total_bars} candle {interval} untuk {args.symbol} "
          f"({args.days} hari + warmup 1 hari)...")
    client = BinanceSpotClient("", "", cfg["LIVE_BASE_URL"], allow_signed=False)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (args.days + 1) * MS_PER_DAY
    klines = fetch_full_klines(client, args.symbol, interval, start_ms, end_ms)
    print(f"Dapat {len(klines)} candle.")

    if len(klines) < warmup + 50:
        raise BacktestError(f"Data terlalu sedikit ({len(klines)} candle) untuk backtest yang berarti.")

    daily_raw = client.get_klines(args.symbol, interval="1d", limit=1000,
                                  start_time_ms=start_ms - 8 * MS_PER_DAY,
                                  end_time_ms=end_ms)
    daily_klines = strategy.parse_klines(daily_raw)
    print(f"Dapat {len(daily_klines)} candle harian untuk gerbang pump.")

    result = run_backtest(klines, cfg, warmup, daily_klines=daily_klines)
    print_single_result(result)


if __name__ == "__main__":
    main()
