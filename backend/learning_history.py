"""Read archived daily research prices for development-only policy selection.

This is not prospective evidence: today's surviving universe and eligibility,
and a later vintage of adjusted history, can bias these samples. Never persist
the returned frames as the independent forward cohort used to adopt a policy.
No live cache, account, LLM, or network is read or started by this module.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from backend.learning_policy import build_frame


STATUSES = {"complete", "ready", "ok"}
INDICES = {"KOSPI": "0001", "KOSDAQ": "1001"}
FIELDS = ("open", "high", "low", "close", "volume", "turnover")


def _json(path):
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("oversized_research_file")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("invalid_research_file")
    return value


def _day(value):
    if type(value) is date:
        return value.isoformat()
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("invalid_research_date")
    return date.fromisoformat(value).isoformat()


def _instant(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("research_timestamp_requires_timezone")
    return stamp.astimezone(timezone.utc)


def _header(directory, observed):
    manifest, progress = _json(directory / "manifest.json"), _json(directory / "progress.json")
    if (type(manifest.get("version")) is not int or manifest["version"] != 1
            or type(progress.get("version")) is not int or progress["version"] != 1
            or progress.get("status") not in STATUSES or progress.get("error")
            or manifest.get("environment") != "paper" or manifest.get("period") != "D"
            or manifest.get("adjusted") is not True or manifest.get("FID_ORG_ADJ_PRC") != "0"):
        raise ValueError("unsupported_research_collection")
    stamp = _instant(progress.get("finished_at") or progress.get("updated_at") or progress.get("started_at"))
    if stamp > observed:
        raise ValueError("future_collection")
    start, end = _day(manifest.get("requested_start")), _day(manifest.get("requested_end"))
    if start > end:
        raise ValueError("invalid_collection_range")
    universe_path = directory / "universe-snapshot.json"
    universe = _json(universe_path)
    digest = hashlib.sha256(universe_path.read_bytes()).hexdigest()
    if (manifest.get("universe_sha256") != digest or universe.get("status") != "verified"
            or not isinstance(universe.get("rows"), list)):
        raise ValueError("invalid_collection_universe")
    rows = {}
    for item in universe["rows"]:
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{6}", symbol) or symbol in rows or item.get("board") not in INDICES:
            raise ValueError("invalid_collection_symbol")
        rows[symbol] = item["board"]
    if type(manifest.get("stock_count")) is not int or len(rows) != manifest["stock_count"]:
        raise ValueError("collection_count_mismatch")
    return manifest, rows, stamp


def _series(path, *, manifest, observed, cutoff, kind, symbol, board):
    value = _json(path)
    if (type(value.get("version")) is not int or value["version"] != 1
            or value.get("status") not in STATUSES or value.get("error") or value.get("conflicts")
            or value.get("kind") != kind or value.get("symbol") != symbol or value.get("board") != board
            or any(value.get(key) != manifest[key] for key in ("universe_sha256", "requested_start", "requested_end"))
            or _instant(value.get("collected_at")) > observed or not isinstance(value.get("rows"), list)):
        raise ValueError("research_series_mismatch")
    result = {}
    for raw in value["rows"]:
        day = _day(raw.get("date"))
        if day > cutoff:
            continue
        if not manifest["requested_start"] <= day <= manifest["requested_end"]:
            raise ValueError("research_row_outside_collection")
        bar = {key: raw.get(key) for key in FIELDS}
        if day in result and result[day] != bar:
            raise ValueError("conflicting_daily_rows")
        result[day] = bar
    return result


def _same(left, right, fields):
    try:
        pairs = [(Decimal(str(left[key])), Decimal(str(right[key]))) for key in fields]
        return all(a.is_finite() and b.is_finite() and a == b for a, b in pairs)
    except (InvalidOperation, ValueError, KeyError, TypeError):
        return False


def _compatible(old, recent, fields):
    overlap = old.keys() & recent.keys()
    return bool(overlap) and all(_same(old[day], recent[day], fields) for day in overlap)


def _frames(directory, manifest, archived_rows, collected, current, current_data, observed, cutoff, limit):
    rows = [item for symbol, item in sorted(current.items()) if archived_rows.get(symbol) == item["board"]]
    if not rows:
        return []
    histories = {item["symbol"]: _series(directory / "series" / (item["symbol"] + ".json"),
                     manifest=manifest, observed=observed, cutoff=min(cutoff, manifest["requested_end"]), kind="stock",
                     symbol=item["symbol"], board=item["board"]) for item in rows}
    indices = {board: _series(directory / "indices" / (symbol + ".json"), manifest=manifest,
                             observed=observed, cutoff=min(cutoff, manifest["requested_end"]), kind="index", symbol=symbol, board=board)
               for board, symbol in INDICES.items()}
    replacements = 0
    for symbol, old in histories.items():
        recent = {_day(day): {key: bar.get(key) for key in FIELDS}
                  for day, bar in current_data.get("histories", {}).get(symbol, {}).items() if _day(day) <= cutoff}
        if recent:
            if _compatible(old, recent, FIELDS):
                old.update(recent)
            else:
                # A revised adjustment vintage replaces the entire symbol.
                histories[symbol] = recent
                replacements += 1
    recent_indices = {board: {_day(day): {"close": value.get("close") if isinstance(value, dict) else value}
                             for day, value in current_data.get("benchmarks", {}).get(board, {}).items() if _day(day) <= cutoff}
                      for board in INDICES}
    replace_indices = any(recent_indices[board] and not _compatible(indices[board], recent_indices[board], ("close",))
                          for board in INDICES)
    for board in INDICES:
        if replace_indices:
            indices[board] = recent_indices[board]
        else:
            indices[board].update(recent_indices[board])
        expected = {_day(day) for day in current_data.get("calendars", {}).get(board, []) if _day(day) <= cutoff}
        if expected - indices[board].keys():
            raise ValueError("current_index_session_missing")
    calendars = {board: sorted(values) for board, values in indices.items()}
    # Both recorded benchmark calendars must agree; never invent a missing day.
    if calendars["KOSPI"] != calendars["KOSDAQ"]:
        raise ValueError("research_calendars_differ")
    days = calendars["KOSPI"]
    if len(days) < 60:
        return []
    provenance = {"source": ".local/research/" + directory.name,
                  "collected_at": collected.isoformat(), "universe_sha256": manifest["universe_sha256"],
                  "through": days[-1], "current_series_replacements": replacements,
                  "universe_warning": "current_universe_and_eligibility; later_adjusted_history; development_only"}
    result = []
    for position in range(max(59, len(days) - limit), len(days)):
        day = days[position]
        lookback = days[max(0, position - 62):position + 1]
        # Each build sees at most 63 sessions, ending at that simulated close.
        eligible_rows = [{**item, "learning_eligible": item["learning_eligible"]
                          and all(value in histories[item["symbol"]] for value in lookback[-60:])}
                         for item in rows]
        data = {"as_of": day, "observed_at": day + "T16:00:00+09:00", "rows": eligible_rows,
                "histories": {symbol: {value: series[value] for value in lookback if value in series}
                              for symbol, series in histories.items()},
                "calendars": {board: lookback for board in INDICES},
                "benchmarks": {board: {value: series[value]["close"] for value in lookback}
                               for board, series in indices.items()}}
        frame = build_frame(data)
        if not frame["rows"] and not result:
            continue
        frame.update(role="development_only", provenance=dict(provenance))
        result.append(frame)
    return result


def development_frames(root: Path, current_data, *, limit=120):
    """Return the latest compatible archived collection, or [] without side effects.

    Only the intersection with current_data.rows is used. Its public name, board,
    and current eligibility are metadata, not proof of historical eligibility.
    All rows end at current_data.as_of and all file vintages must predate its
    observed_at. Only the already supplied current_data may extend the collection;
    conflicting adjustment vintages replace a whole series, never a price prefix.
    """
    try:
        if type(limit) is not int or not 1 <= limit <= 300 or not isinstance(current_data, dict):
            return []
        cutoff, observed = _day(current_data.get("as_of")), _instant(current_data.get("observed_at"))
        current = {}
        for item in current_data["rows"]:
            symbol = item.get("symbol")
            if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{6}", symbol) or item.get("board") not in INDICES:
                continue
            if symbol in current:
                return []
            current[symbol] = {"symbol": symbol, "name": str(item.get("name", symbol)), "board": item["board"],
                               "learning_eligible": item.get("learning_eligible", True) is True}
        candidates = []
        for directory in (Path(root) / ".local" / "research").glob("candidate-screen-*"):
            if directory.is_dir():
                try:
                    manifest, rows, collected = _header(directory, observed)
                    candidates.append((collected, directory, manifest, rows))
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    continue
        for collected, directory, manifest, rows in sorted(candidates, key=lambda item: (item[0], item[1].name), reverse=True):
            try:
                frames = _frames(directory, manifest, rows, collected, current, current_data, observed, cutoff, limit)
                if len(frames) >= 2:
                    return frames
            except (OSError, ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
                continue
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return []
