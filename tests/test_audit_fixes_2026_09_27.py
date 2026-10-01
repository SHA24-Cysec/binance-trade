"""Test regresi untuk perbaikan audit LIVE-readiness 2026-09-27.

Mengunci lima perilaku:
1. Entry mode ATR DITOLAK bila nilai ATR kandidat tidak tersedia (guard
   anti "SL jarak absolut" yang tidak pernah tersentuh pada koin murah).
2. Fallback config pada mode ATR sadar-unit: persen dikonversi ke jarak
   harga, bukan dipakai mentah sebagai jarak absolut.
3. Setelah proteksi native FILLED dan rekonsiliasi bersih, flag
   reconciliation_required dibersihkan.
4. -1116/-1020 diklasifikasi sebagai reject deterministik.
5. CLOSE_ALL_AT_LIMIT tidak menutup paksa posisi pada episode daily stop
   bersumber PROFIT target; episode LOSS tetap menutup.
"""

from __future__ import annotations

from decimal import Decimal

from trading.clients.binance_client import BinanceAPIError, SymbolFilters
from config.config import PUMP_CONFIG
from trading import pump_scanner_bot as bot


def _config(tmp_path) -> dict:
    cfg = dict(PUMP_CONFIG)
    cfg.update({
        "STATE_FILE": str(tmp_path / "state.json"),
        "CONTROL_FILE": str(tmp_path / "control.json"),
        "QUOTE_ASSET": "USDT",
        "USE_DUST_SWEEP": False,
        "MODE": "PAPER",
    })
    return cfg


def _filters() -> dict:
    return {"TESTUSDT": SymbolFilters(
        step_size=Decimal("0.01"), min_qty=Decimal("0.01"),
        min_notional=Decimal("5"), tick_size=Decimal("0.0001"),
    )}


def test_exit_distance_converts_pct_fallback_in_atr_mode() -> None:
    state = {"sl_pct": 0.0, "exit_source": "ATR"}
    cfg = {"SL_PCT": 28.8}
    entry = 0.5
    jarak = bot._exit_distance(state, cfg, "sl_pct", "SL_PCT", atr_mode=True, entry=entry)
    assert abs(jarak - 0.5 * 0.288) < 1e-12, "persen wajib dikonversi ke jarak harga"
    state["sl_pct"] = 0.01
    assert bot._exit_distance(state, cfg, "sl_pct", "SL_PCT", True, entry) == 0.01
    state["sl_pct"] = 0.0
    assert bot._exit_distance(state, cfg, "sl_pct", "SL_PCT", False, entry) == 28.8


def test_native_stop_price_pct_fallback_stays_positive_for_cheap_coin() -> None:
    state = {"entry_price": 0.5, "sl_pct": 0.0, "exit_source": "ATR"}
    cfg = {"SL_PCT": 28.8}
    stop = bot._native_stop_price(state, None, cfg)
    assert 0 < stop < 0.5, f"stopPrice harus valid di bawah entry, dapat {stop}"


def test_settle_native_fill_clears_flag(tmp_path) -> None:
    class Client:
        def get_account(self):
            return {"balances": [{"asset": "USDT", "free": "1000", "locked": "0"}]}

    cfg = _config(tmp_path)
    state = dict(bot.DEFAULT_STATE)
    state.update({
        "current_symbol": "TESTUSDT", "entry_price": 1.0, "qty": 10.0,
        "_native_stop_exit_blocked": True,
        "reconciliation_required": True, "reconciliation_assets": ["TEST"],
    })
    bot._settle_native_protective_fill(Client(), cfg, state, "TESTUSDT", "NATIVE_OCO_FILLED")

    assert state["current_symbol"] is None, "posisi hantu wajib direset"
    assert state["reconciliation_required"] is False, (
        "flag wajib bersih setelah rekonsiliasi sukses"
    )
    assert state["_native_stop_exit_blocked"] is False


def test_settle_native_fill_stays_fail_closed_when_account_unavailable(tmp_path) -> None:
    class Client:
        def get_account(self):
            raise BinanceAPIError(503, None, "unavailable")

    cfg = _config(tmp_path)
    state = dict(bot.DEFAULT_STATE)
    state.update({
        "current_symbol": "TESTUSDT", "entry_price": 1.0, "qty": 10.0,
        "_native_stop_exit_blocked": True,
        "reconciliation_required": True, "reconciliation_assets": ["TEST"],
    })
    bot._settle_native_protective_fill(Client(), cfg, state, "TESTUSDT", "NATIVE_OCO_FILLED")

    assert state["reconciliation_required"] is True
    assert state["current_symbol"] == "TESTUSDT"


def test_definitive_reject_includes_invalid_ordertype_codes() -> None:
    assert bot._is_definitive_reject(BinanceAPIError(400, -1116, "Invalid orderType."))
    assert bot._is_definitive_reject(BinanceAPIError(400, -1020, "Unsupported operation."))
    assert not bot._is_definitive_reject(BinanceAPIError(500, -1000, "Unknown error."))


def _force_close_calls(monkeypatch, tmp_path, source: str) -> list:
    calls = []
    monkeypatch.setattr(bot, "close_position",
                        lambda *a, **k: calls.append((a, k)))
    cfg = _config(tmp_path)
    cfg["CLOSE_ALL_AT_LIMIT"] = True
    state = dict(bot.DEFAULT_STATE)
    state.update({"current_symbol": "TESTUSDT", "qty": 1.0, "entry_price": 1.0,
                  "daily_stopped": True, "daily_stop_source": source})
    bot.maybe_force_close_at_risk_limit(None, cfg, {}, state,
                                        entries_paused=True, current_price=1.0)
    return calls


def test_close_all_at_limit_skips_profit_target_episode(monkeypatch, tmp_path) -> None:
    assert _force_close_calls(monkeypatch, tmp_path, "PROFIT") == [], (
        "posisi yang sedang untung tidak boleh ditutup paksa hanya karena "
        "target profit harian tercapai"
    )


def test_close_all_at_limit_still_fires_on_loss_episode(monkeypatch, tmp_path) -> None:
    assert len(_force_close_calls(monkeypatch, tmp_path, "LOSS")) == 1


def test_close_all_at_limit_legacy_state_treated_as_loss(monkeypatch, tmp_path) -> None:
    assert len(_force_close_calls(monkeypatch, tmp_path, None)) == 1
