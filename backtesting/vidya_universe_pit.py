from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import io
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from urllib.parse import quote
import zipfile
from datetime import datetime, timezone

ALL_SYMBOLS_FILE = "/home/user/usdt_symbols_all.txt"
OLD_UNIVERSE_FILE = "/home/user/universe.txt"
PIT_DIR = "/home/user/.cache/vidya_pit"
ACTIVITY_JSON = "/home/user/pit_activity.json"
LIQUIDITY_JSON = "/home/user/pit_liquidity.json"
UNIVERSE_JSON = "/home/user/pit_universe.json"
MONTHS = [
    "2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03",
    "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10",
]
TRADING_MONTHS = MONTHS[1:]
SAMPLE_DAYS = [5, 15, 25]
UA = {"User-Agent": "binance-trade-backtest/1.0"}


def log(msg):
    print(msg, flush=True)


def symbols_all():
    return [s.strip() for s in open(ALL_SYMBOLS_FILE).read().split() if s.strip()]


def symbols_old():
    return [s.strip() for s in open(OLD_UNIVERSE_FILE).read().split() if s.strip()]


def request(url, method="GET", timeout=30):
    req = urllib.request.Request(url, headers=UA, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def with_retry(fn, tries=5):
    last = None
    for attempt in range(tries):
        try:
            return fn(), None
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 403, 400):
                return None, exc.code
            last = f"HTTP {exc.code}"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < tries - 1:
            time.sleep(1.5 * (attempt + 1))
    return None, last


def monthly_url(sym, month):
    s = quote(sym, safe="")
    return f"https://data.binance.vision/data/spot/monthly/klines/{s}/1m/{s}-1m-{month}.zip"


def daily_url(sym, day):
    s = quote(sym, safe="")
    return f"https://data.binance.vision/data/spot/daily/klines/{s}/1m/{s}-1m-{day}.zip"


def probe_month(sym, month):
    def call():
        status, _ = request(monthly_url(sym, month), method="HEAD", timeout=25)
        return status
    status, err = with_retry(call)
    if status == 200:
        return sym, month, "active"
    if status is None and str(err) in ("404", "403", "400"):
        return sym, month, "absent"
    return sym, month, f"error:{err}"


def stage_activity(symbols):
    if os.path.exists(ACTIVITY_JSON):
        data = json.load(open(ACTIVITY_JSON))
        if len(data.get("activity", {})) >= len(symbols):
            log("reuse activity map")
            return data
    activity = {s: {} for s in symbols}
    jobs = [(s, m) for s in symbols for m in MONTHS]
    done = 0
    errors = 0
    t0 = time.time()
    log(f"probe {len(jobs)} kombinasi simbol-bulan")
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for sym, month, state in ex.map(lambda a: probe_month(*a), jobs):
            done += 1
            activity[sym][month] = state
            if state.startswith("error"):
                errors += 1
            if done % 1000 == 0 or done == len(jobs):
                log(f"probe {done}/{len(jobs)} errors={errors} elapsed={time.time()-t0:.0f}s")
    data = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "months": MONTHS,
        "symbol_count": len(symbols),
        "errors": errors,
        "activity": activity,
    }
    with open(ACTIVITY_JSON, "w") as f:
        json.dump(data, f)
    return data


def day_quote_volume(sym, day):
    def call():
        _status, raw = request(daily_url(sym, day), timeout=30)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            with z.open(z.namelist()[0]) as f:
                rows = csv.reader(io.TextIOWrapper(f, "utf-8"))
                total = 0.0
                minutes = 0
                for row in rows:
                    if not row or not row[0].strip().lstrip("-").isdigit():
                        continue
                    try:
                        qv = float(row[7])
                        vol = float(row[5])
                    except (ValueError, IndexError):
                        continue
                    total += qv
                    if vol > 0:
                        minutes += 1
        return {"quote_volume": total, "active_minutes": minutes}

    res, err = with_retry(call)
    if res is None:
        return None, err
    return res, None


def stage_liquidity(activity):
    if os.path.exists(LIQUIDITY_JSON):
        data = json.load(open(LIQUIDITY_JSON))
        if len(data.get("liquidity", {})) >= len(activity["activity"]):
            log("reuse liquidity samples")
            return data
    jobs = []
    for sym, months in activity["activity"].items():
        for month in MONTHS:
            if months.get(month) == "active":
                jobs.append((sym, month))
    log(f"sampling likuiditas {len(jobs)} simbol-bulan x {len(SAMPLE_DAYS)} hari")
    liquidity = {}
    done = 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(day_quote_volume, sym, f"{month}-{day:02d}"): (sym, month, day) for sym, month in jobs for day in SAMPLE_DAYS}
        for fut in cf.as_completed(futures):
            sym, month, day = futures[fut]
            res, err = fut.result()
            done += 1
            entry = liquidity.setdefault(sym, {}).setdefault(month, {"days": {}, "errors": 0})
            if res is None:
                entry["errors"] += 1
            else:
                entry["days"][str(day)] = res
            if done % 2000 == 0 or done == len(futures):
                log(f"sampling {done}/{len(futures)} elapsed={time.time()-t0:.0f}s")
    for sym, months in liquidity.items():
        for month, entry in months.items():
            vals = [d["quote_volume"] for d in entry["days"].values()]
            mins = [d["active_minutes"] for d in entry["days"].values()]
            entry["samples"] = len(vals)
            entry["median_quote_volume"] = statistics.median(vals) if vals else 0.0
            entry["median_active_minutes"] = statistics.median(mins) if mins else 0
    data = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "months": MONTHS,
        "sample_days": SAMPLE_DAYS,
        "liquidity": liquidity,
    }
    with open(LIQUIDITY_JSON, "w") as f:
        json.dump(data, f)
    return data


def build_universe(activity, liquidity, threshold, min_samples=2, min_active_minutes=720):
    act = activity["activity"]
    liq = liquidity["liquidity"]
    universe = {}
    for month in TRADING_MONTHS:
        idx = MONTHS.index(month)
        prev = MONTHS[idx - 1]
        members = []
        for sym in act:
            if act[sym].get(month) != "active":
                continue
            entry = liq.get(sym, {}).get(prev)
            if not entry or entry.get("samples", 0) < min_samples:
                continue
            if entry.get("median_quote_volume", 0) < threshold:
                continue
            if entry.get("median_active_minutes", 0) < min_active_minutes:
                continue
            members.append(sym)
        universe[month] = sorted(members)
    return universe


def summarize(activity, liquidity, universe, threshold):
    act = activity["activity"]
    liq = liquidity["liquidity"]
    old = set(symbols_old())
    rows = []
    for month in TRADING_MONTHS:
        idx = MONTHS.index(month)
        prev = MONTHS[idx - 1]
        active = [s for s in act if act[s].get(month) == "active"]
        members = universe[month]
        old_active = [s for s in old if act[s].get(month) == "active"]
        old_liq = [s for s in old_active if (liq.get(s, {}).get(prev, {}) or {}).get("median_quote_volume", 0) >= threshold]
        rows.append({
            "month": month,
            "active_pairs": len(active),
            "pit_universe": len(members),
            "old_universe_active": len(old_active),
            "old_universe_liquid": len(old_liq),
            "pit_only_not_in_old": len([s for s in members if s not in old]),
        })
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["activity", "liquidity", "universe", "all"], default="all")
    parser.add_argument("--threshold", type=float, default=0.0)
    args = parser.parse_args()
    os.makedirs(PIT_DIR, exist_ok=True)
    symbols = symbols_all()
    activity = stage_activity(symbols) if args.stage in ("activity", "all") else json.load(open(ACTIVITY_JSON))
    liquidity = stage_liquidity(activity) if args.stage in ("liquidity", "all") else json.load(open(LIQUIDITY_JSON))
    if args.threshold > 0 and args.stage in ("universe", "all"):
        universe = build_universe(activity, liquidity, args.threshold)
        rows = summarize(activity, liquidity, universe, args.threshold)
        act = activity["activity"]
        old = set(symbols_old())
        listed_later = []
        delisted = []
        for sym, months in act.items():
            states = [months.get(m) for m in MONTHS]
            act_flags = [s == "active" for s in states]
            if any(act_flags) and not all(act_flags):
                first = act_flags.index(True)
                last = len(act_flags) - 1 - act_flags[::-1].index(True)
                if first > 0:
                    listed_later.append({"symbol": sym, "first_active": MONTHS[first]})
                if last < len(MONTHS) - 1:
                    delisted.append({"symbol": sym, "last_active": MONTHS[last]})
        members_all = set()
        for m in universe.values():
            members_all.update(m)
        data = {
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "threshold_median_daily_quote_volume_usdt": args.threshold,
            "months": MONTHS,
            "trading_months": TRADING_MONTHS,
            "universe": universe,
            "union_members": sorted(members_all),
            "monthly_summary": rows,
            "old_universe_not_active_at_window_start": sorted(s for s in old if act[s].get(MONTHS[0]) != "active"),
            "old_universe_not_active_at_window_end": sorted(s for s in old if act[s].get(MONTHS[-1]) != "active"),
            "pairs_newly_listed_in_window": listed_later,
            "pairs_delisted_in_window": delisted,
        }
        with open(UNIVERSE_JSON, "w") as f:
            json.dump(data, f, indent=1)
        log("ringkasan per bulan:")
        for r in rows:
            log(f"  {r['month']} aktif={r['active_pairs']} PIT={r['pit_universe']} lama_aktif={r['old_universe_active']} lama_likuid={r['old_universe_liquid']} baru_tak_di_lama={r['pit_only_not_in_old']}")
        log(f"pair lama tidak aktif di awal jendela: {len(data['old_universe_not_active_at_window_start'])} {data['old_universe_not_active_at_window_start'][:10]}")
        log(f"pair listing dalam jendela: {len(listed_later)}, delist dalam jendela: {len(delisted)}")
        log(f"union universe: {len(members_all)} pair")
    log("SELESAI")


if __name__ == "__main__":
    main()
