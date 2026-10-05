from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from trading.clients.binance_client import BinanceSpotClient
from config.config import get_base_url, use_websocket

logger = logging.getLogger("market_data")


class MarketDataProvider:

    def __init__(self, config: dict) -> None:
        self.config = config
        base_url = get_base_url(config)
        self.rest = BinanceSpotClient(
            "",
            "",
            base_url,
            allow_signed=False,
            rate_limit_state_file=config.get("RATE_LIMIT_STATE_FILE"),
            rate_limit_limit=int(config.get("RATE_LIMIT_WEIGHT_LIMIT", 6000) or 6000),
            rate_limit_safety_margin=int(
                config.get("RATE_LIMIT_SAFETY_MARGIN", 100) or 100
            ),
        )

        self._use_ws = use_websocket(config)
        self._max_age = float(config.get("MAX_MARKET_DATA_AGE_SECONDS", 10.0))
        self._depth_limit = int(config.get("PAPER_DEPTH_LIMIT", 100))

        self._workers = max(1, int(config.get("MARKET_DATA_WORKERS", 1) or 1))
        self._ws_overlay_enabled = bool(
            config.get("WS_LAST_PRICE_OVERLAY_ENABLED", True)
        )
        self._ticker_ttl = max(
            0.0, float(config.get("TICKER_SNAPSHOT_TTL_SECONDS", 0) or 0)
        )

        self._ticker_lock = threading.RLock()
        self._ticker_snapshot: list = []
        self._ticker_snapshot_ts = 0.0
        self._ticker_refresher: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._ei_lock = threading.RLock()
        self._exchange_info: Optional[dict] = None
        self._exchange_info_ts = 0.0
        self._ei_refresh_seconds = 6 * 3600.0

        self._depth_lock = threading.RLock()
        self._depth_cache: dict[str, tuple[dict, float]] = {}
        self._depth_cache_ttl = 1.0

        self._ws = None
        self._ws_lock = threading.RLock()
        self._subscribed: set[str] = set()

    def _ensure_ws(self):
        if not self._use_ws:
            return None
        with self._ws_lock:
            if self._ws is None:
                try:
                    from market.market_ws import MarketWebSocket

                    self._ws = MarketWebSocket(
                        self.config.get("WS_BASE_URL", "wss://stream.binance.com:9443")
                    )
                    self._ws.start(all_mini_ticker=True)
                    logger.info("Lapisan data pasar: WebSocket AKTIF (hybrid).")
                except Exception as exc:
                    logger.warning(
                        "Gagal memulai WebSocket (%s). Fallback REST penuh.", exc
                    )
                    self._use_ws = False
                    self._ws = None
            return self._ws

    def _ensure_symbol_stream(self, symbol: str) -> None:
        ws = self._ensure_ws()
        if ws is None:
            return
        key = symbol.upper()
        with self._ws_lock:
            if key in self._subscribed:
                return
            self._subscribed.add(key)
        try:
            ws.subscribe_symbol(symbol, book_ticker=True)
        except Exception as exc:
            logger.debug("Gagal subscribe WS %s: %s", symbol, exc)

    def close(self) -> None:
        self._stop.set()
        with self._ws_lock:
            if self._ws is not None:
                try:
                    self._ws.stop()
                except Exception:
                    pass
                self._ws = None
            self._subscribed.clear()
        self.rest.close()

    def sync_time(self) -> None:
        self.rest.sync_time()

    def get_exchange_info(
        self, symbol: Optional[str] = None, force: bool = False
    ) -> dict:
        if symbol is not None:
            return self.rest.get_exchange_info(symbol)
        with self._ei_lock:
            fresh = (
                self._exchange_info is not None
                and (time.monotonic() - self._exchange_info_ts)
                < self._ei_refresh_seconds
            )
            if fresh and not force:
                return self._exchange_info
        info = self.rest.get_exchange_info()
        with self._ei_lock:
            self._exchange_info = info
            self._exchange_info_ts = time.monotonic()
        return info

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time_ms: Optional[int] = None,
        end_time_ms: Optional[int] = None,
    ) -> list:
        return self.rest.get_klines(symbol, interval, limit, start_time_ms, end_time_ms)

    def prewarm_book_ticker(self, symbols) -> None:
        if not self._use_ws or not symbols:
            return
        try:
            ws = self._ensure_ws()
            if ws is None:
                return
            ws.subscribe_symbols(symbols, book_ticker=True)
        except Exception as exc:
            logger.debug("Pemanasan bookTicker WebSocket gagal: %s", exc)

    def get_klines_many(
        self,
        symbols,
        interval: str,
        limit: int = 500,
        end_time_ms: Optional[int] = None,
        max_workers: Optional[int] = None,
    ) -> dict:
        unique = [str(s) for s in dict.fromkeys(symbols or ())]
        if not unique:
            return {}

        def _one(symbol: str):
            try:
                return self.rest.get_klines(symbol, interval, limit, None, end_time_ms)
            except Exception as exc:
                logger.debug("Gagal mengambil candle %s %s: %s", symbol, interval, exc)
                return None

        workers = max(1, int(max_workers or self._workers or 1))
        if workers <= 1 or len(unique) == 1:
            return {symbol: _one(symbol) for symbol in unique}

        out: dict = {}
        with ThreadPoolExecutor(
            max_workers=min(workers, len(unique)), thread_name_prefix="klines"
        ) as pool:
            futures = {pool.submit(_one, symbol): symbol for symbol in unique}
            for future in as_completed(futures):
                out[futures[future]] = future.result()
        return out

    def _ticker_refresh_loop(self) -> None:
        while not self._stop.wait(self._ticker_ttl):
            try:
                self._refresh_ticker_snapshot()
            except Exception as exc:
                logger.debug("Penyegaran snapshot ticker gagal: %s", exc)
        logger.debug("Thread penyegar snapshot ticker berhenti.")

    def _start_ticker_refresher(self) -> None:
        if self._ticker_ttl <= 0:
            return
        with self._ticker_lock:
            if self._ticker_refresher is not None and self._ticker_refresher.is_alive():
                return
            self._ticker_refresher = threading.Thread(
                target=self._ticker_refresh_loop, name="ticker-snapshot", daemon=True
            )
            self._ticker_refresher.start()
            logger.info(
                "Penyegar snapshot ticker aktif (TTL %g detik): scan tidak lagi "
                "menunggu unduhan daftar ticker 24 jam.",
                self._ticker_ttl,
            )

    def _refresh_ticker_snapshot(self) -> list:
        tickers = self.rest.get_ticker_24hr_all()
        if isinstance(tickers, list) and tickers:
            with self._ticker_lock:
                self._ticker_snapshot = tickers
                self._ticker_snapshot_ts = time.monotonic()
        return tickers

    def _overlay_last_price(self, tickers: list) -> list:
        if not self._use_ws or not self._ws_overlay_enabled or not tickers:
            return tickers
        try:
            ws = self._ensure_ws()
            if ws is None or not ws.is_connected():
                return tickers
            data, age = ws.all_mini_tickers()
            if not data or age > self._max_age:
                return tickers
        except Exception as exc:
            logger.debug("Timpaan harga WebSocket dilewati: %s", exc)
            return tickers

        out: list = []
        for item in tickers:
            symbol = str(item.get("symbol", "")).upper()
            payload = data.get(symbol)
            if payload is not None:
                try:
                    last = float(payload["c"])
                except (KeyError, TypeError, ValueError):
                    last = 0.0
                if math.isfinite(last) and last > 0:
                    item = dict(item)
                    item["lastPrice"] = repr(last)
            out.append(item)
        return out

    def get_ticker_24hr_all(self) -> list:
        if self._ticker_ttl <= 0:
            return self._refresh_ticker_snapshot()

        self._start_ticker_refresher()
        with self._ticker_lock:
            snapshot = self._ticker_snapshot
            usia = time.monotonic() - self._ticker_snapshot_ts
        if not snapshot:
            snapshot = self._refresh_ticker_snapshot()
        elif usia > max(self._ticker_ttl * 3.0, self._ticker_ttl + 30.0):
            logger.warning(
                "Snapshot ticker berusia %.0f detik (TTL %g detik). Menyegarkan "
                "secara sinkron karena penyegar latar tidak berhasil.",
                usia,
                self._ticker_ttl,
            )
            try:
                snapshot = self._refresh_ticker_snapshot()
            except Exception as exc:
                logger.error("Penyegaran sinkron snapshot ticker gagal: %s", exc)
        return self._overlay_last_price(snapshot)

    def get_price(self, symbol: str, max_retries: int = 3) -> float:
        if self._use_ws:
            self._ensure_symbol_stream(symbol)
            ws = self._ws
            if ws is not None:
                price, age = ws.get_price(symbol)
                if price is not None and age <= self._max_age:
                    return float(price)
        return self.rest.get_price(symbol, max_retries=max_retries)

    def get_book_ticker(self, symbol: str, max_retries: int = 3) -> dict:
        if self._use_ws:
            self._ensure_symbol_stream(symbol)
            ws = self._ws
            if ws is not None:
                book, age = ws.get_book_ticker(symbol)
                if book is not None and age <= self._max_age:
                    return {
                        "symbol": symbol.upper(),
                        "bidPrice": f"{book['bid']:.8f}",
                        "bidQty": f"{book.get('bidQty', 0.0):.8f}",
                        "askPrice": f"{book['ask']:.8f}",
                        "askQty": f"{book.get('askQty', 0.0):.8f}",
                    }
        return self.rest.get_book_ticker(symbol, max_retries=max_retries)

    def get_depth(self, symbol: str, limit: Optional[int] = None) -> dict:
        lim = int(limit or self._depth_limit)
        key = f"{symbol.upper()}:{lim}"
        now = time.monotonic()
        with self._depth_lock:
            cached = self._depth_cache.get(key)
            if cached is not None and (now - cached[1]) < self._depth_cache_ttl:
                return cached[0]
        depth = self.rest.get_depth(symbol, limit=lim)
        with self._depth_lock:
            self._depth_cache[key] = (depth, time.monotonic())
        return depth
