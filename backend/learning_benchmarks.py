"""Recover market comparisons without rewriting frozen learning observations."""
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math


class BenchmarkError(ValueError):
    pass


def _day(value):
    if type(value) is date:
        return value.isoformat()
    if not isinstance(value, str):
        raise ValueError("invalid_date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("invalid_date")
    return value


def _price(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid_price")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or not math.isfinite(float(number)) or float(number) <= 0:
            raise ValueError("invalid_price")
        return number
    except (InvalidOperation, OverflowError):
        raise ValueError("invalid_price") from None


def _token(value):
    return str(value) if isinstance(value, (str, int, float, Decimal, date)) else type(value).__name__


def _observation(data, board, day):
    benchmarks = data.get("benchmarks", {})
    if not isinstance(benchmarks, dict):
        return {"status": "invalid", "values": [_token(benchmarks)]}
    series = benchmarks.get(board, {})
    if not isinstance(series, dict):
        return {"status": "invalid", "values": [_token(series)]}
    values, invalid = set(), []
    for key, value in series.items():
        try:
            normalized = _day(key)
        except ValueError:
            invalid.append("date:" + _token(key))
            continue
        if normalized == day:
            try:
                values.add(str(_price(value).normalize()))
            except ValueError:
                invalid.append("price:" + _token(value))
    status = "invalid" if invalid else "conflict" if len(values) > 1 else "ok" if values else "missing"
    return {"status": status, "values": sorted(values), "invalid": sorted(invalid)}


def benchmark_signature(data, start_day=None):
    """Track exact-day evidence changes, including missing or conflicting aliases."""
    data = data if isinstance(data, dict) else {}
    days = []
    for value in (data.get("as_of"), start_day):
        if value is None:
            continue
        try:
            days.append(_day(value))
        except ValueError:
            days.append("invalid:" + _token(value))
    payload = {day: {board: _observation(data, board, day) for board in ("KOSPI", "KOSDAQ")}
               for day in sorted(set(days))}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _resolve(db, day, data, at, expected=None):
    source = _observation(data, "KOSPI", day)
    if source["status"] in {"invalid", "conflict"}:
        reason = "서로 충돌합니다" if source["status"] == "conflict" else "올바르지 않습니다"
        raise BenchmarkError(f"KOSPI {day} 지수 자료가 {reason}.")
    source_price = Decimal(source["values"][0]) if source["status"] == "ok" else None
    row = db.execute("SELECT digest,payload FROM learning_frames WHERE day=?", (day,)).fetchone()
    if row is None:
        raise BenchmarkError(f"KOSPI {day}의 저장된 학습 관측이 없습니다.")
    digest, raw = row
    try:
        frame = json.loads(raw)
        frozen = frame.get("benchmark_prices", {}).get("KOSPI")
        frozen = _price(frozen) if frozen is not None else None
    except (ValueError, TypeError, AttributeError):
        raise BenchmarkError(f"KOSPI {day}의 저장된 지수 자료가 올바르지 않습니다.") from None
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_frame_benchmarks'").fetchone()
    supplemental = db.execute("SELECT frame_digest,payload FROM learning_frame_benchmarks "
                              "WHERE day=? AND board='KOSPI'", (day,)).fetchone() if exists else None
    supplement_price = None
    if supplemental:
        try:
            if supplemental[0] != digest:
                raise ValueError("frame_changed")
            supplement_price = _price(json.loads(supplemental[1])["price"])
        except (ValueError, TypeError, KeyError):
            raise BenchmarkError(f"KOSPI {day}의 보강 근거가 저장된 학습 관측과 일치하지 않습니다.") from None
    prices = [value for value in (frozen, supplement_price, source_price, expected) if value is not None]
    if any(value != prices[0] for value in prices[1:]):
        raise BenchmarkError(f"KOSPI {day} 지수값이 고정된 비교 근거와 충돌합니다.")
    if frozen is not None or supplement_price is not None:
        return frozen if frozen is not None else supplement_price, False
    if source_price is None:
        raise BenchmarkError(f"KOSPI {day} 지수값이 없어 시장 비교를 기다립니다.")
    payload = {"price": str(source_price), "source_input_hash": benchmark_signature(data, day),
               "source_as_of": _day(data["as_of"]),
               "observed_at": data.get("observed_at"), "recovered_at": at}
    db.execute("CREATE TABLE IF NOT EXISTS learning_frame_benchmarks "
               "(day TEXT NOT NULL,board TEXT NOT NULL,frame_digest TEXT NOT NULL,payload TEXT NOT NULL,"
               "PRIMARY KEY(day,board))")
    changed = db.execute("INSERT OR IGNORE INTO learning_frame_benchmarks VALUES (?,?,?,?)",
                         (day, "KOSPI", digest, json.dumps(payload, sort_keys=True, ensure_ascii=False))).rowcount > 0
    # Recheck an immutable record if another worker inserted it first.
    saved = db.execute("SELECT frame_digest,payload FROM learning_frame_benchmarks WHERE day=? AND board='KOSPI'",
                       (day,)).fetchone()
    if saved[0] != digest or _price(json.loads(saved[1])["price"]) != source_price:
        raise BenchmarkError(f"KOSPI {day} 지수값이 고정된 비교 근거와 충돌합니다.")
    return source_price, changed


def refresh_market_comparison(store, state, data, at):
    """Patch derived state; only immutable benchmark supplements are written here."""
    if not state.get("scorecard_start") or not state.get("scorecard_day") or not isinstance(state.get("scorecard"), dict):
        return False
    before = deepcopy(state)
    score, previous_error = state["scorecard"], state.get("scorecard_market_error")
    inserted, error = False, None
    try:
        try:
            start, current, as_of = (_day(value) for value in (
                state["scorecard_start"], state["scorecard_day"], data.get("as_of")))
            if start > current or current > as_of:
                raise ValueError("future_date")
            anchor = _price(state["scorecard_market"]) if state.get("scorecard_market") is not None else None
        except (ValueError, TypeError, AttributeError):
            raise BenchmarkError("시장 비교 기준일·현재일 또는 기준 지수값을 확인할 수 없습니다.") from None
        with store.connect() as db:
            first, added = _resolve(db, start, data, at, expected=anchor)
            inserted |= added
            if current == start:
                latest = first
            else:
                latest, added = _resolve(db, current, data, at)
                inserted |= added
        result = float((latest / first - 1) * 100)
        if not math.isfinite(result):
            raise BenchmarkError("시장 비교 수익률을 계산할 수 없습니다.")
        if anchor is None:
            state["scorecard_market"] = str(first)
        score["market_return_pct"] = result
    except BenchmarkError as failure:
        inserted = False  # The store context rolls back a partial recovery.
        error = str(failure)
        score["market_return_pct"] = None
    state["scorecard_market_error"] = error
    if score.get("error") is None or score.get("error") == previous_error:
        score["error"] = error
    return inserted or state != before
