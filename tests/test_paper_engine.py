"""
Tes mesin simulasi order PAPER (paper_engine.py).

Mencakup skenario yang diminta: penolakan LOT_SIZE & MIN_NOTIONAL, market order
melewati beberapa level order book, partial fill, limit tidak terisi lalu
timeout, gap harga melewati stop-loss, fee dari aset yang benar (+ diskon BNB),
dan idempotensi clientOrderId.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading.clients.binance_client import BinanceAPIError
from conftest import FakeMarket, make_filters


def test_reject_lot_size(make_engine):
    book = {"asks": [["10.00", "100"]], "bids": [["9.90", "100"]]}
    eng, store, _ = make_engine(filters=make_filters(min_qty="1", step="1"),
                                market=FakeMarket(book))
    with pytest.raises(BinanceAPIError) as ei:
        eng.place_order("PEPEUSDT", "BUY", "MARKET", quantity=0.5)
    assert ei.value.code == -1013
    assert "LOT_SIZE" in ei.value.msg


def test_reject_min_notional(make_engine):
    book = {"asks": [["10.00", "100"]], "bids": [["9.90", "100"]]}
    eng, store, _ = make_engine(
        filters=make_filters(min_qty="1", step="1", min_notional="1000"),
        market=FakeMarket(book))
    with pytest.raises(BinanceAPIError) as ei:
        eng.place_order("PEPEUSDT", "BUY", "MARKET", quantity=1)
    assert ei.value.code == -1013
    assert "NOTIONAL" in ei.value.msg


def test_market_buy_walks_multiple_levels(make_engine):
    book = {"asks": [["10.00", "3"], ["10.10", "3"], ["10.50", "10"]],
            "bids": [["9.90", "100"]]}
    eng, store, _ = make_engine(initial_balances={"USDT": 1000.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="5"),
                                market=FakeMarket(book))
    r = eng.place_order("PEPEUSDT", "BUY", "MARKET", quantity=5)
    assert r["status"] == "FILLED"
    assert Decimal(r["executedQty"]) == Decimal("5")
    assert Decimal(r["cummulativeQuoteQty"]) == Decimal("50.20")
    avg = Decimal(r["cummulativeQuoteQty"]) / Decimal(r["executedQty"])
    assert avg > Decimal("10.00")
    assert len(r["fills"]) == 2


def test_partial_fill_expires_remainder(make_engine):
    book = {"asks": [["10.00", "3"], ["10.10", "3"]], "bids": [["9.90", "100"]]}
    eng, store, _ = make_engine(initial_balances={"USDT": 10000.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="5"),
                                market=FakeMarket(book))
    r = eng.place_order("PEPEUSDT", "BUY", "MARKET", quantity=20)
    assert r["status"] == "EXPIRED"
    assert Decimal(r["executedQty"]) == Decimal("6")
    assert Decimal(r["origQty"]) == Decimal("20")


def test_fee_from_correct_asset_with_bnb_discount(make_engine):
    book = {"asks": [["10.00", "100"]], "bids": [["10.00", "100"]]}
    eng, store, _ = make_engine(initial_balances={"USDT": 1000.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="5"),
                                market=FakeMarket(book))
    r = eng.place_order("PEPEUSDT", "BUY", "MARKET", quantity=10)
    assert store.get_free("USDT") == Decimal("900")
    assert store.get_free("PEPE") == Decimal("10") - Decimal("0.0075")
    assert Decimal(store.state["total_fees"]["PEPE"]) == Decimal("0.0075")
    assert r["fills"][0]["commissionAsset"] == "PEPE"

    r2 = eng.place_order("PEPEUSDT", "SELL", "MARKET", quantity=5)
    assert r2["fills"][0]["commissionAsset"] == "USDT"
    assert store.get_free("USDT") == Decimal("900") + Decimal("50") - Decimal("0.0375")


def test_sell_qty_never_exceeds_balance(make_engine):
    book = {"asks": [["10.00", "100"]], "bids": [["10.00", "100"]]}
    eng, store, _ = make_engine(initial_balances={"USDT": 1000.0},
                                filters=make_filters(min_qty="0.00000001", step="0.00000001",
                                                     min_notional="5"),
                                market=FakeMarket(book))
    eng.place_order("PEPEUSDT", "BUY", "MARKET", quantity=10)
    free_base = store.get_free("PEPE")
    assert free_base < Decimal("10")
    with pytest.raises(BinanceAPIError) as ei:
        eng.place_order("PEPEUSDT", "SELL", "MARKET", quantity=10)
    assert ei.value.code == -2010


def test_limit_not_filled_then_timeout(make_engine):
    book = {"asks": [["10.00", "100"]], "bids": [["9.90", "100"]]}
    eng, store, _ = make_engine(initial_balances={"USDT": 1000.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="5", tick="0.01"),
                                market=FakeMarket(book))
    r = eng.place_order("PEPEUSDT", "BUY", "LIMIT", quantity=10, price=9.00,
                        time_in_force="GTC")
    assert r["status"] == "NEW"
    assert store.get_free("USDT") == Decimal("910")
    assert store.get_locked("USDT") == Decimal("90")
    created = int(r["_createdMs"])
    changed = eng.process_open_orders(now_ms=created + 5000)
    statuses = {o["orderId"]: o["status"] for o in changed}
    assert statuses[r["orderId"]] == "EXPIRED"
    assert store.get_free("USDT") == Decimal("1000")
    assert store.get_locked("USDT") == Decimal("0")


def test_limit_fills_when_price_crosses(make_engine):
    book = {"asks": [["9.00", "100"]], "bids": [["8.90", "100"]]}
    eng, store, _ = make_engine(initial_balances={"USDT": 1000.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="5", tick="0.01"),
                                market=FakeMarket(book))
    r = eng.place_order("PEPEUSDT", "BUY", "LIMIT", quantity=10, price=9.00,
                        time_in_force="GTC")
    assert r["status"] == "FILLED"
    assert Decimal(r["executedQty"]) == Decimal("10")


def test_gap_past_stop_loss(make_engine):
    book = {"asks": [["7.90", "100"]], "bids": [["8.00", "100"]]}
    market = FakeMarket(book, price=8.00)
    eng, store, _ = make_engine(initial_balances={"PEPE": 100.0, "USDT": 0.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="5"),
                                market=market)
    r = eng.place_order("PEPEUSDT", "SELL", "STOP_LOSS", quantity=10, stop_price=9.00)
    assert r["status"] == "NEW"
    changed = eng.process_open_orders()
    filled = [o for o in changed if o["orderId"] == r["orderId"]]
    assert filled and filled[0]["status"] == "FILLED"
    avg = Decimal(filled[0]["cummulativeQuoteQty"]) / Decimal(filled[0]["executedQty"])
    assert avg == Decimal("8.00")
    assert avg < Decimal("9.00")


def test_idempotent_client_order_id(make_engine):
    book = {"asks": [["10.00", "100"]], "bids": [["10.00", "100"]]}
    eng, store, _ = make_engine(initial_balances={"USDT": 1000.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="5"),
                                market=FakeMarket(book))
    eng.place_order("PEPEUSDT", "BUY", "MARKET", quantity=1, client_order_id="dup-1")
    with pytest.raises(BinanceAPIError) as ei:
        eng.place_order("PEPEUSDT", "BUY", "MARKET", quantity=1, client_order_id="dup-1")
    assert ei.value.code == -2010
    assert "Duplicate" in ei.value.msg



def test_stop_buy_partial_fill_releases_leftover_quote_lock(make_engine):
    book = {"asks": [["2.00", "6"]], "bids": [["1.99", "100"]]}
    market = FakeMarket(book, price=2.50)
    eng, store, _ = make_engine(initial_balances={"USDT": 100.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="0"),
                                market=market)
    r = eng.place_order("PEPEUSDT", "BUY", "STOP_LOSS", quantity=10, stop_price=2.00)
    assert r["status"] == "NEW"
    assert store.get_locked("USDT") == Decimal("20")
    assert store.get_free("USDT") == Decimal("80")
    changed = eng.process_open_orders()
    order = [o for o in changed if o["orderId"] == r["orderId"]][0]
    assert order["status"] == "PARTIALLY_FILLED"
    assert Decimal(order["executedQty"]) == Decimal("6")
    assert store.get_locked("USDT") == Decimal("0")
    assert store.get_free("USDT") == Decimal("88")
    assert store.get_free("PEPE") == Decimal("6") * (Decimal(1) - Decimal("0.00075"))


def test_limit_buy_partial_fill_then_cancel_releases_exact_leftover(make_engine):
    book = {"asks": [["9.00", "4"]], "bids": [["8.00", "100"]]}
    eng, store, _ = make_engine(initial_balances={"USDT": 100.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="0"),
                                market=FakeMarket(book))
    r = eng.place_order("PEPEUSDT", "BUY", "LIMIT", quantity=10, price=10.00)
    assert r["status"] == "PARTIALLY_FILLED"
    assert Decimal(r["executedQty"]) == Decimal("4")
    assert store.get_locked("USDT") == Decimal("64")
    c = eng.cancel_order("PEPEUSDT", order_id=r["orderId"])
    assert c["status"] == "CANCELED"
    assert store.get_locked("USDT") == Decimal("0")
    assert store.get_free("USDT") == Decimal("64")
