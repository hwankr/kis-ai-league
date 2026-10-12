"""Read-only Timefolio account observations, separate from order decisions."""
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3
import time

from backend.kis import KST
from backend.supervisor import _read, _write
from backend.timefolio_dom import _symbol, _table, parse_snapshot


INTERVAL = 60
STALE_SECONDS = 180
BINDING_KEYS = ("source_account", "source_fingerprint", "account_key", "contest")
ERRORS = {"login_required": "타임폴리오 로그인을 확인하세요.",
          "collection_failed": "타임폴리오 계좌 현황을 갱신하지 못했습니다.",
          "binding_changed": "계좌 변경 후 새 현황을 기다리고 있습니다.",
          "invalid_snapshot": "저장된 타임폴리오 계좌 현황을 확인하지 못했습니다."}


def _number(value, *, optional=False, nonnegative=False):
    if optional and (value is None or value == ""):
        return None
    if isinstance(value, bool) or value is None:
        raise ValueError("account_number_invalid")
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        raise ValueError("account_number_invalid") from None
    if not number.is_finite() or abs(number) > Decimal("1e30") or (nonnegative and number < 0):
        raise ValueError("account_number_invalid")
    return number


def _string(number):
    if number is None:
        return None
    value = format(number, "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _integer(value, *, optional=False):
    number = _number(value, optional=optional, nonnegative=True)
    if number is None:
        return None
    if number != number.to_integral_value() or number > 10**12:
        raise ValueError("account_integer_invalid")
    return int(number)


def _name(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 120 or re.search(r"[\x00-\x1f@]|https?://", value):
        raise ValueError("account_name_invalid")
    return value.strip() or None


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.utcoffset() is not None else None
    except ValueError:
        return None


def _nav(values):
    if not isinstance(values, list) or len(values) < 2:
        raise ValueError("account_nav_missing")
    match = re.fullmatch(r"\s*([+-]?[\d,]+(?:\.\d+)?)\s*\(\s*([+-]?[\d,]+(?:\.\d+)?)%\s*\)\s*", str(values[1]))
    if not match:
        raise ValueError("account_nav_invalid")
    rank = participants = None
    for value in values[2:]:
        ranked = re.fullmatch(r"\s*([\d,]+)위\s*/\s*([\d,]+)명\s*", str(value))
        if ranked:
            rank, participants = (_integer(part) for part in ranked.groups())
            if not 0 < rank <= participants:
                raise ValueError("account_rank_invalid")
    return {"nav": _string(_number(values[0], nonnegative=True)),
            "nav_change": _string(_number(match[1])), "nav_change_pct": _string(_number(match[2])),
            "rank": rank, "participants": participants}


def parse_account_snapshot(payload, account_key, contest):
    """Reuse complete CSV/DOM validation, retaining exact account display fields."""
    parsed = parse_snapshot(payload, (), account_key, contest)
    if parsed.get("verified") is not True:
        raise ValueError("account_snapshot_unverified")
    tables = {}
    for table in payload["tables"]:
        tables.setdefault(table["kind"], []).extend(_table(table))
    holdings = []
    for row in tables["positions"]:
        quantity = _integer(row.get("pos"))
        price = _number(row.get("close"), nonnegative=True)
        average = _number(row.get("avgPrc"), optional=True, nonnegative=True)
        market = price * quantity
        purchase = average * quantity if average is not None else None
        pnl = market - purchase if purchase is not None else None
        holdings.append({"symbol": _symbol(row.get("prodId")), "name": _name(row.get("prodNm")),
                         "quantity": quantity, "avg_price": _string(average), "price": _string(price),
                         "market_value": _string(market), "purchase_amount": _string(purchase), "pnl": _string(pnl),
                         "return_pct": _string(pnl / purchase * 100) if purchase else None,
                         "weight_pct": _string(_number(row.get("wei"), nonnegative=True)),
                         "change_pct": _string(_number(row.get("chPct"), optional=True))})
    holdings.sort(key=lambda row: (-Decimal(row["market_value"]), row["symbol"]))
    market = sum((Decimal(row["market_value"]) for row in holdings), Decimal(0))
    complete_cost = all(row["purchase_amount"] is not None for row in holdings)
    purchase = sum((Decimal(row["purchase_amount"]) for row in holdings), Decimal(0)) if complete_cost else None
    pnl = market - purchase if purchase is not None else None
    summary = {**_nav(payload.get("nav")), "holdings_value": _string(market), "holdings_pnl": _string(pnl),
               "holdings_return_pct": _string(pnl / purchase * 100) if purchase else None}
    history = {row["_id"]: row for row in tables.get("orders", [])}
    details = {row["_id"]: row for kind in ("unaccepted", "order_details") for row in tables.get(kind, [])}
    names = {row["symbol"]: row["name"] for row in holdings}
    orders = []
    for order in parsed["orders"]:
        row = history[order["order_id"]]
        detail = details.get(order["order_id"], {})
        orders.append({"order_id": order["order_id"], "symbol": order["symbol"],
                       "name": _name(row.get("prodNm")) or names.get(order["symbol"]),
                       "side": order["side"], "status": order["status"],
                       "weight": _string(_number(row.get("wei"), nonnegative=True)),
                       "filled_quantity": order["filled_quantity"], "created_at": order.get("created_at"),
                       "order_time": order.get("start_time"),
                       "average_price": _string(_number(detail.get("avgPx"), optional=True, nonnegative=True)),
                       "verified": order["verified"]})
    return {"verified": True, "account_key": account_key, "contest": contest,
            "session_date": parsed["session_date"], "summary": summary, "holdings": holdings, "orders": orders}


def _context(root):
    path = Path(root) / ".local" / "experiments" / "experiments.sqlite3"
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.execute("BEGIN")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='mirror_runtime'").fetchone():
            return None
        row = db.execute("SELECT payload FROM mirror_runtime WHERE id=1").fetchone()
        policy = db.execute("SELECT value FROM settings WHERE key='policy'").fetchone()
        if not row or not policy:
            return None
        binding, policy = json.loads(row[0]), json.loads(policy[0])
        if (not isinstance(binding, dict) or not isinstance(policy, dict)
                or not all(isinstance(binding.get(key), str) and binding[key] for key in BINDING_KEYS)
                or binding["source_account"] != policy.get("account_id")
                or binding["source_fingerprint"] != policy.get("fingerprint")):
            return None
        jobs = [json.loads(row[0]) for row in db.execute("SELECT payload FROM mirror_jobs")]
    return {"binding": {key: binding[key] for key in BINDING_KEYS}, "jobs": jobs}


def _saved(root):
    path = Path(root) / ".local" / "timefolio-account.json"
    if not path.exists():
        return {}
    with path.open("rb") as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("account_snapshot_too_large")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("account_snapshot_invalid")
    return value


def _public_data(snapshot, jobs):
    if not isinstance(snapshot, dict) or snapshot.get("verified") is not True:
        raise ValueError("account_snapshot_invalid")
    session_date = date.fromisoformat(snapshot["session_date"]).isoformat()
    summary = snapshot["summary"]
    clean_summary = {key: _string(_number(summary.get(key), optional=key in {"holdings_pnl", "holdings_return_pct"}))
                     for key in ("nav", "nav_change", "nav_change_pct", "holdings_value", "holdings_pnl", "holdings_return_pct")}
    clean_summary.update({key: _integer(summary.get(key), optional=True) for key in ("rank", "participants")})
    holdings = []
    for row in snapshot["holdings"]:
        clean = {key: _string(_number(row.get(key), optional=key in {"avg_price", "purchase_amount", "pnl", "return_pct", "change_pct"}))
                 for key in ("avg_price", "price", "market_value", "purchase_amount", "pnl", "return_pct", "weight_pct", "change_pct")}
        clean.update(symbol=_symbol(row.get("symbol")), name=_name(row.get("name")), quantity=_integer(row.get("quantity")))
        holdings.append(clean)
    owned = {(receipt.get("order_id"), receipt.get("symbol"), receipt.get("side"))
             for job in jobs if isinstance(receipt := job.get("receipt"), dict) and receipt.get("verified") is True}
    orders = []
    for row in snapshot["orders"]:
        identity = row.get("order_id")
        if (not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", identity)
                or row.get("side") not in {"buy", "sell"}
                or row.get("status") not in {"working", "filled", "completed", "rejected", "cancelled", "unknown"}
                or type(row.get("verified")) is not bool):
            raise ValueError("account_order_invalid")
        symbol = _symbol(row.get("symbol"))
        created = row.get("created_at")
        if created is not None and (not isinstance(created, str) or len(created) > 64 or
                                    not re.fullmatch(r"[\dT:.+Z /-]+", created)):
            raise ValueError("account_order_date_invalid")
        order_time = row.get("order_time")
        if order_time is not None and (not isinstance(order_time, str) or not re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", order_time)):
            raise ValueError("account_order_time_invalid")
        orders.append({"order_id": identity, "symbol": symbol, "name": _name(row.get("name")),
                       "side": row["side"], "status": row["status"], "verified": row["verified"],
                       "weight": _string(_number(row.get("weight"), nonnegative=True)),
                       "filled_quantity": _integer(row.get("filled_quantity")), "created_at": created,
                       "order_time": order_time, "average_price": _string(_number(row.get("average_price"), optional=True, nonnegative=True)),
                       "system_owned": (identity, symbol, row["side"]) in owned})
    orders.sort(key=lambda row: (row["created_at"] or "", row["order_id"]), reverse=True)
    return {"account": {"name": "타임폴리오", "contest": _name(snapshot["contest"]), "session_date": session_date},
            "summary": clean_summary, "holdings": holdings, "orders": orders}


class AccountCollector:
    def __init__(self, root, browser, *, now=None, monotonic=None):
        self.root, self.browser = Path(root), browser
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.next_at = 0

    def tick(self):
        if self.monotonic() < self.next_at:
            return
        self.next_at = self.monotonic() + INTERVAL
        attempted = self.now().astimezone(timezone.utc).isoformat()
        previous, context = {}, None
        try:
            previous = _saved(self.root)
        except (OSError, ValueError, TypeError):
            pass
        try:
            context = _context(self.root)
            if context is None:
                raise ValueError("binding_changed")
            if any(job.get("status") in {"submitting", "unknown", "working"} for job in context["jobs"]):
                return  # Reconciliation and uncertain submissions take priority over account display.
            snapshot = self.browser.account_snapshot()
            if (snapshot.get("account_key") != context["binding"]["account_key"]
                    or snapshot.get("contest") != context["binding"]["contest"]
                    or (_context(self.root) or {}).get("binding") != context["binding"]):
                raise ValueError("binding_changed")
            _public_data(snapshot, context["jobs"])
            saved = {"version": 1, "binding": context["binding"], "snapshot": snapshot,
                     "updated_at": self.now().astimezone(timezone.utc).isoformat(),
                     "last_attempt_at": attempted, "error_code": None}
        except Exception as error:
            binding = context["binding"] if context else previous.get("binding")
            keep = previous if previous.get("binding") == binding else {}
            code = "login_required" if str(error) == "timefolio_login_required" else (
                "binding_changed" if str(error) == "binding_changed" else "collection_failed")
            saved = {"version": 1, "binding": binding, "snapshot": keep.get("snapshot"),
                     "updated_at": keep.get("updated_at"), "last_attempt_at": attempted, "error_code": code}
        try:
            _write(self.root / ".local" / "timefolio-account.json", saved)
        except (OSError, ValueError, TypeError):
            pass  # Account display failure never changes order controls or the ledger.


def account_snapshot(root, *, now=None):
    """GET-side local reads only; never opens a browser, creates a DB, or orders."""
    current = now or datetime.now(timezone.utc)
    result = {"status": "waiting", "stale": True, "error": None, "updated_at": None,
              "last_attempt_at": None, "refresh_interval_seconds": INTERVAL,
              "account": None, "summary": None, "holdings": [], "orders": []}
    try:
        context, saved = _context(root), _saved(root)
        if context is None or not saved:
            return result
        if saved.get("binding") != context["binding"]:
            return {**result, "error": ERRORS["binding_changed"]}
        attempted, updated = _timestamp(saved.get("last_attempt_at")), _timestamp(saved.get("updated_at"))
        result["last_attempt_at"] = attempted.isoformat() if attempted else None
        error = ERRORS.get(saved.get("error_code"))
        if not saved.get("snapshot"):
            return {**result, "status": "error" if error else "waiting", "error": error}
        snapshot = saved["snapshot"]
        if (not isinstance(snapshot, dict) or snapshot.get("account_key") != context["binding"]["account_key"]
                or snapshot.get("contest") != context["binding"]["contest"] or updated is None):
            raise ValueError("account_snapshot_invalid")
        public = _public_data(snapshot, context["jobs"])
        process = _read(Path(root) / ".local" / "timefolio-mirror-state.json")
        heartbeat = _timestamp(process.get("updated_at"))
        stale = (not -5 <= (current - updated).total_seconds() < STALE_SECONDS
                 or error is not None or process.get("status") in {"stopped", "stopping", "error"}
                 or heartbeat is None or (current - heartbeat).total_seconds() >= STALE_SECONDS
                 or public["account"]["session_date"] != current.astimezone(KST).date().isoformat())
        return {**result, **public, "status": "error" if error else "ok", "stale": stale,
                "error": error, "updated_at": updated.isoformat()}
    except (OSError, ValueError, TypeError, KeyError, sqlite3.DatabaseError):
        return {**result, "status": "error", "error": ERRORS["invalid_snapshot"]}
