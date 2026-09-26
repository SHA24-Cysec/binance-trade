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
from typing import Optional

from binance_client import BinanceSpotClient
from config import get_base_url
from exchange_client import ExchangeClient
from market_data import MarketDataProvider

logger = logging.getLogger("live_client")


class LiveClient(ExchangeClient):
    mode = "LIVE"

    def __init__(self, config: dict) -> None:
        self.config = config
        api_key = config.get("API_KEY", "")
        api_secret = config.get("API_SECRET", "")
        # Klien bertanda tangan untuk order & akun (uang asli).
        self.signed = BinanceSpotClient(
            api_key, api_secret, get_base_url(config), allow_signed=True,
            rate_limit_state_file=config.get("RATE_LIMIT_STATE_FILE"),
            rate_limit_limit=int(config.get("RATE_LIMIT_WEIGHT_LIMIT", 6000) or 6000),
            rate_limit_safety_margin=int(config.get("RATE_LIMIT_SAFETY_MARGIN", 100) or 100),
        )
        # Data pasar bersama (WS + REST keyless), sumber sama seperti PAPER.
        self.market = MarketDataProvider(config)
        logger.info("LiveClient siap (UANG ASLI). endpoint=%s", get_base_url(config))

    # --- Infrastruktur / data pasar ---
    def sync_time(self) -> None:
        # Sinkronisasi jam pada klien bertanda tangan (yang butuh timestamp).
        self.signed.sync_time()
        self.market.sync_time()

    def close(self) -> None:
        self.market.close()

    def get_exchange_info(self, symbol: Optional[str] = None) -> dict:
        return self.market.get_exchange_info(symbol=symbol)

    def get_klines(self, symbol: str, interval: str, limit: int = 500,
                   start_time_ms: Optional[int] = None,
                   end_time_ms: Optional[int] = None) -> list:
        return self.market.get_klines(symbol, interval, limit, start_time_ms, end_time_ms)

    def get_ticker_24hr_all(self) -> list:
        return self.market.get_ticker_24hr_all()

    def get_price(self, symbol: str, max_retries: int = 3) -> float:
        return self.market.get_price(symbol, max_retries=max_retries)

    def get_book_ticker(self, symbol: str, max_retries: int = 3) -> dict:
        return self.market.get_book_ticker(symbol, max_retries=max_retries)

    def get_depth(self, symbol: str, limit: int = 100) -> dict:
        return self.market.get_depth(symbol, limit=limit)

    # --- Akun & order (SUNGGUHAN, bertanda tangan) ---
    def get_account(self) -> dict:
        return self.signed.get_account()

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
            params["quantity"] = quantity
        if quote_order_qty is not None:
            params["quoteOrderQty"] = quote_order_qty
        if price is not None:
            params["price"] = price
        if stop_price is not None:
            params["stopPrice"] = stop_price
        if time_in_force is not None:
            params["timeInForce"] = time_in_force
        if new_client_order_id is not None:
            params["newClientOrderId"] = new_client_order_id
        # POST order non-idempotent. Status jaringan UNKNOWN harus
        # direkonsiliasi memakai clientOrderId, bukan diulang otomatis.
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
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if orig_client_order_id is not None:
            params["origClientOrderId"] = orig_client_order_id
        return self.signed._request("GET", "/api/v3/order", params, signed=True)

    def cancel_order(self, symbol: str, order_id: Optional[int] = None,
                     orig_client_order_id: Optional[str] = None) -> dict:
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if orig_client_order_id is not None:
            params["origClientOrderId"] = orig_client_order_id
        return self.signed._request("DELETE", "/api/v3/order", params, signed=True)

    def get_open_orders(self, symbol: Optional[str] = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        return self.signed._request("GET", "/api/v3/openOrders", params, signed=True)

    def get_dust_convertible(self, account_type: str = "SPOT") -> dict:
        return self.signed.get_dust_convertible(account_type)

    def convert_dust(self, assets: list, account_type: str = "SPOT") -> dict:
        return self.signed.convert_dust(assets, account_type)


# ==== RINGKASAN AUDIT (live_client.py) =================================
# Sintaks/tipe: mematuhi ABC ExchangeClient penuh; type hints lengkap.
# Pemisahan: signed client (order/akun) terpisah dari market (data publik);
#   data pasar identik dengan PAPER (satu MarketDataProvider).
# Keamanan: API key hanya dipakai untuk klien signed; dipanggil hanya di LIVE.
# new_order/get_order/cancel_order/get_open_orders meneruskan ke endpoint resmi
#   Binance (bertanda tangan). new_market_order tetap lewat helper yang sudah
#   teruji di binance_client (format qty via _fmt_num).
# Kebocoran rahasia: API key tidak pernah dicetak ke log.
# =======================================================================
