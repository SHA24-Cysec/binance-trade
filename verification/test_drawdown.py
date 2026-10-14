import ast
import copy
import inspect
import json
import math
import socket
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from trading import strategy_drawdown as dd
from trading import equity as eq
from trading import pump_scanner_bot as bot
from infrastructure.storage.trade_ledger import append_trade_record


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Audit tidak boleh mengakses jaringan")
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def clock(monkeypatch):
    value = [2000000000.0]
    monkeypatch.setattr(dd.time, "time", lambda: value[0])
    return value


@pytest.fixture
def config(tmp_path, clock):
    config = {
        "MODE": "LIVE", "QUOTE_ASSET": "USDT", "API_KEY": "offline",
        "STATE_FILE": str(tmp_path / "state.json"),
        "TRADE_LEDGER_FILE": str(tmp_path / "trades.jsonl"),
        "USE_EQUITY_STOP": True, "MAX_DRAWDOWN_PERCENT": 12,
    }
    Path(config["STATE_FILE"]).write_text(json.dumps({"current_symbol": None, "qty": 0, "peak_equity": 100, "max_dd_pct": 2, "dd_stopped": True, "dd_reset_count": 1}))
    return config


def change_state(config, **updates):
    path = Path(config["STATE_FILE"])
    state = json.loads(path.read_text())
    state.update(updates)
    path.write_text(json.dumps(state))
    return state


def start(config, capital="100"):
    inspection = dd.inspect_initialization(config)
    return dd.initialize(config, capital, inspection["fingerprint"])


def record(identity, pnl, clock, revision=1, **changes):
    now = int(clock[0] * 1000)
    return {"trade_id": identity, "revision": revision, "opened_at_ms": now, "closed_at_ms": now + 1, "paper": False, "quote_asset": "USDT", "accounting_status": "VERIFIED", "net_pnl_quote": pnl, "fee_conversion_pending": False, "fill_details_pending": False, **changes}


def add(config, item):
    append_trade_record(dd.ledger_path(config), item)


def test_uninitialized_not_zero(config):
    value = dd.strategy_view(config)
    assert value["status"] == "UNINITIALIZED"
    assert value["result"] is None
    assert not dd.ledger_path(config).exists()


def test_explicit_start_zero_trades_and_no_risk_reset(config):
    before = Path(config["STATE_FILE"]).read_bytes()
    period = start(config)
    view = dd.strategy_view(config)
    assert view["status"] == "VALID"
    assert view["result"]["capital"] == 100
    assert view["result"]["current_dd_pct"] == 0
    assert view["result"]["closed_trade_count"] == 0
    assert view["risk_control"] is False
    assert view["period_id"] == period["period_id"]
    assert Path(config["STATE_FILE"]).read_bytes() == before


def test_losses_recovery_and_historical_max(config, clock):
    start(config)
    clock[0] += 1
    add(config, record("loss", -1, clock))
    loss = dd.strategy_view(config)["result"]
    assert (loss["capital"], loss["current_dd_pct"], loss["max_dd_pct"]) == (99, 1, 1)
    clock[0] += 1
    add(config, record("win", 1, clock))
    recovered = dd.strategy_view(config)["result"]
    assert (recovered["capital"], recovered["current_dd_pct"], recovered["max_dd_pct"]) == (100, 0, 1)


def test_replay_revision_rebuilds_peaks(config, clock):
    start(config)
    clock[0] += 1
    loss = record("loss", -1, clock)
    add(config, loss)
    clock[0] += 1
    win = record("win", 1, clock)
    add(config, win)
    assert dd.strategy_view(config)["result"]["max_dd_pct"] == 1
    add(config, {**loss, "revision": 2, "net_pnl_quote": -2})
    value = dd.strategy_view(config)["result"]
    assert value["closed_trade_count"] == 2
    assert value["revised_trade_count"] == 1
    assert value["capital"] == 99
    assert value["current_dd_pct"] == 1
    assert value["max_dd_pct"] == 2
    assert not append_trade_record(dd.ledger_path(config), {**loss, "revision": 2, "net_pnl_quote": -2})
    assert dd.strategy_view(config)["result"] == value


def test_fee_not_subtracted_twice(config, clock):
    start(config)
    clock[0] += 1
    add(config, record("fee", -1, clock, fee_quote_value=0.5, fees_by_asset={"BNB": 0.001}))
    assert dd.strategy_view(config)["result"]["capital"] == 99


def test_bnb_and_external_balance_changes_not_strategy_pnl(config):
    start(config)
    initial = dd.strategy_view(config)["result"]
    change_state(config, peak_equity=10000, max_dd_pct=3, balance=777, last_bid=999)
    assert dd.strategy_view(config)["result"] == initial


def test_open_and_partial_position_excluded(config):
    start(config)
    change_state(config, current_symbol="BNBUSDT", qty=1, trade_accounting={"status": "VERIFIED", "realized_pnl_quote": 5})
    value = dd.strategy_view(config)
    assert value["status"] == "VALID"
    assert value["open_position"]
    assert value["result"]["capital"] == 100


@pytest.mark.parametrize("field", ["accounting_pending_trades", "accounting_blocked", "pending_native_settlement"])
def test_reconciliation_freezes_previous(config, field):
    start(config)
    change_state(config, **{field: True})
    view = dd.strategy_view(config)
    assert view["status"] == "INCOMPLETE"
    assert view["result"]["capital"] == 100
    change_state(config, **{field: False})
    assert dd.strategy_view(config)["status"] == "VALID"


@pytest.mark.parametrize("changes", [
    {"accounting_status": "UNVERIFIED"}, {"accounting_status": "LEGACY_UNVERIFIED"},
    {"net_pnl_quote": None}, {"fee_conversion_pending": True},
    {"fill_details_pending": True}, {"paper": True}, {"quote_asset": "BTC"},
])
def test_incomplete_or_wrong_context_record(config, clock, changes):
    start(config)
    clock[0] += 1
    add(config, record("x", -1, clock, **changes))
    view = dd.strategy_view(config)
    assert view["status"] == "INCOMPLETE"
    assert view["result"]["closed_trade_count"] == 0
    with pytest.raises(dd.DrawdownError):
        dd.inspect_initialization(config)


def test_pending_revision_recovers_without_double_count(config, clock):
    start(config)
    clock[0] += 1
    incomplete = record("pending", None, clock, accounting_status="UNVERIFIED")
    add(config, incomplete)
    assert dd.strategy_view(config)["status"] == "INCOMPLETE"
    add(config, {**incomplete, "revision": 2, "net_pnl_quote": -1, "accounting_status": "VERIFIED"})
    view = dd.strategy_view(config)
    assert view["status"] == "VALID"
    assert view["result"]["closed_trade_count"] == 1
    assert view["result"]["capital"] == 99


@pytest.mark.parametrize("raw", [b'{', b'[]\n', b'\n', b'{"trade_id":"x","trade_id":"y"}\n', b'{"net_pnl_quote":NaN}\n'])
def test_corrupt_ledger_never_becomes_zero(config, raw):
    start(config)
    dd.ledger_path(config).write_bytes(raw)
    view = dd.strategy_view(config)
    assert view["status"] == "INCOMPLETE"
    assert view["result"]["capital"] == 100


def test_missing_ledger_no_silent_zero_or_recreate(config):
    start(config)
    dd.ledger_path(config).unlink()
    assert dd.strategy_view(config)["status"] == "INCOMPLETE"
    with pytest.raises(dd.DrawdownError):
        dd.inspect_initialization(config)
    assert not dd.ledger_path(config).exists()


def test_truncated_previously_observed_history(config, clock):
    start(config)
    clock[0] += 1
    add(config, record("loss", -1, clock))
    dd.strategy_view(config)
    dd.ledger_path(config).write_bytes(b"")
    view = dd.strategy_view(config)
    assert view["status"] == "INCOMPLETE"
    assert view["result"]["capital"] == 99


def test_old_prefix_change_rejected(config, clock):
    old = record("old", 10, clock)
    add(config, old)
    clock[0] += 2
    start(config)
    dd.ledger_path(config).write_text(json.dumps({**old, "net_pnl_quote": 20}) + "\n")
    assert dd.strategy_view(config)["status"] == "INCOMPLETE"


def test_old_trades_and_revisions_excluded(config, clock):
    old = record("old", 10, clock)
    add(config, old)
    clock[0] += 2
    start(config)
    add(config, {**old, "revision": 2, "net_pnl_quote": 20})
    assert dd.strategy_view(config)["result"]["capital"] == 100


def test_new_record_from_before_period_not_silently_ignored(config, clock):
    old = record("old", 10, clock)
    clock[0] += 2
    start(config)
    add(config, old)
    assert dd.strategy_view(config)["status"] == "INCOMPLETE"


def test_all_records_not_last_thousand(config, clock):
    start(config)
    clock[0] += 1
    rows = [record(str(i), "-0.001", clock) for i in range(1201)]
    dd.ledger_path(config).write_text(''.join(json.dumps(row) + '\n' for row in rows))
    result = dd.strategy_view(config)["result"]
    assert result["capital"] == 98.799
    assert result["closed_trade_count"] == 1201
    assert result["max_dd_pct"] == 1.201


def test_negative_capital_not_clamped(config, clock):
    start(config)
    clock[0] += 1
    add(config, record("loss", -110, clock))
    result = dd.strategy_view(config)["result"]
    assert result["capital"] == -10
    assert result["max_dd_pct"] == 110


def test_restart_persists_period(config, clock):
    original = start(config)
    clock[0] += 1
    add(config, record("loss", -1, clock))
    first = dd.strategy_view(config)
    second = dd.strategy_view(copy.deepcopy(config))
    assert first["result"] == second["result"]
    assert second["period_id"] == original["period_id"]


def test_reset_archives_without_changing_risk_or_ledger(config, clock):
    old = start(config)
    clock[0] += 1
    add(config, record("loss", -1, clock))
    dd.strategy_view(config)
    before_state = Path(config["STATE_FILE"]).read_bytes()
    before_ledger = dd.ledger_path(config).read_bytes()
    clock[0] += 2
    new = start(config, "50")
    assert new["period_id"] != old["period_id"]
    archive = Path(str(dd.period_path(config)) + '.history') / (old["period_id"] + '.json')
    assert json.loads(archive.read_text())["last_complete"]["capital"] == 99
    assert Path(config["STATE_FILE"]).read_bytes() == before_state
    assert dd.ledger_path(config).read_bytes() == before_ledger
    assert dd.strategy_view(config)["result"]["capital"] == 50


@pytest.mark.parametrize("value", [0, -1, "NaN", "Infinity", "1e99", "1e-99", None, True, "abc"])
def test_invalid_initial_capital(config, value):
    with pytest.raises(dd.DrawdownError):
        start(config, value)
    assert not dd.period_path(config).exists()


@pytest.mark.parametrize("change", [{"current_symbol": "BNBUSDT", "qty": 1}, {"pending_order": {} , "accounting_pending_trades": [{}]}, {"native_stop": {"order_id": 1}}, {"reconciliation_required": True}, {"state_integrity_error": "bad"}])
def test_unsafe_initialization_rejected(config, change):
    change_state(config, **change)
    with pytest.raises(dd.DrawdownError):
        dd.inspect_initialization(config)


def test_missing_or_malformed_state(config):
    for payload in ({}, {"current_symbol": [], "qty": 0}, {"current_symbol": "", "qty": 0}, {"current_symbol": None, "qty": "NaN"}):
        Path(config["STATE_FILE"]).write_text(json.dumps(payload))
        with pytest.raises(dd.DrawdownError):
            dd.inspect_initialization(config)


def test_state_or_ledger_changed_after_prepare(config, clock):
    info = dd.inspect_initialization(config)
    change_state(config, dd_reset_count=7)
    with pytest.raises(dd.DrawdownError):
        dd.initialize(config, 100, info["fingerprint"])
    assert not dd.period_path(config).exists()


def test_last_closed_trade_not_in_ledger(config):
    start(config)
    change_state(config, last_closed_trade={"trade_id": "missing"})
    assert dd.strategy_view(config)["status"] == "INCOMPLETE"


@pytest.mark.parametrize("key,value", [("MODE", "PAPER"), ("API_KEY", "another"), ("QUOTE_ASSET", "BTC"), ("TRADE_LEDGER_FILE", "other.jsonl")])
def test_account_mode_ledger_context_separation(config, key, value):
    start(config)
    config[key] = value
    view = dd.strategy_view(config)
    assert view["status"] == "INCOMPLETE"
    assert view["result"] is None


def test_period_corruption_not_reset_to_zero(config):
    start(config)
    dd.period_path(config).write_text('{')
    assert dd.strategy_view(config)["result"] is None
    assert dd.strategy_view(config)["status"] == "INCOMPLETE"


def test_write_failure_keeps_previous_complete(config, clock, monkeypatch):
    start(config)
    clock[0] += 1
    add(config, record("loss", -1, clock))
    monkeypatch.setattr(dd, "atomic_write_json", Mock(side_effect=OSError("full")))
    view = dd.strategy_view(config)
    assert view["status"] == "INCOMPLETE"
    assert view["result"]["capital"] == 100


def test_account_current_and_max_use_same_snapshot(config):
    state = {"peak_equity": 100, "max_dd_pct": 2}
    risk = eq.account_risk_snapshot(99, state)
    assert risk["current_dd_pct"] == 1
    assert risk["max_dd_pct"] == 2
    view = eq.account_drawdown_view(state, {"risk_snapshot": risk, "equity_status": "VALID", "equity_snapshot_id": "same"}, {"status": "RUNNING"}, config)
    assert view["snapshot_id"] == "same"
    assert view["risk_control"]
    assert view["current_dd_pct"] == 1
    assert eq.account_drawdown_view(state, {}, {"status": "STOPPED"}, config)["current_dd_pct"] is None


def test_risk_logic_unchanged():
    fixture = (Path(__file__).parent / 'fixtures' / 'account_risk_legacy.py').read_text()
    assert ast.dump(ast.parse(fixture), include_attributes=False) == ast.dump(ast.parse(inspect.getsource(bot.update_equity_controls)), include_attributes=False)


@pytest.fixture
def dashboard(config, tmp_path, monkeypatch):
    from web import dashboard as dash
    from infrastructure.process import runtime_control
    monkeypatch.setattr(runtime_control, "DATA_DIR", tmp_path / 'runtime')
    monkeypatch.setattr(dash, "PUMP_CONFIG", config)
    monkeypatch.setattr(dash, "QUOTE", "USDT")
    monkeypatch.setattr(dash, "CONFIG_LOAD_ERRORS", [])
    monkeypatch.setattr(dash, "_control_operation", None)
    monkeypatch.setattr(dash, "_confirmations", {})
    monkeypatch.setattr(dash, "_write_attempts", {})
    manager = Mock()
    manager._operation_lock = threading.Lock()
    manager._auto_restart_mode = None
    manager.status.return_value = {"status": "STOPPED"}
    manager.mode_guard.return_value = {"safe": True}
    monkeypatch.setattr(dash, "_process_manager", manager)
    client = Mock()
    client.get_open_orders.return_value = []
    client.get_account.return_value = {"canTrade": True, "accountType": "SPOT", "permissions": ["SPOT"], "balances": [{"asset": "USDT", "free": "100", "locked": "0"}]}
    monkeypatch.setattr(dash, "get_client", lambda: client)
    dash._test_client = client
    return dash


def post(dash, endpoint, data, **options):
    base = f'http://localhost:{dash._DASHBOARD_PORT}'
    headers = {"Origin": base, "X-Admin-Token": dash._ADMIN_TOKEN}
    headers.update(options.pop('headers', {}))
    with dash.app.test_client() as http:
        return http.post(endpoint, json=data, base_url=base, headers=headers, **options)


def prepare(dash):
    if dd.load_period(dash.PUMP_CONFIG) is None:
        start(dash.PUMP_CONFIG)
    return post(dash, '/api/strategy-dd/prepare', {"initial_capital": "100", "acknowledge": True})


def test_confirm_twice_and_replay_token_rejected(config, dashboard):
    before = Path(config["STATE_FILE"]).read_bytes()
    response = prepare(dashboard)
    assert response.status_code == 200, response.get_json()
    previous_id = dd.load_period(config)["period_id"]
    assert previous_id == response.get_json()["previous_period_id"]
    token = response.get_json()["confirmation_id"]
    response = post(dashboard, '/api/strategy-dd/execute', {"confirmation_id": token, "confirmed": True})
    assert response.status_code == 200, response.get_json()
    assert dd.load_period(config)["period_id"] != previous_id
    assert Path(config["STATE_FILE"]).read_bytes() == before
    again = post(dashboard, '/api/strategy-dd/execute', {"confirmation_id": token, "confirmed": True})
    assert again.status_code == 409
    dashboard._test_client.new_market_order.assert_not_called()


@pytest.mark.parametrize("data", [[], {}, {"initial_capital": "100", "acknowledge": False}])
def test_no_implicit_confirmation(dashboard, data):
    assert post(dashboard, '/api/strategy-dd/prepare', data).status_code == 400


def test_expired_confirmation(dashboard, clock):
    token = prepare(dashboard).get_json()["confirmation_id"]
    clock[0] += 121
    assert post(dashboard, '/api/strategy-dd/execute', {"confirmation_id": token, "confirmed": True}).status_code == 409


@pytest.mark.parametrize("headers", [{"X-Admin-Token": "bad"}, {"Origin": "https://foreign.example"}])
def test_security_gates(dashboard, headers):
    assert post(dashboard, '/api/strategy-dd/prepare', {"initial_capital": "100", "acknowledge": True}, headers=headers).status_code == 403


def test_bot_running_during_confirm_rejected(dashboard):
    token = prepare(dashboard).get_json()["confirmation_id"]
    dashboard._process_manager.status.return_value = {"status": "RUNNING"}
    assert post(dashboard, '/api/strategy-dd/execute', {"confirmation_id": token, "confirmed": True}).status_code == 409


def test_live_open_order_rejected(dashboard):
    dashboard._test_client.get_open_orders.return_value = [{"orderId": 7}]
    assert prepare(dashboard).status_code == 409


def test_no_capital_above_free_balance(dashboard):
    response = post(dashboard, '/api/strategy-dd/prepare', {"initial_capital": "101", "acknowledge": True})
    assert response.status_code == 409


def test_api_error_sanitized(dashboard):
    dashboard._test_client.get_open_orders.side_effect = RuntimeError("SECRET-EXAMPLE")
    response = prepare(dashboard)
    assert response.status_code == 503
    assert "SECRET-EXAMPLE" not in response.get_data(as_text=True)


def test_global_lock_stops_concurrent_bot_start(dashboard):
    from infrastructure.process.runtime_control import BotModeLock, BotAlreadyRunningError
    with dashboard._drawdown_maintenance():
        competing = BotModeLock('LIVE', global_scope=True)
        with pytest.raises(BotAlreadyRunningError):
            competing.acquire()
    competing.acquire()
    competing.release()


def test_global_lock_released_on_exception(dashboard):
    from infrastructure.process.runtime_control import BotModeLock
    with pytest.raises(RuntimeError):
        with dashboard._drawdown_maintenance():
            raise RuntimeError()
    lock = BotModeLock('LIVE', global_scope=True)
    lock.acquire()
    lock.release()


def test_two_prepared_tokens_cannot_double_initialize(config, dashboard):
    first = prepare(dashboard).get_json()["confirmation_id"]
    second = prepare(dashboard).get_json()["confirmation_id"]
    assert post(dashboard, '/api/strategy-dd/execute', {"confirmation_id": first, "confirmed": True}).status_code == 200
    identity = dd.load_period(config)["period_id"]
    assert post(dashboard, '/api/strategy-dd/execute', {"confirmation_id": second, "confirmed": True}).status_code == 409
    assert dd.load_period(config)["period_id"] == identity


def test_changed_balance_before_execution_rejected(config, dashboard):
    token = prepare(dashboard).get_json()["confirmation_id"]
    dashboard._test_client.get_account.return_value["balances"][0]["free"] = "99"
    assert post(dashboard, '/api/strategy-dd/execute', {"confirmation_id": token, "confirmed": True}).status_code == 409
    assert dd.load_period(config)["initial_capital"] == "100"


def test_account_context_changed_before_execution_rejected(config, dashboard):
    token = prepare(dashboard).get_json()["confirmation_id"]
    config["API_KEY"] = "different-account"
    assert post(dashboard, '/api/strategy-dd/execute', {"confirmation_id": token, "confirmed": True}).status_code == 409


def test_auto_restart_pending_blocks_initialization(dashboard):
    dashboard._process_manager._auto_restart_mode = "LIVE"
    assert prepare(dashboard).status_code == 409


def test_other_mode_unsafe_blocks_initialization(dashboard):
    dashboard._process_manager.mode_guard.return_value = {"safe": False}
    assert prepare(dashboard).status_code == 409


def test_paper_initialization_no_signed_order_query(config, dashboard, tmp_path):
    config["MODE"] = "PAPER"
    path = tmp_path / "paper.json"
    path.write_text('{"open_orders":[]}')
    config["PAPER_ACCOUNT_STATE_FILE"] = str(path)
    response = prepare(dashboard)
    assert response.status_code == 200, response.get_json()
    dashboard._test_client.get_open_orders.assert_not_called()
    dashboard._test_client.get_api_key_permissions.assert_not_called()


def test_paper_open_orders_rejected(config, dashboard, tmp_path):
    config["MODE"] = "PAPER"
    path = tmp_path / "paper.json"
    path.write_text('{"open_orders":[{"orderId":1}]}')
    config["PAPER_ACCOUNT_STATE_FILE"] = str(path)
    assert prepare(dashboard).status_code == 409


def test_conflicting_same_revision_is_incomplete(config, clock):
    start(config)
    clock[0] += 1
    row = record("x", -1, clock)
    add(config, row)
    with dd.ledger_path(config).open('a') as handle:
        handle.write(json.dumps({**row, "net_pnl_quote": -2}) + '\n')
    assert dd.strategy_view(config)["status"] == "INCOMPLETE"


def test_identical_duplicate_record_not_double_counted(config, clock):
    start(config)
    clock[0] += 1
    row = record("x", -1, clock)
    raw = (json.dumps(row) + '\n').encode()
    dd.ledger_path(config).write_bytes(raw * 2)
    result = dd.strategy_view(config)["result"]
    assert result["capital"] == 99
    assert result["closed_trade_count"] == 1


def test_reset_archive_captures_unobserved_final_trade(config, clock):
    old = start(config)
    clock[0] += 1
    add(config, record("loss", -1, clock))
    clock[0] += 1
    start(config, 50)
    archive = Path(str(dd.period_path(config)) + '.history') / (old["period_id"] + '.json')
    assert json.loads(archive.read_text())["last_complete"]["capital"] == 99


def test_huge_percentage_last_result_remains_available(config, clock):
    start(config, '0.00000001')
    clock[0] += 1
    add(config, record("loss", -1000000000, clock))
    view = dd.strategy_view(config)
    assert view["status"] == "VALID"
    assert math.isfinite(view["result"]["max_dd_pct"])
    dd.ledger_path(config).unlink()
    assert dd.strategy_view(config)["result"] == view["result"]


def test_strategy_endpoint_integration(config, dashboard, monkeypatch):
    start(config)
    monkeypatch.setattr(dashboard, "get_account_equity", lambda status: {"equity_status": "VALID", "equity_live": 100, "usdt_free": 100})
    monkeypatch.setattr(dashboard, "load_state", lambda: dd.read_state(config))
    monkeypatch.setattr(dashboard, "get_idr_rate", lambda: {"enabled": False})
    with dashboard.app.test_client() as http:
        response = http.get('/api/status', base_url=f'http://localhost:{dashboard._DASHBOARD_PORT}')
    assert response.status_code == 200
    value = response.get_json()
    assert value["drawdown_strategy"]["status"] == "VALID"
    assert value["drawdown_strategy"]["result"]["current_dd_pct"] == 0
    assert value["drawdown_account"]["max_dd_pct"] == 2
    assert value["flags"]["dd_stopped"] is True


def test_drawdown_ui_rendering_and_confirmation(tmp_path):
    import shutil
    import subprocess
    if not shutil.which('node'):
        pytest.skip('Node.js diperlukan untuk audit JavaScript')
    source = (Path(__file__).resolve().parents[1] / 'templates/dashboard.html').read_text()
    start_at = source.index('      let drawdownStatus')
    end_at = source.index('      function fmtExitVsEntry', start_at)
    script = source[start_at:end_at]
    prefix = r'''
const assert = require('node:assert/strict');
const nodes = {};
const document = {
 getElementById(id) {return nodes[id] ||= {textContent:'',style:{},value:'',checked:false,hidden:false,disabled:false,focus(){},checkValidity(){return Number(this.value)>0;},classList:{add(){},remove(){},contains(){return false;}}};},
 addEventListener(){}
};
const fmt=(value,d=2)=>Number(value).toLocaleString('id-ID',{minimumFractionDigits:d,maximumFractionDigits:d});
let tick=0;
const performance={now:()=>tick};
const setInterval=()=>0;
let requests=[];
let refreshes=0;
const refresh=async()=>{refreshes++;};
const responseJson=async response=>response.data;
let response={ok:true,data:{confirmation_id:'nonce',initial_capital:'100',quote:'USDT'}};
const adminFetch=async(url,options)=>{requests.push([url,JSON.parse(options.body)]);return response;};
'''
    suffix = r'''
async function run(){
 const state={quote:'USDT',process:{status:'STOPPED'},read_only:false,account:{equity_status:'VALID',usdt_free:100},drawdown_account:{status:'VALID',current_dd_pct:.01,max_dd_pct:.02,stop_enabled:true,limit_pct:12},drawdown_strategy:{status:'UNINITIALIZED',result:null}};
 renderDrawdowns(state);
 assert.equal(nodes.ddCurrent.textContent,'0,01%');
 assert.equal(nodes.strategyDdCurrent.textContent,'-');
 assert.match(nodes.strategyDdMeta.textContent,/inisialisasi otomatis/);
 assert.equal(nodes.strategyPeriodBtn.disabled,true);
 state.drawdown_strategy={status:'VALID',period_id:'existing',started_at_ms:Date.now(),result:{capital:100,initial_capital:100,current_dd_pct:0,max_dd_pct:0,closed_trade_count:0}};
 renderDrawdowns(state);
 openStrategyPeriod();
 assert.equal(nodes.strategyInitialInput.value,'100');
 await prepareStrategyPeriod();
 assert.equal(requests.length,0);
 nodes.strategyAcknowledge.checked=true;
 await prepareStrategyPeriod();
 assert.equal(requests.length,1);
 assert.equal(nodes.strategyExecute.hidden,false);
 assert.equal(requests[0][1].initial_capital,'100');
 invalidateStrategyConfirmation();
 await executeStrategyPeriod();
 assert.equal(requests.length,1);
 await prepareStrategyPeriod();
 response={ok:true,data:{message:'Periode dibuat'}};
 await executeStrategyPeriod();
 assert.equal(requests[2][0],'/api/strategy-dd/execute');
 assert.equal(requests[2][1].confirmed,true);
 assert.equal(refreshes,1);
 await executeStrategyPeriod();
 assert.equal(requests.length,3);
 const result={current_dd_pct:0,max_dd_pct:1,capital:100,initial_capital:100,closed_trade_count:2};
 state.drawdown_strategy={status:'VALID',result,period_id:'p',started_at_ms:Date.now()};
 renderDrawdowns(state);
 assert.equal(nodes.strategyDdCurrent.textContent,'0,00%');
 assert.equal(nodes.strategyDdMax.textContent,'1,00%');
 tick=31001;
 renderDrawdowns(state,false);
 assert.match(nodes.strategyDdMeta.textContent,/kedaluwarsa/);
 assert.equal(nodes.strategyPeriodBtn.disabled,true);
 result.initial_capital=0.00000001;
 renderDrawdowns(state);
 assert.equal(nodes.strategyInitial.textContent,'0,00000001 USDT');
 result.revised_trade_count=1;
 renderDrawdowns(state);
 assert.match(nodes.strategyDdMeta.textContent,/revisi accounting/);
 state.drawdown_strategy.status='INCOMPLETE';
 state.drawdown_strategy.error='<img src=x>';
 renderDrawdowns(state);
 assert.match(nodes.strategyDdMeta.textContent,/hasil terakhir/);
 assert.match(nodes.strategyDdMeta.textContent,/<img src=x>/);
 assert.equal(nodes.strategyPeriodBtn.disabled,true);
 assert.equal(nodes.strategyDdCurrent.textContent,'0,00%');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
'''
    path = tmp_path / 'drawdown-ui.js'
    path.write_text(prefix + script + suffix)
    subprocess.run(['node', str(path)], capture_output=True, check=True)


def test_extreme_decimal_exponent_fails_safely(config, clock):
    with pytest.raises(dd.DrawdownError):
        dd.amount('1e999999999', positive=True)
    start(config)
    clock[0] += 1
    add(config, record('bad', '1e999999999', clock))
    assert dd.strategy_view(config)['status'] == 'INCOMPLETE'


def test_period_path_traversal_id_rejected(config):
    period = start(config)
    period['period_id'] = '../' * 10 + 'xx'
    dd.period_path(config).write_text(json.dumps(period))
    assert dd.strategy_view(config)['status'] == 'INCOMPLETE'


def test_missing_state_during_reporting_keeps_last_complete(config):
    start(config)
    Path(config['STATE_FILE']).unlink()
    result = dd.strategy_view(config)
    assert result['status'] == 'INCOMPLETE'
    assert result['result']['capital'] == 100
