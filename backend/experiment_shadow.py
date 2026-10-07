"""Allocation-free shadow outcomes for distinct paper-experiment entry signals.

Signal-day means contain completed events only; overlapping events are allowed.
Unresolved/unknown/excluded counts remain separate, never imputed as zero return.
Original observed prices and the first finalized outcome are retained on revision.
"""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json

KST = timezone(timedelta(hours=9))
FEE = Decimal("0.000140527")
SELL_TAX = Decimal("0.002")
SLIPPAGES = (Decimal("0.001"), Decimal("0.002"))
PRICE_FIELDS = ("open", "high", "low", "close", "volume", "turnover")


def _day(value):
    return value.isoformat() if type(value) is date else date.fromisoformat(value).isoformat()


def _time(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Unverified observation timestamp")
    return parsed.astimezone(timezone.utc)


def _text(number):
    value = format(number, "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _bar(value):
    try:
        if not isinstance(value, dict):
            return None
        result = {key: Decimal(str(value[key])) for key in PRICE_FIELDS}
        if (any(not number.is_finite() or abs(number.adjusted()) > 30 for number in result.values())
                or min(result[key] for key in PRICE_FIELDS[:4]) <= 0
                or result["volume"] < 0 or result["turnover"] < 0
                or not result["low"] <= min(result["open"], result["close"]) <= max(result["open"], result["close"]) <= result["high"]):
            return None
        return {key: _text(number) for key, number in result.items()}
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return None


def _unknown(record, reason):
    if record["status"] != "unknown":
        record.update(status="unknown", reason=reason, net_pct=None, stress_pct=None)


def _prices(histories, symbol, as_of):
    raw = histories.get(symbol, {})
    if not isinstance(raw, dict):
        return {}
    return {_day(day): value for day, value in raw.items() if _day(day) <= as_of}


def _signal_key(strategy, symbol, day):
    return hashlib.sha256(json.dumps([strategy, symbol, day], separators=(",", ":")).encode()).hexdigest()


def _new_record(run, decision):
    day = _day(decision.get("as_of") or run["as_of"])
    analysis = run.get("analysis") or {}
    frozen = analysis.get("input") or {}
    raw_calendar = frozen.get("calendars", {}).get(decision["board"], [])
    calendar = [_day(value) for value in raw_calendar if _day(value) <= day]
    if calendar != sorted(set(calendar)):
        calendar = []
    original = frozen.get("histories", {}).get(decision["symbol"], {}).get(day)
    original = _bar(original) if original else None
    record = {"run_id": run.get("id"), "version_id": run.get("version_id"),
              "strategy_id": decision["strategy_id"], "symbol": decision["symbol"],
              "board": decision["board"], "signal_date": day, "created_at": run.get("created_at"),
              "status": "pending", "reason": "awaiting_next_session", "calendar": calendar,
              "basis": {day: original} if original else {}, "entry_date": None, "exit_date": None,
              "net_pct": None, "stress_pct": None, "first_closed": None}
    try:
        if _time(run.get("created_at")) < datetime.combine(date.fromisoformat(day), time(16), KST):
            record.update(status="excluded", reason="observed_before_close")
    except (ValueError, TypeError):
        _unknown(record, "invalid_observed_at")
    return record


def _update_record(record, calendars, histories, as_of):
    if record["status"] in {"unknown", "excluded"}:
        return
    series = _prices(histories, record["symbol"], as_of)
    available = sorted(series)
    # Verify original observed prices whenever their dates are still covered.
    # Rolled-off history can use the sealed originals; an interior gap cannot.
    for day, original in record["basis"].items():
        if day in series:
            current = _bar(series[day])
            if current != original:
                _unknown(record, "price_revision" if current else "invalid_bar")
                return
        elif available and available[0] <= day <= available[-1]:
            _unknown(record, "missing_bar")
            return
    if record["status"] == "closed":
        return
    current = calendars.get(record["board"], [])
    old = record["calendar"]
    if not current or current[-1] != as_of:
        _unknown(record, "missing_calendar")
        return
    if old:
        overlap = set(old).intersection(current)
        if not overlap:
            _unknown(record, "calendar_gap")
            return
        if any(current[0] <= day <= current[-1] and day not in current for day in old):
            _unknown(record, "calendar_revision")
            return
    calendar = sorted(set(old).union(current))
    record["calendar"] = calendar
    if record["signal_date"] not in calendar:
        _unknown(record, "signal_date_not_in_calendar")
        return
    position = calendar.index(record["signal_date"]) + 1
    if position >= len(calendar):
        record.update(status="pending", reason="awaiting_next_session")
        return
    entry_day = calendar[position]
    if _time(record["created_at"]) >= datetime.combine(date.fromisoformat(entry_day), time(9), KST):
        record.update(status="excluded", reason="late_signal")
        return
    record["entry_date"] = entry_day
    exit_index = position + 5
    reached = min(exit_index + 1, len(calendar))
    for day in calendar[position:reached]:
        original = record["basis"].get(day)
        current_bar = _bar(series[day]) if day in series else None
        if current_bar is None and original is None:
            _unknown(record, "missing_bar" if day not in series else "invalid_bar")
            return
        bar = original or current_bar
        if Decimal(bar["volume"]) == 0 or bar["high"] == bar["low"]:
            _unknown(record, "fill_unverifiable")
            return
        record["basis"].setdefault(day, bar)
    if exit_index >= len(calendar):
        record.update(status="open", reason="holding")
        return
    exit_day = calendar[exit_index]
    entry = Decimal(record["basis"][entry_day]["open"])
    exit_price = Decimal(record["basis"][exit_day]["open"])
    returns = [((exit_price * (1 - slip) * (1 - FEE - SELL_TAX)) /
                (entry * (1 + slip) * (1 + FEE)) - 1) * 100 for slip in SLIPPAGES]
    record.update(status="closed", reason=None, exit_date=exit_day,
                  net_pct=_text(returns[0]), stress_pct=_text(returns[1]))
    record["first_closed"] = {"entry_date": entry_day, "exit_date": exit_day,
                              "entry_open": _text(entry), "exit_open": _text(exit_price),
                              "net_pct": record["net_pct"], "stress_pct": record["stress_pct"]}


def update_shadow(store, runs, histories, calendars, as_of):
    """Update durable originals and return per-strategy event-return diagnostics.

    ``runs`` use ``signals`` and actual post-analysis ``created_at``. Current
    histories/calendars accept date objects or ISO keys. Futures beyond ``as_of``
    are never used. The first ready decision for (strategy,symbol,signal day) wins,
    including hold/avoid; reruns cannot replace it with a more favorable decision.
    """
    as_of = _day(as_of)
    normalized = {}
    for board, raw in calendars.items():
        days = [_day(day) for day in raw if _day(day) <= as_of]
        if days != sorted(set(days)):
            raise ValueError("Invalid shadow calendar")
        normalized[board] = days
    state = store.setting("shadow", {"version": 1, "records": {}})
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("records"), dict):
        raise ValueError("Invalid saved shadow state")
    original = deepcopy(state)
    records = state["records"]
    decisions = state.setdefault("decisions", {})
    strategies = set()
    for run in sorted(runs, key=lambda row: (str(row.get("created_at", "")), str(row.get("id", "")))):
        for decision in run.get("signals", (run.get("analysis") or {}).get("decisions", [])):
            strategy = decision["strategy_id"]
            strategies.add(strategy)
            if decision.get("status") != "ready":
                continue
            day = _day(decision.get("as_of") or run["as_of"])
            if day > as_of:
                continue
            key = _signal_key(strategy, decision["symbol"], day)
            if key in decisions:
                continue
            decisions[key] = {"run_id": run.get("id"), "action": decision.get("action")}
            if decision.get("action") != "buy":
                if key in records:
                    records[key].update(status="excluded", reason="earlier_decision_not_buy", net_pct=None, stress_pct=None)
                continue
            if key not in records:
                records[key] = _new_record(run, decision)
    for record in records.values():
        strategies.add(record["strategy_id"])
        _update_record(record, normalized, histories, as_of)
    if state != original:
        store.save_setting("shadow", state)
    result = []
    for strategy in sorted(strategies):
        group = [record for record in records.values() if record["strategy_id"] == strategy]
        closed = [record for record in group if record["status"] == "closed"]
        def day_mean(field):
            days = {}
            for record in closed:
                days.setdefault(record["signal_date"], []).append(Decimal(record[field]))
            return _text(sum(sum(items) / len(items) for items in days.values()) / len(days)) if days else None
        result.append({"strategy_id": strategy, "shadow_signals": len(group),
                       **{f"shadow_{status}": sum(record["status"] == status for record in group)
                          for status in ("closed", "open", "unknown", "excluded", "pending")},
                       "shadow_net_pct": day_mean("net_pct"), "shadow_stress_pct": day_mean("stress_pct"),
                       "shadow_signal_days": len({record["signal_date"] for record in closed}),
                       "shadow_version_count": len({record["version_id"] for record in group}),
                       "aggregation": "signal_day_equal_mean", "completed_events_only": True,
                       "overlapping_signals": True})
    return result
