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
POLICY_OPTIONS = {"allocation_mode", "candidate_scope", "held_before_top_n", "no_same_day_reentry"}
STRATEGY_OPTIONS = {"exit_mode", "max_holding_sessions"}
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
    if (not isinstance(policy, dict) or not set(BASELINE_POLICY) <= set(policy)
            or set(policy) - set(BASELINE_POLICY) - POLICY_OPTIONS):
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
        if (strategies or set(policy) & POLICY_OPTIONS
                or any(result[key] != BASELINE_POLICY[key] for key in result if key != "name")):
            raise ValueError("legacy_policy_is_fixed")
        return result
    if not 1 <= len(strategies) <= 20:
        raise ValueError("invalid_strategy_count")
    for key, allowed in (("allocation_mode", {"legacy", "equal_eligible", "score_rank"}),
                         ("candidate_scope", {"unrestricted", "liquid_top20", "liquid_all"})):
        if key in policy:
            if policy[key] not in allowed:
                raise ValueError("invalid_" + key)
            result[key] = policy[key]
    for key in ("held_before_top_n", "no_same_day_reentry"):
        if key in policy:
            if type(policy[key]) is not bool:
                raise ValueError("invalid_" + key)
            result[key] = policy[key]
    fields = {"id", "name", "weight", "conditions", "rank_by", "descending", "top_n",
              "holding_sessions", "stop_loss_pct", "take_profit_pct"}
    identities = set()
    for strategy in strategies:
        if (not isinstance(strategy, dict) or not fields <= set(strategy)
                or set(strategy) - fields - STRATEGY_OPTIONS):
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
        if "exit_mode" in strategy:
            if strategy["exit_mode"] not in {"fixed", "signal_extension"}:
                raise ValueError("invalid_exit_mode")
            result["strategies"][-1]["exit_mode"] = strategy["exit_mode"]
        if "max_holding_sessions" in strategy:
            result["strategies"][-1]["max_holding_sessions"] = _integer(
                strategy["max_holding_sessions"], result["strategies"][-1]["holding_sessions"], 20,
                "invalid_max_holding_sessions")
        if strategy.get("exit_mode") == "signal_extension" and "max_holding_sessions" not in strategy:
            raise ValueError("missing_max_holding_sessions")
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
        selection = {"complete_61": item["board"] in sessions and window(61) is not None, "positive_volume_20": False,
                     "turnover_20d_mean": None}
        recent = window(20)
        if recent and item["board"] in sessions:
            selection["positive_volume_20"] = all(value["volume"] > 0 for value in recent)
            try:
                selection["turnover_20d_mean"] = sum(
                    _number(series[value].get("turnover"), minimum=0) for value in days[-20:]) / 20
            except ValueError:
                pass
        rows.append({"symbol": symbol, "name": str(item.get("name", symbol)), "board": item["board"], **bar,
                     "features": features, "selection": selection,
                     "learning_eligible": item.get("learning_eligible", True) is True})
    benchmark_prices = {}
    for board in ("KOSPI", "KOSDAQ"):
        try:
            matching = [_number(value, "invalid_benchmark", minimum=0)
                        for key, value in benchmarks.get(board, {}).items() if _day(key) == day]
            if not matching or len(set(matching)) != 1:
                continue
            price = matching[0]
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


def canonical_entry_context(initial_state):
    """Freeze the held/reserved symbol sets without changing the supplied book."""
    if not isinstance(initial_state, dict) or not isinstance(initial_state.get("positions"), list):
        raise ValueError("invalid_entry_context_state")
    if "reserved_entries" not in initial_state and _state_decimal(initial_state.get("reserved_cash", 0)):
        raise ValueError("reserved_entries_required")
    reserved = initial_state.get("reserved_entries", [])
    if not isinstance(reserved, list):
        raise ValueError("invalid_entry_context_state")
    result = {}
    for key, values in (("held_symbols", initial_state["positions"]), ("reserved_symbols", reserved)):
        if any(not isinstance(value, dict) for value in values):
            raise ValueError("invalid_entry_context_state")
        result[key] = sorted({_symbol(value.get("symbol")) for value in values})
    return result


def signals(policy, frame, *, entry_context=None):
    """Return close-frozen entries, optionally excluding the supplied close book.

    The context is required for opt-in policies. A later execution-time book must
    not silently replace the book used to prepare an immutable signal run.
    """
    policy = validate_policy(policy)
    excluded = set()
    if policy.get("held_before_top_n", False):
        if entry_context is None:
            raise ValueError("entry_context_required")
        if not isinstance(entry_context, dict) or set(entry_context) != {"held_symbols", "reserved_symbols"}:
            raise ValueError("invalid_entry_context")
        for key in ("held_symbols", "reserved_symbols"):
            values = entry_context[key]
            if not isinstance(values, list):
                raise ValueError("invalid_entry_context")
            symbols = [_symbol(value) for value in values]
            if len(set(symbols)) != len(symbols):
                raise ValueError("duplicate_entry_context_symbol")
            excluded.update(symbols)
    return _rule_signals(policy, frame, excluded_symbols=excluded)


def _rule_signals(policy, frame, *, excluded_symbols=()):
    """Apply the full-frame data contract before optional entry-only exclusions."""
    identity = policy_id(policy)
    rows = {row["symbol"]: row for row in frame["rows"]}
    scope = policy.get("candidate_scope", "unrestricted")
    if scope != "unrestricted":
        if any("selection" not in row for row in rows.values()):
            raise ValueError("candidate_scope_metadata_missing")
        liquid = [row for row in rows.values() if row.get("learning_eligible", True)
                  and row["selection"].get("complete_61") is True
                  and row["selection"].get("positive_volume_20") is True
                  and row["selection"].get("turnover_20d_mean") is not None
                  and _number(row["selection"]["turnover_20d_mean"], minimum=0) >= 10_000_000_000]
        liquid.sort(key=lambda row: (-row["selection"]["turnover_20d_mean"], row["symbol"]))
        rows = {row["symbol"]: row for row in (liquid[:20] if scope == "liquid_top20" else liquid)}
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
            if row["symbol"] in excluded_symbols:
                continue
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
        for rank, candidate in enumerate(candidates, 1):
            if policy.get("allocation_mode", "legacy") != "legacy":
                candidate["rank"] = rank
            if strategy.get("exit_mode") == "signal_extension":
                candidate["exit_policy"].update(exit_mode="signal_extension",
                    max_holding_sessions=strategy["max_holding_sessions"],
                    continuation_policy=deepcopy(policy), continuation_strategy=strategy["id"])
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
    result = {"holding_sessions": _integer(value.get("holding_sessions"), 1, 20, "invalid_initial_exit_policy"),
            "stop_loss_pct": _number(value.get("stop_loss_pct"), minimum=0, maximum=20),
            "take_profit_pct": _number(value.get("take_profit_pct"), minimum=0, maximum=100)}
    if value.get("exit_mode", "fixed") not in {"fixed", "signal_extension"}:
        raise ValueError("invalid_initial_exit_mode")
    if value.get("exit_mode") == "signal_extension":
        frozen = validate_policy(value.get("continuation_policy"))
        strategy = value.get("continuation_strategy")
        if frozen["kind"] != "rules" or strategy not in {item["id"] for item in frozen["strategies"]}:
            raise ValueError("invalid_continuation_policy")
        result.update(exit_mode="signal_extension", continuation_policy=frozen, continuation_strategy=strategy,
                      max_holding_sessions=_integer(value.get("max_holding_sessions"),
                          result["holding_sessions"], 20, "invalid_max_holding_sessions"))
    return result


def position_exit_due(exit_policy, *, symbol, held_sessions, entry_price, last_close, quantity,
                      cost, frame=None, fee_rate=FEE, tax_rate=TAX):
    """Use the entry's frozen rule and the preceding completed close only."""
    rule = _exit_policy(exit_policy)
    change = (_decimal(last_close) / _decimal(entry_price) - 1) * 100
    if ((rule["stop_loss_pct"] > 0 and change <= -_decimal(rule["stop_loss_pct"]))
            or (rule["take_profit_pct"] > 0 and change >= _decimal(rule["take_profit_pct"]))):
        return True
    if held_sessions < rule["holding_sessions"]:
        return False
    if rule.get("exit_mode") != "signal_extension" or held_sessions >= rule["max_holding_sessions"]:
        return True
    if not frame or _decimal(quantity) * _decimal(last_close) * (1 - _decimal(fee_rate) - _decimal(tax_rate)) <= _decimal(cost):
        return True
    try:
        # Entry-only exclusions must never make a held lot fail its frozen
        # continuation rule merely because it is already owned.
        maintained = _rule_signals(validate_policy(rule["continuation_policy"]), frame)
    except (ValueError, KeyError, TypeError):
        return True
    return not any(item["symbol"] == symbol and item["strategy_id"] == rule["continuation_strategy"]
                   for item in maintained)


def _execution_model(value):
    value = {} if value is None else value
    allowed = {"mode", "fill_ratio", "buy_fill_ratio", "sell_fill_ratio", "fee_rate", "tax_rate",
               "slippage_bps", "stress_slippage_bps", "stress_fee_rate", "stress_fill_ratio",
               "sell_delay_sessions", "adverse_buy", "daily_buy_caps"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("invalid_execution_model")
    mode = value.get("mode", "daily_open")
    if mode not in {"daily_open", "observed_quotes"}:
        raise ValueError("invalid_execution_mode")
    ratio = _number(value.get("fill_ratio", 1), "invalid_execution_model", minimum=0, maximum=1)
    fee = value.get("fee_rate", FEE)
    result = {"mode": mode}
    caps = value.get("daily_buy_caps", {})
    if not isinstance(caps, dict):
        raise ValueError("invalid_daily_buy_caps")
    result["daily_buy_caps"] = {_day(day): _state_decimal(cap) for day, cap in caps.items()}
    result["sell_delay_sessions"] = _integer(value.get("sell_delay_sessions", 0), 0, 20, "invalid_sell_delay")
    if type(value.get("adverse_buy", False)) is not bool:
        raise ValueError("invalid_adverse_buy")
    result["adverse_buy"] = value.get("adverse_buy", False)
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
    if "reserved_entries" in value:
        entries, seen = value["reserved_entries"], set()
        if not isinstance(entries, list):
            raise ValueError("invalid_reserved_entries")
        total = Decimal(0)
        for item in entries:
            if not isinstance(item, dict) or set(item) != {"symbol", "strategy_id", "cost"}:
                raise ValueError("invalid_reserved_entries")
            symbol = _symbol(item["symbol"])
            _text(item["strategy_id"], "invalid_initial_strategy")
            cost = _state_decimal(item["cost"])
            if symbol in seen or cost <= 0:
                raise ValueError("invalid_reserved_entries")
            seen.add(symbol)
            total += cost
        if total != reserved:
            raise ValueError("reserved_entries_cost_mismatch")
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
        position["exit_delay_remaining"] = (None if item.get("exit_delay_remaining") is None else
                                            _integer(item["exit_delay_remaining"], 0, 20, "invalid_exit_delay"))
        positions[symbol] = position
    return cash, reserved, positions, value


def _replay(policy, frames, limits, model, initial_state, *, stress=False, execution_stress=False, support=None):
    budget, order_cap, daily_limit = (limits[key] for key in ("budget", "order_cap", "daily_buy_limit"))
    cash, reserved, positions, saved = _initial_state(initial_state, budget, frames[0]["as_of"])
    protection = deepcopy((saved or {}).get("account_protection"))
    if protection is not None:
        if (not isinstance(protection, dict) or type(protection.get("active")) is not bool
                or set(protection) != {"units", "peak", "active", "limit_pct"}):
            raise ValueError("invalid_account_protection")
        for key in ("units", "peak"):
            protection[key] = _state_decimal(protection[key])
            if protection[key] <= 0:
                raise ValueError("invalid_account_protection")
        protection["limit_pct"] = _decimal(_number(protection["limit_pct"],
            "invalid_account_protection", minimum=.000001, maximum=100))
    def observe_protection(equity):
        if protection is None:
            return
        unit = equity / protection["units"]
        protection["peak"] = max(protection["peak"], unit)
        if (1 - unit / protection["peak"]) * 100 >= protection["limit_pct"]:
            protection["active"] = True
    reserved_entries = (saved or {}).get("reserved_entries", [])
    reserved_symbols = {item["symbol"] for item in reserved_entries}
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
                   "delayed_exit_sessions", "scenario_delayed_sells", "adverse_buys_omitted")}
    coverage = {"checked": 0, "outside": 0, "reasons": set()}
    def check_support(side, price, observed, quote):
        if support is None:
            return
        coverage["checked"] += 1
        bounds, reasons = support.get(side), []
        if not bounds:
            reasons.append(side + "_context_missing")
        else:
            values = {"price": float(price), "minute": observed.astimezone(KST).hour * 60 + observed.astimezone(KST).minute}
            try:
                values["volume"] = _number(quote.get("volume"), minimum=0)
            except ValueError:
                reasons.append(side + "_volume_missing")
            for key, value in values.items():
                if not bounds[key + "_min"] <= value <= bounds[key + "_max"]:
                    reasons.append(side + "_" + key + "_outside")
        if reasons:
            coverage["outside"] += 1
            coverage["reasons"].update(reasons)
    paid_fee, paid_tax = Decimal(0), Decimal(0)
    gross_buy, gross_sell = Decimal(0), Decimal(0)
    traded_days, traded_symbols = set(), set()
    buy_fills, sell_fills = 0, 0
    for index, frame in enumerate(frames):
        day_buy, day_sell = Decimal(0), Decimal(0)
        sold_today = set()
        rows = {row["symbol"]: row for row in frame["rows"]}
        bars = {}
        protection_observed_at = None
        protection_was_active = bool(protection and protection["active"])
        def bar_for(symbol):
            if symbol not in bars:
                if symbol not in rows:
                    raise ValueError("required_price_missing")
                bars[symbol] = {key: _decimal(value) for key, value in _bar(rows[symbol]).items()}
            return bars[symbol]
        def execution_price(symbol, side, limit=None, position=None):
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
                if (not protection_was_active and protection_observed_at is not None
                        and observed < protection_observed_at):
                    raise ValueError("protection_quote_after_decision")
                if not quote["eligible"] or side == "buy" and not quote.get("buy_eligible", True):
                    diagnostics["ineligible_quotes"] += 1
                    diagnostics["unfilled_orders"] += 1
                    return None
                price = _decimal(quote.get("price"))
                if price <= 0:
                    raise ValueError("invalid_execution_quote")
                check_support(side, price, observed, quote)
                explicit = quote.get(side + "_limit_price", quote.get("limit_price", price))
                if explicit is not None:
                    explicit = _decimal(explicit)
                    if explicit <= 0:
                        raise ValueError("invalid_execution_limit")
                    limit = explicit if limit is None else (min(limit, explicit) if side == "buy" else max(limit, explicit))
            else:
                price = bar["open"]
            # Deliberately ex-post stress, not an estimate or a signal feature.
            # The same date/symbol/quote has the same outcome for either policy.
            if side == "buy" and model["adverse_buy"] and bar["close"] > price:
                diagnostics["adverse_buys_omitted"] += 1
                diagnostics["unfilled_orders"] += 1
                return None
            if side == "sell" and position is not None:
                if position["exit_delay_remaining"] is None:
                    position["exit_delay_remaining"] = model["sell_delay_sessions"]
                if position["exit_delay_remaining"]:
                    position["exit_delay_remaining"] -= 1
                    diagnostics["scenario_delayed_sells"] += 1
                    diagnostics["unfilled_orders"] += 1
                    return None
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
        # The live account checks its durable NAV peak before exits and entries.
        # Intraday observations are only modelled where a frozen quote exists.
        if index and protection is not None:
            marked = Decimal(0)
            for symbol, position in positions.items():
                bar = bar_for(symbol)
                price = bar["open"]
                if model["mode"] == "observed_quotes":
                    quote = frame.get("execution_quotes", {}).get(symbol)
                    if not isinstance(quote, dict) or quote.get("price") is None:
                        raise ValueError("protection_quote_missing")
                    observed = _instant(quote.get("observed_at"))
                    if (observed.astimezone(KST).date().isoformat() != frame["as_of"]
                            or observed > _instant(frame["observed_at"])):
                        raise ValueError("execution_quote_outside_session")
                    price = _decimal(quote["price"])
                    if price <= 0:
                        raise ValueError("invalid_execution_quote")
                    protection_observed_at = max(protection_observed_at or observed, observed)
                marked += position["quantity"] * price * (1 - fee - tax)
            if protection_observed_at is not None and not protection_was_active:
                for signal in pending:
                    if signal["symbol"] in positions:
                        continue
                    quote = frame.get("execution_quotes", {}).get(signal["symbol"])
                    if (isinstance(quote, dict) and quote.get("eligible") is True
                            and quote.get("buy_eligible", True) is True and quote.get("price") is not None
                            and _instant(quote.get("observed_at")) < protection_observed_at):
                        # A later holding quote cannot decide whether an earlier
                        # entry should be blocked, even if protection now trips.
                        raise ValueError("protection_quote_after_decision")
            observe_protection(cash + marked)
        for symbol, position in list(positions.items()):
            bar_for(symbol)  # Suspensions still have a close for valuation.
            if not index:
                continue
            position["held_sessions"] += 1
            exit_policy = position["exit_policy"]
            due = position_exit_due(exit_policy, symbol=symbol, held_sessions=position["held_sessions"],
                entry_price=position["entry_price"], last_close=position["last_close"],
                quantity=position["quantity"], cost=position["cost"], frame=frames[index - 1],
                fee_rate=fee, tax_rate=tax)
            if position["exit_pending"] or due or protection is not None and protection["active"]:
                position["exit_pending"] = True
                price = execution_price(symbol, "sell", position=position)
                quantity = fill_quantity(position["quantity"], "sell", position) if price is not None else 0
                if quantity:
                    sold_today.add(symbol)
                    proceeds = quantity * price
                    gross_sell += proceeds
                    day_sell += proceeds
                    sell_fills += 1
                    traded_days.add(frame["as_of"])
                    traded_symbols.add(symbol)
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
        if protection is not None and protection["active"]:
            pending = []
        counts = {key: sum(item["strategy_id"] == key for item in pending) for key in weights}
        spent, strategy_spent = Decimal(0), dict.fromkeys(weights, Decimal(0))
        session_limit = min(daily_limit, model["daily_buy_caps"].get(frame["as_of"], daily_limit))
        planned = None
        if policy.get("allocation_mode", "legacy") != "legacy":
            from backend.portfolio_plan import plan_entries
            if reserved and "reserved_entries" not in (saved or {}):
                raise ValueError("reserved_entries_required")
            candidates = []
            quoted = {}
            for signal in pending:
                symbol = signal["symbol"]
                if (symbol in positions or symbol in reserved_symbols
                        or policy.get("no_same_day_reentry", False) and symbol in sold_today):
                    continue
                if symbol not in quoted:
                    limit = _decimal(signal["reference_close"]) * (1 + _decimal(policy["entry_slippage_bps"]) / 10000)
                    quoted[symbol] = execution_price(symbol, "buy", limit)
                if quoted[symbol] is not None:
                    candidates.append({**signal, "price": quoted[symbol], "quantity_cap": 10**12})
            used = sum((position["cost"] for position in positions.values()), Decimal(0)) + reserved
            remaining = {key: max(Decimal(0), budget * weight - sum((position["cost"] for position in positions.values()
                         if position["strategy_id"] == key), Decimal(0)) - sum((_state_decimal(item["cost"])
                         for item in reserved_entries if item["strategy_id"] == key), Decimal(0))) for key, weight in weights.items()}
            plan = plan_entries(candidates, mode=policy["allocation_mode"], available_cash=max(Decimal(0), cash-reserved-reserve),
                daily_remaining=session_limit, portfolio_remaining=max(Decimal(0), budget-reserve-used), order_cap=order_cap,
                strategy_remaining=remaining, strategy_weights=weights, fee_rate=fee,
                max_new_positions=max(0, policy["max_positions"]-len(set(positions) | reserved_symbols)))
            planned = {item["symbol"]: item for item in plan}
            by_key = {(item["symbol"], item["strategy_id"]): item for item in pending}
            pending = [by_key[(item["symbol"], item["strategy_id"])] for item in plan]
        for signal in pending:
            symbol, strategy = signal["symbol"], signal["strategy_id"]
            if (symbol in positions or policy["kind"] != "legacy" and len(positions) >= policy["max_positions"]
                    or policy.get("no_same_day_reentry", False) and symbol in sold_today):
                continue
            used = sum((position["cost"] for position in positions.values()), Decimal(0))
            strategy_used = sum((position["cost"] for position in positions.values() if position["strategy_id"] == strategy), Decimal(0))
            capacity = min(order_cap, daily_limit * weights[strategy] / max(1, counts[strategy]),
                           budget - reserve - used, session_limit - spent, cash - reserved - reserve,
                           budget * weights[strategy] - strategy_used,
                           daily_limit * weights[strategy] - strategy_spent[strategy])
            if planned is not None:
                capacity = _decimal(planned[symbol]["estimated_cost"])
            if capacity <= 0:
                continue
            limit = (None if policy["kind"] == "legacy" else _decimal(signal["reference_close"]) *
                     (1 + _decimal(policy["entry_slippage_bps"]) / 10000))
            price = _decimal(planned[symbol]["price"]) if planned is not None else execution_price(symbol, "buy", limit)
            if price is None:
                continue
            unit_cost = price * (1 + fee)
            requested = (planned[symbol]["quantity"] if planned is not None else
                         max(0, int((capacity / unit_cost).to_integral_value(rounding=ROUND_FLOOR))))
            quantity = fill_quantity(requested, "buy") if requested else 0
            if not quantity:
                continue
            cost = quantity * unit_cost
            gross_buy += quantity * price
            day_buy += quantity * price
            buy_fills += 1
            traded_days.add(frame["as_of"])
            traded_symbols.add(symbol)
            cash -= cost
            spent += cost
            strategy_spent[strategy] += cost
            paid_fee += quantity * price * fee
            positions[symbol] = {"strategy_id": strategy, "quantity": quantity, "cost": cost,
                "entry_price": price, "held_sessions": 0, "exit_policy": deepcopy(signal["exit_policy"]),
                "exit_pending": False, "exit_fill_remainder": Decimal(0), "exit_delay_remaining": None}
        for symbol, position in positions.items():
            position["last_close"] = bar_for(symbol)["close"]
        position_value = sum((position["quantity"] * position["last_close"] * (1 - slip) * (1 - fee - tax)
                              for position in positions.values()), Decimal(0))
        equity = cash + position_value
        if equity <= 0:
            raise ValueError("nonpositive_equity")
        observe_protection(cash + sum((position["quantity"] * position["last_close"] * (1-fee-tax)
                                      for position in positions.values()), Decimal(0)))
        curve.append({"as_of": frame["as_of"], "equity": float(equity), "cash": float(cash), "buy_cost": float(spent),
                      "position_value": float(position_value), "reserved_cash": float(reserved),
                      "gross_buy_amount": float(day_buy), "gross_sell_amount": float(day_sell),
                      **({"protection_active": protection["active"]} if protection is not None else {})})
        pending = signals(policy, frame, entry_context={"held_symbols": sorted(positions),
                          "reserved_symbols": sorted(reserved_symbols)})
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
                       sell_delay_sessions=model["sell_delay_sessions"], adverse_buy=model["adverse_buy"],
                       entry_remainder="cancelled_after_observation", exit_remainder="retry_next_observation")
    coverage.update(supported=coverage["outside"] == 0 if support is not None else None,
                    reasons=sorted(coverage["reasons"]))
    activity = {"basis": "modelled_fills", "gross_buy_amount": float(gross_buy),
                "gross_sell_amount": float(gross_sell), "gross_amount": float(gross_buy + gross_sell),
                "trading_days": len(traded_days), "symbols": len(traded_symbols),
                "buy_fill_events": buy_fills, "sell_fill_events": sell_fills,
                "intervals": len(frames) - 1,
                "mean_daily_gross_amount": float((gross_buy + gross_sell) / (len(frames) - 1)),
                "average_exposure_pct": sum(point["position_value"] / point["equity"] * 100
                                            for point in curve[1:]) / (len(frames) - 1)}
    return {"return": float((equities[-1] / start - 1) * 100), "drawdown": float(-drawdown),
            "trading_activity": activity,
            "closed": closed, "half_returns": [float((midpoint / start - 1) * 100),
                                                float((equities[-1] / midpoint - 1) * 100)],
            "equity_curve": curve, "daily_log_returns": [math.log(float(after / before)) for before, after in zip(equities, equities[1:])],
            "final_state": {"as_of": frames[-1]["as_of"], "policy_id": identity, "cash": _decimal_text(cash),
                            "reserved_cash": _decimal_text(reserved), "positions": state_positions, "pending_entries": pending,
                            **({"account_protection": {key: _decimal_text(value) if isinstance(value, Decimal) else value
                                                      for key, value in protection.items()}} if protection is not None else {}),
                            **({"reserved_entries": deepcopy(reserved_entries)} if "reserved_entries" in (saved or {}) else {})},
            "diagnostics": diagnostics, "support": coverage}


def _portfolio_metrics(policy, frames, limits, *, initial_state=None, execution_model=None, support=None):
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
              "execution_stress_drawdown_pct": None, "diagnostics": {}, "support": {"supported": False},
              "trading_activity": None, "stress_trading_activity": None}
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
        normal = _replay(policy, frames, limits, model, states["normal"], support=support)
        stress = _replay(policy, frames, limits, model, states["stress"], stress=True, support=support)
        execution_stress = (_replay(policy, frames, limits, model, states["normal"], execution_stress=True)
                            if model["stress_fill_ratio"] is not None else None)
        result.update(valid=True, net_return_pct=normal["return"], stress_return_pct=stress["return"],
                      max_drawdown_pct=normal["drawdown"], stress_drawdown_pct=stress["drawdown"],
                      closed_trades=normal["closed"], half_returns=normal["half_returns"],
                      trading_activity=normal["trading_activity"], stress_trading_activity=stress["trading_activity"],
                      equity_curve=normal["equity_curve"], stress_equity_curve=stress["equity_curve"],
                      daily_log_returns=normal["daily_log_returns"], stress_daily_log_returns=stress["daily_log_returns"],
                      final_state=normal["final_state"], stress_final_state=stress["final_state"],
                      execution_stress_return_pct=execution_stress["return"] if execution_stress else None,
                      execution_stress_drawdown_pct=execution_stress["drawdown"] if execution_stress else None,
                      support={"supported": (normal["support"]["supported"] and stress["support"]["supported"]),
                               "normal": normal["support"], "cost_stress": stress["support"]},
                      diagnostics={"normal": normal["diagnostics"], "cost_stress": stress["diagnostics"],
                                   "execution_stress": execution_stress["diagnostics"] if execution_stress else None})
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError) as error:
        reason = str(error)
        result["error"] = reason if re.fullmatch(r"[a-z][a-z0-9_]*", reason) else "invalid_evaluation_input"
    return result


def portfolio_metrics(policy, frames, limits, *, initial_state=None, execution_model=None):
    """Evaluate a flat research model or a frozen execution_profile.

    A profile evaluates every fixed environment for both normal and stressed
    costs. The first scenario is a representative path, never a claim that an
    unknown fill rate is one. Adoption must use all scenario_results and
    promotion_supported. Coarse observed price/time/one-minute-volume ranges
    only describe applicability, not the probability of an execution outcome.
    For continuation pass {scenarios: {id: {normal: state, stress: state}}}; a
    single initial state instead gives every scenario the same starting book.
    Flat models remain explicit development assumptions, with no adoption proof.
    """
    profile = execution_model
    if not isinstance(profile, dict) or "scenarios" not in profile:
        result = _portfolio_metrics(policy, frames, limits, initial_state=initial_state, execution_model=profile)
        result.update(promotion_supported=False, scenario_results={})
        return result
    try:
        if type(profile.get("version")) is not int or profile["version"] != 1 or profile.get("status") not in {"estimated", "insufficient"}:
            raise ValueError("invalid_execution_profile")
        estimates = profile.get("estimates", {})
        for side in ("buy", "sell"):
            estimate = estimates.get(side + "_fill_ratio")
            if estimate is not None:
                _number(estimate, "invalid_execution_profile", minimum=0, maximum=1)
            elif profile["status"] == "estimated":
                raise ValueError("invalid_execution_profile")
        support = profile.get("support")
        if not isinstance(support, dict) or set(support) != {"buy", "sell"}:
            raise ValueError("invalid_execution_support")
        for bounds in support.values():
            if bounds is None:
                continue
            if not isinstance(bounds, dict):
                raise ValueError("invalid_execution_support")
            for key in ("price", "minute", "volume"):
                low = _number(bounds.get(key + "_min"), "invalid_execution_support", minimum=0)
                high = _number(bounds.get(key + "_max"), "invalid_execution_support", minimum=low)
                if key != "minute" and low <= 0 or key == "minute" and high >= 24 * 60:
                    raise ValueError("invalid_execution_support")
        scenarios = profile["scenarios"]
        if not isinstance(scenarios, list) or not 3 <= len(scenarios) <= 4:
            raise ValueError("invalid_execution_scenarios")
        ids = [item["id"] for item in scenarios]
        expected = {"partial_fill", "delayed_sell", "adverse_buy"}
        if profile["status"] == "estimated":
            expected.add("empirical")
        if len(set(ids)) != len(ids) or set(ids) != expected:
            raise ValueError("invalid_execution_scenarios")
        saved = initial_state.get("scenarios") if isinstance(initial_state, dict) and "scenarios" in initial_state else None
        if saved is not None and (not isinstance(saved, dict) or set(saved) != set(ids)):
            raise ValueError("invalid_initial_scenario_states")
        results = {}
        for scenario in scenarios:
            model = scenario["model"]
            if model.get("mode") != "observed_quotes":
                raise ValueError("invalid_execution_profile_mode")
            current = _portfolio_metrics(policy, frames, limits, initial_state=saved[scenario["id"]] if saved is not None else initial_state,
                                         execution_model=model, support=support)
            current["assumption"] = _text(scenario.get("assumption"), "invalid_execution_assumption", 200)
            results[scenario["id"]] = current
        result = deepcopy(results[ids[0]])
        reasons = {side + "_context_missing" for side in ("buy", "sell") if support[side] is None}
        if profile["status"] == "insufficient":
            reasons.add("insufficient_terminal_orders")
        for current in results.values():
            if not current["valid"]:
                reasons.add(current["error"])
            for side in ("normal", "cost_stress"):
                reasons.update(current["support"].get(side, {}).get("reasons", []))
        supported = not reasons and all(current["support"]["supported"] for current in results.values())
        valid = all(current["valid"] for current in results.values())
        result.update(valid=valid, error=next((current["error"] for current in results.values() if not current["valid"]), None),
                      scenario_results=results, representative_scenario=ids[0], promotion_supported=supported,
                      support={"supported": supported, "reasons": sorted(reasons)}, execution_profile_status=profile["status"])
        return result
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError) as error:
        result = _portfolio_metrics(policy, [], limits)
        reason = str(error)
        result.update(error=reason if re.fullmatch(r"[a-z][a-z0-9_]*", reason) else "invalid_execution_profile",
                      promotion_supported=False, scenario_results={}, support={"supported": False, "reasons": ["invalid_execution_profile"]})
        return result
