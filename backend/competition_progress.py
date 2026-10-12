"""KIS contest progress from whole-account fills and proven API acknowledgements."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re

from backend.competition_deadline import KIS_LAST_DAY
from backend.history import series_for_profile
from backend.kis import KST, load_profiles


KIS_FIRST_DAY = date(2026, 10, 12)
TARGETS = {"trading_days": 5, "gross_amount": "500000000", "symbols": 5, "api_orders": 5}


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        return result if result.utcoffset() is not None else None
    except ValueError:
        return None


def _number(value):
    if isinstance(value, bool):
        raise ValueError("invalid_fill_number")
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError("invalid_fill_number")
        return result
    except InvalidOperation:
        raise ValueError("invalid_fill_number") from None


def _identity(row):
    day = date.fromisoformat(row["order_date"])
    if day.isoformat() != row["order_date"]:
        raise ValueError("invalid_order_date")
    branch, order = row["branch_id"], row["order_id"]
    if (not isinstance(branch, str) or not re.fullmatch(r"[0-9]{1,10}", branch)
            or not isinstance(order, str) or not re.fullmatch(r"[0-9]{1,20}", order)
            or int(order) == 0):
        raise ValueError("invalid_order_identity")
    return day.isoformat(), branch, order


def _fills(rows, through):
    """Keep one cumulative observation per order, including partial fills."""
    result = {}
    if not isinstance(rows, list):
        raise ValueError("invalid_fills")
    for row in rows:
        key = _identity(row)
        if not KIS_FIRST_DAY.isoformat() <= key[0] <= through.isoformat():
            continue
        quantity, amount = _number(row["quantity"]), _number(row["amount"])
        if quantity == 0:
            continue
        if (quantity != quantity.to_integral_value() or amount <= 0
                or row.get("side") not in {"buy", "sell"}
                or not isinstance(row.get("symbol"), str)
                or not re.fullmatch(r"[A-Z0-9]{6,12}", row["symbol"])):
            raise ValueError("invalid_fill")
        fill = {"symbol": row["symbol"], "side": row["side"], "quantity": quantity, "amount": amount}
        previous = result.get(key)
        if previous:
            if (previous["symbol"] != fill["symbol"] or previous["side"] != fill["side"]
                    or quantity == previous["quantity"] and amount != previous["amount"]
                    or (quantity - previous["quantity"]) * (amount - previous["amount"]) < 0
                    or quantity != previous["quantity"] and amount == previous["amount"]):
                raise ValueError("conflicting_cumulative_fill")
            if quantity < previous["quantity"]:
                continue
        result[key] = fill
    return result


def _api_count(orders, fills, fingerprint, now):
    if orders is None:
        return None
    if not isinstance(orders, list) or not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("invalid_api_ledger")
    matched = set()
    for order in orders:
        if (not isinstance(order, dict) or order.get("fingerprint") != fingerprint
                or order.get("submission_skipped") is True or order.get("request_sent") is False):
            continue
        accepted = _instant(order.get("accepted_at"))
        if accepted is None or accepted > now:
            continue
        try:
            key = _identity(order)
        except (KeyError, TypeError, ValueError):
            continue
        fill = fills.get(key)
        quantity = order.get("quantity")
        if (fill and accepted.astimezone(KST).date().isoformat() == key[0]
                and order.get("symbol") == fill["symbol"] and order.get("side") == fill["side"]
                and type(quantity) is int and quantity >= fill["quantity"]):
            matched.add(key)
    return len(matched)


def _metric(value, target):
    return {"value": value, "target": target,
            "met": None if value is None else Decimal(str(value)) >= Decimal(str(target)),
            "progress_pct": None if value is None else format(
                min(Decimal(100), Decimal(str(value)) / Decimal(str(target)) * 100), ".2f")}


def _base(now, account=None):
    if now.utcoffset() is None:
        raise ValueError("competition_clock_requires_timezone")
    today = now.astimezone(KST).date()
    return {"status": "ok", "phase": "not_started" if today < KIS_FIRST_DAY else
            "ended" if today > KIS_LAST_DAY else "active",
            "account": account or {"id": "competition", "name": "대회 모의투자"},
            "period": {"start_date": KIS_FIRST_DAY.isoformat(), "end_date": KIS_LAST_DAY.isoformat(),
                       "as_of": min(today, KIS_LAST_DAY).isoformat()},
            "updated_at": None, "stale": False, "error": None, "coverage": "none",
            "metrics": {key: {**_metric(None, target), **({"lower_bound": True} if key == "api_orders" else {})}
                        for key, target in TARGETS.items()},
            "basis": {"trades": "whole_account_broker_fills",
                      "api_orders": "accepted_api_order_matches", "reset_adjusted": False}}


def calculate_kis_progress(snapshot, orders, *, fingerprint, now):
    """Calculate period totals; a missing complete sync must not become zero."""
    result = _base(now)
    account = snapshot.get("account", {})
    if account.get("id") != "competition":
        raise ValueError("competition_account_required")
    result["account"] = {"id": "competition", "name": account.get("name", "대회 모의투자")}
    through = date.fromisoformat(result["period"]["as_of"])
    if result["phase"] == "not_started":
        fills, complete = {}, True
    else:
        if (snapshot.get("environment") != "paper" or snapshot.get("start_date") != KIS_FIRST_DAY.isoformat()
                or snapshot.get("end_date") != through.isoformat()):
            raise ValueError("competition_period_required")
        fills = _fills(snapshot.get("trades"), through)
        complete = _instant(snapshot.get("updated_at")) is not None
        result["updated_at"] = snapshot.get("updated_at") if complete else None
    known = complete or bool(fills)
    result["coverage"] = "complete" if complete else "partial" if fills else "none"
    result["stale"] = bool(snapshot.get("stale") or snapshot.get("status") != "ok" or not complete) and known
    if snapshot.get("status") != "ok" or not complete:
        result.update(status="error", error="대회 기간 체결 조회 미완료")
    if known:
        try:
            api_count = _api_count(orders, fills, fingerprint, now)
        except ValueError:
            api_count = None
        values = {"trading_days": len({key[0] for key in fills}),
                  "gross_amount": format(sum((fill["amount"] for fill in fills.values()), Decimal(0)), "f"),
                  "symbols": len({fill["symbol"] for fill in fills.values()}),
                  "api_orders": api_count}
        for key, value in values.items():
            result["metrics"][key].update(_metric(value, TARGETS[key]))
    if orders is None or known and result["metrics"]["api_orders"]["value"] is None:
        result.update(status="error", error="API 주문 기록 조회 미완료" if complete else result["error"])
    return result


class KisCompetitionProgress:
    """Use AccountDirectory's complete, cached, account-isolated trade queries."""

    def __init__(self, account_directory, orders_reader=None, *, now=None, profile_reader=None):
        self.accounts = account_directory
        self.orders_reader = orders_reader
        self.now = now or (lambda: datetime.now(KST))
        self.profile_reader = profile_reader or (
            lambda: load_profiles(self.accounts.config_path).select("competition"))

    def snapshot(self):
        now = self.now()
        failure = _base(now)
        for _ in range(3):
            try:
                profile = self.profile_reader()
                if profile.id != "competition" or not profile.configured:
                    raise ValueError("competition_account_unconfigured")
                series = series_for_profile(profile)
                account = {"id": profile.id, "name": profile.name}
                failure["account"] = account
                if failure["phase"] == "not_started":
                    source, orders = {"account": account, "status": "ok"}, []
                else:
                    source = self.accounts.trades("competition", KIS_FIRST_DAY.isoformat(),
                                                  failure["period"]["as_of"])
                    try:
                        orders = self.orders_reader() if self.orders_reader is not None else None
                    except Exception:
                        orders = None
                current = self.profile_reader()
                if not current.configured or series_for_profile(current) != series:
                    continue
                return calculate_kis_progress(source, orders, fingerprint=series.fingerprint, now=now)
            except Exception:
                # Do not return configuration values or raw broker/ledger errors.
                failure.update(status="error", error="대회 진행률 조회 실패")
                return failure
        failure.update(status="error", error="대회 계좌 설정 변경 중")
        return failure
