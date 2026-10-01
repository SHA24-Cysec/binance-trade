"""Regresi audit T2-2 (2026-10-01): estimasi bobot request BinanceSpotClient.

Estimasi bobot untuk endpoint bertanda tangan sebelumnya mengembalikan 1 untuk
semua, padahal dokumentasi resmi Binance (diakses 2026-10-01) menetapkan:
- GET /api/v3/account      : 20  (changelog resmi 2023-08-25: 10 -> 20)
- GET /api/v3/order        : 4   (changelog resmi 2023-08-25: 2 -> 4)
- GET /api/v3/orderList    : 4   (changelog resmi 2023-08-25: 2 -> 4)
- GET /api/v3/openOrders   : 6 dengan symbol, 80 tanpa symbol
- POST /api/v3/order       : 1
Sumber: https://developers.binance.com/docs/binance-spot-api-docs (REST API,
endpoint Market/Trade/Account) dan CHANGELOG resmi
https://github.com/binance/binance-spot-api-docs/blob/master/CHANGELOG.md

Underestimasi bobot membuat SharedRequestWeightLimiter meloloskan lebih banyak
request daripada kuota IP; sinkronisasi lewat header x-mbx-used-weight-1m
(binance_client.py _record_used_weight) hanya terjadi SETELAH respons diterima.
"""

from __future__ import annotations

from trading.clients.binance_client import BinanceSpotClient


def test_bobot_endpoint_bertanda_tangan_sesuai_dokumentasi_resmi():
    est = BinanceSpotClient._estimate_request_weight
    assert est("GET", "/api/v3/account", {}) == 20
    assert est("GET", "/api/v3/order", {"symbol": "BTCUSDT", "orderId": 1}) == 4
    assert est("GET", "/api/v3/orderList", {"origClientOrderId": "x"}) == 4
    assert est("GET", "/api/v3/openOrders", {"symbol": "BTCUSDT"}) == 6
    assert est("GET", "/api/v3/openOrders", {}) == 80
    assert est("POST", "/api/v3/order", {"symbol": "BTCUSDT", "side": "BUY"}) == 1
    assert est("DELETE", "/api/v3/order", {"symbol": "BTCUSDT", "orderId": 1}) == 1


def test_bobot_endpoint_publik_tetap_sesuai_dokumentasi():
    est = BinanceSpotClient._estimate_request_weight
    assert est("GET", "/api/v3/ticker/24hr", {}) == 80
    assert est("GET", "/api/v3/ticker/24hr", {"symbol": "BTCUSDT"}) == 2
    assert est("GET", "/api/v3/exchangeInfo", {}) == 20
    assert est("GET", "/api/v3/exchangeInfo", {"symbol": "BTCUSDT"}) == 1
    assert est("GET", "/api/v3/klines", {"symbol": "BTCUSDT"}) == 2
    assert est("GET", "/api/v3/depth", {"limit": 100}) == 5
    assert est("GET", "/api/v3/depth", {"limit": 500}) == 25
    assert est("GET", "/api/v3/depth", {"limit": 1000}) == 50
    assert est("GET", "/api/v3/depth", {"limit": 5000}) == 250
    assert est("GET", "/api/v3/ticker/bookTicker", {"symbol": "BTCUSDT"}) == 2
    assert est("GET", "/api/v3/ticker/bookTicker", {}) == 4
    assert est("GET", "/api/v3/ticker/price", {"symbol": "BTCUSDT"}) == 2
    assert est("GET", "/api/v3/time", {}) == 2
