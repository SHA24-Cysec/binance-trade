#!/usr/bin/env python3
"""
Backtest PORTOFOLIO untuk Pump Scanner Bot.
============================================

Bedanya dengan backtest.py (satu simbol):

    backtest.py       menjawab "KALAU bot kebetulan hanya memantau simbol ini,
                      bagaimana hasil parameter exit saya?"

    modul ini         menjawab "kalau bot memindai SELURUH pasar seperti
                      aslinya, simbol mana yang benar-benar akan dia pilih,
                      dan berapa hasilnya?"

Perbedaan itu penting karena bot asli hanya memegang SATU posisi. Ketika
satu simbol memberi sinyal, bot mungkin sedang sibuk memegang simbol lain,
atau memilih simbol lain yang peringkat kenaikan 24 jamnya lebih tinggi.
Backtest satu simbol menghitung SEMUA sinyal sebagai trade, sehingga hasilnya
hampir selalu lebih optimistis daripada yang bisa dicapai bot sungguhan.


CARA KERJA
----------
1. Ambil daftar pair dari ticker 24 jam, saring persis seperti
   market_scanner.filter_and_rank_candidates (buang stablecoin, token
   leveraged, simbol yang dikecualikan).
2. Unduh candle historis untuk SEMUA simbol yang lolos saringan.
3. Bangun "papan peringkat" per bar waktu: untuk setiap titik waktu,
   urutkan simbol berdasarkan kenaikan 24 jam bergulir yang dihitung dari
   candle (compute_rolling_24h_stats), persis metrik yang dipakai bot live
   untuk me-ranking.
4. Maju bar demi bar melewati garis waktu gabungan:
     - kalau TIDAK punya posisi: ambil top-N peringkat saat itu, konfirmasi
       satu per satu dengan scanner.confirm_entry(), ambil yang PERTAMA
       lolos. Ini meniru find_best_candidate() secara harfiah.
     - kalau PUNYA posisi: kelola exit memakai logika yang sama dengan
       backtest satu simbol (SL/TP/BE/Trailing/MaxHold), DITAMBAH
       MOMENTUM_FADE yang di backtest satu simbol tidak bisa disimulasikan.
5. Hormati cooldown setelah setiap posisi ditutup.

Yang dipakai bersama dengan bot live (BUKAN ditulis ulang):
    market_scanner.confirm_entry()     -> aturan entry
    market_scanner.STABLE_BASE_ASSETS  -> saringan pasar
    strategy.resolve_exit_levels()     -> level SL/TP/BE/Trailing
    backtest.compute_rolling_24h_stats -> statistik 24 jam bergulir
    backtest.fetch_full_klines()       -> pengambilan candle
Menyalin logika ini akan menciptakan risiko backtest diam-diam menyimpang
dari bot sungguhan, jenis bug yang baru ketahuan setelah kehilangan uang.


KETERBATASAN YANG TETAP ADA (baca sebelum percaya hasilnya)
-----------------------------------------------------------
1. SURVIVORSHIP BIAS, dan ini tidak bisa diperbaiki. Binance hanya
   menyediakan data historis untuk pair yang MASIH listing hari ini. Koin
   yang sudah didelisting -- sering justru yang kolaps setelah pump -- tidak
   ada dalam data. Hasil backtest portofolio karenanya masih cenderung lebih
   baik daripada kenyataan. Ini batas struktural dari sumber datanya, bukan
   sesuatu yang bisa diakali dengan kode.

2. GRANULARITAS CANDLE. Exit dievaluasi per candle (default 5 menit), bukan
   tiap 15 detik seperti loop bot live. Kalau SL dan TP tersentuh di candle
   yang sama, urutan sebenarnya tidak diketahui. Modul ini memakai prioritas
   konservatif yang sama dengan backtest.py: STOP_LOSS diperiksa paling awal,
   sehingga hasil tidak melebih-lebihkan profit.

3. RANKING DIREKONSTRUKSI, BUKAN DIREKAM. Peringkat 24 jam dihitung ulang
   dari candle, bukan diambil dari snapshot ticker/24hr historis (Binance
   tidak menyediakannya). Nilainya sangat dekat tetapi tidak identik dengan
   angka yang dilihat bot pada saat itu, karena ticker/24hr adalah jendela
   bergulir tepat 24 jam sedangkan rekonstruksi ini dibulatkan ke batas
   candle terdekat.

4. VOLUME 24 JAM juga direkonstruksi dari penjumlahan quote volume candle.
   Angkanya bisa sedikit berbeda dari field quoteVolume di ticker.

5. LATENSI DAN SLIPPAGE tidak dimodelkan. Entry dianggap terjadi tepat di
   harga penutupan candle sinyal. Fee taker pulang-pergi SUDAH dipotong.

6. Bot live memindai tiap LOOP_INTERVAL_SECONDS (default 15 detik),
   sedangkan simulasi ini memeriksa peluang entry sekali per bar candle.
   Pada praktiknya perbedaan ini kecil karena sinyal entry memang dihitung
   dari candle tertutup, tetapi bot live bisa menangkap peluang beberapa
   menit lebih cepat.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from strategy import Kline
import strategy
import market_scanner as scanner

from backtest import (
    BacktestError,
    MS_PER_MIN,
    bars_per_day,
    compute_rolling_24h_stats,
    fetch_full_klines,
)


@dataclass
class PortfolioTrade:
    """Satu trade lengkap hasil simulasi portofolio."""
    symbol: str
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    reason: str
    hold_minutes: float
    pnl_pct: float           # sudah dipotong fee pulang-pergi
    gross_pnl_pct: float
    fee_pct: float
    sl_pct: float
    tp_pct: float
    atr_pct: float
    exit_source: str
    rank_at_entry: int       # peringkat simbol saat dipilih (1 = gainer teratas)
    pct24h_at_entry: float
    candidates_at_entry: int  # berapa simbol lolos saringan pada bar itu


@dataclass
class SkippedSignal:
    """Sinyal yang valid tetapi TIDAK bisa diambil bot.

    Inilah bukti konkret kenapa backtest satu simbol terlalu optimistis:
    setiap baris di sini adalah trade yang akan dihitung oleh backtest satu
    simbol, tetapi tidak akan pernah terjadi di bot sungguhan.
    """
    time: int
    symbol: str
    reason: str       # "SEDANG_PEGANG_POSISI_LAIN" atau "KALAH_PERINGKAT"
    holding: str      # simbol yang sedang dipegang saat itu


@dataclass
class PortfolioResult:
    interval: str
    symbols_scanned: int
    symbols_with_data: int
    bars_total: int
    start_time: int
    end_time: int
    trades: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    symbols_failed: list = field(default_factory=list)


# ======================================================================
# Tahap 1: pilih semesta simbol
# ======================================================================

def select_universe(tickers: list, config: dict, max_symbols: Optional[int] = None) -> list[str]:
    """Tentukan simbol mana yang ikut disimulasikan.

    Memakai filter_and_rank_candidates() milik scanner supaya aturan
    penyaringannya identik dengan bot live (stablecoin dibuang, token
    leveraged dibuang, simbol yang dikecualikan dibuang).

    PENTING soal ambang: saringan scanner memakai MIN_PUMP_PCT_24H terhadap
    kondisi pasar HARI INI, padahal kita butuh simbol yang pernah pump KAPAN
    SAJA selama periode backtest. Karena itu ambang pump di-nolkan khusus
    untuk pemilihan semesta -- kalau tidak, kita hanya akan mengunduh koin
    yang kebetulan sedang naik saat backtest dijalankan, yang justru
    menciptakan bias pemilihan baru. Ambang pump yang sebenarnya tetap
    ditegakkan per-bar di dalam simulasi.
    """
    cfg = dict(config)
    cfg["MIN_PUMP_PCT_24H"] = -1e9   # jangan saring berdasarkan kondisi hari ini
    cfg["MIN_QUOTE_VOLUME_USDT_24H"] = float(config.get("MIN_QUOTE_VOLUME_USDT_24H", 0))

    ranked = scanner.filter_and_rank_candidates(tickers, cfg)
    # Urutkan berdasarkan likuiditas, bukan kenaikan hari ini. Kalau daftar
    # harus dipotong, yang dipertahankan adalah pair paling likuid, yang juga
    # paling mungkin lolos filter volume bot pada periode mana pun.
    ranked.sort(key=lambda c: c.quote_volume, reverse=True)
    symbols = [c.symbol for c in ranked]
    if max_symbols is not None and max_symbols > 0:
        symbols = symbols[:max_symbols]
    return symbols


# ======================================================================
# Tahap 2: unduh candle untuk semua simbol
# ======================================================================

def fetch_universe_klines(
    client,
    symbols: list[str],
    interval: str,
    start_ms: int,
    end_ms: int,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    sleep_between_symbols: float = 0.0,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> tuple[dict, list]:
    """Unduh candle untuk setiap simbol. Return (data, gagal).

    Satu simbol yang gagal TIDAK membatalkan seluruh backtest -- simbol itu
    dicatat di daftar gagal lalu dilewati. Dengan ratusan simbol, memaksa
    semuanya berhasil berarti satu koin bermasalah bisa menggagalkan proses
    yang sudah berjalan sepuluh menit.
    """
    data: dict = {}
    failed: list = []
    total = max(1, len(symbols))

    for idx, sym in enumerate(symbols):
        if cancel_cb is not None and cancel_cb():
            raise BacktestError("Backtest dibatalkan.")
        try:
            kl = fetch_full_klines(client, sym, interval, start_ms, end_ms,
                                   sleep_between_calls=0.0)
            if kl:
                data[sym] = kl
            else:
                failed.append({"symbol": sym, "error": "tidak ada data candle pada rentang ini"})
        except Exception as exc:  # noqa: BLE001
            failed.append({"symbol": sym, "error": str(exc)[:160]})

        if progress_cb:
            progress_cb((idx + 1) / total, sym)
        if sleep_between_symbols:
            time.sleep(sleep_between_symbols)

    return data, failed


# ======================================================================
# Tahap 3: bangun papan peringkat per bar
# ======================================================================

def build_timeline(data: dict, interval: str) -> tuple[list, dict, dict]:
    """Siapkan struktur yang bisa ditelusuri per titik waktu.

    Return:
        timeline : daftar open_time unik terurut, gabungan semua simbol
        index_of : {symbol: {open_time: index candle}} untuk pencarian O(1)
        stats_of : {symbol: list stats 24 jam sejajar dengan candle}

    Memakai open_time sebagai kunci (bukan indeks candle) karena tiap simbol
    bisa punya jumlah candle berbeda: koin yang listing belakangan tidak
    punya candle di awal periode, dan sesekali ada celah data.
    """
    window = bars_per_day(interval)
    index_of: dict = {}
    stats_of: dict = {}
    all_times: set = set()

    for sym, kl in data.items():
        index_of[sym] = {k.open_time: i for i, k in enumerate(kl)}
        stats_of[sym] = compute_rolling_24h_stats(kl, window)
        all_times.update(k.open_time for k in kl)

    timeline = sorted(all_times)
    return timeline, index_of, stats_of


# ======================================================================
# Tahap 4: simulasi portofolio
# ======================================================================

def run_portfolio_backtest(
    data: dict,
    config: dict,
    interval: str,
    warmup_ms: int = 0,
    progress_cb: Optional[Callable[[float], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    max_skipped_records: int = 400,
) -> PortfolioResult:
    """Jalankan simulasi satu-posisi melintasi seluruh semesta simbol."""
    if not data:
        raise BacktestError("Tidak ada data candle untuk disimulasikan.")

    timeline, index_of, stats_of = build_timeline(data, interval)
    if not timeline:
        raise BacktestError("Garis waktu kosong, tidak ada candle yang bisa diproses.")

    lookback = int(config["CONFIRM_LOOKBACK_BARS"])
    min_pump = float(config["MIN_PUMP_PCT_24H"])
    min_vol = float(config["MIN_QUOTE_VOLUME_USDT_24H"])
    top_n = int(config.get("TOP_N_CANDIDATES_TO_CONFIRM", 10))
    cooldown_ms = int(config["COOLDOWN_MINUTES_AFTER_CLOSE"]) * MS_PER_MIN
    max_hold = float(config["MAX_HOLD_MINUTES"])
    atr_need = max(int(config.get("ATR_PERIOD", 14)) + 1, lookback)

    fade_on = bool(config.get("MOMENTUM_FADE_EXIT", False))
    fade_rank = int(config.get("MOMENTUM_FADE_RANK_THRESHOLD", 30))

    # confirm_momentum() menolak jendela yang kurang dari 8 candle. Kalau
    # CONFIRM_LOOKBACK_BARS diset di bawah itu, SETIAP konfirmasi entry akan
    # gagal dan backtest menghasilkan nol trade tanpa alasan yang terlihat.
    # Diperiksa di depan supaya penyebabnya jelas, bukan berupa hasil kosong
    # yang membingungkan.
    _pre_warnings: list = []
    if lookback < 8:
        _pre_warnings.append(
            f"CONFIRM_LOOKBACK_BARS={lookback} lebih kecil dari 8 candle minimum yang "
            "dibutuhkan konfirmasi momentum, sehingga tidak akan pernah ada entry. "
            "Naikkan nilainya menjadi 8 atau lebih."
        )

    try:
        from config import get_taker_fee_pct as _fee_fn
        fee_round_trip = _fee_fn(config) * 2.0
    except ImportError:
        fee_round_trip = float(config.get("TAKER_FEE_PCT", 0.1)) * 2.0

    trades: list = []
    skipped: list = []
    warnings: list = list(_pre_warnings)

    # Status posisi
    holding: Optional[str] = None
    entry_price = 0.0
    entry_time = 0
    be_active = False
    be_stop = 0.0
    trailing_active = False
    trailing_stop = 0.0
    cur = {}          # level exit yang dikunci saat entry
    rank_at_entry = 0
    pct24h_at_entry = 0.0
    cands_at_entry = 0
    next_entry_allowed_at = 0

    first_allowed_time = timeline[0] + warmup_ms
    total_bars = len(timeline)

    for bi, t_now in enumerate(timeline):
        if progress_cb and bi % 50 == 0:
            progress_cb(bi / total_bars)
        if cancel_cb is not None and bi % 200 == 0 and cancel_cb():
            raise BacktestError("Backtest dibatalkan.")

        # --- Papan peringkat pada titik waktu ini -------------------
        # Dihitung sekali per bar lalu dipakai bersama oleh jalur entry
        # maupun jalur momentum fade, supaya keduanya melihat pasar yang
        # sama persis seperti bot live yang juga memakai satu snapshot
        # ticker per rotasi.
        board = []
        for sym, kl in data.items():
            i = index_of[sym].get(t_now)
            if i is None:
                continue
            st = stats_of[sym][i]
            if st is None:
                continue
            if st["vol24h"] < min_vol:
                continue
            board.append((st["pct24h"], sym, i))
        board.sort(key=lambda x: -x[0])

        # ============ SUDAH PUNYA POSISI: kelola exit ============
        if holding is not None:
            hi = index_of[holding].get(t_now)
            if hi is None:
                # Celah data pada simbol yang sedang dipegang. Posisi
                # dipertahankan; bar ini dilewati untuk simbol tersebut.
                continue

            candle = data[holding][hi]
            pnl_high = (candle.high / entry_price - 1.0) * 100.0
            pnl_low = (candle.low / entry_price - 1.0) * 100.0
            hold_minutes = (candle.close_time - entry_time) / 60000.0

            sl_price = entry_price * (1 - cur["sl"] / 100.0) if config["USE_STOP_LOSS"] else None

            if config["USE_BREAKEVEN"] and not be_active and pnl_high >= cur["be_trig"]:
                be_active = True
                be_stop = entry_price * (1 + cur["be_lock"] / 100.0)

            if config["USE_TRAILING"]:
                if not trailing_active and pnl_high >= cur["tr_start"]:
                    trailing_active = True
                    trailing_stop = candle.high * (1 - cur["tr_step"] / 100.0)
                elif trailing_active:
                    cand_stop = candle.high * (1 - cur["tr_step"] / 100.0)
                    if cand_stop > trailing_stop:
                        trailing_stop = cand_stop

            exit_reason = None
            exit_price = None

            # Urutan prioritas SENGAJA konservatif dan identik dengan
            # backtest.py: risiko dianggap terealisasi lebih dulu kalau
            # dalam satu candle harga menyentuh SL maupun TP.
            if config["USE_STOP_LOSS"] and pnl_low <= -cur["sl"]:
                exit_reason = "STOP_LOSS"
                exit_price = sl_price
            elif config["USE_TP"] and pnl_high >= cur["tp"]:
                exit_reason = "TAKE_PROFIT"
                exit_price = entry_price * (1 + cur["tp"] / 100.0)
            elif be_active and candle.low <= be_stop:
                exit_reason = "BREAKEVEN"
                exit_price = be_stop
            elif trailing_active and candle.low <= trailing_stop:
                exit_reason = "TRAILING_STOP"
                exit_price = trailing_stop
            elif hold_minutes >= max_hold:
                exit_reason = "MAX_HOLD_TIME"
                exit_price = candle.close
            elif fade_on:
                # MOMENTUM_FADE: keluar kalau simbol tidak lagi masuk
                # top-N gainer. Inilah yang backtest satu simbol TIDAK
                # bisa lakukan, karena butuh peringkat seluruh pasar.
                still = any(sym == holding for _p, sym, _i in board[:fade_rank])
                if not still:
                    exit_reason = "MOMENTUM_FADE"
                    exit_price = candle.close

            is_last = (bi == total_bars - 1)
            if exit_reason is None and is_last:
                exit_reason = "END_OF_DATA"
                exit_price = candle.close
                warnings.append(
                    "Posisi terakhir masih terbuka saat data habis (ditutup paksa di harga "
                    "penutupan terakhir demi kelengkapan statistik, bukan exit sungguhan)."
                )

            if exit_reason:
                gross = (exit_price / entry_price - 1.0) * 100.0
                trades.append(PortfolioTrade(
                    symbol=holding,
                    entry_time=entry_time, entry_price=entry_price,
                    exit_time=candle.close_time, exit_price=exit_price,
                    reason=exit_reason, hold_minutes=hold_minutes,
                    pnl_pct=gross - fee_round_trip, gross_pnl_pct=gross,
                    fee_pct=fee_round_trip,
                    sl_pct=cur["sl"], tp_pct=cur["tp"],
                    atr_pct=cur["atr"], exit_source=cur["src"],
                    rank_at_entry=rank_at_entry, pct24h_at_entry=pct24h_at_entry,
                    candidates_at_entry=cands_at_entry,
                ))
                holding = None
                be_active = False
                trailing_active = False
                next_entry_allowed_at = candle.close_time + cooldown_ms
            continue

        # ============ TIDAK PUNYA POSISI: cari kandidat ============
        if t_now < first_allowed_time or t_now < next_entry_allowed_at:
            continue

        eligible = [(p, s, i) for (p, s, i) in board if p >= min_pump]
        if not eligible:
            continue

        # Meniru find_best_candidate(): periksa top-N berurutan, ambil
        # YANG PERTAMA lolos konfirmasi, lalu berhenti.
        chosen = None
        for rank, (pct, sym, i) in enumerate(eligible[:top_n], start=1):
            kl = data[sym]
            if i + 1 < lookback:
                continue
            window_kl = kl[max(0, i - lookback + 1): i + 1]
            try:
                ok, _reason = scanner.confirm_entry(window_kl, config)
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                chosen = (rank, pct, sym, i)
                break
            # Simbol ini memberi sinyal peringkat lebih tinggi tapi gagal
            # konfirmasi; bukan "terlewat", jadi tidak dicatat.

        if chosen is None:
            continue

        rank, pct, sym, i = chosen

        # Catat sinyal valid lain pada bar yang sama yang TIDAK terambil
        # karena bot hanya boleh pegang satu posisi. Ini yang membuat
        # backtest satu simbol terlihat lebih bagus dari kenyataan.
        if len(skipped) < max_skipped_records:
            for rank2, (pct2, sym2, i2) in enumerate(eligible[:top_n], start=1):
                if sym2 == sym or rank2 <= rank:
                    continue
                kl2 = data[sym2]
                if i2 + 1 < lookback:
                    continue
                try:
                    ok2, _r2 = scanner.confirm_entry(kl2[max(0, i2 - lookback + 1): i2 + 1], config)
                except Exception:  # noqa: BLE001
                    ok2 = False
                if ok2:
                    skipped.append(SkippedSignal(
                        time=t_now, symbol=sym2,
                        reason="KALAH_PERINGKAT", holding=sym,
                    ))
                    if len(skipped) >= max_skipped_records:
                        break

        kl = data[sym]
        candle = kl[i]
        holding = sym
        entry_price = candle.close
        entry_time = candle.close_time
        be_active = False
        trailing_active = False
        be_stop = 0.0
        trailing_stop = 0.0
        rank_at_entry = rank
        pct24h_at_entry = pct
        cands_at_entry = len(eligible)

        # Level exit dikunci memakai fungsi yang SAMA dengan bot live.
        # ATR hanya dari candle sampai bar entry, tidak pernah dari masa
        # depan, supaya tidak ada look-ahead bias.
        atr_window = kl[max(0, i - atr_need + 1): i + 1]
        lv = strategy.resolve_exit_levels(config, atr_window, entry_price)
        cur = {
            "sl": lv["sl_pct"], "tp": lv["tp_pct"],
            "be_trig": lv["be_trigger_pct"], "be_lock": lv["be_lock_pct"],
            "tr_start": lv["trail_start_pct"], "tr_step": lv["trail_step_pct"],
            "atr": lv["atr_pct"] or 0.0, "src": lv["source"],
        }

    if progress_cb:
        progress_cb(1.0)

    return PortfolioResult(
        interval=interval,
        symbols_scanned=len(data),
        symbols_with_data=len(data),
        bars_total=total_bars,
        start_time=timeline[0],
        end_time=timeline[-1],
        trades=trades,
        skipped=skipped,
        warnings=list(dict.fromkeys(warnings)),   # buang duplikat, jaga urutan
    )


# ======================================================================
# Ringkasan
# ======================================================================

def summarize_portfolio(result: PortfolioResult) -> dict:
    """Statistik hasil simulasi portofolio.

    Return dibuat mirip backtest.summarize() supaya dashboard bisa memakai
    komponen tampilan yang sama, ditambah beberapa metrik khas portofolio.
    """
    trades = result.trades
    total = len(trades)
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]

    equity_curve = [0.0]
    mult = 1.0
    gross_mult = 1.0
    for t in trades:
        mult *= (1 + t.pnl_pct / 100.0)
        gross_mult *= (1 + t.gross_pnl_pct / 100.0)
        equity_curve.append((mult - 1.0) * 100.0)

    peak = -1e18
    max_dd = 0.0
    for v in equity_curve:
        level = 1 + v / 100.0
        if level > peak:
            peak = level
        dd = (peak - level) / peak * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    gross_win = sum(t.pnl_pct for t in wins)
    gross_loss = abs(sum(t.pnl_pct for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0)

    reason_counts: dict = {}
    for t in trades:
        reason_counts[t.reason] = reason_counts.get(t.reason, 0) + 1

    symbol_counts: dict = {}
    symbol_pnl: dict = {}
    for t in trades:
        symbol_counts[t.symbol] = symbol_counts.get(t.symbol, 0) + 1
        symbol_pnl[t.symbol] = symbol_pnl.get(t.symbol, 0.0) + t.pnl_pct
    top_symbols = sorted(symbol_pnl.items(), key=lambda kv: kv[1], reverse=True)

    span_ms = max(1, result.end_time - result.start_time)
    span_days = span_ms / (24 * 60 * 60 * 1000)
    total_hold = sum(t.hold_minutes for t in trades)
    exposure = (total_hold / (span_days * 24 * 60) * 100.0) if span_days > 0 else 0.0

    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / total * 100.0) if total else 0.0,
        "total_return_pct": (mult - 1.0) * 100.0,
        "gross_return_pct": (gross_mult - 1.0) * 100.0,
        "fee_drag_pct": (gross_mult - mult) * 100.0,
        "total_fee_pct": sum(t.fee_pct for t in trades),
        "max_drawdown_pct": max_dd,
        "avg_win_pct": (sum(t.pnl_pct for t in wins) / len(wins)) if wins else 0.0,
        "avg_loss_pct": (sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0.0,
        "profit_factor": profit_factor,
        "avg_hold_minutes": (total_hold / total) if total else 0.0,
        "reason_counts": reason_counts,
        "equity_curve": equity_curve,
        # --- khas portofolio ---
        "unique_symbols": len(symbol_counts),
        "symbols_in_universe": result.symbols_with_data,
        "top_symbols": [{"symbol": s, "pnl_pct": p, "trades": symbol_counts[s]}
                        for s, p in top_symbols[:10]],
        "worst_symbols": [{"symbol": s, "pnl_pct": p, "trades": symbol_counts[s]}
                          for s, p in top_symbols[-5:][::-1] if p < 0],
        "skipped_signals": len(result.skipped),
        "avg_rank_at_entry": (sum(t.rank_at_entry for t in trades) / total) if total else 0.0,
        "exposure_pct": exposure,
        "span_days": span_days,
        "trades_per_day": (total / span_days) if span_days > 0 else 0.0,
    }


# ======================================================================
# Selftest
# ======================================================================

def _mk(t, o, h, l, c, qv=5_000_000.0):
    """Buat candle 5 menit. t dalam indeks bar, bukan milidetik."""
    ms = t * 5 * MS_PER_MIN
    return Kline(open_time=ms, open=o, high=h, low=l, close=c,
                 close_time=ms + 5 * MS_PER_MIN - 1, volume=1000.0, quote_volume=qv)


def selftest() -> bool:
    """Uji mandiri tanpa jaringan. Return True kalau semua lolos."""
    ok_all = True

    def check(name, cond, extra=""):
        nonlocal ok_all
        if not cond:
            ok_all = False
        print(("  LULUS " if cond else "  GAGAL ") + name + (("  -> " + str(extra)) if extra else ""))

    print("Selftest portfolio_backtest")

    # --- build_timeline menggabungkan waktu dari simbol berbeda ---
    a = [_mk(i, 100, 101, 99, 100) for i in range(5)]
    b = [_mk(i, 50, 51, 49, 50) for i in range(3, 9)]
    tl, idx, _st = build_timeline({"AUSDT": a, "BUSDT": b}, "5m")
    check("timeline gabungan unik & terurut", tl == sorted(set(tl)) and len(tl) == 9, len(tl))
    check("index_of memetakan waktu ke posisi", idx["BUSDT"][b[0].open_time] == 0)

    # --- satu posisi saja pada satu waktu ---
    cfg = {
        "CONFIRM_LOOKBACK_BARS": 10, "MIN_PUMP_PCT_24H": 5.0,
        # Nama kunci yang BENAR (perbaikan audit temuan R-03): sebelumnya
        # tertulis "CONFIRM_MIN_CLOSE_POSITION" -- kunci yang tidak pernah
        # dibaca scanner -- sehingga relaksasi gerbang ini tidak pernah
        # berlaku dan tes diam-diam memakai default 0.35.
        "MIN_CLOSE_POSITION_IN_RANGE": 0.0,
        "MIN_QUOTE_VOLUME_USDT_24H": 0, "TOP_N_CANDIDATES_TO_CONFIRM": 10,
        "COOLDOWN_MINUTES_AFTER_CLOSE": 0, "MAX_HOLD_MINUTES": 10_000,
        "USE_STOP_LOSS": True, "USE_TP": True, "USE_BREAKEVEN": False,
        "USE_TRAILING": False, "SL_PCT": 2.0, "TP_PCT": 3.0,
        "BE_TRIGGER_PCT": 1.0, "BE_LOCK_PCT": 0.1,
        "TRAILING_START_PCT": 1.5, "TRAILING_STEP_PCT": 0.6,
        "USE_ATR_EXITS": False, "ATR_PERIOD": 14, "TAKER_FEE_PCT": 0.1,
        "MOMENTUM_FADE_EXIT": False, "QUOTE_ASSET": "USDT",
    }

    # Dua simbol naik bersamaan. Bot hanya boleh memegang satu.
    # 288 bar = jendela 24 jam. Perlu jauh lebih banyak supaya ada
    # cukup bar SESUDAH warmup untuk benar-benar menghasilkan trade.
    bars = 700
    up_a, up_b = [], []
    price_a, price_b = 100.0, 200.0
    for i in range(bars):
        # naik perlahan supaya stats 24h positif dan momentum terkonfirmasi
        price_a *= 1.002
        price_b *= 1.0015
        up_a.append(_mk(i, price_a, price_a * 1.003, price_a * 0.999, price_a * 1.002))
        up_b.append(_mk(i, price_b, price_b * 1.003, price_b * 0.999, price_b * 1.002))

    res = run_portfolio_backtest({"AUSDT": up_a, "BUSDT": up_b}, cfg, "5m")
    overlaps = 0
    for i in range(len(res.trades)):
        for j in range(i + 1, len(res.trades)):
            t1, t2 = res.trades[i], res.trades[j]
            if t1.entry_time < t2.exit_time and t2.entry_time < t1.exit_time:
                overlaps += 1
    check("tidak pernah dua posisi bersamaan", overlaps == 0, f"{overlaps} tumpang tindih")
    check("menghasilkan trade", len(res.trades) > 0, len(res.trades))

    # --- fee benar-benar dipotong ---
    if res.trades:
        t = res.trades[0]
        check("fee pulang-pergi dipotong",
              abs((t.gross_pnl_pct - t.pnl_pct) - 0.2) < 1e-9,
              f"selisih {t.gross_pnl_pct - t.pnl_pct:.4f}")

    # --- cooldown dihormati ---
    cfg_cd = dict(cfg)
    cfg_cd["COOLDOWN_MINUTES_AFTER_CLOSE"] = 60
    res_cd = run_portfolio_backtest({"AUSDT": up_a, "BUSDT": up_b}, cfg_cd, "5m")
    viol = 0
    for i in range(1, len(res_cd.trades)):
        gap_min = (res_cd.trades[i].entry_time - res_cd.trades[i - 1].exit_time) / 60000.0
        if gap_min < 59.9:
            viol += 1
    check("cooldown dihormati", viol == 0, f"{viol} pelanggaran")
    check("cooldown mengurangi jumlah trade",
          len(res_cd.trades) <= len(res.trades),
          f"{len(res_cd.trades)} vs {len(res.trades)}")

    # --- prioritas konservatif: SL menang atas TP di candle yang sama ---
    # CATATAN: versi awal tes ini memakai rangkaian ~300 bar, padahal
    # warmup statistik 24 jam saja sudah memakan 288 bar. Akibatnya nol
    # trade terbentuk dan assertion "atau tidak ada trade" membuat tes
    # LULUS TANPA MENGUJI APA PUN. Sekarang dipakai basis 700 bar naik
    # yang sudah terbukti menghasilkan entry, lalu satu candle ekstrem
    # disisipkan, dan tes menuntut trade benar-benar ada.
    spike_at = 600
    seq_sl = []
    p_sl = 100.0
    for i in range(bars):
        if i == spike_at:
            # Satu candle yang menyentuh TP (+3%) DAN SL (-2%) sekaligus.
            # Rentangnya sengaja dibuat lebar (-12% s/d +12%) supaya kedua
            # level pasti terlampaui berapa pun harga entry persisnya.
            # Versi sebelumnya memakai -3%/+5% dan ternyata TIDAK menyentuh
            # SL karena entry terjadi beberapa bar lebih awal pada harga
            # yang lebih rendah, sehingga tes menuduh mesin keliru padahal
            # mesinnya benar.
            seq_sl.append(_mk(i, p_sl, p_sl * 1.12, p_sl * 0.88, p_sl))
            continue
        p_sl *= 1.002
        seq_sl.append(_mk(i, p_sl, p_sl * 1.003, p_sl * 0.999, p_sl * 1.002))

    res_sl = run_portfolio_backtest({"AUSDT": seq_sl}, cfg, "5m")
    check("skenario SL-vs-TP benar-benar menghasilkan trade (tes tidak vakum)",
          len(res_sl.trades) > 0, len(res_sl.trades))
    # Trade yang MELEWATI candle ekstrem wajib keluar sebagai STOP_LOSS,
    # bukan TAKE_PROFIT. Mengasumsikan yang terbaik dari satu candle
    # adalah cara paling umum membuat backtest terlihat palsu bagus.
    spanning = [t for t in res_sl.trades
                if t.entry_time <= seq_sl[spike_at].open_time <= t.exit_time]
    if spanning:
        check("SL diprioritaskan saat SL & TP kena di satu candle",
              all(t.reason == "STOP_LOSS" for t in spanning),
              [t.reason for t in spanning][:4])
    else:
        check("SL diprioritaskan saat SL & TP kena di satu candle",
              False, "tidak ada trade yang melewati candle ekstrem")

    # --- momentum fade aktif memicu exit ---
    # CATATAN: versi awal tes ini juga vakum ("... or len(trades) == 0").
    # Candle-nya dibuat dengan close == open sehingga tidak pernah dianggap
    # bullish dan nol trade terbentuk, jadi tesnya selalu hijau tanpa
    # pernah menyentuh jalur MOMENTUM_FADE sama sekali.
    #
    # Sekarang: SL/TP sengaja dimatikan supaya posisi tertahan cukup lama
    # untuk bisa memudar. A memimpin di paruh pertama lalu melempem,
    # B menyusul kencang, sehingga A wajib dilepas karena kalah peringkat.
    cfg_fade = dict(cfg)
    cfg_fade["MOMENTUM_FADE_EXIT"] = True
    cfg_fade["MOMENTUM_FADE_RANK_THRESHOLD"] = 1
    cfg_fade["USE_STOP_LOSS"] = False
    cfg_fade["USE_TP"] = False
    a2, b2 = [], []
    pa, pb = 100.0, 100.0
    for i in range(bars):
        pa *= 1.006 if i < 400 else 1.0001   # dulu juara, lalu melempem
        pb *= 1.001 if i < 400 else 1.008    # menyusul kencang
        a2.append(_mk(i, pa, pa * 1.003, pa * 0.999, pa * 1.002))
        b2.append(_mk(i, pb, pb * 1.003, pb * 0.999, pb * 1.002))
    res_fade = run_portfolio_backtest({"AUSDT": a2, "BUSDT": b2}, cfg_fade, "5m")
    reasons = [t.reason for t in res_fade.trades]
    check("skenario fade benar-benar menghasilkan trade (tes tidak vakum)",
          len(res_fade.trades) > 0, len(res_fade.trades))
    check("MOMENTUM_FADE benar-benar terpicu", "MOMENTUM_FADE" in reasons, reasons)
    # Setelah melepas yang memudar, modal harus berotasi ke simbol lain.
    check("setelah fade, modal berotasi ke simbol lebih kuat",
          len({t.symbol for t in res_fade.trades}) > 1,
          sorted({t.symbol for t in res_fade.trades}))

    # --- filter volume menyingkirkan simbol ilikuid ---
    cfg_vol = dict(cfg)
    cfg_vol["MIN_QUOTE_VOLUME_USDT_24H"] = 1e15   # tak ada yang lolos
    res_vol = run_portfolio_backtest({"AUSDT": up_a}, cfg_vol, "5m")
    check("filter volume menyaring semua", len(res_vol.trades) == 0, len(res_vol.trades))
    # Kontrol positif: data yang sama tanpa ambang volume mustahil harus
    # tetap menghasilkan trade. Tanpa ini, tes di atas ikut hijau kalau
    # mesinnya rusak dan tidak pernah membuka posisi sama sekali.
    check("kontrol positif: data sama tanpa ambang volume tetap menghasilkan trade",
          len(run_portfolio_backtest({"AUSDT": up_a}, cfg, "5m").trades) > 0)

    # --- ringkasan konsisten ---
    s = summarize_portfolio(res)
    check("total = menang + kalah", s["total_trades"] == s["wins"] + s["losses"])
    check("kurva equity panjangnya benar", len(s["equity_curve"]) == s["total_trades"] + 1)
    check("drawdown tidak negatif", s["max_drawdown_pct"] >= 0, s["max_drawdown_pct"])
    check("return kotor >= bersih",
          s["gross_return_pct"] >= s["total_return_pct"] - 1e-9,
          f'{s["gross_return_pct"]:.3f} vs {s["total_return_pct"]:.3f}')
    check("eksposur masuk akal (0-100%)", 0 <= s["exposure_pct"] <= 100, s["exposure_pct"])

    # --- select_universe menyaring stablecoin & token leveraged ---
    tickers = [
        {"symbol": "BTCUSDT", "priceChangePercent": "1.0", "quoteVolume": "9e9", "lastPrice": "60000"},
        {"symbol": "USDCUSDT", "priceChangePercent": "0.0", "quoteVolume": "9e9", "lastPrice": "1"},
        {"symbol": "BTCUPUSDT", "priceChangePercent": "5.0", "quoteVolume": "9e9", "lastPrice": "10"},
        {"symbol": "ETHBTC", "priceChangePercent": "1.0", "quoteVolume": "9e9", "lastPrice": "0.05"},
        {"symbol": "SOLUSDT", "priceChangePercent": "-3.0", "quoteVolume": "5e8", "lastPrice": "200"},
    ]
    uni = select_universe(tickers, {"QUOTE_ASSET": "USDT", "MIN_PUMP_PCT_24H": 8.0,
                                    "MIN_QUOTE_VOLUME_USDT_24H": 0,
                                    "EXTRA_EXCLUDE_SYMBOLS": []})
    check("semesta buang stablecoin/leveraged/non-USDT",
          set(uni) == {"BTCUSDT", "SOLUSDT"}, uni)
    check("semesta TIDAK tersaring oleh pump hari ini (SOL turun tetap masuk)",
          "SOLUSDT" in uni)

    print("\nHASIL: " + ("SEMUA LULUS" if ok_all else "ADA YANG GAGAL"))
    return ok_all


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
