"""Offline event intake and one-shot valuation; all I/O uses fakes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from backend.history import series_for_profile
from backend.kis import AccountProfile, AccountProfiles, KST, Settings
from backend.mirror_source import TimefolioMirrorSource


NOW = datetime(2026, 10, 12, 10, 0, tzinfo=KST)
PROFILE = AccountProfile("paper", "test", Settings("fake-key", "fake-secret", "12345678"))
FP = series_for_profile(PROFILE).fingerprint


def event(identity=1):
    return {"id": identity, "source_fingerprint": FP, "symbol": "005930", "side": "buy",
            "source_order_id": "local-1", "source_key": json.dumps([FP, "2026-10-12", "12", "1"]),
            "quantity": 1, "amount": "10000", "from_quantity": identity - 1, "to_quantity": identity,
            "observed_at": NOW.isoformat()}


class FakeDashboard:
    def __init__(self):
        self.state = {"environment": "paper", "policy": {"account_id": "paper"},
                      "automation": {"enabled": True}, "busy": False, "error": None}
        self.feed = {"source_account": "paper", "source_fingerprint": FP, "status": "observing",
                     "orders_enabled": True, "user_paused": False, "submission_enabled": False,
                     "source_issues": [], "owned_quantities": {"005930": 2}}
        self.events, self.calls, self.page_size = [event()], [], 200

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        assert method == "GET" and payload is None, "source attempted a mutation"
        if path == "/api/experiments":
            return deepcopy(self.state)
        assert path.startswith("/api/mirror?")
        after = int(parse_qs(urlsplit(path).query)["after"][0])
        events = [value for value in self.events if value["id"] > after]
        chunk = events[:self.page_size]
        return deepcopy({**self.feed, "events": chunk,
                         "next_cursor": chunk[-1]["id"] if chunk else after,
                         "has_more": len(events) > self.page_size})


class FakeBroker:
    def __init__(self):
        self.account = {"environment": "paper", "total_value": "1000000", "as_of": NOW.isoformat(),
                        "holdings": {"005930": {"quantity": 2, "price": "10000"},
                                     "000660": {"quantity": 50, "price": "20000"}}}
        self.snapshot = Mock(side_effect=lambda: deepcopy(self.account))
        for name in ("quote", "market_session", "session_days", "orders", "submit", "cancel"):
            setattr(self, name, Mock(side_effect=AssertionError("unneeded broker call: " + name)))


class MirrorSourceTests(unittest.TestCase):
    def setUp(self):
        self.dashboard, self.broker = FakeDashboard(), FakeBroker()
        self.clock = NOW
        self.factory = Mock(return_value=self.broker)
        self.profiles = Mock(return_value=AccountProfiles("paper", {"paper": PROFILE}))
        self.source = TimefolioMirrorSource(http=self.dashboard, broker_factory=self.factory,
                                           profile_loader=self.profiles, now=lambda: self.clock)

    def test_construct_is_inert_and_read_uses_local_feed_only(self):
        self.assertEqual(self.dashboard.calls, [])
        self.profiles.assert_not_called()
        result = self.source.read()
        self.assertEqual(len(self.dashboard.calls), 2)
        self.assertTrue(result["orders_enabled"])
        self.assertTrue(result["market_open"])
        self.assertIsNone(result["equity"])
        self.assertEqual(result["prices"], {})
        self.assertEqual(result["as_of"], NOW.astimezone(timezone.utc).isoformat())
        self.factory.assert_not_called()

    def test_busy_analysis_and_unrelated_issue_do_not_rejudge_confirmed_fill(self):
        self.dashboard.state.update(busy=True, error="unrelated analysis failure")
        self.dashboard.feed["source_issues"] = [{"id": "other", "reason": "source_order_unconfirmed"}]
        result = self.source.read()
        self.assertEqual(result["events"], [event()])
        self.assertEqual(result["source_issues"], self.dashboard.feed["source_issues"])
        self.assertTrue(result["orders_enabled"])
        self.assertEqual(len(self.dashboard.calls), 2)
        self.factory.assert_not_called()

    def test_drains_pages_before_returning_latest_positions(self):
        self.dashboard.page_size = 1
        self.dashboard.events.append(event(2))
        result = self.source.read()
        self.assertEqual([row["id"] for row in result["events"]], [1, 2])
        self.assertEqual((result["next_cursor"], result["has_more"]), (2, False))
        self.assertEqual(result["owned_quantities"], {"005930": 2})
        self.assertEqual(self.source.read(2)["events"], [])
        self.factory.assert_not_called()

    def test_pause_flags_are_authoritative_without_broker_queries(self):
        for field in ("orders_enabled", "user_paused"):
            self.dashboard.feed.update(orders_enabled=field != "orders_enabled", user_paused=field == "user_paused")
            self.assertFalse(self.source.read()["orders_enabled"])
        self.dashboard.feed.update(orders_enabled=True, user_paused=False)
        self.dashboard.state["automation"]["enabled"] = False
        self.assertFalse(self.source.read()["orders_enabled"])
        self.factory.assert_not_called()

    def test_market_hours_are_local_hint_without_calendar_api(self):
        for stamp, opened in ((NOW.replace(hour=9), True), (NOW.replace(hour=15, minute=20), True),
                              (NOW.replace(hour=15, minute=30), False),
                              (datetime(2026, 10, 10, 10, tzinfo=KST), False)):
            self.clock = stamp
            self.assertEqual(self.source.read()["market_open"], opened)
        self.factory.assert_not_called()

    def test_binding_missing_controls_and_nonpaper_environment_fail_closed(self):
        for update in ({"source_account": "competition"}, {"source_fingerprint": "other"}, {"user_paused": None}):
            original = deepcopy(self.dashboard.feed)
            self.dashboard.feed.update(update)
            with self.assertRaises(ValueError):
                self.source.read()
            self.dashboard.feed = original
        self.dashboard.state["environment"] = "live"
        with self.assertRaisesRegex(ValueError, "environment_not_paper"):
            self.source.read()
        self.factory.assert_not_called()

    def test_batch_has_one_balance_and_no_quote_or_reconcile(self):
        source = self.source.read()
        self.dashboard.calls.clear()
        valued = self.source.value(source, ["005930", "005930"])
        self.assertEqual(self.dashboard.calls, [])
        self.broker.snapshot.assert_called_once()
        self.assertEqual(valued["equity"], "1000000")
        self.assertEqual(valued["prices"], {"005930": {"price": "10000", "fresh": True}})
        self.assertEqual(valued["valuation_issues"], [])
        for name in ("quote", "market_session", "session_days", "orders", "submit", "cancel"):
            getattr(self.broker, name).assert_not_called()

    def test_value_preserves_input_and_ignores_unrequested_holding(self):
        source = self.source.read()
        source["owned_quantities"]["000660"] = 10
        original = deepcopy(source)
        self.broker.account["holdings"].pop("000660")
        result = self.source.value(source, ["005930"])
        self.assertEqual(source, original)
        self.assertEqual(result["valuation_issues"], [])
        self.assertNotIn("000660", result["prices"])

    def test_price_and_quantity_errors_are_per_symbol(self):
        source = self.source.read()
        source["owned_quantities"].update({"000660": 10, "035420": 2})
        self.broker.account["holdings"]["000660"].pop("price")
        result = self.source.value(source, ["005930", "000660", "035420"])
        self.assertEqual(result["prices"], {"005930": {"price": "10000", "fresh": True}})
        self.assertEqual({row["symbol"]: row["reason"] for row in result["valuation_issues"]},
                         {"000660": "source_price_missing", "035420": "source_balance_mismatch"})
        self.assertEqual(result["source_issues"], [])
        self.broker.snapshot.assert_called_once()

    def test_no_candidates_need_no_balance_and_zero_owned_exit_needs_no_price(self):
        source = self.source.read()
        self.assertIsNone(self.source.value(source, [])["equity"])
        self.factory.assert_not_called()
        source["owned_quantities"]["005930"] = 0
        self.broker.account["holdings"].pop("005930")
        result = self.source.value(source, ["005930"])
        self.assertEqual(result["valuation_issues"], [])
        self.assertEqual(result["prices"], {})

    def test_bad_nav_environment_and_stale_or_naive_balance_fail_closed(self):
        source = self.source.read()
        for update in ({"environment": "live"}, {"total_value": "NaN"}, {"total_value": "0"},
                       {"as_of": (NOW - timedelta(seconds=181)).isoformat()},
                       {"as_of": "2026-10-12T10:00:00"}, {"as_of": (NOW + timedelta(seconds=1)).isoformat()}):
            with self.subTest(update=update):
                original = deepcopy(self.broker.account)
                self.broker.account.update(update)
                with self.assertRaises(ValueError):
                    self.source.value(source, ["005930"])
                self.broker.account = original

    def test_valuation_rechecks_profile_binding_before_broker(self):
        source = self.source.read()
        changed = AccountProfile("paper", "test", Settings("rotated", "secret", "12345678"))
        self.profiles.return_value = AccountProfiles("paper", {"paper": changed})
        with self.assertRaisesRegex(ValueError, "profile_binding_changed"):
            self.source.value(source, ["005930"])
        self.factory.assert_not_called()

    def test_guard_reads_new_fill_and_pause_without_repeating_valuation(self):
        first = self.source.read()
        self.source.value(first, ["005930"])
        self.dashboard.events.append(event(2))
        self.dashboard.feed["user_paused"] = True
        current = self.source.read(first["next_cursor"])
        self.assertEqual([row["id"] for row in current["events"]], [2])
        self.assertFalse(current["orders_enabled"])
        self.assertIsNone(current["equity"])
        self.broker.snapshot.assert_called_once()

    def test_invalid_cursor_or_symbols_fail_before_io(self):
        for cursor in (-1, True, 1.5, 2**63):
            with self.assertRaises(ValueError):
                self.source.read(cursor)
        self.assertEqual(self.dashboard.calls, [])
        source = self.source.read()
        for symbols in (["wrong"], [None], "005930"):
            with self.assertRaises(ValueError):
                self.source.value(source, symbols)
        self.factory.assert_not_called()

    def test_account_change_between_pages_is_rejected(self):
        self.dashboard.page_size = 1
        self.dashboard.events.append(event(2))
        def changed(method, path, payload=None):
            result = self.dashboard(method, path, payload)
            if "after=1" in path:
                result["source_account"] = "competition"
            return result
        self.source.http = changed
        with self.assertRaisesRegex(ValueError, "account_changed"):
            self.source.read()

    def test_nonadvancing_feed_and_page_limit_fail(self):
        def stalled(method, path, payload=None):
            result = self.dashboard(method, path, payload)
            if path.startswith("/api/mirror?"):
                result.update(events=[], next_cursor=0, has_more=True)
            return result
        self.source.http = stalled
        with self.assertRaisesRegex(ValueError, "not_advancing"):
            self.source.read()
        self.source.http = self.dashboard
        self.source.max_pages = self.dashboard.page_size = 1
        self.dashboard.events.append(event(2))
        with self.assertRaisesRegex(ValueError, "page_limit"):
            self.source.read()

    def test_http_boundary_disallows_remote_urls_and_all_mutations(self):
        for url in ("https://localhost:8765", "http://example.com", "http://user@localhost", "http://localhost/path"):
            with self.assertRaises(ValueError):
                TimefolioMirrorSource(base_url=url)
        with patch("backend.mirror_source.build_opener") as opener:
            for action in ("reconcile", "start", "cancel", "configure", "pause"):
                with self.assertRaises(ValueError):
                    self.source._request("POST", "/api/experiments", {"action": action})
            opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
