"""Regresi temuan SEDANG-03 (audit 2026-09-27) untuk live_client.new_order.

Sebelum perbaikan, new_order meneruskan float mentah ke params request.
Python merender float kecil sebagai notasi ilmiah (str(0.0000833) ->
'8.33e-05') yang ditolak Binance dengan -1100/-1013, dan NaN/inf bisa lolos
sampai ke bursa. Test ini mengunci kontrak baru: semua angka melewati
_fmt_num (bentuk desimal biasa, non-finite ditolak keras) dan POST order
tidak pernah di-retry otomatis (max_retries=1, non-idempotent).

Tidak ada koneksi nyata: LiveClient dibuat tanpa __init__ dan self.signed
diganti fake yang hanya merekam panggilan _request.
"""

from __future__ import annotations

import pytest

from live_client import LiveClient


class _FakeSigned:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _request(self, method, path, params, signed=False, max_retries=3):
        self.calls.append({
            "method": method, "path": path, "params": dict(params),
            "signed": signed, "max_retries": max_retries,
        })
        return {"orderId": 1, "clientOrderId": params.get("newClientOrderId"),
                "status": "NEW"}


def _client() -> tuple[LiveClient, _FakeSigned]:
    # object.__new__: konstruktor asli membangun BinanceSpotClient dan
    # MarketDataProvider sungguhan; untuk menguji format parameter cukup
    # atribut signed yang dipakai new_order.
    client = object.__new__(LiveClient)
    fake = _FakeSigned()
    client.signed = fake
    return client, fake


def test_new_order_formats_small_numbers_without_scientific_notation() -> None:
    client, fake = _client()
    client.new_order("TESTUSDT", "SELL", "STOP_LOSS_LIMIT",
                     quantity=0.00001, price=0.0000833,
                     stop_price=0.00002, time_in_force="GTC",
                     new_client_order_id="sl-x")

    assert len(fake.calls) == 1
    params = fake.calls[0]["params"]
    assert params["quantity"] == "0.00001"
    assert params["price"] == "0.0000833"
    assert params["stopPrice"] == "0.00002"
    for key in ("quantity", "price", "stopPrice"):
        assert "e" not in str(params[key]).lower(), (
            f"{key} terkirim sebagai notasi ilmiah: {params[key]!r}")
    assert params["timeInForce"] == "GTC"
    assert params["newClientOrderId"] == "sl-x"


def test_new_order_formats_quote_order_qty_and_omits_absent_params() -> None:
    client, fake = _client()
    client.new_order("TESTUSDT", "BUY", "MARKET", quote_order_qty=0.00005)

    params = fake.calls[0]["params"]
    assert params["quoteOrderQty"] == "0.00005"
    for absent in ("quantity", "price", "stopPrice", "timeInForce",
                   "newClientOrderId"):
        assert absent not in params


def test_new_order_rejects_non_finite_values_before_sending() -> None:
    client, fake = _client()
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            client.new_order("TESTUSDT", "SELL", "MARKET", quantity=bad)
    # Tidak satu pun request boleh terkirim ketika angka non-finite.
    assert fake.calls == []


def test_new_order_is_signed_and_never_auto_retried() -> None:
    client, fake = _client()
    client.new_order("TESTUSDT", "BUY", "MARKET", quantity=1.0)

    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v3/order"
    assert call["signed"] is True
    # POST order non-idempotent: retry otomatis dilarang (rekonsiliasi
    # memakai clientOrderId adalah satu-satunya jalur pemulihan yang aman).
    assert call["max_retries"] == 1
