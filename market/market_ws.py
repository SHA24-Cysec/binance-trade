from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

try:
    from websocket import WebSocketApp
    _WS_AVAILABLE = True
except ImportError:
    WebSocketApp = object
    _WS_AVAILABLE = False

logger = logging.getLogger("market_ws")

_PROACTIVE_RECONNECT_SECONDS = 23 * 3600
_PING_INTERVAL = 20
_PING_TIMEOUT = 10


class _StreamCache:

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self._ts: dict[str, float] = {}

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._ts[key] = time.monotonic()

    def put_many(self, items: dict) -> None:
        if not items:
            return
        now = time.monotonic()
        with self._lock:
            self._data.update(items)
            self._ts.update(dict.fromkeys(items, now))

    def get(self, key: str) -> tuple[Optional[Any], float]:
        with self._lock:
            if key not in self._data:
                return None, float("inf")
            return self._data[key], time.monotonic() - self._ts[key]

    def snapshot_all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)


class MarketWebSocket:

    def __init__(self, ws_base_url: str = "wss://stream.binance.com:9443") -> None:
        if not _WS_AVAILABLE:
            raise RuntimeError(
                "Paket 'websocket-client' belum terpasang. Jalankan "
                "`pip install -r requirements.txt`, atau set USE_WEBSOCKET=False "
                "di config untuk memakai REST polling penuh."
            )
        self.ws_base_url = ws_base_url.rstrip("/")
        self._app: "Optional[WebSocketApp]" = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = threading.Event()

        self._prices = _StreamCache()
        self._book = _StreamCache()
        self._mini = _StreamCache()

        self._mini_lock = threading.RLock()
        self._mini_arr_ts = 0.0

        self._sub_lock = threading.RLock()
        self._want_all_mini = False
        self._want_streams: set[str] = set()
        self._msg_id = 0
        self._last_connect_ts = 0.0

    def start(self, all_mini_ticker: bool = True) -> None:
        with self._sub_lock:
            self._want_all_mini = all_mini_ticker
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="market-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        app = self._app
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=5.0)

    def subscribe_symbol(self, symbol: str, book_ticker: bool = True) -> None:
        s = symbol.lower()
        new: list[str] = []
        with self._sub_lock:
            if book_ticker:
                name = f"{s}@bookTicker"
                if name not in self._want_streams:
                    self._want_streams.add(name)
                    new.append(name)
        if new:
            self._send({"method": "SUBSCRIBE", "params": new, "id": self._next_id()})

    def get_price(self, symbol: str) -> tuple[Optional[float], float]:
        return self._prices.get(symbol.upper())

    def get_book_ticker(self, symbol: str) -> tuple[Optional[dict], float]:
        return self._book.get(symbol.upper())

    def subscribe_symbols(self, symbols, book_ticker: bool = True) -> None:
        new: list[str] = []
        with self._sub_lock:
            for symbol in symbols or ():
                s = str(symbol).strip().lower()
                if not s:
                    continue
                if book_ticker:
                    name = f"{s}@bookTicker"
                    if name not in self._want_streams:
                        self._want_streams.add(name)
                        new.append(name)
        if new:
            self._send({"method": "SUBSCRIBE", "params": new, "id": self._next_id()})

    def all_mini_tickers(self) -> "tuple[dict[str, dict], float]":
        with self._mini_lock:
            if not self._mini_arr_ts:
                return {}, float("inf")
            age = time.monotonic() - self._mini_arr_ts
        return self._mini.snapshot_all(), age

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def _next_id(self) -> int:
        with self._sub_lock:
            self._msg_id += 1
            return self._msg_id

    def _build_url(self) -> str:
        with self._sub_lock:
            streams = set(self._want_streams)
            if self._want_all_mini:
                streams.add("!miniTicker@arr")
        if not streams:
            streams = {"!miniTicker@arr"}
        joined = "/".join(sorted(streams))
        return f"{self.ws_base_url}/stream?streams={joined}"

    def _send(self, payload: dict) -> None:
        app = self._app
        if app is None or not self._connected.is_set():
            return
        try:
            app.send(json.dumps(payload))
        except Exception as exc:
            logger.debug("Gagal kirim pesan langganan WS: %s", exc)

    @staticmethod
    def _next_backoff(current: float, connected_before_close: bool) -> float:
        if connected_before_close:
            return 1.0
        return min(float(current) * 2.0, 60.0)

    def _run_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            url = self._build_url()
            self._last_connect_ts = time.monotonic()
            logger.info("WebSocket menyambung: %s", url)
            self._app = WebSocketApp(
                url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            try:
                self._app.run_forever(ping_interval=_PING_INTERVAL,
                                      ping_timeout=_PING_TIMEOUT,
                                      reconnect=None)
            except Exception as exc:
                logger.warning("WebSocket run_forever error: %s", exc)
            connected_before_close = self._connected.is_set()
            self._connected.clear()
            if self._stop.is_set():
                break
            wait = backoff
            logger.info("WebSocket terputus. Menyambung ulang dalam %.0f detik.", wait)
            if self._stop.wait(timeout=wait):
                break
            backoff = self._next_backoff(backoff, connected_before_close)
        logger.info("Thread WebSocket berhenti.")

    def _on_open(self, _app) -> None:
        self._connected.set()
        logger.info("WebSocket tersambung.")

    def _on_error(self, _app, error) -> None:
        logger.debug("WebSocket error: %s", error)

    def _on_close(self, _app, status_code, msg) -> None:
        self._connected.clear()
        logger.debug("WebSocket ditutup (code=%s, msg=%s).", status_code, msg)

    def _on_message(self, _app, message: str) -> None:
        try:
            obj = json.loads(message)
        except (ValueError, TypeError):
            return
        data = obj.get("data") if isinstance(obj, dict) else None
        if data is None:
            return
        self._handle_payload(data)
        if time.monotonic() - self._last_connect_ts > _PROACTIVE_RECONNECT_SECONDS:
            logger.info("Mendekati batas koneksi 24 jam, menyambung ulang proaktif.")
            app = self._app
            if app is not None:
                try:
                    app.close()
                except Exception:
                    pass

    def _handle_payload(self, data: Any) -> None:
        if isinstance(data, list):
            prices: dict[str, float] = {}
            minis: dict[str, Any] = {}
            for item in data:
                if isinstance(item, dict) and item.get("e") == "24hrMiniTicker":
                    sym = item.get("s")
                    close = item.get("c")
                    if sym and close is not None:
                        try:
                            prices[sym.upper()] = float(close)
                        except (TypeError, ValueError):
                            continue
                        minis[sym.upper()] = item
            if minis:
                self._prices.put_many(prices)
                self._mini.put_many(minis)
                with self._mini_lock:
                    self._mini_arr_ts = time.monotonic()
            return
        if not isinstance(data, dict):
            return
        etype = data.get("e")
        if etype == "bookTicker" or ("b" in data and "a" in data and "s" in data and "k" not in data):
            sym = data.get("s")
            if sym:
                try:
                    bid = float(data["b"]); ask = float(data["a"])
                    self._book.put(sym.upper(), {
                        "bid": bid, "ask": ask,
                        "bidQty": float(data.get("B", 0) or 0),
                        "askQty": float(data.get("A", 0) or 0),
                    })
                    self._prices.put(sym.upper(), (bid + ask) / 2.0)
                except (TypeError, ValueError, KeyError):
                    pass
            return
        if etype == "24hrMiniTicker":
            sym = data.get("s")
            close = data.get("c")
            if sym and close is not None:
                try:
                    self._prices.put(sym.upper(), float(close))
                except (TypeError, ValueError):
                    pass
