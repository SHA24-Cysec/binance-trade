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

from binance_client import BinanceAPIError
from conftest import FakeMarket, make_filters


def test_reject_lot_size(make_engine):
    """qty di bawah minQty -> ditolak -1013 LOT_SIZE."""
    book = {"asks": [["10.00", "100"]], "bids": [["9.90", "100"]]}
    eng, store, _ = make_engine(filters=make_filters(min_qty="1", step="1"),
                                market=FakeMarket(book))
    with pytest.raises(BinanceAPIError) as ei:
        eng.place_order("PEPEUSDT", "BUY", "MARKET", quantity=0.5)
    assert ei.value.code == -1013
    assert "LOT_SIZE" in ei.value.msg


def test_reject_min_notional(make_engine):
    """notional di bawah minNotional -> ditolak -1013 NOTIONAL."""
    book = {"asks": [["10.00", "100"]], "bids": [["9.90", "100"]]}
    eng, store, _ = make_engine(
        filters=make_filters(min_qty="1", step="1", min_notional="1000"),
        market=FakeMarket(book))
    with pytest.raises(BinanceAPIError) as ei:
        eng.place_order("PEPEUSDT", "BUY", "MARKET", quantity=1)
    assert ei.value.code == -1013
    assert "NOTIONAL" in ei.value.msg


def test_market_buy_walks_multiple_levels(make_engine):
    """Market BUY berjalan melalui beberapa level ask -> harga rata-rata & slippage."""
    book = {"asks": [["10.00", "3"], ["10.10", "3"], ["10.50", "10"]],
            "bids": [["9.90", "100"]]}
    eng, store, _ = make_engine(initial_balances={"USDT": 1000.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="5"),
                                market=FakeMarket(book))
    r = eng.place_order("PEPEUSDT", "BUY", "MARKET", quantity=5)
    assert r["status"] == "FILLED"
    assert Decimal(r["executedQty"]) == Decimal("5")
    # 3@10.00 + 2@10.10 = 50.20
    assert Decimal(r["cummulativeQuoteQty"]) == Decimal("50.20")
    avg = Decimal(r["cummulativeQuoteQty"]) / Decimal(r["executedQty"])
    assert avg > Decimal("10.00")  # slippage di atas harga terbaik
    assert len(r["fills"]) == 2


def test_partial_fill_expires_remainder(make_engine):
    """Kedalaman kurang -> partial fill, sisa qty EXPIRED."""
    book = {"asks": [["10.00", "3"], ["10.10", "3"]], "bids": [["9.90", "100"]]}
    eng, store, _ = make_engine(initial_balances={"USDT": 10000.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="5"),
                                market=FakeMarket(book))
    r = eng.place_order("PEPEUSDT", "BUY", "MARKET", quantity=20)
    assert r["status"] == "EXPIRED"
    assert Decimal(r["executedQty"]) == Decimal("6")  # hanya 3+3 tersedia
    assert Decimal(r["origQty"]) == Decimal("20")


def test_fee_from_correct_asset_with_bnb_discount(make_engine):
    """BUY: fee dari BASE; SELL: fee dari QUOTE; tarif 0,075% (diskon BNB)."""
    book = {"asks": [["10.00", "100"]], "bids": [["10.00", "100"]]}
    eng, store, _ = make_engine(initial_balances={"USDT": 1000.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="5"),
                                market=FakeMarket(book))
    r = eng.place_order("PEPEUSDT", "BUY", "MARKET", quantity=10)
    # Bayar quote 100; fee dari base = 10 * 0.00075 = 0.0075 -> base bersih 9.9925
    assert store.get_free("USDT") == Decimal("900")
    assert store.get_free("PEPE") == Decimal("10") - Decimal("0.0075")
    # Bandingkan sebagai Decimal (representasi string bisa punya nol di belakang).
    assert Decimal(store.state["total_fees"]["PEPE"]) == Decimal("0.0075")
    assert r["fills"][0]["commissionAsset"] == "PEPE"

    # SELL 5: terima quote 50; fee dari quote = 50 * 0.00075 = 0.0375
    r2 = eng.place_order("PEPEUSDT", "SELL", "MARKET", quantity=5)
    assert r2["fills"][0]["commissionAsset"] == "USDT"
    # quote sebelumnya 900, + (50 - 0.0375)
    assert store.get_free("USDT") == Decimal("900") + Decimal("50") - Decimal("0.0375")


def test_sell_qty_never_exceeds_balance(make_engine):
    """Karena fee BUY dipotong dari base, saldo base < qty order -> mencegah
    jual lebih besar dari saldo (proteksi yang diminta)."""
    book = {"asks": [["10.00", "100"]], "bids": [["10.00", "100"]]}
    eng, store, _ = make_engine(initial_balances={"USDT": 1000.0},
                                filters=make_filters(min_qty="0.00000001", step="0.00000001",
                                                     min_notional="5"),
                                market=FakeMarket(book))
    eng.place_order("PEPEUSDT", "BUY", "MARKET", quantity=10)
    free_base = store.get_free("PEPE")
    assert free_base < Decimal("10")
    # Menjual seluruh qty order (10) harus gagal karena saldo < 10.
    with pytest.raises(BinanceAPIError) as ei:
        eng.place_order("PEPEUSDT", "SELL", "MARKET", quantity=10)
    assert ei.value.code == -2010


def test_limit_not_filled_then_timeout(make_engine):
    """LIMIT BUY di bawah harga pasar tidak terisi (NEW), lalu EXPIRED saat
    timeout; dana terkunci dikembalikan."""
    book = {"asks": [["10.00", "100"]], "bids": [["9.90", "100"]]}
    eng, store, _ = make_engine(initial_balances={"USDT": 1000.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="5", tick="0.01"),
                                market=FakeMarket(book))
    r = eng.place_order("PEPEUSDT", "BUY", "LIMIT", quantity=10, price=9.00,
                        time_in_force="GTC")
    assert r["status"] == "NEW"
    # Dana terkunci: 10 * 9 = 90.
    assert store.get_free("USDT") == Decimal("910")
    assert store.get_locked("USDT") == Decimal("90")
    created = int(r["_createdMs"])
    changed = eng.process_open_orders(now_ms=created + 5000)  # > timeout 1 detik
    statuses = {o["orderId"]: o["status"] for o in changed}
    assert statuses[r["orderId"]] == "EXPIRED"
    # Dana dikembalikan penuh.
    assert store.get_free("USDT") == Decimal("1000")
    assert store.get_locked("USDT") == Decimal("0")


def test_limit_fills_when_price_crosses(make_engine):
    """LIMIT BUY menjadi terisi saat ask <= harga limit (aturan konservatif)."""
    book = {"asks": [["9.00", "100"]], "bids": [["8.90", "100"]]}
    eng, store, _ = make_engine(initial_balances={"USDT": 1000.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="5", tick="0.01"),
                                market=FakeMarket(book))
    r = eng.place_order("PEPEUSDT", "BUY", "LIMIT", quantity=10, price=9.00,
                        time_in_force="GTC")
    # ask 9.00 <= limit 9.00 -> langsung terisi sebagai maker.
    assert r["status"] == "FILLED"
    assert Decimal(r["executedQty"]) == Decimal("10")


def test_gap_past_stop_loss(make_engine):
    """Stop-loss SELL: harga melompat (gap) melewati stopPrice -> terisi pada
    harga pasar setelah gap, BUKAN pada stopPrice."""
    # Punya 100 base untuk dijual. Order book saat terpicu jauh di bawah stop.
    book = {"asks": [["7.90", "100"]], "bids": [["8.00", "100"]]}
    market = FakeMarket(book, price=8.00)  # harga terkini gap di bawah stop 9.0
    eng, store, _ = make_engine(initial_balances={"PEPE": 100.0, "USDT": 0.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="5"),
                                market=market)
    r = eng.place_order("PEPEUSDT", "SELL", "STOP_LOSS", quantity=10, stop_price=9.00)
    assert r["status"] == "NEW"  # belum terpicu saat ditempatkan (menunggu proses)
    changed = eng.process_open_orders()
    filled = [o for o in changed if o["orderId"] == r["orderId"]]
    assert filled and filled[0]["status"] == "FILLED"
    avg = Decimal(filled[0]["cummulativeQuoteQty"]) / Decimal(filled[0]["executedQty"])
    # Terisi pada 8.00 (bid pasca-gap), lebih rendah dari stopPrice 9.00.
    assert avg == Decimal("8.00")
    assert avg < Decimal("9.00")


def test_idempotent_client_order_id(make_engine):
    """clientOrderId duplikat ditolak (-2010 Duplicate order sent)."""
    book = {"asks": [["10.00", "100"]], "bids": [["10.00", "100"]]}
    eng, store, _ = make_engine(initial_balances={"USDT": 1000.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="5"),
                                market=FakeMarket(book))
    eng.place_order("PEPEUSDT", "BUY", "MARKET", quantity=1, client_order_id="dup-1")
    with pytest.raises(BinanceAPIError) as ei:
        eng.place_order("PEPEUSDT", "BUY", "MARKET", quantity=1, client_order_id="dup-1")
    assert ei.value.code == -2010
    assert "Duplicate" in ei.value.msg


# ---------------------------------------------------------------------
# Regresi audit B-05: rilis sisa dana terkunci pada order stop/limit
# ---------------------------------------------------------------------

def test_stop_buy_partial_fill_releases_leftover_quote_lock(make_engine):
    """STOP_LOSS BUY market: kunci qty x stopPrice; terpicu dengan kedalaman
    kurang -> partial fill lalu finalisasi. Sisa kunci WAJIB dilepas penuh.

    Bug lama: _release_leftover_lock menghitung ulang dari order["price"],
    yang bernilai "0" untuk stop market, sehingga sisa kunci quote tidak
    pernah kembali ke free (regresi B-05)."""
    book = {"asks": [["2.00", "6"]], "bids": [["1.99", "100"]]}
    market = FakeMarket(book, price=2.50)  # menembus stop BUY 2.00 ke atas
    eng, store, _ = make_engine(initial_balances={"USDT": 100.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="0"),
                                market=market)
    r = eng.place_order("PEPEUSDT", "BUY", "STOP_LOSS", quantity=10, stop_price=2.00)
    assert r["status"] == "NEW"
    # Terkunci 10 x stopPrice 2.00 = 20 USDT saat penempatan.
    assert store.get_locked("USDT") == Decimal("20")
    assert store.get_free("USDT") == Decimal("80")
    changed = eng.process_open_orders()
    order = [o for o in changed if o["orderId"] == r["orderId"]][0]
    assert order["status"] == "PARTIALLY_FILLED"   # hanya 6 dari 10 terisi
    assert Decimal(order["executedQty"]) == Decimal("6")
    # Terisi 6 x 2.00 = 12 USDT. Sisa kunci 8 USDT harus kembali ke free,
    # bukan menggantung di locked selamanya.
    assert store.get_locked("USDT") == Decimal("0")
    assert store.get_free("USDT") == Decimal("88")
    # Base diterima setelah fee (0.075% dengan diskon BNB dari conftest).
    assert store.get_free("PEPE") == Decimal("6") * (Decimal(1) - Decimal("0.00075"))


def test_limit_buy_partial_fill_then_cancel_releases_exact_leftover(make_engine):
    """LIMIT BUY terisi parsial pada harga LEBIH BAIK dari limit, lalu
    dibatalkan: yang dirilis adalah sisa kunci persis (kunci awal dikurangi
    spent nyata), bukan sisa qty x limitPrice (regresi B-05)."""
    book = {"asks": [["9.00", "4"]], "bids": [["8.00", "100"]]}
    eng, store, _ = make_engine(initial_balances={"USDT": 100.0},
                                filters=make_filters(min_qty="1", step="1", min_notional="0"),
                                market=FakeMarket(book))
    r = eng.place_order("PEPEUSDT", "BUY", "LIMIT", quantity=10, price=10.00)
    # Kunci awal 10 x 10.00 = 100 USDT; ask 9.00 x 4 terisi langsung.
    assert r["status"] == "PARTIALLY_FILLED"
    assert Decimal(r["executedQty"]) == Decimal("4")
    # spent nyata 4 x 9.00 = 36 (bukan 4 x 10.00), sisa kunci 64.
    assert store.get_locked("USDT") == Decimal("64")
    c = eng.cancel_order("PEPEUSDT", order_id=r["orderId"])
    assert c["status"] == "CANCELED"
    # Sisa kunci 64 (100 - 36) harus kembali penuh ke free.
    assert store.get_locked("USDT") == Decimal("0")
    assert store.get_free("USDT") == Decimal("64")
