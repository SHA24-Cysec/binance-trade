"""
Perkakas bersama untuk tes PAPER. Semua tes berjalan TANPA jaringan: data pasar
(order book & harga) di-inject lewat objek palsu, jadi deterministik dan cepat.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

import pytest

# Pastikan root repo ada di sys.path saat pytest dijalankan dari mana pun.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from binance_client import SymbolFilters  # noqa: E402
from paper_engine import PaperMatchingEngine  # noqa: E402
from paper_store import PaperStore  # noqa: E402


class FakeMarket:
    """Sumber data pasar palsu yang bisa diatur per tes."""

    def __init__(self, book=None, price=10.0):
        self.book = book or {"bids": [], "asks": []}
        self.price = price

    def depth(self, symbol):
        return self.book

    def get_price(self, symbol):
        return self.price


def make_filters(step="1", min_qty="1", min_notional="5", tick="0.01") -> SymbolFilters:
    return SymbolFilters(
        step_size=Decimal(step), min_qty=Decimal(min_qty),
        min_notional=Decimal(min_notional), tick_size=Decimal(tick),
    )


@pytest.fixture
def base_config():
    """Config minimal untuk mesin simulasi (fee 0,1% + diskon BNB -> 0,075%)."""
    return {
        "QUOTE_ASSET": "USDT",
        "TAKER_FEE_PCT": 0.1,
        "MAKER_FEE_PCT": 0.1,
        "USE_BNB_FEE_DISCOUNT": True,
        "PAPER_LIMIT_ORDER_TIMEOUT_SECONDS": 1,
        "PAPER_DEPTH_LIMIT": 100,
    }


@pytest.fixture
def make_engine(base_config, tmp_path):
    """Factory: buat (engine, store, market) dengan filter & saldo awal tertentu."""

    def _factory(initial_balances=None, filters=None, market=None):
        store = PaperStore(str(tmp_path / "acct.json"),
                           initial_balances or {"USDT": 10000.0})
        filt = filters or make_filters()
        mkt = market or FakeMarket()
        eng = PaperMatchingEngine(
            config=base_config,
            store=store,
            filters_provider=lambda s: filt,
            depth_provider=mkt.depth,
            price_provider=mkt.get_price,
            quote_asset="USDT",
        )
        return eng, store, mkt

    return _factory
