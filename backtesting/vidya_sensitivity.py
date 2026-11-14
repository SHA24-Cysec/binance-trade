from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import gc
import hashlib
import io
import json
import math
import os
import tempfile
import time
import urllib.request
import zipfile
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone

from config.config import PUMP_CONFIG
from strategy import entry_rule, vidya
from strategy import indicators as strategy
from strategy.indicators import Kline
from market import market_scanner as scanner
from backtesting import portfolio_backtest as pb
from backtesting import vidya_fast
from backtesting.backtest import bars_per_day, compute_rolling_24h_stats
from backtesting.backtest_storage import KlineStore

ROOT = "/home/user/.cache/vidya_data"
OOS_ROOT = "/home/user/.cache/vidya_oos"
IS_STORE = os.path.join(ROOT, "stores", "15m.sqlite3")
OOS_STORE_DIR = os.path.join(OOS_ROOT, "stores")
OOS_ZIP_DIR = os.path.join(OOS_ROOT, "zips")
UNIVERSE_FILE = "/home/user/universe.txt"
IS_JSON = "/home/user/vidya_sensitivity_is.json"
MANIFEST_JSON = "/home/user/vidya_sensitivity_manifest.json"
OOS_JSON = "/home/user/vidya_sensitivity_oos.json"
PREV_PULLBACK_JSON = "/home/user/vidya_backtest_results.json"
INTERVAL = "15m"
STEP_MS = 15 * 60 * 1000
SYMBOLS = [s.strip() for s in open(UNIVERSE_FILE).read().split() if s.strip()]


def log(msg):
    print(msg, flush=True)


def utc_ms(year, month, day=1):
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1000)


IS_WARMUP_START = utc_ms(2026, 4, 1)
IS_START = utc_ms(2026, 5, 1)
IS_END = utc_ms(2026, 11, 1)
OOS_WARMUP_START = utc_ms(2025, 10, 1)
OOS_START = utc_ms(2025, 11, 1)
OOS_END = utc_ms(2026, 5, 1)
OOS_MONTHS = ["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04"]
IS_MONTHS = ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
OOS_MONTH_LABELS = ["2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04"]

BASE_OVERRIDES = {
    "ENTRY_FILTER_MODE": "VIDYA",
    "VIDYA_ENTRY_TRIGGER": "PULLBACK",
    "CONFIRM_INTERVAL": INTERVAL,
    "CONFIRM_LOOKBACK_BARS": 997,
    "BACKTEST_SYMBOL_CACHE_SIZE": 2,
}

CONFIGS = [
    {"id": "base", "label": "Baseline 15m PULLBACK", "family": "base", "overrides": {}},
    {"id": "pb10", "label": "Pullback maks 10 bar", "family": "pullback_max_bars", "overrides": {"VIDYA_PULLBACK_MAX_BARS": 10}},
    {"id": "pb50", "label": "Pullback maks 50 bar", "family": "pullback_max_bars", "overrides": {"VIDYA_PULLBACK_MAX_BARS": 50}},
    {"id": "target_band", "label": "Target pullback lower band", "family": "pullback_target", "overrides": {"VIDYA_PULLBACK_TARGET": "LOWER_BAND"}},
    {"id": "touch00", "label": "Toleransi sentuh 0 ATR", "family": "pullback_touch_atr", "overrides": {"VIDYA_PULLBACK_TOUCH_ATR": 0.0}},
    {"id": "touch05", "label": "Toleransi sentuh 0.5 ATR", "family": "pullback_touch_atr", "overrides": {"VIDYA_PULLBACK_TOUCH_ATR": 0.5}},
    {"id": "anchor_signal", "label": "SL anchor signal low", "family": "sl_anchor", "overrides": {"VIDYA_SL_ANCHOR": "SIGNAL_LOW"}},
    {"id": "anchor_vidya", "label": "SL anchor garis VIDYA", "family": "sl_anchor", "overrides": {"VIDYA_SL_ANCHOR": "VIDYA_LINE"}},
    {"id": "rr15", "label": "R:R 1:1.5", "family": "rr", "overrides": {"ENTRY_MIN_RR_RATIO": 1.5}},
    {"id": "rr20", "label": "R:R 1:2.0", "family": "rr", "overrides": {"ENTRY_MIN_RR_RATIO": 2.0}},
    {"id": "rr30", "label": "R:R 1:3.0", "family": "rr", "overrides": {"ENTRY_MIN_RR_RATIO": 3.0}},
    {"id": "no_bull", "label": "Reclaim tidak wajib bullish", "family": "require_bullish", "overrides": {"VIDYA_PULLBACK_REQUIRE_BULLISH": False}},
    {"id": "hold12", "label": "Max hold 12 jam", "family": "max_hold", "overrides": {"EXIT_MAX_HOLD_HOURS": 12.0}},
    {"id": "hold48", "label": "Max hold 48 jam", "family": "max_hold", "overrides": {"EXIT_MAX_HOLD_HOURS": 48.0}},
    {"id": "buffer1", "label": "Buffer SL 1 x ATR", "family": "sl_buffer", "overrides": {"ENTRY_SL_ATR_BUFFER_MULT": 1.0}},
    {"id": "buffer3", "label": "Buffer SL 3 x ATR", "family": "sl_buffer", "overrides": {"ENTRY_SL_ATR_BUFFER_MULT": 3.0}},
    {"id": "delta10", "label": "Delta volume minimum 10%", "family": "delta_min", "overrides": {"VIDYA_DELTA_MIN_RATIO": 10.0}},
    {"id": "delta20", "label": "Delta volume minimum 20%", "family": "delta_min", "overrides": {"VIDYA_DELTA_MIN_RATIO": 20.0}},
]


def make_config(entry):
    cfg = deepcopy(PUMP_CONFIG)
    cfg.update(BASE_OVERRIDES)
    cfg.update(entry.get("overrides", {}))
    return cfg


def cost_stress_entry(entry):
    return {
        "id": entry["id"] + "_cost2x",
        "label": entry["label"] + " spread/slippage 2x",
        "family": entry.get("family", "base"),
        "overrides": dict(entry.get("overrides", {}), BACKTEST_ENTRY_SPREAD_PCT=0.20, BACKTEST_SLIPPAGE_PCT=0.10),
        "stress_of": entry["id"],
    }


def fetch_bytes(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "binance-trade-backtest/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def download_one(sym, month):
    os.makedirs(OOS_ZIP_DIR, exist_ok=True)
    name = f"{sym}-1m-{month}"
    path = os.path.join(OOS_ZIP_DIR, name + ".zip")
    chk_path = path + ".CHECKSUM"
    base = f"https://data.binance.vision/data/spot/monthly/klines/{sym}/1m/{name}"
    if os.path.exists(path) and os.path.getsize(path) > 0 and os.path.exists(chk_path):
        expected = open(chk_path).read().split()[0]
        if hashlib.sha256(open(path, "rb").read()).hexdigest() == expected:
            return sym, month, True, "cached"
    last = "unreachable"
    for attempt in range(4):
        tmp = None
        try:
            raw = fetch_bytes(base + ".zip")
            expected = fetch_bytes(base + ".zip.CHECKSUM", timeout=30).decode().split()[0]
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("checksum mismatch")
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                if z.testzip() is not None:
                    raise ValueError("zip corrupt")
            fd, tmp = tempfile.mkstemp(prefix=name + ".", suffix=".tmp", dir=OOS_ZIP_DIR)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            os.replace(tmp, path)
            with open(chk_path, "w") as f:
                f.write(expected + "  " + name + ".zip\n")
            return sym, month, True, "ok"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1))
    return sym, month, False, last


def download_oos():
    jobs = [(s, m) for s in SYMBOLS for m in OOS_MONTHS]
    done = 0
    fails = []
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for sym, month, ok, info in ex.map(lambda a: download_one(*a), jobs):
            done += 1
            if not ok:
                fails.append({"symbol": sym, "month": month, "error": info})
            if done % 50 == 0 or done == len(jobs):
                log(f"download OOS {done}/{len(jobs)} fails={len(fails)}")
    return fails


def _ms(v):
    v = int(v)
    return v // 1000 if v > 10**14 else v


def load_symbol_oos(sym):
    bars = []
    seen = set()
    duplicate = 0
    invalid = 0
    for month in OOS_MONTHS:
        path = os.path.join(OOS_ZIP_DIR, f"{sym}-1m-{month}.zip")
        if not os.path.exists(path):
            continue
        with zipfile.ZipFile(path) as z:
            with z.open(z.namelist()[0]) as f:
                for row in csv.reader(io.TextIOWrapper(f, "utf-8")):
                    if not row or not row[0].strip().lstrip("-").isdigit():
                        continue
                    try:
                        ot = _ms(row[0])
                        if ot < OOS_WARMUP_START or ot >= OOS_END:
                            continue
                        o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
                        v = float(row[5])
                        ct = _ms(row[6])
                        qv = float(row[7])
                    except (ValueError, IndexError):
                        invalid += 1
                        continue
                    if not all(math.isfinite(x) for x in (o, h, l, c, v, qv)):
                        invalid += 1
                        continue
                    if c <= 0 or o <= 0 or h < l or h < max(o, c) or l > min(o, c) or v < 0 or qv < 0 or ct <= ot:
                        invalid += 1
                        continue
                    if ot in seen:
                        duplicate += 1
                        continue
                    seen.add(ot)
                    bars.append(Kline(ot, o, h, l, c, ct, v, qv))
    bars.sort(key=lambda k: k.open_time)
    expected = (OOS_END - OOS_WARMUP_START) // 60000
    missing = max(0, expected - len(bars))
    gaps = sum(1 for a, b in zip(bars, bars[1:]) if b.open_time - a.open_time > 60000)
    return bars, {"symbol": sym, "bars_1m": len(bars), "missing_minutes": missing, "gap_events": gaps, "duplicate_rows": duplicate, "invalid_rows": invalid}


def resample_complete(bars, minutes=15):
    step = minutes * 60000
    out = []
    incomplete = 0
    bucket = None
    count = 0
    o = h = l = c = v = qv = 0.0
    for k in bars:
        b = k.open_time // step
        if b != bucket:
            if bucket is not None:
                if count == minutes:
                    ot = bucket * step
                    out.append(Kline(ot, o, h, l, c, ot + step - 1, v, qv))
                else:
                    incomplete += 1
            bucket = b
            count = 1
            o, h, l, c, v, qv = k.open, k.high, k.low, k.close, k.volume, k.quote_volume
        else:
            count += 1
            h = max(h, k.high)
            l = min(l, k.low)
            c = k.close
            v += k.volume
            qv += k.quote_volume
    if bucket is not None:
        if count == minutes:
            ot = bucket * step
            out.append(Kline(ot, o, h, l, c, ot + step - 1, v, qv))
        else:
            incomplete += 1
    return out, incomplete


def build_oos_store():
    os.makedirs(OOS_STORE_DIR, exist_ok=True)
    path = os.path.join(OOS_STORE_DIR, "15m.sqlite3")
    if os.path.exists(path):
        existing = KlineStore(path, cache_size=2)
        if len(existing.symbols()) >= max(1, int(len(SYMBOLS) * 0.9)):
            log(f"reuse OOS store symbols={len(existing.symbols())}")
            existing.close()
            return path, []
        existing.close()
        os.unlink(path)
    store = KlineStore(path, cache_size=2)
    audit = []
    t0 = time.time()
    for idx, sym in enumerate(SYMBOLS):
        bars, info = load_symbol_oos(sym)
        rb, incomplete = resample_complete(bars, 15)
        info["incomplete_15m_buckets"] = incomplete
        info["bars_15m"] = len(rb)
        audit.append(info)
        if rb:
            store.write_symbol(sym, rb)
        del bars, rb
        if (idx + 1) % 10 == 0 or idx + 1 == len(SYMBOLS):
            log(f"build OOS 15m {idx+1}/{len(SYMBOLS)} elapsed={time.time()-t0:.0f}s")
    store.finish_writing()
    store.close()
    return path, audit


def month_bounds(months):
    out = []
    for m in months:
        y, mo = int(m[:4]), int(m[5:7])
        start = utc_ms(y, mo, 1)
        end = utc_ms(y + 1, 1, 1) if mo == 12 else utc_ms(y, mo + 1, 1)
        out.append((m, start, end))
    return out


def universe_intervals(universe_by_month):
    bounds = {m: (s, e) for m, s, e in month_bounds(sorted(set(universe_by_month)))}
    intervals = {}
    for month, members in universe_by_month.items():
        start, end = bounds[month]
        for sym in members:
            intervals.setdefault(sym, []).append((start, end))
    for sym in intervals:
        intervals[sym].sort()
    return intervals


def build_caches(store, entries, warmup_ms, end_ms, universe_intervals_by_symbol=None):
    cfg0 = make_config(entries[0])
    tradable_meta = cfg0.get("_historical_tradable_symbols")
    symbols = [s for s in store.symbols() if scanner.is_structurally_allowed_symbol(s, cfg0, tradable_meta)]
    prebuilt = pb.build_timeline(store, INTERVAL, symbols, cfg0, include_ohlcv=False)
    timeline, _series_of, _deret = pb.unpack_prebuilt(prebuilt)
    caches = {}
    cfgs = {}
    for entry in entries:
        cfg = make_config(entry)
        cfgs[entry["id"]] = cfg
        caches[entry["id"]] = pb.EntrySignalCache({}, exit_signature=pb.exit_level_signature(cfg))
    if not symbols or not timeline:
        return prebuilt, caches, cfgs

    lookback = strategy.confirm_window_bars(cfg0)
    min_vol = float(cfg0.get("MIN_QUOTE_VOLUME_USDT_24H", 0) or 0)
    per_day = bars_per_day(INTERVAL)
    vol_params = entry_rule.volume_rise_params(cfg0)
    atr_period = entry_rule.atr_period(cfg0)
    required = entry_rule.required_bars(cfg0)
    first_allowed = timeline[0] + int(warmup_ms or 0)
    base_cfg = deepcopy(cfg0)
    base_cfg["VIDYA_ENTRY_TRIGGER"] = "FLIP"
    total = len(symbols)
    t0 = time.time()

    for si, sym in enumerate(symbols):
        if universe_intervals_by_symbol is not None and not universe_intervals_by_symbol.get(sym):
            continue
        kl = store.klines(sym)
        n = len(kl)
        if n < required:
            continue
        iv = universe_intervals_by_symbol.get(sym, []) if universe_intervals_by_symbol is not None else []
        series = vidya.compute_series(kl, base_cfg)
        highs = [k.high for k in kl]
        lows = [k.low for k in kl]
        closes = [k.close for k in kl]
        atr14 = vidya._wilder_atr(highs, lows, closes, atr_period)
        stats = compute_rolling_24h_stats(kl, per_day)
        tv = vidya_fast._turnover_array(kl)
        cum = [0.0]
        for value in tv:
            cum.append(cum[-1] + value)
        start_i = max(lookback - 1, required - 1)
        scans = 0
        scans_by_time = {}
        eligible = []
        iv_ptr = 0
        for i in range(start_i, n):
            ot = int(kl[i].open_time)
            if ot < first_allowed or ot > end_ms:
                continue
            if iv:
                while iv_ptr < len(iv) and iv[iv_ptr][1] <= ot:
                    iv_ptr += 1
                if iv_ptr >= len(iv) or ot < iv[iv_ptr][0]:
                    continue
            st = stats[i]
            if st is None or st["vol24h"] < min_vol:
                continue
            if not scanner.market_gate_ok_at(st["pct24h"], st["vol24h"], cfg0):
                continue
            scans += 1
            scans_by_time[ot] = scans_by_time.get(ot, 0) + 1
            ratio = None
            if vol_params["enabled"] and vol_params["mult"] > 0.0:
                ratio = vidya_fast._volume_ratio_at(cum, i, per_day, vol_params["days"], lookback)
                if ratio is not None and ratio + 1e-12 < vol_params["mult"]:
                    continue
            eligible.append((i, ot, int(kl[i].close_time), st["pct24h"], st["vol24h"], ratio))
        if not eligible:
            for entry in entries:
                cache = caches[entry["id"]]
                cache.entry_rule_scans += scans
                cache.entry_rule_skips += scans
                for ot, count in scans_by_time.items():
                    cache.entry_rule_scans_by_time[ot] = cache.entry_rule_scans_by_time.get(ot, 0) + count
                    cache.entry_rule_skips_by_time[ot] = cache.entry_rule_skips_by_time.get(ot, 0) + count
            del kl, series, atr14, stats, tv, cum
            continue

        for entry in entries:
            cfg = cfgs[entry["id"]]
            cache = caches[entry["id"]]
            vp = vidya.vidya_params(cfg)
            pullback = vidya._pullback_entries(closes, [k.open for k in kl], lows, series["vidya"], series["atr"], series["lower"], series["trend_up"], series["cross_up"], series["cross_down"], vp)
            cache.entry_rule_scans += scans
            for ot, count in scans_by_time.items():
                cache.entry_rule_scans_by_time[ot] = cache.entry_rule_scans_by_time.get(ot, 0) + count
            by_time = {}
            signal_count = 0
            for i, ot, ct, pct24, vol24, ratio in eligible:
                if not series["ready"][i] or not pullback[i]:
                    continue
                dr = series["delta_ratio"][i]
                if dr is None or dr < vp["delta_min_ratio"]:
                    continue
                bar = kl[i]
                price = float(bar.close)
                anchor_mode = entry_rule.vidya_sl_anchor(cfg)
                if anchor_mode == "SIGNAL_LOW":
                    anchor = float(bar.low)
                elif anchor_mode == "VIDYA_LINE":
                    anchor = float(series["vidya"][i])
                else:
                    anchor = float(series["lower"][i])
                if anchor <= 0.0 or anchor >= price:
                    anchor = float(bar.low)
                levels = entry_rule.exit_levels(price, anchor, cfg, atr=atr14[i])
                if not levels["ok"]:
                    continue
                setup = scanner.SetupResult(
                    ok=True, reason="VIDYA PULLBACK + delta volume beli", stage="OK",
                    signal_close=price, signal_close_time=ct, atr_value=atr14[i],
                    entry_interval=INTERVAL, bull_count=1, bull_required=1,
                    entry_low=anchor, sl_price=levels["sl_price"], tp_price=levels["tp_price"],
                    sl_pct=levels["sl_pct"], tp_pct=levels["tp_pct"], rr_ratio=levels["rr_ratio"],
                    turnover_ratio=ratio, ema_fast=series["vidya"][i], ema_slow=series["lower"][i],
                )
                by_time.setdefault(ot, []).append((pct24, vol24, sym, i, setup))
                signal_count += 1
            for ot, rows in by_time.items():
                rows.sort(key=lambda x: (-x[0], -x[1], x[2]))
                cache.signals[ot] = [(s, i, v24, setup) for (_pct, v24, s, i, setup) in rows]
            skips = scans - signal_count
            cache.entry_rule_skips += skips
            signal_by_time = {ot: len(rows) for ot, rows in by_time.items()}
            for ot, count in scans_by_time.items():
                skip_count = count - signal_by_time.get(ot, 0)
                if skip_count:
                    cache.entry_rule_skips_by_time[ot] = cache.entry_rule_skips_by_time.get(ot, 0) + skip_count
        del kl, series, atr14, stats, tv, cum, eligible
        if (si + 1) % 10 == 0 or si + 1 == total:
            log(f"precompute shared {si+1}/{total} elapsed={time.time()-t0:.0f}s")
    return prebuilt, caches, cfgs


def month_key(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m")


def metrics(res, month_labels):
    trades = res.trades
    initial = float(res.initial_equity or 0)
    open_pnl = float(res.mark_to_market_pnl_quote or 0) if res.mark_to_market_open else 0.0
    final = float(res.final_equity or 0) + open_pnl
    pnls = [float(t.pnl_quote or 0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    monthly = {m: 0.0 for m in month_labels}
    for t in trades:
        m = month_key(t.exit_time)
        if m in monthly:
            monthly[m] += float(t.pnl_quote or 0)
    if res.mark_to_market_open and trades:
        m = month_key(trades[-1].entry_time)
        if m in monthly:
            monthly[m] += open_pnl
    monthly_returns = {}
    equity = initial
    for m in month_labels:
        start_eq = equity
        equity += monthly[m]
        monthly_returns[m] = ((equity / start_eq) - 1) * 100 if start_eq > 0 else 0.0
    positive_months = sum(1 for m in month_labels if monthly[m] > 0)
    worst_month = min(monthly_returns.values()) if monthly_returns else 0.0
    best_month = max(monthly.values()) if monthly else 0.0
    net = final - initial
    concentration = (best_month / net * 100) if net > 0 else 0.0
    reasons = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    return {
        "return_pct": ((final / initial) - 1) * 100 if initial > 0 else 0.0,
        "closed_return_pct": ((float(res.final_equity or 0) / initial) - 1) * 100 if initial > 0 else 0.0,
        "final_equity": final,
        "open_pnl_quote": open_pnl,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": len(wins) / len(trades) * 100 if trades else 0.0,
        "profit_factor": pf,
        "max_drawdown_pct": float(res.intra_drawdown_pct or 0),
        "kill_switch_triggers": int((res.risk_events or {}).get("kill_switch_triggers", 0)),
        "entry_rule_scans": int(res.entry_rule_scans or 0),
        "entry_rule_skips": int(res.entry_rule_skips or 0),
        "signals": int(res.entry_rule_scans or 0) - int(res.entry_rule_skips or 0),
        "monthly_pnl_quote": monthly,
        "monthly_return_pct": monthly_returns,
        "positive_months": positive_months,
        "worst_month_pct": worst_month,
        "best_month_concentration_pct": concentration,
        "exit_reasons": reasons,
        "mark_to_market_open": bool(res.mark_to_market_open),
    }


def run_entries(store_path, entries, warmup_ms, end_ms, month_labels, universe_by_month=None):
    store = KlineStore(store_path, cache_size=2)
    uiv = universe_intervals(universe_by_month) if universe_by_month else None
    prebuilt, caches, cfgs = build_caches(store, entries, warmup_ms, end_ms, uiv)
    results = {}
    for entry in entries:
        cid = entry["id"]
        t0 = time.time()
        res = pb.run_portfolio_backtest(
            store, cfgs[cid], INTERVAL, warmup_ms=warmup_ms, prebuilt=prebuilt,
            entry_signal_cache=caches[cid], end_ms=end_ms, max_skipped_records=50,
        )
        m = metrics(res, month_labels)
        results[cid] = {"entry": entry, "config": {k: cfgs[cid].get(k) for k in sorted(set(BASE_OVERRIDES) | set(entry.get("overrides", {})) | {"ENTRY_MIN_RR_RATIO", "EXIT_MAX_HOLD_HOURS", "ENTRY_SL_ATR_BUFFER_MULT", "BACKTEST_ENTRY_SPREAD_PCT", "BACKTEST_SLIPPAGE_PCT", "RISK_PERCENT", "MAX_DRAWDOWN_PERCENT"})}, "metrics": m, "result": asdict(res)}
        log(f"RESULT {cid} return={m['return_pct']:.2f}% trades={m['trades']} PF={m['profit_factor']:.2f} DD={m['max_drawdown_pct']:.2f}% kill={m['kill_switch_triggers']} elapsed={time.time()-t0:.0f}s")
        del res
        gc.collect()
    store.close()
    return results


def is_eligible(m):
    return (
        m["return_pct"] > 0
        and m["profit_factor"] >= 1.2
        and m["trades"] >= 20
        and m["kill_switch_triggers"] == 0
        and m["positive_months"] >= 3
        and m["worst_month_pct"] >= -10.0
    )


def select_configs(is_results):
    base_m = is_results["base"]["metrics"]
    family_members = {"base": ["base"]}
    for entry in CONFIGS:
        if entry["id"] != "base":
            family_members.setdefault(entry["family"], ["base"]).append(entry["id"])
    stable_families = {
        fam for fam, ids in family_members.items()
        if all(is_eligible(is_results[i]["metrics"]) for i in ids if i in is_results)
    }
    candidates = []
    for entry in CONFIGS:
        cid = entry["id"]
        if cid == "base":
            continue
        m = is_results[cid]["metrics"]
        if is_eligible(m):
            candidates.append((entry, m))
    candidates.sort(key=lambda item: (
        item[0]["family"] not in stable_families,
        -item[1]["positive_months"],
        item[1]["max_drawdown_pct"],
        -item[1]["trades"],
        abs(item[1]["return_pct"] - base_m["return_pct"]),
    ))
    selected = [next(e for e in CONFIGS if e["id"] == "base")]
    used_families = {"base"}
    for entry, _m in candidates:
        if entry["family"] in used_families:
            continue
        selected.append(entry)
        used_families.add(entry["family"])
        if len(selected) >= 4:
            break
    return selected, sorted(stable_families)


def compare_baseline(is_results):
    if not os.path.exists(PREV_PULLBACK_JSON):
        return {"available": False}
    prev = json.load(open(PREV_PULLBACK_JSON))["results"]["15m"]
    new = is_results["base"]["result"]
    prev_trades = prev["trades"]
    new_trades = new["trades"]
    same = len(prev_trades) == len(new_trades)
    diffs = []
    if same:
        for a, b in zip(prev_trades, new_trades):
            for key in ("symbol", "entry_time", "exit_time", "reason"):
                if a.get(key) != b.get(key):
                    same = False
                    diffs.append({"key": key, "prev": a.get(key), "new": b.get(key)})
            if abs(float(a.get("pnl_quote", 0)) - float(b.get("pnl_quote", 0))) > 1e-9:
                same = False
                diffs.append({"key": "pnl_quote", "prev": a.get("pnl_quote"), "new": b.get("pnl_quote")})
    return {"available": True, "identical": same, "prev_trades": len(prev_trades), "new_trades": len(new_trades), "diffs": diffs[:10]}


FULL_ROOT = "/home/user/.cache/vidya_full"
FULL_STORE_DIR = os.path.join(FULL_ROOT, "stores")
FULL_JSON = "/home/user/vidya_sensitivity_full.json"
FULL_ZIP_DIRS = [os.path.join(ROOT, "zips"), OOS_ZIP_DIR]
FULL_MONTHS = ["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
FULL_START = OOS_START
FULL_END = IS_END
FULL_MONTH_LABELS = OOS_MONTH_LABELS + IS_MONTHS


def zip_path_for(sym, month):
    for base_dir in FULL_ZIP_DIRS:
        p = os.path.join(base_dir, f"{sym}-1m-{month}.zip")
        if os.path.exists(p):
            return p
    return None


def load_symbol_span(sym, months, start_ms, end_ms):
    bars = []
    seen = set()
    duplicate = invalid = 0
    for month in months:
        path = zip_path_for(sym, month)
        if not path:
            continue
        with zipfile.ZipFile(path) as z:
            with z.open(z.namelist()[0]) as f:
                for row in csv.reader(io.TextIOWrapper(f, "utf-8")):
                    if not row or not row[0].strip().lstrip("-").isdigit():
                        continue
                    try:
                        ot = _ms(row[0])
                        if ot < start_ms or ot >= end_ms:
                            continue
                        o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
                        v = float(row[5])
                        ct = _ms(row[6])
                        qv = float(row[7])
                    except (ValueError, IndexError):
                        invalid += 1
                        continue
                    if not all(math.isfinite(x) for x in (o, h, l, c, v, qv)):
                        invalid += 1
                        continue
                    if c <= 0 or o <= 0 or h < l or h < max(o, c) or l > min(o, c) or v < 0 or qv < 0 or ct <= ot:
                        invalid += 1
                        continue
                    if ot in seen:
                        duplicate += 1
                        continue
                    seen.add(ot)
                    bars.append(Kline(ot, o, h, l, c, ct, v, qv))
    bars.sort(key=lambda k: k.open_time)
    expected = (end_ms - start_ms) // 60000
    missing = max(0, expected - len(bars))
    return bars, {"symbol": sym, "bars_1m": len(bars), "missing_minutes": missing, "duplicate_rows": duplicate, "invalid_rows": invalid}


def build_full_store():
    os.makedirs(FULL_STORE_DIR, exist_ok=True)
    path = os.path.join(FULL_STORE_DIR, "15m.sqlite3")
    if os.path.exists(path):
        existing = KlineStore(path, cache_size=2)
        if len(existing.symbols()) >= max(1, int(len(SYMBOLS) * 0.9)):
            log(f"reuse full store symbols={len(existing.symbols())}")
            existing.close()
            return path
        existing.close()
        os.unlink(path)
    store = KlineStore(path, cache_size=2)
    t0 = time.time()
    for idx, sym in enumerate(SYMBOLS):
        bars, info = load_symbol_span(sym, FULL_MONTHS, OOS_WARMUP_START, FULL_END)
        rb, _incomplete = resample_complete(bars, 15)
        if rb:
            store.write_symbol(sym, rb)
        del bars, rb
        if (idx + 1) % 10 == 0 or idx + 1 == len(SYMBOLS):
            log(f"build full 15m {idx+1}/{len(SYMBOLS)} elapsed={time.time()-t0:.0f}s")
    store.finish_writing()
    store.close()
    return path


def stage_full(force=False):
    manifest = json.load(open(MANIFEST_JSON))
    if os.path.exists(FULL_JSON) and not force:
        data = json.load(open(FULL_JSON))
        needed = [e["id"] for e in manifest["selected"]] + ["base_cost2x"]
        if all(cid in data.get("results", {}) for cid in needed):
            log("reuse full results")
            return data
    store_path = build_full_store()
    entries = list(manifest["selected"]) + [cost_stress_entry(next(e for e in CONFIGS if e["id"] == "base"))]
    results = run_entries(store_path, entries, FULL_START - OOS_WARMUP_START, FULL_END, FULL_MONTH_LABELS)
    data = {"manifest": manifest, "store_path": store_path, "results": results}
    with open(FULL_JSON, "w") as f:
        json.dump(data, f, default=str)
    log("FULL SELESAI")
    return data


def stage_is(force=False):
    if os.path.exists(IS_JSON) and not force:
        data = json.load(open(IS_JSON))
        if all(e["id"] in data.get("results", {}) for e in CONFIGS):
            log("reuse IS results")
            return data
    t0 = time.time()
    results = run_entries(IS_STORE, CONFIGS, IS_START - IS_WARMUP_START, IS_END, IS_MONTHS)
    selected, stable_families = select_configs(results)
    stress_entries = [cost_stress_entry(e) for e in selected]
    stress_results = run_entries(IS_STORE, stress_entries, IS_START - IS_WARMUP_START, IS_END, IS_MONTHS)
    results.update(stress_results)
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "interval": INTERVAL,
        "universe": SYMBOLS,
        "in_sample": {"warmup_start": IS_WARMUP_START, "start": IS_START, "end": IS_END, "months": IS_MONTHS},
        "out_of_sample": {"warmup_start": OOS_WARMUP_START, "start": OOS_START, "end": OOS_END, "months": OOS_MONTH_LABELS},
        "selection_rule": "Baseline selalu diuji. Kandidat wajib return positif, PF >= 1.2, trade >= 20, kill switch 0, bulan positif >= 3, dan bulan terburuk >= -10%. Pilih maksimal 3 keluarga parameter berbeda, utamakan keluarga yang semua variasinya lolos, lalu bulan positif terbanyak dan drawdown terkecil. Pemilihan dibekukan sebelum melihat OOS.",
        "configs": CONFIGS,
        "selected": selected,
        "stable_families": stable_families,
        "baseline_comparison": compare_baseline(results),
        "elapsed_seconds": time.time() - t0,
    }
    data = {"manifest": manifest, "results": results}
    with open(IS_JSON, "w") as f:
        json.dump(data, f, default=str)
    with open(MANIFEST_JSON, "w") as f:
        json.dump(manifest, f, indent=2, default=str)
    log("IS selected=" + ",".join(e["id"] for e in selected) + " stable_families=" + ",".join(stable_families))
    log("baseline_comparison=" + json.dumps(manifest["baseline_comparison"]))
    return data


def stage_oos(force=False):
    manifest = json.load(open(MANIFEST_JSON))
    if os.path.exists(OOS_JSON) and not force:
        data = json.load(open(OOS_JSON))
        needed = [e["id"] for e in manifest["selected"]] + [e["id"] + "_cost2x" for e in manifest["selected"]]
        if all(cid in data.get("results", {}) for cid in needed):
            log("reuse OOS results")
            return data
    fails = download_oos()
    store_path, audit = build_oos_store()
    entries = list(manifest["selected"]) + [cost_stress_entry(e) for e in manifest["selected"]]
    results = run_entries(store_path, entries, OOS_START - OOS_WARMUP_START, OOS_END, OOS_MONTH_LABELS)
    data = {
        "manifest": manifest,
        "download_failures": fails,
        "data_audit": audit,
        "store_path": store_path,
        "results": results,
    }
    with open(OOS_JSON, "w") as f:
        json.dump(data, f, default=str)
    log("OOS SELESAI")
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["is", "oos", "full", "all"], default="all")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    os.makedirs(OOS_ROOT, exist_ok=True)
    tempfile.tempdir = OOS_ROOT
    os.environ["TMPDIR"] = OOS_ROOT
    if args.stage in ("is", "all"):
        stage_is(args.force)
    if args.stage in ("oos", "all"):
        stage_oos(args.force)
    if args.stage in ("full", "all"):
        stage_full(args.force)
    log("SELESAI")


if __name__ == "__main__":
    main()
