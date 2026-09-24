"""
PaperClient: implementasi ExchangeClient untuk mode PAPER.

- Data pasar: ASLI dari Binance produksi publik lewat MarketDataProvider
  (REST keyless + WebSocket). Tidak ada API key, tidak ada tanda tangan.
- Eksekusi order, fee, dan saldo: DISIMULASIKAN lokal oleh PaperMatchingEngine,
  disimpan persisten oleh PaperStore.
- Pengaman keras: endpoint bertanda tangan (akun/order/dust asli) TIDAK PERNAH
  dipanggil. get_dust_convertible/convert_dust melempar SignedEndpointBlockedError.
  MarketDataProvider sendiri memakai klien REST allow_signed=False, jadi bahkan
  jika ada kode nakal yang mencoba, request signed gagal keras.

Bentuk respons dibuat identik dengan REST Binance agar bot tidak perlu cabang
khusus PAPER.

Versi acuan: Python 3.10+.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from binance_client import SymbolFilters, build_filters_cache, SignedEndpointBlockedError
from exchange_client import ExchangeClient
from market_data import MarketDataProvider
from paper_engine import PaperMatchingEngine
from paper_store import PaperStore

logger = logging.getLogger("paper_client")


class PaperClient(ExchangeClient):
    mode = "PAPER"

    def __init__(self, config: dict) -> None:
        self.config = config
        self.quote_asset = str(config.get("QUOTE_ASSET", "USDT")).upper()

        self.market = MarketDataProvider(config)
        state_file = config.get("PAPER_ACCOUNT_STATE_FILE", "pump_paper_account_paper.json")
        self.store = PaperStore(state_file, config.get("PAPER_INITIAL_BALANCES"))

        # Cache filter simbol (dibangun dari exchangeInfo publik).
        self._filters_cache: dict[str, SymbolFilters] = {}

        self.engine = PaperMatchingEngine(
            config=config,
            store=self.store,
            filters_provider=self._get_filters,
            depth_provider=self._depth_for_engine,
            price_provider=self.market.get_price,
            quote_asset=self.quote_asset,
        )
        logger.info("PaperClient siap. State=%s | saldo awal=%s | fee taker=%s%%%s",
                    state_file, config.get("PAPER_INITIAL_BALANCES"),
                    config.get("TAKER_FEE_PCT"),
                    " (diskon BNB aktif)" if config.get("USE_BNB_FEE_DISCOUNT") else "")

    # ------------------------------------------------------------------
    # Filter simbol
    # ------------------------------------------------------------------
    def _ensure_filters(self, force: bool = False) -> None:
        if self._filters_cache and not force:
            return
        info = self.market.get_exchange_info(force=force)
        self._filters_cache = build_filters_cache(info)
        logger.info("Filter simbol PAPER dimuat/diperbarui (%d simbol).",
                    len(self._filters_cache))

    def _get_filters(self, symbol: str) -> Optional[SymbolFilters]:
        s = symbol.upper()
        if s not in self._filters_cache:
            self._ensure_filters()
        if s not in self._filters_cache:
            # Simbol tidak ada di cache: coba ambil khusus simbol itu.
            try:
                info = self.market.get_exchange_info(symbol=s)
                syms = info.get("symbols", []) if isinstance(info, dict) else []
                if syms:
                    self._filters_cache[s] = SymbolFilters.from_symbol_data(syms[0])
            except Exception as exc:  # noqa: BLE001
                logger.debug("Gagal ambil filter %s: %s", s, exc)
        return self._filters_cache.get(s)

    def _depth_for_engine(self, symbol: str) -> dict:
        # Staleness guard: depth REST snapshot selalu SEGAR saat diambil, jadi
        # order simulasi tidak pernah jalan di atas data basi.
        limit = int(self.config.get("PAPER_DEPTH_LIMIT", 100))
        return self.market.get_depth(symbol, limit=limit)

    # ------------------------------------------------------------------
    # Infrastruktur / data pasar (delegasi ke MarketDataProvider)
    # ------------------------------------------------------------------
    def sync_time(self) -> None:
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

    def get_book_ticker(self, symbol: str) -> dict:
        return self.market.get_book_ticker(symbol)

    def get_depth(self, symbol: str, limit: int = 100) -> dict:
        return self.market.get_depth(symbol, limit=limit)

    # ------------------------------------------------------------------
    # Akun & order (disimulasikan)
    # ------------------------------------------------------------------
    def get_account(self) -> dict:
        # Sebelum melaporkan saldo, proses order terbuka (limit/stop) supaya
        # saldo mencerminkan fill terbaru.
        try:
            self.engine.process_open_orders()
        except Exception as exc:  # noqa: BLE001
            logger.debug("process_open_orders saat get_account: %s", exc)
        return self.store.account_snapshot()

    def new_market_order(self, symbol: str, side: str,
                         quantity: Optional[float] = None,
                         quote_order_qty: Optional[float] = None,
                         new_client_order_id: Optional[str] = None) -> dict:
        order = self.engine.place_order(
            symbol=symbol, side=side, order_type="MARKET",
            quantity=quantity, quote_order_qty=quote_order_qty,
            client_order_id=new_client_order_id,
        )
        return self._public_order(order)

    def new_order(self, symbol: str, side: str, order_type: str,
                  quantity: Optional[float] = None,
                  price: Optional[float] = None,
                  stop_price: Optional[float] = None,
                  time_in_force: Optional[str] = None,
                  quote_order_qty: Optional[float] = None,
                  new_client_order_id: Optional[str] = None) -> dict:
        order = self.engine.place_order(
            symbol=symbol, side=side, order_type=order_type,
            quantity=quantity, price=price, stop_price=stop_price,
            time_in_force=time_in_force, quote_order_qty=quote_order_qty,
            client_order_id=new_client_order_id,
        )
        return self._public_order(order)

    def get_order(self, symbol: str, order_id: Optional[int] = None,
                  orig_client_order_id: Optional[str] = None) -> dict:
        self.engine.process_open_orders()
        o = self.store.find_order(order_id=order_id, orig_client_order_id=orig_client_order_id)
        if o is None:
            from binance_client import BinanceAPIError
            raise BinanceAPIError(400, -2013, "Order does not exist.")
        return self._public_order(o)

    def cancel_order(self, symbol: str, order_id: Optional[int] = None,
                     orig_client_order_id: Optional[str] = None) -> dict:
        o = self.engine.cancel_order(symbol, order_id=order_id,
                                     orig_client_order_id=orig_client_order_id)
        return self._public_order(o)

    def get_open_orders(self, symbol: Optional[str] = None) -> list:
        self.engine.process_open_orders()
        return [self._public_order(o) for o in self.store.get_open_orders(symbol)]

    # ------------------------------------------------------------------
    # Endpoint bertanda tangan: DILARANG di PAPER
    # ------------------------------------------------------------------
    def get_dust_convertible(self, account_type: str = "SPOT") -> dict:
        raise SignedEndpointBlockedError(
            "get_dust_convertible (POST /sapi/v1/asset/dust-btc) diblokir di mode PAPER: "
            "endpoint bertanda tangan tidak boleh dipanggil.")

    def convert_dust(self, assets: list, account_type: str = "SPOT") -> dict:
        raise SignedEndpointBlockedError(
            "convert_dust (POST /sapi/v1/asset/dust) diblokir di mode PAPER: "
            "endpoint bertanda tangan tidak boleh dipanggil.")

    # ------------------------------------------------------------------
    # Util
    # ------------------------------------------------------------------
    @staticmethod
    def _public_order(order: dict) -> dict:
        """Buang kunci internal (_lockedAsset, dll) sebelum dikembalikan ke
        pemanggil, agar bentuknya bersih seperti respons Binance."""
        return {k: v for k, v in order.items() if not k.startswith("_")}


# ==== RINGKASAN AUDIT (paper_client.py) ================================
# Sintaks/tipe: type hints lengkap; mematuhi ABC ExchangeClient (semua metode
#   abstrak diimplementasikan) -> tidak bisa lupa metode (TypeError saat init).
# Guard PAPER: get_dust_convertible/convert_dust melempar
#   SignedEndpointBlockedError; MarketDataProvider memakai REST allow_signed=
#   False -> tidak ada jalur signed sama sekali. Diuji di tests.
# Staleness: fill order memakai depth REST snapshot yang segar; get_price
#   memakai WS hanya bila cukup segar (logika di MarketDataProvider).
# Race: penulisan state seluruhnya di dalam engine yang memegang store.lock.
# Kebersihan respons: _public_order membuang kunci internal.
# Kebocoran rahasia: tidak menyentuh API key sama sekali.
# =======================================================================
