from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
import threading
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


class BinanceTransportError(BinanceAPIError):

    def __init__(self, msg: str):
        super().__init__(0, None, msg)


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
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise ValueError(f"nilai numerik tidak valid untuk API: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"nilai non-finite tidak boleh dikirim ke API: {value!r}")
    return format(number, "f")


QUOTE_PRECISION_DEFAULT = 8


def _quote_precision_places(value) -> int:
    """Ubah presisi quote asset menjadi jumlah desimal yang aman (0..18)."""
    try:
        places = int(value)
    except (TypeError, ValueError):
        places = QUOTE_PRECISION_DEFAULT
    return max(0, min(18, places))


def _round_down_places(value, places: int):
    """Bulatkan KE BAWAH ke jumlah desimal tertentu memakai Decimal.

    Dipakai untuk parameter quoteOrderQty. Nilai hasil perhitungan float
    (contoh 7.58 * 0.995 = 7.5421000000000005) mengandung belasan desimal dan
    ditolak bursa dengan kode -1111 "Parameter 'quoteOrderQty' has too much
    precision". Pembulatan ke bawah dipilih supaya nilai tidak pernah melebihi
    saldo yang tersedia.
    """
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if not number.is_finite():
        raise ValueError(f"nilai non-finite tidak boleh dibulatkan: {value!r}")
    step = Decimal(1).scaleb(-_quote_precision_places(places))
    hasil = number.quantize(step, rounding=ROUND_DOWN)
    # Buang nol di belakang koma supaya teks yang dikirim ringkas
    # (7.54210000 -> 7.5421) tanpa mengubah nilainya.
    teks = format(hasil, "f")
    if "." in teks:
        teks = teks.rstrip("0").rstrip(".")
    if teks in ("", "-", "-0"):
        teks = "0"
    return Decimal(teks)


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
        self._tls = threading.local()
        self._sessions: list = []
        self._sessions_lock = threading.Lock()
        self._prime_session()
        self._time_offset_ms = 0
        self._rate_limiter = SharedRequestWeightLimiter(
            rate_limit_state_file,
            limit=rate_limit_limit,
            safety_margin=rate_limit_safety_margin,
        )

        self.used_weight_1m = 0
        self.used_weight_ts = 0.0
        self.blocked_until = 0.0

    def _new_session(self):
        session = requests.Session()
        if self.allow_signed and self.api_key:
            session.headers.update({"X-MBX-APIKEY": self.api_key})
        with self._sessions_lock:
            self._sessions.append(session)
        return session

    def _prime_session(self):
        session = self._new_session()
        self._tls.session = session
        return session

    @property
    def session(self):
        session = getattr(self._tls, "session", None)
        if session is None:
            session = self._new_session()
            self._tls.session = session
        return session

    def sync_time(self) -> None:
        server_time = self.get_server_time()
        local_time = int(time.time() * 1000)
        self._time_offset_ms = server_time - local_time
        logger.info("Sinkronisasi waktu server selesai. Offset = %d ms", self._time_offset_ms)

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

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
            return 20
        if path == "/api/v3/klines":
            return 2
        if path == "/api/v3/depth":
            limit = int(params.get("limit", 100) or 100)
            return 5 if limit <= 100 else 25 if limit <= 500 else 50 if limit <= 1000 else 250
        if path == "/api/v3/ticker/bookTicker":
            return 4 if not params.get("symbol") else 2
        if path == "/api/v3/time":
            return 1
        if path == "/api/v3/ticker/price":
            return 2
        if path == "/api/v3/account" and method == "GET":
            return 20
        if path == "/sapi/v1/account/apiRestrictions" and method == "GET":
            return 1
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
        max_retries = max(1, int(max_retries or 1))

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
                        if not isinstance(body, dict):
                            raise ValueError("error body bukan object")
                        code = body.get("code")
                        msg = body.get("msg", resp.text)
                    except (ValueError, TypeError):
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
                try:
                    return resp.json()
                except ValueError as exc:
                    raise BinanceTransportError(
                        f"respons JSON tidak valid dari {method} {path}"
                    ) from exc
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

                retryable = isinstance(exc, requests.exceptions.RequestException)
                if isinstance(exc, BinanceAPIError):
                    retryable = (
                        exc.status_code == 0
                        or exc.status_code >= 500
                        or exc.code in (-1000, -1001, -1021)
                    )
                    if exc.code == -1021:
                        logger.warning("Timestamp meleset, sinkronisasi ulang jam server...")
                        self.sync_time()

                if retryable and attempt < max_retries:
                    wait = min(2 ** attempt, 10)
                    logger.warning(
                        "Request %s %s gagal sementara (percobaan %d/%d): %s. Tunggu %ds.",
                        method, path, attempt, max_retries, self._redact(exc), wait,
                    )
                    time.sleep(wait)
                    continue

                if not retryable:
                    logger.warning(
                        "Request %s %s ditolak tanpa retry: %s.",
                        method, path, self._redact(exc),
                    )
                    raise

                logger.warning(
                    "Request %s %s gagal (percobaan terakhir %d/%d): %s.",
                    method, path, attempt, max_retries, self._redact(exc),
                )

        if isinstance(last_exc, requests.exceptions.RequestException):
            raise BinanceTransportError(
                f"gangguan transport pada {method} {path}: {self._redact(last_exc)}"
            ) from last_exc
        if isinstance(last_exc, BinanceAPIError):
            raise last_exc
        raise BinanceTransportError(f"request {method} {path} gagal tanpa detail")

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

    def get_price(self, symbol: str, max_retries: int = 3) -> float:
        data = self._request("GET", "/api/v3/ticker/price", {"symbol": symbol},
                             max_retries=max_retries)
        return float(data["price"])

    def get_account(self) -> dict:
        return self._request("GET", "/api/v3/account", signed=True)

    def get_api_key_permissions(self) -> dict:
        return self._request(
            "GET", "/sapi/v1/account/apiRestrictions", signed=True
        )

    def get_order(self, symbol: str, order_id: Optional[int] = None,
                  orig_client_order_id: Optional[str] = None) -> dict:
        if (order_id is None) == (orig_client_order_id is None):
            raise ValueError("get_order wajib memakai tepat satu orderId/origClientOrderId")
        params = {"symbol": str(symbol).upper()}
        if order_id is not None:
            params["orderId"] = int(order_id)
        else:
            params["origClientOrderId"] = str(orig_client_order_id)
        return self._request("GET", "/api/v3/order", params, signed=True)

    def cancel_order(self, symbol: str, order_id: Optional[int] = None,
                     orig_client_order_id: Optional[str] = None) -> dict:
        if (order_id is None) == (orig_client_order_id is None):
            raise ValueError("cancel_order wajib memakai tepat satu orderId/origClientOrderId")
        params = {"symbol": str(symbol).upper()}
        if order_id is not None:
            params["orderId"] = int(order_id)
        else:
            params["origClientOrderId"] = str(orig_client_order_id)
        return self._request("DELETE", "/api/v3/order", params, signed=True,
                             max_retries=1)

    def get_open_orders(self, symbol: Optional[str] = None) -> list:
        params = {"symbol": str(symbol).upper()} if symbol else {}
        return self._request("GET", "/api/v3/openOrders", params, signed=True)

    def new_market_order(self, symbol: str, side: str, quantity: Optional[float] = None,
                          quote_order_qty: Optional[float] = None,
                          new_client_order_id: Optional[str] = None,
                          quote_precision: Optional[int] = None) -> dict:
        if (quantity is None) == (quote_order_qty is None):
            raise ValueError("MARKET order wajib memakai tepat satu dari quantity/quoteOrderQty")
        side = str(side).upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side order tidak valid: {side!r}")
        params = {"symbol": str(symbol).upper(), "side": side, "type": "MARKET"}
        if quantity is not None:
            if Decimal(_fmt_num(quantity)) <= 0:
                raise ValueError("quantity MARKET harus lebih besar dari nol")
            params["quantity"] = _fmt_num(quantity)
        if quote_order_qty is not None:
            # Lapis pengaman terakhir sebelum dikirim: bulatkan ke bawah sesuai
            # presisi quote asset. Tanpa ini nilai float berdesimal panjang
            # (mis. 7.5421000000000005) ditolak bursa dengan kode -1111.
            quote_order_qty = _round_down_places(
                quote_order_qty,
                _quote_precision_places(
                    quote_precision if quote_precision is not None
                    else QUOTE_PRECISION_DEFAULT
                ),
            )
            if Decimal(_fmt_num(quote_order_qty)) <= 0:
                raise ValueError("quoteOrderQty MARKET harus lebih besar dari nol")
            params["quoteOrderQty"] = _fmt_num(quote_order_qty)
        if new_client_order_id:
            params["newClientOrderId"] = str(new_client_order_id)
        return self._request("POST", "/api/v3/order", params, signed=True,
                             max_retries=1)

    def new_stop_loss_order(self, symbol: str, quantity: float,
                            stop_price: float,
                            new_client_order_id: str | None = None) -> dict:
        quantity_text = _fmt_num(quantity)
        stop_text = _fmt_num(stop_price)
        if Decimal(quantity_text) <= 0 or Decimal(stop_text) <= 0:
            raise ValueError("quantity dan stopPrice STOP_LOSS harus lebih besar dari nol")
        params = {
            "symbol": str(symbol).upper(),
            "side": "SELL",
            "type": "STOP_LOSS",
            "quantity": quantity_text,
            "stopPrice": stop_text,
            "newOrderRespType": "RESULT",
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
        numeric = [quantity, above_price, above_stop_price, below_price, below_stop_price]
        if any(Decimal(_fmt_num(value)) <= 0 for value in numeric):
            raise ValueError("quantity dan seluruh harga OCO harus lebih besar dari nol")
        ids = [str(list_client_order_id), str(above_client_order_id), str(below_client_order_id)]
        if any(not value for value in ids) or len(set(ids)) != 3:
            raise ValueError("ketiga client order ID OCO wajib terisi dan berbeda")
        params = {
            "symbol": str(symbol).upper(),
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
        if (order_list_id is None) == (list_client_order_id is None):
            raise ValueError("get_order_list wajib memakai tepat satu ID list")
        params = {}
        if order_list_id is not None:
            params["orderListId"] = int(order_list_id)
        else:
            params["origClientOrderId"] = str(list_client_order_id)
        return self._request("GET", "/api/v3/orderList", params, signed=True)

    def cancel_order_list(self, symbol: str, order_list_id: int | None = None,
                          list_client_order_id: str | None = None) -> dict:
        if (order_list_id is None) == (list_client_order_id is None):
            raise ValueError("cancel_order_list wajib memakai tepat satu ID list")
        params = {"symbol": str(symbol).upper()}
        if order_list_id is not None:
            params["orderListId"] = int(order_list_id)
        else:
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


def parse_symbol_permission_sets(
    sym_data: dict,
) -> tuple[tuple[frozenset[str], ...], bool]:
    if "permissionSets" in sym_data:
        raw_sets = sym_data.get("permissionSets")
        if not isinstance(raw_sets, list) or not raw_sets:
            return (), False
        groups: list[frozenset[str]] = []
        for raw_group in raw_sets:
            if not isinstance(raw_group, list) or not raw_group:
                return (), False
            if any(not isinstance(item, str) or not item.strip() for item in raw_group):
                return (), False
            group = frozenset(item.strip().upper() for item in raw_group)
            if not group:
                return (), False
            groups.append(group)
        return tuple(groups), True

    legacy = sym_data.get("permissions")
    if isinstance(legacy, list) and legacy:
        if any(not isinstance(item, str) or not item.strip() for item in legacy):
            return (), False
        group = frozenset(item.strip().upper() for item in legacy)
        if group:
            return (group,), True
        return (), False
    return (), False


def permission_sets_allow(
    permission_sets: tuple[frozenset[str], ...],
    account_permissions: set[str] | frozenset[str],
) -> bool:
    normalized = {str(item).strip().upper() for item in account_permissions if item}
    return bool(permission_sets) and all(bool(group & normalized) for group in permission_sets)


class SymbolFilters:

    def __init__(self, step_size: Decimal, min_qty: Decimal, min_notional: Decimal,
                 tick_size: Decimal, max_qty: Decimal = Decimal("0"),
                 max_notional: Decimal = Decimal("0"),
                 quote_order_qty_market_allowed: bool = True,
                 *, lot_step_size: Decimal | None = None,
                 lot_min_qty: Decimal | None = None,
                 lot_max_qty: Decimal | None = None,
                 limit_min_notional: Decimal | None = None,
                 limit_max_notional: Decimal | None = None,
                 min_price: Decimal = Decimal("0"),
                 max_price: Decimal = Decimal("0"),
                 order_types: set[str] | None = None,
                 permission_sets: tuple[frozenset[str], ...] | None = None,
                 permission_metadata_verified: bool = False,
                 quote_precision: int = QUOTE_PRECISION_DEFAULT):
        self.step_size = Decimal(step_size)
        self.min_qty = Decimal(min_qty)
        self.min_notional = Decimal(min_notional)
        self.tick_size = Decimal(tick_size)
        self.max_qty = Decimal(max_qty)
        self.max_notional = Decimal(max_notional)
        self.quote_order_qty_market_allowed = bool(quote_order_qty_market_allowed)

        self.lot_step_size = Decimal(lot_step_size if lot_step_size is not None else step_size)
        self.lot_min_qty = Decimal(lot_min_qty if lot_min_qty is not None else min_qty)
        self.lot_max_qty = Decimal(lot_max_qty if lot_max_qty is not None else max_qty)
        self.limit_min_notional = Decimal(
            limit_min_notional if limit_min_notional is not None else min_notional
        )
        self.limit_max_notional = Decimal(
            limit_max_notional if limit_max_notional is not None else max_notional
        )
        self.min_price = Decimal(min_price)
        self.max_price = Decimal(max_price)
        self.order_types = set(order_types or {
            "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT",
        })
        self.permission_sets = tuple(permission_sets or ())
        self.permission_metadata_verified = bool(
            permission_metadata_verified and self.permission_sets
        )
        # Presisi quote asset (quoteAssetPrecision dari exchangeInfo). Dipakai
        # untuk membulatkan quoteOrderQty sebelum dikirim ke bursa.
        self.quote_precision = _quote_precision_places(quote_precision)

        if self.step_size <= 0 or self.lot_step_size <= 0 or self.tick_size <= 0:
            raise ValueError("stepSize/tickSize simbol wajib lebih besar dari nol")
        if self.min_qty < 0 or self.lot_min_qty < 0:
            raise ValueError("minQty simbol tidak boleh negatif")
        if self.max_qty > 0 and self.max_qty < self.min_qty:
            raise ValueError("maxQty MARKET lebih kecil dari minQty")
        if self.lot_max_qty > 0 and self.lot_max_qty < self.lot_min_qty:
            raise ValueError("maxQty LOT_SIZE lebih kecil dari minQty")

    @staticmethod
    def _common_step(first: Decimal, second: Decimal) -> Decimal:
        values = [value for value in (first, second) if value > 0]
        if not values:
            raise ValueError("LOT_SIZE dan MARKET_LOT_SIZE tidak memiliki stepSize aktif")
        if len(values) == 1:
            return values[0]
        places = max(0, *(-value.as_tuple().exponent for value in values))
        scale = 10 ** places
        integers = [int(value * scale) for value in values]
        common = math.lcm(*integers)
        return Decimal(common) / Decimal(scale)

    @classmethod
    def from_symbol_data(cls, sym_data: dict) -> "SymbolFilters":
        if not isinstance(sym_data, dict):
            raise ValueError("data simbol exchangeInfo bukan object")
        symbol = str(sym_data.get("symbol") or "?")
        raw_filters = sym_data.get("filters")
        if not isinstance(raw_filters, list):
            raise ValueError(f"filters exchangeInfo {symbol} tidak tersedia")

        by_type = {
            str(item.get("filterType")): item
            for item in raw_filters if isinstance(item, dict) and item.get("filterType")
        }
        lot = by_type.get("LOT_SIZE")
        price_filter = by_type.get("PRICE_FILTER")
        if not isinstance(lot, dict) or not isinstance(price_filter, dict):
            raise ValueError(f"LOT_SIZE/PRICE_FILTER wajib tersedia untuk {symbol}")

        lot_step = Decimal(str(lot["stepSize"]))
        lot_min = Decimal(str(lot["minQty"]))
        lot_max = Decimal(str(lot.get("maxQty", "0")))
        tick_size = Decimal(str(price_filter["tickSize"]))
        min_price = Decimal(str(price_filter.get("minPrice", "0")))
        max_price = Decimal(str(price_filter.get("maxPrice", "0")))
        if lot_step <= 0 or tick_size <= 0:
            raise ValueError(f"stepSize/tickSize tidak aktif untuk {symbol}")

        market = by_type.get("MARKET_LOT_SIZE")
        market_step = Decimal("0")
        market_min = Decimal("0")
        market_max = Decimal("0")
        if isinstance(market, dict):
            market_step = Decimal(str(market.get("stepSize", "0")))
            market_min = Decimal(str(market.get("minQty", "0")))
            market_max = Decimal(str(market.get("maxQty", "0")))

        effective_step = cls._common_step(lot_step, market_step)
        effective_min = max(lot_min, market_min)
        max_values = [value for value in (lot_max, market_max) if value > 0]
        effective_max = min(max_values) if max_values else Decimal("0")

        limit_min_values: list[Decimal] = []
        market_min_values: list[Decimal] = []
        limit_max_values: list[Decimal] = []
        market_max_values: list[Decimal] = []
        for item in raw_filters:
            if not isinstance(item, dict):
                continue
            filter_type = item.get("filterType")
            if filter_type == "MIN_NOTIONAL":
                value = Decimal(str(item["minNotional"]))
                if value > 0:
                    limit_min_values.append(value)
                    if bool(item.get("applyToMarket", True)):
                        market_min_values.append(value)
            elif filter_type == "NOTIONAL":
                minimum = Decimal(str(item.get("minNotional", "0")))
                maximum = Decimal(str(item.get("maxNotional", "0")))
                if minimum > 0:
                    limit_min_values.append(minimum)
                    if bool(item.get("applyMinToMarket", False)):
                        market_min_values.append(minimum)
                if maximum > 0:
                    limit_max_values.append(maximum)
                    if bool(item.get("applyMaxToMarket", False)):
                        market_max_values.append(maximum)

        if not limit_min_values:
            raise ValueError(f"MIN_NOTIONAL/NOTIONAL minimum tidak tersedia untuk {symbol}")
        limit_min = max(limit_min_values)
        market_min_notional = max(market_min_values) if market_min_values else Decimal("0")
        limit_max = min(limit_max_values) if limit_max_values else Decimal("0")
        market_max_notional = min(market_max_values) if market_max_values else Decimal("0")

        quote_allowed = sym_data.get("quoteOrderQtyMarketAllowed")
        if not isinstance(quote_allowed, bool):
            quote_allowed = False
        raw_order_types = sym_data.get("orderTypes")
        if not isinstance(raw_order_types, list) or not raw_order_types:
            raise ValueError(f"orderTypes tidak tersedia untuk {symbol}")
        order_types = {str(value).upper() for value in raw_order_types if value}
        if "MARKET" not in order_types:
            raise ValueError(f"MARKET order tidak didukung untuk {symbol}")

        permission_sets, permission_metadata_verified = parse_symbol_permission_sets(
            sym_data
        )

        raw_quote_precision = sym_data.get("quoteAssetPrecision")
        if raw_quote_precision is None:
            raw_quote_precision = sym_data.get("quotePrecision")
        quote_precision = _quote_precision_places(
            raw_quote_precision if raw_quote_precision is not None
            else QUOTE_PRECISION_DEFAULT
        )

        return cls(
            step_size=effective_step,
            min_qty=effective_min,
            min_notional=market_min_notional,
            tick_size=tick_size,
            max_qty=effective_max,
            max_notional=market_max_notional,
            quote_order_qty_market_allowed=quote_allowed,
            lot_step_size=lot_step,
            lot_min_qty=lot_min,
            lot_max_qty=lot_max,
            limit_min_notional=limit_min,
            limit_max_notional=limit_max,
            min_price=min_price,
            max_price=max_price,
            order_types=order_types,
            permission_sets=permission_sets,
            permission_metadata_verified=permission_metadata_verified,
            quote_precision=quote_precision,
        )

    @staticmethod
    def _round_step(value: float, step: Decimal, *, rounding=ROUND_DOWN) -> float:
        number = Decimal(str(value))
        steps = (number / step).to_integral_value(rounding=rounding)
        return float(steps * step)

    def round_qty(self, qty: float) -> float:
        return self._round_step(qty, self.step_size)

    def round_limit_qty(self, qty: float) -> float:
        return self._round_step(qty, self.lot_step_size)

    def round_price(self, price: float, *, rounding=ROUND_DOWN) -> float:
        return self._round_step(price, self.tick_size, rounding=rounding)

    def round_quote_amount(self, value: float) -> float:
        """Bulatkan nominal quote (USDT) ke presisi quote asset bursa.

        Wajib dipakai sebelum mengirim quoteOrderQty: hasil perhitungan float
        seperti 7.58 * 0.995 = 7.5421000000000005 akan ditolak dengan
        -1111 "Parameter 'quoteOrderQty' has too much precision".
        """
        return float(_round_down_places(value, self.quote_precision))

    def market_qty_valid(self, qty: float) -> bool:
        value = Decimal(str(qty))
        return (
            value >= self.min_qty
            and (self.max_qty <= 0 or value <= self.max_qty)
            and value % self.step_size == 0
        )

    def limit_qty_valid(self, qty: float) -> bool:
        value = Decimal(str(qty))
        return (
            value >= self.lot_min_qty
            and (self.lot_max_qty <= 0 or value <= self.lot_max_qty)
            and value % self.lot_step_size == 0
        )

    def price_valid(self, price: float) -> bool:
        value = Decimal(str(price))
        return (
            value > 0
            and (self.min_price <= 0 or value >= self.min_price)
            and (self.max_price <= 0 or value <= self.max_price)
            and value % self.tick_size == 0
        )


def build_trading_symbols(
    exchange_info: dict,
    account_permissions: set[str] | frozenset[str] | None = None,
) -> set:
    out = set()
    for sym_data in exchange_info.get("symbols", []):
        symbol = sym_data.get("symbol")
        if not symbol:
            continue
        if str(sym_data.get("status", "")).upper() != "TRADING":
            continue
        if sym_data.get("isSpotTradingAllowed") is False:
            continue
        if account_permissions is not None:
            if sym_data.get("isSpotTradingAllowed") is not True:
                continue
            permission_sets, verified = parse_symbol_permission_sets(sym_data)
            if not verified or not permission_sets_allow(
                permission_sets, account_permissions
            ):
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
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            logger.debug("Lewati parsing filter untuk %s: %s", symbol, exc)
    return cache
