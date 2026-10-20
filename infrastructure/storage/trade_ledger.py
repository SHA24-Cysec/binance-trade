from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from infrastructure.storage.atomic_io import interprocess_lock

_INDEX_SUFFIX = ".revisions.log"
_INDEX_VERSION = 4
_FINGERPRINT_BYTES = 4096
_CACHE_LIMIT = 8
_COMPACT_SLACK = 200

_INDEX_CACHE: "dict[str, tuple[dict, dict, int]]" = {}


def _fingerprint(target: Path) -> "dict[str, Any] | None":
    try:
        size = target.stat().st_size
        with target.open("rb") as handle:
            head = handle.read(_FINGERPRINT_BYTES)
            if size > _FINGERPRINT_BYTES:
                handle.seek(max(_FINGERPRINT_BYTES, size - _FINGERPRINT_BYTES))
                tail = handle.read(_FINGERPRINT_BYTES)
            else:
                tail = head
    except OSError:
        return None
    return {
        "size": size,
        "head": hashlib.sha256(head).hexdigest(),
        "tail": hashlib.sha256(tail).hexdigest(),
    }


def _index_path(target: Path) -> Path:
    return target.with_name(f"{target.name}{_INDEX_SUFFIX}")


def _cache_get(target: Path, fingerprint: dict) -> "tuple[dict, int] | None":
    entry = _INDEX_CACHE.get(str(target))
    if entry is None:
        return None
    cached_fingerprint, revisions, lines = entry
    if cached_fingerprint == fingerprint:
        return revisions, lines
    return None


def _cache_put(target: Path, fingerprint: dict, revisions: dict, lines: int) -> None:
    key = str(target)
    _INDEX_CACHE.pop(key, None)
    _INDEX_CACHE[key] = (dict(fingerprint), revisions, lines)
    while len(_INDEX_CACHE) > _CACHE_LIMIT:
        _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))


def _cached_lines(target: Path) -> int:
    entry = _INDEX_CACHE.get(str(target))
    return entry[2] if entry else 0


def _scan_revisions(target: Path) -> dict[str, int]:
    revisions: dict[str, int] = {}
    try:
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                try:
                    old = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(old, dict):
                    continue
                trade_id = str(old.get("trade_id") or "").strip()
                if not trade_id:
                    continue
                try:
                    revision = int(old.get("revision", 1) or 1)
                except (TypeError, ValueError, OverflowError):
                    continue
                if revision > revisions.get(trade_id, 0):
                    revisions[trade_id] = revision
    except OSError:
        return {}
    return revisions


def _last_index_line(target: Path) -> "dict[str, Any] | None":
    index = _index_path(target)
    try:
        with index.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size <= 0:
                return None
            akhir = min(size, 8192)
            handle.seek(size - akhir)
            potongan = handle.read(akhir)
    except OSError:
        return None
    baris = [x for x in potongan.split(b"\n") if x.strip()]
    if not baris:
        return None
    try:
        entry = json.loads(baris[-1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return entry if isinstance(entry, dict) else None


def _entry_fingerprint(entry: "dict[str, Any] | None") -> "dict[str, Any] | None":
    if not isinstance(entry, dict):
        return None
    bagian = entry.get("fp")
    if not isinstance(bagian, dict):
        return None
    if type(bagian.get("size")) is not int:
        return None
    if not isinstance(bagian.get("head"), str) or not isinstance(bagian.get("tail"), str):
        return None
    return {"size": bagian["size"], "head": bagian["head"], "tail": bagian["tail"]}


def _read_index_log(target: Path) -> "tuple[dict[str, int], int] | None":
    revisions: dict[str, int] = {}
    lines = 0
    try:
        with _index_path(target).open("r", encoding="utf-8") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                lines += 1
                try:
                    entry = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    return None
                if not isinstance(entry, dict):
                    return None
                trade_id = entry.get("trade_id")
                revision = entry.get("revision")
                if not isinstance(trade_id, str) or not trade_id:
                    return None
                if type(revision) is not int or revision < 1:
                    return None
                if revision > revisions.get(trade_id, 0):
                    revisions[trade_id] = revision
    except OSError:
        return None
    return revisions, lines


def _tulis_baris(handle, fingerprint: dict, entries: "list[tuple[str, int]]") -> None:
    for trade_id, revision in entries:
        handle.write(
            json.dumps(
                {
                    "trade_id": trade_id,
                    "revision": revision,
                    "fp": fingerprint,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )


def _rewrite_index(target: Path, revisions: dict[str, int]) -> None:
    fingerprint = _fingerprint(target)
    if fingerprint is None:
        return
    index = _index_path(target)
    tmp = index.with_name(f"{index.name}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            _tulis_baris(handle, fingerprint, list(revisions.items()))
            handle.flush()
        os.replace(tmp, index)
    except OSError:
        return
    _cache_put(target, fingerprint, revisions, len(revisions))


def _append_index(
    target: Path,
    fingerprint: dict,
    revisions: dict,
    lines: int,
    trade_id: str,
    revision: int,
) -> None:
    if lines > 2 * len(revisions) + _COMPACT_SLACK:
        _rewrite_index(target, revisions)
        return
    try:
        with _index_path(target).open("a", encoding="utf-8", newline="") as handle:
            _tulis_baris(handle, fingerprint, [(trade_id, revision)])
            handle.flush()
    except OSError:
        return
    _cache_put(target, fingerprint, revisions, lines + 1)


def _load_revisions(target: Path) -> "dict[str, int] | None":
    fingerprint = _fingerprint(target)
    if fingerprint is None:
        return None
    cached = _cache_get(target, fingerprint)
    if cached is not None:
        return cached[0]
    terakhir = _last_index_line(target)
    if _entry_fingerprint(terakhir) != fingerprint:
        return None
    hasil = _read_index_log(target)
    if hasil is None:
        return None
    revisions, lines = hasil
    _cache_put(target, fingerprint, revisions, lines)
    return revisions


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
        revisions: dict[str, int] = {}
        lines_sebelum = 0
        if target.exists():
            loaded = _load_revisions(target)
            if loaded is not None:
                revisions = loaded
                lines_sebelum = _cached_lines(target)
            else:
                revisions = _scan_revisions(target)
                _rewrite_index(target, revisions)
                lines_sebelum = len(revisions)
            if revisions.get(trade_id, 0) >= revision:
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
        revisions[trade_id] = max(revision, revisions.get(trade_id, 0))
        fingerprint = _fingerprint(target)
        if fingerprint is not None:
            _append_index(
                target,
                fingerprint,
                revisions,
                lines_sebelum,
                trade_id,
                revisions[trade_id],
            )
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
