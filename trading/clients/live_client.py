"""
LiveClient: implementasi ExchangeClient untuk mode LIVE.

- Data pasar: sama seperti PAPER, lewat MarketDataProvider (REST publik keyless
  + WebSocket). Dipakai bersama supaya sumber data identik antar mode.
- Akun & order: SUNGGUHAN lewat BinanceSpotClient bertanda tangan (uang asli).
- Dust sweep: didukung (endpoint /sapi/* bertanda tangan) -- hanya relevan LIVE.

Perbedaan satu-satunya dari PAPER ada di lapisan eksekusi & sumber saldo; logika
strategi di pump_scanner_bot.py identik.

Versi acuan: requests>=2.32.4, Python 3.10+.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from trading.clients.binance_client import BinanceAPIError, BinanceSpotClient, _fmt_num
from config.config import get_base_url
from trading.clients.exchange_client import (
    ACCOUNT_SPOT_PERMISSION_SOURCE,
    ACCOUNT_SPOT_PERMISSION_VERIFIED,
    ExchangeClient,
)
from market.market_data import MarketDataProvider

logger = logging.getLogger("live_client")


class LiveClient(ExchangeClient):
    mode = "LIVE"

    def __init__(self, config: dict) -> None:
        self.config = config
        api_key = config.get("API_KEY", "")
        api_secret = config.get("API_SECRET", "")
        self.signed = BinanceSpotClient(
            api_key, api_secret, get_base_url(config), allow_signed=True,
            rate_limit_state_file=config.get("RATE_LIMIT_STATE_FILE"),
            rate_limit_limit=int(config.get("RATE_LIMIT_WEIGHT_LIMIT", 6000) or 6000),
            rate_limit_safety_margin=int(config.get("RATE_LIMIT_SAFETY_MARGIN", 100) or 100),
        )
        self.market = MarketDataProvider(config)
        self._spot_permission_verified_until = 0.0
        self._spot_permission_logged = False
        self._account_permission_diagnostic_logged = False
        logger.info("LiveClient siap (UANG ASLI). endpoint=%s", get_base_url(config))

    def sync_time(self) -> None:
        self.signed.sync_time()
        self.market.sync_time()

    def close(self) -> None:
        try:
            self.market.close()
        finally:
            self.signed.close()

    def get_exchange_info(self, symbol: Optional[str] = None) -> dict:
        return self.market.get_exchange_info(symbol=symbol)

    def get_klines(self, symbol: str, interval: str, limit: int = 500,
                   start_time_ms: Optional[int] = None,
                   end_time_ms: Optional[int] = None) -> list:
        return self.market.get_klines(symbol, interval, limit, start_time_ms, end_time_ms)

    def get_ticker_24hr_all(self) -> list:
        return self.market.get_ticker_24hr_all()

    def get_klines_many(self, symbols, interval: str, limit: int = 500,
                        end_time_ms: Optional[int] = None,
                        max_workers: Optional[int] = None) -> dict:
        return self.market.get_klines_many(symbols, interval, limit=limit,
                                           end_time_ms=end_time_ms,
                                           max_workers=max_workers)

    def prewarm_book_ticker(self, symbols) -> None:
        return self.market.prewarm_book_ticker(symbols)

    def get_price(self, symbol: str, max_retries: int = 3) -> float:
        return self.market.get_price(symbol, max_retries=max_retries)

    def get_book_ticker(self, symbol: str, max_retries: int = 3) -> dict:
        return self.market.get_book_ticker(symbol, max_retries=max_retries)

    def get_depth(self, symbol: str, limit: int = 100) -> dict:
        return self.market.get_depth(symbol, limit=limit)

    def _verify_api_key_spot_permission(self) -> None:
        """Verifikasi izin trading milik API key, bukan hanya status akun.

        ``GET /api/v3/account`` pada sebagian respons produksi dapat tidak
        menyertakan ``permissions`` walaupun ``canTrade`` dan ``accountType``
        valid. Endpoint apiRestrictions adalah sumber resmi untuk izin API key.
        Hasil di-cache singkat agar tiap pembacaan saldo tidak menambah request.
        """
        now = time.monotonic()
        if now < self._spot_permission_verified_until:
            return
        permission = self.signed.get_api_key_permissions()
        if not isinstance(permission, dict):
            raise BinanceAPIError(
                502, None, "respons API-key permission bukan object"
            )
        if permission.get("enableReading") is not True:
            raise BinanceAPIError(
                403, None, "API key tidak memiliki izin membaca akun"
            )
        if permission.get("enableSpotAndMarginTrading") is not True:
            raise BinanceAPIError(
                403, None,
                "API key tidak mengizinkan Spot & Margin Trading",
            )
        self._spot_permission_verified_until = now + 60.0
        if not self._spot_permission_logged:
            logger.info(
                "Izin API key Spot trading terverifikasi melalui "
                "GET /sapi/v1/account/apiRestrictions."
            )
            self._spot_permission_logged = True

    @staticmethod
    def _safe_diagnostic_label(value, limit: int = 64) -> str:
        text = str(value).strip().upper()
        safe = "".join(
            char if char.isascii() and (char.isalnum() or char in "_:-") else "?"
            for char in text
        )[:limit]
        return safe or "<kosong>"

    @classmethod
    def _safe_permission_diagnostic(cls, account: dict) -> str:
        """Ringkas field permission tanpa saldo, UID, atau karakter kontrol."""
        if "permissions" not in account:
            return "<field tidak ada>"
        raw = account.get("permissions")
        if not isinstance(raw, list):
            return f"<tipe {type(raw).__name__}>"
        labels = [cls._safe_diagnostic_label(item) for item in raw[:20]]
        if len(raw) > 20:
            labels.append(f"<dan {len(raw) - 20} lainnya>")
        return repr(labels)

    def get_account(self) -> dict:
        account = self.signed.get_account()
        if not isinstance(account, dict):
            return account
        self._verify_api_key_spot_permission()
        if not self._account_permission_diagnostic_logged:
            logger.info(
                "DIAGNOSTIK AMAN permission akun: permissions=%s | "
                "canTrade=%s | accountType=%s | apiRestrictionsSpot=True. "
                "Saldo, UID, API key, dan secret tidak dicatat.",
                self._safe_permission_diagnostic(account),
                self._safe_diagnostic_label(account.get("canTrade")),
                self._safe_diagnostic_label(
                    account.get("accountType") or "<kosong>", limit=32
                ),
            )
            self._account_permission_diagnostic_logged = True
        verified = dict(account)
        verified[ACCOUNT_SPOT_PERMISSION_VERIFIED] = True
        verified[ACCOUNT_SPOT_PERMISSION_SOURCE] = (
            "GET /sapi/v1/account/apiRestrictions"
        )
        return verified

    def new_market_order(self, symbol: str, side: str,
                         quantity: Optional[float] = None,
                         quote_order_qty: Optional[float] = None,
                         new_client_order_id: Optional[str] = None) -> dict:
        return self.signed.new_market_order(
            symbol, side, quantity=quantity, quote_order_qty=quote_order_qty,
            new_client_order_id=new_client_order_id,
        )

    def new_order(self, symbol: str, side: str, order_type: str,
                  quantity: Optional[float] = None,
                  price: Optional[float] = None,
                  stop_price: Optional[float] = None,
                  time_in_force: Optional[str] = None,
                  quote_order_qty: Optional[float] = None,
                  new_client_order_id: Optional[str] = None) -> dict:
        params = {"symbol": symbol, "side": side, "type": order_type}
        if quantity is not None:
            params["quantity"] = _fmt_num(quantity)
        if quote_order_qty is not None:
            params["quoteOrderQty"] = _fmt_num(quote_order_qty)
        if price is not None:
            params["price"] = _fmt_num(price)
        if stop_price is not None:
            params["stopPrice"] = _fmt_num(stop_price)
        if time_in_force is not None:
            params["timeInForce"] = time_in_force
        if new_client_order_id is not None:
            params["newClientOrderId"] = new_client_order_id
        return self.signed._request("POST", "/api/v3/order", params,
                                    signed=True, max_retries=1)

    def place_native_stop_loss(self, symbol: str, quantity: float,
                               stop_price: float,
                               new_client_order_id: str) -> dict:
        return self.signed.new_stop_loss_order(
            symbol, quantity=quantity, stop_price=stop_price,
            new_client_order_id=new_client_order_id,
        )

    def place_native_oco(self, symbol: str, quantity: float,
                        above_price: float, above_stop_price: float,
                        below_price: float, below_stop_price: float,
                        list_client_order_id: str,
                        above_client_order_id: str,
                        below_client_order_id: str) -> dict:
        return self.signed.new_oco_sell_order(
            symbol, quantity=quantity,
            above_price=above_price, above_stop_price=above_stop_price,
            below_price=below_price, below_stop_price=below_stop_price,
            list_client_order_id=list_client_order_id,
            above_client_order_id=above_client_order_id,
            below_client_order_id=below_client_order_id,
        )

    def get_order_list(self, order_list_id: Optional[int] = None,
                       list_client_order_id: Optional[str] = None) -> dict:
        return self.signed.get_order_list(
            order_list_id=order_list_id,
            list_client_order_id=list_client_order_id,
        )

    def cancel_order_list(self, symbol: str,
                          order_list_id: Optional[int] = None,
                          list_client_order_id: Optional[str] = None) -> dict:
        return self.signed.cancel_order_list(
            symbol, order_list_id=order_list_id,
            list_client_order_id=list_client_order_id,
        )

    def get_order(self, symbol: str, order_id: Optional[int] = None,
                  orig_client_order_id: Optional[str] = None) -> dict:
        return self.signed.get_order(
            symbol, order_id=order_id,
            orig_client_order_id=orig_client_order_id,
        )

    def cancel_order(self, symbol: str, order_id: Optional[int] = None,
                     orig_client_order_id: Optional[str] = None) -> dict:
        return self.signed.cancel_order(
            symbol, order_id=order_id,
            orig_client_order_id=orig_client_order_id,
        )

    def get_open_orders(self, symbol: Optional[str] = None) -> list:
        return self.signed.get_open_orders(symbol)

    def get_dust_convertible(self, account_type: str = "SPOT") -> dict:
        return self.signed.get_dust_convertible(account_type)

    def convert_dust(self, assets: list, account_type: str = "SPOT") -> dict:
        return self.signed.convert_dust(assets, account_type)
