from __future__ import annotations

import hashlib
import json
import math
import re
import uuid

MAX_CLOSE_AGE_MS = 120_000


def position_id(state: dict) -> str | None:
    if not isinstance(state, dict) or state.get("state_integrity_error"):
        return None
    symbol = state.get("current_symbol")
    if not isinstance(symbol, str) or not symbol:
        return None
    try:
        qty = float(state.get("qty") or 0)
        entry = float(state.get("entry_price") or 0)
        timestamp = state.get("entry_time")
        if isinstance(timestamp, bool):
            return None
        opened_at = int(timestamp)
        if opened_at != float(timestamp):
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(qty) or not math.isfinite(entry) or qty <= 0 or entry <= 0 or opened_at <= 0:
        return None
    accounting = state.get("trade_accounting")
    trade_id = str(accounting.get("trade_id") or "") if isinstance(accounting, dict) else ""
    identity = [symbol, opened_at, trade_id or format(entry, ".17g")]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def valid_request_id(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) is not None


def new_close_command(state: dict, requested_at: int, request_id: str | None = None) -> dict:
    identity = position_id(state)
    if identity is None:
        raise ValueError("Identitas posisi belum terverifikasi. Periksa state posisi.")
    identifier = request_id if request_id is not None else uuid.uuid4().hex
    if not valid_request_id(identifier):
        raise ValueError("ID permintaan jual tidak valid.")
    return {
        "action": "CLOSE_POSITION",
        "symbol": state["current_symbol"],
        "position_id": identity,
        "request_id": identifier,
        "requested_at": int(requested_at),
    }


def close_command_matches(command: dict, state: dict, now_ms: int) -> bool:
    if not isinstance(command, dict) or not valid_request_id(command.get("request_id")):
        return False
    requested_at = command.get("requested_at")
    if isinstance(requested_at, bool) or not isinstance(requested_at, int):
        return False
    age = now_ms - requested_at
    identity = position_id(state)
    return bool(
        requested_at > 0
        and 0 <= age <= MAX_CLOSE_AGE_MS
        and identity is not None
        and command.get("action") == "CLOSE_POSITION"
        and command.get("symbol") == state.get("current_symbol")
        and command.get("position_id") == identity
        and requested_at >= int(state["entry_time"])
    )
