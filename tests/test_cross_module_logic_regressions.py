from __future__ import annotations

from decimal import Decimal

from binance_client import SymbolFilters
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
