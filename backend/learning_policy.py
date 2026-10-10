"""Pure, bounded trading policies and prospective daily-bar portfolio evaluation.

No training, storage, broker calls, or automatic adoption happens here. A signal
uses a completed close and can enter only at the next session's open. Daily bars
cannot evaluate cancellation minutes; that field is execution metadata only.
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
    return {"as_of": day, "observed_at": observed.isoformat(), "rows": sorted(rows, key=lambda row: row["symbol"]),
            "baseline_signals": deepcopy(baseline_signals or []), "missing_symbols": sorted(missing),
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


def _replay(policy, frames, limits, slip):
    budget, order_cap, daily_limit = (limits[key] for key in ("budget", "order_cap", "daily_buy_limit"))
    cash, positions, pending, equity_curve = budget, {}, [], []
    weights = {row["id"]: _decimal(row["weight"]) for row in _strategies(policy)}
    reserve, closed = budget * _decimal(policy["cash_reserve"]), 0
    previous_rows = {}
    for index, frame in enumerate(frames):
        rows = {row["symbol"]: row for row in frame["rows"]}
        def trade_bar(symbol):
            if symbol not in rows:
                raise ValueError("required_price_missing")
            bar = _bar(rows[symbol])
            if bar["volume"] <= 0 or bar["high"] == bar["low"]:
                raise ValueError("fill_unverifiable")
            return {key: _decimal(value) for key, value in bar.items()}
        for symbol, position in list(positions.items()):
            bar = trade_bar(symbol)
            prior_close = _decimal(previous_rows[symbol]["close"])
            change = (prior_close / position["entry_price"] - 1) * 100
            exit_policy = position["exit_policy"]
            timed = index - position["entry_index"] >= exit_policy["holding_sessions"]
            stopped = exit_policy["stop_loss_pct"] > 0 and change <= -_decimal(exit_policy["stop_loss_pct"])
            taken = exit_policy["take_profit_pct"] > 0 and change >= _decimal(exit_policy["take_profit_pct"])
            if timed or stopped or taken:
                cash += position["quantity"] * bar["open"] * (1 - slip) * (1 - FEE - TAX)
                del positions[symbol]
                closed += 1
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
            bar = trade_bar(symbol)
            if symbol in positions or policy["kind"] != "legacy" and len(positions) >= policy["max_positions"]:
                continue
            if (policy["kind"] != "legacy" and bar["open"] > _decimal(signal["reference_close"]) *
                    (1 + _decimal(policy["entry_slippage_bps"]) / 10000)):
                continue
            used = sum((position["cost"] for position in positions.values()), Decimal(0))
            strategy_used = sum((position["cost"] for position in positions.values() if position["strategy_id"] == strategy), Decimal(0))
            capacity = min(order_cap, daily_limit * weights[strategy] / max(1, counts[strategy]),
                           budget - used, daily_limit - spent, cash - reserve,
                           budget * weights[strategy] - strategy_used,
                           daily_limit * weights[strategy] - strategy_spent[strategy])
            price = bar["open"] * (1 + slip)
            unit_cost = price * (1 + FEE)
            quantity = max(0, int((capacity / unit_cost).to_integral_value(rounding=ROUND_FLOOR)))
            if not quantity:
                continue
            cost = quantity * unit_cost
            cash -= cost
            spent += cost
            strategy_spent[strategy] += cost
            positions[symbol] = {"strategy_id": strategy, "quantity": quantity, "cost": cost,
                "entry_price": price, "entry_index": index, "exit_policy": signal["exit_policy"]}
        equity = cash + sum((position["quantity"] * trade_bar(symbol)["close"] * (1 - slip) * (1 - FEE - TAX)
                             for symbol, position in positions.items()), Decimal(0))
        equity_curve.append(equity)
        pending, previous_rows = signals(policy, frame), rows
    peak, drawdown = budget, Decimal(0)
    for equity in equity_curve:
        peak = max(peak, equity)
        drawdown = min(drawdown, (equity / peak - 1) * 100)
    midpoint = equity_curve[max(0, len(equity_curve) // 2 - 1)]
    return {"return": float((equity_curve[-1] / budget - 1) * 100), "drawdown": float(-drawdown),
            "closed": closed, "half_returns": [float((midpoint / budget - 1) * 100),
                                                float((equity_curve[-1] / midpoint - 1) * 100)]}


def portfolio_metrics(policy, frames, limits):
    """Replay one cash-funded portfolio twice, with normal/stress trading costs.

    Historical frame gaps invalidate evaluation when session_days proves a missed
    session. Missing/untradeable prices for held or pending-entry symbols also
    invalidate it. Existing unused/unavailable universe rows need no invented
    price. Final positions are marked net of estimated exit costs, not forcibly
    counted as completed trades. cancel_after_minutes cannot be scored by bars.
    """
    result = {"valid": False, "error": None, "net_return_pct": None, "stress_return_pct": None,
              "max_drawdown_pct": None, "stress_drawdown_pct": None, "closed_trades": 0,
              "sessions": len(frames) if isinstance(frames, list) else 0, "half_returns": [None, None]}
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
        normal = _replay(policy, frames, limits, Decimal("0.001"))
        stress = _replay(policy, frames, limits, Decimal("0.002"))
        result.update(valid=True, net_return_pct=normal["return"], stress_return_pct=stress["return"],
                      max_drawdown_pct=normal["drawdown"], stress_drawdown_pct=stress["drawdown"],
                      closed_trades=normal["closed"], half_returns=normal["half_returns"])
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError) as error:
        reason = str(error)
        result["error"] = reason if re.fullmatch(r"[a-z][a-z0-9_]*", reason) else "invalid_evaluation_input"
    return result
