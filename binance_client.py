"""
Klien REST Binance Spot minimal, dibuat manual (bukan pakai SDK pihak ketiga).

Kenapa tidak pakai SDK resmi `binance-sdk-spot`?
- Saat diaudit, versi SDK terbaru (per Sep 2026) punya bug pada parsing
  filter simbol di exchangeInfo (field oneOf `filters` gagal ter-parse jadi
  None saat divalidasi dari JSON respons asli), yang krusial untuk
  menghitung pembulatan quantity (LOT_SIZE) dan minimum notional. Daripada
  mewariskan bug itu ke bot yang menyentuh uang sungguhan, klien REST ini
  dibuat manual dan sederhana, memakai endpoint resmi yang didokumentasikan
  di developers.binance.com, dan sudah diuji cocok dengan contoh signature
  resmi Binance.

Aturan signature (berlaku sejak 2026-01-15): payload harus di-percent-encode
dulu sebelum dihitung HMAC SHA256, atau request ditolak dengan -1022
INVALID_SIGNATURE. Fungsi `_build_query` di bawah sudah menerapkan ini.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.parse
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

import requests

logger = logging.getLogger("binance_client")


class BinanceAPIError(Exception):
    def __init__(self, status_code: int, code: Optional[int], msg: str):
        self.status_code = status_code
        self.code = code
        self.msg = msg
        super().__init__(f"HTTP {status_code} | code={code} | {msg}")


def _build_query(params: dict) -> str:
    """Percent-encode key & value lalu gabungkan jadi query string.
    Urutan dict di Python 3.7+ terjaga (insertion order), jadi urutan
    parameter yang kita masukkan akan konsisten dipakai untuk signing
    maupun pengiriman request (harus sama persis)."""
    items = []
    for k, v in params.items():
        if v is None:
            continue
        items.append(
            f"{urllib.parse.quote_plus(str(k))}={urllib.parse.quote_plus(str(v))}"
        )
    return "&".join(items)


class BinanceSpotClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str, timeout: float = 10.0):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": self.api_key})
        self._time_offset_ms = 0

    # ---------------------------------------------------------------
    # Infrastruktur dasar
    # ---------------------------------------------------------------
    def sync_time(self) -> None:
        """Samakan jam lokal dengan server Binance supaya timestamp request
        tidak pernah meleset (penting karena request signed punya recvWindow)."""
        server_time = self.get_server_time()
        local_time = int(time.time() * 1000)
        self._time_offset_ms = server_time - local_time
        logger.info("Sinkronisasi waktu server selesai. Offset = %d ms", self._time_offset_ms)

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        signed: bool = False,
        max_retries: int = 3,
    ) -> Any:
        base_params = dict(params or {})

        def build_url() -> str:
            """Dibangun ulang di SETIAP percobaan (bukan sekali di luar loop),
            supaya timestamp & signature selalu segar. Sebelumnya ini dibangun
            sekali di luar loop retry -- bug: kalau percobaan pertama gagal
            karena -1021 lalu di-resync, percobaan berikutnya tetap memakai
            timestamp basi yang sama sehingga -1021 terus berulang."""
            local_params = dict(base_params)
            if signed:
                local_params["timestamp"] = self._timestamp()
                local_params.setdefault("recvWindow", 5000)
                q = _build_query(local_params)
                signature = hmac.new(
                    self.api_secret.encode("utf-8"), q.encode("utf-8"), hashlib.sha256
                ).hexdigest()
                q = f"{q}&signature={signature}"
            else:
                q = _build_query(local_params)
            u = f"{self.base_url}{path}"
            return f"{u}?{q}" if q else u

        last_exc = None
        for attempt in range(1, max_retries + 1):
            try:
                url = build_url()
                resp = self.session.request(method, url, timeout=self.timeout)
                if resp.status_code >= 400:
                    try:
                        body = resp.json()
                        code = body.get("code")
                        msg = body.get("msg", resp.text)
                    except ValueError:
                        code = None
                        msg = resp.text
                    raise BinanceAPIError(resp.status_code, code, msg)
                if resp.text == "":
                    return {}
                return resp.json()
            except (requests.exceptions.RequestException, BinanceAPIError) as exc:
                last_exc = exc
                # -1021 = timestamp di luar recvWindow -> re-sync lalu retry
                if isinstance(exc, BinanceAPIError) and exc.code == -1021:
                    logger.warning("Timestamp meleset, sinkronisasi ulang jam server...")
                    self.sync_time()
                wait = min(2 ** attempt, 10)
                logger.warning(
                    "Request %s %s gagal (percobaan %d/%d): %s. Tunggu %ds.",
                    method, path, attempt, max_retries, exc, wait,
                )
                time.sleep(wait)
        raise last_exc

    # ---------------------------------------------------------------
    # Public endpoints (tidak butuh API key)
    # ---------------------------------------------------------------
    def get_server_time(self) -> int:
        data = self._request("GET", "/api/v3/time")
        return int(data["serverTime"])

    def get_exchange_info(self, symbol: Optional[str] = None) -> dict:
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/api/v3/exchangeInfo", params)

    def get_ticker_24hr_all(self) -> list:
        """Ambil statistik 24 jam untuk SEMUA pair sekaligus dalam satu
        request (weight lumayan besar, ~80 -- jangan dipanggil terlalu
        sering; cukup tiap beberapa menit untuk scanning pasar)."""
        return self._request("GET", "/api/v3/ticker/24hr", {})

    def get_klines(self, symbol: str, interval: str, limit: int = 500) -> list:
        return self._request(
            "GET", "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit}
        )

    def get_book_ticker(self, symbol: str) -> dict:
        return self._request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol})

    def get_price(self, symbol: str) -> float:
        data = self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})
        return float(data["price"])

    # ---------------------------------------------------------------
    # Signed endpoints (butuh API key + secret)
    # ---------------------------------------------------------------
    def get_account(self) -> dict:
        return self._request("GET", "/api/v3/account", signed=True)

    def new_market_order(self, symbol: str, side: str, quantity: Optional[float] = None,
                          quote_order_qty: Optional[float] = None) -> dict:
        params = {"symbol": symbol, "side": side, "type": "MARKET"}
        if quantity is not None:
            params["quantity"] = quantity
        if quote_order_qty is not None:
            params["quoteOrderQty"] = quote_order_qty
        return self._request("POST", "/api/v3/order", params, signed=True)


# ---------------------------------------------------------------------
# Util filter simbol (LOT_SIZE, NOTIONAL/MIN_NOTIONAL, PRICE_FILTER)
# ---------------------------------------------------------------------
class SymbolFilters:
    def __init__(self, step_size: Decimal, min_qty: Decimal, min_notional: Decimal,
                 tick_size: Decimal):
        self.step_size = step_size
        self.min_qty = min_qty
        self.min_notional = min_notional
        self.tick_size = tick_size

    @classmethod
    def from_symbol_data(cls, sym_data: dict) -> "SymbolFilters":
        """Parsing filter dari SATU entry symbol di exchangeInfo['symbols'].
        Dipisah dari from_exchange_info() supaya bisa dipakai untuk
        membangun cache banyak simbol sekaligus tanpa scan ulang list
        symbols setiap kali (dipakai oleh build_filters_cache)."""
        symbol = sym_data.get("symbol", "?")

        # PENTING: untuk simbol likuid seperti BTCUSDT, Binance sekarang
        # mengirim MARKET_LOT_SIZE dengan stepSize/minQty = "0.00000000",
        # yang artinya "tidak ada batasan tambahan di luar LOT_SIZE" --
        # BUKAN berarti steps-nya benar-benar nol. Kalau nilai 0 ini asal
        # ditimpakan begitu saja, pembulatan quantity jadi rusak (tidak
        # dibulatkan sama sekali) dan order akan ditolak bursa karena
        # presisi quantity tidak sesuai LOT_SIZE. Jadi nilai 0 pada filter
        # manapun harus dianggap "abaikan", bukan "pakai nilai ini".
        lot_step = Decimal("0")
        lot_min = Decimal("0")
        market_lot_step = Decimal("0")
        market_lot_min = Decimal("0")
        min_notional = Decimal("0")
        tick_size = Decimal("0.01")

        for f in sym_data.get("filters", []):
            ftype = f.get("filterType")
            if ftype == "LOT_SIZE":
                lot_step = Decimal(f["stepSize"])
                lot_min = Decimal(f["minQty"])
            elif ftype == "MARKET_LOT_SIZE":
                market_lot_step = Decimal(f["stepSize"])
                market_lot_min = Decimal(f["minQty"])
            elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                min_notional = Decimal(f["minNotional"])
            elif ftype == "PRICE_FILTER":
                tick_size = Decimal(f["tickSize"])

        # Bot ini SELALU memakai order MARKET, jadi idealnya patuhi
        # MARKET_LOT_SIZE -- tapi hanya kalau nilainya benar-benar > 0.
        step_size = market_lot_step if market_lot_step > 0 else lot_step
        min_qty = market_lot_min if market_lot_min > 0 else lot_min
        # LOT_SIZE tetap wajib dipatuhi juga (dua-duanya berlaku bersamaan
        # di sisi bursa), jadi ambil yang paling ketat di antara keduanya.
        if lot_step > 0:
            step_size = max(step_size, lot_step)
        min_qty = max(min_qty, lot_min)

        if step_size <= 0:
            # Jaga-jaga kalau suatu saat kedua filter sama-sama 0 -- jangan
            # sampai bot mengira "tidak perlu pembulatan" padahal itu keliru.
            step_size = Decimal("0.00000001")
            logger.warning(
                "LOT_SIZE dan MARKET_LOT_SIZE sama-sama 0 untuk %s -- "
                "memakai fallback stepSize=%s. Mohon cek manual di Binance.",
                symbol, step_size,
            )

        return cls(step_size=step_size, min_qty=min_qty, min_notional=min_notional,
                   tick_size=tick_size)

    @classmethod
    def from_exchange_info(cls, exchange_info: dict, symbol: str) -> "SymbolFilters":
        symbols = exchange_info.get("symbols", [])
        sym_data = next((s for s in symbols if s.get("symbol") == symbol), None)
        if sym_data is None:
            raise ValueError(f"Simbol {symbol} tidak ditemukan di exchangeInfo")
        return cls.from_symbol_data(sym_data)

    def round_qty(self, qty: float) -> float:
        q = Decimal(str(qty))
        if self.step_size <= 0:
            return float(q)
        steps = (q / self.step_size).to_integral_value(rounding=ROUND_DOWN)
        rounded = steps * self.step_size
        return float(rounded)


def build_filters_cache(exchange_info: dict) -> dict:
    """Bangun cache {symbol: SymbolFilters} untuk SEMUA simbol dari satu
    respons exchangeInfo (dipakai mode pump scanner yang butuh filter utk
    banyak koin berbeda, bukan cuma satu simbol tetap)."""
    cache = {}
    for sym_data in exchange_info.get("symbols", []):
        symbol = sym_data.get("symbol")
        if not symbol:
            continue
        try:
            cache[symbol] = SymbolFilters.from_symbol_data(sym_data)
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug("Lewati parsing filter untuk %s: %s", symbol, exc)
    return cache
