from __future__ import annotations

from decimal import Decimal

from binance_client import BinanceSpotClient, SymbolFilters
import backtest as bt
from config import PUMP_CONFIG
import market_scanner as scanner
import pump_scanner_bot as bot
import state as state_mod
import strategy
import watchlist_auto


def _config(tmp_path) -> dict:
    cfg = dict(PUMP_CONFIG)
    cfg.update({
        "STATE_FILE": str(tmp_path / "state.json"),
        "CONTROL_FILE": str(tmp_path / "control.json"),
        "QUOTE_ASSET": "USDT",
        "USE_DUST_SWEEP": False,
        "COOLDOWN_MINUTES_AFTER_CLOSE": 1,
    })
    return cfg


def _filters() -> SymbolFilters:
    return SymbolFilters(step_size=Decimal("0.01"), min_qty=Decimal("0.01"),
                         min_notional=Decimal("5"), tick_size=Decimal("0.0001"))


def test_exchange_filters_keep_market_bounds_and_quote_policy() -> None:
    filters = SymbolFilters.from_symbol_data({
        "symbol": "TESTUSDT",
        "quoteOrderQtyMarketAllowed": False,
        "filters": [
            {"filterType": "LOT_SIZE", "minQty": "0.01", "maxQty": "100", "stepSize": "0.01"},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.1", "maxQty": "50", "stepSize": "0.1"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "5", "applyToMarket": False},
            {"filterType": "NOTIONAL", "minNotional": "10", "maxNotional": "100",
             "applyMinToMarket": True, "applyMaxToMarket": True},
            {"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
        ],
    })

    assert filters.min_qty == Decimal("0.1")
    assert filters.max_qty == Decimal("50")
    assert filters.min_notional == Decimal("10")
    assert filters.max_notional == Decimal("100")
    assert filters.quote_order_qty_market_allowed is False


def test_position_sizing_shared_policy_has_buffer_and_cap() -> None:
    percent = strategy.resolve_position_notional({
        "USE_RISK_PERCENT": True, "RISK_PERCENT": 50,
        "BALANCE_BUFFER_PCT": 10, "MAX_POSITION_USDT": 40,
    }, 100)
    assert percent["requested_notional"] == 45
    assert percent["notional"] == 40
    assert percent["cap_active"] is True
    assert percent["effective_pct_of_free"] == 40

    fixed = strategy.resolve_position_notional({
        "USE_RISK_PERCENT": False, "POSITION_SIZE_USDT": 75,
        "MAX_POSITION_USDT": 50,
    }, 100)
    assert fixed["mode"] == "FIXED"
    assert fixed["notional"] == 50
    assert fixed["cap_active"] is True


def test_fixed_exit_levels_enforce_be_and_trailing_invariants() -> None:
    levels = strategy.resolve_exit_levels({
        "SL_PCT": 2, "TP_PCT": 4,
        "BE_TRIGGER_PCT": 1, "BE_LOCK_PCT": 3,
        "TRAILING_START_PCT": 2, "TRAILING_STEP_PCT": 5,
    })
    assert levels["be_lock_pct"] <= levels["be_trigger_pct"] <= levels["trail_start_pct"]
    assert levels["trail_step_pct"] <= levels["sl_pct"]


def test_backtest_execution_model_is_adverse_on_both_sides() -> None:
    buy = strategy.backtest_buy_execution_price(100.0, spread_pct=0.2, slippage_pct=0.1)
    sell = strategy.backtest_sell_execution_price(100.0, spread_pct=0.2, slippage_pct=0.1)
    assert buy > 100.0
    assert sell < 100.0


def test_binance_oco_wrapper_uses_order_list_endpoint_without_network() -> None:
    client = BinanceSpotClient("", "", "https://example.invalid", allow_signed=False)
    calls = []

    def fake_request(method, path, params, signed=False, max_retries=3):
        calls.append((method, path, params, signed, max_retries))
        return {"listOrderStatus": "EXECUTING"}

    client._request = fake_request
    response = client.new_oco_sell_order(
        "TESTUSDT", 2.0, 105.0, 105.0, 96.9, 97.0,
        "pump-oc-list", "pump-oc-above", "pump-oc-below",
    )

    assert response["listOrderStatus"] == "EXECUTING"
    assert calls == [(
        "POST", "/api/v3/orderList/oco", {
            "symbol": "TESTUSDT", "side": "SELL", "quantity": "2.0",
            "listClientOrderId": "pump-oc-list",
            "aboveType": "TAKE_PROFIT_LIMIT", "aboveClientOrderId": "pump-oc-above",
            "abovePrice": "105.0", "aboveStopPrice": "105.0", "aboveTimeInForce": "GTC",
            "belowType": "STOP_LOSS_LIMIT", "belowClientOrderId": "pump-oc-below",
            "belowPrice": "96.9", "belowStopPrice": "97.0", "belowTimeInForce": "GTC",
            "newOrderRespType": "FULL",
        }, True, 1,
    )]


def test_native_stop_is_saved_before_fake_live_request(tmp_path) -> None:
    class Client:
        def __init__(self):
            self.intent_ids = []
            self.calls = []

        def get_book_ticker(self, symbol, max_retries=3):
            return {"bidPrice": "100", "askPrice": "100.1"}

        def place_native_stop_loss(self, symbol, quantity, stop_price,
                                   new_client_order_id):
            assert new_client_order_id.startswith("pump-sl-")
            self.intent_ids.append(new_client_order_id)
            self.calls.append((symbol, quantity, stop_price, new_client_order_id))
            return {"orderId": 9001, "clientOrderId": new_client_order_id, "status": "NEW"}

    cfg = _config(tmp_path)
    cfg.update({"MODE": "LIVE", "USE_STOP_LOSS": True,
                "USE_NATIVE_STOP_LOSS": True, "SL_PCT": 3.0})
    state = dict(bot.DEFAULT_STATE)
    state.update({"current_symbol": "TESTUSDT", "entry_price": 100.0,
                  "qty": 2.0, "sl_pct": 3.0, "exit_source": "FIXED"})
    client = Client()

    assert bot._arm_native_stop(client, cfg, _filters(), state) is True
    assert len(client.calls) == 1
    assert state["native_stop"]["client_order_id"] == client.intent_ids[0]
    assert state["native_stop"]["order_id"] == 9001
    assert state["native_stop"]["status"] == "NEW"
    assert state["reconciliation_required"] is False


def test_native_oco_saves_list_and_leg_ids_and_cancels_before_manual_exit(tmp_path) -> None:
    class Client:
        def __init__(self):
            self.intent = None
            self.cancel_calls = []

        def get_book_ticker(self, symbol, max_retries=3):
            return {"bidPrice": "100", "askPrice": "100.1"}

        def place_native_oco(self, symbol, quantity, above_price, above_stop_price,
                             below_price, below_stop_price, list_client_order_id,
                             above_client_order_id, below_client_order_id):
            self.intent = {
                "list": list_client_order_id,
                "above": above_client_order_id,
                "below": below_client_order_id,
            }
            return {
                "orderListId": 7001,
                "listStatusType": "EXEC_STARTED",
                "listOrderStatus": "EXECUTING",
                "orders": [
                    {"orderId": 7002, "clientOrderId": above_client_order_id},
                    {"orderId": 7003, "clientOrderId": below_client_order_id},
                ],
            }

        def get_order_list(self, order_list_id=None, list_client_order_id=None):
            assert order_list_id == 7001
            return {
                "orderListId": 7001,
                "listStatusType": "EXEC_STARTED",
                "listOrderStatus": "EXECUTING",
                "orders": [
                    {"orderId": 7002, "clientOrderId": self.intent["above"]},
                    {"orderId": 7003, "clientOrderId": self.intent["below"]},
                ],
            }

        def cancel_order_list(self, symbol, order_list_id=None, list_client_order_id=None):
            self.cancel_calls.append((symbol, order_list_id, list_client_order_id))
            return {
                "orderListId": 7001,
                "listOrderStatus": "ALL_DONE",
                "orderReports": [
                    {"status": "CANCELED", "executedQty": "0"},
                    {"status": "CANCELED", "executedQty": "0"},
                ],
            }

    cfg = _config(tmp_path)
    cfg.update({"MODE": "LIVE", "USE_STOP_LOSS": True,
                "USE_TP": True, "USE_NATIVE_OCO": True,
                "USE_NATIVE_STOP_LOSS": True, "SL_PCT": 3.0,
                "TP_PCT": 5.0, "NATIVE_OCO_LIMIT_BUFFER_PCT": 0.1})
    state = dict(bot.DEFAULT_STATE)
    state.update({"current_symbol": "TESTUSDT", "entry_price": 100.0,
                  "qty": 2.0, "sl_pct": 3.0, "tp_pct": 5.0,
                  "exit_source": "FIXED"})
    client = Client()

    assert bot._arm_native_oco(client, cfg, _filters(), state) is True
    assert state["native_oco"]["order_list_id"] == 7001
    assert state["native_oco"]["above"]["order_id"] == 7002
    assert state["native_oco"]["below"]["order_id"] == 7003
    assert state["native_oco"]["status"] == "EXECUTING"
    assert bot._cancel_native_oco_before_exit(client, cfg, state) is True
    assert client.cancel_calls == [("TESTUSDT", 7001, None)]
    assert state["native_oco"] is None


def test_native_oco_filled_reconciles_without_market_sell(tmp_path) -> None:
    class Client:
        def __init__(self):
            self.sell_calls = 0

        def get_order_list(self, order_list_id=None, list_client_order_id=None):
            return {
                "orderListId": 7001,
                "listOrderStatus": "ALL_DONE",
                "orderReports": [
                    {"status": "FILLED", "executedQty": "2", "clientOrderId": "above"},
                    {"status": "CANCELED", "executedQty": "0", "clientOrderId": "below"},
                ],
            }

        def get_account(self):
            return {"balances": [
                {"asset": "TEST", "free": "0", "locked": "0"},
                {"asset": "USDT", "free": "100", "locked": "0"},
            ]}

        def new_market_order(self, *args, **kwargs):
            self.sell_calls += 1
            raise AssertionError("OCO FILLED tidak boleh diikuti SELL market kedua")

    cfg = _config(tmp_path)
    cfg.update({"MODE": "LIVE", "USE_STOP_LOSS": True,
                "USE_TP": True, "USE_NATIVE_OCO": True})
    state = dict(bot.DEFAULT_STATE)
    state.update({"current_symbol": "TESTUSDT", "entry_price": 100.0,
                  "qty": 2.0, "native_oco": {
                      "symbol": "TESTUSDT", "order_list_id": 7001,
                      "list_client_order_id": "list", "status": "EXECUTING",
                      "above": {"client_order_id": "above", "order_id": 7002},
                      "below": {"client_order_id": "below", "order_id": 7003},
                  }})
    client = Client()

    assert bot._reconcile_native_oco(client, cfg, state) is False
    assert client.sell_calls == 0
    assert state["current_symbol"] is None


def test_partial_sell_keeps_position_and_remaining_qty(tmp_path) -> None:
    class Client:
        def get_account(self):
            return {"balances": [
                {"asset": "TEST", "free": "10", "locked": "0"},
                {"asset": "USDT", "free": "100", "locked": "0"},
            ]}

        def new_market_order(self, symbol, side, quantity=None, quote_order_qty=None,
                             new_client_order_id=None):
            assert symbol == "TESTUSDT" and side == "SELL"
            return {"status": "EXPIRED", "executedQty": "4", "cummulativeQuoteQty": "400"}

    cfg = _config(tmp_path)
    state = dict(bot.DEFAULT_STATE)
    state.update({"current_symbol": "TESTUSDT", "entry_price": 90, "qty": 10})
    bot.close_position(Client(), cfg, {"TESTUSDT": _filters()}, state, "TEST_PARTIAL")

    assert state["current_symbol"] == "TESTUSDT"
    assert state["qty"] == 6
    assert state["cooldown_until"] == 0
    assert state["pending_order"] is None


def test_locked_balance_does_not_reset_position(tmp_path) -> None:
    class Client:
        def __init__(self):
            self.orders = []

        def get_account(self):
            return {"balances": [
                {"asset": "TEST", "free": "0", "locked": "5"},
                {"asset": "USDT", "free": "100", "locked": "0"},
            ]}

        def new_market_order(self, *args, **kwargs):
            self.orders.append((args, kwargs))
            return {"status": "FILLED", "executedQty": "0", "cummulativeQuoteQty": "0"}

    cfg = _config(tmp_path)
    state = dict(bot.DEFAULT_STATE)
    state.update({"current_symbol": "TESTUSDT", "entry_price": 90, "qty": 10})
    client = Client()
    bot.close_position(client, cfg, {"TESTUSDT": _filters()}, state, "LOCKED_TEST")

    assert client.orders == []
    assert state["current_symbol"] == "TESTUSDT"
    assert state["qty"] == 10
    assert state["reconciliation_required"] is True
    assert state["reconciliation_assets"] == ["TEST"]


def test_sell_filter_uses_fresh_executable_bid(tmp_path) -> None:
    class Client:
        def __init__(self):
            self.account_calls = 0
            self.book_calls = []

        def get_book_ticker(self, symbol, max_retries=3):
            self.book_calls.append((symbol, max_retries))
            return {"bidPrice": "90", "askPrice": "91"}

        def get_account(self):
            self.account_calls += 1
            free = "10" if self.account_calls == 1 else "0"
            return {"balances": [
                {"asset": "TEST", "free": free, "locked": "0"},
                {"asset": "USDT", "free": "100", "locked": "0"},
            ]}

        def new_market_order(self, symbol, side, quantity=None, quote_order_qty=None,
                             new_client_order_id=None):
            return {"status": "FILLED", "executedQty": "10",
                    "cummulativeQuoteQty": "900"}

    cfg = _config(tmp_path)
    state = dict(bot.DEFAULT_STATE)
    state.update({"current_symbol": "TESTUSDT", "entry_price": 80, "qty": 10})
    client = Client()
    bot.close_position(client, cfg, {"TESTUSDT": _filters()}, state, "BID_TEST")

    assert client.book_calls == [("TESTUSDT", 1)]
    assert state["current_symbol"] is None


def test_nonterminal_sell_keeps_intent_and_blocks_duplicate(tmp_path) -> None:
    class Client:
        def __init__(self):
            self.orders = []

        def get_account(self):
            return {"balances": [
                {"asset": "TEST", "free": "10", "locked": "0"},
                {"asset": "USDT", "free": "100", "locked": "0"},
            ]}

        def new_market_order(self, symbol, side, quantity=None, quote_order_qty=None,
                             new_client_order_id=None):
            self.orders.append(new_client_order_id)
            return {"status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0"}

    cfg = _config(tmp_path)
    state = dict(bot.DEFAULT_STATE)
    state.update({"current_symbol": "TESTUSDT", "entry_price": 90, "qty": 10})
    client = Client()
    bot.close_position(client, cfg, {"TESTUSDT": _filters()}, state, "NONTERMINAL_TEST")

    assert len(client.orders) == 1
    assert state["pending_order"]["side"] == "SELL"
    assert state["pending_order"]["last_status"] == "NEW"
    assert state["reconciliation_required"] is True
    assert state["qty"] == 10


def test_buy_uses_quote_order_qty_for_nominal_limit(tmp_path) -> None:
    class Client:
        def __init__(self):
            self.calls = []
            self.account_calls = 0

        def get_account(self):
            self.account_calls += 1
            base_free = "0" if self.account_calls == 1 else "0.25"
            return {"balances": [
                {"asset": "TEST", "free": base_free, "locked": "0"},
                {"asset": "USDT", "free": "1000", "locked": "0"},
            ]}

        def new_market_order(self, symbol, side, quantity=None, quote_order_qty=None,
                             new_client_order_id=None):
            self.calls.append({"quantity": quantity, "quote_order_qty": quote_order_qty})
            return {"status": "FILLED", "executedQty": "0.25",
                    "cummulativeQuoteQty": "25"}

    cfg = _config(tmp_path)
    cfg.update({"USE_RISK_PERCENT": False, "POSITION_SIZE_USDT": 25.0,
                "MAX_POSITION_USDT": 100.0})
    state = dict(bot.DEFAULT_STATE)
    candidate = scanner.Candidate(
        symbol="TESTUSDT", base_asset="TEST", price_change_pct=20.0,
        quote_volume=1_000_000, last_price=100.0, confirmed=True,
        confirm_reason="test",
    )
    filters = {"TESTUSDT": SymbolFilters(
        step_size=Decimal("0.01"), min_qty=Decimal("0.01"),
        min_notional=Decimal("5"), tick_size=Decimal("0.0001"),
    )}
    client = Client()
    bot.open_position(client, cfg, filters, state, candidate, reference_price=100.0)

    assert client.calls == [{"quantity": None, "quote_order_qty": 25.0}]
    assert state["current_symbol"] == "TESTUSDT"
    assert state["entry_price"] == 100.0
    assert state["qty"] == 0.25


def test_startup_pending_buy_is_restored_and_orphan_blocks_entry(tmp_path) -> None:
    class RecoverClient:
        def get_account(self):
            return {"balances": [
                {"asset": "TEST", "free": "1.9", "locked": "0"},
                {"asset": "USDT", "free": "90", "locked": "0"},
            ]}

        def get_order(self, symbol, order_id=None, orig_client_order_id=None):
            assert symbol == "TESTUSDT" and orig_client_order_id == "pump-buy-test"
            return {"symbol": symbol, "side": "BUY", "status": "FILLED",
                    "executedQty": "2", "cummulativeQuoteQty": "10", "transactTime": 123}

    cfg = _config(tmp_path)
    state = dict(bot.DEFAULT_STATE)
    state["pending_order"] = {
        "side": "BUY", "symbol": "TESTUSDT", "qty": 2,
        "client_order_id": "pump-buy-test",
        "levels": {"sl_pct": 2, "tp_pct": 4},
    }
    bot.reconcile_state_with_exchange(RecoverClient(), cfg, state)
    assert state["current_symbol"] == "TESTUSDT"
    assert state["qty"] == 1.9  # fee base asset tidak boleh membuat qty state terlalu besar
    assert state["pending_order"] is None

    class OrphanClient:
        def get_account(self):
            return {"balances": [
                {"asset": "PEPE", "free": "123", "locked": "0"},
                {"asset": "USDT", "free": "100", "locked": "0"},
            ]}

    orphan = dict(bot.DEFAULT_STATE)
    bot.reconcile_state_with_exchange(OrphanClient(), cfg, orphan)
    assert orphan["reconciliation_required"] is True
    assert orphan["reconciliation_assets"] == ["PEPE"]


def test_startup_partial_buy_stays_pending_until_terminal(tmp_path) -> None:
    class Client:
        def get_account(self):
            return {"balances": [
                {"asset": "TEST", "free": "1", "locked": "0"},
                {"asset": "USDT", "free": "90", "locked": "0"},
            ]}

        def get_order(self, symbol, order_id=None, orig_client_order_id=None):
            return {"symbol": symbol, "side": "BUY", "status": "PARTIALLY_FILLED",
                    "executedQty": "1", "cummulativeQuoteQty": "10"}

    cfg = _config(tmp_path)
    state = dict(bot.DEFAULT_STATE)
    state["pending_order"] = {
        "side": "BUY", "symbol": "TESTUSDT", "qty": 2,
        "client_order_id": "pump-buy-partial", "levels": {},
    }
    bot.reconcile_state_with_exchange(Client(), cfg, state)

    assert state["pending_order"]["client_order_id"] == "pump-buy-partial"
    assert state["pending_order"]["last_status"] == "PARTIALLY_FILLED"
    assert state["reconciliation_required"] is True
    assert state["current_symbol"] is None


def test_backtest_exit_mode_overrides_match_atr_and_fixed_live_paths() -> None:
    atr_cfg = bt.apply_overrides(PUMP_CONFIG, {
        "USE_ATR_EXIT": "true", "ATR_PERIOD": "20",
        "ATR_MULT_SL": "1.7", "ATR_MULT_TP": "3.4",
        "ATR_MULT_BE_TRIGGER": "1.1", "ATR_MULT_BE_LOCK": "0.2",
        "ATR_MULT_TRAIL_START": "1.6", "ATR_MULT_TRAIL": "0.8",
    })
    bt.validate_params(atr_cfg)
    atr_levels = strategy.resolve_exit_levels(dict(atr_cfg, _atr_value=2.0))
    assert atr_cfg["USE_ATR_EXIT"] is True
    assert atr_levels["source"] == "ATR"
    assert atr_levels["sl_pct"] == 3.4
    assert atr_levels["tp_pct"] == 6.8

    fixed_cfg = bt.apply_overrides(PUMP_CONFIG, {
        "USE_ATR_EXIT": "false", "SL_PCT": "2.0", "TP_PCT": "4.0",
        "BE_TRIGGER_PCT": "1.0", "BE_LOCK_PCT": "0.2",
        "TRAILING_START_PCT": "1.5", "TRAILING_STEP_PCT": "0.6",
    })
    bt.validate_params(fixed_cfg)
    fixed_levels = strategy.resolve_exit_levels(fixed_cfg)
    assert fixed_cfg["USE_ATR_EXIT"] is False
    assert fixed_levels["source"] == "FIXED"
    assert fixed_levels["sl_pct"] == 2.0
    assert fixed_levels["tp_pct"] == 4.0


def test_watchlist_uses_configured_interval_for_24h_window(monkeypatch) -> None:
    # 170 candle 15 menit = 1,77 hari. Kalau watchlist masih mengasumsikan
    # 5 menit, ia menganggapnya hanya 0,59 hari dan bahkan menolak datanya.
    from types import SimpleNamespace
    from strategy import Kline

    monkeypatch.setattr(scanner, "detect_pullback_retest", lambda *_args, **_kwargs: SimpleNamespace(ok=False))
    bar = 15 * 60_000
    kl = [Kline(open_time=i * bar, open=100, high=101, low=99, close=100,
                close_time=(i + 1) * bar - 1, volume=10, quote_volume=1_000_000)
          for i in range(170)]
    cfg = dict(PUMP_CONFIG)
    cfg.update({"CONFIRM_INTERVAL": "15m", "MIN_QUOTE_VOLUME_USDT_24H": 0})
    row = watchlist_auto.score_symbol("TESTUSDT", kl, {"spread_pct": 0.1}, cfg)
    assert row is not None
    assert row["days"] == 1.8


def test_shared_candidate_policy_spread_and_closed_watchlist_candles() -> None:
    cfg = {"QUOTE_ASSET": "USDT", "EXTRA_EXCLUDE_SYMBOLS": ["NOPEUSDT"]}
    assert scanner.is_structurally_allowed_symbol("BTCUSDT", cfg)
    assert not scanner.is_structurally_allowed_symbol("USDCUSDT", cfg)
    assert not scanner.is_structurally_allowed_symbol("BTCUPUSDT", cfg)
    assert not scanner.is_structurally_allowed_symbol("NOPEUSDT", cfg)
    assert scanner.spread_pct_from_book(99, 101) == 2.0

    now = state_mod.now_ms()
    raw = [
        [0, "1", "2", "0.5", "1.5", "10", now - 1, "15"],
        [1, "1", "2", "0.5", "1.5", "10", now + 1, "15"],
    ]
    closed = watchlist_auto.to_klines(raw, now_ms=now)
    assert len(closed) == 1
