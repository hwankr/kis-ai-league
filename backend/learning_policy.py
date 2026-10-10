"""Pure, bounded trading policies and prospective daily-bar portfolio evaluation.

No training, storage, broker calls, or automatic adoption happens here. A signal
uses a completed close and enters no earlier than the next session, using its
open or an observed decision quote. Daily bars cannot evaluate cancellation
minutes; that field is execution metadata only.
"""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
import hashlib
import json
import math
import re


FEATURES = (
    "return_5d_pct", "return_20d_pct", "excess_20d_pp", "close_sma20_pct",
    "close_sma60_pct", "breakout_20d_pct", "volume_ratio", "turnover",
)
BASELINE_POLICY = {"kind": "legacy", "name": "기존 네 전략", "strategies": [],
                   "cash_reserve": 0.0, "max_positions": 20,
                   "entry_slippage_bps": 100, "cancel_after_minutes": 10}
LEGACY_IDS = ("trend-breakout-v1", "pullback-recovery-v1", "relative-strength-v1", "llm-evidence-v1")
KST = timezone(timedelta(hours=9))
FEE, TAX = Decimal("0.000140527"), Decimal("0.002")


def _number(value, reason="invalid_number", *, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise ValueError(reason)
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(reason) from None
    if (not math.isfinite(result) or minimum is not None and result < minimum
            or maximum is not None and result > maximum):
        raise ValueError(reason)
    return 0.0 if result == 0 else result


def _integer(value, low, high, reason):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(reason)
    return value


def _text(value, reason, maximum=80):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(reason)
    return value.strip()


def _day(value):
    if type(value) is date:
        return value.isoformat()
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("invalid_date")
    return date.fromisoformat(value).isoformat()


def _instant(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if result.utcoffset() is None:
            raise ValueError
        return result
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid_observed_at") from None


def _symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{6}", value):
        raise ValueError("invalid_symbol")
    return value


def validate_policy(policy):
    """Return a canonical JSON policy; reject arbitrary code and unknown fields."""
    if not isinstance(policy, dict) or set(policy) != set(BASELINE_POLICY):
        raise ValueError("invalid_policy_fields")
    if policy["kind"] not in {"legacy", "rules"}:
        raise ValueError("invalid_policy_kind")
    result = {"kind": policy["kind"], "name": _text(policy["name"], "invalid_policy_name"),
              "cash_reserve": _number(policy["cash_reserve"], "invalid_cash_reserve", minimum=0, maximum=.9),
              "max_positions": _integer(policy["max_positions"], 1, 20, "invalid_max_positions"),
              "entry_slippage_bps": _number(policy["entry_slippage_bps"], "invalid_entry_slippage", minimum=0, maximum=200),
              "cancel_after_minutes": _integer(policy["cancel_after_minutes"], 5, 60, "invalid_cancel_minutes"),
              "strategies": []}
    strategies = policy["strategies"]
    if not isinstance(strategies, list):
        raise ValueError("invalid_strategies")
    if policy["kind"] == "legacy":
        if strategies or any(result[key] != BASELINE_POLICY[key] for key in result if key != "name"):
            raise ValueError("legacy_policy_is_fixed")
        return result
    if not 1 <= len(strategies) <= 20:
        raise ValueError("invalid_strategy_count")
    fields = {"id", "name", "weight", "conditions", "rank_by", "descending", "top_n",
              "holding_sessions", "stop_loss_pct", "take_profit_pct"}
    identities = set()
    for strategy in strategies:
        if not isinstance(strategy, dict) or set(strategy) != fields:
            raise ValueError("invalid_strategy_fields")
        identity = _text(strategy["id"], "invalid_strategy_id", 100)
        if identity in LEGACY_IDS:
            raise ValueError("reserved_strategy_id")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", identity) or identity in identities:
            raise ValueError("duplicate_or_invalid_strategy_id")
        identities.add(identity)
        if strategy["rank_by"] not in FEATURES or type(strategy["descending"]) is not bool:
            raise ValueError("invalid_ranking")
        conditions = strategy["conditions"]
        if not isinstance(conditions, list) or len(conditions) > 16:
            raise ValueError("invalid_conditions")
        cleaned = []
        for condition in conditions:
            if (not isinstance(condition, dict) or set(condition) != {"feature", "op", "value"}
                    or condition["feature"] not in FEATURES or condition["op"] not in {"gt", "lt"}):
                raise ValueError("invalid_condition")
            cleaned.append({"feature": condition["feature"], "op": condition["op"],
                            "value": _number(condition["value"], "invalid_condition_value")})
        result["strategies"].append({"id": identity, "name": _text(strategy["name"], "invalid_strategy_name"),
            "weight": _number(strategy["weight"], "invalid_strategy_weight", minimum=0, maximum=1),
            "conditions": cleaned, "rank_by": strategy["rank_by"], "descending": strategy["descending"],
            "top_n": _integer(strategy["top_n"], 1, 20, "invalid_top_n"),
            "holding_sessions": _integer(strategy["holding_sessions"], 1, 20, "invalid_holding_sessions"),
            "stop_loss_pct": _number(strategy["stop_loss_pct"], "invalid_stop_loss", minimum=0, maximum=20),
            "take_profit_pct": _number(strategy["take_profit_pct"], "invalid_take_profit", minimum=0, maximum=100)})
    if sum(Decimal(str(item["weight"])) for item in result["strategies"]) > 1 - Decimal(str(result["cash_reserve"])):
        raise ValueError("weights_exceed_investable_budget")
    return result


def policy_id(policy):
    encoded = json.dumps(validate_policy(policy), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _bar(raw):
    if not isinstance(raw, dict):
        raise ValueError("invalid_bar")
    result = {key: _number(raw.get(key), "invalid_bar", minimum=0) for key in ("open", "high", "low", "close", "volume")}
    if (min(result[key] for key in ("open", "high", "low", "close")) <= 0
            or not result["low"] <= min(result["open"], result["close"]) <= max(result["open"], result["close"]) <= result["high"]):
        raise ValueError("invalid_bar")
    return result


def build_frame(data, baseline_signals=None):
    """Freeze completed OHLCV and eight features, with no observations after as_of.

    SMA ratios and returns are percentages; volume_ratio is a multiple; turnover
    is the original bar's currency amount. Breakout compares the close to the
    preceding 20 highs, excluding today's high. Missing lookbacks remain None.
    learning_eligible=False keeps a price for marking but excludes new signals.
    """
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise ValueError("invalid_frame_input")
    day, observed = _day(data.get("as_of")), _instant(data.get("observed_at"))
    if observed < datetime.combine(date.fromisoformat(day), time(16), KST):
        raise ValueError("incomplete_session")
    histories, calendars, benchmarks = data.get("histories", {}), data.get("calendars", {}), data.get("benchmarks", {})
    if not all(isinstance(value, dict) for value in (histories, calendars, benchmarks)):
        raise ValueError("invalid_frame_input")
    sessions = {board: sorted({_day(value) for value in days if _day(value) <= day}) for board, days in calendars.items()}
    rows, missing, seen = [], [], set()
    for item in data["rows"]:
        symbol = _symbol(item.get("symbol"))
        if symbol in seen or item.get("board") not in {"KOSPI", "KOSDAQ"}:
            raise ValueError("duplicate_symbol_or_invalid_board")
        seen.add(symbol)
        series = {_day(key): value for key, value in histories.get(symbol, {}).items() if _day(key) <= day}
        try:
            bar = _bar(series.get(day))
        except ValueError:
            missing.append(symbol)
            continue
        days = sessions.get(item["board"], sorted(series))
        if not days or days[-1] != day:
            missing.append(symbol)
            continue
        features = dict.fromkeys(FEATURES)
        try:
            features["turnover"] = _number(series[day].get("turnover"), "invalid_turnover", minimum=0)
        except ValueError:
            pass
        def window(count):
            if len(days) < count:
                return None
            try:
                return [_bar(series.get(value)) for value in days[-count:]]
            except ValueError:
                return None
        for length in (5, 20):
            bars = window(length + 1)
            if bars:
                features[f"return_{length}d_pct"] = (bar["close"] / bars[0]["close"] - 1) * 100
        for length in (20, 60):
            bars = window(length)
            if bars:
                features[f"close_sma{length}_pct"] = (bar["close"] / (sum(row["close"] for row in bars) / length) - 1) * 100
        previous = window(21)
        if previous:
            features["breakout_20d_pct"] = (bar["close"] / max(row["high"] for row in previous[:-1]) - 1) * 100
            average = sum(row["volume"] for row in previous[:-1]) / 20
            features["volume_ratio"] = bar["volume"] / average if average else None
            index = {_day(key): value for key, value in benchmarks.get(item["board"], {}).items() if _day(key) <= day}
            try:
                start, end = (_number(index.get(value), "invalid_benchmark", minimum=0) for value in (days[-21], day))
                if start > 0 and end > 0:
                    features["excess_20d_pp"] = features["return_20d_pct"] - (end / start - 1) * 100
            except ValueError:
                pass
        rows.append({"symbol": symbol, "name": str(item.get("name", symbol)), "board": item["board"], **bar,
                     "features": features, "learning_eligible": item.get("learning_eligible", True) is True})
    benchmark_prices = {}
    for board in ("KOSPI", "KOSDAQ"):
        try:
            price = _number(benchmarks.get(board, {}).get(day), "invalid_benchmark", minimum=0)
            if price > 0:
                benchmark_prices[board] = price
        except ValueError:
            pass
    return {"as_of": day, "observed_at": observed.isoformat(), "rows": sorted(rows, key=lambda row: row["symbol"]),
            "baseline_signals": deepcopy(baseline_signals or []), "missing_symbols": sorted(missing),
            "benchmark_prices": benchmark_prices,
            "session_days": sorted(set().union(*sessions.values())) if sessions else [day]}


def _strategies(policy):
    if policy["kind"] == "legacy":
        return [{"id": identity, "weight": .25, "holding_sessions": 5, "stop_loss_pct": 0., "take_profit_pct": 0.} for identity in LEGACY_IDS]
    return policy["strategies"]


def signals(policy, frame):
    """Return deterministic buy signals; a hold/exit remains the executor's job."""
    policy = validate_policy(policy)
    identity = policy_id(policy)
    rows = {row["symbol"]: row for row in frame["rows"]}
    result = []
    strategies = _strategies(policy)
    # Match the legacy executor's rotating priority, without sorting rule ranks away.
    if policy["kind"] == "legacy":
        offset = date.fromisoformat(frame["as_of"]).toordinal() % len(strategies)
        strategies = strategies[offset:] + strategies[:offset]
    groups = []
    for strategy in strategies:
        candidates = []
        if strategy["weight"] <= 0:
            groups.append([])
            continue
        for row in rows.values():
            if not row.get("learning_eligible", True):
                continue
            score = 0.
            if policy["kind"] == "legacy":
                eligible = any(item.get("strategy_id") == strategy["id"] and item.get("symbol") == row["symbol"]
                               and item.get("action") == "buy" and item.get("status", "ready") == "ready"
                               for item in frame.get("baseline_signals", []))
                if not eligible:
                    continue
            else:
                features = row.get("features", {})
                try:
                    score = _number(features.get(strategy["rank_by"]))
                    if not all((_number(features.get(item["feature"])) > item["value"] if item["op"] == "gt"
                                else _number(features.get(item["feature"])) < item["value"]) for item in strategy["conditions"]):
                        continue
                except ValueError:
                    continue
            candidates.append({"strategy_id": strategy["id"], "symbol": row["symbol"], "name": row["name"],
                "board": row["board"], "action": "buy", "status": "ready", "score": score,
                "reason": "legacy_signal" if policy["kind"] == "legacy" else "learned_policy_conditions",
                "evidence_ids": [f"market:{row['symbol']}:{frame['as_of']}"],
                "exit_policy": {key: strategy[key] for key in ("holding_sessions", "stop_loss_pct", "take_profit_pct")},
                "policy_id": identity, "reference_close": row["close"]})
        candidates.sort(key=lambda item: ((-item["score"] if strategy.get("descending", True) else item["score"]), item["symbol"]))
        groups.append(candidates if policy["kind"] == "legacy" else candidates[:strategy["top_n"]])
    for index in range(max((len(group) for group in groups), default=0)):
        result.extend(group[index] for group in groups if index < len(group))
    return result


def _decimal(value):
    return Decimal(str(_number(value)))


def _state_decimal(value):
    # Restore money without a float round-trip, including partial-sale cost basis.
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid_initial_number")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("invalid_initial_number")
    return result


def _decimal_text(value):
    return format(value.normalize(), "f")


def _exit_policy(value):
    if not isinstance(value, dict):
        raise ValueError("invalid_initial_exit_policy")
    return {"holding_sessions": _integer(value.get("holding_sessions"), 1, 20, "invalid_initial_exit_policy"),
            "stop_loss_pct": _number(value.get("stop_loss_pct"), minimum=0, maximum=20),
            "take_profit_pct": _number(value.get("take_profit_pct"), minimum=0, maximum=100)}


def _execution_model(value):
    value = {} if value is None else value
    allowed = {"mode", "fill_ratio", "buy_fill_ratio", "sell_fill_ratio", "fee_rate", "tax_rate",
               "slippage_bps", "stress_slippage_bps", "stress_fee_rate", "stress_fill_ratio"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("invalid_execution_model")
    mode = value.get("mode", "daily_open")
    if mode not in {"daily_open", "observed_quotes"}:
        raise ValueError("invalid_execution_mode")
    ratio = _number(value.get("fill_ratio", 1), "invalid_execution_model", minimum=0, maximum=1)
    fee = value.get("fee_rate", FEE)
    result = {"mode": mode}
    for key, default, maximum in (("buy_fill_ratio", ratio, 1), ("sell_fill_ratio", ratio, 1),
            ("fee_rate", fee, .05), ("tax_rate", TAX, .1),
            ("slippage_bps", 10 if mode == "daily_open" else 0, 1000),
            ("stress_slippage_bps", 20 if mode == "daily_open" else 0, 1000),
            ("stress_fee_rate", fee if mode == "daily_open" else _decimal(fee) + Decimal(".001"), .05)):
        result[key] = _decimal(_number(value.get(key, default), "invalid_execution_model", minimum=0, maximum=maximum))
    result["stress_fill_ratio"] = (None if value.get("stress_fill_ratio") is None else
                                   _decimal(_number(value["stress_fill_ratio"], "invalid_execution_model", minimum=0, maximum=1)))
    if result["stress_slippage_bps"] < result["slippage_bps"] or result["stress_fee_rate"] < result["fee_rate"]:
        raise ValueError("invalid_cost_stress")
    return result


def _initial_state(value, budget, first_day):
    if value is None:
        return budget, Decimal(0), {}, None
    if not isinstance(value, dict) or value.get("as_of", first_day) != first_day:
        raise ValueError("invalid_initial_state_date")
    cash = _state_decimal(value.get("cash"))
    reserved = _state_decimal(value.get("reserved_cash", 0))
    if reserved > cash or not isinstance(value.get("positions", []), list):
        raise ValueError("invalid_initial_state")
    positions = {}
    for item in value.get("positions", []):
        symbol = _symbol(item.get("symbol"))
        if symbol in positions:
            raise ValueError("duplicate_initial_position")
        position = {"strategy_id": _text(item.get("strategy_id"), "invalid_initial_strategy"),
                    "quantity": _integer(item.get("quantity"), 1, 10**12, "invalid_initial_quantity"),
                    "held_sessions": _integer(item.get("held_sessions", 0), 0, 10**6, "invalid_initial_age"),
                    "exit_policy": _exit_policy(item.get("exit_policy"))}
        for key in ("cost", "entry_price", "last_close"):
            position[key] = _state_decimal(item.get(key))
            if position[key] <= 0:
                raise ValueError("invalid_initial_position")
        if type(item.get("exit_pending", False)) is not bool:
            raise ValueError("invalid_initial_exit_pending")
        position["exit_pending"] = item.get("exit_pending", False)
        position["exit_fill_remainder"] = _state_decimal(item.get("exit_fill_remainder", 0))
        if position["exit_fill_remainder"] >= 1:
            raise ValueError("invalid_exit_fill_remainder")
        positions[symbol] = position
    return cash, reserved, positions, value


def _replay(policy, frames, limits, model, initial_state, *, stress=False, execution_stress=False):
    budget, order_cap, daily_limit = (limits[key] for key in ("budget", "order_cap", "daily_buy_limit"))
    cash, reserved, positions, saved = _initial_state(initial_state, budget, frames[0]["as_of"])
    slip = model["stress_slippage_bps" if stress else "slippage_bps"] / 10000
    fee, tax = model["stress_fee_rate" if stress else "fee_rate"], model["tax_rate"]
    ratios = {side: model[side + "_fill_ratio"] for side in ("buy", "sell")}
    if execution_stress:
        ratios = {side: min(ratio, model["stress_fill_ratio"]) for side, ratio in ratios.items()}
    weights = {row["id"]: _decimal(row["weight"]) for row in _strategies(policy)}
    reserve, closed, identity = budget * _decimal(policy["cash_reserve"]), 0, policy_id(policy)
    curve, pending = [], []
    diagnostics = {key: 0 for key in ("buy_attempts", "sell_attempts", "buy_filled_quantity", "sell_filled_quantity",
                   "partial_orders", "unfilled_orders", "untradeable_bars", "ineligible_quotes", "limit_blocked",
                   "delayed_exit_sessions")}
    paid_fee, paid_tax = Decimal(0), Decimal(0)
    for index, frame in enumerate(frames):
        rows = {row["symbol"]: row for row in frame["rows"]}
        bars = {}
        def bar_for(symbol):
            if symbol not in bars:
                if symbol not in rows:
                    raise ValueError("required_price_missing")
                bars[symbol] = {key: _decimal(value) for key, value in _bar(rows[symbol]).items()}
            return bars[symbol]
        def execution_price(symbol, side, limit=None):
            bar = bar_for(symbol)
            diagnostics[side + "_attempts"] += 1
            if bar["volume"] <= 0 or bar["high"] == bar["low"]:
                diagnostics["untradeable_bars"] += 1
                diagnostics["unfilled_orders"] += 1
                return None
            quote = {}
            if model["mode"] == "observed_quotes":
                quote = frame.get("execution_quotes", {}).get(symbol)
                if (not isinstance(quote, dict) or type(quote.get("eligible")) is not bool
                        or type(quote.get("buy_eligible", True)) is not bool):
                    raise ValueError("execution_quote_missing")
                observed = _instant(quote.get("observed_at"))
                if observed.astimezone(KST).date().isoformat() != frame["as_of"] or observed > _instant(frame["observed_at"]):
                    raise ValueError("execution_quote_outside_session")
                if not quote["eligible"] or side == "buy" and not quote.get("buy_eligible", True):
                    diagnostics["ineligible_quotes"] += 1
                    diagnostics["unfilled_orders"] += 1
                    return None
                price = _decimal(quote.get("price"))
                if price <= 0:
                    raise ValueError("invalid_execution_quote")
                explicit = quote.get(side + "_limit_price", quote.get("limit_price", price))
                if explicit is not None:
                    explicit = _decimal(explicit)
                    if explicit <= 0:
                        raise ValueError("invalid_execution_limit")
                    limit = explicit if limit is None else (min(limit, explicit) if side == "buy" else max(limit, explicit))
            else:
                price = bar["open"]
            price *= 1 + slip if side == "buy" else 1 - slip
            if limit is not None and (price > limit if side == "buy" else price < limit):
                diagnostics["limit_blocked"] += 1
                diagnostics["unfilled_orders"] += 1
                return None
            return price
        def fill_quantity(requested, side, position=None):
            expected = requested * ratios[side]
            if position is not None:
                expected += position["exit_fill_remainder"]
            quantity = min(requested, int(expected.to_integral_value(rounding=ROUND_FLOOR)))
            if position is not None:
                position["exit_fill_remainder"] = expected - quantity
            diagnostics[side + "_filled_quantity"] += quantity
            if quantity < requested:
                diagnostics["partial_orders" if quantity else "unfilled_orders"] += 1
            return quantity
        for symbol, position in list(positions.items()):
            bar_for(symbol)  # Suspensions still have a close for valuation.
            if not index:
                continue
            position["held_sessions"] += 1
            change = (position["last_close"] / position["entry_price"] - 1) * 100
            exit_policy = position["exit_policy"]
            timed = position["held_sessions"] >= exit_policy["holding_sessions"]
            stopped = exit_policy["stop_loss_pct"] > 0 and change <= -_decimal(exit_policy["stop_loss_pct"])
            taken = exit_policy["take_profit_pct"] > 0 and change >= _decimal(exit_policy["take_profit_pct"])
            if position["exit_pending"] or timed or stopped or taken:
                position["exit_pending"] = True
                price = execution_price(symbol, "sell")
                quantity = fill_quantity(position["quantity"], "sell", position) if price is not None else 0
                if quantity:
                    proceeds = quantity * price
                    cash += proceeds * (1 - fee - tax)
                    paid_fee += proceeds * fee
                    paid_tax += proceeds * tax
                    position["cost"] *= Decimal(position["quantity"] - quantity) / position["quantity"]
                    position["quantity"] -= quantity
                if not position["quantity"]:
                    del positions[symbol]
                    closed += 1
                else:
                    diagnostics["delayed_exit_sessions"] += 1
        if policy["kind"] == "legacy":
            offset = date.fromisoformat(frame["as_of"]).toordinal() % len(LEGACY_IDS)
            ordering = LEGACY_IDS[offset:] + LEGACY_IDS[:offset]
            groups = [sorted((item for item in pending if item["strategy_id"] == key), key=lambda item: item["symbol"])
                      for key in ordering]
            pending = [group[number] for number in range(max((len(group) for group in groups), default=0))
                       for group in groups if number < len(group)]
        counts = {key: sum(item["strategy_id"] == key for item in pending) for key in weights}
        spent, strategy_spent = Decimal(0), dict.fromkeys(weights, Decimal(0))
        for signal in pending:
            symbol, strategy = signal["symbol"], signal["strategy_id"]
            if symbol in positions or policy["kind"] != "legacy" and len(positions) >= policy["max_positions"]:
                continue
            used = sum((position["cost"] for position in positions.values()), Decimal(0))
            strategy_used = sum((position["cost"] for position in positions.values() if position["strategy_id"] == strategy), Decimal(0))
            capacity = min(order_cap, daily_limit * weights[strategy] / max(1, counts[strategy]),
                           budget - reserve - used, daily_limit - spent, cash - reserved - reserve,
                           budget * weights[strategy] - strategy_used,
                           daily_limit * weights[strategy] - strategy_spent[strategy])
            if capacity <= 0:
                continue
            limit = (None if policy["kind"] == "legacy" else _decimal(signal["reference_close"]) *
                     (1 + _decimal(policy["entry_slippage_bps"]) / 10000))
            price = execution_price(symbol, "buy", limit)
            if price is None:
                continue
            unit_cost = price * (1 + fee)
            requested = max(0, int((capacity / unit_cost).to_integral_value(rounding=ROUND_FLOOR)))
            quantity = fill_quantity(requested, "buy") if requested else 0
            if not quantity:
                continue
            cost = quantity * unit_cost
            cash -= cost
            spent += cost
            strategy_spent[strategy] += cost
            paid_fee += quantity * price * fee
            positions[symbol] = {"strategy_id": strategy, "quantity": quantity, "cost": cost,
                "entry_price": price, "held_sessions": 0, "exit_policy": deepcopy(signal["exit_policy"]),
                "exit_pending": False, "exit_fill_remainder": Decimal(0)}
        for symbol, position in positions.items():
            position["last_close"] = bar_for(symbol)["close"]
        position_value = sum((position["quantity"] * position["last_close"] * (1 - slip) * (1 - fee - tax)
                              for position in positions.values()), Decimal(0))
        equity = cash + position_value
        if equity <= 0:
            raise ValueError("nonpositive_equity")
        curve.append({"as_of": frame["as_of"], "equity": float(equity), "cash": float(cash),
                      "position_value": float(position_value), "reserved_cash": float(reserved)})
        pending = signals(policy, frame)
        if not index and saved and saved.get("policy_id") == identity and "pending_entries" in saved:
            supplied = saved["pending_entries"]
            if not isinstance(supplied, list) or supplied != pending:
                raise ValueError("initial_pending_signals_changed")
    equities = [_decimal(point["equity"]) for point in curve]
    start, peak, drawdown = equities[0], equities[0], Decimal(0)
    for equity in equities:
        peak = max(peak, equity)
        drawdown = min(drawdown, (equity / peak - 1) * 100)
    midpoint = equities[max(0, len(equities) // 2 - 1)]
    state_positions = []
    for symbol, position in sorted(positions.items()):
        state_positions.append({"symbol": symbol, **{key: _decimal_text(value) if isinstance(value, Decimal) else deepcopy(value)
                                                     for key, value in position.items()}})
    diagnostics.update(fees_paid=float(paid_fee), taxes_paid=float(paid_tax), mode=model["mode"],
                       buy_fill_ratio=float(ratios["buy"]), sell_fill_ratio=float(ratios["sell"]),
                       fee_rate=float(fee), tax_rate=float(tax), slippage_bps=float(slip * 10000),
                       entry_remainder="cancelled_after_observation", exit_remainder="retry_next_observation")
    return {"return": float((equities[-1] / start - 1) * 100), "drawdown": float(-drawdown),
            "closed": closed, "half_returns": [float((midpoint / start - 1) * 100),
                                                float((equities[-1] / midpoint - 1) * 100)],
            "equity_curve": curve, "daily_log_returns": [math.log(float(after / before)) for before, after in zip(equities, equities[1:])],
            "final_state": {"as_of": frames[-1]["as_of"], "policy_id": identity, "cash": _decimal_text(cash),
                            "reserved_cash": _decimal_text(reserved), "positions": state_positions, "pending_entries": pending},
            "diagnostics": diagnostics}


def portfolio_metrics(policy, frames, limits, *, initial_state=None, execution_model=None):
    """Replay one cash-funded portfolio twice, with normal/stress trading costs.

    Historical frame gaps invalidate evaluation when session_days proves a missed
    session. Missing required prices invalidate evaluation; zero-volume/flat bars
    merely prevent fills, retaining the position and its loss. Frame zero is the
    close-valued anchor; its signals execute on the next frame. Initial positions
    retain their exit policy. held_sessions=0 means the entry-day close, increasing
    once per subsequent session. Cash includes reserved_cash, which cannot fund a
    buy. initial_state accepts one common state or {normal: state, stress: state}.

    observed_quotes uses next-frame execution_quotes[symbol] with an aware
    observed_at on that session, eligible bool, and price when eligible. Optional
    quote.price is the decision limit unless a side-specific or common limit_price
    is supplied. Worse synthetic fills are forbidden. Its default
    cost stress adds 10bp commission, without inventing a worse-than-limit price.
    Daily-open defaults retain 10/20bp slippage. Optional stress_fill_ratio runs a
    separate execution stress with normal costs. Entry remainders are cancelled
    after each observation; exit intent persists. Fractional sell-fill remainder
    carries to the next attempt, so a positive ratio can eventually close one
    remaining share. Cancellation minutes and queue priority cannot be inferred
    from these inputs. The caller supplies quotes observed inside its live order
    window. Final holdings are marked net
    of estimated liquidation costs, not counted as completed trades.
    """
    result = {"valid": False, "error": None, "net_return_pct": None, "stress_return_pct": None,
              "max_drawdown_pct": None, "stress_drawdown_pct": None, "closed_trades": 0,
              "sessions": len(frames) if isinstance(frames, list) else 0, "half_returns": [None, None],
              "intervals": max(0, len(frames) - 1) if isinstance(frames, list) else 0,
              "equity_curve": [], "stress_equity_curve": [], "daily_log_returns": [], "stress_daily_log_returns": [],
              "final_state": None, "stress_final_state": None, "execution_stress_return_pct": None,
              "execution_stress_drawdown_pct": None, "diagnostics": {}}
    try:
        policy = validate_policy(policy)
        if not isinstance(frames, list) or len(frames) < 2:
            raise ValueError("insufficient_frames")
        if not isinstance(limits, dict):
            raise ValueError("invalid_limits")
        limits = {key: _decimal(limits.get(key)) for key in ("budget", "order_cap", "daily_buy_limit")}
        if not 0 < limits["order_cap"] <= limits["daily_buy_limit"] <= limits["budget"]:
            raise ValueError("invalid_limits")
        last = None
        for frame in frames:
            day = _day(frame.get("as_of"))
            if last and day <= last:
                raise ValueError("unordered_frames")
            if _instant(frame.get("observed_at")) < datetime.combine(date.fromisoformat(day), time(16), KST):
                raise ValueError("incomplete_session")
            if last and any(last < _day(session) < day for session in frame.get("session_days", [])):
                raise ValueError("missing_session")
            if not isinstance(frame.get("rows"), list):
                raise ValueError("invalid_frame_rows")
            symbols = [_symbol(row.get("symbol")) for row in frame["rows"]]
            if len(symbols) != len(set(symbols)):
                raise ValueError("duplicate_frame_symbol")
            last = day
        model = _execution_model(execution_model)
        states = initial_state if isinstance(initial_state, dict) and "normal" in initial_state else {
            "normal": initial_state, "stress": initial_state}
        if "normal" not in states or "stress" not in states:
            raise ValueError("invalid_initial_states")
        normal = _replay(policy, frames, limits, model, states["normal"])
        stress = _replay(policy, frames, limits, model, states["stress"], stress=True)
        execution_stress = (_replay(policy, frames, limits, model, states["normal"], execution_stress=True)
                            if model["stress_fill_ratio"] is not None else None)
        result.update(valid=True, net_return_pct=normal["return"], stress_return_pct=stress["return"],
                      max_drawdown_pct=normal["drawdown"], stress_drawdown_pct=stress["drawdown"],
                      closed_trades=normal["closed"], half_returns=normal["half_returns"],
                      equity_curve=normal["equity_curve"], stress_equity_curve=stress["equity_curve"],
                      daily_log_returns=normal["daily_log_returns"], stress_daily_log_returns=stress["daily_log_returns"],
                      final_state=normal["final_state"], stress_final_state=stress["final_state"],
                      execution_stress_return_pct=execution_stress["return"] if execution_stress else None,
                      execution_stress_drawdown_pct=execution_stress["drawdown"] if execution_stress else None,
                      diagnostics={"normal": normal["diagnostics"], "cost_stress": stress["diagnostics"],
                                   "execution_stress": execution_stress["diagnostics"] if execution_stress else None})
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError) as error:
        reason = str(error)
        result["error"] = reason if re.fullmatch(r"[a-z][a-z0-9_]*", reason) else "invalid_evaluation_input"
    return result
