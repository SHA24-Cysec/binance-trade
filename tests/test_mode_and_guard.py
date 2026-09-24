"""
Tes pengaman mode dan guard endpoint bertanda tangan:
- MODE tidak valid -> bot berhenti (InvalidModeError), tidak jatuh ke LIVE.
- Guard PAPER menolak endpoint bertanda tangan walau API key terisi.
"""

from __future__ import annotations

import copy

import pytest

import config
from binance_client import BinanceSpotClient, SignedEndpointBlockedError


# ------------------------------------------------------------------
# MODE tidak valid
# ------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", "  ", "TEST", "testnet", "LIV", None, "PAPERR"])
def test_invalid_mode_raises(bad):
    cfg = dict(config.PUMP_CONFIG)
    cfg["MODE"] = bad
    with pytest.raises(config.InvalidModeError):
        config.require_valid_mode(cfg)


def test_get_mode_defaults_to_paper_not_live():
    cfg = dict(config.PUMP_CONFIG)
    cfg["MODE"] = "sesuatu-yang-typo"
    # get_mode 'memaafkan' tetapi TIDAK pernah ke LIVE -> jatuh ke PAPER.
    assert config.get_mode(cfg) == "PAPER"


def test_valid_modes_only_paper_live():
    assert config.VALID_MODES == ("PAPER", "LIVE")


def test_create_exchange_client_rejects_invalid_mode():
    from exchange_client import create_exchange_client
    cfg = dict(config.PUMP_CONFIG)
    cfg["MODE"] = "FOO"
    with pytest.raises(config.InvalidModeError):
        create_exchange_client(cfg)


# ------------------------------------------------------------------
# Guard endpoint bertanda tangan
# ------------------------------------------------------------------
def test_keyless_client_blocks_signed_even_with_key():
    """Klien allow_signed=False menolak request signed meski API key diisi."""
    # API key sengaja diisi untuk membuktikan guard tidak bergantung pada
    # kosong/tidaknya key.
    c = BinanceSpotClient("APIKEYSENGAJADIISI", "SECRET", "https://api.binance.com",
                          allow_signed=False)
    with pytest.raises(SignedEndpointBlockedError):
        c.get_account()
    # Header API key TIDAK dipasang pada klien keyless.
    assert "X-MBX-APIKEY" not in c.session.headers


def test_paper_client_dust_endpoints_blocked(tmp_path, monkeypatch):
    """PaperClient.get_dust_convertible/convert_dust melempar guard."""
    from paper_client import PaperClient
    cfg = dict(config.PUMP_CONFIG)
    cfg["MODE"] = "PAPER"
    cfg["USE_WEBSOCKET"] = False  # hindari koneksi jaringan saat init
    cfg["PAPER_ACCOUNT_STATE_FILE"] = str(tmp_path / "acct.json")
    cfg["PAPER_INITIAL_BALANCES"] = {"USDT": 100.0}
    client = PaperClient(cfg)
    try:
        with pytest.raises(SignedEndpointBlockedError):
            client.get_dust_convertible()
        with pytest.raises(SignedEndpointBlockedError):
            client.convert_dust(["PEPE"])
    finally:
        client.close()


def test_paper_client_market_data_rest_is_keyless(tmp_path):
    """MarketDataProvider di PAPER memakai REST allow_signed=False."""
    from paper_client import PaperClient
    cfg = dict(config.PUMP_CONFIG)
    cfg["MODE"] = "PAPER"
    cfg["USE_WEBSOCKET"] = False
    cfg["PAPER_ACCOUNT_STATE_FILE"] = str(tmp_path / "acct.json")
    client = PaperClient(cfg)
    try:
        assert client.market.rest.allow_signed is False
        # get_account mengembalikan saldo virtual, bukan memanggil endpoint asli.
        acct = client.get_account()
        assert acct["accountType"] == "SPOT"
    finally:
        client.close()
