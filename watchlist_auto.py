"""Penyegar watchlist otomatis (READ-ONLY terhadap keputusan trading).

Modul ini menyusun ulang DAFTAR simbol watchlist secara berkala dari data
Binance terbaru, memakai metodologi yang sama dengan daftar bawaan di
config.py, tapi dengan ruang lingkup yang dikecilkan supaya aman dijalankan
berdampingan dengan bot yang sedang live.

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

import market_scanner as scanner
from strategy import Kline, atr_percent

logger = logging.getLogger("watchlist_auto")

# Weight resmi tiap endpoint (dicek 2026-09-24).
WEIGHT_TICKER_ALL = 80
WEIGHT_BOOK_ALL = 4
WEIGHT_KLINES = 2
DEFAULT_WEIGHT_LIMIT = 6000

BARS_PER_DAY_5M = 288
KLINE_PAGE = 1000


# ======================================================================
# Utilitas
# ======================================================================
def _auto_file(config: dict) -> str:
    """Nama file hasil, dipisah per mode supaya TESTNET dan LIVE tidak campur."""
    import config as cfg_mod
    mode = cfg_mod.get_mode(config).lower()
    return f"watchlist_auto_{mode}.json"


def to_klines(raw: list) -> list:
    out = []
    for k in raw:
        try:
            out.append(Kline(
                open_time=int(k[0]), open=float(k[1]), high=float(k[2]),
                low=float(k[3]), close=float(k[4]), volume=float(k[5]),
                close_time=int(k[6]), quote_volume=float(k[7]),
            ))
        except (TypeError, ValueError, IndexError):
            continue
    return out


class Budget:
    """Penjaga anggaran weight dengan jeda antar panggilan.

    Tujuannya bukan sekadar menghitung, tapi memastikan penyegaran BERHENTI
    sendiri sebelum mengganggu bot, bukan setelah.
    """

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


# ======================================================================
# Pengambilan data
# ======================================================================
def fetch_klines_paged(client, symbol: str, interval: str, bars: int,
                       budget: Budget) -> list:
    """Ambil candle dengan paging mundur, berhenti kalau anggaran habis."""
    out: list = []
    end = None
    while len(out) < bars:
        if not budget.can_spend(WEIGHT_KLINES):
            break
        need = min(KLINE_PAGE, bars - len(out))
        # Perhatikan nama parameternya: klien repo ini memakai end_time_ms
        # (snake_case), bukan endTime seperti nama field mentah Binance.
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


# ======================================================================
# Penilaian
# ======================================================================
def score_symbol(sym: str, kl: list, meta: dict, config: dict) -> Optional[dict]:
    """Nilai satu simbol memakai logika keputusan bot yang asli.

    confirm_entry() dan atr_percent() yang dipanggil di sini adalah fungsi
    yang sama persis dengan yang dipakai bot live, jadi angka jumlah sinyal
    bukan perkiraan melainkan hasil menjalankan logika bot itu sendiri.
    """
    lookback = int(config.get("CONFIRM_LOOKBACK_BARS", 20))
    atr_period = int(config.get("ATR_PERIOD", 14))
    min_pump = float(config.get("MIN_PUMP_PCT_24H", 13.0))
    min_vol = float(config.get("MIN_QUOTE_VOLUME_USDT_24H", 2_000_000))
    need = max(lookback, atr_period + 1, 20)

    if len(kl) < BARS_PER_DAY_5M + need + 10:
        return None

    closes = [k.close for k in kl]
    qv = [k.quote_volume for k in kl]
    n = len(kl)

    pre = [0.0]
    for v in qv:
        pre.append(pre[-1] + v)

    signals = 0
    gate_bars = 0
    atrs: list = []
    roll_vols: list = []
    last_sig = -10 ** 9
    # Bot hanya memegang satu posisi dan MAX_HOLD_MINUTES default 45 menit
    # (9 bar 5 menit). Sinyal yang terlalu berdekatan tidak mungkin jadi
    # trade terpisah, jadi digabung supaya jumlahnya tidak dilebih-lebihkan.
    min_gap = max(1, int(config.get("MAX_HOLD_MINUTES", 45)) // 5)

    for j in range(BARS_PER_DAY_5M + need, n):
        vol24 = pre[j + 1] - pre[j + 1 - BARS_PER_DAY_5M]
        roll_vols.append(vol24)
        prev = closes[j - BARS_PER_DAY_5M]
        if prev <= 0:
            continue
        chg = (closes[j] / prev - 1.0) * 100.0
        if chg < min_pump or vol24 < min_vol:
            continue
        gate_bars += 1

        ok, _ = scanner.confirm_entry(kl[j - lookback + 1: j + 1], config)
        if not ok or (j - last_sig) < min_gap:
            continue
        last_sig = j
        signals += 1
        a = atr_percent(kl[max(0, j - (atr_period + 5)): j + 1], period=atr_period)
        if a:
            atrs.append(a)

    if not roll_vols:
        return None

    days = n * 5 / 60 / 24
    uptime = sum(1 for v in roll_vols if v >= min_vol) / len(roll_vols) * 100.0
    vol_median = statistics.median(roll_vols)
    atr_med = statistics.median(atrs) if atrs else None

    # Deteksi instrumen yang bukan crypto 24/7 (saham tokenisasi bStocks).
    # Crypto spot berjalan terus, jadi porsi volume Sabtu+Minggu wajar di
    # kisaran 20-28%. Saham tokenisasi anjlok jauh di bawah itu karena
    # harganya ditambatkan ke bursa AS yang tutup akhir pekan.
    weekend_pct = None
    if days >= 10:
        import datetime as dt
        dow = [0.0] * 7
        for k in kl:
            dow[dt.datetime.fromtimestamp(k.open_time / 1000, dt.UTC).weekday()] += k.quote_volume
        tot = sum(dow)
        if tot > 0:
            weekend_pct = (dow[5] + dow[6]) / tot * 100.0

    return _compose_score(sym, meta, config, signals, days, uptime, vol_median,
                          atr_med, weekend_pct, gate_bars, n)


def _compose_score(sym, meta, config, signals, days, uptime, vol_median,
                   atr_med, weekend_pct, gate_bars, n) -> dict:
    """Gabungkan komponen jadi skor 0-100 (bobot sama dengan daftar bawaan)."""
    max_spread = float(config.get("MAX_SPREAD_PCT", 0.25))
    min_vol = float(config.get("MIN_QUOTE_VOLUME_USDT_24H", 2_000_000))
    sl_min = float(config.get("ATR_SL_MIN_PCT", 1.2))
    sl_max = float(config.get("ATR_SL_MAX_PCT", 4.0))
    atr_mult = float(config.get("ATR_MULTIPLIER_SL", 2.0))

    spread = meta.get("spread_pct")
    sig30 = (signals / days * 30.0) if days > 0 else 0.0

    # A. frekuensi sinyal (35)
    s_sig = min(35.0, sig30 / 25.0 * 35.0)
    # B. likuiditas (25)
    ratio = vol_median / min_vol if min_vol else 0
    s_liq = uptime / 100.0 * 15.0 + min(10.0, (ratio ** 0.5) * 3.5)
    # C. spread (20)
    s_spread = 0.0 if spread is None else max(0.0, (1.0 - spread / max_spread) * 20.0)
    # D. kecocokan ATR dengan rentang SL (20)
    notes = []
    if atr_med is None:
        s_atr = 0.0
        notes.append("ATR tidak terukur")
    else:
        raw_sl = atr_mult * atr_med
        if raw_sl < sl_min:
            s_atr = max(0.0, 20.0 * (raw_sl / sl_min) * 0.6)
            notes.append(f"ATR rendah, SL sering dipaksa ke lantai {sl_min}%")
        elif raw_sl > sl_max:
            s_atr = max(0.0, 20.0 * (sl_max / raw_sl) * 0.6)
            notes.append(f"ATR tinggi, SL sering kena plafon {sl_max}%")
        else:
            pos = (raw_sl - sl_min) / (sl_max - sl_min)
            s_atr = 20.0 * (1.0 - abs(pos - 0.35) / 0.65 * 0.35)

    if uptime < 60:
        notes.append(f"likuiditas di atas ambang bot hanya {uptime:.0f}% waktu")

    # Diskualifikasi struktural
    dq = None
    base = sym[:-4] if sym.endswith("USDT") else sym
    if base.endswith("B") and weekend_pct is not None and weekend_pct < 16.0:
        dq = "saham tokenisasi (bStocks), tidak berjalan 24/7"
    elif spread is not None and spread > max_spread:
        dq = f"spread {spread:.3f}% melewati MAX_SPREAD_PCT {max_spread}%"
    elif signals < 1:
        dq = f"tidak ada sinyal dalam {days:.0f} hari"

    tier = "INTI" if uptime >= 90 else ("MOMENTUM" if uptime >= 60 else "SPEKULATIF")

    return {
        "symbol": sym, "tier": tier,
        "score": round(s_sig + s_liq + s_spread + s_atr, 1),
        "signals": signals, "signals_per_30d": round(sig30, 2),
        "days": round(days, 1), "uptime": round(uptime, 1),
        "vol_median": vol_median, "atr_pct": round(atr_med, 3) if atr_med else None,
        "spread_pct": spread, "weekend_pct": round(weekend_pct, 1) if weekend_pct else None,
        "gate_pct": round(gate_bars / n * 100, 2) if n else 0,
        "disqualified": dq, "notes": notes,
        "note": _build_note(signals, days, uptime, atr_med, spread, notes),
    }


def _build_note(signals, days, uptime, atr_med, spread, notes) -> str:
    bits = [f"{signals} sinyal/{days:.0f}h"]
    if atr_med:
        bits.append(f"ATR {atr_med:.2f}%")
    if spread is not None:
        bits.append(f"spread {spread:.3f}%")
    bits.append(f"likuiditas {uptime:.0f}% waktu")
    if notes:
        bits.append(notes[0])
    return ", ".join(bits)


# ======================================================================
# Siklus penyegaran
# ======================================================================
def refresh_once(client, config: dict,
                 has_open_position: Optional[Callable[[], bool]] = None,
                 progress_cb: Optional[Callable[[str, float], None]] = None) -> dict:
    """Jalankan SATU siklus penyegaran. Return ringkasan hasil.

    Tidak pernah melempar exception ke pemanggil: semua kegagalan
    dikembalikan sebagai dict berisi "error", karena ini proses latar
    yang tidak boleh menjatuhkan dashboard.
    """
    started = time.time()

    def prog(msg, frac):
        if progress_cb:
            try:
                progress_cb(msg, frac)
            except Exception:  # noqa: BLE001
                pass

    # --- REM 1: jangan pernah bersaing dengan bot yang sedang pegang posisi ---
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

    # --- Tahap 1: ticker + bookTicker (2 panggilan untuk seluruh pasar) ---
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
            # Metadata dipanggil lewat metode PUBLIK klien (perbaikan audit
            # temuan R-06): memanggil _request (API privat) dari modul lain
            # membuat watchlist rawan patah diam-diam kalau internal klien
            # berubah.
            books = client.get_book_ticker_all()
            budget.spend(WEIGHT_BOOK_ALL)
            for b in books if isinstance(books, list) else []:
                try:
                    bid, ask = float(b["bidPrice"]), float(b["askPrice"])
                    if bid > 0 and ask > 0:
                        spreads[b["symbol"]] = (ask - bid) / ask * 100.0
                except (KeyError, TypeError, ValueError):
                    continue
    except Exception as exc:  # noqa: BLE001
        logger.warning("bookTicker gagal, spread diabaikan: %s", exc)

    # --- Tahap 2: pilih kandidat paling likuid ---
    # Ambang pump di-nolkan khusus untuk pemilihan semesta, sama seperti
    # portfolio_backtest.select_universe(): kita butuh koin yang pernah pump
    # KAPAN SAJA dalam periode, bukan yang kebetulan naik hari ini.
    cfg_universe = dict(config)
    cfg_universe["MIN_PUMP_PCT_24H"] = -1e9
    ranked = scanner.filter_and_rank_candidates(tickers, cfg_universe)
    ranked.sort(key=lambda c: c.quote_volume, reverse=True)
    shortlist = ranked[:max_symbols]
    if not shortlist:
        return {"ok": False, "error": "tidak ada simbol lolos filter struktural"}

    # --- Tahap 3: unduh candle & nilai ---
    bars = days * BARS_PER_DAY_5M
    interval = str(config.get("CONFIRM_INTERVAL", "5m"))
    scored: list = []
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
            row = score_symbol(cand.symbol, kl, meta, config)
            if row:
                scored.append(row)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("gagal menilai %s: %s", cand.symbol, str(exc)[:120])
            if "429" in str(exc) or "418" in str(exc):
                budget.stopped_reason = "kena batas rate, siklus dihentikan"
                break

    ok_rows = [r for r in scored if not r["disqualified"]]
    ok_rows.sort(key=lambda r: r["score"], reverse=True)

    # Jaga komposisi tier supaya panel tidak didominasi satu jenis koin.
    inti = [r for r in ok_rows if r["tier"] == "INTI"][:12]
    mom = [r for r in ok_rows if r["tier"] == "MOMENTUM"][:8]
    spek = [r for r in ok_rows if r["tier"] == "SPEKULATIF"][:6]
    final = (inti + mom + spek)[:keep]

    result = {
        "ok": bool(final),
        "generated_at": int(time.time()),
        "duration_seconds": round(time.time() - started, 1),
        "weight_spent": budget.spent,
        "weight_budget": max_weight,
        "symbols_examined": len(scored),
        "symbols_requested": len(shortlist),
        "symbols_failed": failed,
        "days": days,
        "stopped_reason": budget.stopped_reason,
        "items": [{"symbol": r["symbol"], "tier": r["tier"], "score": r["score"],
                   "note": r["note"]} for r in final],
        "detail": final,
    }
    if not final:
        result["error"] = budget.stopped_reason or "tidak ada simbol lolos penilaian"
    return result


def save_result(result: dict, config: dict) -> Optional[str]:
    """Tulis hasil ke file JSON secara atomik (tulis sementara lalu rename)."""
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
    """Baca hasil terakhir. Return None kalau belum ada atau rusak."""
    path = _auto_file(config)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("items") else None
    except (json.JSONDecodeError, OSError):
        return None


# ======================================================================
# Penjadwal latar
# ======================================================================
class AutoRefresher:
    """Menjalankan refresh_once() berkala di thread latar (daemon).

    Thread daemon dipilih supaya menutup dashboard tidak pernah tertahan
    menunggu siklus selesai.
    """

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

        # Jeda awal: jangan menambah beban tepat saat dashboard dan bot
        # sama-sama baru dinyalakan.
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
