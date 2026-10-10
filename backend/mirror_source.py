"""User-run, read-only KIS inputs for the contest mirror.

Construction performs no I/O. read() consumes the dashboard's confirmed fills.
value() reads one balance only when an execution needs a valuation. This adapter
never reconciles, starts, submits, cancels, or configures a KIS order.
"""
from copy import deepcopy
from datetime import datetime, time as daytime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from backend.history import series_for_profile
from backend.kis import KST, ROOT, KisError, load_profiles
from backend.paper_broker import PaperBroker


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("source_dashboard_redirect")


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise ValueError("source_timestamp_invalid") from None


def _positive(value):
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0:
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise ValueError("source_value_invalid") from None


class TimefolioMirrorSource:
    def __init__(self, base_url="http://127.0.0.1:8765", config_path=ROOT / "config.local.toml", *,
                 http=None, broker_factory=None, profile_loader=None, now=None,
                 max_pages=1000, max_age_seconds=180):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise ValueError("source_dashboard_must_be_loopback")
        if type(max_pages) is not int or not 1 <= max_pages <= 10000:
            raise ValueError("source_max_pages_invalid")
        if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 180:
            raise ValueError("source_max_age_invalid")
        self.base_url, self.config_path = base_url.rstrip("/"), Path(config_path)
        self.http = http or self._request
        self.broker_factory = broker_factory or PaperBroker
        self.profile_loader = profile_loader or load_profiles
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.max_pages = max_pages
        self.max_age_seconds = max_age_seconds

    def _request(self, method, path, payload=None):
        # No caller can supply an arbitrary dashboard mutation or URL.
        if (method != "GET" or payload is not None
                or not (path == "/api/experiments" or path.startswith("/api/mirror?"))):
            raise ValueError("source_request_not_allowed")
        headers = {"X-KIS-Dashboard": "1", "Content-Type": "application/json"}
        request = Request(self.base_url + path, headers=headers, method=method)
        try:
            with build_opener(_NoRedirect()).open(request, timeout=10) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except Exception:
            raise ValueError("source_dashboard_unavailable") from None

    def _state(self):
        result = self.http("GET", "/api/experiments")
        if not isinstance(result, dict) or result.get("environment") != "paper":
            raise ValueError("source_environment_not_paper")
        if not isinstance(result.get("policy"), dict) or not isinstance(result.get("automation"), dict):
            raise ValueError("source_state_invalid")
        if type(result["automation"].get("enabled")) is not bool:
            raise ValueError("source_enable_state_unknown")
        return result

    def _feed(self, cursor):
        events, binding, position = [], None, cursor
        for _ in range(self.max_pages):
            page = self.http("GET", "/api/mirror?" + urlencode({"after": position, "limit": 200}))
            if not isinstance(page, dict):
                raise ValueError("source_feed_invalid")
            identity = (page.get("source_account"), page.get("source_fingerprint"))
            if not all(isinstance(value, str) and value.strip() for value in identity):
                raise ValueError("source_account_unconfigured")
            if binding is not None and identity != binding:
                raise ValueError("source_account_changed")
            binding = identity
            chunk, following = page.get("events"), page.get("next_cursor")
            if (not isinstance(chunk, list) or len(chunk) > 200 or type(following) is not int
                    or type(page.get("has_more")) is not bool):
                raise ValueError("source_feed_invalid")
            for event in chunk:
                if (not isinstance(event, dict) or type(event.get("id")) is not int
                        or event["id"] <= position or event.get("source_fingerprint") != binding[1]):
                    raise ValueError("source_feed_event_invalid")
                position = event["id"]
                events.append(event)
            if following != position or page["has_more"] and not chunk:
                raise ValueError("source_cursor_not_advancing")
            if not page["has_more"]:
                if (not isinstance(page.get("owned_quantities"), dict)
                        or not isinstance(page.get("source_issues"), list)
                        or any(type(quantity) is not int or quantity < 0
                               for quantity in page["owned_quantities"].values())):
                    raise ValueError("source_feed_invalid")
                return {**page, "events": events, "has_more": False}
        raise ValueError("source_feed_page_limit")

    def _profile(self, state, feed):
        if state["policy"].get("account_id") != feed["source_account"]:
            raise ValueError("source_policy_account_changed")
        try:
            profile = self.profile_loader(self.config_path).select(feed["source_account"])
            profile.settings.validate_credentials()
            profile.settings.validate_account()
        except (KisError, OSError, ValueError):
            raise ValueError("source_profile_invalid") from None
        if profile.id != feed["source_account"] or series_for_profile(profile).fingerprint != feed["source_fingerprint"]:
            raise ValueError("source_profile_binding_changed")
        return profile

    @staticmethod
    def _enabled(state, feed):
        if type(feed.get("orders_enabled")) is not bool or type(feed.get("user_paused")) is not bool:
            raise ValueError("source_enable_state_unknown")
        return state["automation"]["enabled"] and feed["orders_enabled"] and not feed["user_paused"]

    def _fresh(self, value, now):
        return 0 <= (now - _stamp(value)).total_seconds() <= self.max_age_seconds

    def read(self, cursor=0):
        """Read local confirmed events and execution controls, without broker I/O."""
        if type(cursor) is not int or not 0 <= cursor <= 2**63 - 1:
            raise ValueError("source_cursor_invalid")
        observed = _stamp(self.now())
        state, feed = self._state(), self._feed(cursor)
        self._profile(state, feed)
        current = observed.astimezone(KST)
        # Mirror fills may arrive after the source strategy's 15:15 entry cutoff.
        # The destination adapter still applies its own order-entry hours.
        within_hours = current.weekday() < 5 and daytime(9) <= current.time() < daytime(15, 30)
        return {**deepcopy(feed), "environment": "paper", "prices": {}, "equity": None,
                "as_of": observed.isoformat(), "market_open": within_hours,
                "orders_enabled": self._enabled(state, feed), "valuation_issues": []}

    def value(self, source, symbols):
        """Value candidate symbols from one balance; do not reassess investments.

        Account/NAV failures invalidate the valuation. Position or price failures
        are per-symbol issues so an unrelated holding cannot block every order.
        The caller checks fresh local controls again before submission.
        """
        if (not isinstance(source, dict) or source.get("environment") != "paper"
                or not isinstance(source.get("owned_quantities"), dict)
                or not isinstance(symbols, (list, tuple, set))
                or any(not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{6}", symbol) for symbol in symbols)):
            raise ValueError("source_valuation_input_invalid")
        result = deepcopy(source)
        result.update(prices={}, equity=None, valuation_issues=[])
        if not symbols:
            return result
        profile = self._profile({"policy": {"account_id": source.get("source_account")}}, source)
        broker = self.broker_factory(profile)
        account = broker.snapshot()
        now = _stamp(self.now())
        if (not isinstance(account, dict) or account.get("environment") != "paper"
                or not isinstance(account.get("holdings"), dict) or not self._fresh(account.get("as_of"), now)):
            raise ValueError("source_balance_unverified")
        result.update(equity=str(_positive(account.get("total_value"))), valuation_at=_stamp(account["as_of"]).isoformat())
        for symbol in sorted(set(symbols)):
            quantity = source["owned_quantities"].get(symbol)
            if type(quantity) is not int or quantity < 0:
                result["valuation_issues"].append({"symbol": symbol, "reason": "source_quantity_invalid"})
                continue
            holding = account["holdings"].get(symbol, {})
            actual = holding.get("quantity", 0) if isinstance(holding, dict) else None
            if type(actual) is not int or actual < quantity:
                result["valuation_issues"].append({"symbol": symbol, "reason": "source_balance_mismatch"})
                continue
            if not quantity:
                continue
            try:
                result["prices"][symbol] = {"price": str(_positive(holding.get("price"))), "fresh": True}
            except ValueError:
                result["valuation_issues"].append({"symbol": symbol, "reason": "source_price_missing"})
        return result
