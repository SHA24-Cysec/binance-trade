"""
Lapisan abstraksi klien exchange.

Logika strategi bot (pump_scanner_bot.py) HANYA bicara ke antarmuka
`ExchangeClient` di sini, tidak pernah tahu mode mana yang aktif. Ada dua
implementasi konkret:

- `LiveClient` (live_client.py)  -> order & saldo SUNGGUHAN lewat endpoint
                                    Binance bertanda tangan (uang asli).
- `PaperClient` (paper_client.py) -> order, fee, dan saldo DISIMULASIKAN
                                    lokal; data pasar tetap ASLI dari produksi.

Satu-satunya perbedaan antara PAPER dan LIVE ada di lapisan eksekusi order dan
sumber saldo. Data pasar (harga, order book, kline, exchangeInfo) identik dan
berasal dari Binance produksi publik untuk KEDUA mode (lihat market_data.py).

Factory `create_exchange_client(config)` memilih implementasi berdasarkan MODE
di config. MODE yang tidak dikenal / kosong / typo membuat bot BERHENTI dengan
pesan jelas (via require_valid_mode di config.py), tidak pernah diam-diam jatuh
ke LIVE.

Versi acuan: Python 3.10+ (memakai typing modern + abc).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger("exchange_client")

# Metadata internal yang hanya ditambahkan oleh LiveClient setelah endpoint
# resmi API-key permission Binance mengonfirmasi izin Spot trading.
ACCOUNT_SPOT_PERMISSION_VERIFIED = "_pump_bot_spot_permission_verified"
ACCOUNT_SPOT_PERMISSION_SOURCE = "_pump_bot_spot_permission_source"


class ExchangeClient(ABC):

    mode: str = "?"

    @abstractmethod
    def sync_time(self) -> None:
        pass

    def close(self) -> None:
        return None

    @abstractmethod
    def get_exchange_info(self, symbol: Optional[str] = None) -> dict: ...

    @abstractmethod
    def get_klines(self, symbol: str, interval: str, limit: int = 500,
                   start_time_ms: Optional[int] = None,
                   end_time_ms: Optional[int] = None) -> list: ...

    @abstractmethod
    def get_ticker_24hr_all(self) -> list: ...

    @abstractmethod
    def get_price(self, symbol: str, max_retries: int = 3) -> float: ...

    @abstractmethod
    def get_book_ticker(self, symbol: str, max_retries: int = 3) -> dict: ...

    @abstractmethod
    def get_depth(self, symbol: str, limit: int = 100) -> dict: ...

    @abstractmethod
    def get_account(self) -> dict: ...

    @abstractmethod
    def new_market_order(self, symbol: str, side: str,
                         quantity: Optional[float] = None,
                         quote_order_qty: Optional[float] = None,
                         new_client_order_id: Optional[str] = None) -> dict: ...

    @abstractmethod
    def new_order(self, symbol: str, side: str, order_type: str,
                  quantity: Optional[float] = None,
                  price: Optional[float] = None,
                  stop_price: Optional[float] = None,
                  time_in_force: Optional[str] = None,
                  quote_order_qty: Optional[float] = None,
                  new_client_order_id: Optional[str] = None) -> dict:
        pass

    def place_native_stop_loss(self, symbol: str, quantity: float,
                               stop_price: float,
                               new_client_order_id: str) -> dict:
        raise NotImplementedError("native stop loss tidak tersedia pada client ini")

    def place_native_oco(self, symbol: str, quantity: float,
                        above_price: float, above_stop_price: float,
                        below_price: float, below_stop_price: float,
                        list_client_order_id: str,
                        above_client_order_id: str,
                        below_client_order_id: str) -> dict:
        raise NotImplementedError("native OCO tidak tersedia pada client ini")

    @abstractmethod
    def get_order(self, symbol: str, order_id: Optional[int] = None,
                  orig_client_order_id: Optional[str] = None) -> dict: ...

    @abstractmethod
    def cancel_order(self, symbol: str, order_id: Optional[int] = None,
                     orig_client_order_id: Optional[str] = None) -> dict: ...

    def get_order_list(self, order_list_id: Optional[int] = None,
                       list_client_order_id: Optional[str] = None) -> dict:
        raise NotImplementedError("query order list tidak tersedia pada client ini")

    def cancel_order_list(self, symbol: str,
                          order_list_id: Optional[int] = None,
                          list_client_order_id: Optional[str] = None) -> dict:
        raise NotImplementedError("cancel order list tidak tersedia pada client ini")

    @abstractmethod
    def get_open_orders(self, symbol: Optional[str] = None) -> list: ...

    @abstractmethod
    def get_dust_convertible(self, account_type: str = "SPOT") -> dict:
        pass

    @abstractmethod
    def convert_dust(self, assets: list, account_type: str = "SPOT") -> dict:
        pass


def create_exchange_client(config: dict) -> ExchangeClient:
    from config.config import require_valid_mode

    mode = require_valid_mode(config)
    if mode == "LIVE":
        from trading.clients.live_client import LiveClient
        logger.info("Membuat LiveClient (MODE=LIVE): order & saldo SUNGGUHAN.")
        return LiveClient(config)
    from trading.clients.paper_client import PaperClient
    logger.info("Membuat PaperClient (MODE=PAPER): eksekusi & saldo DISIMULASIKAN lokal.")
    return PaperClient(config)
