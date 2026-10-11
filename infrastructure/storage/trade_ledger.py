from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from infrastructure.storage.atomic_io import interprocess_lock


def append_trade_record(path: os.PathLike | str, record: dict) -> bool:
    if not isinstance(record, dict):
        raise ValueError("trade record harus berupa object")
    trade_id = str(record.get("trade_id") or "").strip()
    if not trade_id:
        raise ValueError("trade_id wajib terisi")
    try:
        revision = int(record.get("revision", 1))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("revision trade tidak valid") from exc
    if revision < 1:
        raise ValueError("revision trade harus positif")
    payload = dict(record)
    payload["trade_id"] = trade_id
    payload["revision"] = revision
    line = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with interprocess_lock(target):
        if target.exists():
            with target.open("r", encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    try:
                        old = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(old, dict) or str(old.get("trade_id") or "") != trade_id:
                        continue
                    try:
                        old_revision = int(old.get("revision", 1) or 1)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if old_revision >= revision:
                        return False
        with target.open("a+", encoding="utf-8", newline="") as handle:
            if handle.tell() > 0:
                handle.seek(0, os.SEEK_END)
                handle.seek(handle.tell() - 1)
                last = handle.read(1)
                handle.seek(0, os.SEEK_END)
                if last != "\n":
                    handle.write("\n")
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return True


def load_trade_records(path: os.PathLike | str, *, limit: int = 1000) -> list[dict]:
    target = Path(path)
    try:
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    latest: dict[str, dict] = {}
    for raw in lines:
        try:
            record: Any = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(record, dict):
            continue
        trade_id = str(record.get("trade_id") or "").strip()
        if not trade_id:
            continue
        try:
            revision = int(record.get("revision", 1) or 1)
        except (TypeError, ValueError, OverflowError):
            continue
        old = latest.get(trade_id)
        if old is None or revision >= int(old.get("revision", 1) or 1):
            latest[trade_id] = record
    records = list(latest.values())
    records.sort(
        key=lambda item: (
            _safe_int(item.get("closed_at_ms")),
            _safe_int(item.get("opened_at_ms")),
            str(item.get("trade_id") or ""),
        ),
        reverse=True,
    )
    return records[: max(1, int(limit))]


def _safe_int(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if math.isfinite(number) else 0
