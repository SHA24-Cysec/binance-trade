"""Penyegar watchlist otomatis (READ-ONLY terhadap keputusan trading).

Modul ini menyusun ulang KEANGGOTAAN daftar watchlist secara berkala dari data
Binance terbaru. Keanggotaan diperbarui tiap beberapa jam untuk menghemat weight,
sedangkan panel hanya menampilkan data monitoring likuiditas dan harga.
Semua perhitungan di modul ini bersifat read-only dan tidak memengaruhi
operasi bot.

================================================================
YANG PALING PENTING DIPAHAMI
================================================================
Modul ini TIDAK PERNAH memengaruhi keputusan trading. Ia hanya mengganti
isi panel pantau di dashboard. Bot tetap memindai SELURUH pair USDT.
Tidak ada fungsi di sini yang dipanggil oleh pump_scanner_bot.py.

Hasilnya ditulis ke file terpisah (watchlist_auto_<mode>.json), TIDAK
PERNAH menimpa config.py. Daftar manual di config.py tetap utuh sebagai
cadangan kalau penyegaran gagal atau dimatikan.

================================================================
KENAPA RUANG LINGKUPNYA DIKECILKAN
================================================================
Batas resmi Binance adalah 6000 request weight per menit, DAN batas itu
dihitung per IP, bukan per API key (sumber: developers.binance.com,
General REST API Information / LIMITS, dicek 2026-09-24). Artinya
dashboard dan bot berbagi jatah yang sama persis. Kalau jatah habis,
Binance membalas HTTP 429, dan kalau tetap dipaksa berlanjut ke HTTP 418
yaitu ban IP yang durasinya meningkat "from 2 minutes to 3 days". Ban
seperti itu bisa membuat bot gagal menutup posisi tepat waktu.

Karena itu metodologi penuh (110 simbol x 45 hari = 2.944 weight) TIDAK
dipakai untuk penyegaran berulang. Yang dipakai:

    ticker/24hr semua pair    :  80 weight  (1 panggilan)
    bookTicker semua pair     :   4 weight  (1 panggilan)
    klines 5m 14 hari x 60    : 600 weight  (300 panggilan @2)
    --------------------------------------------------------
    TOTAL per siklus          : 684 weight

Disebar selama 15 menit, itu hanya 45,6 weight/menit atau 0,76% dari
anggaran. Sisa 99% tetap milik bot.

================================================================
REM KEAMANAN YANG TIDAK BISA DIMATIKAN LEWAT CONFIG
================================================================
1. Penyegaran DILEWATI selama bot sedang memegang posisi terbuka. Itu
   saat paling kritis, ketika bot harus bisa mengirim order jual kapan
   saja tanpa bersaing dengan unduhan data.
2. Penyegaran berhenti kalau sisa kuota weight menit ini turun di bawah
   ambang aman (dibaca dari header x-mbx-used-weight-1m milik Binance).
3. Kalau kena 429/418, siklus langsung dibatalkan dan dijadwal ulang jauh
   ke depan, bukan dicoba lagi.
4. Setiap panggilan diberi jeda, tidak pernah dikirim sekaligus.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import threading
import time
from typing import Callable, Optional

from market import market_scanner as scanner
from strategy import indicators as strategy
from strategy.indicators import Kline

logger = logging.getLogger("watchlist_auto")

WEIGHT_TICKER_ALL = 80
WEIGHT_BOOK_ALL = 4
WEIGHT_KLINES = 2
DEFAULT_WEIGHT_LIMIT = 6000

KLINE_PAGE = 1000


def _auto_file(config: dict) -> str:
    from config import config as cfg_mod
    mode = cfg_mod.get_mode(config).lower()
    return f"watchlist_auto_{mode}.json"


def to_klines(raw: list, now_ms: Optional[int] = None) -> list:
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    out = []
    for k in raw:
        try:
            close_time = int(k[6])
            if close_time >= now_ms:
                continue
            out.append(Kline(
                open_time=int(k[0]), open=float(k[1]), high=float(k[2]),
                low=float(k[3]), close=float(k[4]), volume=float(k[5]),
                close_time=close_time, quote_volume=float(k[7]),
            ))
        except (TypeError, ValueError, IndexError):
            continue
    return out


class Budget:

    def __init__(self, client, max_weight: int, pace_seconds: float,
                 min_headroom: float, weight_limit: int = DEFAULT_WEIGHT_LIMIT):
        self.client = client
        self.max_weight = max_weight
        self.pace = max(0.0, pace_seconds)
        self.min_headroom = min_headroom
        self.weight_limit = weight_limit
        self.spent = 0
        self.stopped_reason: Optional[str] = None

    def can_spend(self, weight: int) -> bool:
        if self.spent + weight > self.max_weight:
            self.stopped_reason = (
                f"anggaran weight siklus habis ({self.spent}/{self.max_weight})"
            )
            return False
        if self.client is not None:
            if getattr(self.client, "is_rate_limited", lambda: False)():
                self.stopped_reason = "IP sedang kena batas rate Binance"
                return False
            headroom = getattr(self.client, "weight_headroom", lambda l=None: 1.0)(
                self.weight_limit
            )
            if headroom < self.min_headroom:
                self.stopped_reason = (
                    f"sisa kuota menit ini tinggal {headroom*100:.0f}%, "
                    f"di bawah ambang aman {self.min_headroom*100:.0f}%"
                )
                return False
        return True

    def spend(self, weight: int) -> None:
        self.spent += weight
        if self.pace:
            time.sleep(self.pace)


def fetch_klines_paged(client, symbol: str, interval: str, bars: int,
                       budget: Budget) -> list:
    out: list = []
    end = None
    while len(out) < bars:
        if not budget.can_spend(WEIGHT_KLINES):
            break
        need = min(KLINE_PAGE, bars - len(out))
        chunk = client.get_klines(symbol, interval, limit=need, end_time_ms=end)
        budget.spend(WEIGHT_KLINES)
        if not chunk:
            break
        out = list(chunk) + out
        try:
            end = int(chunk[0][0]) - 1
        except (TypeError, ValueError, IndexError):
            break
        if len(chunk) < need:
            break
    return out


MONITORING_SPREAD_LIMIT_PCT = 0.25
MONITORING_LOOKBACK_BARS = 30


def evaluate_symbol(sym: str, kl: list, meta: dict, config: dict) -> Optional[dict]:
    interval = str(config.get("MARKET_DATA_INTERVAL", "5m"))
    interval_ms = strategy.interval_to_ms(interval)
    bars_per_day = max(1, round(24 * 60 * 60 * 1000 / interval_ms))
    need = MONITORING_LOOKBACK_BARS
    if len(kl) < bars_per_day + need + 10:
        return None

    min_vol = float(config.get("MIN_QUOTE_VOLUME_USDT_24H", 2_000_000))
    qv = [k.quote_volume for k in kl]
    n = len(kl)
    pre = [0.0]
    for value in qv:
        pre.append(pre[-1] + value)

    roll_vols = []
    gate_bars = 0
    for j in range(bars_per_day + need, n):
        vol24 = pre[j + 1] - pre[j + 1 - bars_per_day]
        roll_vols.append(vol24)
        if vol24 >= min_vol:
            gate_bars += 1
    if not roll_vols:
        return None

    days = n * interval_ms / 86_400_000
    uptime = sum(1 for value in roll_vols if value >= min_vol) / len(roll_vols) * 100.0
    vol_median = statistics.median(roll_vols)

    weekend_pct = None
    if days >= 10:
        import datetime as dt
        dow = [0.0] * 7
        for candle in kl:
            dow[dt.datetime.fromtimestamp(candle.open_time / 1000, dt.UTC).weekday()] += candle.quote_volume
        total = sum(dow)
        if total > 0:
            weekend_pct = (dow[5] + dow[6]) / total * 100.0

    spread = meta.get("spread_pct")
    ratio = vol_median / min_vol if min_vol else 0.0
    liquidity_part = uptime / 100.0 * 15.0 + min(10.0, (ratio ** 0.5) * 3.5)
    spread_part = 0.0 if spread is None else max(
        0.0, (1.0 - spread / MONITORING_SPREAD_LIMIT_PCT) * 20.0
    )
    monitoring_score = round(liquidity_part + spread_part, 1)

    notes = []
    if uptime < 60:
        notes.append(f"likuiditas di atas ambang hanya {uptime:.0f}% waktu")

    disqualified = None
    quote = str(config.get("QUOTE_ASSET", "USDT"))
    base = sym[:-len(quote)] if quote and sym.endswith(quote) else sym
    if base.endswith("B") and weekend_pct is not None and weekend_pct < 16.0:
        disqualified = "saham tokenisasi (bStocks), tidak berjalan 24/7"
    elif spread is not None and spread > MONITORING_SPREAD_LIMIT_PCT:
        disqualified = f"spread {spread:.3f}% melewati batas monitoring {MONITORING_SPREAD_LIMIT_PCT}%"

    tier = "INTI" if uptime >= 90 else ("AKTIF" if uptime >= 60 else "SPEKULATIF")
    note_parts = [f"spread {spread:.3f}%" if spread is not None else "spread tidak tersedia",
                  f"likuiditas {uptime:.0f}% waktu"]
    note_parts.extend(notes)
    return {
        "symbol": sym,
        "tier": tier,
        "monitoring_score": monitoring_score,
        "days": round(days, 1),
        "uptime": round(uptime, 1),
        "vol_median": vol_median,
        "spread_pct": spread,
        "weekend_pct": round(weekend_pct, 1) if weekend_pct is not None else None,
        "gate_pct": round(gate_bars / len(roll_vols) * 100, 2),
        "disqualified": disqualified,
        "notes": notes,
        "note": ", ".join(note_parts),
    }


def refresh_once(client, config: dict,
                 has_open_position: Optional[Callable[[], bool]] = None,
                 progress_cb: Optional[Callable[[str, float], None]] = None) -> dict:
    started = time.time()

    def prog(msg, frac):
        if progress_cb:
            try:
                progress_cb(msg, frac)
            except Exception:  # noqa: BLE001
                pass

    if has_open_position is not None:
        try:
            if has_open_position():
                return {"ok": False, "skipped": True,
                        "error": "dilewati: bot sedang memegang posisi terbuka"}
        except Exception:  # noqa: BLE001
            pass

    if client is None:
        return {"ok": False, "error": "klien Binance tidak tersedia"}
    if getattr(client, "is_rate_limited", lambda: False)():
        return {"ok": False, "error": "dilewati: IP sedang kena batas rate Binance"}

    max_symbols = int(config.get("WATCHLIST_AUTO_MAX_SYMBOLS", 60))
    days = int(config.get("WATCHLIST_AUTO_DAYS", 14))
    max_weight = int(config.get("WATCHLIST_AUTO_MAX_WEIGHT", 900))
    pace = float(config.get("WATCHLIST_AUTO_PACE_SECONDS", 2.0))
    min_headroom = float(config.get("WATCHLIST_AUTO_MIN_HEADROOM", 0.5))
    keep = int(config.get("WATCHLIST_AUTO_KEEP", 26))

    budget = Budget(client, max_weight, pace, min_headroom)

    try:
        if not budget.can_spend(WEIGHT_TICKER_ALL):
            return {"ok": False, "error": budget.stopped_reason or "anggaran habis"}
        prog("mengambil ticker 24 jam seluruh pasar...", 0.02)
        tickers = client.get_ticker_24hr_all()
        budget.spend(WEIGHT_TICKER_ALL)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"gagal ticker 24 jam: {str(exc)[:140]}"}

    spreads: dict = {}
    try:
        if budget.can_spend(WEIGHT_BOOK_ALL):
            prog("mengambil spread bid-ask...", 0.05)
            books = client.get_book_ticker_all()
            budget.spend(WEIGHT_BOOK_ALL)
            for b in books if isinstance(books, list) else []:
                try:
                    bid, ask = float(b["bidPrice"]), float(b["askPrice"])
                    if bid > 0 and ask > 0:
                        spreads[b["symbol"]] = scanner.spread_pct_from_book(bid, ask)
                except (KeyError, TypeError, ValueError):
                    continue
    except Exception as exc:  # noqa: BLE001
        logger.warning("bookTicker gagal, spread diabaikan: %s", exc)

    _daily_cache: dict = {}
    _daily_raw = scanner.make_daily_klines_fetcher(client, cache=_daily_cache)

    def _daily_fetcher(symbol: str):
        if symbol in _daily_cache:
            return _daily_cache[symbol]
        if not budget.can_spend(WEIGHT_KLINES):
            raise RuntimeError(budget.stopped_reason or "anggaran request habis")
        hasil = _daily_raw(symbol)
        budget.spend(WEIGHT_KLINES)
        return hasil

    ranked = scanner.filter_and_rank_candidates(
        tickers, dict(config), get_daily_klines_fn=_daily_fetcher)
    ranked.sort(key=lambda c: c.quote_volume, reverse=True)
    shortlist = ranked[:max_symbols]
    if not shortlist:
        return {"ok": False,
                "error": "tidak ada simbol lolos filter struktural dan gerbang pump "
                         "(naik 24 jam dan volume naik)"}

    interval = str(config.get("MARKET_DATA_INTERVAL", "5m"))
    if interval not in strategy.INTERVAL_MINUTES:
        return {"ok": False, "error": f"interval watchlist tidak didukung: {interval}"}
    interval_ms = strategy.interval_to_ms(interval)
    bars_per_day = max(1, round(86_400_000 / interval_ms))
    bars = days * bars_per_day + MONITORING_LOOKBACK_BARS + 2
    evaluated: list = []
    failed = 0

    for i, cand in enumerate(shortlist):
        if has_open_position is not None:
            try:
                if has_open_position():
                    budget.stopped_reason = "bot membuka posisi di tengah penyegaran"
                    break
            except Exception:  # noqa: BLE001
                pass
        if not budget.can_spend(WEIGHT_KLINES):
            break
        prog(f"menilai {cand.symbol} ({i+1}/{len(shortlist)})...",
             0.05 + 0.9 * (i + 1) / len(shortlist))
        try:
            raw = fetch_klines_paged(client, cand.symbol, interval, bars, budget)
            kl = to_klines(raw)
            meta = {"spread_pct": spreads.get(cand.symbol)}
            row = evaluate_symbol(cand.symbol, kl, meta, config)
            if row:
                evaluated.append(row)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("gagal menilai %s: %s", cand.symbol, str(exc)[:120])
            if "429" in str(exc) or "418" in str(exc):
                budget.stopped_reason = "kena batas rate, siklus dihentikan"
                break

    ok_rows = [r for r in evaluated if not r["disqualified"]]
    ok_rows.sort(key=lambda r: r.get("monitoring_score", 0.0), reverse=True)

    inti = [r for r in ok_rows if r["tier"] == "INTI"][:12]
    aktif = [r for r in ok_rows if r["tier"] == "AKTIF"][:8]
    spek = [r for r in ok_rows if r["tier"] == "SPEKULATIF"][:6]
    final = (inti + aktif + spek)[:keep]

    result = {
        "ok": bool(final),
        "generated_at": int(time.time()),
        "duration_seconds": round(time.time() - started, 1),
        "weight_spent": budget.spent,
        "weight_budget": max_weight,
        "symbols_examined": len(evaluated),
        "symbols_requested": len(shortlist),
        "symbols_failed": failed,
        "days": days,
        "stopped_reason": budget.stopped_reason,
        "items": [{"symbol": r["symbol"], "tier": r["tier"],
                   "monitoring_score": r.get("monitoring_score", 0.0),
                   "note": r["note"]} for r in final],
        "detail": final,
    }
    if not final:
        result["error"] = budget.stopped_reason or "tidak ada simbol lolos penilaian"
    return result


def save_result(result: dict, config: dict) -> Optional[str]:
    try:
        path = _auto_file(config)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return path
    except OSError as exc:
        logger.error("gagal menyimpan hasil watchlist otomatis: %s", exc)
        return None


def load_result(config: dict) -> Optional[dict]:
    path = _auto_file(config)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not (isinstance(data, dict) and data.get("items")):
            return None
        for row in data.get("items", []) + data.get("detail", []):
            if isinstance(row, dict):
                row.pop("score", None)
                row.pop("signals", None)
                row.pop("signals_per_30d", None)
        return data
    except (json.JSONDecodeError, OSError):
        return None


class AutoRefresher:

    def __init__(self, client_getter: Callable, config: dict,
                 has_open_position: Optional[Callable[[], bool]] = None):
        self.client_getter = client_getter
        self.config = config
        self.has_open_position = has_open_position
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.status = {"state": "idle", "message": "belum berjalan",
                       "progress": 0.0, "last_run": None, "next_run": None,
                       "last_error": None}
        self._lock = threading.Lock()

    def _set(self, **kw):
        with self._lock:
            self.status.update(kw)

    def get_status(self) -> dict:
        with self._lock:
            return dict(self.status)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="watchlist-auto")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        interval = max(1, int(self.config.get("WATCHLIST_AUTO_INTERVAL_HOURS", 6))) * 3600
        delay = max(0, int(self.config.get("WATCHLIST_AUTO_STARTUP_DELAY_SECONDS", 60)))

        prev = load_result(self.config)
        if prev:
            age = time.time() - prev.get("generated_at", 0)
            if age < interval:
                delay = max(delay, int(interval - age))
                self._set(message=f"memakai hasil sebelumnya, umur {age/3600:.1f} jam")

        self._set(next_run=int(time.time() + delay))
        if self._stop.wait(delay):
            return

        while not self._stop.is_set():
            self._set(state="running", message="menyiapkan...", progress=0.0)
            try:
                res = refresh_once(
                    self.client_getter(), self.config,
                    has_open_position=self.has_open_position,
                    progress_cb=lambda m, f: self._set(message=m, progress=f),
                )
                if res.get("ok"):
                    save_result(res, self.config)
                    self._set(state="idle", progress=1.0,
                              message=(f"selesai: {len(res['items'])} simbol, "
                                       f"{res['weight_spent']} weight, "
                                       f"{res['duration_seconds']:.0f} detik"),
                              last_run=res["generated_at"], last_error=None)
                else:
                    self._set(state="idle", progress=0.0,
                              message=res.get("error", "gagal"),
                              last_error=res.get("error"))
            except Exception as exc:  # noqa: BLE001
                logger.exception("siklus penyegaran watchlist gagal")
                self._set(state="idle", message=f"error: {str(exc)[:140]}",
                          last_error=str(exc)[:140])

            nxt = int(time.time() + interval)
            self._set(next_run=nxt)
            if self._stop.wait(interval):
                return
