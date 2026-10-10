"""Pure calculations for the contest mirror; no persistence or submission.

Callers must supply the FULL KIS experiment ledger, never a paginated UI list.
It must come from a successfully reconciled upstream snapshot; this module does
not establish freshness, import external trades, or reconcile broker responses.
A checkpoint acknowledges observation, not a destination trade. mirror_runtime
persists pending work with its cursor before advancing it. Proposals require
verified browser state before the user-run program can submit them.
"""
from datetime import date
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_DOWN
import json
import re


class MirrorInputError(ValueError):
    """An incomplete or conflicting source cannot advance its checkpoint."""


def _number(value, field, *, positive=False, percent=False):
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or positive and result <= 0 or percent and result > 100:
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise MirrorInputError("invalid_" + field) from None


def _quantity(value, field):
    if type(value) is not int or value < 0:
        raise MirrorInputError("invalid_" + field)
    return value


def _symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{6}", value):
        raise MirrorInputError("invalid_symbol")
    return value


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise MirrorInputError("invalid_" + field)
    return value


def _orders(rows, fingerprint):
    if not isinstance(rows, list):
        raise MirrorInputError("full_order_ledger_required")
    result, local_ids = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            raise MirrorInputError("invalid_order")
        if _text(row.get("fingerprint"), "order_fingerprint") != fingerprint:
            continue
        filled = _quantity(row.get("filled_quantity"), "filled_quantity")
        amount = _number(row.get("filled_amount"), "filled_amount")
        if (filled == 0) != (amount == 0):
            raise MirrorInputError("conflicting_fill_amount")
        # An unacknowledged, unfilled local intent has no broker identity yet.
        if not filled and row.get("order_id") is None and row.get("branch_id") is None:
            continue
        for field, length in (("order_id", 20), ("branch_id", 10)):
            if not isinstance(row.get(field), str) or not re.fullmatch(r"[0-9]{1," + str(length) + "}", row[field]):
                raise MirrorInputError("missing_or_invalid_" + field)
        try:
            day = date.fromisoformat(row["order_date"]).isoformat()
            if day != row["order_date"]:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise MirrorInputError("invalid_order_date") from None
        quantity = _quantity(row.get("quantity"), "order_quantity")
        if not quantity or filled > quantity or row.get("side") not in ("buy", "sell"):
            raise MirrorInputError("invalid_order_quantity_or_side")
        key = json.dumps([fingerprint, day, row["branch_id"], row["order_id"]], separators=(",", ":"))
        local_id = row.get("id")
        if local_id is not None:
            _text(local_id, "local_id")
        if key in result or local_id is not None and local_id in local_ids:
            raise MirrorInputError("duplicate_order_identity")
        if local_id is not None:
            local_ids.add(_text(local_id, "local_id"))
        result[key] = {"fingerprint": fingerprint, "order_date": day,
                       "branch_id": row["branch_id"], "order_id": row["order_id"],
                       "symbol": _symbol(row.get("symbol")), "side": row["side"],
                       "quantity": quantity, "filled_quantity": filled, "filled_amount": str(amount),
                       **({"id": local_id} if local_id is not None else {})}
    return result


def detect_fill_changes(orders, *, source_fingerprint, checkpoint=None, initialize=None):
    """Return changes, a JSON-serializable checkpoint, and system-owned quantities.

    First use requires initialize='baseline' (skip old fills) or 'replay'. Later
    calls require the previous checkpoint and no initialize argument. Changes
    contain cumulative quantity boundaries, not fictitious individual fill IDs.
    Baseline suppresses initial notifications only: owned_quantities still counts
    ALL historical system fills, not just positions opened after the baseline.
    """
    fingerprint = _text(source_fingerprint, "source_fingerprint")
    current = _orders(orders, fingerprint)
    if checkpoint is None:
        if initialize not in ("baseline", "replay"):
            raise MirrorInputError("explicit_baseline_or_replay_required")
        previous = {}
    else:
        if (initialize is not None or not isinstance(checkpoint, dict)
                or type(checkpoint.get("version")) is not int or checkpoint["version"] != 1):
            raise MirrorInputError("invalid_checkpoint")
        if checkpoint.get("source_fingerprint") != fingerprint:
            raise MirrorInputError("source_account_changed")
        rows = checkpoint.get("orders")
        if not isinstance(rows, list) or any(not isinstance(row, dict) or row.get("fingerprint") != fingerprint for row in rows):
            raise MirrorInputError("invalid_checkpoint_orders")
        previous = _orders(rows, fingerprint)
        if len(previous) != len(rows):
            raise MirrorInputError("invalid_checkpoint_orders")
        if previous.keys() - current.keys():
            raise MirrorInputError("source_order_missing_or_identity_changed")
    changes, owned = [], {}
    for key, row in sorted(current.items()):
        old = previous.get(key)
        before = old["filled_quantity"] if old else 0
        before_amount = Decimal(old["filled_amount"]) if old else Decimal(0)
        amount = Decimal(row["filled_amount"])
        if old and any(old[field] != row[field] for field in ("symbol", "side", "quantity")):
            raise MirrorInputError("source_order_conflict")
        if row["filled_quantity"] < before or amount < before_amount:
            raise MirrorInputError("source_fill_decreased")
        delta = row["filled_quantity"] - before
        if delta == 0 and amount != before_amount or delta > 0 and amount <= before_amount:
            raise MirrorInputError("source_fill_amount_conflict")
        symbol = row["symbol"]
        owned[symbol] = owned.get(symbol, 0) + row["filled_quantity"] * (1 if row["side"] == "buy" else -1)
        if delta and initialize != "baseline":
            changes.append({"source_key": key, "source_fingerprint": fingerprint,
                            "symbol": symbol, "side": row["side"], "quantity": delta,
                            "amount": str(amount - before_amount), "from_quantity": before,
                            "to_quantity": row["filled_quantity"]})
    if any(quantity < 0 for quantity in owned.values()):
        raise MirrorInputError("source_sales_exceed_owned_fills")
    return {"changes": changes, "owned_quantities": owned,
            "checkpoint": {"version": 1, "source_fingerprint": fingerprint,
                           "orders": [current[key] for key in sorted(current)]}}


def plan_weight_changes(changes, *, source_fingerprint, owned_quantities, source_prices,
                        source_equity, destination_positions, pending_orders, destination_verified=False):
    """Plan only symbols with new fills; never rebalance merely because prices move.

    All weights are percentage points of destination equity. Prices are
    {symbol: {price, fresh: True}}; positions are {symbol: {weight, sellable_weight}}.
    sellable_weight is gross eligible holding BEFORE pending-sell reservations;
    an adapter must not pass a net available value and subtract reservations twice.
    Pending orders are the COMPLETE list of {symbol, side, weight} reservations.
    Destination positions must explicitly include zero holdings for absent symbols.

    The destination order form enforces its trading limits; this calculation
    does not duplicate eligibility, cash, stock, sector, or small-cap limits.
    Sells use actual sellable_weight less pending sells, without resizing.
    Differences smaller than the 0.01 percentage-point step are unchanged.
    Opposite fills caught up together net to zero and do not replay a round trip.
    Outputs are proposal/blocked/unchanged, never permission to submit an order.
    """
    if changes == []:
        return []
    try:
        fingerprint = _text(source_fingerprint, "source_fingerprint")
        if not isinstance(changes, list):
            raise MirrorInputError("invalid_changes")
        touched = {}
        for change in changes:
            if not isinstance(change, dict) or change.get("source_fingerprint") != fingerprint:
                raise MirrorInputError("source_account_changed")
            symbol = _symbol(change.get("symbol"))
            quantity = _quantity(change.get("quantity"), "change_quantity")
            if not quantity or change.get("side") not in ("buy", "sell"):
                raise MirrorInputError("invalid_change")
            touched[symbol] = touched.get(symbol, 0) + quantity * (1 if change["side"] == "buy" else -1)
        if destination_verified is not True or not isinstance(pending_orders, list):
            raise MirrorInputError("destination_state_unverified")
        equity = _number(source_equity, "source_equity", positive=True)
        reserved = {}
        for order in pending_orders:
            if not isinstance(order, dict) or order.get("side") not in ("buy", "sell"):
                raise MirrorInputError("invalid_pending_order")
            pair = (_symbol(order.get("symbol")), order["side"])
            reserved[pair] = reserved.get(pair, Decimal(0)) + _number(order.get("weight"), "pending_weight", positive=True, percent=True)
    except (MirrorInputError, DecimalException) as error:
        return [{"symbol": None, "status": "blocked", "reason": str(error) if isinstance(error, MirrorInputError) else "invalid_numeric_input"}]
    results = []
    for symbol, net_fill in sorted(touched.items()):
        result = {"symbol": symbol, "status": "blocked"}
        try:
            if net_fill == 0:
                results.append({**result, "status": "unchanged", "reason": "net_fills_zero"})
                continue
            quantity = _quantity(owned_quantities.get(symbol), "owned_quantity")
            target = Decimal(0)
            if quantity:
                quote = source_prices.get(symbol, {})
                if not isinstance(quote, dict) or quote.get("fresh") is not True:
                    raise MirrorInputError("source_price_missing_or_stale")
                price = _number(quote.get("price"), "source_price", positive=True)
                target = Decimal(quantity) * price / equity * 100
            if target > 100:
                raise MirrorInputError("source_weight_exceeds_equity")
            position = destination_positions.get(symbol, {})
            weight = _number(position.get("weight"), "destination_weight", percent=True)
            sellable = _number(position.get("sellable_weight"), "sellable_weight", percent=True)
            if sellable > weight:
                raise MirrorInputError("sellable_exceeds_holding")
            buy, sell = reserved.get((symbol, "buy"), Decimal(0)), reserved.get((symbol, "sell"), Decimal(0))
            if sell > sellable:
                raise MirrorInputError("pending_sell_exceeds_sellable")
            if buy and sell or net_fill > 0 and sell or net_fill < 0 and buy:
                raise MirrorInputError("pending_reverse_side_conflict")
            delta = target - weight - buy + sell
            result.update(target_weight=str(target), current_weight=str(weight), desired_delta=str(delta))
            if delta == 0:
                results.append({**result, "status": "unchanged", "reason": "target_already_reserved"})
                continue
            amount = abs(delta).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if not amount:
                results.append({**result, "status": "unchanged", "reason": "below_weight_step"})
                continue
            if delta > 0 and net_fill < 0 or delta < 0 and net_fill > 0:
                raise MirrorInputError("direction_conflict")
            if delta < 0 and amount > sellable - sell:
                result.update(wanted_weight=str(amount), available_weight=str(sellable - sell))
                raise MirrorInputError("sellable_weight_exceeded")
            result.update(status="proposal", side="buy" if delta > 0 else "sell", weight=str(amount),
                          delta_weight=str(amount if delta > 0 else -amount))
        except (MirrorInputError, AttributeError, TypeError, DecimalException) as error:
            result["reason"] = str(error) if isinstance(error, MirrorInputError) else "invalid_input_shape"
        results.append(result)
    return results
