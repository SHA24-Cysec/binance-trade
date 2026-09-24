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
from typing import Any, Optional

# Diekspor ulang di sini supaya modul lain bisa `from exchange_client import
# SignedEndpointBlockedError` tanpa perlu tahu ia sebenarnya didefinisikan di
# lapisan REST tingkat rendah.
from binance_client import BinanceAPIError, SignedEndpointBlockedError  # noqa: F401

logger = logging.getLogger("exchange_client")


class ExchangeClient(ABC):
    """Kontrak yang dipenuhi LiveClient dan PaperClient.

    Bentuk argumen dan bentuk respons dibuat identik dengan REST Binance Spot
    asli, supaya kode strategi tidak perlu cabang khusus per mode.
    """

    #: "PAPER" atau "LIVE". Diisi oleh subclass.
    mode: str = "?"

    # ------------------------------------------------------------------
    # Infrastruktur
    # ------------------------------------------------------------------
    @abstractmethod
    def sync_time(self) -> None:
        """Sinkronisasi jam lokal dengan server (relevan untuk request signed)."""

    def close(self) -> None:
        """Tutup sumber daya (mis. thread WebSocket). Aman dipanggil berulang.
        Default no-op; subclass menimpanya bila perlu."""
        return None

    # ------------------------------------------------------------------
    # Data pasar publik (implementasi sama untuk PAPER & LIVE)
    # ------------------------------------------------------------------
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
    def get_book_ticker(self, symbol: str) -> dict: ...

    @abstractmethod
    def get_depth(self, symbol: str, limit: int = 100) -> dict: ...

    # ------------------------------------------------------------------
    # Akun & order (berbeda antara PAPER dan LIVE)
    # ------------------------------------------------------------------
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
        """Order generik (LIMIT/STOP_LOSS/TAKE_PROFIT/...).

        Bot pump saat ini HANYA memakai new_market_order, tetapi mesin
        simulasi mendukung tipe order penuh untuk pengujian & kesiapan masa
        depan. LiveClient meneruskannya ke POST /api/v3/order.
        """

    @abstractmethod
    def get_order(self, symbol: str, order_id: Optional[int] = None,
                  orig_client_order_id: Optional[str] = None) -> dict: ...

    @abstractmethod
    def cancel_order(self, symbol: str, order_id: Optional[int] = None,
                     orig_client_order_id: Optional[str] = None) -> dict: ...

    @abstractmethod
    def get_open_orders(self, symbol: Optional[str] = None) -> list: ...

    @abstractmethod
    def get_dust_convertible(self, account_type: str = "SPOT") -> dict:
        """Hanya bermakna di LIVE. Di PAPER melempar SignedEndpointBlockedError
        (konversi dust bukan bagian dari simulasi eksekusi)."""

    @abstractmethod
    def convert_dust(self, assets: list, account_type: str = "SPOT") -> dict:
        """Hanya bermakna di LIVE. Di PAPER melempar SignedEndpointBlockedError."""


def create_exchange_client(config: dict) -> ExchangeClient:
    """Buat implementasi ExchangeClient sesuai MODE di config.

    - "PAPER" -> PaperClient (simulasi lokal, data pasar publik).
    - "LIVE"  -> LiveClient  (order & saldo asli bertanda tangan).
    - selain itu -> config.require_valid_mode() melempar InvalidModeError,
      sehingga pemanggil berhenti keras. TIDAK PERNAH jatuh diam-diam ke LIVE.

    Import subclass dilakukan di dalam fungsi (lazy) untuk menghindari
    ketergantungan melingkar saat modul-modul saling meng-import.
    """
    from config import require_valid_mode  # lokal: hindari circular import

    mode = require_valid_mode(config)  # melempar bila tidak valid
    if mode == "LIVE":
        from live_client import LiveClient
        logger.info("Membuat LiveClient (MODE=LIVE): order & saldo SUNGGUHAN.")
        return LiveClient(config)
    # mode == "PAPER"
    from paper_client import PaperClient
    logger.info("Membuat PaperClient (MODE=PAPER): eksekusi & saldo DISIMULASIKAN lokal.")
    return PaperClient(config)


# ==== RINGKASAN AUDIT (exchange_client.py) ==============================
# Sintaks/tipe: ABC dengan @abstractmethod; type hints lengkap; Python 3.10+.
# Edge case MODE: create_exchange_client memakai require_valid_mode() yang
#   melempar InvalidModeError untuk nilai tak dikenal/kosong -> tidak ada
#   jalur diam-diam ke LIVE. Diuji di tests/test_mode_and_guard.py.
# Circular import: import LiveClient/PaperClient + require_valid_mode dilakukan
#   lazy di dalam fungsi; hanya BinanceAPIError/SignedEndpointBlockedError yang
#   di-import di tingkat modul (aman, binance_client tidak meng-import file ini).
# Kebocoran rahasia: tidak ada; file ini tidak menyentuh API key.
# Race condition: tidak ada state bersama di tingkat modul.
# =======================================================================
