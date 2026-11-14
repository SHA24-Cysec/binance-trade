from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import hashlib
import io
import json
import math
import os
import tempfile
import time
import zipfile
from urllib.parse import quote

from backtesting import vidya_sensitivity as vs
from backtesting.backtest_storage import KlineStore
from strategy.indicators import Kline

PIT_ROOT = "/home/user/.cache/vidya_pit"
PIT_ZIP_DIR = os.path.join(PIT_ROOT, "zips")
PIT_STORE_DIR = os.path.join(PIT_ROOT, "stores")
PIT_STORE = os.path.join(PIT_STORE_DIR, "15m.sqlite3")
UNIVERSE_JSON = "/home/user/pit_universe.json"
OUT_JSON = "/home/user/vidya_pit_results.json"
OLD_STORE = "/home/user/.cache/vidya_full/stores/15m.sqlite3"
RUN_IDS = ["base", "no_bull", "hold48"]

log = vs.log
SYMBOLS = vs.SYMBOLS
MONTHS = None


def download_one(sym, month):
    os.makedirs(PIT_ZIP_DIR, exist_ok=True)
    name = f"{sym}-1m-{month}"
    path = os.path.join(PIT_ZIP_DIR, name + ".zip")
    chk_path = path + ".CHECKSUM"
    enc = quote(sym, safe="")
    base = f"https://data.binance.vision/data/spot/monthly/klines/{enc}/1m/{enc}-1m-{month}"
    if os.path.exists(path) and os.path.getsize(path) > 0 and os.path.exists(chk_path):
        expected = open(chk_path).read().split()[0]
        if hashlib.sha256(open(path, "rb").read()).hexdigest() == expected:
            return sym, month, True, "cached"
    last = "unreachable"
    for attempt in range(4):
        tmp = None
        try:
            raw = vs.fetch_bytes(base + ".zip")
            expected = vs.fetch_bytes(base + ".zip.CHECKSUM", timeout=30).decode().split()[0]
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("checksum mismatch")
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                if z.testzip() is not None:
                    raise ValueError("zip corrupt")
            fd, tmp = tempfile.mkstemp(prefix=name + ".", suffix=".tmp", dir=PIT_ZIP_DIR)
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


def download_plan(plan):
    jobs = [(s, m) for s, months in plan.items() for m in months]
    done = 0
    fails = []
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        for sym, month, ok, info in ex.map(lambda a: download_one(*a), jobs):
            done += 1
            if not ok:
                fails.append({"symbol": sym, "month": month, "error": info})
            if done % 100 == 0 or done == len(jobs):
                log(f"download PIT {done}/{len(jobs)} fails={len(fails)} elapsed={time.time()-t0:.0f}s")
    return fails


def load_symbol_span(sym, months, start_ms, end_ms):
    bars = []
    seen = set()
    duplicate = invalid = 0
    for month in months:
        path = os.path.join(PIT_ZIP_DIR, f"{sym}-1m-{month}.zip")
        if not os.path.exists(path):
            continue
        with zipfile.ZipFile(path) as z:
            with z.open(z.namelist()[0]) as f:
                for row in csv.reader(io.TextIOWrapper(f, "utf-8")):
                    if not row or not row[0].strip().lstrip("-").isdigit():
                        continue
                    try:
                        ot = vs._ms(row[0])
                        if ot < start_ms or ot >= end_ms:
                            continue
                        o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
                        v = float(row[5])
                        ct = vs._ms(row[6])
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
    return bars, {"symbol": sym, "bars_1m": len(bars), "duplicate_rows": duplicate, "invalid_rows": invalid, "months": len(months)}


def build_pit_store(plan, rebuild=False):
    os.makedirs(PIT_STORE_DIR, exist_ok=True)
    if os.path.exists(PIT_STORE) and rebuild:
        os.unlink(PIT_STORE)
    if os.path.exists(PIT_STORE):
        existing = KlineStore(PIT_STORE, cache_size=2)
        if len(existing.symbols()) >= max(1, int(len(plan) * 0.95)):
            log(f"reuse PIT store symbols={len(existing.symbols())}")
            existing.close()
            return PIT_STORE, []
        existing.close()
        os.unlink(PIT_STORE)
    store = KlineStore(PIT_STORE, cache_size=2)
    audit = []
    t0 = time.time()
    items = sorted(plan.items())
    for idx, (sym, months) in enumerate(items):
        bars, info = load_symbol_span(sym, months, vs.OOS_WARMUP_START, vs.FULL_END)
        rb, incomplete = vs.resample_complete(bars, 15)
        info["incomplete_15m_buckets"] = incomplete
        info["bars_15m"] = len(rb)
        audit.append(info)
        if rb:
            store.write_symbol(sym, rb)
        del bars, rb
        if (idx + 1) % 25 == 0 or idx + 1 == len(items):
            log(f"build PIT 15m {idx+1}/{len(items)} elapsed={time.time()-t0:.0f}s")
    store.finish_writing()
    store.close()
    return PIT_STORE, audit


def contiguous_plan(universe, months):
    plan = {}
    for month, members in universe.items():
        i = months.index(month)
        for sym in members:
            plan.setdefault(sym, set()).update({i, i - 1})
    return {sym: [months[i] for i in range(min(v), max(v) + 1)] for sym, v in plan.items()}


def top_n_universe(universe, liquidity, months, n):
    out = {}
    for month in sorted(universe):
        prev = months[months.index(month) - 1]
        ranked = []
        for sym in universe[month]:
            entry = liquidity.get(sym, {}).get(prev) or {}
            ranked.append((entry.get("median_quote_volume", 0.0), sym))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        out[month] = [sym for _v, sym in ranked[:n]]
    return out


def run_case(store_path, entries, universe_by_month, label, month_labels, results):
    t0 = time.time()
    got = vs.run_entries(store_path, entries, vs.FULL_START - vs.OOS_WARMUP_START, vs.FULL_END, month_labels, universe_by_month)
    for cid, payload in got.items():
        m = payload["metrics"]
        results[f"{label}::{cid}"] = payload
        log(f"RESULT {label} {cid} return={m['return_pct']:.2f}% trades={m['trades']} PF={m['profit_factor']:.2f} DD={m['max_drawdown_pct']:.2f}% kill={m['kill_switch_triggers']} elapsed={time.time()-t0:.0f}s")
    return got


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["download", "build", "run", "old", "pit94", "all"], default="all")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    os.makedirs(PIT_ROOT, exist_ok=True)
    tempfile.tempdir = PIT_ROOT
    os.environ["TMPDIR"] = PIT_ROOT
    data = json.load(open(UNIVERSE_JSON))
    universe = data["universe"]
    months = data["months"]
    month_labels = data["trading_months"]
    plan = contiguous_plan(universe, months)
    log(f"plan: {len(plan)} pair, {sum(len(v) for v in plan.values())} zip bulan")

    results = {}
    store_path = PIT_STORE
    audit = []
    if args.stage in ("download", "all"):
        fails = download_plan(plan)
        if fails:
            log(f"GAGAL unduh {len(fails)}: {fails[:5]}")
    if args.stage in ("build", "all"):
        store_path, audit = build_pit_store(plan, args.rebuild)
    if args.stage in ("run", "all"):
        entries = [e for e in vs.CONFIGS if e["id"] in RUN_IDS]
        run_case(store_path, entries, universe, "pit", month_labels, results)
    if args.stage in ("old", "all"):
        entries = [e for e in vs.CONFIGS if e["id"] in RUN_IDS]
        run_case(OLD_STORE, entries, None, "fixed94", month_labels, results)
    if args.stage in ("pit94", "all"):
        liq = json.load(open("/home/user/pit_liquidity.json"))["liquidity"]
        u94 = top_n_universe(universe, liq, months, len(SYMBOLS))
        entries = [e for e in vs.CONFIGS if e["id"] in RUN_IDS]
        run_case(PIT_STORE, entries, u94, "pit_top94", month_labels, results)
        log("ukuran universe pit_top94: " + ",".join(str(len(v)) for k, v in sorted(u94.items())))

    out = {}
    if os.path.exists(OUT_JSON):
        out = json.load(open(OUT_JSON))
    out.setdefault("meta", {})["threshold_usdt_per_day"] = data["threshold_median_daily_quote_volume_usdt"]
    out["meta"]["months"] = months
    out["meta"]["trading_months"] = month_labels
    out["meta"]["pit_pairs"] = sorted(plan)
    out["meta"]["pit_universe_sizes"] = {m: len(v) for m, v in universe.items()}
    out["meta"]["old_universe_size"] = len(SYMBOLS)
    out["meta"]["download_failures"] = out.get("meta", {}).get("download_failures", 0)
    if audit:
        out["meta"]["data_audit"] = {
            "symbols": len(audit),
            "total_bars_15m": sum(a["bars_15m"] for a in audit),
            "invalid_rows": sum(a["invalid_rows"] for a in audit),
            "duplicate_rows": sum(a["duplicate_rows"] for a in audit),
            "incomplete_buckets": sum(a["incomplete_15m_buckets"] for a in audit),
        }
    out.setdefault("results", {}).update(results)
    with open(OUT_JSON, "w") as f:
        json.dump(out, f, default=str)
    log("SELESAI")


if __name__ == "__main__":
    main()
