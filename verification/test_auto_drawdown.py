import ast
import copy
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from trading import strategy_drawdown as dd
from trading import equity as eq
from trading import pump_scanner_bot as bot
from verification.test_drawdown import config, clock, offline, dashboard, post, change_state, start, record, add


def fake_client(config, free='7.06'):
    client = Mock()
    client.get_account.return_value = {
        'canTrade': True, 'accountType': 'SPOT', 'permissions': ['SPOT'],
        'balances': [{'asset': 'USDT', 'free': free, 'locked': '0'}, {'asset': 'BNB', 'free': '0.00015', 'locked': '0'}],
    }
    client.get_ticker_24hr_all.return_value = [{'symbol': 'BNBUSDT', 'bidPrice': '1000', 'closeTime': int(dd.time.time() * 1000)}]
    client.get_open_orders.return_value = []
    client.store.get_open_orders.return_value = []
    return client


def sample(config, free='7.06'):
    client = fake_client(config, free)
    snapshot = eq.collect_equity(client, config, {})
    client.reset_mock()
    return client, snapshot


def state(config):
    return dd.read_state(config)


def test_first_period_auto_uses_free_quote_not_equity(config):
    current = state(config)
    before = copy.deepcopy(current)
    client, snapshot = sample(config)
    assert snapshot['equity'] == pytest.approx(7.21)
    result = dd.auto_entry_gate(client, config, current, snapshot)
    assert result['ready'] and result['created']
    saved = dd.load_period(config)
    assert saved['initial_capital'] == '7.06'
    assert saved['initialization_source'] == 'AUTO_FREE_QUOTE'
    assert saved['capital_snapshot_id'] == snapshot['snapshot_id']
    assert current['strategy_dd_initialized'] is True
    for key, value in before.items():
        assert current[key] == value
    assert dd.read_receipt(config) == dd.period_receipt(saved)
    client.get_account.assert_not_called()
    client.new_market_order.assert_not_called()


def test_subsequent_loops_deposit_bnb_and_restart_no_reset(config, clock):
    current = state(config)
    client, snapshot = sample(config)
    first = dd.auto_entry_gate(client, config, current, snapshot)
    period_bytes = dd.period_path(config).read_bytes()
    Path(config['STATE_FILE']).write_text(json.dumps(current))
    clock[0] += 10
    fresh_client, changed_snapshot = sample(config, '100')
    restarted = state(config)
    result = dd.auto_entry_gate(fresh_client, config, restarted, changed_snapshot)
    assert result['ready'] and not result['created']
    assert result['period_id'] == first['period_id']
    assert result['initial_capital'] == '7.06'
    assert dd.period_path(config).read_bytes() == period_bytes
    fresh_client.get_open_orders.assert_not_called()


def test_existing_manual_period_adopted_without_reset(config):
    old = start(config, '5')
    dd.receipt_path(config).unlink()
    client, snapshot = sample(config)
    result = dd.auto_entry_gate(client, config, state(config), snapshot)
    assert result['ready'] and not result['created']
    assert result['period_id'] == old['period_id']
    assert result['initial_capital'] == '5'
    assert dd.read_receipt(config) == dd.period_receipt(old)
    client.get_open_orders.assert_not_called()


@pytest.mark.parametrize('updates', [
    {'current_symbol': 'BNBUSDT', 'qty': 1},
    {'pending_order': {'side': 'BUY'}}, {'accounting_pending_trades': [{}]},
    {'accounting_blocked': True}, {'reconciliation_required': True},
    {'pending_native_settlement': {'order_id': 1}}, {'state_integrity_error': 'bad'},
])
def test_not_flat_or_not_reconciled_waits_without_risk_reset(config, updates):
    current = state(config)
    current.update(updates)
    client, snapshot = sample(config)
    before_risk = {key: current[key] for key in ('peak_equity', 'max_dd_pct', 'dd_stopped')}
    result = dd.auto_entry_gate(client, config, current, snapshot)
    assert not result['ready']
    assert not dd.period_path(config).exists()
    for key, value in before_risk.items():
        assert current[key] == value
    client.get_open_orders.assert_not_called()


def test_existing_period_does_not_reset_while_position_open(config):
    original = start(config, '5')
    current = state(config)
    current.update(current_symbol='BNBUSDT', qty=1)
    client, snapshot = sample(config)
    result = dd.auto_entry_gate(client, config, current, snapshot)
    assert result['ready']
    assert result['period_id'] == original['period_id']
    assert result['initial_capital'] == '5'


@pytest.mark.parametrize('orders', [[{'orderId': 1}], None, {}])
def test_unknown_or_open_orders_block_bootstrap(config, orders):
    client, snapshot = sample(config)
    client.get_open_orders.return_value = orders
    result = dd.auto_entry_gate(client, config, state(config), snapshot)
    assert not result['ready']
    assert not dd.period_path(config).exists()


def test_api_failure_redacted_and_retry_succeeds(config):
    client, snapshot = sample(config)
    current = state(config)
    client.get_open_orders.side_effect = RuntimeError('secret-value')
    result = dd.auto_entry_gate(client, config, current, snapshot)
    assert not result['ready'] and 'secret-value' not in result['reason']
    client.get_open_orders.side_effect = None
    result = dd.auto_entry_gate(client, config, current, snapshot)
    assert result['ready'] and result['created']


@pytest.mark.parametrize('change', [
    {'status': 'UNVERIFIED', 'equity': None}, {'source': 'DASHBOARD'},
    {'context': 'wrong'}, {'quote_free': 0}, {'quote_free': float('nan')},
])
def test_invalid_snapshot_cannot_set_initial_capital(config, change):
    client, snapshot = sample(config)
    snapshot.update(change)
    assert not dd.auto_entry_gate(client, config, state(config), snapshot)['ready']
    assert not dd.period_path(config).exists()


def test_stale_snapshot_and_slow_order_request_rejected(config, clock):
    client, snapshot = sample(config)
    clock[0] += 31
    assert not dd.auto_entry_gate(client, config, state(config), snapshot)['ready']
    client, snapshot = sample(config)
    def delayed():
        clock[0] += 31
        return []
    client.get_open_orders.side_effect = delayed
    assert not dd.auto_entry_gate(client, config, state(config), snapshot)['ready']


def test_paper_reads_store_without_processing_orders(config):
    config['MODE'] = 'PAPER'
    client, snapshot = sample(config)
    result = dd.auto_entry_gate(client, config, state(config), snapshot)
    assert result['created']
    client.store.get_open_orders.assert_called_once()
    client.get_open_orders.assert_not_called()
    client.engine.process_open_orders.assert_not_called()
    client.get_account.assert_not_called()


def test_no_dashboard_no_disk_state_bootstrap(config):
    current = state(config)
    Path(config['STATE_FILE']).unlink()
    client, snapshot = sample(config)
    assert dd.auto_entry_gate(client, config, current, snapshot)['ready']
    assert dd.require_entry_period(config, current)['initial_capital'] == '7.06'


def test_prior_verified_history_becomes_baseline(config, clock):
    old = record('old', -1, clock)
    add(config, old)
    clock[0] += 10
    current = change_state(config, last_closed_trade={'trade_id': 'old'})
    client, snapshot = sample(config)
    assert dd.auto_entry_gate(client, config, current, snapshot)['created']
    assert dd.load_period(config)['baseline_ids'] == ['old']
    assert dd.strategy_view(config)['result']['closed_trade_count'] == 0


def test_unverified_historical_records_not_silently_excluded(config, clock):
    add(config, record('old', None, clock, accounting_status='UNVERIFIED'))
    clock[0] += 10
    client, snapshot = sample(config)
    assert not dd.auto_entry_gate(client, config, state(config), snapshot)['ready']
    assert not dd.period_path(config).exists()


def test_metadata_deleted_no_auto_reset(config):
    current = state(config)
    client, snapshot = sample(config)
    dd.auto_entry_gate(client, config, current, snapshot)
    dd.period_path(config).unlink()
    result = dd.auto_entry_gate(client, config, current, snapshot)
    assert not result['ready']
    assert not dd.period_path(config).exists()
    assert dd.strategy_view(config)['status'] == 'INCOMPLETE'


def test_both_metadata_files_deleted_state_marker_blocks(config):
    current = state(config)
    client, snapshot = sample(config)
    dd.auto_entry_gate(client, config, current, snapshot)
    Path(config['STATE_FILE']).write_text(json.dumps(current))
    dd.period_path(config).unlink()
    dd.receipt_path(config).unlink()
    assert not dd.auto_entry_gate(client, config, current, snapshot)['ready']
    assert dd.strategy_view(config)['status'] == 'INCOMPLETE'


def test_corrupt_or_mismatched_receipt_blocks(config):
    current = state(config)
    client, snapshot = sample(config)
    dd.auto_entry_gate(client, config, current, snapshot)
    original = dd.read_receipt(config)
    dd.receipt_path(config).write_text('{')
    assert not dd.auto_entry_gate(client, config, current, snapshot)['ready']
    original['period_id'] = '0' * 32
    dd.receipt_path(config).write_text(json.dumps(original))
    assert not dd.auto_entry_gate(client, config, current, snapshot)['ready']


def test_partial_commit_cannot_be_treated_as_fresh(config, monkeypatch):
    client, snapshot = sample(config)
    current = state(config)
    writer = dd.atomic_write_json
    def failing(path, data, **kwargs):
        if Path(path) == dd.period_path(config):
            raise OSError('disk full')
        return writer(path, data, **kwargs)
    monkeypatch.setattr(dd, 'atomic_write_json', failing)
    assert not dd.auto_entry_gate(client, config, current, snapshot)['ready']
    assert dd.receipt_path(config).exists()
    assert not dd.period_path(config).exists()
    monkeypatch.setattr(dd, 'atomic_write_json', writer)
    assert not dd.auto_entry_gate(client, config, current, snapshot)['ready']


def test_existing_period_observed_ledger_protected_without_dashboard(config, clock):
    current = state(config)
    client, snapshot = sample(config)
    dd.auto_entry_gate(client, config, current, snapshot)
    clock[0] += 1
    add(config, record('new', -1, clock))
    assert dd.auto_entry_gate(client, config, current, snapshot)['ready']
    dd.ledger_path(config).write_bytes(b'')
    assert not dd.auto_entry_gate(client, config, current, snapshot)['ready']


def test_race_creates_exactly_one_period(config):
    client, snapshot = sample(config)
    def attempt(_):
        return dd.auto_entry_gate(client, config, state(config), snapshot)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert all(item['ready'] for item in results)
    assert sum(item['created'] for item in results) == 1
    assert len({item['period_id'] for item in results}) == 1


def test_final_entry_guard_rejects_late_metadata_loss_no_orders(config):
    client, snapshot = sample(config)
    current = state(config)
    dd.auto_entry_gate(client, config, current, snapshot)
    assert dd.require_entry_period(config, current)
    dd.period_path(config).unlink()
    bot.open_position(client, config, {}, current, Mock(symbol='BNBUSDT'))
    client.new_market_order.assert_not_called()


def test_entry_guard_precedes_all_entry_side_effects(config):
    client = Mock()
    bot.open_position(client, config, {}, state(config), Mock(symbol='BNBUSDT'))
    assert not client.mock_calls


def test_manual_endpoint_cannot_initialize_first_period(config, dashboard):
    response = post(dashboard, '/api/strategy-dd/prepare', {'initial_capital': '100', 'acknowledge': True})
    assert response.status_code == 409
    assert 'otomatis' in response.get_json()['error']
    assert not dd.period_path(config).exists()


def test_auto_failure_does_not_set_risk_stop(config):
    current = state(config)
    current['dd_stopped'] = False
    client, snapshot = sample(config)
    client.get_open_orders.return_value = [{}]
    dd.auto_entry_gate(client, config, current, snapshot)
    assert current['dd_stopped'] is False
    assert current['max_dd_pct'] == 2


def test_loop_wiring_keeps_exit_before_gate_and_separate_risk_flag():
    source = inspect.getsource(bot.run)
    assert source.index('manage_exit(') < source.index('strategy_dd.auto_entry_gate(')
    assert source.index('maybe_force_close_at_risk_limit(') < source.index('strategy_dd.auto_entry_gate(')
    assert source.index('strategy_dd.auto_entry_gate(') < source.index('can_enter =')
    tree = ast.parse(source)
    can_enter = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'can_enter' for t in n.targets))
    assert 'strategy_period_ready' in ast.unparse(can_enter.value)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'entries_paused' for t in node.targets):
            assert 'strategy_period' not in ast.unparse(node.value)


def test_waiting_reason_visible_without_creating_period(config):
    client, snapshot = sample(config)
    current = state(config)
    client.get_open_orders.return_value = [{}]
    dd.auto_entry_gate(client, config, current, snapshot)
    Path(config['STATE_FILE']).write_text(json.dumps(current))
    view = dd.strategy_view(config)
    assert view['status'] == 'UNINITIALIZED'
    assert 'order terbuka' in view['initialization_error']
    assert view['result'] is None
    assert view['initialization_mode'] == 'AUTO'
    Path(config['STATE_FILE']).write_text('{broken')
    assert dd.strategy_view(config)['status'] == 'INCOMPLETE'


def test_ui_no_initial_button_and_reset_only_in_advanced():
    html = (Path(__file__).resolve().parents[1] / 'templates/dashboard.html').read_text()
    card_start = html.index('<div class="card" id="cardStrategyDd">')
    card_end = html.index('<div class="grid cols"', card_start)
    assert '<button' not in html[card_start:card_end]
    control_start = html.index('<div class="tabpane" id="paneKontrol"')
    control_end = html.index('<div class="tabpane" id="paneBacktest"')
    assert 'id="strategyAdvanced"' in html[control_start:control_end]
    assert 'id="strategyPeriodBtn"' in html[control_start:control_end]
    assert html.count('id="strategyPeriodBtn"') == 1
    assert 'Tetapkan modal awal' not in html
    assert 'strategyAutoNote' not in html
    assert 'PnL bersih terverifikasi, termasuk fee.' not in html
    assert 'id="strategyDdMeta"' in html[card_start:card_end]
    assert 'id="strategyDdPeriod"' in html[card_start:card_end]


def test_snapshot_before_risk_forced_close_not_used_for_initial_capital(config):
    source = ast.parse(inspect.getsource(bot.run))
    assignment = next(node for node in ast.walk(source) if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'strategy_initial_snapshot' for t in node.targets))
    selector = compile(ast.Expression(assignment.value), '<snapshot-selector>', 'eval')
    current = state(config)
    current.update(current_symbol='BNBUSDT', qty=1)
    client, snapshot = sample(config)
    initial_snapshot = eval(selector, {'state': current, 'equity_snapshot': snapshot})
    assert initial_snapshot is None
    current.update(current_symbol=None, qty=0)
    assert not dd.auto_entry_gate(client, config, current, initial_snapshot)['ready']
    client, fresh = sample(config, '8')
    initial_snapshot = eval(selector, {'state': current, 'equity_snapshot': fresh})
    assert dd.auto_entry_gate(client, config, current, initial_snapshot)['initial_capital'] == '8.0'


def test_quote_snapshot_rechecked_after_slow_ledger_read(config, clock, monkeypatch):
    client, snapshot = sample(config)
    reader = dd.read_ledger
    def slow(*args, **kwargs):
        result = reader(*args, **kwargs)
        clock[0] += 31
        return result
    monkeypatch.setattr(dd, 'read_ledger', slow)
    assert not dd.auto_entry_gate(client, config, state(config), snapshot)['ready']
    assert not dd.period_path(config).exists()


def test_archive_evidence_prevents_new_automatic_period(config):
    history = Path(str(dd.period_path(config)) + '.history')
    history.mkdir()
    (history / 'old.json').write_text('{}')
    client, snapshot = sample(config)
    assert not dd.auto_entry_gate(client, config, state(config), snapshot)['ready']
    assert not dd.period_path(config).exists()


def test_zero_free_quote_cannot_initialize_even_with_positive_bnb_equity(config):
    client, snapshot = sample(config, '0')
    assert snapshot['status'] == 'VALID'
    assert snapshot['equity'] > 0
    assert not dd.auto_entry_gate(client, config, state(config), snapshot)['ready']


def test_reset_after_automatic_initialization_keeps_state_marker_and_risk(config, dashboard):
    current = state(config)
    client, snapshot = sample(config)
    original = dd.auto_entry_gate(client, config, current, snapshot)
    Path(config['STATE_FILE']).write_text(json.dumps(current))
    before = Path(config['STATE_FILE']).read_bytes()
    response = post(dashboard, '/api/strategy-dd/prepare', {'initial_capital': '5', 'acknowledge': True})
    assert response.status_code == 200
    response = post(dashboard, '/api/strategy-dd/execute', {'confirmation_id': response.get_json()['confirmation_id'], 'confirmed': True})
    assert response.status_code == 200
    assert Path(config['STATE_FILE']).read_bytes() == before
    result = dd.auto_entry_gate(client, config, current, snapshot)
    assert result['ready'] and not result['created']
    assert result['period_id'] != original['period_id']
    assert result['initial_capital'] == '5'
    assert current['dd_stopped'] is True


@pytest.mark.parametrize('marker', [0, 'false', None])
def test_invalid_initialization_marker_rejected(config, marker):
    current = state(config)
    current['strategy_dd_initialized'] = marker
    client, snapshot = sample(config)
    assert not dd.auto_entry_gate(client, config, current, snapshot)['ready']
    assert not dd.period_path(config).exists()


def test_receipt_boolean_version_rejected(config):
    client, snapshot = sample(config)
    current = state(config)
    dd.auto_entry_gate(client, config, current, snapshot)
    receipt = dd.read_receipt(config)
    receipt['version'] = True
    dd.receipt_path(config).write_text(json.dumps(receipt))
    assert not dd.auto_entry_gate(client, config, current, snapshot)['ready']
