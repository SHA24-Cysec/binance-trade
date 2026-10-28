from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from strategy import confluence_code as cc
from strategy.indicators import INTERVAL_MINUTES, Kline

DATA_HOST = "https://data-api.binance.vision"
SESSION = requests.Session()


def ambil_klines(symbol: str, interval: str, limit: int = 500) -> list[Kline]:
    respon = SESSION.get(
        f"{DATA_HOST}/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": int(limit)},
        timeout=20,
    )
    respon.raise_for_status()
    baris = respon.json()
    hasil: list[Kline] = []
    for item in baris:
        hasil.append(
            Kline(
                int(item[0]),
                float(item[1]),
                float(item[2]),
                float(item[3]),
                float(item[4]),
                int(item[6]),
                float(item[5]),
                float(item[7]),
            )
        )
    return hasil


def daftar_simbol(min_quote_volume: float, batas: int) -> list[str]:
    respon = SESSION.get(f"{DATA_HOST}/api/v3/ticker/24hr", timeout=25)
    respon.raise_for_status()
    kandidat = []
    for item in respon.json():
        symbol = str(item.get("symbol", ""))
        if not symbol.endswith("USDT"):
            continue
        if any(symbol.endswith(s) for s in ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")):
            continue
        try:
            volume = float(item.get("quoteVolume", 0) or 0)
            harga = float(item.get("lastPrice", 0) or 0)
        except (TypeError, ValueError):
            continue
        if volume < min_quote_volume or harga <= 0:
            continue
        kandidat.append((volume, symbol))
    kandidat.sort(reverse=True)
    return [symbol for _, symbol in kandidat[:batas]]


def ukur_hasil_maju(
    klines: list[Kline],
    index: int,
    entry: float,
    tp: float,
    sl: float,
    horizon: int,
) -> dict:
    batas = min(len(klines), index + 1 + horizon)
    mfe = 0.0
    mae = 0.0
    hasil = "TERBUKA"
    for posisi in range(index + 1, batas):
        tinggi = float(klines[posisi].high)
        rendah = float(klines[posisi].low)
        mfe = max(mfe, (tinggi - entry) / entry * 100.0)
        mae = max(mae, (entry - rendah) / entry * 100.0)
        if rendah <= sl and tinggi >= tp:
            hasil = "AMBIGU"
            break
        if rendah <= sl:
            hasil = "SL"
            break
        if tinggi >= tp:
            hasil = "TP"
            break
    return {"mfe_pct": round(mfe, 2), "mae_pct": round(mae, 2), "hasil": hasil}


def evaluasi_simbol(
    symbol: str,
    klines: list[Kline],
    config: dict,
    warmup: int,
    horizon: int = 24,
) -> tuple[dict, Counter, list[dict]]:
    tahap: Counter = Counter()
    lolos: list[dict] = []
    ringkas = {
        "symbol": symbol,
        "bars": len(klines),
        "evaluasi": 0,
        "zona_terbentuk": 0,
        "trigger_valid": 0,
        "skor_tertinggi": 0.0,
    }
    if len(klines) <= warmup + 1:
        return ringkas, tahap, lolos

    for index in range(warmup, len(klines)):
        irisan = klines[:index]
        struktur = cc.evaluate_confluence_zone(irisan, config)
        ringkas["evaluasi"] += 1
        tahap[str(struktur.get("stage"))] += 1
        if not struktur.get("ok"):
            continue
        ringkas["zona_terbentuk"] += 1
        skor = float(struktur.get("score") or 0.0)
        ringkas["skor_tertinggi"] = max(ringkas["skor_tertinggi"], skor)
        sinyal = klines[index]
        reaksi = cc.evaluate_confluence_reaction(
            klines[: index + 1], config, struktur
        )
        if not reaksi.get("ok"):
            tahap[f"TRIGGER_{reaksi.get('stage')}"] += 1
            continue
        ringkas["trigger_valid"] += 1
        sl = float(struktur["poi_zone_low"]) * (1.0 - float(config["CONFLUENCE_SL_BUFFER_PCT"]) / 100.0)
        tp = float(struktur["bsl_level"] or sinyal.close)
        maju = ukur_hasil_maju(klines, index, float(sinyal.close), tp, sl, horizon)
        lolos.append(
            {
                "symbol": symbol,
                "sl": round(sl, 8),
                "tp": round(tp, 8),
                **maju,
                "open_time": int(sinyal.open_time),
                "close": float(sinyal.close),
                "poi_low": float(struktur["poi_zone_low"]),
                "poi_high": float(struktur["poi_zone_high"]),
                "score": round(skor, 1),
                "ssl": float(struktur["ssl_level"] or 0.0),
                "bsl": float(struktur["bsl_level"] or 0.0),
                "impulse_pct": round(float(struktur["impulse_pct"] or 0.0), 2),
            }
        )
    return ringkas, tahap, lolos


def jalankan(
    interval: str,
    batas: int,
    min_volume: float,
    bars: int,
    lookback: int,
    simbol_manual: Iterable[str] | None = None,
    horizon: int = 24,
) -> dict:
    config = dict(cc.CONFLUENCE_CODE_DEFAULTS)
    config["CONFLUENCE_STRUCTURE_INTERVAL"] = interval
    config["CONFLUENCE_LOOKBACK_BARS"] = lookback
    warmup = lookback + 5

    simbol = list(simbol_manual) if simbol_manual else daftar_simbol(min_volume, batas)
    total_tahap: Counter = Counter()
    semua_lolos: list[dict] = []
    per_simbol: list[dict] = []

    for symbol in simbol:
        try:
            klines = ambil_klines(symbol, interval, bars)
        except Exception as exc:
            per_simbol.append({"symbol": symbol, "error": str(exc)[:120]})
            continue
        ringkas, tahap, lolos = evaluasi_simbol(symbol, klines, config, warmup, horizon)
        per_simbol.append(ringkas)
        total_tahap.update(tahap)
        semua_lolos.extend(lolos)

    semua_lolos.sort(key=lambda item: item["open_time"], reverse=True)
    hitung_hasil = Counter(item["hasil"] for item in semua_lolos)
    mfe_rata = round(sum(i["mfe_pct"] for i in semua_lolos) / len(semua_lolos), 2) if semua_lolos else 0.0
    mae_rata = round(sum(i["mae_pct"] for i in semua_lolos) / len(semua_lolos), 2) if semua_lolos else 0.0
    total_evaluasi = sum(int(item.get("evaluasi", 0)) for item in per_simbol)
    total_zona = sum(int(item.get("zona_terbentuk", 0)) for item in per_simbol)
    return {
        "interval": interval,
        "lookback": lookback,
        "simbol": len(simbol),
        "total_evaluasi": total_evaluasi,
        "zona_terbentuk": total_zona,
        "trigger_valid": len(semua_lolos),
        "rasio_zona_persen": round(total_zona / total_evaluasi * 100, 3) if total_evaluasi else 0.0,
        "rasio_trigger_persen": round(len(semua_lolos) / total_evaluasi * 100, 3) if total_evaluasi else 0.0,
        "horizon_bars": horizon,
        "hasil_maju": dict(hitung_hasil),
        "mfe_rata_persen": mfe_rata,
        "mae_rata_persen": mae_rata,
        "tahap_penolakan": dict(total_tahap.most_common()),
        "per_simbol": per_simbol,
        "sinyal": semua_lolos[:40],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe The Confluence Code pada data Binance asli")
    parser.add_argument("--interval", default="4h")
    parser.add_argument("--symbols", type=int, default=25)
    parser.add_argument("--bars", type=int, default=500)
    parser.add_argument("--min-volume", type=float, default=50_000_000.0)
    parser.add_argument("--lookback", type=int, default=120)
    parser.add_argument("--out", default="")
    parser.add_argument("--list", default="")
    parser.add_argument("--horizon", type=int, default=24)
    args = parser.parse_args()

    if args.interval not in INTERVAL_MINUTES:
        print(f"interval tidak dikenal: {args.interval}", file=sys.stderr)
        return 2

    manual = [s.strip().upper() for s in args.list.split(",") if s.strip()] or None
    hasil = jalankan(
        args.interval,
        args.symbols,
        args.min_volume,
        args.bars,
        args.lookback,
        manual,
        args.horizon,
    )
    teks = json.dumps(hasil, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(teks, encoding="utf-8")
    print(teks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
