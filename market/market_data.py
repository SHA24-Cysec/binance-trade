"""
Lapisan data pasar publik BERSAMA untuk PAPER dan LIVE.

Mode HYBRID (keputusan desain):
- WebSocket = sumber PRIMER data real-time (harga, bookTicker, kline).
- REST publik (keyless, unsigned) dipakai untuk yang tidak punya padanan WS
  atau saat WS basi/putus:
    * exchangeInfo (tidak ada stream WS-nya) -> di-cache + refresh berkala.
    * kline historis (WS hanya candle live) -> untuk backfill.
    * depth snapshot (untuk mengisi market order simulasi) -> selalu segar.
    * fallback harga/bookTicker saat WS basi atau belum panas.

Semua request REST di sini KEYLESS (allow_signed=False), sehingga lapisan ini
tidak mungkin menyentuh endpoint bertanda tangan -- aman dipakai bersama oleh
PAPER (yang dilarang keras memakai signed) maupun LIVE.

Staleness guard: data WS yang lebih tua dari MAX_MARKET_DATA_AGE_SECONDS tidak
dipakai; provider jatuh ke REST. Bila REST juga gagal, exception diteruskan ke
pemanggil (PaperClient akan menolak mengisi order di atas data basi).

Versi acuan: requests>=2.32.4, websocket-client>=1.7, Python 3.10+.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from trading.clients.binance_client import BinanceSpotClient
from config.config import get_base_url, use_websocket

logger = logging.getLogger("market_data")


class MarketDataProvider:

    def __init__(self, config: dict) -> None:
        self.config = config
        base_url = get_base_url(config)
        self.rest = BinanceSpotClient(
            "", "", base_url, allow_signed=False,
            rate_limit_state_file=config.get("RATE_LIMIT_STATE_FILE"),
            rate_limit_limit=int(config.get("RATE_LIMIT_WEIGHT_LIMIT", 6000) or 6000),
            rate_limit_safety_margin=int(config.get("RATE_LIMIT_SAFETY_MARGIN", 100) or 100),
        )

        self._use_ws = use_websocket(config)
        self._max_age = float(config.get("MAX_MARKET_DATA_AGE_SECONDS", 10.0))
        self._depth_limit = int(config.get("PAPER_DEPTH_LIMIT", 100))

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
                    self._ws = MarketWebSocket(self.config.get(
                        "WS_BASE_URL", "wss://stream.binance.com:9443"))
                    self._ws.start(all_mini_ticker=True)
                    logger.info("Lapisan data pasar: WebSocket AKTIF (hybrid).")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Gagal memulai WebSocket (%s). Fallback REST penuh.", exc)
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
        except Exception as exc:  # noqa: BLE001
            logger.debug("Gagal subscribe WS %s: %s", symbol, exc)

    def close(self) -> None:
        with self._ws_lock:
            if self._ws is not None:
                try:
                    self._ws.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._ws = None
            self._subscribed.clear()

    def sync_time(self) -> None:
        self.rest.sync_time()

    def get_exchange_info(self, symbol: Optional[str] = None, force: bool = False) -> dict:
        if symbol is not None:
            return self.rest.get_exchange_info(symbol)
        with self._ei_lock:
            fresh = (self._exchange_info is not None
                     and (time.monotonic() - self._exchange_info_ts) < self._ei_refresh_seconds)
            if fresh and not force:
                return self._exchange_info
        info = self.rest.get_exchange_info()
        with self._ei_lock:
            self._exchange_info = info
            self._exchange_info_ts = time.monotonic()
        return info

    def get_klines(self, symbol: str, interval: str, limit: int = 500,
                   start_time_ms: Optional[int] = None,
                   end_time_ms: Optional[int] = None) -> list:
        return self.rest.get_klines(symbol, interval, limit, start_time_ms, end_time_ms)

    def get_ticker_24hr_all(self) -> list:
        return self.rest.get_ticker_24hr_all()

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
