"""Test regresi untuk perbaikan audit LIVE-readiness 2026-09-27.

Mengunci lima perilaku:
1. Entry mode ATR DITOLAK bila nilai ATR kandidat tidak tersedia (guard
   anti "SL jarak absolut" yang tidak pernah tersentuh pada koin murah).
2. Fallback config pada mode ATR sadar-unit: persen dikonversi ke jarak
   harga, bukan dipakai mentah sebagai jarak absolut.
3. Setelah proteksi native FILLED dan rekonsiliasi bersih, flag
   reconciliation_required DIBERSIHKAN dan cooldown diset (bot tidak lagi
   berhenti entry permanen setelah setiap exit exchange-side).
4. -1116/-1020 diklasifikasi sebagai reject deterministik.
5. CLOSE_ALL_AT_LIMIT tidak menutup paksa posisi pada episode daily stop
   bersumber PROFIT target; episode LOSS tetap menutup.
"""

from __future__ import annotations

from decimal import Decimal

from binance_client import BinanceAPIError, SymbolFilters
from config import PUMP_CONFIG
import market_scanner as scanner
import pump_scanner_bot as bot


def _config(tmp_path) -> dict:
    cfg = dict(PUMP_CONFIG)
    cfg.update({
        "STATE_FILE": str(tmp_path / "state.json"),
        "CONTROL_FILE": str(tmp_path / "control.json"),
        "QUOTE_ASSET": "USDT",
        "USE_DUST_SWEEP": False,
        "COOLDOWN_MINUTES_AFTER_CLOSE": 1,
        "MODE": "PAPER",
    })
    return cfg


def _filters() -> dict:
    return {"TESTUSDT": SymbolFilters(
        step_size=Decimal("0.01"), min_qty=Decimal("0.01"),
        min_notional=Decimal("5"), tick_size=Decimal("0.0001"),
    )}


# ---------------------------------------------------------------------
# 1. Guard entry ATR tanpa nilai ATR
# ---------------------------------------------------------------------
def test_open_position_rejects_atr_exit_without_atr_value(tmp_path) -> None:
    class Client:
        def __init__(self):
            self.orders = []

        def get_account(self):
            return {"balances": [{"asset": "USDT", "free": "1000", "locked": "0"}]}

        def new_market_order(self, *a, **k):
            self.orders.append((a, k))
            return {"status": "FILLED", "executedQty": "1", "cummulativeQuoteQty": "100"}

    cfg = _config(tmp_path)
    cfg.update({"USE_ATR_EXIT": True, "USE_RISK_PERCENT": False,
                "POSITION_SIZE_USDT": 25.0, "MAX_POSITION_USDT": 100.0})
    state = dict(bot.DEFAULT_STATE)
    candidate = scanner.Candidate(
        symbol="TESTUSDT", base_asset="TEST", price_change_pct=20.0,
        quote_volume=1_000_000, last_price=100.0, confirmed=True,
        confirm_reason="test",  # setup=None -> atr_value tidak tersedia
    )
    client = Client()
    bot.open_position(client, cfg, _filters(), state, candidate, reference_price=100.0)

    assert client.orders == [], "BUY tidak boleh terkirim tanpa nilai ATR di mode ATR"
    assert state["current_symbol"] is None
    assert state["pending_order"] is None


# ---------------------------------------------------------------------
# 2. Fallback sadar-unit
# ---------------------------------------------------------------------
def test_exit_distance_converts_pct_fallback_in_atr_mode() -> None:
    state = {"sl_pct": 0.0, "exit_source": "ATR"}
    cfg = {"SL_PCT": 28.8}
    entry = 0.5  # koin murah: fallback lama menghasilkan jarak absolut 28.8
    jarak = bot._exit_distance(state, cfg, "sl_pct", "SL_PCT", atr_mode=True, entry=entry)
    assert abs(jarak - 0.5 * 0.288) < 1e-12, "persen wajib dikonversi ke jarak harga"
    # Nilai state yang sudah terkunci tetap menang apa adanya.
    state["sl_pct"] = 0.01
    assert bot._exit_distance(state, cfg, "sl_pct", "SL_PCT", True, entry) == 0.01
    # Mode persen: fallback tetap berdenominasi persen (perilaku lama).
    state["sl_pct"] = 0.0
    assert bot._exit_distance(state, cfg, "sl_pct", "SL_PCT", False, entry) == 28.8


def test_native_stop_price_pct_fallback_stays_positive_for_cheap_coin() -> None:
    state = {"entry_price": 0.5, "sl_pct": 0.0, "exit_source": "ATR"}
    cfg = {"SL_PCT": 28.8}
    stop = bot._native_stop_price(state, None, cfg)
    assert 0 < stop < 0.5, f"stopPrice harus valid di bawah entry, dapat {stop}"


# ---------------------------------------------------------------------
# 3. Settle pasca-fill proteksi native
# ---------------------------------------------------------------------
def test_settle_native_fill_clears_flag_and_sets_cooldown(tmp_path) -> None:
    class Client:
        def get_account(self):
            # Leg proteksi sudah menjual habis base asset.
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
        "flag wajib bersih setelah rekonsiliasi sukses; tanpa ini bot berhenti "
        "entry permanen setelah SETIAP exit native (bug audit 2026-09-27)"
    )
    assert state["cooldown_until"] > 0
    assert state["last_trade_time"] > 0
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

    # Saldo tidak terverifikasi -> tetap fail-closed, posisi tidak disentuh.
    assert state["reconciliation_required"] is True
    assert state["current_symbol"] == "TESTUSDT"
    assert state["cooldown_until"] == 0


# ---------------------------------------------------------------------
# 4. Klasifikasi reject deterministik
# ---------------------------------------------------------------------
def test_definitive_reject_includes_invalid_ordertype_codes() -> None:
    assert bot._is_definitive_reject(BinanceAPIError(400, -1116, "Invalid orderType."))
    assert bot._is_definitive_reject(BinanceAPIError(400, -1020, "Unsupported operation."))
    # Kode tak dikenal tetap fail-closed (UNKNOWN).
    assert not bot._is_definitive_reject(BinanceAPIError(500, -1000, "Unknown error."))


# ---------------------------------------------------------------------
# 5. Force close hanya untuk episode kerugian
# ---------------------------------------------------------------------
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
    # State lama tanpa daily_stop_source -> konservatif: tetap menutup.
    assert len(_force_close_calls(monkeypatch, tmp_path, None)) == 1
