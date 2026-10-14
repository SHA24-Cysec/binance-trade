from __future__ import annotations

import hashlib
import json
import time
import uuid
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

from config.config import get_mode
from infrastructure.storage.atomic_io import atomic_write_json, interprocess_lock
from trading.equity import equity_context, valid_snapshot


class DrawdownError(ValueError):
    pass


def amount(value, *, positive=False) -> Decimal:
    if isinstance(value, bool) or value is None or len(str(value)) > 80:
        raise DrawdownError("Nominal tidak valid.")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise DrawdownError("Nominal bukan angka.") from exc
    if not number.is_finite() or number.copy_abs() > Decimal("1e15"):
        raise DrawdownError("Nominal harus finite dan tidak melebihi 1e15.")
    if positive and not Decimal("0.00000001") <= number:
        raise DrawdownError("Modal awal minimal 0.00000001 unit quote.")
    return number


def period_path(config):
    if not config.get("STATE_FILE"):
        raise DrawdownError("STATE_FILE tidak tersedia.")
    return Path(str(config["STATE_FILE"]) + ".strategy-dd.json")


def ledger_path(config):
    return Path(config.get("TRADE_LEDGER_FILE") or f"{config.get('STATE_FILE', 'data/pump_bot_state.json')}.trades.jsonl")


def context(config):
    return hashlib.sha256((equity_context(config) + "|" + str(ledger_path(config).resolve())).encode()).hexdigest()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def anchor(raw):
    return {"size": len(raw), "sha256": digest(raw)}


def check_anchor(raw, saved):
    if not isinstance(saved, dict) or type(saved.get("size")) is not int or saved["size"] < 0:
        raise DrawdownError("Anchor ledger tidak valid.")
    size = saved["size"]
    if len(raw) < size or digest(raw[:size]) != saved.get("sha256"):
        raise DrawdownError("Ledger terpotong atau isi lama berubah. Pulihkan ledger, jangan anggap hasil nol.")


def strict_json(raw):
    def constant(value):
        raise DrawdownError("JSON memuat angka non-finite.")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DrawdownError("JSON memuat field duplikat.")
            result[key] = value
        return result
    try:
        return json.loads(raw, parse_constant=constant, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DrawdownError("Data JSON rusak atau tidak valid.") from exc


def read_state(config):
    try:
        state = strict_json(Path(config["STATE_FILE"]).read_bytes())
    except OSError as exc:
        raise DrawdownError("State bot hilang atau tidak dapat dibaca.") from exc
    return validate_state(state)


def validate_state(state):
    if not isinstance(state, dict) or "current_symbol" not in state or "qty" not in state:
        raise DrawdownError("State bot belum memiliki identitas posisi yang valid.")
    symbol = state["current_symbol"]
    if symbol is not None and (not isinstance(symbol, str) or not symbol.strip()):
        raise DrawdownError("Simbol posisi dalam state tidak valid.")
    qty = amount(state["qty"])
    if qty < 0 or (bool(state["current_symbol"]) != (qty > 0)):
        raise DrawdownError("Posisi dalam state tidak konsisten.")
    if "strategy_dd_initialized" in state and type(state["strategy_dd_initialized"]) is not bool:
        raise DrawdownError("Penanda inisialisasi strategi pada state tidak valid.")
    if state.get("state_integrity_error"):
        raise DrawdownError("Integritas state bot belum terverifikasi.")
    return state


def assert_flat(state):
    if state.get("current_symbol") or amount(state.get("qty")) != 0:
        raise DrawdownError("Inisialisasi hanya saat bot tidak memiliki posisi.")
    for key in ("pending_order", "pending_native_settlement", "native_oco", "native_stop", "trade_accounting", "accounting_pending_trades", "accounting_blocked", "reconciliation_required"):
        if state.get(key):
            raise DrawdownError("Masih ada posisi/order/accounting/rekonsiliasi tertunda: " + key)


def parse_ledger(raw):
    if raw and not raw.endswith(b"\n"):
        raise DrawdownError("Baris akhir ledger belum lengkap.")
    latest = {}
    revisions = {}
    now_ms = int(time.time() * 1000)
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            raise DrawdownError(f"Baris ledger kosong: {line_number}.")
        item = strict_json(line)
        if not isinstance(item, dict) or not isinstance(item.get("trade_id"), str) or not item["trade_id"].strip():
            raise DrawdownError(f"Identitas trade tidak valid pada baris {line_number}.")
        revision = item.get("revision")
        if type(revision) is not int or revision < 1:
            raise DrawdownError("Revisi trade tidak valid.")
        for key in ("opened_at_ms", "closed_at_ms"):
            value = item.get(key)
            if type(value) is not int or not 0 < value <= now_ms + 5000:
                raise DrawdownError("Waktu trade tidak valid.")
        if item["closed_at_ms"] < item["opened_at_ms"]:
            raise DrawdownError("Waktu penutupan mendahului entry.")
        if type(item.get("paper")) is not bool or not isinstance(item.get("quote_asset"), str):
            raise DrawdownError("Mode atau quote trade tidak valid.")
        identity = (item["trade_id"], revision)
        if identity in revisions and revisions[identity] != item:
            raise DrawdownError("Revisi trade yang sama mempunyai isi berbeda.")
        revisions[identity] = item
        old = latest.get(item["trade_id"])
        if old is None or revision > old["revision"]:
            latest[item["trade_id"]] = item
    return latest


def read_ledger(config, *, allow_missing=False):
    try:
        raw = ledger_path(config).read_bytes()
    except FileNotFoundError as exc:
        if allow_missing:
            return b"", {}
        raise DrawdownError("Ledger hilang. Nilai nol tidak dapat disimpulkan.") from exc
    except OSError as exc:
        raise DrawdownError("Ledger tidak dapat dibaca.") from exc
    return raw, parse_ledger(raw)


def load_period(config):
    try:
        period = strict_json(period_path(config).read_bytes())
    except FileNotFoundError:
        if read_receipt(config) is not None:
            raise DrawdownError("Metadata periode hilang setelah inisialisasi. Pulihkan backup, bukan membuat periode otomatis baru.")
        history = Path(str(period_path(config)) + ".history")
        if history.exists() and any(history.iterdir()):
            raise DrawdownError("Metadata periode hilang tetapi arsip lama masih ada. Pulihkan periode yang benar.")
        return None
    except OSError as exc:
        raise DrawdownError("Periode strategi tidak dapat dibaca.") from exc
    if not isinstance(period, dict) or type(period.get("version")) is not int or period["version"] != 1:
        raise DrawdownError("Format periode strategi tidak valid.")
    if period.get("context") != context(config):
        raise DrawdownError("Periode strategi milik konteks akun/mode/quote/ledger yang berbeda.")
    if not isinstance(period.get("period_id"), str) or len(period["period_id"]) != 32 or any(c not in "0123456789abcdef" for c in period["period_id"]):
        raise DrawdownError("ID periode tidak valid.")
    started = period.get("started_at_ms")
    if type(started) is not int or not 0 < started <= int(time.time() * 1000) + 5000:
        raise DrawdownError("Waktu awal periode tidak valid.")
    amount(period.get("initial_capital"), positive=True)
    if not isinstance(period.get("baseline_ids"), list) or any(not isinstance(x, str) for x in period["baseline_ids"]):
        raise DrawdownError("Daftar trade awal periode tidak valid.")
    for key in ("baseline", "observed"):
        saved = period.get(key)
        if not isinstance(saved, dict) or type(saved.get("size")) is not int or saved["size"] < 0 or not isinstance(saved.get("sha256"), str) or len(saved["sha256"]) != 64:
            raise DrawdownError("Anchor periode rusak.")
    receipt = read_receipt(config)
    if receipt is not None and receipt != period_receipt(period):
        raise DrawdownError("Receipt dan metadata periode tidak cocok. Pulihkan pasangan backup yang konsisten.")
    return period


def ensure_closed_record(state, records):
    closed = state.get("last_closed_trade")
    if closed:
        if not isinstance(closed, dict) or not closed.get("trade_id") or closed["trade_id"] not in records:
            raise DrawdownError("Trade terakhir pada state tidak ditemukan dalam ledger.")


def initialization_fingerprint(config, state, raw, period):
    payload = {"context": context(config), "state": state, "ledger": anchor(raw), "period_id": (period or {}).get("period_id")}
    return digest(json.dumps(payload, sort_keys=True, allow_nan=False).encode())


def inspect_initialization(config):
    with interprocess_lock(period_path(config)):
        with interprocess_lock(ledger_path(config)):
            state = read_state(config)
            assert_flat(state)
            period = load_period(config)
            raw, records = read_ledger(config, allow_missing=period is None and not state.get("last_closed_trade"))
            ensure_closed_record(state, records)
            if period:
                check_anchor(raw, period["baseline"])
                check_anchor(raw, period["observed"])
                calculate(period, records, config, state)
            return {"fingerprint": initialization_fingerprint(config, state, raw, period), "period_id": (period or {}).get("period_id"), "context": context(config)}


def initial_result(capital):
    return {"capital": float(capital), "initial_capital": float(capital), "net_pnl": 0.0, "peak_capital": float(capital), "current_dd_pct": 0.0, "max_dd_pct": 0.0, "closed_trade_count": 0, "revised_trade_count": 0, "calculated_at_ms": int(time.time() * 1000)}


def initialize(config, initial_capital, expected_fingerprint):
    capital = amount(initial_capital, positive=True)
    with interprocess_lock(period_path(config)):
        with interprocess_lock(ledger_path(config)):
            state = read_state(config)
            assert_flat(state)
            previous = load_period(config)
            raw, records = read_ledger(config, allow_missing=previous is None and not state.get("last_closed_trade"))
            ensure_closed_record(state, records)
            if previous:
                check_anchor(raw, previous["baseline"])
                check_anchor(raw, previous["observed"])
                finalized_previous = calculate(previous, records, config, state)
            if initialization_fingerprint(config, state, raw, previous) != expected_fingerprint:
                raise DrawdownError("State, ledger, akun, atau periode berubah sejak konfirmasi. Ulangi pemeriksaan.")
            if not ledger_path(config).exists():
                ledger_path(config).parent.mkdir(parents=True, exist_ok=True)
                with ledger_path(config).open("xb"):
                    pass
            period = {
                "version": 1,
                "initialization_source": "MANUAL_RESET",
                "context": context(config),
                "period_id": uuid.uuid4().hex,
                "started_at_ms": int(time.time() * 1000),
                "initial_capital": str(capital),
                "baseline": anchor(raw),
                "observed": anchor(raw),
                "baseline_ids": sorted(records),
                "last_complete": initial_result(capital),
                "last_calculation_digest": digest(raw),
            }
            if previous:
                previous = dict(previous)
                previous["last_complete"] = finalized_previous
                previous["observed"] = anchor(raw)
                previous["last_calculation_digest"] = digest(raw)
                archive = Path(str(period_path(config)) + ".history") / (previous["period_id"] + ".json")
                atomic_write_json(archive, previous, mode=0o600)
            commit_period(config, period)
            return period


def calculate(period, records, config, state):
    if state.get("accounting_pending_trades") or state.get("accounting_blocked") or state.get("pending_native_settlement"):
        raise DrawdownError("Menunggu rekonsiliasi fill, fee, atau penyimpanan transaksi.")
    ensure_closed_record(state, records)
    excluded = set(period["baseline_ids"])
    included = []
    for identity, item in records.items():
        if identity in excluded:
            continue
        if item["paper"] != (get_mode(config) == "PAPER") or item["quote_asset"].upper() != str(config.get("QUOTE_ASSET", "")).upper():
            raise DrawdownError("Ledger periode memuat trade dari mode atau quote berbeda.")
        if item["opened_at_ms"] < period["started_at_ms"]:
            raise DrawdownError("Trade baru dalam ledger berasal dari sebelum periode dimulai.")
        if item.get("accounting_status") != "VERIFIED" or item.get("fee_conversion_pending") or item.get("fill_details_pending"):
            raise DrawdownError("Ada transaksi selesai dengan PnL/fill/fee belum terverifikasi.")
        amount(item.get("net_pnl_quote"))
        included.append(item)
    included.sort(key=lambda item: (item["closed_at_ms"], item["opened_at_ms"], item["trade_id"]))
    with localcontext() as ctx:
        ctx.prec = 40
        initial = amount(period["initial_capital"], positive=True)
        capital = initial
        peak = initial
        maximum = Decimal(0)
        current = Decimal(0)
        for item in included:
            capital += amount(item["net_pnl_quote"])
            peak = max(peak, capital)
            current = max(Decimal(0), (peak - capital) / peak * 100)
            maximum = max(maximum, current)
        result = {"capital": float(capital), "initial_capital": float(initial), "net_pnl": float(capital - initial), "peak_capital": float(peak), "current_dd_pct": float(current), "max_dd_pct": float(maximum), "closed_trade_count": len(included), "revised_trade_count": sum(item["revision"] > 1 for item in included), "calculated_at_ms": int(time.time() * 1000)}
    return result


def _checked_previous(period):
    result = period.get("last_complete")
    if not isinstance(result, dict):
        return None
    try:
        for key in ("capital", "initial_capital", "net_pnl", "peak_capital", "current_dd_pct", "max_dd_pct"):
            value = result.get(key)
            if type(value) not in (int, float):
                return None
            number = Decimal(str(value))
            if not number.is_finite() or number.copy_abs() > Decimal("1e300"):
                return None
        if type(result.get("closed_trade_count")) is not int or result["closed_trade_count"] < 0:
            return None
        if result["initial_capital"] != float(amount(period["initial_capital"], positive=True)) or result["peak_capital"] <= 0 or result["current_dd_pct"] < 0 or result["max_dd_pct"] < result["current_dd_pct"]:
            return None
        if type(result.get("calculated_at_ms")) is not int or result["calculated_at_ms"] <= 0:
            return None
    except (DrawdownError, TypeError):
        return None
    return result


def strategy_view(config):
    view = {"status": "UNINITIALIZED", "error": None, "period_id": None, "started_at_ms": None, "result": None, "risk_control": False, "checked_at_ms": int(time.time() * 1000), "open_position_excluded": True, "initialization_mode": "AUTO", "initialization_error": None, "initialization_source": None}
    period = None
    try:
        with interprocess_lock(period_path(config)):
            period = load_period(config)
            if period is None:
                try:
                    state = read_state(config)
                except DrawdownError:
                    if Path(config["STATE_FILE"]).exists():
                        raise
                    view["initialization_error"] = "State awal dan saldo akan diperiksa saat bot berjalan."
                else:
                    if state.get("strategy_dd_initialized"):
                        raise DrawdownError("Penanda state menunjukkan periode pernah dibuat, tetapi metadata hilang.")
                    pending = state.get("strategy_dd_initialization")
                    if isinstance(pending, dict) and isinstance(pending.get("reason"), str):
                        view["initialization_error"] = pending["reason"]
                return view
            view["initialization_source"] = period.get("initialization_source", "LEGACY_MANUAL")
            view.update(period_id=period["period_id"], started_at_ms=period["started_at_ms"], result=_checked_previous(period))
            with interprocess_lock(ledger_path(config)):
                raw, records = read_ledger(config)
                check_anchor(raw, period["baseline"])
                check_anchor(raw, period["observed"])
                state = read_state(config)
                result = None
                error = None
                try:
                    result = calculate(period, records, config, state)
                except DrawdownError as exc:
                    error = str(exc)
                updated = dict(period)
                updated["observed"] = anchor(raw)
                if result is not None:
                    if updated.get("last_calculation_digest") == digest(raw) and _checked_previous(period):
                        result["calculated_at_ms"] = period["last_complete"]["calculated_at_ms"]
                    updated["last_complete"] = result
                    updated["last_calculation_digest"] = digest(raw)
                if updated != period:
                    atomic_write_json(period_path(config), updated, mode=0o600)
                if error:
                    raise DrawdownError(error)
                view.update(status="VALID", result=result, open_position=bool(state.get("current_symbol")))
    except (DrawdownError, OSError, ValueError, TypeError, OverflowError) as exc:
        view.update(status="INCOMPLETE", error=str(exc) if isinstance(exc, DrawdownError) else "Data strategi tidak dapat diperiksa: " + type(exc).__name__)
    return view


def receipt_path(config):
    return Path(str(period_path(config)) + ".receipt.json")


def period_receipt(period):
    return {key: period[key] for key in ("version", "context", "period_id", "started_at_ms", "initial_capital")}


def read_receipt(config):
    try:
        receipt = strict_json(receipt_path(config).read_bytes())
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DrawdownError("Receipt inisialisasi tidak dapat dibaca.") from exc
    if not isinstance(receipt, dict) or set(receipt) != {"version", "context", "period_id", "started_at_ms", "initial_capital"}:
        raise DrawdownError("Receipt inisialisasi rusak.")
    if type(receipt["version"]) is not int or receipt["version"] != 1 or receipt["context"] != context(config):
        raise DrawdownError("Receipt inisialisasi milik konteks berbeda atau versinya tidak valid.")
    if not isinstance(receipt["period_id"], str) or len(receipt["period_id"]) != 32 or any(c not in "0123456789abcdef" for c in receipt["period_id"]):
        raise DrawdownError("Identitas receipt inisialisasi rusak.")
    if type(receipt["started_at_ms"]) is not int or not 0 < receipt["started_at_ms"] <= int(time.time() * 1000) + 5000:
        raise DrawdownError("Waktu receipt inisialisasi tidak valid.")
    amount(receipt["initial_capital"], positive=True)
    return receipt


def commit_period(config, period):
    atomic_write_json(receipt_path(config), period_receipt(period), mode=0o600)
    atomic_write_json(period_path(config), period, mode=0o600)


def verify_existing_period(config, period, state=None):
    raw, records = read_ledger(config)
    check_anchor(raw, period["baseline"])
    check_anchor(raw, period["observed"])
    if state is not None:
        ensure_closed_record(state, records)
    if read_receipt(config) is None:
        atomic_write_json(receipt_path(config), period_receipt(period), mode=0o600)
    if period["observed"] != anchor(raw):
        period = dict(period)
        period["observed"] = anchor(raw)
        atomic_write_json(period_path(config), period, mode=0o600)
    return period


def initialization_orders(client, config):
    if get_mode(config) == "PAPER":
        orders = client.store.get_open_orders()
    else:
        orders = client.get_open_orders()
    if orders != []:
        raise DrawdownError("Masih ada order terbuka atau respons order belum valid. Menunggu inisialisasi otomatis.")


def initialization_capital(config, snapshot):
    if not valid_snapshot(snapshot, config) or snapshot.get("status") != "VALID" or snapshot.get("source") != "BOT":
        raise DrawdownError("Saldo belum terverifikasi untuk modal awal otomatis.")
    age = int(time.time() * 1000) - snapshot["captured_at_ms"]
    if not 0 <= age <= 30000:
        raise DrawdownError("Snapshot modal awal kedaluwarsa. Menunggu snapshot bot baru.")
    return amount(snapshot.get("quote_free"), positive=True)


def ensure_automatic_period(client, config, state, snapshot):
    validate_state(state)
    with interprocess_lock(period_path(config)):
        with interprocess_lock(ledger_path(config)):
            existing = load_period(config)
            if existing is not None:
                existing = verify_existing_period(config, existing, state)
                state["strategy_dd_initialized"] = True
                return existing, False
            if state.get("strategy_dd_initialized"):
                raise DrawdownError("Periode pernah dibuat, tetapi metadata hilang. Entry baru menunggu pemulihan.")
            assert_flat(state)
    initialization_capital(config, snapshot)
    initialization_orders(client, config)
    capital = initialization_capital(config, snapshot)
    with interprocess_lock(period_path(config)):
        with interprocess_lock(ledger_path(config)):
            existing = load_period(config)
            if existing is not None:
                existing = verify_existing_period(config, existing, state)
                state["strategy_dd_initialized"] = True
                return existing, False
            if state.get("strategy_dd_initialized"):
                raise DrawdownError("Penanda inisialisasi berubah. Periksa metadata periode.")
            assert_flat(validate_state(state))
            raw, records = read_ledger(config, allow_missing=not state.get("last_closed_trade"))
            ensure_closed_record(state, records)
            for item in records.values():
                if item.get("accounting_status") != "VERIFIED" or item.get("fee_conversion_pending") or item.get("fill_details_pending"):
                    raise DrawdownError("Ledger lama masih memuat accounting belum terverifikasi. Rekonsiliasi sebelum inisialisasi otomatis.")
                amount(item.get("net_pnl_quote"))
                if item["paper"] != (get_mode(config) == "PAPER") or item["quote_asset"].upper() != str(config.get("QUOTE_ASSET", "")).upper():
                    raise DrawdownError("Ledger awal tidak sesuai mode atau quote.")
            if not ledger_path(config).exists():
                ledger_path(config).parent.mkdir(parents=True, exist_ok=True)
                with ledger_path(config).open("xb"):
                    pass
            capital = initialization_capital(config, snapshot)
            period = {
                "version": 1,
                "context": context(config),
                "period_id": uuid.uuid4().hex,
                "started_at_ms": int(time.time() * 1000),
                "initial_capital": str(capital),
                "initialization_source": "AUTO_FREE_QUOTE",
                "capital_snapshot_id": snapshot["snapshot_id"],
                "baseline": anchor(raw),
                "observed": anchor(raw),
                "baseline_ids": sorted(records),
                "last_complete": initial_result(capital),
                "last_calculation_digest": digest(raw),
            }
            commit_period(config, period)
            state["strategy_dd_initialized"] = True
            return period, True


def auto_entry_gate(client, config, state, snapshot):
    try:
        period, created = ensure_automatic_period(client, config, state, snapshot)
        result = {"ready": True, "created": created, "period_id": period["period_id"], "reason": "", "initial_capital": period["initial_capital"]}
    except Exception as exc:
        result = {"ready": False, "created": False, "period_id": None, "reason": str(exc) if isinstance(exc, DrawdownError) else "Inisialisasi/periode belum terverifikasi: " + type(exc).__name__}
    state["strategy_dd_initialization"] = {**result, "checked_at_ms": int(time.time() * 1000)}
    return result


def require_entry_period(config, state):
    with interprocess_lock(period_path(config)):
        with interprocess_lock(ledger_path(config)):
            period = load_period(config)
            if period is None or state.get("strategy_dd_initialized") is not True:
                raise DrawdownError("Entry menunggu inisialisasi periode strategi otomatis.")
            verify_existing_period(config, period, state)
            return period
