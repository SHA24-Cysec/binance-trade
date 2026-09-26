"""Regresi perbaikan KRITIS-01 (audit 2026-09-27).

Sebelum perbaikan, SEMUA BinanceAPIError pada pemasangan/rekonsiliasi
proteksi native diperlakukan UNKNOWN dan menyalakan _native_stop_exit_blocked.
Untuk penolakan DETERMINISTIK (order dijamin tidak tercipta oleh Binance,
misal -2010 saldo kurang atau -1013 filter) hal itu membuat posisi telanjang
permanen: tidak ada proteksi di bursa DAN exit lokal terblokir setiap loop.

Test di file ini mengunci kontrak baru:
- Penolakan deterministik saat POST -> intent FAILED, exit lokal TIDAK diblokir,
  fallback native stop tetap berjalan.
- Error tidak pasti (5xx/kode tak dikenal) -> tetap UNKNOWN + blokir (fail-closed).
- GET "does not exist" pada intent yang TIDAK PERNAH terkonfirmasi tercipta dan
  sudah cukup tua -> intent dibersihkan, exit lokal hidup kembali.
- GET "does not exist" pada intent yang SUDAH terkonfirmasi tercipta -> tetap
  fail-closed (tidak boleh disimpulkan aman).
"""

from __future__ import annotations

from decimal import Decimal

from binance_client import BinanceAPIError, SymbolFilters
from config import PUMP_CONFIG
import pump_scanner_bot as bot
import state as state_mod


def _config(tmp_path) -> dict:
    cfg = dict(PUMP_CONFIG)
    cfg.update({
        "STATE_FILE": str(tmp_path / "state.json"),
        "CONTROL_FILE": str(tmp_path / "control.json"),
        "QUOTE_ASSET": "USDT",
        "MODE": "LIVE",
        "USE_STOP_LOSS": True,
        "USE_TP": True,
        "USE_NATIVE_OCO": True,
        "USE_NATIVE_STOP_LOSS": True,
        "SL_PCT": 3.0,
        "TP_PCT": 5.0,
        "NATIVE_OCO_LIMIT_BUFFER_PCT": 0.1,
    })
    return cfg


def _filters() -> SymbolFilters:
    return SymbolFilters(step_size=Decimal("0.01"), min_qty=Decimal("0.01"),
                         min_notional=Decimal("5"), tick_size=Decimal("0.0001"))


def _position_state() -> dict:
    state = dict(bot.DEFAULT_STATE)
    state.update({"current_symbol": "TESTUSDT", "entry_price": 100.0,
                  "qty": 2.0, "sl_pct": 3.0, "tp_pct": 5.0,
                  "exit_source": "FIXED"})
    return state


class _BookTicker:
    def get_book_ticker(self, symbol, max_retries=3):
        return {"bidPrice": "100", "askPrice": "100.1"}


def test_definitive_reject_marks_oco_failed_without_blocking_local_exit(tmp_path) -> None:
    class Client(_BookTicker):
        def place_native_oco(self, *args, **kwargs):
            raise BinanceAPIError(400, -2010, "Account has insufficient balance.")

    cfg = _config(tmp_path)
    state = _position_state()

    assert bot._arm_native_oco(Client(), cfg, _filters(), state) is False
    assert state["native_oco"]["status"] == "FAILED"
    # Inti perbaikan: exit lokal TIDAK boleh terblokir untuk penolakan pasti.
    assert state["_native_stop_exit_blocked"] is False
    assert state["reconciliation_required"] is True


def test_uncertain_error_still_fails_closed_on_oco_post(tmp_path) -> None:
    class Client(_BookTicker):
        def place_native_oco(self, *args, **kwargs):
            raise BinanceAPIError(503, None, "Service unavailable.")

    cfg = _config(tmp_path)
    state = _position_state()

    assert bot._arm_native_oco(Client(), cfg, _filters(), state) is False
    assert state["native_oco"]["status"] == "UNKNOWN"
    assert state["_native_stop_exit_blocked"] is True


def test_definitive_oco_reject_falls_back_to_native_stop(tmp_path) -> None:
    class Client(_BookTicker):
        def __init__(self):
            self.stop_calls = 0

        def place_native_oco(self, *args, **kwargs):
            raise BinanceAPIError(400, -1013, "Filter failure: PRICE_FILTER")

        def place_native_stop_loss(self, symbol, quantity, stop_price,
                                   new_client_order_id):
            self.stop_calls += 1
            return {"orderId": 9001, "clientOrderId": new_client_order_id,
                    "status": "NEW"}

    cfg = _config(tmp_path)
    state = _position_state()
    client = Client()

    assert bot._ensure_native_protection(client, cfg, _filters(), state) is True
    assert client.stop_calls == 1
    assert state["native_oco"] is None
    assert state["native_stop"]["status"] == "NEW"
    assert state["_native_stop_exit_blocked"] is False


def test_unconfirmed_missing_order_list_unblocks_local_exit(tmp_path) -> None:
    class Client:
        def get_order_list(self, order_list_id=None, list_client_order_id=None):
            assert order_list_id is None
            assert list_client_order_id == "list-x"
            raise BinanceAPIError(400, -2013, "Order list does not exist.")

    cfg = _config(tmp_path)
    state = _position_state()
    state["native_oco"] = {
        "symbol": "TESTUSDT", "order_list_id": None,
        "list_client_order_id": "list-x", "status": "PENDING",
        "created_at": state_mod.now_ms() - 60_000,
    }
    state["_native_stop_exit_blocked"] = True

    assert bot._reconcile_native_oco(Client(), cfg, state) is False
    # POST tidak pernah mendarat: intent dibersihkan, exit lokal hidup lagi.
    assert state["native_oco"] is None
    assert state["_native_stop_exit_blocked"] is False
    assert state["reconciliation_required"] is True


def test_fresh_unconfirmed_intent_stays_fail_closed(tmp_path) -> None:
    class Client:
        def get_order_list(self, order_list_id=None, list_client_order_id=None):
            raise BinanceAPIError(400, -2013, "Order list does not exist.")

    cfg = _config(tmp_path)
    state = _position_state()
    state["native_oco"] = {
        "symbol": "TESTUSDT", "order_list_id": None,
        "list_client_order_id": "list-x", "status": "PENDING",
        "created_at": state_mod.now_ms(),  # baru saja: bisa masih in-flight
    }

    assert bot._reconcile_native_oco(Client(), cfg, state) is False
    assert isinstance(state["native_oco"], dict)
    assert state["native_oco"]["status"] == "UNKNOWN"
    assert state["_native_stop_exit_blocked"] is True


def test_confirmed_intent_with_not_found_error_stays_fail_closed(tmp_path) -> None:
    class Client:
        def get_order_list(self, order_list_id=None, list_client_order_id=None):
            raise BinanceAPIError(400, -2013, "Order list does not exist.")

    cfg = _config(tmp_path)
    state = _position_state()
    state["native_oco"] = {
        "symbol": "TESTUSDT", "order_list_id": 7001,  # SUDAH terkonfirmasi
        "list_client_order_id": "list-x", "status": "EXECUTING",
        "created_at": state_mod.now_ms() - 60_000,
    }

    assert bot._reconcile_native_oco(Client(), cfg, state) is False
    assert isinstance(state["native_oco"], dict)
    assert state["_native_stop_exit_blocked"] is True


def test_unconfirmed_missing_native_stop_unblocks_local_exit(tmp_path) -> None:
    class Client:
        def get_order(self, symbol, order_id=None, orig_client_order_id=None):
            assert order_id is None
            assert orig_client_order_id == "sl-x"
            raise BinanceAPIError(400, -2013, "Order does not exist.")

    cfg = _config(tmp_path)
    state = _position_state()
    state["native_stop"] = {
        "symbol": "TESTUSDT", "order_id": None,
        "client_order_id": "sl-x", "status": "PENDING",
        "created_at": state_mod.now_ms() - 60_000,
    }
    state["_native_stop_exit_blocked"] = True

    assert bot._reconcile_native_stop(Client(), cfg, state) is False
    assert state["native_stop"] is None
    assert state["_native_stop_exit_blocked"] is False


def test_definitive_reject_on_native_stop_post_marks_failed(tmp_path) -> None:
    class Client(_BookTicker):
        def place_native_stop_loss(self, *args, **kwargs):
            raise BinanceAPIError(400, -2010, "Account has insufficient balance.")

    cfg = _config(tmp_path)
    cfg["USE_NATIVE_OCO"] = False
    state = _position_state()

    assert bot._arm_native_stop(Client(), cfg, _filters(), state) is False
    assert state["native_stop"]["status"] == "FAILED"
    assert state["_native_stop_exit_blocked"] is False
