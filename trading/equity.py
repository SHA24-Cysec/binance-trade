from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
import uuid
from pathlib import Path

from config.config import get_base_url, get_mode
from infrastructure.storage.atomic_io import atomic_write_json
from trading.clients.binance_client import BinanceAPIError
from trading.clients.exchange_client import (
    ACCOUNT_SPOT_PERMISSION_SOURCE,
    ACCOUNT_SPOT_PERMISSION_VERIFIED,
    ExchangeClient,
)

logger = logging.getLogger("equity")


class EquityError(ValueError):
    pass


def _spot_api_key_permission_verified(account: dict) -> bool:
    return (
        account.get(ACCOUNT_SPOT_PERMISSION_VERIFIED) is True
        and str(account.get(ACCOUNT_SPOT_PERMISSION_SOURCE) or "")
        == "GET /sapi/v1/account/apiRestrictions"
    )


def _effective_account_permissions(account: dict) -> set[str]:
    permissions: set[str] = set()
    raw = account.get("permissions")
    if isinstance(raw, list):
        permissions.update(
            item.strip().upper()
            for item in raw
            if isinstance(item, str) and item.strip()
        )
    if (
        account.get("canTrade") is True
        and str(account.get("accountType") or "").upper() == "SPOT"
        and ("SPOT" in permissions or _spot_api_key_permission_verified(account))
    ):
        permissions.add("SPOT")
    return permissions


def _validate_live_account_snapshot(account: dict, quote_asset: str) -> None:
    if not isinstance(account, dict):
        raise BinanceAPIError(502, None, "respons account bukan object")
    if account.get("canTrade") is not True:
        raise BinanceAPIError(403, None, "akun Binance melaporkan canTrade bukan true")
    account_type = str(account.get("accountType") or "").upper()
    if account_type != "SPOT":
        raise BinanceAPIError(
            403, None, f"accountType bukan SPOT: {account_type or 'kosong'}"
        )
    permissions = account.get("permissions")
    api_key_spot_verified = _spot_api_key_permission_verified(account)
    if permissions is None:
        if not api_key_spot_verified:
            raise BinanceAPIError(
                403,
                None,
                "permission SPOT tidak ada pada account dan izin API key belum terverifikasi",
            )
    elif not isinstance(permissions, list):
        raise BinanceAPIError(502, None, "field permissions account bukan list")
    else:
        if any(not isinstance(item, str) or not item.strip() for item in permissions):
            raise BinanceAPIError(502, None, "field permissions account tidak valid")
        normalized_permissions = {item.strip().upper() for item in permissions}
        if "SPOT" not in normalized_permissions and not api_key_spot_verified:
            raise BinanceAPIError(
                403,
                None,
                "permission SPOT account/API key tidak dapat diverifikasi",
            )
    if "SPOT" not in _effective_account_permissions(account):
        raise BinanceAPIError(403, None, "izin efektif SPOT tidak dapat dibentuk")
    balances = account.get("balances")
    if not isinstance(balances, list):
        raise BinanceAPIError(502, None, "balances account tidak tersedia")
    seen: set[str] = set()
    for item in balances:
        if not isinstance(item, dict):
            raise BinanceAPIError(502, None, "entri balance bukan object")
        asset = str(item.get("asset") or "")
        if not asset or asset in seen:
            raise BinanceAPIError(
                502, None, f"asset balance kosong/duplikat: {asset!r}"
            )
        seen.add(asset)
        try:
            free = float(item.get("free"))
            locked = float(item.get("locked"))
        except (TypeError, ValueError):
            raise BinanceAPIError(502, None, f"balance {asset} bukan angka") from None
        if (
            not math.isfinite(free)
            or not math.isfinite(locked)
            or free < 0
            or locked < 0
        ):
            raise BinanceAPIError(502, None, f"balance {asset} tidak valid")
    if quote_asset not in seen:
        raise BinanceAPIError(
            502, None, f"balance aset kuotasi {quote_asset} tidak tersedia"
        )


def calculate_equity(
    client: ExchangeClient,
    config: dict,
    state: dict,
    position_price: float | None = None,
) -> dict:
    quote_asset = str(config.get("QUOTE_ASSET") or "").strip().upper()
    if not quote_asset:
        raise EquityError("aset kuotasi kosong")

    def unverified(reason: str):
        raise EquityError(reason)

    try:
        account = client.get_account()
        if str(config.get("MODE", "PAPER")).upper() == "LIVE":
            _validate_live_account_snapshot(account, quote_asset)
        elif isinstance(account, dict) and account.get("snapshot_error"):
            raise EquityError(
                f"snapshot akun PAPER tidak terbaca: {account['snapshot_error']}"
            )
    except Exception as exc:
        return unverified(
            f"snapshot account tidak tersedia/valid: {type(exc).__name__}"
            + (f" ({exc})" if str(exc) else "")
        )

    if not isinstance(account, dict):
        return unverified("respons account bukan object")
    raw_balances = account.get("balances")
    if not isinstance(raw_balances, list):
        return unverified("daftar balances tidak tersedia")
    if not raw_balances:
        return unverified("daftar balances kosong sehingga ekuitas tidak dapat dihitung")

    balances: dict[str, float] = {}
    assets = {}
    for item in raw_balances:
        if not isinstance(item, dict):
            return unverified("entri balance bukan object")
        asset = str(item.get("asset") or "").strip().upper()
        if not asset or asset in balances:
            return unverified(f"aset kosong atau duplikat: {asset or '<kosong>'}")
        try:
            free = float(item.get("free"))
            locked = float(item.get("locked"))
        except (TypeError, ValueError, OverflowError):
            return unverified(f"saldo {asset} bukan angka")
        if (
            not math.isfinite(free)
            or not math.isfinite(locked)
            or free < 0
            or locked < 0
        ):
            return unverified(f"saldo {asset} tidak valid")
        total = free + locked
        if not math.isfinite(total):
            return unverified(f"saldo total {asset} tidak finite")
        balances[asset] = total
        if total > 0 or asset == quote_asset:
            assets[asset] = {
                "asset": asset,
                "free": free,
                "locked": locked,
                "total": total,
                "price": 1.0 if asset == quote_asset else None,
                "value": total if asset == quote_asset else None,
                "price_time_ms": None,
                "price_source": "QUOTE" if asset == quote_asset else None,
            }

    equity_parts = [balances.get(quote_asset, 0.0)]
    active_symbol = str(state.get("current_symbol") or "").strip().upper()
    active_price = None
    if position_price is not None:
        try:
            candidate = float(position_price)
        except (TypeError, ValueError, OverflowError):
            candidate = 0.0
        if math.isfinite(candidate) and candidate > 0:
            active_price = candidate

    assets_to_price = []
    for asset, total in balances.items():
        if asset == quote_asset or total <= 0:
            continue
        symbol = f"{asset}{quote_asset}"
        if symbol == active_symbol and active_price is not None:
            equity_parts.append(total * active_price)
            assets[asset].update(price=active_price, value=total * active_price,
                                 price_source="ACTIVE_BID")
        else:
            assets_to_price.append((asset, symbol, total))

    if assets_to_price:
        try:
            tickers = client.get_ticker_24hr_all()
        except Exception as exc:
            return unverified(f"snapshot ticker tidak tersedia: {type(exc).__name__}")
        if not isinstance(tickers, list):
            return unverified("snapshot ticker bukan list")

        ticker_by_symbol = {}
        duplicate_symbols = set()
        for ticker in tickers:
            if not isinstance(ticker, dict):
                continue
            symbol = str(ticker.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            if symbol in ticker_by_symbol:
                duplicate_symbols.add(symbol)
            else:
                ticker_by_symbol[symbol] = ticker

        try:
            ticker_ttl = float(config.get("TICKER_SNAPSHOT_TTL_SECONDS", 30.0) or 0.0)
            max_market_age = float(
                config.get("MAX_MARKET_DATA_AGE_SECONDS", 10.0) or 0.0
            )
        except (TypeError, ValueError, OverflowError):
            return unverified("batas umur snapshot ticker bukan angka")
        if (
            not math.isfinite(ticker_ttl)
            or not math.isfinite(max_market_age)
            or ticker_ttl < 0
            or max_market_age < 0
        ):
            return unverified("batas umur snapshot ticker tidak valid")
        max_age_seconds = min(
            ticker_ttl + max_market_age, max(60.0, max_market_age)
        )
        max_age_ms = int(max_age_seconds * 1000.0)
        now_ms = int(time.time() * 1000.0)

        for asset, symbol, amount in assets_to_price:
            if symbol in duplicate_symbols:
                return unverified(f"ticker {symbol} duplikat")
            ticker = ticker_by_symbol.get(symbol)
            if ticker is None:
                return unverified(f"harga bid {symbol} tidak tersedia")
            try:
                bid = float(ticker.get("bidPrice"))
                close_time_ms = int(ticker.get("closeTime"))
            except (TypeError, ValueError, OverflowError):
                return unverified(f"bid/timestamp {symbol} bukan angka")
            if not math.isfinite(bid) or bid <= 0 or close_time_ms <= 0:
                return unverified(f"bid/timestamp {symbol} tidak valid")
            if abs(now_ms - close_time_ms) > max_age_ms:
                return unverified(f"snapshot {symbol} kedaluwarsa")
            value = amount * bid
            if not math.isfinite(value):
                return unverified(f"nilai saldo {asset} tidak finite")
            equity_parts.append(value)
            assets[asset].update(price=bid, value=value,
                                 price_time_ms=close_time_ms,
                                 price_source="TICKER_BID")

    try:
        equity = math.fsum(equity_parts)
    except OverflowError:
        return unverified("total ekuitas overflow")
    if not math.isfinite(equity) or equity < 0:
        return unverified("total ekuitas tidak valid")
    quote_balance = assets.get(quote_asset, {})
    return {
        "equity": equity,
        "quote_free": quote_balance.get("free", 0.0),
        "quote_locked": quote_balance.get("locked", 0.0),
        "other_assets_value": math.fsum(
            item["value"] for asset, item in assets.items() if asset != quote_asset
        ),
        "assets": list(assets.values()),
    }


class SpotEquityReader:
    def __init__(self, client):
        self.client = client

    def get_account(self):
        account = self.client.get_account()
        if not isinstance(account, dict):
            return account
        permissions = account.get("permissions")
        has_spot = isinstance(permissions, list) and "SPOT" in {
            item.strip().upper() for item in permissions if isinstance(item, str)
        }
        if has_spot or _spot_api_key_permission_verified(account):
            return account
        permission = self.client.get_api_key_permissions()
        expected = {
            "enableReading": True,
            "enableSpotAndMarginTrading": True,
            "enableWithdrawals": False,
            "ipRestrict": True,
        }
        if not isinstance(permission, dict) or any(
            permission.get(key) is not value for key, value in expected.items()
        ):
            raise EquityError("izin API key Spot tidak terverifikasi")
        verified = dict(account)
        verified[ACCOUNT_SPOT_PERMISSION_VERIFIED] = True
        verified[ACCOUNT_SPOT_PERMISSION_SOURCE] = "GET /sapi/v1/account/apiRestrictions"
        return verified

    def get_ticker_24hr_all(self):
        return self.client.get_ticker_24hr_all()


def equity_context(config: dict) -> str:
    mode = get_mode(config)
    identity = (
        str(config.get("API_KEY") or "")
        if mode == "LIVE"
        else str(Path(config.get("PAPER_ACCOUNT_STATE_FILE") or "data/pump_paper_account_paper.json").resolve())
    )
    fields = [mode, get_base_url(config), str(config.get("QUOTE_ASSET") or "").strip().upper(), identity]
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def collect_equity(client, config: dict, state: dict, position_price=None, source="BOT") -> dict:
    snapshot = {
        "version": 1,
        "snapshot_id": uuid.uuid4().hex,
        "context": equity_context(config),
        "mode": get_mode(config),
        "quote": str(config.get("QUOTE_ASSET") or "").strip().upper(),
        "source": source,
        "pid": os.getpid(),
        "captured_at_ms": int(time.time() * 1000),
        "status": "UNVERIFIED",
        "error": None,
        "equity": None,
        "quote_free": None,
        "quote_locked": None,
        "other_assets_value": None,
        "assets": [],
    }
    try:
        snapshot.update(calculate_equity(client, config, state, position_price))
        snapshot["status"] = "VALID"
    except (EquityError, ValueError, TypeError, OverflowError) as exc:
        snapshot["error"] = str(exc) if isinstance(exc, EquityError) else type(exc).__name__
        if source == "BOT":
            logger.warning(
                "Ekuitas seluruh akun tidak dapat diverifikasi (%s). Evaluasi batas risiko dilewati satu iterasi.",
                snapshot["error"],
            )
    snapshot["completed_at_ms"] = int(time.time() * 1000)
    return snapshot


def snapshot_path(config: dict) -> Path:
    state_file = config.get("STATE_FILE")
    if not state_file:
        raise ValueError("STATE_FILE diperlukan untuk snapshot equity")
    return Path(str(state_file) + ".equity.json")


def valid_snapshot(snapshot, config: dict) -> bool:
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("version") != 1 or snapshot.get("context") != equity_context(config):
        return False
    if snapshot.get("mode") != get_mode(config) or snapshot.get("quote") != str(config.get("QUOTE_ASSET") or "").strip().upper():
        return False
    if snapshot.get("source") not in ("BOT", "DASHBOARD"):
        return False
    if not isinstance(snapshot.get("snapshot_id"), str) or not snapshot["snapshot_id"]:
        return False
    for key in ("captured_at_ms", "completed_at_ms", "pid"):
        value = snapshot.get(key)
        if type(value) is not int or not 0 < value <= 253402300799999:
            return False
    if snapshot["completed_at_ms"] < snapshot["captured_at_ms"]:
        return False
    if snapshot.get("status") == "UNVERIFIED":
        return snapshot.get("equity") is None and isinstance(snapshot.get("error"), str)
    if snapshot.get("status") != "VALID" or not isinstance(snapshot.get("assets"), list):
        return False
    for key in ("equity", "quote_free", "quote_locked", "other_assets_value"):
        value = snapshot.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return False
    seen = set()
    for asset in snapshot["assets"]:
        if not isinstance(asset, dict) or not isinstance(asset.get("asset"), str):
            return False
        if not asset["asset"] or asset["asset"] in seen:
            return False
        seen.add(asset["asset"])
        for key in ("free", "locked", "total", "price", "value"):
            value = asset.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                return False
        if not math.isclose(asset["free"] + asset["locked"], asset["total"], abs_tol=1e-10):
            return False
        if not math.isclose(asset["total"] * asset["price"], asset["value"], abs_tol=1e-10):
            return False
    try:
        total = math.fsum(item["value"] for item in snapshot["assets"])
        parts = math.fsum(snapshot[key] for key in ("quote_free", "quote_locked", "other_assets_value"))
    except OverflowError:
        return False
    return math.isclose(total, snapshot["equity"], abs_tol=1e-10) and math.isclose(parts, total, abs_tol=1e-10)


def load_equity(config: dict) -> dict | None:
    try:
        with snapshot_path(config).open(encoding="utf-8") as handle:
            envelope = json.load(handle)
        if not isinstance(envelope, dict) or not valid_snapshot(envelope.get("latest"), config):
            return None
        previous = envelope.get("last_valid")
        if previous is not None and (not valid_snapshot(previous, config) or previous.get("status") != "VALID"):
            envelope["last_valid"] = None
        return envelope
    except (OSError, ValueError, TypeError, OverflowError):
        return None


def equity_envelope(latest: dict, previous: dict | None = None) -> dict:
    last_valid = latest if latest["status"] == "VALID" else (previous or {}).get("last_valid")
    return {"latest": latest, "last_valid": last_valid}


def publish_equity(config: dict, snapshot: dict) -> None:
    if not valid_snapshot(snapshot, config) or snapshot["source"] != "BOT":
        raise ValueError("snapshot bot tidak valid")
    previous = load_equity(config) if snapshot["status"] != "VALID" else None
    atomic_write_json(snapshot_path(config), equity_envelope(snapshot, previous), mode=0o600)


def equity_view(envelope: dict | None, config: dict, process: dict | None = None) -> dict:
    result = {
        "equity_live": None,
        "equity_display": None,
        "usdt_free": None,
        "quote_locked": None,
        "other_assets_value": None,
        "assets": [],
        "equity_status": "WAITING",
        "equity_source": "BOT" if process else "DASHBOARD",
        "equity_snapshot_id": None,
        "equity_updated_at_ms": None,
        "equity_age_seconds": None,
        "equity_error": "Menunggu snapshot equity yang valid.",
        "equity_attempt_id": None,
        "equity_attempt_at_ms": None,
        "risk_snapshot": None,
    }
    latest = (envelope or {}).get("latest")
    if not valid_snapshot(latest, config):
        return result
    if process:
        if latest["source"] != "BOT" or latest["pid"] != process.get("pid"):
            return result
        started_ms = float(process.get("started_at") or 0) * 1000
        if latest["captured_at_ms"] < started_ms:
            return result
    displayed = latest if latest["status"] == "VALID" else envelope.get("last_valid")
    result.update(
        equity_status="UNVERIFIED",
        equity_error=latest.get("error"),
        equity_source=latest["source"],
        equity_attempt_id=latest["snapshot_id"],
        equity_attempt_at_ms=latest["captured_at_ms"],
    )
    if not valid_snapshot(displayed, config) or displayed["status"] != "VALID":
        return result
    now_ms = int(time.time() * 1000)
    age = (now_ms - displayed["captured_at_ms"]) / 1000.0
    fresh = 0 <= age <= 30.0 and latest["status"] == "VALID"
    result.update(
        equity_live=displayed["equity"],
        equity_display=f'{displayed["equity"]:.2f}',
        usdt_free=displayed["quote_free"],
        quote_locked=displayed["quote_locked"],
        other_assets_value=displayed["other_assets_value"],
        assets=displayed["assets"],
        equity_status="VALID" if fresh else "STALE",
        equity_source=displayed["source"],
        equity_snapshot_id=displayed["snapshot_id"],
        equity_updated_at_ms=displayed["captured_at_ms"],
        equity_age_seconds=max(0.0, age),
    )
    risk = displayed.get("risk_snapshot")
    if isinstance(risk, dict) and all(key in risk for key in ("current_dd_pct", "max_dd_pct", "peak_equity")):
        numbers = [risk.get(key) for key in ("current_dd_pct", "max_dd_pct", "peak_equity")]
        if all(value is None or (type(value) in (int, float) and math.isfinite(value) and value >= 0) for value in numbers):
            result["risk_snapshot"] = {key: risk.get(key) for key in ("current_dd_pct", "max_dd_pct", "peak_equity")}
            resets = risk.get("reset_count")
            if type(resets) is int and resets >= 0:
                result["risk_snapshot"]["reset_count"] = resets
    if not fresh and not result["equity_error"]:
        result["equity_error"] = "Snapshot lebih dari 30 detik atau jam sistem berubah."
    return result


def account_risk_snapshot(equity, state):
    def number(value):
        try:
            value = float(value)
            return value if math.isfinite(value) and value >= 0 else None
        except (ValueError, TypeError, OverflowError):
            return None
    peak = number(state.get("peak_equity"))
    maximum = number(state.get("max_dd_pct")) if peak is not None else None
    current = None
    if equity is not None and peak and not state.get("state_integrity_error"):
        current = max(0.0, (peak - equity) / peak * 100.0)
    resets = state.get("dd_reset_count", 0)
    return {"current_dd_pct": current, "max_dd_pct": maximum, "peak_equity": peak, "reset_count": resets if type(resets) is int and resets >= 0 else 0}


def account_drawdown_view(state, account, process, config):
    historical = account_risk_snapshot(None, state)
    active = process.get("status") in ("STARTING", "RUNNING", "STOPPING")
    risk = account.get("risk_snapshot") if active else None
    result = dict(risk or historical)
    result.update(
        status=account.get("equity_status", "WAITING") if risk else ("WAITING" if active else "STOPPED"),
        snapshot_id=account.get("equity_snapshot_id") if risk else None,
        risk_control=True,
        stop_enabled=bool(config.get("USE_EQUITY_STOP")),
        limit_pct=config.get("MAX_DRAWDOWN_PERCENT"),
        reset_count=result.get("reset_count", 0),
    )
    return result
