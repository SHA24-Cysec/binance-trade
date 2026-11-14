from __future__ import annotations

from typing import Callable, Optional

from strategy import entry_rule
from strategy import indicators as strategy
from strategy import vidya
from strategy.indicators import Kline
from market import market_scanner as scanner
from backtesting import portfolio_backtest as pb
from backtesting.backtest import bars_per_day, compute_rolling_24h_stats
from backtesting.backtest_storage import KlineStore


def _turnover_array(klines: list[Kline]) -> list[float]:
    out = []
    for k in klines:
        v = float(k.quote_volume or 0.0)
        if v <= 0.0:
            v = float(k.close) * float(k.volume)
        out.append(max(0.0, v))
    return out


def _volume_ratio_at(cum: list[float], i: int, per_day: int, days: int, lookback: int) -> Optional[float]:
    available = lookback // per_day - 1
    if per_day <= 0 or days <= 0 or available < 1:
        return None
    count = min(days, available)
    span = per_day * (count + 1)
    start = i + 1 - span
    if start < 0:
        return None
    current = cum[i + 1] - cum[i + 1 - per_day]
    past = []
    for j in range(count):
        a = start + j * per_day
        past.append(cum[a + per_day] - cum[a])
    avg = sum(past) / count
    if avg <= 0.0:
        return None
    return current / avg


def precompute_vidya_signals(
    store: KlineStore,
    config: dict,
    interval: str,
    warmup_ms: int = 0,
    progress_cb: Optional[Callable[[float], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> tuple:
    tradable_meta = config.get("_historical_tradable_symbols")
    symbols = [s for s in store.symbols() if scanner.is_structurally_allowed_symbol(s, config, tradable_meta)]
    prebuilt = pb.build_timeline(store, interval, symbols, config, include_ohlcv=False)
    timeline, series_of, deret_of = pb.unpack_prebuilt(prebuilt)
    cache = pb.EntrySignalCache({}, exit_signature=pb.exit_level_signature(config))
    if not symbols or not timeline:
        return cache, prebuilt

    lookback = strategy.confirm_window_bars(config)
    min_vol = float(config.get("MIN_QUOTE_VOLUME_USDT_24H", 0) or 0)
    per_day = bars_per_day(interval)
    vol_params = entry_rule.volume_rise_params(config)
    atr_period = entry_rule.atr_period(config)
    vp = vidya.vidya_params(config)
    min_ratio = vp["delta_min_ratio"]
    trigger = vp["entry_trigger"]
    required = entry_rule.required_bars(config)
    first_allowed = timeline[0] + int(warmup_ms or 0)
    by_time: dict[int, list] = {}
    total = len(symbols)

    for si, sym in enumerate(symbols):
        if cancel_cb is not None and si % 5 == 0 and cancel_cb():
            raise RuntimeError("Precompute VIDYA dibatalkan.")
        kl = store.klines(sym)
        n = len(kl)
        if n < required:
            continue
        series = vidya.compute_series(kl, config)
        highs = [k.high for k in kl]
        lows = [k.low for k in kl]
        closes = [k.close for k in kl]
        atr14 = vidya._wilder_atr(highs, lows, closes, atr_period)
        stats = compute_rolling_24h_stats(kl, per_day)
        tv = _turnover_array(kl)
        cum = [0.0]
        for v in tv:
            cum.append(cum[-1] + v)
        start_i = max(lookback - 1, required - 1)
        for i in range(start_i, n):
            ot = int(kl[i].open_time)
            ct = int(kl[i].close_time)
            if ot < first_allowed:
                continue
            st = stats[i]
            if st is None or st["vol24h"] < min_vol:
                continue
            if not scanner.market_gate_ok_at(st["pct24h"], st["vol24h"], config):
                continue
            cache.entry_rule_scans += 1
            cache.entry_rule_scans_by_time[ot] = cache.entry_rule_scans_by_time.get(ot, 0) + 1
            ratio = None
            if vol_params["enabled"] and vol_params["mult"] > 0.0:
                ratio = _volume_ratio_at(cum, i, per_day, vol_params["days"], lookback)
                if ratio is not None and ratio + 1e-12 < vol_params["mult"]:
                    cache.entry_rule_skips += 1
                    cache.entry_rule_skips_by_time[ot] = cache.entry_rule_skips_by_time.get(ot, 0) + 1
                    continue
            trig = series["pullback_entry"] if trigger == "PULLBACK" else series["cross_up"]
            if not series["ready"][i] or not trig[i]:
                cache.entry_rule_skips += 1
                cache.entry_rule_skips_by_time[ot] = cache.entry_rule_skips_by_time.get(ot, 0) + 1
                continue
            dr = series["delta_ratio"][i]
            if dr is None or dr < min_ratio:
                cache.entry_rule_skips += 1
                cache.entry_rule_skips_by_time[ot] = cache.entry_rule_skips_by_time.get(ot, 0) + 1
                continue
            bar = kl[i]
            entry = float(bar.close)
            anchor_mode = entry_rule.vidya_sl_anchor(config)
            if anchor_mode == "SIGNAL_LOW":
                anchor = float(bar.low)
            elif anchor_mode == "VIDYA_LINE":
                anchor = float(series["vidya"][i])
            else:
                anchor = float(series["lower"][i])
            if anchor <= 0.0 or anchor >= entry:
                anchor = float(bar.low)
            atr = atr14[i]
            levels = entry_rule.exit_levels(entry, anchor, config, atr=atr)
            if not levels["ok"]:
                cache.entry_rule_skips += 1
                cache.entry_rule_skips_by_time[ot] = cache.entry_rule_skips_by_time.get(ot, 0) + 1
                continue
            setup = scanner.SetupResult(
                ok=True, reason=f"VIDYA {trigger} + delta volume beli", stage="OK",
                signal_close=entry, signal_close_time=ct, atr_value=atr,
                entry_interval=interval, bull_count=1, bull_required=1,
                entry_low=anchor, sl_price=levels["sl_price"], tp_price=levels["tp_price"],
                sl_pct=levels["sl_pct"], tp_pct=levels["tp_pct"], rr_ratio=levels["rr_ratio"],
                turnover_ratio=ratio, ema_fast=series["vidya"][i], ema_slow=series["lower"][i],
            )
            by_time.setdefault(ot, []).append((st["pct24h"], st["vol24h"], sym, i, setup))
        if progress_cb:
            progress_cb((si + 1) / total)

    for ot, rows in by_time.items():
        rows.sort(key=lambda x: (-x[0], -x[1], x[2]))
        cache.signals[ot] = [(sym, i, vol24, setup) for (_pct, vol24, sym, i, setup) in rows]
    if progress_cb:
        progress_cb(1.0)
    return cache, prebuilt
