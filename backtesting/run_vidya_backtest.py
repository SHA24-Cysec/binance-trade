from __future__ import annotations

import concurrent.futures as cf
import csv
import gc
import hashlib
import io
import json
import os
import tempfile
import time
import urllib.request
import zipfile
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone

from config.config import PUMP_CONFIG
from strategy.indicators import Kline
from backtesting.backtest_storage import KlineStore
from backtesting import portfolio_backtest as pb
from backtesting import vidya_fast

ROOT = "/home/user/.cache/vidya_data"
ZIP_DIR = os.path.join(ROOT, "zips")
STORE_DIR = os.path.join(ROOT, "stores")
os.makedirs(ROOT, exist_ok=True)
os.makedirs(STORE_DIR, exist_ok=True)
tempfile.tempdir = ROOT
os.environ["TMPDIR"] = ROOT
OUT_JSON = "/home/user/vidya_backtest_results.json"
SYMBOLS = [s.strip() for s in open("/home/user/universe.txt").read().split() if s.strip()]
MONTHS = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
APR1 = int(datetime(2026, 4, 1, tzinfo=timezone.utc).timestamp() * 1000)
MAY1 = int(datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp() * 1000)
NOV1 = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp() * 1000)
WARMUP_MS = MAY1 - APR1
INTERVALS = ["1m", "3m", "5m", "15m"]
STEP_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}


def log(msg):
    print(msg, flush=True)


def _ms(v):
    v = int(v)
    return v // 1000 if v > 10**14 else v


def download_one(sym, month):
    name = f"{sym}-1m-{month}"
    path = os.path.join(ZIP_DIR, name + ".zip")
    base = f"https://data.binance.vision/data/spot/monthly/klines/{sym}/1m/{name}"
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return sym, month, True, "cached"
    for attempt in range(4):
        try:
            raw = urllib.request.urlopen(base + ".zip", timeout=60).read()
            chk = urllib.request.urlopen(base + ".zip.CHECKSUM", timeout=30).read().decode().split()[0]
            if hashlib.sha256(raw).hexdigest() != chk:
                raise ValueError("checksum mismatch")
            with open(path, "wb") as f:
                f.write(raw)
            return sym, month, True, "ok"
        except Exception as e:
            if attempt == 3:
                return sym, month, False, str(e)
            time.sleep(1.5 * (attempt + 1))
    return sym, month, False, "unreachable"


def download_all():
    os.makedirs(ZIP_DIR, exist_ok=True)
    jobs = [(s, m) for s in SYMBOLS for m in MONTHS]
    done = 0
    fails = []
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for sym, month, ok, info in ex.map(lambda a: download_one(*a), jobs):
            done += 1
            if not ok:
                fails.append((sym, month, info))
            if done % 50 == 0 or done == len(jobs):
                log(f"download {done}/{len(jobs)} fails={len(fails)}")
    if fails:
        log("download failures: " + json.dumps(fails[:20]))
    return fails


def load_symbol_1m(sym):
    bars = []
    for month in MONTHS:
        path = os.path.join(ZIP_DIR, f"{sym}-1m-{month}.zip")
        if not os.path.exists(path):
            continue
        with zipfile.ZipFile(path) as z:
            with z.open(z.namelist()[0]) as f:
                for row in csv.reader(io.TextIOWrapper(f, "utf-8")):
                    if not row or not row[0].strip().lstrip("-").isdigit():
                        continue
                    ot = _ms(row[0])
                    if ot < APR1 or ot >= NOV1:
                        continue
                    o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
                    v = float(row[5])
                    ct = _ms(row[6])
                    qv = float(row[7])
                    if c <= 0 or h < l:
                        continue
                    bars.append(Kline(ot, o, h, l, c, ct, v, qv))
    bars.sort(key=lambda k: k.open_time)
    return bars


def resample(bars, minutes):
    if minutes == 1:
        return bars
    step = minutes * 60000
    out = []
    cur = None
    bucket = None
    for k in bars:
        b = k.open_time // step
        if b != bucket:
            if cur is not None:
                out.append(cur)
            bucket = b
            ot = b * step
            cur = [ot, k.open, k.high, k.low, k.close, ot + step - 1, k.volume, k.quote_volume]
        else:
            cur[2] = max(cur[2], k.high)
            cur[3] = min(cur[3], k.low)
            cur[4] = k.close
            cur[6] += k.volume
            cur[7] += k.quote_volume
    if cur is not None:
        out.append(Kline(*cur))
    return [Kline(*c) for c in out] if out and isinstance(out[0], list) else out


def build_store(interval):
    path = os.path.join(STORE_DIR, f"{interval}.sqlite3")
    if os.path.exists(path):
        existing = KlineStore(path, cache_size=2)
        if len(existing.symbols()) >= max(1, int(len(SYMBOLS) * 0.9)):
            log(f"reuse store {interval} symbols={len(existing.symbols())}")
            return existing
        existing.close()
        os.unlink(path)
    store = KlineStore(path, cache_size=2)
    minutes = STEP_MIN[interval]
    t0 = time.time()
    for idx, sym in enumerate(SYMBOLS):
        bars = load_symbol_1m(sym)
        if not bars:
            continue
        rb = resample(bars, minutes)
        if rb:
            store.write_symbol(sym, rb)
        if (idx + 1) % 20 == 0 or idx + 1 == len(SYMBOLS):
            log(f"build {interval} {idx+1}/{len(SYMBOLS)} elapsed={time.time()-t0:.0f}s")
    store.finish_writing()
    return store


def build_store_resampled(base_store, interval):
    path = os.path.join(STORE_DIR, f"{interval}.sqlite3")
    if os.path.exists(path):
        existing = KlineStore(path, cache_size=2)
        if len(existing.symbols()) >= max(1, int(len(base_store.symbols()) * 0.9)):
            log(f"reuse store {interval} symbols={len(existing.symbols())}")
            return existing
        existing.close()
        os.unlink(path)
    store = KlineStore(path, cache_size=2)
    minutes = STEP_MIN[interval]
    syms = base_store.symbols()
    t0 = time.time()
    for idx, sym in enumerate(syms):
        rb = resample(base_store.klines(sym), minutes)
        if rb:
            store.write_symbol(sym, rb)
        if (idx + 1) % 20 == 0 or idx + 1 == len(syms):
            log(f"resample {interval} {idx+1}/{len(syms)} elapsed={time.time()-t0:.0f}s")
    store.finish_writing()
    return store


def base_config(interval):
    cfg = deepcopy(PUMP_CONFIG)
    cfg["ENTRY_FILTER_MODE"] = "VIDYA"
    cfg["CONFIRM_INTERVAL"] = interval
    cfg["CONFIRM_LOOKBACK_BARS"] = 997
    cfg["BACKTEST_SYMBOL_CACHE_SIZE"] = 2
    return cfg


def to_jsonable(obj):
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


def main():
    log(f"universe={len(SYMBOLS)} months={MONTHS[0]}..{MONTHS[-1]} trade_window=May1..Oct31")
    fails = download_all()
    results = {}
    if os.path.exists(OUT_JSON):
        try:
            prev = json.load(open(OUT_JSON)).get("results", {})
            results = {k: v for k, v in prev.items() if v.get("final_equity") is not None}
            if results:
                log(f"resume completed={list(results)}")
        except Exception:
            results = {}
    base_store = build_store("1m")
    for interval in INTERVALS:
        if interval in results:
            log(f"=== interval {interval} skipped (sudah selesai) ===")
            continue
        log(f"=== interval {interval} ===")
        store = base_store if interval == "1m" else build_store_resampled(base_store, interval)
        cfg = base_config(interval)
        t0 = time.time()
        cache, prebuilt = vidya_fast.precompute_vidya_signals(store, cfg, interval, warmup_ms=WARMUP_MS)
        log(f"precompute {interval} signals={sum(len(v) for v in cache.signals.values())} times={len(cache.signals)} elapsed={time.time()-t0:.0f}s")
        t1 = time.time()
        res = pb.run_portfolio_backtest(
            store, cfg, interval, warmup_ms=WARMUP_MS, prebuilt=prebuilt,
            entry_signal_cache=cache, end_ms=NOV1, max_skipped_records=50,
        )
        log(f"backtest {interval} elapsed={time.time()-t1:.0f}s")
        results[interval] = to_jsonable(res)
        eq0 = getattr(res, "initial_equity", 0) or 0
        eq1 = getattr(res, "final_equity", 0) or 0
        ret = (eq1 / eq0 - 1) * 100 if eq0 else 0.0
        log(f"RESULT {interval} trades={len(getattr(res,'trades',[]))} return={ret:.2f}% dd={getattr(res,'intra_drawdown_pct',0):.2f}%")
        if store is not base_store:
            store.close()
        del cache, prebuilt, res
        gc.collect()
        with open(OUT_JSON, "w") as f:
            json.dump({"universe": SYMBOLS, "months": MONTHS, "download_failures": fails, "results": results}, f, default=str)
    base_store.close()
    log("SELESAI")


if __name__ == "__main__":
    main()
