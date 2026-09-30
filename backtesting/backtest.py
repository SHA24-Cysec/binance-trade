#!/usr/bin/env python3
"""
Backtest data dan ringkasan tanpa pembukaan posisi.

Backtest hanya memuat, memvalidasi, dan merangkum data historis. Ia tidak
memiliki generator pembukaan posisi atau simulasi BUY.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from strategy.indicators import Kline
from strategy import indicators as strategy

MS_PER_MIN = 60_000
MS_PER_DAY = 24 * 60 * MS_PER_MIN

# Satu sumber kebenaran ada di strategy.py supaya bot live, backtest satu
# simbol, dan backtest portofolio tidak pernah memakai tabel yang berbeda.
INTERVAL_MINUTES = strategy.INTERVAL_MINUTES


class BacktestError(Exception):
    pass


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
    from strategy.indicators import parse_klines

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
    """Kembalikan backtest kosong karena pembukaan posisi baru dinonaktifkan.

    Fungsi dan format hasil dipertahankan agar dashboard serta pemanggil lama
    tetap stabil. Tidak ada gerbang pembukaan, indikator, atau simulasi BUY yang
    dijalankan. Posisi historis tidak diciptakan oleh backtest baru ini.
    """
    interval = str(config.get("MARKET_DATA_INTERVAL", "5m"))
    initial_equity = initial_backtest_equity(config)
    if progress_cb:
        progress_cb(1.0)
    return BacktestResult(
        symbol=str(config.get("_symbol", "?")),
        interval=interval,
        bars_total=len(klines),
        bars_usable=0,
        start_time=klines[0].open_time if klines else 0,
        end_time=klines[-1].close_time if klines else 0,
        trades=[],
        params=config,
        warnings=["Pembukaan posisi dinonaktifkan; backtest tidak membuat trade baru."],
        initial_equity=initial_equity,
        final_equity=initial_equity,
    )

def summarize(result: BacktestResult) -> dict:
    """Ringkas backtest data-only dengan invariant tanpa trade."""
    initial = result.initial_equity or initial_backtest_equity(result.params)
    final = result.final_equity if result.final_equity else initial
    return {
        "total_trades": 0,
        "real_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "total_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "avg_win_pct": 0.0,
        "avg_loss_pct": 0.0,
        "profit_factor": 0.0,
        "avg_hold_minutes": 0.0,
        "reason_counts": {},
        "equity_curve": [0.0],
        "initial_equity": initial,
        "final_equity": final,
        "total_pnl_quote": 0.0,
        "gross_return_pct": 0.0,
        "fee_drag_pct": 0.0,
        "total_fee_pct": 0.0,
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
        "USE_ATR_EXIT": _as_bool,
        "ATR_PERIOD": int,
        "ATR_MULT_SL": float,
        "ATR_MULT_TP": float,
        "ATR_MULT_BE_TRIGGER": float,
        "ATR_MULT_BE_LOCK": float,
        "ATR_MULT_TRAIL_START": float,
        "ATR_MULT_TRAIL": float,
        "SL_PCT": float,
        "TP_PCT": float,
        "BE_TRIGGER_PCT": float,
        "BE_LOCK_PCT": float,
        "TRAILING_START_PCT": float,
        "TRAILING_STEP_PCT": float,
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
    if bool(cfg.get("USE_ATR_EXIT", False)):
        checks = [
            ("ATR_PERIOD", 1, 1000),
            ("ATR_MULT_SL", 0.0001, 1000),
            ("ATR_MULT_TP", 0.0001, 1000),
            ("ATR_MULT_BE_TRIGGER", 0.0001, 1000),
            ("ATR_MULT_BE_LOCK", 0.0, 1000),
            ("ATR_MULT_TRAIL_START", 0.0001, 1000),
            ("ATR_MULT_TRAIL", 0.0001, 1000),
        ]
    else:
        checks = [
            ("SL_PCT", 0.01, 1000),
            ("TP_PCT", 0.01, 1000),
            ("BE_TRIGGER_PCT", 0.01, 1000),
            ("BE_LOCK_PCT", -100, 1000),
            ("TRAILING_START_PCT", 0.01, 1000),
            ("TRAILING_STEP_PCT", 0.01, 1000),
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

    print("\n=== SELFTEST backtest.py: tidak ada pembukaan posisi ===")
    from config.config import PUMP_CONFIG
    cfg = dict(PUMP_CONFIG)
    cfg["_symbol"] = "TESTUSDT"
    result = run_backtest(klines, cfg, warmup_bars=0)
    assert result.trades == []
    assert result.final_equity == initial_backtest_equity(cfg)
    assert any("tidak membuat trade baru" in w for w in result.warnings)
    print("  -> OK (backtest tidak membuat trade)")

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

    print("\n=== SELFTEST backtest.py: exit tetap tersedia ===")
    assert callable(strategy.resolve_exit_levels)
    print("  -> OK (fungsi exit tetap tersedia untuk posisi yang sudah ada)")

    print("\n=== SELFTEST backtest.py: apply_overrides, validate_params, dan exit tetap ===")
    merged = apply_overrides(dict(PUMP_CONFIG), {"USE_ATR_EXIT": "false", "TP_PCT": "8.5", "SL_PCT": "4.5"})
    assert merged["TP_PCT"] == 8.5 and merged["SL_PCT"] == 4.5
    validate_params(merged)
    try:
        validate_params(apply_overrides(dict(PUMP_CONFIG), {"USE_ATR_EXIT": "false", "TP_PCT": "-5"}))
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

    parser = _argparse.ArgumentParser(description="Backtest data pasar")
    parser.add_argument("--selftest", action="store_true",
                         help="Jalankan audit logika lokal tanpa jaringan lalu keluar.")
    parser.add_argument("--symbol", default="BTCUSDT", help="Simbol, mis. SOLUSDT")
    parser.add_argument("--days", type=int, default=30, help="Jumlah hari data historis")
    parser.add_argument("--interval", default=None, help="Interval candle, default dari config")
    args = parser.parse_args()

    if args.selftest or len(_sys.argv) == 1:
        selftest()
        return

    from config.config import PUMP_CONFIG
    from trading.clients.binance_client import BinanceSpotClient

    cfg = copy.deepcopy(PUMP_CONFIG)
    cfg["_symbol"] = args.symbol
    if args.interval:
        cfg["MARKET_DATA_INTERVAL"] = args.interval
    validate_params(cfg)

    interval = cfg["MARKET_DATA_INTERVAL"]
    warmup = bars_per_day(interval)
    total_bars = warmup + bars_per_day(interval) * args.days

    print(f"Mengambil {total_bars} candle {interval} untuk {args.symbol} "
          f"({args.days} hari + warmup 1 hari)...")
    client = BinanceSpotClient(
        "", "", cfg["LIVE_BASE_URL"], allow_signed=False,
        rate_limit_state_file=cfg.get("RATE_LIMIT_STATE_FILE"),
        rate_limit_limit=int(cfg.get("RATE_LIMIT_WEIGHT_LIMIT", 6000) or 6000),
        rate_limit_safety_margin=int(cfg.get("RATE_LIMIT_SAFETY_MARGIN", 100) or 100),
    )
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
