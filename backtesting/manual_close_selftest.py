from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from infrastructure.storage import state as storage
from infrastructure.storage.position_control import position_id, new_close_command, close_command_matches
from infrastructure.process.runtime_control import BotProcessManager, BotControlError
from trading import pump_scanner_bot as bot
from web import dashboard as dash


class ManualCloseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.cfg = {"STATE_FILE": str(folder / "state.json"), "CONTROL_FILE": str(folder / "control.json"), "MODE": "PAPER"}
        self.now = storage.now_ms()
        self.state = {"current_symbol": "BTCUSDT", "qty": 1.0, "entry_price": 100.0, "entry_time": self.now - 60_000, "trade_accounting": {"trade_id": "trade-A"}}
        self.command = new_close_command(self.state, self.now, "a" * 32)
        storage.save_state(self.cfg["STATE_FILE"], self.state)
        dash.app.config.update(TESTING=True)
        dash._last_manual_close_request["ts"] = 0.0
        dash._write_attempts.clear()
        dash._control_operation = None
        dash._confirmations.clear()
        self.client = dash.app.test_client()
        self.headers = {"Origin": "http://localhost", "X-Admin-Token": dash._ADMIN_TOKEN}

    def post(self, payload):
        with patch.object(dash, "load_state", return_value=deepcopy(self.state)), patch.object(dash, "CONTROL_FILE", self.cfg["CONTROL_FILE"]), patch.object(dash, "_bot_looks_alive", return_value=True):
            return self.client.post("/api/manual/close", json=payload, headers=self.headers)

    def payload(self):
        return {key: self.command[key] for key in ("symbol", "position_id", "request_id")}

    def consume(self, state=None):
        with patch.object(bot, "close_position") as close:
            bot.check_manual_control(Mock(), self.cfg, {}, self.state if state is None else state)
            return close.call_count

    def test_api_to_bot_same_position(self):
        self.assertEqual(self.post(self.payload()).status_code, 200)
        self.assertEqual(self.consume(), 1)

    def test_api_to_bot_position_changes_after_acceptance(self):
        self.assertEqual(self.post(self.payload()).status_code, 200)
        changed = {**self.state, "entry_time": self.now + 1, "trade_accounting": {"trade_id": "new-trade"}}
        self.assertEqual(self.consume(changed), 0)

    def test_concurrent_submissions_do_not_overwrite(self):
        def submit(identifier):
            command = new_close_command(self.state, self.now, identifier * 32)
            try:
                storage.submit_control(self.cfg["CONTROL_FILE"], command)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, ("a", "b")))
        self.assertEqual(sum(results), 1)
        pending = storage.load_control(self.cfg["CONTROL_FILE"])
        self.assertIn(pending["request_id"], ("a" * 32, "b" * 32))

    def test_identity_stable_after_partial_fill(self):
        updated = {**self.state, "qty": 0.4, "sl_price": 102, "be_active": True}
        self.assertEqual(position_id(updated), position_id(self.state))

    def test_identity_changes_for_new_trade(self):
        updated = {**self.state, "trade_accounting": {"trade_id": "trade-B"}}
        self.assertNotEqual(position_id(updated), position_id(self.state))

    def test_legacy_identity_uses_entry_time(self):
        old = {**self.state, "trade_accounting": None}
        self.assertNotEqual(position_id(old), position_id({**old, "entry_time": self.now}))

    def test_identity_rejects_invalid_state(self):
        for values in ({"entry_time": 0}, {"qty": float("nan")}, {"entry_price": float("inf")}, {"state_integrity_error": "corrupt"}, {"entry_time": True}):
            self.assertIsNone(position_id({**self.state, **values}))

    def test_api_accepts_bound_command(self):
        response = self.post(self.payload())
        self.assertEqual(response.status_code, 200)
        pending = storage.load_control(self.cfg["CONTROL_FILE"])
        self.assertEqual(pending["position_id"], position_id(self.state))
        self.assertTrue(close_command_matches(pending, self.state, storage.now_ms()))

    def test_api_rejects_replaced_position_same_symbol(self):
        old = self.payload()
        self.state.update(entry_time=self.now, trade_accounting={"trade_id": "trade-B"})
        self.assertEqual(self.post(old).status_code, 409)
        self.assertFalse(storage.load_control(self.cfg["CONTROL_FILE"]))

    def test_api_rejects_replaced_symbol(self):
        payload = {**self.payload(), "symbol": "ETHUSDT"}
        self.assertEqual(self.post(payload).status_code, 409)

    def test_api_rejects_legacy_unbound_payload(self):
        self.assertEqual(self.post({"symbol": "BTCUSDT"}).status_code, 400)

    def test_api_rejects_nonobject(self):
        for value in ([1], None, "bad"):
            self.assertEqual(self.post(value).status_code, 400)

    def test_api_idempotent_while_pending(self):
        self.assertEqual(self.post(self.payload()).status_code, 200)
        original = storage.load_control(self.cfg["CONTROL_FILE"])
        self.assertEqual(self.post(self.payload()).status_code, 200)
        self.assertEqual(storage.load_control(self.cfg["CONTROL_FILE"]), original)

    def test_api_rejects_consumed_id(self):
        self.state["consumed_close_requests"] = {"a" * 32: self.now}
        self.assertEqual(self.post(self.payload()).status_code, 409)

    def test_api_does_not_overwrite_other_pending(self):
        other = new_close_command(self.state, self.now, "b" * 32)
        storage.submit_control(self.cfg["CONTROL_FILE"], other)
        self.assertEqual(self.post(self.payload()).status_code, 409)
        self.assertEqual(storage.load_control(self.cfg["CONTROL_FILE"]), other)

    def test_bot_accepts_current_position_once(self):
        storage.submit_control(self.cfg["CONTROL_FILE"], self.command)
        self.assertEqual(self.consume(), 1)
        self.assertFalse(storage.load_control(self.cfg["CONTROL_FILE"]))
        self.assertIn("a" * 32, storage.load_state(self.cfg["STATE_FILE"])["consumed_close_requests"])

    def test_bot_rejects_replay_after_state_reload(self):
        storage.submit_control(self.cfg["CONTROL_FILE"], self.command)
        self.assertEqual(self.consume(), 1)
        reloaded = storage.load_state(self.cfg["STATE_FILE"])
        storage.submit_control(self.cfg["CONTROL_FILE"], self.command)
        self.assertEqual(self.consume(reloaded), 0)

    def test_bot_rejects_new_same_symbol_position(self):
        storage.submit_control(self.cfg["CONTROL_FILE"], self.command)
        replacement = {**self.state, "entry_time": self.now + 1, "trade_accounting": {"trade_id": "trade-B"}}
        self.assertEqual(self.consume(replacement), 0)

    def test_bot_rejects_unbound_old_command(self):
        storage.save_control(self.cfg["CONTROL_FILE"], {"action": "CLOSE_POSITION", "symbol": "BTCUSDT", "requested_at": self.now})
        self.assertEqual(self.consume(), 0)

    def test_bot_rejects_expired_and_future_commands(self):
        for timestamp in (self.now - 120_001, self.now + 60_000):
            command = {**self.command, "requested_at": timestamp}
            self.assertFalse(close_command_matches(command, self.state, self.now))

    def test_bot_rejects_malformed_timestamp(self):
        for value in (True, "bad", float("inf"), None):
            self.assertFalse(close_command_matches({**self.command, "requested_at": value}, self.state, self.now))

    def test_atomic_consume_preserves_next_request(self):
        storage.submit_control(self.cfg["CONTROL_FILE"], self.command)
        self.assertEqual(storage.consume_control(self.cfg["CONTROL_FILE"]), self.command)
        next_command = new_close_command(self.state, self.now, "b" * 32)
        storage.submit_control(self.cfg["CONTROL_FILE"], next_command)
        self.assertEqual(storage.load_control(self.cfg["CONTROL_FILE"]), next_command)

    def test_save_receipt_failure_does_not_sell(self):
        storage.submit_control(self.cfg["CONTROL_FILE"], self.command)
        with patch.object(bot.state_mod, "save_state", side_effect=OSError("disk error")), patch.object(bot, "close_position") as close:
            with self.assertRaises(OSError):
                bot.check_manual_control(Mock(), self.cfg, {}, self.state)
            close.assert_not_called()

    def test_security_gate_still_requires_token(self):
        response = self.client.post("/api/manual/close", json=self.payload(), headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 403)

    def test_sell_first_rejects_new_position_before_submit(self):
        manager = object.__new__(BotProcessManager)
        with self.assertRaises(BotControlError):
            manager._sell_first(self.cfg, 1, "wrong-position")
        self.assertFalse(storage.load_control(self.cfg["CONTROL_FILE"]))

    def test_sell_first_sends_bound_command(self):
        manager = object.__new__(BotProcessManager)
        with patch.object(manager, "_position", return_value={"has_position": False, "state_valid": True}):
            manager._sell_first(self.cfg, 1, position_id(self.state))
        pending = storage.load_control(self.cfg["CONTROL_FILE"])
        self.assertEqual(pending["position_id"], position_id(self.state))
        self.assertTrue(close_command_matches(pending, self.state, storage.now_ms()))

    def test_sell_first_aborts_when_position_changes_while_waiting(self):
        manager = object.__new__(BotProcessManager)
        with patch.object(manager, "_position", return_value={"has_position": True, "state_valid": True, "position_id": "new-position"}):
            with self.assertRaises(BotControlError):
                manager._sell_first(self.cfg, 1, position_id(self.state))

    def test_sell_first_aborts_when_state_becomes_unverified(self):
        manager = object.__new__(BotProcessManager)
        with patch.object(manager, "_position", return_value={"has_position": False, "state_valid": False}):
            with self.assertRaises(BotControlError):
                manager._sell_first(self.cfg, 1, position_id(self.state))

    def test_prepare_stop_requires_matching_position(self):
        position = {"has_position": True, "symbol": "BTCUSDT", "qty": 1, "position_id": position_id(self.state)}
        with patch.object(dash._process_manager, "status", return_value={"status": "RUNNING", "pid": 123}), patch.object(dash._process_manager, "position", return_value=position):
            response = self.client.post("/api/control/prepare", json={"action": "STOP", "position_policy": "SELL_FIRST", "position_id": "old-position"}, headers=self.headers)
        self.assertEqual(response.status_code, 409)

    def test_execute_stop_rechecks_position(self):
        token = dash._make_confirmation("process", {"action": "STOP", "position_policy": "SELL_FIRST", "expected_position_id": "old-position", "mode": dash.get_mode(dash.PUMP_CONFIG), "pid": 123})
        with patch.object(dash._process_manager, "status", return_value={"status": "RUNNING", "pid": 123}), patch.object(dash._process_manager, "position", return_value={"position_id": "new-position"}):
            response = self.client.post("/api/control/execute", json={"confirmation_id": token}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(dash._control_operation)


if __name__ == "__main__":
    unittest.main(verbosity=2)
