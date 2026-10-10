"""Parse user-visible Timefolio grids and their UI CSV exports, without I/O.

The collector pairs a complete, unfiltered grid with its CSV export and verifies
that DOM row identities/order stayed unchanged across export (``complete`` /
``stable``). Live prices and fill counters may change during that observation.
CSV primitive values retain precision; display percentages and progress bars do
not. Cell IDs expose the grid's real row.Id; the '#' column is only a row number.
"""

from __future__ import annotations

import csv
import io
import math
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit


class SnapshotError(ValueError):
    pass


def _text(value):
    return str(value.get("text", "") if isinstance(value, dict) else value or "").strip()


def _number(value, *, blank=None):
    text = _text(value).replace(",", "").replace("%", "").strip()
    if not text:
        return blank
    try:
        result = float(text)
    except (TypeError, ValueError) as exc:
        raise SnapshotError("숫자 형식 확인 실패") from exc
    if not math.isfinite(result):
        raise SnapshotError("유한한 숫자가 아님")
    return result


def _symbol(value):
    value = _text(value)
    if not re.fullmatch(r"A?\d{6}", value):
        raise SnapshotError("종목코드 확인 실패")
    return value.removeprefix("A")


def _same_number(left, right):
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
        return a.is_finite() and b.is_finite() and a == b
    except (InvalidOperation, ValueError):
        return False


def rejection_reason(messages):
    text = " / ".join(dict.fromkeys(str(m).strip() for m in messages if str(m).strip()))
    text = re.sub(r"https?://\S+|[\w.+-]+@[\w.-]+", "[비공개]", text)
    text = re.sub(r"(?i)(?:token|secret|password|계좌|이메일)\s*[:=]\s*\S+", "[비공개]", text)
    text = re.sub(r"(?<!\d)\d[\d-]{7,}(?!\d)", "[비공개]", text)
    return "destination_rejected: " + " ".join(text.split())[:240]


def _row_id(cells):
    ids = []
    for column, cell in cells.items():
        if column.startswith(" ") or not isinstance(cell, dict):
            continue
        cell_id = str(cell.get("id", ""))
        suffix = "_" + column
        if cell_id.startswith("cell") and cell_id.endswith(suffix):
            candidate = cell_id[4:-len(suffix)]
            if re.fullmatch(r"[A-Za-z0-9-]+", candidate):
                ids.append(candidate)
    if len(ids) < 2 or len(set(ids)) != 1:
        raise SnapshotError("서로 일치하는 주문 셀 ID가 없음")
    return ids[0]


def _table(table):
    if table.get("complete") is not True or table.get("stable") is not True or table.get("filtered"):
        raise SnapshotError("전체 행·내보내기 전후 일치 확인 필요")
    headers = table.get("headers", [])
    if not headers or not all(isinstance(h, dict) and h.get("id") for h in headers):
        raise SnapshotError("원본 컬럼 ID가 없음")
    columns = [str(h["id"]) for h in headers]
    if len(set(columns)) != len(columns):
        raise SnapshotError("중복 컬럼 ID")
    records = [row for row in csv.reader(io.StringIO(str(table.get("csv_text", "")).lstrip("\ufeff"))) if row]
    if not records or records[0] != [_text(h) for h in headers]:
        raise SnapshotError("CSV와 DOM 헤더 불일치")
    if any(len(row) != len(columns) for row in records[1:]):
        raise SnapshotError("CSV 컬럼 수 불일치")
    rows = table.get("rows", [])
    if len(records) - 1 != len(rows):
        raise SnapshotError("CSV와 DOM 행 수 불일치")
    result = []
    for values, dom in zip(records[1:], rows):
        record = dict(zip(columns, values))
        cells = dom.get("cells", {})
        for column in ("prodId", "secCd"):
            if column in record and _text(record[column]) != _text(cells.get(column)):
                raise SnapshotError("CSV와 DOM 종목·섹터 행 순서 불일치")
        record["_cells"] = cells
        record["_id"] = _row_id(cells)
        record["_parent_symbol"] = table.get("parent_symbol")
        record["_kind"] = table.get("kind")
        result.append(record)
    if len({r["_id"] for r in result}) != len(result):
        raise SnapshotError("중복 그리드 행 ID")
    return result


def _side(value):
    side = {"1": "buy", "-1": "sell", "매수": "buy", "매도": "sell"}.get(_text(value))
    if side is None:
        raise SnapshotError("매수·매도 구분 확인 실패")
    return side


def _status(state, cancelled):
    if cancelled or state == "Deleted":
        return "cancelled"
    if state == "Done":
        return "completed"
    if state in {"Accepted", "Working"}:
        return "working"
    return "unknown"


def parse_receipts(dom_payload, account_key, contest):
    """Read order receipts without holdings or investment-rule checks."""
    return parse_snapshot(dom_payload, (), account_key, contest, receipts_only=True)


def parse_snapshot(dom_payload, symbols, account_key, contest, *, receipts_only=False):
    """Return normalized evidence, failing closed on missing or ambiguous data.

    sellable_weight is the gross holding before pending-sell reservations, which
    the planner deducts exactly once. Unknown reservations isolate their symbol.
    filled_quantity is exact CSV quantity, but filled_weight remains unknown
    without an exact accepted order quantity. ``completed`` means terminal Done,
    not necessarily fully filled. Never infer zero fill from a missing value.
    """
    errors = []
    result = {"verified": False, "account_key": dom_payload.get("account_key"),
              "contest": dom_payload.get("contest"), "session_date": dom_payload.get("session_date"),
              "positions": {}, "pending_orders": [], "orders": [],
              "unresolved_symbols": [],
              "errors": errors}
    try:
        url = urlsplit(str(dom_payload.get("url", "")))
        if url.scheme != "https" or url.netloc != "contest.timefolio.net":
            raise SnapshotError("대회 사이트 출처 불일치")
        if not account_key or dom_payload.get("account_key") != account_key:
            raise SnapshotError("선택된 운용 계좌 불일치")
        if not contest or dom_payload.get("contest") != contest:
            raise SnapshotError("선택된 대회 불일치")
        date.fromisoformat(str(dom_payload.get("session_date", "")))
        requested = {_symbol(s) for s in symbols}
    except (ValueError, TypeError) as exc:
        errors.append(str(exc))
        return result

    tables = {}
    required = {"orders"} if receipts_only else {"positions", "targets", "orders"}
    for table in dom_payload.get("tables", []):
        kind = table.get("kind")
        if kind not in required | {"order_details", "unaccepted"}:
            continue
        try:
            records = _table(table)
            if kind in tables and kind not in {"order_details", "unaccepted"}:
                raise SnapshotError("같은 종류의 표가 중복됨")
            tables.setdefault(kind, []).extend(records)
        except (SnapshotError, TypeError, KeyError) as exc:
            errors.append(f"{kind}: {exc}")
    for kind in required:
        if kind not in tables:
            errors.append(f"{kind}: 전체 표 없음")

    # A row number or a rounded progress bar is never ID/fill evidence.
    detail_by_id = {}
    for row in tables.get("unaccepted", []) + tables.get("order_details", []):
        previous = detail_by_id.get(row["_id"])
        if previous:
            # Generated orders can appear in both the error grid and target
            # details. Later details supply state/quantity; keep visible errors.
            if (previous["_kind"] == row["_kind"] or
                    _symbol(previous.get("prodId") or previous.get("_parent_symbol")) !=
                    _symbol(row.get("prodId") or row.get("_parent_symbol"))):
                errors.append("주문 상태 표의 ID 중복")
            cells = {**previous["_cells"], **row["_cells"]}
            if previous["_cells"].get("state", {}).get("errors"):
                cells["state"] = {**cells.get("state", {}), "errors": previous["_cells"]["state"]["errors"]}
            row = {**previous, **row, "_cells": cells}
        detail_by_id[row["_id"]] = row
    for row in tables.get("orders", []):
        try:
            symbol = _symbol(row.get("prodId"))
            side = _side(row.get("sgn"))
            weight = _number(row.get("wei"))
            quantity = _number(row.get("cumQty"))
            if weight is None or weight <= 0 or quantity is None or quantity < 0 or quantity != int(quantity):
                raise SnapshotError("주문 비중·체결 수량 확인 실패")
            detail = detail_by_id.get(row["_id"])
            if detail and _symbol(detail.get("prodId") or detail.get("_parent_symbol")) != symbol:
                raise SnapshotError("주문 상태의 부모 종목 불일치")
            detail_quantity = _number(detail.get("cumQty")) if detail else None
            state = detail.get("state", "") if detail else ""
            status = _status(state, bool(row.get("cnclT")))
            rejection = detail.get("_cells", {}).get("state", {}).get("errors", []) if detail else []
            if state == "Generated" and rejection:
                status = "rejected"
            previous = dom_payload.get("known_receipts", {}).get(row["_id"], {})
            if not detail and status == "unknown" and (
                previous.get("verified") is True
                and previous.get("order_id") == row["_id"]
                and previous.get("status") in {"completed", "filled", "cancelled", "rejected"}
                and previous.get("symbol") == symbol and previous.get("side") == side
                and _same_number(previous.get("weight"), weight) and previous.get("filled_quantity") == quantity
            ):
                status = previous["status"]
            # History is collected later; a fill between exports is expected.
            if detail_quantity is not None and detail_quantity > quantity:
                status = "unknown"
            known = status != "unknown"
            order = {"verified": known, "order_id": row["_id"], "symbol": symbol,
                     "side": side, "weight": weight, "filled_weight": None,
                     "filled_quantity": int(quantity), "status": status,
                     "created_at": row.get("genT"), "start_time": row.get("hm0"),
                     "limit_price": _number(row.get("limitPrc")),
                     "terminal": status in {"completed", "filled", "cancelled", "rejected"}}
            result["orders"].append(order)
            if status == "rejected" and rejection:
                order["reason"] = rejection_reason(rejection)
        except (SnapshotError, TypeError) as exc:
            errors.append(f"order: {exc}")
    if receipts_only:
        result["verified"] = not errors
        return result

    try:
        positions, target_map = {}, {}
        for row in tables.get("positions", []):
            symbol = _symbol(row.get("prodId"))
            weight = _number(row.get("wei"))
            if symbol in positions or weight is None or weight < 0:
                raise SnapshotError("보유 비중 또는 중복 종목 확인 실패")
            positions[symbol] = weight
        for row in tables.get("targets", []):
            symbol = _symbol(row.get("prodId"))
            if symbol in target_map:
                raise SnapshotError("중복 주문 타겟 종목")
            target_map[symbol] = row
        active = {}
        for order in result["orders"]:
            current_unknown = order["status"] == "unknown" and (
                order["order_id"] in detail_by_id
                or str(order.get("created_at") or "").startswith(result["session_date"]))
            if order["status"] == "working" or current_unknown:
                active.setdefault(order["symbol"], []).append(order)
        unresolved = set(active) - target_map.keys()
        for symbol, row in target_map.items():
            remaining = _number(row.get("w2o"), blank=0)
            orders = active.get(symbol, [])
            sides = {o["side"] for o in orders}
            if (len(sides) > 1 or (not sides and remaining)
                    or any(o["status"] == "unknown" for o in orders)):
                unresolved.add(symbol)
                continue
            if sides:
                side = next(iter(sides))
                if (side == "buy" and remaining < 0) or (side == "sell" and remaining > 0):
                    unresolved.add(symbol)
                    continue
                if remaining:
                    result["pending_orders"].append({"symbol": symbol, "side": side,
                                                      "weight": abs(remaining)})
        observed = {o["order_id"] for o in result["orders"]}
        for identity, row in detail_by_id.items():
            if identity not in observed:
                unresolved.add(_symbol(row.get("prodId") or row.get("_parent_symbol")))
        result["unresolved_symbols"] = sorted(unresolved)
        for symbol in set(positions) | requested:
            weight = positions.get(symbol, 0)
            result["positions"][symbol] = {"weight": weight,
                                            "sellable_weight": weight}
    except (SnapshotError, TypeError, KeyError) as exc:
        errors.append(str(exc))
    result["verified"] = not errors
    return result
