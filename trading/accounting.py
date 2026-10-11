from __future__ import annotations

import math
from typing import Any


class TradeAccountingError(ValueError):
    pass


def _finite_number(value: Any, label: str, *, allow_zero: bool = True) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TradeAccountingError(f"{label} bukan angka valid") from exc
    if not math.isfinite(number) or number < 0 or (not allow_zero and number == 0):
        raise TradeAccountingError(f"{label} di luar rentang valid")
    return number


def normalize_fills(
    raw_fills: Any,
    *,
    symbol: str,
    side: str,
    order_id: Any = None,
    fallback_time_ms: Any = None,
) -> list[dict]:
    if not isinstance(raw_fills, list):
        raise TradeAccountingError("daftar fill bukan list")
    normalized = []
    seen_ids: set[str] = set()
    try:
        default_time = max(0, int(fallback_time_ms or 0))
    except (TypeError, ValueError, OverflowError):
        default_time = 0
    for index, raw in enumerate(raw_fills):
        if not isinstance(raw, dict):
            raise TradeAccountingError(f"fill ke-{index + 1} bukan object")
        price = _finite_number(raw.get("price"), "harga fill", allow_zero=False)
        qty = _finite_number(raw.get("qty"), "qty fill", allow_zero=False)
        quote_qty_raw = raw.get("quoteQty", raw.get("quote_qty"))
        quote_qty = (
            price * qty
            if quote_qty_raw in (None, "")
            else _finite_number(quote_qty_raw, "quoteQty fill", allow_zero=False)
        )
        if not math.isclose(quote_qty, price * qty, rel_tol=1e-7, abs_tol=1e-8):
            raise TradeAccountingError("quoteQty fill tidak konsisten dengan harga x qty")
        commission = _finite_number(raw.get("commission", 0.0), "komisi fill")
        commission_asset = str(
            raw.get("commissionAsset", raw.get("commission_asset")) or ""
        ).strip().upper()
        if commission > 0 and not commission_asset:
            raise TradeAccountingError("aset komisi fill tidak tersedia")
        trade_id_value = raw.get("id", raw.get("tradeId", raw.get("trade_id")))
        trade_id = str(trade_id_value) if trade_id_value is not None else ""
        if trade_id:
            if trade_id in seen_ids:
                raise TradeAccountingError("trade ID duplikat pada order yang sama")
            seen_ids.add(trade_id)
        try:
            fill_time_ms = int(raw.get("time", raw.get("time_ms", default_time)) or 0)
        except (TypeError, ValueError, OverflowError):
            fill_time_ms = default_time
        fill_side = raw.get("side")
        if not fill_side and "isBuyer" in raw:
            is_buyer = raw.get("isBuyer")
            if isinstance(is_buyer, bool):
                fill_side = "BUY" if is_buyer else "SELL"
            elif str(is_buyer).strip().lower() in ("true", "1"):
                fill_side = "BUY"
            elif str(is_buyer).strip().lower() in ("false", "0"):
                fill_side = "SELL"
            else:
                raise TradeAccountingError("isBuyer fill tidak valid")
        normalized.append(
            {
                "symbol": str(raw.get("symbol") or symbol).upper(),
                "side": str(fill_side or side).upper(),
                "order_id": str(raw.get("orderId", raw.get("order_id", order_id)) or ""),
                "trade_id": trade_id,
                "time_ms": max(0, fill_time_ms),
                "price": price,
                "qty": qty,
                "quote_qty": quote_qty,
                "commission": commission,
                "commission_asset": commission_asset,
                "fee_quote_rate": None,
                "fee_quote_source": "",
                "fee_quote_value": None,
            }
        )
    if not normalized:
        raise TradeAccountingError("order terisi tetapi detail fill kosong")
    return normalized


def validate_fill_totals(
    fills: list[dict], expected_qty: Any, expected_quote: Any
) -> None:
    expected_qty_value = _finite_number(expected_qty, "executedQty", allow_zero=False)
    expected_quote_value = _finite_number(
        expected_quote, "cummulativeQuoteQty", allow_zero=False
    )
    actual_qty = sum(float(fill["qty"]) for fill in fills)
    actual_quote = sum(float(fill["quote_qty"]) for fill in fills)
    if not math.isclose(
        actual_qty,
        expected_qty_value,
        rel_tol=1e-8,
        abs_tol=max(1e-12, expected_qty_value * 1e-10),
    ):
        raise TradeAccountingError("total qty fill tidak cocok dengan status order")
    if not math.isclose(
        actual_quote,
        expected_quote_value,
        rel_tol=1e-8,
        abs_tol=max(1e-8, expected_quote_value * 1e-10),
    ):
        raise TradeAccountingError("total quote fill tidak cocok dengan status order")


def _historical_quote_rate(
    client: Any, asset: str, quote_asset: str, time_ms: int
) -> tuple[float, str]:
    if not asset or not quote_asset or time_ms <= 0:
        raise TradeAccountingError("aset atau waktu konversi komisi tidak tersedia")
    minute_open = (int(time_ms) // 60_000) * 60_000
    minute_end = minute_open + 59_999
    attempts = ((f"{asset}{quote_asset}", False), (f"{quote_asset}{asset}", True))
    last_error = None
    for pair, inverse in attempts:
        try:
            candles = client.get_klines(
                pair,
                "1m",
                limit=1,
                start_time_ms=minute_open,
                end_time_ms=minute_end,
            )
        except Exception as exc:
            last_error = exc
            continue
        if not isinstance(candles, list):
            last_error = TradeAccountingError("response candle konversi bukan list")
            continue
        for candle in candles:
            if not isinstance(candle, (list, tuple)) or len(candle) < 6:
                continue
            try:
                candle_open = int(candle[0])
                close_price = _finite_number(candle[4], "harga candle konversi", allow_zero=False)
            except (TypeError, ValueError, OverflowError, TradeAccountingError) as exc:
                last_error = exc
                continue
            if candle_open != minute_open:
                continue
            rate = 1.0 / close_price if inverse else close_price
            if math.isfinite(rate) and rate > 0:
                return rate, f"{pair}:1m-close"
    detail = f": {last_error}" if last_error else ""
    raise TradeAccountingError(
        f"harga historis {asset}/{quote_asset} untuk komisi tidak tersedia{detail}"
    )


def value_fill_commissions(
    fills: list[dict],
    *,
    base_asset: str,
    quote_asset: str,
    client: Any,
) -> dict:
    base = str(base_asset).upper()
    quote = str(quote_asset).upper()
    total_quote_value = 0.0
    cashflow_fee_quote = 0.0
    base_fee_qty = 0.0
    fees_by_asset: dict[str, float] = {}
    unpriced: list[dict] = []
    for fill in fills:
        amount = float(fill["commission"])
        if amount <= 0:
            fill["fee_quote_rate"] = 0.0
            fill["fee_quote_source"] = "zero"
            fill["fee_quote_value"] = 0.0
            continue
        asset = str(fill["commission_asset"]).upper()
        fees_by_asset[asset] = fees_by_asset.get(asset, 0.0) + amount
        if asset == quote:
            rate, source = 1.0, "quote"
        elif asset == base:
            rate, source = float(fill["price"]), "fill-price"
            base_fee_qty += amount
        else:
            stored_rate = fill.get("fee_quote_rate")
            try:
                rate = _finite_number(stored_rate, "kurs biaya", allow_zero=False)
                source = str(fill.get("fee_quote_source") or "historical-1m")
            except TradeAccountingError:
                try:
                    rate, source = _historical_quote_rate(
                        client,
                        asset,
                        quote,
                        int(fill.get("time_ms") or 0),
                    )
                except TradeAccountingError as exc:
                    unpriced.append(
                        {
                            "trade_id": fill.get("trade_id", ""),
                            "asset": asset,
                            "amount": amount,
                            "time_ms": int(fill.get("time_ms") or 0),
                            "error": str(exc)[:160],
                        }
                    )
                    fill["fee_quote_rate"] = None
                    fill["fee_quote_source"] = "unpriced"
                    fill["fee_quote_value"] = None
                    continue
        value_quote = amount * rate
        if not math.isfinite(value_quote) or value_quote < 0:
            unpriced.append(
                {
                    "trade_id": fill.get("trade_id", ""),
                    "asset": asset,
                    "amount": amount,
                    "time_ms": int(fill.get("time_ms") or 0),
                    "error": "nilai komisi quote tidak valid",
                }
            )
            fill["fee_quote_rate"] = None
            fill["fee_quote_source"] = "unpriced"
            fill["fee_quote_value"] = None
            continue
        fill["fee_quote_rate"] = rate
        fill["fee_quote_source"] = source
        fill["fee_quote_value"] = value_quote
        total_quote_value += value_quote
        if asset != base:
            cashflow_fee_quote += value_quote
    return {
        "fee_quote_value": total_quote_value,
        "cashflow_fee_quote": cashflow_fee_quote,
        "base_fee_qty": base_fee_qty,
        "fees_by_asset": fees_by_asset,
        "unpriced_fees": unpriced,
    }


def recompute_trade_accounting(
    accounting: dict, *, client: Any, actual_remaining_qty: Any = None
) -> dict:
    symbol = str(accounting.get("symbol") or "").upper()
    quote = str(accounting.get("quote_asset") or "").upper()
    if not symbol or not quote or not symbol.endswith(quote):
        raise TradeAccountingError("identitas trade accounting tidak valid")
    base = symbol[: -len(quote)]
    if not base:
        raise TradeAccountingError("base asset trade accounting kosong")
    legacy = (
        str(accounting.get("status") or "").upper() == "LEGACY_UNVERIFIED"
        or bool(accounting.get("legacy"))
    )
    orders = accounting.get("orders")
    if not isinstance(orders, list):
        orders = []
        accounting["orders"] = orders
    open_qty = 0.0
    remaining_cost = 0.0
    gross_remaining_cost = 0.0
    realized_pnl = 0.0
    gross_realized_pnl = 0.0
    entry_cost_quote = 0.0
    gross_entry_cost_quote = 0.0
    entry_net_qty = 0.0
    entry_gross_qty = 0.0
    all_fee_quote_value = 0.0
    all_cashflow_fee = 0.0
    unpriced_all = []
    unverified_orders = []
    accounting_verified = not legacy
    has_entry = legacy

    if legacy:
        open_qty = _finite_number(accounting.get("legacy_qty", 0), "legacy qty")
        remaining_cost = _finite_number(
            accounting.get("legacy_cost_quote", 0), "legacy cost"
        )
        gross_remaining_cost = remaining_cost
        entry_cost_quote = remaining_cost
        gross_entry_cost_quote = remaining_cost
        entry_net_qty = open_qty
        entry_gross_qty = open_qty

    for order in orders:
        if not isinstance(order, dict):
            accounting_verified = False
            unverified_orders.append({"order_id": "", "error": "rekaman order bukan object"})
            continue
        side = str(order.get("side") or "").upper()
        fills = order.get("fills")
        if side not in ("BUY", "SELL"):
            accounting_verified = False
            order["fill_status"] = "UNVERIFIED"
            unverified_orders.append(
                {"order_id": str(order.get("order_id") or ""), "error": "side tidak dikenal"}
            )
            continue
        if legacy and side == "BUY":
            continue
        valid_fills = isinstance(fills, list) and bool(fills)
        if valid_fills:
            try:
                normalized_fills = normalize_fills(
                    fills,
                    symbol=symbol,
                    side=side,
                    order_id=order.get("order_id"),
                    fallback_time_ms=order.get("time_ms"),
                )
                if any(fill["symbol"] != symbol for fill in normalized_fills):
                    raise TradeAccountingError("symbol fill tidak cocok dengan order")
                if any(fill["side"] != side for fill in normalized_fills):
                    raise TradeAccountingError("side fill tidak cocok dengan order")
                order_id = str(order.get("order_id") or "")
                if order_id and any(
                    fill["order_id"] and fill["order_id"] != order_id
                    for fill in normalized_fills
                ):
                    raise TradeAccountingError("order ID fill tidak cocok")
                validate_fill_totals(
                    normalized_fills,
                    order.get("expected_qty"),
                    order.get("expected_quote"),
                )
                for normalized, raw in zip(normalized_fills, fills):
                    if isinstance(raw, dict):
                        normalized["fee_quote_rate"] = raw.get("fee_quote_rate")
                        normalized["fee_quote_source"] = str(
                            raw.get("fee_quote_source") or ""
                        )
                        normalized["fee_quote_value"] = raw.get("fee_quote_value")
                fills = normalized_fills
                order["fills"] = normalized_fills
            except (TradeAccountingError, TypeError, ValueError, ArithmeticError) as exc:
                valid_fills = False
                order["fill_status"] = "UNVERIFIED"
                order["accounting_error"] = str(exc)[:160]
        if not valid_fills:
            accounting_verified = False
            order["fill_status"] = "UNVERIFIED"
            if _finite_number(order.get("expected_qty", 0), "expected qty") > 0:
                unverified_orders.append(
                    {
                        "order_id": str(order.get("order_id") or ""),
                        "side": side,
                        "error": str(order.get("accounting_error") or "detail fill belum diverifikasi")[:160],
                    }
                )
            gross_qty = _finite_number(order.get("expected_qty", 0), "expected qty")
            gross_quote = _finite_number(order.get("expected_quote", 0), "expected quote")
            fee_valuation = {"base_fee_qty": 0.0, "cashflow_fee_quote": 0.0}
            fees_by_asset = {}
            fee_quote_value = 0.0
        else:
            try:
                fee_valuation = value_fill_commissions(
                    fills, base_asset=base, quote_asset=quote, client=client
                )
            except (TradeAccountingError, TypeError, ValueError, ArithmeticError):
                accounting_verified = False
                fee_valuation = {
                    "base_fee_qty": 0.0,
                    "cashflow_fee_quote": 0.0,
                    "fee_quote_value": 0.0,
                    "fees_by_asset": {},
                    "unpriced_fees": [],
                }
            if fee_valuation.get("unpriced_fees"):
                accounting_verified = False
                unpriced_all.extend(
                    dict(item, order_id=str(order.get("order_id") or ""))
                    for item in fee_valuation["unpriced_fees"]
                )
            gross_qty = sum(float(fill["qty"]) for fill in fills)
            gross_quote = sum(float(fill["quote_qty"]) for fill in fills)
            fees_by_asset = fee_valuation["fees_by_asset"]
            fee_quote_value = fee_valuation["fee_quote_value"]
            all_fee_quote_value += fee_quote_value
            all_cashflow_fee += fee_valuation["cashflow_fee_quote"]
            order["gross_qty"] = gross_qty
            order["gross_quote"] = gross_quote
            order["fees_by_asset"] = fees_by_asset
            order["fee_quote_value"] = fee_quote_value
            order["cashflow_fee_quote"] = fee_valuation["cashflow_fee_quote"]
            order["base_fee_qty"] = fee_valuation["base_fee_qty"]
            order["fill_status"] = (
                "VERIFIED"
                if not fee_valuation.get("unpriced_fees")
                else "UNVERIFIED"
            )
            if fee_valuation.get("unpriced_fees"):
                order["unpriced_fees"] = fee_valuation["unpriced_fees"]
            else:
                order.pop("unpriced_fees", None)

        if side == "BUY":
            received_qty = gross_qty - fee_valuation["base_fee_qty"]
            if received_qty <= 0:
                accounting_verified = False
                continue
            has_entry = True
            open_qty += received_qty
            remaining_cost += gross_quote + fee_valuation["cashflow_fee_quote"]
            gross_remaining_cost += gross_quote
            entry_cost_quote += gross_quote + fee_valuation["cashflow_fee_quote"]
            gross_entry_cost_quote += gross_quote
            entry_net_qty += received_qty
            entry_gross_qty += gross_qty
        else:
            base_removed = (
                gross_qty
                if legacy or not valid_fills
                else gross_qty + fee_valuation["base_fee_qty"]
            )
            if open_qty <= 0 or base_removed > open_qty + max(1e-10, open_qty * 1e-8):
                accounting_verified = False
                continue
            removed = min(open_qty, base_removed)
            allocated_cost = remaining_cost * (removed / open_qty)
            allocated_gross_cost = gross_remaining_cost * (removed / open_qty)
            cashflow_fee = fee_valuation["cashflow_fee_quote"]
            net_proceeds = gross_quote - cashflow_fee
            realized_pnl += net_proceeds - allocated_cost
            gross_realized_pnl += gross_quote - allocated_gross_cost
            open_qty -= removed
            remaining_cost = max(0.0, remaining_cost - allocated_cost)
            gross_remaining_cost = max(0.0, gross_remaining_cost - allocated_gross_cost)

    if not has_entry:
        accounting_verified = False
    if legacy:
        accounting["status"] = "LEGACY_UNVERIFIED"
    elif accounting_verified:
        accounting["status"] = "VERIFIED"
        accounting.pop("accounting_error", None)
    else:
        accounting["status"] = "UNVERIFIED"
    if actual_remaining_qty is not None:
        actual_qty = _finite_number(actual_remaining_qty, "saldo sisa base")
        tolerance = max(1e-9, max(actual_qty, open_qty) * 1e-7)
        if abs(actual_qty - open_qty) > tolerance:
            accounting["status"] = "LEGACY_UNVERIFIED" if legacy else "UNVERIFIED"
            accounting["accounting_error"] = "saldo base tidak cocok dengan fill ledger"
            if open_qty > 0:
                ratio = min(actual_qty, open_qty) / open_qty
                remaining_cost *= ratio
                gross_remaining_cost *= ratio
            open_qty = actual_qty
    accounting["entry_cost_quote"] = entry_cost_quote
    accounting["gross_entry_cost_quote"] = gross_entry_cost_quote
    accounting["entry_net_qty"] = entry_net_qty
    accounting["entry_gross_qty"] = entry_gross_qty
    accounting["remaining_qty"] = open_qty
    accounting["remaining_cost_quote"] = remaining_cost
    accounting["gross_remaining_cost_quote"] = gross_remaining_cost
    accounting["realized_pnl_quote"] = realized_pnl
    accounting["gross_estimated_pnl_quote"] = gross_realized_pnl
    accounting["fee_quote_value_total"] = all_fee_quote_value
    accounting["cashflow_fee_quote_total"] = all_cashflow_fee
    accounting["unpriced_fees"] = unpriced_all
    accounting["fee_conversion_pending"] = bool(unpriced_all)
    accounting["unverified_orders"] = unverified_orders
    accounting["fill_details_pending"] = bool(unverified_orders)
    accounting["retryable_fill_details_pending"] = any(
        bool(str(item.get("order_id") or "")) for item in unverified_orders
    )
    accounting["accounting_verified"] = accounting.get("status") == "VERIFIED"
    accounting["has_entry"] = has_entry
    return accounting
