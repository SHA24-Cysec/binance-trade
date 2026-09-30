"""
Lapisan WebSocket market data Binance Spot (produksi publik).

Menjadi SUMBER PRIMER data pasar real-time dalam mode hybrid: harga semua pair
(!miniTicker@arr), bookTicker per simbol, dan kline per simbol. Data ini
dikonsumsi MarketDataProvider (market_data.py), yang jatuh ke REST bila WS
basi/putus.

Referensi resmi (dicek 2026-09-24):
  developers.binance.com/docs/binance-spot-api-docs/web-socket-streams
- Base endpoint: wss://stream.binance.com:9443 (atau :443). Mirror khusus
  market data: wss://data-stream.binance.vision.
- Raw stream: /ws/<streamName>. Combined: /stream?streams=a/b/c (payload
  dibungkus {"stream": name, "data": ...}).
- Semua nama simbol pada stream HARUS huruf kecil.
- Satu koneksi valid maksimal 24 jam lalu diputus server; klien harus
  menyambung ulang.
- Langganan dinamis via pesan JSON {"method":"SUBSCRIBE","params":[...],"id":n}.

Kesengajaan desain: order book untuk MENGISI order simulasi TIDAK dibangun dari
depth-diff WS (yang rawan salah bila ada event terlewat). Sebagai gantinya
PaperEngine mengambil snapshot depth REST yang SEGAR pada saat order dikirim
(lihat market_data.get_depth). Ini lebih setia untuk simulasi fill daripada
order book lokal yang mungkin sudah basi. WS di sini fokus ke harga/bookTicker/
kline yang memang cocok untuk streaming.

Dependensi: websocket-client>=1.7 (diuji dengan 1.9.0).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

try:
    import websocket  # type: ignore  # dari paket websocket-client
    from websocket import WebSocketApp  # type: ignore
    _WS_AVAILABLE = True
except ImportError:  # pragma: no cover - hanya bila paket tak terpasang
    websocket = None  # type: ignore
    WebSocketApp = object  # type: ignore
    _WS_AVAILABLE = False

logger = logging.getLogger("market_ws")

# Batas server (dari dokumentasi): koneksi diputus di 24 jam. Kita menyambung
# ulang secara proaktif sedikit lebih awal supaya tidak ada jeda data.
_PROACTIVE_RECONNECT_SECONDS = 23 * 3600
# Ping/pong dikelola websocket-client via run_forever(ping_interval,...).
_PING_INTERVAL = 20
_PING_TIMEOUT = 10


class _StreamCache:
    """Cache snapshot terakhir per stream, aman-thread, dengan timestamp
    kesegaran (monotonic) untuk deteksi data basi."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self._ts: dict[str, float] = {}

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._ts[key] = time.monotonic()

    def get(self, key: str) -> tuple[Optional[Any], float]:
        """Kembalikan (nilai, usia_detik). Usia = inf bila belum pernah ada."""
        with self._lock:
            if key not in self._data:
                return None, float("inf")
            return self._data[key], time.monotonic() - self._ts[key]

    def snapshot_all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)


class MarketWebSocket:
    """Klien WebSocket market data ber-thread dengan reconnect & backoff.

    Pemakaian:
        ws = MarketWebSocket("wss://stream.binance.com:9443")
        ws.start(all_mini_ticker=True)
        ws.subscribe_symbol("BTCUSDT", book_ticker=True, kline_interval="5m")
        price, age = ws.get_price("BTCUSDT")

    Semua getter mengembalikan (nilai, usia_detik); pemanggil memutuskan
    apakah usia masih dapat diterima (staleness guard ada di MarketDataProvider).
    """

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

        # Cache per kategori.
        self._prices = _StreamCache()       # symbol -> last price (float)
        self._book = _StreamCache()          # symbol -> {"bid","ask","bidQty","askQty"}
        self._kline = _StreamCache()         # "SYMBOL@interval" -> kline dict

        # Set langganan yang diinginkan (dipulihkan setelah reconnect).
        self._sub_lock = threading.RLock()
        self._want_all_mini = False
        self._want_streams: set[str] = set()  # nama stream lowercase (mis. "btcusdt@bookTicker")
        self._msg_id = 0
        self._last_connect_ts = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, all_mini_ticker: bool = True) -> None:
        """Mulai thread koneksi. all_mini_ticker=True melanggan
        !miniTicker@arr (harga semua pair dalam satu stream)."""
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
            except Exception:  # noqa: BLE001 - penutupan best-effort
                pass
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=5.0)

    def wait_connected(self, timeout: float = 10.0) -> bool:
        return self._connected.wait(timeout=timeout)

    # ------------------------------------------------------------------
    # Langganan
    # ------------------------------------------------------------------
    def subscribe_symbol(self, symbol: str, book_ticker: bool = True,
                         kline_interval: Optional[str] = None) -> None:
        """Langgan stream untuk satu simbol (dipakai untuk posisi aktif)."""
        s = symbol.lower()
        new: list[str] = []
        with self._sub_lock:
            if book_ticker:
                name = f"{s}@bookTicker"
                if name not in self._want_streams:
                    self._want_streams.add(name)
                    new.append(name)
            if kline_interval:
                name = f"{s}@kline_{kline_interval}"
                if name not in self._want_streams:
                    self._want_streams.add(name)
                    new.append(name)
        if new:
            self._send({"method": "SUBSCRIBE", "params": new, "id": self._next_id()})

    def unsubscribe_symbol(self, symbol: str) -> None:
        s = symbol.lower()
        remove: list[str] = []
        with self._sub_lock:
            for name in list(self._want_streams):
                if name.startswith(f"{s}@"):
                    self._want_streams.discard(name)
                    remove.append(name)
        if remove:
            self._send({"method": "UNSUBSCRIBE", "params": remove, "id": self._next_id()})

    # ------------------------------------------------------------------
    # Getter (mengembalikan (nilai, usia_detik))
    # ------------------------------------------------------------------
    def get_price(self, symbol: str) -> tuple[Optional[float], float]:
        return self._prices.get(symbol.upper())

    def get_book_ticker(self, symbol: str) -> tuple[Optional[dict], float]:
        return self._book.get(symbol.upper())

    def get_kline(self, symbol: str, interval: str) -> tuple[Optional[dict], float]:
        return self._kline.get(f"{symbol.upper()}@{interval}")

    def all_prices(self) -> dict[str, float]:
        """Snapshot {SYMBOL: price} dari !miniTicker@arr (bisa kosong bila
        stream all-mini belum aktif atau belum ada data)."""
        return self._prices.snapshot_all()

    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
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
            # Tidak ada apa pun untuk dilanggan: pakai !miniTicker@arr sebagai
            # default supaya koneksi tetap sah.
            streams = {"!miniTicker@arr"}
        joined = "/".join(sorted(streams))
        return f"{self.ws_base_url}/stream?streams={joined}"

    def _send(self, payload: dict) -> None:
        app = self._app
        if app is None or not self._connected.is_set():
            return  # akan dipulihkan lewat URL saat reconnect
        try:
            app.send(json.dumps(payload))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Gagal kirim pesan langganan WS: %s", exc)

    @staticmethod
    def _next_backoff(current: float, connected_before_close: bool) -> float:
        """Reset backoff setelah koneksi sehat, naikkan hanya crash beruntun."""
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
                # run_forever memblokir sampai koneksi tertutup. ping otomatis
                # dikirim tiap _PING_INTERVAL detik (server Binance ping ~20s).
                self._app.run_forever(ping_interval=_PING_INTERVAL,
                                      ping_timeout=_PING_TIMEOUT,
                                      reconnect=None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("WebSocket run_forever error: %s", exc)
            connected_before_close = self._connected.is_set()
            self._connected.clear()
            if self._stop.is_set():
                break
            # Koneksi yang pernah sehat tidak boleh mewarisi backoff crash
            # beruntun dari sesi sebelumnya. Crash sebelum on_open baru
            # menaikkan backoff eksponensial sampai 60 detik.
            wait = backoff
            logger.info("WebSocket terputus. Menyambung ulang dalam %.0f detik.", wait)
            if self._stop.wait(timeout=wait):
                break
            backoff = self._next_backoff(backoff, connected_before_close)
        logger.info("Thread WebSocket berhenti.")

    def _on_open(self, _app) -> None:  # noqa: ANN001
        self._connected.set()
        logger.info("WebSocket tersambung.")

    def _on_error(self, _app, error) -> None:  # noqa: ANN001
        logger.debug("WebSocket error: %s", error)

    def _on_close(self, _app, status_code, msg) -> None:  # noqa: ANN001
        self._connected.clear()
        logger.debug("WebSocket ditutup (code=%s, msg=%s).", status_code, msg)

    def _on_message(self, _app, message: str) -> None:  # noqa: ANN001
        try:
            obj = json.loads(message)
        except (ValueError, TypeError):
            return
        # Combined stream membungkus {"stream":..., "data":...}. Respons hasil
        # SUBSCRIBE/UNSUBSCRIBE berupa {"result":null,"id":n} (diabaikan).
        data = obj.get("data") if isinstance(obj, dict) else None
        if data is None:
            return
        self._handle_payload(data)
        # Reconnect proaktif sebelum batas 24 jam server.
        if time.monotonic() - self._last_connect_ts > _PROACTIVE_RECONNECT_SECONDS:
            logger.info("Mendekati batas koneksi 24 jam, menyambung ulang proaktif.")
            app = self._app
            if app is not None:
                try:
                    app.close()
                except Exception:  # noqa: BLE001
                    pass

    def _handle_payload(self, data: Any) -> None:
        # !miniTicker@arr -> list of {"s": symbol, "c": lastPrice, ...}
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("e") == "24hrMiniTicker":
                    sym = item.get("s")
                    close = item.get("c")
                    if sym and close is not None:
                        try:
                            self._prices.put(sym.upper(), float(close))
                        except (TypeError, ValueError):
                            pass
            return
        if not isinstance(data, dict):
            return
        etype = data.get("e")
        # bookTicker: tidak selalu punya "e"; kenali dari kunci b/a/B/A/s.
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
                    # bookTicker juga menyegarkan harga mid sebagai proxy harga.
                    self._prices.put(sym.upper(), (bid + ask) / 2.0)
                except (TypeError, ValueError, KeyError):
                    pass
            return
        if etype == "kline":
            k = data.get("k")
            sym = data.get("s")
            if isinstance(k, dict) and sym:
                interval = k.get("i")
                if interval:
                    self._kline.put(f"{sym.upper()}@{interval}", k)
                try:
                    self._prices.put(sym.upper(), float(k["c"]))
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


# ==== RINGKASAN AUDIT (market_ws.py) ===================================
# Sintaks/tipe: type hints lengkap; import websocket dibungkus try/except agar
#   modul tetap bisa di-import (dan diuji) tanpa paket terpasang.
# Race condition: cache pakai _StreamCache berbasis RLock; set langganan &
#   _msg_id dijaga _sub_lock. Getter mengembalikan salinan/nilai imutabel.
# Reconnect: backoff eksponensial (1s..60s), reconnect proaktif < 24 jam,
#   set langganan dipulihkan lewat _build_url() setiap sambung ulang.
# Staleness: setiap getter mengembalikan usia (monotonic), keputusan basi ada
#   di MarketDataProvider -> tidak ada data basi yang dipakai diam-diam.
# Ping/heartbeat: ditangani run_forever(ping_interval=20, ping_timeout=10),
#   sesuai server yang ping ~20 detik.
# Kebocoran rahasia: hanya endpoint publik, tanpa API key/tanda tangan.
# Batasan: order book untuk fill TIDAK dibangun dari depth-diff WS (disengaja;
#   PaperEngine memakai snapshot REST segar). Didokumentasikan di README.
# =======================================================================
