from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from collections import OrderedDict
from typing import Any, Optional

try:
    from websocket import WebSocketApp

    _WS_AVAILABLE = True
except ImportError:
    WebSocketApp = object
    _WS_AVAILABLE = False

logger = logging.getLogger("market_ws")

_PROACTIVE_RECONNECT_SECONDS = 23 * 3600
_MAX_STREAMS_PER_CONNECTION = 1024
_MAX_BOOK_TICKER_STREAMS = _MAX_STREAMS_PER_CONNECTION - 1
_PING_INTERVAL = 20
_PING_TIMEOUT = 10

WS_FALLBACK_BASE_URLS = (
    "wss://data-stream.binance.vision",
    "wss://stream.binance.com:443",
)

_SKEMA_PROXY_WS = {
    "http": "http",
    "https": "http",
    "socks4": "socks4",
    "socks5": "socks5",
    "socks5h": "socks5h",
}


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

    def drop(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)
            self._ts.pop(key, None)

    def snapshot_all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)


class MarketWebSocket:

    def __init__(
        self,
        ws_base_url: str = "wss://stream.binance.com:9443",
        fallback_base_urls: Optional[list] = None,
        proxy_url: str = "",
    ) -> None:
        if not _WS_AVAILABLE:
            raise RuntimeError(
                "Paket 'websocket-client' belum terpasang. Jalankan "
                "`pip install -r requirements.txt`, atau set USE_WEBSOCKET=False "
                "di config untuk memakai REST polling penuh."
            )
        self.ws_base_url = ws_base_url.rstrip("/")
        kandidat = [self.ws_base_url]
        for url in (fallback_base_urls or WS_FALLBACK_BASE_URLS):
            bersih = str(url or "").rstrip("/")
            if bersih and bersih not in kandidat:
                kandidat.append(bersih)
        self._base_candidates = kandidat
        self._proxy_kwargs = self._parse_proxy(proxy_url)
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
        self._want_streams: OrderedDict[str, None] = OrderedDict()
        self._msg_id = 0
        self._last_connect_ts = 0.0
        self._connected_once = False

    def start(self, all_mini_ticker: bool = True) -> None:
        with self._sub_lock:
            self._want_all_mini = all_mini_ticker
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="market-ws", daemon=True
        )
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
        self.subscribe_symbols((symbol,), book_ticker=book_ticker)

    def get_price(self, symbol: str) -> tuple[Optional[float], float]:
        return self._prices.get(symbol.upper())

    def get_book_ticker(self, symbol: str) -> tuple[Optional[dict], float]:
        return self._book.get(symbol.upper())

    def subscribe_symbols(self, symbols, book_ticker: bool = True) -> None:
        if not book_ticker:
            return
        requested: list[str] = []
        seen: set[str] = set()
        for symbol in symbols or ():
            value = str(symbol).strip().lower()
            if not value:
                continue
            stream = f"{value}@bookTicker"
            if stream not in seen:
                requested.append(stream)
                seen.add(stream)
        if not requested:
            return

        new_streams: list[str] = []
        removed_streams: list[str] = []
        with self._sub_lock:
            for stream in requested:
                if stream in self._want_streams:
                    self._want_streams.move_to_end(stream)
                else:
                    self._want_streams[stream] = None
                    new_streams.append(stream)
            while len(self._want_streams) > _MAX_BOOK_TICKER_STREAMS:
                evicted, _ = self._want_streams.popitem(last=False)
                if evicted in new_streams:
                    new_streams.remove(evicted)
                else:
                    removed_streams.append(evicted)
                    self._book.drop(evicted.split("@")[0].upper())

            if removed_streams:
                self._send(
                    {
                        "method": "UNSUBSCRIBE",
                        "params": removed_streams,
                        "id": self._next_id(),
                    }
                )
            if new_streams:
                self._send(
                    {
                        "method": "SUBSCRIBE",
                        "params": new_streams,
                        "id": self._next_id(),
                    }
                )
            active_count = len(self._want_streams)

        if removed_streams:
            logger.warning(
                "Batas WebSocket bookTicker tercapai: %d/%d stream dipertahankan; "
                "%d stream paling lama dilepas.",
                active_count,
                _MAX_BOOK_TICKER_STREAMS,
                len(removed_streams),
            )
        elif new_streams:
            logger.debug(
                "WebSocket bookTicker bertambah: %d/%d stream aktif.",
                active_count,
                _MAX_BOOK_TICKER_STREAMS,
            )

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

    def _configured_stream_count(self) -> int:
        with self._sub_lock:
            includes_mini_ticker = self._want_all_mini or not self._want_streams
            return len(self._want_streams) + int(includes_mini_ticker)

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

    @staticmethod
    def _parse_proxy(proxy_url: str) -> dict:
        raw = str(proxy_url or "").strip()
        if not raw:
            return {}
        if any(karakter.isspace() for karakter in raw):
            return {}
        bagian = urllib.parse.urlsplit(raw)
        if not bagian.hostname or not bagian.scheme:
            return {}
        skema = bagian.scheme.lower()
        jenis = _SKEMA_PROXY_WS.get(skema)
        if jenis is None:
            return {}
        port_bawaan = (
            443
            if skema == "https"
            else 1080
            if skema.startswith("socks")
            else 80
        )
        hasil = {
            "proxy_type": jenis,
            "http_proxy_host": bagian.hostname,
            "http_proxy_port": bagian.port or port_bawaan,
        }
        if bagian.username:
            hasil["http_proxy_auth"] = (
                urllib.parse.unquote(bagian.username),
                urllib.parse.unquote(bagian.password or ""),
            )
        return hasil

    def _rotate_base(self) -> bool:
        if len(self._base_candidates) < 2:
            return False
        with self._sub_lock:
            try:
                indeks = self._base_candidates.index(self.ws_base_url)
            except ValueError:
                indeks = 0
            baru = self._base_candidates[(indeks + 1) % len(self._base_candidates)]
            if baru == self.ws_base_url:
                return False
            lama = self.ws_base_url
            self.ws_base_url = baru
        logger.warning(
            "Endpoint WebSocket %s tidak dapat dipakai. Beralih ke %s.", lama, baru
        )
        return True

    def _run_loop(self) -> None:
        backoff = 1.0
        sisa_rotasi = len(self._base_candidates) - 1
        while not self._stop.is_set():
            url = self._build_url()
            self._last_connect_ts = time.monotonic()
            self._connected_once = False
            logger.info(
                "WebSocket menyambung ke %s dengan %d/%d stream.",
                self.ws_base_url,
                self._configured_stream_count(),
                _MAX_STREAMS_PER_CONNECTION,
            )
            self._app = WebSocketApp(
                url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            try:
                self._app.run_forever(
                    ping_interval=_PING_INTERVAL,
                    ping_timeout=_PING_TIMEOUT,
                    reconnect=None,
                    **self._proxy_kwargs,
                )
            except Exception as exc:
                logger.warning("WebSocket run_forever error: %s", exc)
            connected_before_close = self._connected_once
            self._connected.clear()
            if self._stop.is_set():
                break
            if not connected_before_close and sisa_rotasi > 0 and self._rotate_base():
                sisa_rotasi -= 1
                continue
            if connected_before_close:
                backoff = 1.0
            sisa_rotasi = len(self._base_candidates) - 1
            wait = backoff
            logger.info("WebSocket terputus. Menyambung ulang dalam %.0f detik.", wait)
            if self._stop.wait(timeout=wait):
                break
            backoff = self._next_backoff(backoff, connected_before_close)
        logger.info("Thread WebSocket berhenti.")

    def _on_open(self, _app) -> None:
        self._connected.set()
        self._connected_once = True
        logger.info(
            "WebSocket tersambung dengan %d/%d stream.",
            self._configured_stream_count(),
            _MAX_STREAMS_PER_CONNECTION,
        )

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
        if isinstance(obj, dict) and "id" in obj and (
            "result" in obj or "code" in obj or "msg" in obj
        ):
            if "code" in obj or "msg" in obj:
                logger.warning(
                    "Permintaan kontrol WebSocket ditolak (id=%s, code=%s): %s",
                    obj.get("id"),
                    obj.get("code"),
                    str(obj.get("msg") or "respons error tanpa pesan")[:180],
                )
            else:
                logger.debug(
                    "Ack kontrol WebSocket id=%s; stream=%d/%d.",
                    obj.get("id"),
                    self._configured_stream_count(),
                    _MAX_STREAMS_PER_CONNECTION,
                )
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
        if etype == "bookTicker" or (
            "b" in data and "a" in data and "s" in data and "k" not in data
        ):
            sym = data.get("s")
            if sym:
                try:
                    bid = float(data["b"])
                    ask = float(data["a"])
                    self._book.put(
                        sym.upper(),
                        {
                            "bid": bid,
                            "ask": ask,
                            "bidQty": float(data.get("B", 0) or 0),
                            "askQty": float(data.get("A", 0) or 0),
                        },
                    )
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
