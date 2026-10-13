"""Pure cash-funded entry sizing shared by replay and live preparation.

Callers supply executable, unowned candidates in priority order. No quote,
broker, account, or policy is changed here. Money includes entry commission.
"""
from decimal import Decimal


MODES = {"legacy", "equal_eligible", "score_rank"}
FEE = Decimal("0.000140527")


def _amount(value):
    if isinstance(value, bool):
        raise ValueError("invalid_allocation_amount")
    try:
        number = Decimal(str(value))
    except Exception:
        raise ValueError("invalid_allocation_amount") from None
    if not number.is_finite() or number < 0:
        raise ValueError("invalid_allocation_amount")
    return number


def plan_entries(candidates, *, mode, available_cash, daily_remaining,
                 portfolio_remaining, order_cap, strategy_remaining,
                 strategy_weights, max_new_positions=None, fee_rate=FEE):
    """Return deterministic integer-share orders without exceeding any cap.

    Each candidate has symbol, strategy_id, price, quantity_cap and optionally
    rank (positive integer), cash_cap (broker buying power), eligible (bool).
    Ineligible rows are ignored by new modes; legacy counts them before sizing.
    A repeated symbol is owned by its first executable strategy. Equal mode
    splits each active strategy's weight over its unique eligible symbols;
    score_rank uses normalized reciprocal ranks, never raw feature scores.
    Duplicate strategy votes add to the symbol's weight, but never its caps.

    Strategy remaining amounts are hard position-budget caps. New modes do
    not impose a second daily strategy quota: unused daily cash is redistributed
    to executable symbols within all hard caps. Legacy retains signal-count
    daily quotas and performs no redistribution. Existing caller legacy paths
    may stay unchanged to retain historical policy semantics.
    """
    if mode not in MODES or not isinstance(candidates, list):
        raise ValueError("invalid_allocation_mode")
    if max_new_positions is not None and (type(max_new_positions) is not int or max_new_positions < 0):
        raise ValueError("invalid_allocation_position_limit")
    fee = _amount(fee_rate)
    if fee > 1:
        raise ValueError("invalid_allocation_fee")
    pool = min(_amount(available_cash), _amount(daily_remaining), _amount(portfolio_remaining))
    cap = _amount(order_cap)
    weights = {key: _amount(value) for key, value in strategy_weights.items()}
    remaining = {key: _amount(value) for key, value in strategy_remaining.items()}
    counts, groups, entries = {}, {}, {}
    for index, raw in enumerate(candidates):
        symbol, strategy = raw["symbol"], raw["strategy_id"]
        if not isinstance(symbol, str) or not isinstance(strategy, str):
            raise ValueError("invalid_allocation_candidate")
        if strategy not in weights or strategy not in remaining:
            raise ValueError("unknown_allocation_strategy")
        counts[strategy] = counts.get(strategy, 0) + 1
        if raw.get("eligible", True) is not True or weights[strategy] <= 0:
            continue
        price = _amount(raw["price"])
        quantity_cap = raw["quantity_cap"]
        rank = raw.get("rank", counts[strategy])
        if (price <= 0 or type(quantity_cap) is not int or quantity_cap < 0
                or type(rank) is not int or rank < 1):
            raise ValueError("invalid_allocation_candidate")
        unit = price * (1 + fee)
        cash_cap = min(cap, _amount(raw.get("cash_cap", cap)), unit * quantity_cap)
        if min(cash_cap, remaining[strategy], pool) < unit:
            continue
        group = groups.setdefault(strategy, {})
        group[symbol] = min(rank, group.get(symbol, rank))
        if symbol in entries:
            if entries[symbol]["price"] != price:
                raise ValueError("inconsistent_allocation_quote")
            continue
        entries[symbol] = {"symbol": symbol, "strategy_id": strategy, "price": price,
                           "unit": unit, "cap": cash_cap, "quantity": 0,
                           "weight": Decimal(0), "index": index}
    if not pool or not entries or max_new_positions == 0:
        return []
    for strategy, symbols in groups.items():
        votes = {symbol: (Decimal(1) / rank if mode == "score_rank" else Decimal(1))
                 for symbol, rank in symbols.items()}
        total = sum(votes.values(), Decimal(0))
        for symbol, vote in votes.items():
            entries[symbol]["weight"] += weights[strategy] * vote / total
    ordered = sorted(entries.values(), key=lambda item: (-item["weight"], item["index"]))
    if max_new_positions is not None:
        ordered = ordered[:max_new_positions]
    if mode == "legacy":
        ordered.sort(key=lambda item: item["index"])
        daily = _amount(daily_remaining)
        for item in ordered:
            strategy, unit = item["strategy_id"], item["unit"]
            amount = min(pool, item["cap"], remaining[strategy],
                         daily * weights[strategy] / counts[strategy])
            quantity = int(amount / unit)
            item["quantity"] = quantity
            pool -= unit * quantity
            remaining[strategy] -= unit * quantity
    else:
        # Each full pass gives every active symbol a proportional share. Caps
        # drop saturated symbols; a final affordable share resolves rounding.
        while pool > 0:
            active = [item for item in ordered if min(
                item["cap"] - item["unit"] * item["quantity"],
                remaining[item["strategy_id"]], pool) >= item["unit"]]
            if not active:
                break
            total = sum((item["weight"] for item in active), Decimal(0))
            before, progress = pool, False
            for item in active:
                strategy, unit = item["strategy_id"], item["unit"]
                amount = min(before * item["weight"] / total, pool,
                             item["cap"] - unit * item["quantity"], remaining[strategy])
                quantity = int(amount / unit)
                if quantity:
                    item["quantity"] += quantity
                    pool -= unit * quantity
                    remaining[strategy] -= unit * quantity
                    progress = True
            if not progress:
                item = max(active, key=lambda row: (before * row["weight"] / total / row["unit"], -row["index"]))
                item["quantity"] += 1
                pool -= item["unit"]
                remaining[item["strategy_id"]] -= item["unit"]
    return [{"symbol": item["symbol"], "strategy_id": item["strategy_id"],
             "quantity": item["quantity"], "price": format(item["price"], "f"),
             "estimated_cost": format(item["unit"] * item["quantity"], "f"),
             "allocation_weight": format(item["weight"], "f")}
            for item in sorted(ordered, key=lambda row: row["index"]) if item["quantity"]]
