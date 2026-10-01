"""
Klien REST Binance Spot minimal, dibuat manual (bukan pakai SDK pihak ketiga).

Kenapa tidak pakai SDK resmi `binance-sdk-spot`?
- Keputusan ini dibuat berdasarkan temuan saat klien ini pertama ditulis
  (bug parse filter simbol oneOf di exchangeInfo). CATATAN AUDIT 2026-09-24:
  klaim tersebut TIDAK berhasil diverifikasi ulang dari sumber publik
  (SDK resmi ada dan aktif, versi 11.2.0). Statusnya: PERLU VERIFIKASI
  DOKUMENTASI. Apa pun hasilnya, klien manual ini tetap aman dipakai: ia
  memakai endpoint resmi yang didokumentasikan di developers.binance.com dan
  sudah diuji cocok dengan contoh signature resmi Binance.

Aturan signature (berlaku sejak 2026-01-15): payload harus di-percent-encode
dulu sebelum dihitung HMAC SHA256, atau request ditolak dengan -1022
INVALID_SIGNATURE. Fungsi `_build_query` di bawah sudah menerapkan ini.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
import time
import urllib.parse
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

import requests

from infrastructure.network.rate_limiter import RateLimitBlockedError, SharedRequestWeightLimiter

logger = logging.getLogger("binance_client")


class BinanceAPIError(Exception):
    def __init__(self, status_code: int, code: Optional[int], msg: str):
        self.status_code = status_code
        self.code = code
        self.msg = msg
        super().__init__(f"HTTP {status_code} | code={code} | {msg}")


class SignedEndpointBlockedError(RuntimeError):
    pass


class BinanceRateLimitError(BinanceAPIError):

    def __init__(self, status_code: int, code: Optional[int], msg: str,
                 retry_after: Optional[int] = None):
        super().__init__(status_code, code, msg)
        self.retry_after = retry_after


def _build_query(params: dict) -> str:
    items = []
    for k, v in params.items():
        if v is None:
            continue
        items.append(
            f"{urllib.parse.quote_plus(str(k))}={urllib.parse.quote_plus(str(v))}"
        )
    return "&".join(items)


def _fmt_num(value) -> str:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"nilai non-finite tidak boleh dikirim ke API: {value!r}")
    if isinstance(value, Decimal):
        return format(value, "f")
    return format(Decimal(str(value)), "f")


_REDACT_PARAMS = ("signature", "apiKey", "api_key", "secret", "token")
_REDACT_RE = re.compile(
    r"(?i)\b(" + "|".join(_REDACT_PARAMS) + r")=[^&\s\"'>]*"
)


class BinanceSpotClient:
    @staticmethod
    def _redact(text) -> str:
        try:
            return _REDACT_RE.sub(r"\1=<REDACTED>", str(text))
        except Exception:
            return "<pesan tidak dapat diredaksi>"

    def __init__(self, api_key: str, api_secret: str, base_url: str, timeout: float = 10.0,
                 allow_signed: bool = True, rate_limit_state_file: str | None = None,
                 rate_limit_limit: int = 6000, rate_limit_safety_margin: int = 100):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.allow_signed = allow_signed
        self.session = requests.Session()
        if allow_signed and self.api_key:
            self.session.headers.update({"X-MBX-APIKEY": self.api_key})
        self._time_offset_ms = 0
        self._rate_limiter = SharedRequestWeightLimiter(
            rate_limit_state_file,
            limit=rate_limit_limit,
            safety_margin=rate_limit_safety_margin,
        )

        self.used_weight_1m = 0
        self.used_weight_ts = 0.0
        self.blocked_until = 0.0

    def sync_time(self) -> None:
        server_time = self.get_server_time()
        local_time = int(time.time() * 1000)
        self._time_offset_ms = server_time - local_time
        logger.info("Sinkronisasi waktu server selesai. Offset = %d ms", self._time_offset_ms)

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    @staticmethod
    def _estimate_request_weight(method: str, path: str,
                                 params: dict | None = None) -> int:
        method = str(method or "GET").upper()
        params = params or {}
        if path == "/api/v3/ticker/24hr":
            return 2 if params.get("symbol") else 80
        if path == "/api/v3/exchangeInfo":
            # Docs resmi Binance (General endpoints, akses 2026-10-01):
            # bobot 20 untuk SEMUA kombinasi parameter, termasuk saat
            # difilter per symbol (sejak 2023-08-25, naik dari 10 ke 20).
            return 20
        if path == "/api/v3/klines":
            return 2
        if path == "/api/v3/depth":
            limit = int(params.get("limit", 100) or 100)
            return 5 if limit <= 100 else 25 if limit <= 500 else 50 if limit <= 1000 else 250
        if path == "/api/v3/ticker/bookTicker":
            return 4 if not params.get("symbol") else 2
        if path in ("/api/v3/ticker/price", "/api/v3/time"):
            return 2
        if path == "/api/v3/account" and method == "GET":
            return 20
        if path == "/api/v3/order" and method == "GET":
            return 4
        if path == "/api/v3/orderList" and method == "GET":
            return 4
        if path == "/api/v3/openOrders" and method == "GET":
            return 6 if params.get("symbol") else 80
        return 1

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        signed: bool = False,
        max_retries: int = 3,
    ) -> Any:
        if signed and not self.allow_signed:
            raise SignedEndpointBlockedError(
                f"Request bertanda tangan ke {method} {path} diblokir: klien ini "
                "dibuat dengan allow_signed=False (mode data pasar publik/PAPER)."
            )
        base_params = dict(params or {})

        def build_url() -> str:
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

        request_weight = self._estimate_request_weight(method, path, base_params)
        last_exc = None
        skip_shared_block = False
        for attempt in range(1, max_retries + 1):
            try:
                try:
                    if skip_shared_block:
                        self._rate_limiter.record_retry_after_server_wait(request_weight)
                    else:
                        self._rate_limiter.reserve(request_weight)
                    skip_shared_block = False
                except RateLimitBlockedError as exc:
                    raise BinanceRateLimitError(
                        429, None,
                        "shared rate limiter masih memblokir request sebelum dikirim",
                        retry_after=int(exc.retry_after),
                    ) from exc
                url = build_url()
                resp = self.session.request(method, url, timeout=self.timeout)

                self._record_used_weight(resp.headers)

                if resp.status_code >= 400:
                    try:
                        body = resp.json()
                        code = body.get("code")
                        msg = body.get("msg", resp.text)
                    except ValueError:
                        code = None
                        msg = resp.text

                    if resp.status_code in (429, 418):
                        retry_after = self._parse_retry_after(resp.headers)
                        self._note_rate_limited(resp.status_code, retry_after)
                        raise BinanceRateLimitError(
                            resp.status_code, code, msg, retry_after=retry_after
                        )

                    raise BinanceAPIError(resp.status_code, code, msg)
                if resp.text == "":
                    return {}
                return resp.json()
            except (requests.exceptions.RequestException, BinanceAPIError) as exc:
                last_exc = exc

                if isinstance(exc, BinanceRateLimitError):
                    wait = max(5.0, float(exc.retry_after)) if exc.retry_after else min(60 * attempt, 180)
                    logger.error(
                        "Kena batas rate Binance (HTTP %s) pada %s %s. "
                        "Mundur %ds sesuai instruksi server (percobaan %d/%d).",
                        exc.status_code, method, path, wait, attempt, max_retries,
                    )
                    if exc.status_code == 418:
                        logger.error(
                            "HTTP 418: IP ini sedang diblokir Binance sampai %ds "
                            "ke depan. Permintaan dihentikan, tidak dicoba ulang.", wait,
                        )
                        raise
                    if attempt < max_retries:
                        time.sleep(wait)
                        skip_shared_block = True
                        continue
                    raise

                if isinstance(exc, BinanceAPIError) and exc.code == -1021:
                    logger.warning("Timestamp meleset, sinkronisasi ulang jam server...")
                    self.sync_time()
                if attempt < max_retries:
                    wait = min(2 ** attempt, 10)
                    logger.warning(
                        "Request %s %s gagal (percobaan %d/%d): %s. Tunggu %ds.",
                        method, path, attempt, max_retries, self._redact(exc), wait,
                    )
                    time.sleep(wait)
                else:
                    logger.warning(
                        "Request %s %s gagal (percobaan terakhir %d/%d): %s.",
                        method, path, attempt, max_retries, self._redact(exc),
                    )
        raise last_exc

    def _record_used_weight(self, headers) -> None:
        try:
            raw = headers.get("x-mbx-used-weight-1m") or headers.get("X-MBX-USED-WEIGHT-1M")
            if raw is not None:
                self.used_weight_1m = int(raw)
                self.used_weight_ts = time.time()
                self._rate_limiter.observe_server_weight(self.used_weight_1m)
        except (TypeError, ValueError):
            pass

    @staticmethod
    def _parse_retry_after(headers) -> Optional[int]:
        try:
            raw = headers.get("Retry-After") or headers.get("retry-after")
            if raw is not None:
                return max(1, int(float(raw)))
        except (TypeError, ValueError):
            pass
        return None

    def _note_rate_limited(self, status_code: int, retry_after: Optional[int]) -> None:
        wait = retry_after if retry_after else (300 if status_code == 418 else 60)
        self.blocked_until = max(getattr(self, "blocked_until", 0.0), time.time() + wait)
        self._rate_limiter.block(wait)

    def is_rate_limited(self) -> bool:
        return time.time() < getattr(self, "blocked_until", 0.0)

    def weight_headroom(self, limit: int = 6000) -> float:
        ts = getattr(self, "used_weight_ts", 0.0)
        if not ts or time.time() - ts > 60:
            return 1.0
        used = getattr(self, "used_weight_1m", 0)
        return max(0.0, 1.0 - used / float(limit or 6000))

    def get_server_time(self) -> int:
        data = self._request("GET", "/api/v3/time")
        return int(data["serverTime"])

    def get_exchange_info(self, symbol: Optional[str] = None) -> dict:
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/api/v3/exchangeInfo", params)

    def get_ticker_24hr_all(self) -> list:
        return self._request("GET", "/api/v3/ticker/24hr", {})

    def get_klines(self, symbol: str, interval: str, limit: int = 500,
                    start_time_ms: Optional[int] = None, end_time_ms: Optional[int] = None) -> list:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        return self._request("GET", "/api/v3/klines", params)

    def get_book_ticker(self, symbol: str, max_retries: int = 3) -> dict:
        return self._request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol},
                             max_retries=max_retries)

    def get_depth(self, symbol: str, limit: int = 100, max_retries: int = 3) -> dict:
        return self._request("GET", "/api/v3/depth",
                             {"symbol": symbol, "limit": limit}, max_retries=max_retries)

    def get_book_ticker_all(self) -> list:
        return self._request("GET", "/api/v3/ticker/bookTicker")

    def get_price(self, symbol: str, max_retries: int = 3) -> float:
        data = self._request("GET", "/api/v3/ticker/price", {"symbol": symbol},
                             max_retries=max_retries)
        return float(data["price"])

    def get_account(self) -> dict:
        return self._request("GET", "/api/v3/account", signed=True)

    def new_market_order(self, symbol: str, side: str, quantity: Optional[float] = None,
                          quote_order_qty: Optional[float] = None,
                          new_client_order_id: Optional[str] = None) -> dict:
        params = {"symbol": symbol, "side": side, "type": "MARKET"}
        if quantity is not None:
            params["quantity"] = _fmt_num(quantity)
        if quote_order_qty is not None:
            params["quoteOrderQty"] = _fmt_num(quote_order_qty)
        if new_client_order_id:
            params["newClientOrderId"] = str(new_client_order_id)
        return self._request("POST", "/api/v3/order", params, signed=True,
                             max_retries=1)

    def new_stop_loss_order(self, symbol: str, quantity: float,
                            stop_price: float,
                            new_client_order_id: str | None = None) -> dict:
        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "STOP_LOSS",
            "quantity": _fmt_num(quantity),
            "stopPrice": _fmt_num(stop_price),
        }
        if new_client_order_id:
            params["newClientOrderId"] = str(new_client_order_id)
        return self._request("POST", "/api/v3/order", params, signed=True,
                             max_retries=1)

    def new_oco_sell_order(self, symbol: str, quantity: float,
                           above_price: float, above_stop_price: float,
                           below_price: float, below_stop_price: float,
                           list_client_order_id: str,
                           above_client_order_id: str,
                           below_client_order_id: str) -> dict:
        params = {
            "symbol": symbol,
            "side": "SELL",
            "quantity": _fmt_num(quantity),
            "listClientOrderId": str(list_client_order_id),
            "aboveType": "TAKE_PROFIT_LIMIT",
            "aboveClientOrderId": str(above_client_order_id),
            "abovePrice": _fmt_num(above_price),
            "aboveStopPrice": _fmt_num(above_stop_price),
            "aboveTimeInForce": "GTC",
            "belowType": "STOP_LOSS_LIMIT",
            "belowClientOrderId": str(below_client_order_id),
            "belowPrice": _fmt_num(below_price),
            "belowStopPrice": _fmt_num(below_stop_price),
            "belowTimeInForce": "GTC",
            "newOrderRespType": "FULL",
        }
        return self._request("POST", "/api/v3/orderList/oco", params,
                             signed=True, max_retries=1)

    def get_order_list(self, order_list_id: int | None = None,
                       list_client_order_id: str | None = None) -> dict:
        params = {}
        if order_list_id is not None:
            params["orderListId"] = order_list_id
        if list_client_order_id is not None:
            params["origClientOrderId"] = str(list_client_order_id)
        return self._request("GET", "/api/v3/orderList", params, signed=True)

    def cancel_order_list(self, symbol: str, order_list_id: int | None = None,
                          list_client_order_id: str | None = None) -> dict:
        params = {"symbol": symbol}
        if order_list_id is not None:
            params["orderListId"] = order_list_id
        if list_client_order_id is not None:
            params["listClientOrderId"] = str(list_client_order_id)
        return self._request("DELETE", "/api/v3/orderList", params,
                             signed=True, max_retries=1)

    def get_dust_convertible(self, account_type: str = "SPOT") -> dict:
        return self._request("POST", "/sapi/v1/asset/dust-btc",
                             {"accountType": account_type}, signed=True,
                             max_retries=1)

    def convert_dust(self, assets: list, account_type: str = "SPOT") -> dict:
        params = {"asset": ",".join(assets), "accountType": account_type}
        return self._request("POST", "/sapi/v1/asset/dust", params, signed=True,
                             max_retries=1)


class SymbolFilters:
    def __init__(self, step_size: Decimal, min_qty: Decimal, min_notional: Decimal,
                 tick_size: Decimal, max_qty: Decimal = Decimal("0"),
                 max_notional: Decimal = Decimal("0"),
                 quote_order_qty_market_allowed: bool = True):
        self.step_size = step_size
        self.min_qty = min_qty
        self.min_notional = min_notional
        self.tick_size = tick_size
        self.max_qty = max_qty
        self.max_notional = max_notional
        self.quote_order_qty_market_allowed = quote_order_qty_market_allowed

    @classmethod
    def from_symbol_data(cls, sym_data: dict) -> "SymbolFilters":
        symbol = sym_data.get("symbol", "?")

        lot_step = Decimal("0")
        lot_min = Decimal("0")
        market_lot_step = Decimal("0")
        market_lot_min = Decimal("0")
        lot_max_values = []
        market_lot_max_values = []
        min_notional_values = []
        max_notional_values = []
        tick_size = Decimal("0.01")

        for f in sym_data.get("filters", []):
            ftype = f.get("filterType")
            if ftype == "LOT_SIZE":
                lot_step = Decimal(f["stepSize"])
                lot_min = Decimal(f["minQty"])
                if Decimal(f.get("maxQty", "0")) > 0:
                    lot_max_values.append(Decimal(f["maxQty"]))
            elif ftype == "MARKET_LOT_SIZE":
                market_lot_step = Decimal(f["stepSize"])
                market_lot_min = Decimal(f["minQty"])
                if Decimal(f.get("maxQty", "0")) > 0:
                    market_lot_max_values.append(Decimal(f["maxQty"]))
            elif ftype == "MIN_NOTIONAL":
                if f.get("applyToMarket", True):
                    min_notional_values.append(Decimal(f["minNotional"]))
            elif ftype == "NOTIONAL":
                if f.get("applyMinToMarket", True):
                    min_notional_values.append(Decimal(f["minNotional"]))
                if f.get("applyMaxToMarket", False) and Decimal(f.get("maxNotional", "0")) > 0:
                    max_notional_values.append(Decimal(f["maxNotional"]))
            elif ftype == "PRICE_FILTER":
                tick_size = Decimal(f["tickSize"])

        step_size = market_lot_step if market_lot_step > 0 else lot_step
        min_qty = market_lot_min if market_lot_min > 0 else lot_min
        if lot_step > 0:
            step_size = max(step_size, lot_step)
        min_qty = max(min_qty, lot_min)
        max_qty_values = lot_max_values + market_lot_max_values
        max_qty = min(max_qty_values) if max_qty_values else Decimal("0")
        min_notional = max(min_notional_values) if min_notional_values else Decimal("0")
        max_notional = min(max_notional_values) if max_notional_values else Decimal("0")

        if step_size <= 0:
            step_size = Decimal("0.00000001")
            logger.warning(
                "LOT_SIZE dan MARKET_LOT_SIZE sama-sama 0 untuk %s -- "
                "memakai fallback stepSize=%s. Mohon cek manual di Binance.",
                symbol, step_size,
            )

        return cls(
            step_size=step_size,
            min_qty=min_qty,
            min_notional=min_notional,
            tick_size=tick_size,
            max_qty=max_qty,
            max_notional=max_notional,
            quote_order_qty_market_allowed=bool(
                sym_data.get("quoteOrderQtyMarketAllowed", True)
            ),
        )

    def round_qty(self, qty: float) -> float:
        q = Decimal(str(qty))
        if self.step_size <= 0:
            return float(q)
        steps = (q / self.step_size).to_integral_value(rounding=ROUND_DOWN)
        rounded = steps * self.step_size
        return float(rounded)

    def round_price(self, price: float, *, rounding=ROUND_DOWN) -> float:
        p = Decimal(str(price))
        if self.tick_size <= 0:
            return float(p)
        steps = (p / self.tick_size).to_integral_value(rounding=rounding)
        return float(steps * self.tick_size)


def build_trading_symbols(exchange_info: dict) -> set:
    out = set()
    for sym_data in exchange_info.get("symbols", []):
        symbol = sym_data.get("symbol")
        if not symbol:
            continue
        if str(sym_data.get("status", "")).upper() != "TRADING":
            continue
        if sym_data.get("isSpotTradingAllowed") is False:
            continue
        out.add(symbol)
    return out


def build_filters_cache(exchange_info: dict) -> dict:
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
