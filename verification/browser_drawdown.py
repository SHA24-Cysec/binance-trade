import argparse
import json
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright


def run(screenshots=None):
    root = Path(__file__).resolve().parents[1]
    if screenshots:
        screenshots = Path(screenshots)
        screenshots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1050}, timezone_id="Asia/Jakarta")
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.route("https://**/*", lambda route: route.abort())
        page.route("http://**/*", lambda route: route.abort())
        page.goto((root / 'docs/drawdown-preview.html').as_uri())
        assert page.locator('#ddCurrent').inner_text() == '0,01%'
        assert page.locator('#strategyDdCurrent').inner_text() == '0,00%'
        assert page.locator('#strategyAutoNote').count() == 0
        assert 'PnL bersih terverifikasi, termasuk fee.' not in page.locator('#cardStrategyDd').inner_text()
        assert page.locator('#strategyDdMeta').is_visible()
        assert page.locator('#strategyDdPeriod').is_visible()
        if screenshots:
            page.screenshot(path=str(screenshots / 'drawdown-desktop.png'), full_page=True)
        page.locator('[data-scene="loss"]').click()
        assert page.locator('#strategyDdCurrent').inner_text() == '1,00%'
        page.locator('[data-scene="recovered"]').click()
        assert page.locator('#strategyDdCurrent').inner_text() == '0,00%'
        assert page.locator('#strategyDdMax').inner_text() == '1,00%'
        page.locator('[data-scene="pending"]').click()
        assert page.locator('#strategyPeriodBtn').is_disabled()
        page.locator('[data-scene="init"]').click()
        assert page.locator('#strategyDdCurrent').inner_text() == '-'
        assert page.locator('#strategyPeriodBtn').is_disabled()
        assert page.locator('#cardStrategyDd button').count() == 0
        page.wait_for_function("document.getElementById('scenarioNote').textContent.includes('periode tersimpan otomatis')")
        assert page.locator('#strategyInitial').inner_text() == '100,00 USDT'
        assert page.locator('#strategyDdCurrent').inner_text() == '0,00%'
        page.locator('[data-scene="reset"]').click()
        page.locator('#strategyAdvanced summary').click()
        page.locator('#strategyPeriodBtn').click()
        page.locator('#strategyInitialInput').fill('75')
        page.locator('#strategyAcknowledge').check()
        page.locator('#strategyPrepare').click()
        page.locator('#strategyExecute').wait_for(state='visible')
        page.locator('#strategyExecute').click()
        page.wait_for_function("document.getElementById('strategyPeriodMessage').textContent.includes('Simulasi berhasil')")
        page.locator('#strategyCancel').click()
        assert page.locator('#strategyInitial').inner_text() == '75,00 USDT'
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('[data-scene="bnb"]').click()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        if screenshots:
            page.screenshot(path=str(screenshots / 'drawdown-mobile.png'), full_page=True)
        page.locator('[data-scene="reset"]').click()
        page.locator('#strategyPeriodBtn').click()
        assert page.locator('#strategyInitialInput').is_visible()
        page.keyboard.press('Escape')
        assert not page.locator('#strategyPeriodModal').is_visible()
        assert not errors, errors
        page.close()

        from web import dashboard as dash
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            config = dict(dash.PUMP_CONFIG)
            config.update(MODE='LIVE', STATE_FILE=str(Path(temporary) / 'state.json'), TRADE_LEDGER_FILE=str(Path(temporary) / 'trades.jsonl'), API_KEY='offline')
            state = {"current_symbol": None, "qty": 0, "peak_equity": 100.15, "max_dd_pct": 0.01}
            process = {"status": "RUNNING", "pid": 123, "uptime_seconds": 10}
            account = {"equity_status": "VALID", "equity_live": 100.14, "equity_display": "100.14", "usdt_free": 100, "quote_locked": 0, "other_assets_value": 0.14, "assets": [], "equity_source": "BOT", "equity_snapshot_id": "offline", "equity_age_seconds": 1, "risk_snapshot": {"current_dd_pct": 0.01, "max_dd_pct": 0.01, "peak_equity": 100.15}}
            stack.enter_context(patch.object(dash, 'PUMP_CONFIG', config))
            stack.enter_context(patch.object(dash, 'load_state', return_value=state))
            stack.enter_context(patch.object(dash._process_manager, 'status', return_value=process))
            stack.enter_context(patch.object(dash, 'get_account_equity', return_value=account))
            stack.enter_context(patch.object(dash, 'get_idr_rate', return_value={"enabled": False}))
            status = dash.build_status()
            with dash.app.test_request_context('/'):
                html = dash.render_template('dashboard.html', admin_token='offline')
            payload = {"status": status, "summary": dash.build_trade_summary([]), "trades": [], "events": [], "log_levels": {}}
            page = browser.new_page(viewport={"width": 1280, "height": 1050}, timezone_id="Asia/Jakarta")
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            def route(request):
                url = request.request.url
                if url == 'https://dashboard.invalid/':
                    request.fulfill(status=200, content_type='text/html', body=html)
                elif url.endswith('/api/all'):
                    request.fulfill(status=200, content_type='application/json', body=json.dumps(payload))
                elif '/api/control/status' in url:
                    request.fulfill(status=200, content_type='application/json', body=json.dumps({"process": process, "position": {"has_position": False}, "mode": "LIVE", "mode_guard": {"safe": True}, "config_errors": [], "public_egress_ip": {}}))
                else:
                    request.fulfill(status=200, content_type='application/json', body='{}')
            page.route('**/*', route)
            page.goto('https://dashboard.invalid/')
            page.wait_for_function("document.getElementById('ddCurrent').textContent === '0,01%'")
            assert page.locator('#strategyDdCurrent').inner_text() == '-'
            assert 'inisialisasi otomatis' in page.locator('#strategyDdMeta').inner_text()
            assert page.locator('#cardStrategyDd button').count() == 0
            assert page.locator('#strategyAutoNote').count() == 0
            assert 'PnL bersih terverifikasi, termasuk fee.' not in page.locator('#cardStrategyDd').inner_text()
            assert page.locator('#strategyDdMeta').is_visible()
            assert page.locator('#strategyDdPeriod').is_visible()
            assert page.locator('#paneKontrol #strategyAdvanced #strategyPeriodBtn').count() == 1
            assert page.locator('.account-summary > .card').count() == 4
            if screenshots:
                page.screenshot(path=str(screenshots / 'dashboard-desktop.png'), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.locator('#cardStrategyDd').scroll_into_view_if_needed()
            page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            assert page.locator('#cardStrategyDd').is_visible()
            if screenshots:
                page.screenshot(path=str(screenshots / 'dashboard-strategy-mobile.png'))
                page.screenshot(path=str(screenshots / 'dashboard-mobile.png'), full_page=True)
            assert not errors, errors
            page.close()
        browser.close()
    print('PASS: preview and full dashboard, desktop/mobile, six scenarios, automatic initialization, advanced reset, two-step modal, Escape, no page errors. All browser HTTP requests intercepted; no Binance calls.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--screenshots')
    args = parser.parse_args()
    run(args.screenshots)
