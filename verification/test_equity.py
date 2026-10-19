import copy
import json
import math
import os
import random
import socket
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from trading import equity as eq
from trading import pump_scanner_bot as bot


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Jaringan dilarang dalam audit offline")
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def config(tmp_path):
    return {
        "MODE": "LIVE",
        "QUOTE_ASSET": "USDT",
        "API_KEY": "offline-test-key",
        "STATE_FILE": str(tmp_path / "state_live.json"),
        "TICKER_SNAPSHOT_TTL_SECONDS": 30,
        "MAX_MARKET_DATA_AGE_SECONDS": 10,
    }


def account(free="7.06", locked="0", others=()):
    return {
        "canTrade": True,
        "accountType": "SPOT",
        "permissions": ["SPOT"],
        "balances": [{"asset": "USDT", "free": free, "locked": locked}, *others],
    }


def asset(name="BNB", free="0.00015", locked="0"):
    return {"asset": name, "free": free, "locked": locked}


def ticker(symbol="BNBUSDT", bid="1000", age=0):
    return {"symbol": symbol, "bidPrice": bid, "closeTime": int(time.time() * 1000) - age}


def client(data=None, tickers=None):
    result = Mock()
    result.get_account.return_value = data if data is not None else account()
    result.get_ticker_24hr_all.return_value = tickers if tickers is not None else []
    return result


def test_only_quote(config):
    c = client()
    result = eq.collect_equity(c, config, {})
    assert result["status"] == "VALID"
    assert result["equity"] == 7.06
    assert result["quote_free"] == 7.06
    assert result["quote_locked"] == result["other_assets_value"] == 0
    assert eq.valid_snapshot(result, config)
    c.get_ticker_24hr_all.assert_not_called()


def test_locked_quote(config):
    result = eq.collect_equity(client(account(locked="0.15")), config, {})
    assert result["equity"] == pytest.approx(7.21)
    assert result["quote_locked"] == 0.15


def test_dust_without_position(config):
    result = eq.collect_equity(client(account(others=[asset()]), [ticker()]), config, {})
    assert result["equity"] == pytest.approx(7.21)
    assert result["other_assets_value"] == pytest.approx(0.15)
    assert result["assets"][1]["price_source"] == "TICKER_BID"


def test_position_not_counted_twice(config):
    c = client(account(others=[asset(free="1", locked="2")]))
    result = eq.collect_equity(c, config, {"current_symbol": "BNBUSDT", "qty": 3}, 1000)
    assert result["equity"] == pytest.approx(3007.06)
    assert result["other_assets_value"] == 3000
    c.get_ticker_24hr_all.assert_not_called()


@pytest.mark.parametrize("free,locked", [("nan", "0"), ("inf", "0"), ("-1", "0"), ("0", "-1"), (None, "0"), ("1e308", "1e308")])
def test_bad_balances_fail_closed(config, free, locked):
    result = eq.collect_equity(client(account(free, locked)), config, {})
    assert result["status"] == "UNVERIFIED"
    assert result["equity"] is None


@pytest.mark.parametrize("change", [
    {"canTrade": False}, {"accountType": "MARGIN"}, {"permissions": []},
    {"permissions": None}, {"permissions": "SPOT"}, {"balances": {}},
    {"balances": []}, {"balances": [asset("USDT"), asset("USDT")]},
])
def test_live_validation_preserved(config, change):
    data = account()
    data.update(change)
    result = eq.collect_equity(client(data), config, {})
    assert result["equity"] is None


def test_api_permission_evidence(config):
    data = account()
    data.pop("permissions")
    data[eq.ACCOUNT_SPOT_PERMISSION_VERIFIED] = True
    data[eq.ACCOUNT_SPOT_PERMISSION_SOURCE] = "GET /sapi/v1/account/apiRestrictions"
    assert eq.collect_equity(client(data), config, {})["equity"] == 7.06


@pytest.mark.parametrize("prices", [[], [ticker(bid="0")], [ticker(bid="nan")], [ticker(age=120000)], [ticker(age=-120000)], [ticker(), ticker()], [{}], {}])
def test_unpriceable_assets_fail_closed(config, prices):
    result = eq.collect_equity(client(account(others=[asset()]), prices), config, {})
    assert result["status"] == "UNVERIFIED"
    assert result["equity"] is None
    assert eq.valid_snapshot(result, config)


@pytest.mark.parametrize("method", ["get_account", "get_ticker_24hr_all"])
def test_api_failure(config, method):
    c = client(account(others=[asset()]), [ticker()])
    getattr(c, method).side_effect = RuntimeError("private information")
    result = eq.collect_equity(c, config, {})
    assert result["equity"] is None
    assert "private information" not in result["error"]


def test_paper_separation(config):
    config["MODE"] = "PAPER"
    data = {"balances": account()["balances"]}
    result = eq.collect_equity(client(data), config, {})
    assert result["equity"] == 7.06
    config["MODE"] = "LIVE"
    assert not eq.valid_snapshot(result, config)


def test_non_usdt_quote(config):
    config.update(QUOTE_ASSET="BTC", MODE="PAPER")
    c = client({"balances": [asset("BTC", "0.2", "0.1"), asset("ETH", "2")]}, [ticker("ETHBTC", "0.04")])
    result = eq.collect_equity(c, config, {})
    assert result["equity"] == pytest.approx(0.38)
    assert result["quote"] == "BTC"


def test_publication_and_matching_snapshot(config):
    result = eq.collect_equity(client(account(others=[asset()]), [ticker()]), config, {})
    eq.publish_equity(config, result)
    envelope = eq.load_equity(config)
    view = eq.equity_view(envelope, config, {"pid": os.getpid(), "started_at": time.time() - 10})
    assert view["equity_snapshot_id"] == result["snapshot_id"]
    assert view["equity_live"] == result["equity"]
    assert view["equity_status"] == "VALID"
    assert "offline-test-key" not in eq.snapshot_path(config).read_text()
    if os.name == "posix":
        assert eq.snapshot_path(config).stat().st_mode & 0o777 == 0o600


def test_invalid_retains_last_valid_for_display_only(config):
    good = eq.collect_equity(client(), config, {})
    eq.publish_equity(config, good)
    c = client()
    c.get_account.side_effect = RuntimeError()
    bad = eq.collect_equity(c, config, {})
    eq.publish_equity(config, bad)
    envelope = eq.load_equity(config)
    view = eq.equity_view(envelope, config)
    assert envelope["latest"]["equity"] is None
    assert view["equity_status"] == "STALE"
    assert view["equity_live"] == 7.06
    assert view["equity_snapshot_id"] == good["snapshot_id"]
    assert view["equity_attempt_id"] == bad["snapshot_id"]
    assert bot.get_equity(c, config, {}) is None


@pytest.mark.parametrize("offset", [-31000, 60000])
def test_stale_or_future_timestamp(config, monkeypatch, offset):
    result = eq.collect_equity(client(), config, {})
    result["captured_at_ms"] += offset
    result["completed_at_ms"] += offset
    view = eq.equity_view(eq.equity_envelope(result), config)
    assert view["equity_status"] == "STALE"


@pytest.mark.parametrize("change", [{"pid": -1}, {"started_at": 9999999999}])
def test_previous_bot_run_not_reused(config, change):
    result = eq.collect_equity(client(), config, {})
    process = {"pid": os.getpid(), "started_at": time.time() - 10, **change}
    view = eq.equity_view(eq.equity_envelope(result), config, process)
    assert view["equity_status"] == "WAITING"
    assert view["equity_live"] is None


@pytest.mark.parametrize("key,value", [("API_KEY", "different"), ("MODE", "PAPER"), ("QUOTE_ASSET", "BTC"), ("LIVE_BASE_URL", "https://testnet.binance.vision")])
def test_foreign_context_rejected(config, key, value):
    result = eq.collect_equity(client(), config, {})
    eq.publish_equity(config, result)
    config[key] = value
    assert eq.load_equity(config) is None


@pytest.mark.parametrize("payload", ["{", "[]", '{"latest":{}}', '{"latest":null}', '{"latest":{"version":1}}'])
def test_corrupt_snapshot_safe(config, payload):
    eq.snapshot_path(config).write_text(payload)
    assert eq.load_equity(config) is None
    assert eq.equity_view(None, config)["equity_status"] == "WAITING"


def test_nonfinite_disk_value_rejected(config):
    result = eq.collect_equity(client(), config, {})
    result["equity"] = float("nan")
    eq.snapshot_path(config).write_text(json.dumps(eq.equity_envelope(result)))
    assert eq.load_equity(config) is None


def test_dashboard_does_not_write_bot_snapshot(config):
    result = eq.collect_equity(client(), config, {}, source="DASHBOARD")
    with pytest.raises(ValueError):
        eq.publish_equity(config, result)
    assert not eq.snapshot_path(config).exists()


def test_legacy_parity(config):
    namespace = {
        "ExchangeClient": eq.ExchangeClient,
        "math": math,
        "time": time,
        "logger": Mock(),
        "_validate_live_account_snapshot": eq._validate_live_account_snapshot,
    }
    exec((Path(__file__).parent / "fixtures" / "equity_legacy.py").read_text(), namespace)
    legacy = namespace["get_equity"]
    rng = random.Random(421)
    for _ in range(100):
        data = account(str(rng.random() * 100), str(rng.random() * 10), [asset(free=str(rng.random()), locked=str(rng.random()))])
        prices = [ticker(bid=str(rng.random() * 1000 + 1))]
        state = {"current_symbol": "BNBUSDT", "qty": 999} if rng.choice([True, False]) else {}
        position_price = rng.random() * 1000 + 1 if state else None
        c = client(data, prices)
        assert bot.get_equity(c, config, state, position_price) == legacy(c, config, state, position_price)
    for prices in ([], [ticker(bid="0")], [ticker(age=999999)]):
        c = client(account(others=[asset()]), prices)
        assert bot.get_equity(c, config, {}) is legacy(c, config, {}) is None


@pytest.fixture
def dashboard(config, monkeypatch):
    from web import dashboard as dash
    monkeypatch.setattr(dash, "PUMP_CONFIG", config)
    monkeypatch.setattr(dash, "QUOTE", "USDT")
    monkeypatch.setattr(dash, "_equity_cache", {"context": None, "envelope": None, "ts": 0.0})
    return dash


def test_running_dashboard_no_account_api(config, dashboard, monkeypatch):
    result = eq.collect_equity(client(), config, {})
    eq.publish_equity(config, result)
    get_client = Mock(side_effect=AssertionError("Dashboard aktif tidak boleh meminta saldo sendiri"))
    monkeypatch.setattr(dashboard, "get_client", get_client)
    process = {"status": "RUNNING", "pid": os.getpid(), "started_at": time.time() - 10}
    view = dashboard.get_account_equity(process)
    assert view["equity_live"] == result["equity"]
    assert view["equity_snapshot_id"] == result["snapshot_id"]
    get_client.assert_not_called()


def test_running_dashboard_missing_snapshot_no_fallback(dashboard, monkeypatch):
    monkeypatch.setattr(dashboard, "get_client", Mock(side_effect=AssertionError()))
    view = dashboard.get_account_equity({"status": "RUNNING", "pid": os.getpid()})
    assert view["equity_status"] == "WAITING"


def test_stopped_dashboard_cache_and_failure(dashboard, monkeypatch):
    c = client(account(others=[asset()]), [ticker()])
    monkeypatch.setattr(dashboard, "get_client", lambda: c)
    process = {"status": "STOPPED"}
    first = dashboard.get_account_equity(process)
    second = dashboard.get_account_equity(process)
    assert first["equity_live"] == pytest.approx(7.21)
    assert first["equity_source"] == "DASHBOARD"
    assert first["equity_snapshot_id"] == second["equity_snapshot_id"]
    assert c.get_account.call_count == 1
    c.get_account.side_effect = RuntimeError()
    dashboard._equity_cache["ts"] = 0
    failed = dashboard.get_account_equity(process)
    assert failed["equity_status"] == "STALE"
    assert failed["equity_snapshot_id"] == first["equity_snapshot_id"]


def test_build_status_and_json(config, dashboard, monkeypatch):
    c = client(account(others=[asset()]), [ticker()])
    result = eq.collect_equity(c, config, {})
    eq.publish_equity(config, result)
    process = {"status": "RUNNING", "pid": os.getpid(), "started_at": time.time() - 10}
    monkeypatch.setattr(dashboard, "load_state", lambda: {})
    monkeypatch.setattr(dashboard._process_manager, "status", lambda mode: process)
    monkeypatch.setattr(dashboard, "get_idr_rate", lambda: {"enabled": False})
    with dashboard.app.test_client() as http:
        response = http.get('/api/status', base_url=f'http://localhost:{dashboard._DASHBOARD_PORT}')
        assert response.status_code == 200
        data = response.get_json()
    assert data["account"]["equity_live"] == result["equity"]
    assert data["account"]["usdt_free"] == 7.06
    assert data["account"]["equity_snapshot_id"] == result["snapshot_id"]
    assert data["live_connected"] is True


def test_template_renders(dashboard):
    with dashboard.app.test_request_context('/'):
        html = dashboard.render_template("dashboard.html", admin_token="offline")
    assert 'id="equityLocked"' in html
    assert 'renderEquityDetails(a, s.quote)' in html


def test_risk_controls_receive_same_value(config):
    c = client(account(others=[asset()]), [ticker()])
    result = eq.collect_equity(c, config, {})
    state = {"peak_equity": 8.0, "max_dd_pct": 0.0}
    config.update(USE_EQUITY_STOP=False)
    bot.update_equity_controls(state, result["equity"], config)
    assert state["max_dd_pct"] == pytest.approx((8 - 7.21) / 8 * 100)


@pytest.mark.parametrize("field,value", [
    ("enableReading", False), ("enableSpotAndMarginTrading", False),
    ("enableWithdrawals", True), ("ipRestrict", False),
])
def test_standalone_permission_failure(config, field, value):
    data = account()
    data["permissions"] = []
    c = client(data)
    c.get_api_key_permissions.return_value = {
        "enableReading": True, "enableSpotAndMarginTrading": True,
        "enableWithdrawals": False, "ipRestrict": True, field: value,
    }
    assert eq.collect_equity(eq.SpotEquityReader(c), config, {}, source="DASHBOARD")["equity"] is None


def test_standalone_permission_fallback(config):
    data = account()
    data.pop("permissions")
    c = client(data)
    c.get_api_key_permissions.return_value = {
        "enableReading": True, "enableSpotAndMarginTrading": True,
        "enableWithdrawals": False, "ipRestrict": True,
    }
    result = eq.collect_equity(eq.SpotEquityReader(c), config, {}, source="DASHBOARD")
    assert result["equity"] == 7.06
    c.get_api_key_permissions.assert_called_once()
    c.new_market_order.assert_not_called()


def test_snapshot_write_failure_preserves_previous(config, monkeypatch):
    previous = eq.collect_equity(client(), config, {})
    eq.publish_equity(config, previous)
    current = eq.collect_equity(client(account("8")), config, {})
    monkeypatch.setattr(eq, "atomic_write_json", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        eq.publish_equity(config, current)
    assert eq.load_equity(config)["latest"]["snapshot_id"] == previous["snapshot_id"]
    assert current["equity"] == 8


def test_read_only_safety_gate_unchanged(dashboard):
    with dashboard.app.test_client() as http:
        response = http.get('/api/status', base_url='http://untrusted.example')
    assert response.status_code == 400


def test_ui_script_and_rendering(dashboard, tmp_path):
    import re
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("Node.js diperlukan untuk audit JavaScript")
    with dashboard.app.test_request_context('/'):
        html = dashboard.render_template("dashboard.html", admin_token="offline")
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
    script_path = tmp_path / 'dashboard.js'
    script_path.write_text('\n'.join(scripts))
    subprocess.run(['node', '--check', str(script_path)], check=True, capture_output=True)
    source = Path(dashboard.app.template_folder, 'dashboard.html').read_text()
    start = source.index('      let equityDisplaySnapshot')
    end = source.index('\n      function ', source.index('      function renderEquityDetails') + 20)
    function_source = source[start:end]
    runner = r'''
const assert = require('node:assert/strict');
const nodes = {};
const document = {getElementById(id) {return nodes[id] ||= {textContent:'', innerHTML:'', style:{}};}};
const fmt = (value, digits=2) => Number(value).toLocaleString('id-ID', {minimumFractionDigits:digits,maximumFractionDigits:digits});
const esc = value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
let clock = 0;
const performance = {now: () => clock};
'''
    runner += function_source
    runner += r'''
const data = {equity_status:'VALID', equity_source:'BOT', quote_locked:0, other_assets_value:.15,equity_updated_at_ms:Date.now(),equity_age_seconds:1,equity_snapshot_id:'audit-id',assets:[{asset:'<img src=x>',free:.1,locked:0,value:.15}]};
renderEquityDetails(data, 'USDT');
assert.equal(nodes.equityOther.textContent, '0,1500 USDT');
assert.match(nodes.equityMeta.textContent, /Valid/);
assert.match(nodes.equityMeta.textContent, /audit-id/);
assert.ok(!nodes.equityAssets.innerHTML.includes('<img'));
assert.ok(nodes.equityAssets.innerHTML.includes('&lt;img'));
clock=31000;
renderEquityDetails(data, 'USDT', false);
assert.match(nodes.equityMeta.textContent, /Kedaluwarsa/);
assert.ok(nodes.equityVal.style.color.includes('amber'));
renderEquityDetails({equity_status:'WAITING',assets:[]}, 'USDT');
assert.match(nodes.equityMeta.textContent,/Menunggu snapshot/);
assert.match(nodes.equityAssets.innerHTML,/Belum ada data/);
'''
    script_path.write_text(runner)
    subprocess.run(['node', str(script_path)], check=True, capture_output=True)


def test_rounding_matches_heartbeat(config):
    result = eq.collect_equity(client(account("2.675")), config, {})
    view = eq.equity_view(eq.equity_envelope(result), config)
    assert view["equity_display"] == f'{result["equity"]:.2f}' == "2.67"


def test_zero_balance_is_not_missing(config):
    result = eq.collect_equity(client(account("0")), config, {})
    view = eq.equity_view(eq.equity_envelope(result), config)
    assert view["equity_live"] == 0
    assert view["equity_status"] == "VALID"


def test_paper_account_path_separation(config):
    config.update(MODE="PAPER", PAPER_ACCOUNT_STATE_FILE="one.json")
    result = eq.collect_equity(client(), config, {})
    config["PAPER_ACCOUNT_STATE_FILE"] = "two.json"
    assert not eq.valid_snapshot(result, config)


def test_preview_scenarios(tmp_path):
    import re
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("Node.js diperlukan untuk audit preview")
    preview = Path(__file__).resolve().parents[1] / "docs" / "equity-preview.html"
    if not preview.exists():
        pytest.skip("docs/equity-preview.html belum ada di repo")
    html = preview.read_text()
    source = '\n'.join(re.findall(r'<script[^>]*>(.*?)</script>', html, re.S))
    prefix = r'''
const assert = require('node:assert/strict');
const nodes = {};
const buttons = ['assets','locked','stale','waiting'].map(mode => ({dataset:{mode},setAttribute(){},addEventListener(){}}));
const document = {getElementById(id){return nodes[id] ||= {textContent:'',innerHTML:'',style:{}};},querySelectorAll(){return buttons;}};
const setInterval = () => 0;
'''
    suffix = r'''
assert.equal(nodes.equityVal.textContent,'7,21 USDT');
assert.match(nodes.equityOther.textContent,/0,1500/);
show('locked');
assert.match(nodes.equityLocked.textContent,/0,1000/);
assert.match(nodes.equityOther.textContent,/0,0500/);
show('stale');
assert.match(nodes.equityMeta.textContent,/Kedaluwarsa/);
assert.match(nodes.previewLog.textContent,/simulasi-gagal-002/);
show('waiting');
assert.equal(nodes.equityVal.textContent,'-');
assert.match(nodes.equityMeta.textContent,/Menunggu/);
'''
    path = tmp_path / 'preview.js'
    path.write_text(prefix + source + suffix)
    subprocess.run(['node', str(path)], check=True, capture_output=True)
