from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from backtesting import parity
from backtesting.exit_selftest import _candle, _posisi, config_dasar
from strategy import entry_rule
from trading import pump_scanner_bot as bot


class TakeProfitTests(unittest.TestCase):
    def setUp(self):
        self.config = config_dasar()
        self.config.update({"USE_NATIVE_STOP_LOSS": False, "USE_NATIVE_OCO": False})
        self.state = {
            "current_symbol": "BTCUSDT",
            "qty": 1.0,
            "entry_price": 100.0,
            "sl_price": 98.0,
            "initial_sl_price": 98.0,
            "tp_price": 108.0,
            "entry_time": 0,
        }
        self.client = Mock()

    def test_invalid_levels(self):
        for value in (None, "bad", 0, -1, 100, 99, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertFalse(entry_rule.take_profit_due(100, value, 110))
        for value in (None, "bad", 0, float("nan"), float("inf")):
            self.assertFalse(entry_rule.take_profit_due(100, 108, value))

    def test_threshold(self):
        self.assertFalse(entry_rule.take_profit_due(100, 108, 107.999))
        self.assertTrue(entry_rule.take_profit_due(100, 108, 108))
        self.assertTrue(entry_rule.take_profit_due(100, 108, 110))

    def test_live_exact_and_overshoot(self):
        for price in (108.0, 110.0):
            with patch.object(bot, "close_position") as close:
                bot.manage_exit(self.client, self.config, {}, dict(self.state), price)
                close.assert_called_once_with(
                    self.client, self.config, {}, self.state,
                    "TAKE_PROFIT", reference_price=price,
                )

    def test_live_below_target(self):
        with patch.object(bot, "close_position") as close, patch.object(
            bot, "_maybe_amend_native_protection"
        ):
            bot.manage_exit(self.client, self.config, {}, dict(self.state), 100)
            close.assert_not_called()

    def test_backtest_target_and_gap(self):
        for opening, high, expected in ((107, 108, 108), (110, 112, 110)):
            result = parity.evaluate_candle_exit(
                _posisi({}), _candle(high, opening, high, open_price=opening), self.config
            )
            self.assertEqual(result, ("TAKE_PROFIT", expected))

    def test_backtest_ambiguous_bar_stop_first(self):
        result = parity.evaluate_candle_exit(
            _posisi({}), _candle(110, 97, 109, open_price=100), self.config
        )
        self.assertEqual(result, ("STOP_LOSS", 98.0))

    def test_live_does_not_create_protection_at_target(self):
        self.config.update({"MODE": "LIVE", "USE_NATIVE_STOP_LOSS": True})
        with patch.object(bot, "_ensure_native_protection") as ensure, patch.object(
            bot, "close_position"
        ) as close:
            bot.manage_exit(self.client, self.config, {}, self.state, 108)
            ensure.assert_not_called()
            close.assert_called_once()

    def test_unknown_native_status_blocks_duplicate_exit(self):
        self.config.update({"MODE": "LIVE", "USE_NATIVE_STOP_LOSS": True})
        self.state.update({"native_stop": {}, "_native_stop_exit_blocked": True})
        with patch.object(bot, "_reconcile_native_stop", return_value=False), patch.object(
            bot, "close_position"
        ) as close:
            bot.manage_exit(self.client, self.config, {}, self.state, 108)
            close.assert_not_called()

    def test_native_fill_does_not_send_second_sell(self):
        self.config.update({"MODE": "LIVE", "USE_NATIVE_OCO": True})
        self.state["native_oco"] = {}
        def settle(*args):
            self.state["current_symbol"] = None
            return False
        with patch.object(bot, "_reconcile_native_oco", side_effect=settle), patch.object(
            bot, "close_position"
        ) as close:
            bot.manage_exit(self.client, self.config, {}, self.state, 108)
            close.assert_not_called()

    def test_active_native_stop_routes_to_existing_close(self):
        self.config.update({"MODE": "LIVE", "USE_NATIVE_STOP_LOSS": True})
        self.state["native_stop"] = {"quantity": 1}
        with patch.object(bot, "_reconcile_native_stop", return_value=True), patch.object(
            bot, "close_position"
        ) as close:
            bot.manage_exit(self.client, self.config, {}, self.state, 108)
            close.assert_called_once()

    def test_pending_intent_prevents_new_sell(self):
        self.state["pending_order"] = {"client_order_id": "existing-order"}
        with patch.object(bot.state_mod, "save_state"):
            bot.close_position(self.client, self.config, {}, self.state, "TAKE_PROFIT", 108)
        self.client.new_market_order.assert_not_called()
        self.assertEqual(self.state["reconciliation_reason"], "SELL_PENDING_ORDER_UNRESOLVED")

    def test_cancel_failure_prevents_market_sell(self):
        filters = Mock(step_size=0.001)
        self.state["native_stop"] = {"quantity": 1}
        self.client.get_account.return_value = {
            "balances": [{"asset": "BTC", "free": "0", "locked": "1"}]
        }
        with patch.object(bot, "_executable_bid", return_value=108), patch.object(
            bot, "_cancel_native_stop_before_exit", return_value=False
        ) as cancel:
            bot.close_position(
                self.client, self.config, {"BTCUSDT": filters}, self.state,
                "TAKE_PROFIT", 108,
            )
            cancel.assert_called_once()
        self.client.new_market_order.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
