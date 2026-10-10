"""Fake adapter tests and optional local HTML checks. Never contacts a website."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.kis import KST
from backend.mirror_runtime import MirrorBlocked, MirrorRejected, MirrorUnknown
from backend.timefolio_browser import CONTEST, GRID_DOM, ORIGIN, TimefolioBrowser, match_receipt


NOW = datetime(2026, 10, 12, 10, 0, tzinfo=KST)


def proposal(**changes):
    return {"symbol": "005930", "side": "buy", "weight": "2.00", "account_key": "account",
            "contest": CONTEST, "session_date": "2026-10-12", "job_id": "job1", "prior_order_ids": ["old"], **changes}


def receipt(identity="new", **changes):
    return {"verified": True, "order_id": identity, "symbol": "005930", "side": "buy",
            "weight": 2.0, "filled_quantity": 0, "filled_weight": None, "status": "working", **changes}


class FakeDialog:
    def __init__(self, message, kind="confirm"):
        self.type, self.message = kind, message
        self.accepted = self.dismissed = False

    def accept(self):
        self.accepted = True

    def dismiss(self):
        self.dismissed = True


class FakeLocator:
    def __init__(self, page, key):
        self.page, self.key = page, key

    @property
    def first(self):
        return self

    def count(self):
        return self.page.counts.get(self.key, 1)

    def locator(self, selector, **kwargs):
        return FakeLocator(self.page, selector)

    def get_by_role(self, role, name=None, **kwargs):
        return self.page.get_by_role(role, name=name, **kwargs)

    def get_by_placeholder(self, placeholder, **kwargs):
        return FakeLocator(self.page, "placeholder:" + placeholder)

    def filter(self, **kwargs):
        return self

    def nth(self, index):
        return self

    def wait_for(self, **kwargs):
        return None

    def click(self):
        self.page.clicks.append(self.key)
        if self.key == 'button[type="submit"]' and self.page.click_error:
            raise self.page.click_error
        if "CSV 다운로드" in self.key:
            dialog = FakeDialog(self.page.csv_message)
            self.page.dialog_handler(dialog)
            self.page.last_dialog = dialog
            if not dialog.accepted:
                raise TimeoutError("fake download confirmation was dismissed")
        if self.key == "li":
            self.page.fields["symbol"] = "A" + self.page.search
        elif '매수도_true' in self.key:
            self.page.fields["side"] = "true"
        elif '매수도_false' in self.key:
            self.page.fields["side"] = "false"

    def fill(self, value):
        self.page.fills.append((self.key, value))
        if self.key == "placeholder:종목 선택":
            self.page.search = value
        elif 'step="0.01"' in self.key:
            self.page.fields["weight"] = value
        elif 'max="10"' in self.key:
            self.page.fields["level"] = value

    def evaluate(self, expression):
        if expression == GRID_DOM:
            return deepcopy(self.page.grid_states.pop(0))
        if "fieldset div" in expression:
            return deepcopy(self.page.validation_errors)
        return deepcopy(self.page.fields)

    def evaluate_all(self, expression):
        if self.key == "[data-alert-dialog]":
            return list(self.page.alert_messages)
        return ["2026-10-12"]

    def inner_text(self):
        return self.page.contest

    def select_option(self, **kwargs):
        self.page.portfolio_id = kwargs.get("value", self.page.portfolio_id)


    def input_value(self):
        if self.key == "select":
            return self.page.portfolio_id
        if 'name="d"' in self.key:
            return self.page.fields["date"]
        return self.page.fields["symbol"]


class FakePage:
    def __init__(self):
        self.url = ORIGIN + "/order"
        self.fields = {"symbol": "A005930", "date": "2026-10-12", "weight": "2.00",
                       "side": "true", "kind": "Opp", "slice": "false", "level": "5", "exitAll": False, "valid": True}
        self.clicks, self.fills, self.grid_states = [], [], []
        self.counts = {"role:dialog:": 0, 'table:has(> thead th[id="avgPx"])': 0,
                       'input[type="password"]:visible': 0}
        self.search = "005930"
        self.click_error = None
        self.csv_message = "'주문_내역.csv' 로 다운로드할까요?"
        self.dialog_handler = None
        self.last_dialog = None
        self.validation_ok = True
        self.validation_errors = []
        self.alert_messages = []
        self.contest, self.portfolio_id = CONTEST, "pf13"
        self.responses = []
        self.response_candidates = None
        self.selected_response = None
        self.finished_candidates = None
        self.finished_requests = []
        self.finish_error = None
        self.evaluations = []
        self.download = SimpleNamespace(path=lambda: "offline-fake.csv", delete=Mock())

    def get_by_role(self, role, name=None, **kwargs):
        label = getattr(name, "pattern", name) or ""
        return FakeLocator(self, "role:" + role + ":" + label)

    def locator(self, selector, **kwargs):
        return FakeLocator(self, selector)

    def evaluate(self, expression):
        self.evaluations.append(expression)
        return None

    def expect_response(self, predicate, **kwargs):
        urls = [ORIGIN + "/Portfolio/ValidateProduct?d=" + self.fields["date"]
                + "&prodId=A" + self.search + "&entry=" + self.fields["side"]]
        urls += [ORIGIN + "/Portfolio/" + name for name in ("Session", "Orders", "Universe")]
        candidates = [SimpleNamespace(url=url, request=SimpleNamespace(method="GET"),
                      ok=self.validation_ok if "ValidateProduct" in url else True, finished=lambda: None) for url in urls]
        if self.response_candidates is not None:
            candidates = self.response_candidates
        response = next((candidate for candidate in candidates if predicate(candidate)), None)
        if response is None:
            raise TimeoutError("fake validation did not match requested product/date/side")
        self.selected_response = response
        self.responses.append(response.url)
        event = SimpleNamespace(value=response)

        class Context:
            def __enter__(self):
                return event

            def __exit__(self, *args):
                return False

        return Context()

    def expect_request_finished(self, predicate, **kwargs):
        page = self
        event = SimpleNamespace(value=None)

        class Context:
            def __enter__(self):
                return event

            def __exit__(self, exc_type, *args):
                if exc_type:
                    return False
                if page.finish_error:
                    raise page.finish_error
                candidates = page.finished_candidates
                if candidates is None:
                    candidates = [page.selected_response.request]
                request = next((request for request in candidates if predicate(request)), None)
                if request is None:
                    raise TimeoutError("fake selected request did not finish")
                page.finished_requests.append(request)
                event.value = request
                return False

        return Context()

    def expect_download(self, **kwargs):
        event = SimpleNamespace(value=self.download)

        class Context:
            def __enter__(self):
                return event

            def __exit__(self, *args):
                return False

        return Context()


class TimefolioBrowserTests(unittest.TestCase):
    def setUp(self):
        prevent_open = patch.object(TimefolioBrowser, "open", side_effect=AssertionError("No browser may open in tests"))
        self.addCleanup(prevent_open.stop)
        self.open = prevent_open.start()
        self.browser = TimefolioBrowser("unused-offline-profile", now=lambda: NOW, sleep=lambda _: None)
        self.page = FakePage()
        self.page.dialog_handler = self.browser._dialog
        self.browser.page = self.page
        self.browser._account_key = "account"
        self.browser._portfolio_id = "pf13"
        self.browser._prepared = proposal()

    def submit_clicks(self):
        return self.page.clicks.count('button[type="submit"]')

    def test_ui_query_accepts_same_origin_get_with_optional_api_prefix(self):
        for prefix in ("", "/api"):
            with self.subTest(prefix=prefix):
                url = ORIGIN + prefix + "/Portfolio/Orders?period=A"
                self.page.response_candidates = [SimpleNamespace(
                    url=url, request=SimpleNamespace(method="GET"), ok=True, finished=lambda: None)]
                action = Mock()
                self.browser._ui_query("/Portfolio/Orders", action)
                action.assert_called_once_with()
                self.assertEqual(self.page.responses[-1], url)
        self.assertEqual(self.submit_clicks(), 0)

    def test_ui_query_rejects_wrong_endpoint_origin_or_method(self):
        for prefix in ("", "/api"):
            path = prefix + "/Portfolio/Orders"
            for url, method in (
                (ORIGIN + prefix + "/Portfolio/Session", "GET"),
                ("https://example.invalid" + path, "GET"),
                ("http://contest.timefolio.net" + path, "GET"),
                (ORIGIN + path, "POST"),
            ):
                with self.subTest(url=url, method=method):
                    self.page.response_candidates = [SimpleNamespace(
                        url=url, request=SimpleNamespace(method=method), ok=True, finished=lambda: None)]
                    with self.assertRaises(TimeoutError):
                        self.browser._ui_query("/Portfolio/Orders", Mock())
        self.assertEqual(self.page.responses, [])
        self.assertEqual(self.submit_clicks(), 0)

    def test_ui_query_waits_for_selected_request_not_same_url_request(self):
        url = ORIGIN + "/api/Portfolio/Orders"
        selected = SimpleNamespace(url=url, method="GET")
        other = SimpleNamespace(url=url, method="GET")
        self.page.response_candidates = [SimpleNamespace(url=url, request=selected, ok=True)]
        self.page.finished_candidates = [other, selected]
        self.browser._ui_query("/Portfolio/Orders", Mock())
        self.assertIs(self.page.finished_requests[-1], selected)
        self.page.finished_candidates = [other]
        with self.assertRaisesRegex(TimeoutError, "selected request did not finish"):
            self.browser._ui_query("/Portfolio/Orders", Mock())

    def test_ui_query_http_failure_blocks_without_waiting_for_body(self):
        response = SimpleNamespace(url=ORIGIN + "/api/Portfolio/Orders",
                                   request=SimpleNamespace(method="GET"), ok=False)
        self.page.response_candidates = [response]
        self.page.finish_error = AssertionError("HTTP failure must not wait for the body")
        with self.assertRaisesRegex(MirrorBlocked, "destination_read_failed"):
            self.browser._ui_query("/Portfolio/Orders", Mock())
        self.assertEqual(self.page.finished_requests, [])
        self.assertEqual(self.page.evaluations, [])

    def test_request_completion_failure_blocks_query_and_product_validation(self):
        failure = TimeoutError("request body did not finish")
        self.page.finish_error = failure
        for action in (lambda: self.browser._ui_query("/Portfolio/Orders", Mock()),
                       lambda: self.browser._select_validated(
                           FakeLocator(self.page, "dialog"), FakeLocator(self.page, "li"), "005930")):
            with self.subTest(action=action):
                with self.assertRaises(TimeoutError) as caught:
                    action()
                self.assertIs(caught.exception, failure)
        self.assertEqual(self.page.evaluations, [])
        self.assertEqual(self.submit_clicks(), 0)

    def test_submit_confirms_exact_new_receipt_with_one_click(self):
        self.browser.receipt_snapshot = Mock(return_value={"verified": True, "session_date": "2026-10-12", "account_key": "account", "contest": CONTEST,
                                                   "orders": [receipt("old"), receipt()]})
        self.assertEqual(self.browser.submit(proposal())["order_id"], "new")
        self.assertEqual(self.submit_clicks(), 1)
        self.open.assert_not_called()

    def test_ambiguous_receipts_only_retry_reads_never_reclick(self):
        self.browser.receipt_snapshot = Mock(return_value={"verified": True, "session_date": "2026-10-12", "account_key": "account", "contest": CONTEST,
                                                   "orders": [receipt("one"), receipt("two")]})
        with self.assertRaises(MirrorUnknown):
            self.browser.submit(proposal())
        self.assertEqual(self.submit_clicks(), 1)
        self.assertEqual(self.browser.receipt_snapshot.call_count, 3)
        with self.assertRaises(MirrorBlocked):
            self.browser.submit(proposal())
        self.assertEqual(self.submit_clicks(), 1)

    def test_uncertain_click_timeout_consumes_prepared_form_without_retry(self):
        self.page.click_error = TimeoutError("fake click may have succeeded")
        with self.assertRaises(MirrorUnknown):
            self.browser.submit(proposal())
        with self.assertRaises(MirrorBlocked):
            self.browser.submit(proposal())
        self.assertEqual(self.submit_clicks(), 1)

    def test_changed_form_never_reaches_submit(self):
        for field, value in (("symbol", "A000660"), ("date", "2026-10-13"), ("weight", "2.01"),
                             ("side", "false"), ("kind", "Limit"), ("slice", "true"), ("level", "4"),
                             ("exitAll", True), ("valid", False)):
            with self.subTest(field=field):
                old = self.page.fields[field]
                self.page.fields[field] = value
                with self.assertRaises(MirrorBlocked):
                    self.browser.submit(proposal())
                self.page.fields[field] = old
        self.assertEqual(self.submit_clicks(), 0)

    def test_prepare_populates_verified_form_without_submitting(self):
        self.assertTrue(self.browser.prepare(proposal())["verified"])
        self.assertIn(('input[type="number"][step="0.01"]', "2.00"), self.page.fills)
        self.assertEqual(self.submit_clicks(), 0)

    def test_sell_prepare_waits_for_matching_sell_validation(self):
        result = self.browser.prepare(proposal(side="sell"))
        self.assertEqual(result["side"], "sell")
        self.assertTrue(any("ValidateProduct" in url and "entry=false" in url for url in self.page.responses))
        self.assertEqual(self.submit_clicks(), 0)

    def test_red_validation_error_blocks_even_valid_html_inputs(self):
        self.page.validation_errors = ["종목 거래 제한"]
        with self.assertRaisesRegex(MirrorRejected, "종목 거래 제한"):
            self.browser.submit(proposal())
        self.assertEqual(self.submit_clicks(), 0)

    def test_post_click_receipt_cannot_be_claimed_from_another_account(self):
        self.browser.receipt_snapshot = Mock(return_value={"verified": True, "session_date": "2026-10-12", "account_key": "different", "contest": CONTEST,
                                                   "orders": [receipt()]})
        with self.assertRaises(MirrorUnknown):
            self.browser.submit(proposal())
        self.assertEqual(self.submit_clicks(), 1)

    def test_post_click_read_timeout_never_repeats_the_click(self):
        self.browser.receipt_snapshot = Mock(side_effect=TimeoutError("fake receipt read timeout"))
        with self.assertRaises((MirrorUnknown, TimeoutError)):
            self.browser.submit(proposal())
        with self.assertRaises(MirrorBlocked):
            self.browser.submit(proposal())
        self.assertEqual(self.submit_clicks(), 1)

    def test_session_gate_applies_again_immediately_before_submit(self):
        for value in (NOW.replace(hour=8, minute=59), NOW.replace(hour=15, minute=19),
                      NOW.replace(day=17), NOW.replace(month=12, day=1)):
            with self.subTest(now=value):
                self.browser.now = lambda: value
                current = proposal(session_date=value.date().isoformat())
                self.browser._prepared = current
                with self.assertRaisesRegex(MirrorBlocked, "outside_order_session"):
                    self.browser.submit(current)
        self.assertEqual(self.submit_clicks(), 0)

    def test_lookup_checks_account_and_contest_before_matching(self):
        for changes in ({"account_key": "different"}, {"contest": "different"}):
            self.browser.receipt_snapshot = Mock(return_value={"verified": True, "session_date": "2026-10-12", "account_key": "account", "contest": CONTEST,
                                                       "orders": [receipt()], **changes})
            with self.assertRaisesRegex(MirrorUnknown, "binding_changed"):
                self.browser.lookup({"symbol": "005930", "proposal": proposal()})
        self.assertEqual(self.submit_clicks(), 0)

    def test_lookup_uses_existing_receipt_id_even_with_other_identical_orders(self):
        self.browser.receipt_snapshot = Mock(return_value={"verified": True, "session_date": "2026-10-12", "account_key": "account", "contest": CONTEST,
                                                   "orders": [receipt("new"), receipt("another")]})
        result = self.browser.lookup({"symbol": "005930", "proposal": proposal(), "receipt": receipt("new")})
        self.assertEqual(result["order_id"], "new")

    def test_only_exact_csv_confirmation_is_accepted_inside_download_scope(self):
        for active, kind, message, expected in ((True, "confirm", "'주문.csv' 로 다운로드할까요?", True),
                (False, "confirm", "'주문.csv' 로 다운로드할까요?", False),
                (True, "confirm", "주문을 제출할까요?", False),
                (True, "alert", "'주문.csv' 로 다운로드할까요?", False),
                (True, "confirm", "'주문.exe' 로 다운로드할까요?", False)):
            self.browser._csv_download = active
            dialog = FakeDialog(message, kind)
            self.browser._dialog(dialog)
            self.assertEqual((dialog.accepted, dialog.dismissed), (expected, not expected))

    def test_grid_download_matches_stable_dom_and_resets_confirmation_scope(self):
        state = {"headers": [{"id": "prodId", "text": "종목"}],
                 "rows": [{"cells": {"prodId": {"text": "005930", "id": "order-prodId"}}}],
                 "filtered": False, "count_text": "선택 0 전체 1"}
        self.page.grid_states = [state, state]
        with patch("backend.timefolio_browser.Path.read_text", return_value="종목\n005930\n"):
            result = self.browser._grid(FakeLocator(self.page, "grid"), "orders")
        self.assertTrue(result["complete"])
        self.assertTrue(self.page.last_dialog.accepted)
        self.assertFalse(self.browser._csv_download)
        self.page.download.delete.assert_called_once()
        self.assertEqual(self.submit_clicks(), 0)

    def test_csv_scope_resets_after_unexpected_confirmation_or_changed_dom(self):
        state = {"headers": [], "rows": [], "filtered": False, "count_text": "전체 0"}
        self.page.grid_states = [state]
        self.page.csv_message = "주문을 확정할까요?"
        with self.assertRaises(TimeoutError):
            self.browser._grid(FakeLocator(self.page, "grid"), "orders")
        self.assertFalse(self.browser._csv_download)
        self.assertTrue(self.page.last_dialog.dismissed)
        self.page.csv_message = "'data.csv' 로 다운로드할까요?"
        changed = {**state, "rows": [{"cells": {"prodId": {"id": "cell-new_prodId", "text": "005930"}}}]}
        self.page.grid_states = [state, changed] * 3
        with patch("backend.timefolio_browser.Path.read_text", return_value="종목\n"):
            with self.assertRaisesRegex(MirrorBlocked, "changed_during_csv"):
                self.browser._grid(FakeLocator(self.page, "grid"), "orders")

    def test_live_numeric_state_and_progress_changes_do_not_retry_csv(self):
        columns = ["prodId", "sgn", "genT", "wei", "cumQty", "w2o", "state", "close", "prft"]
        before = {"headers": [{"id": c, "text": c} for c in columns], "filtered": False,
                  "count_text": "선택 0 전체 1", "rows": [{"cells": {
                      c: {"id": "cellord-1_" + c, "text": value}
                      for c, value in zip(columns, ["005930", "매수", "10-12 10:00", "2", "1", "1", "작동", "70000", "50"])}}]}
        after = deepcopy(before)
        for column in columns[3:]:
            after["rows"][0]["cells"][column]["text"] = "changed"
            after["rows"][0]["cells"][column]["progress"] = [{"value": 100, "max": 100}]
        self.page.grid_states = [before, after]
        raw = ",".join(columns) + "\n005930,1,2026-10-12T10:00:00,2.00,3,0.8,Working,71000,55\n"
        with patch("backend.timefolio_browser.Path.read_text", return_value=raw):
            result = self.browser._grid(FakeLocator(self.page, "grid"), "orders")
        self.assertTrue(result["complete"])
        self.assertEqual(result["csv_text"], raw)
        self.assertEqual(self.page.clicks.count("role:button:CSV 다운로드"), 1)

    def test_csv_row_order_and_symbol_binding_changes_are_rejected(self):
        before = {"headers": [{"id": "prodId", "text": "종목"}], "filtered": False,
                  "count_text": "전체 2", "rows": [{"cells": {
                      "prodId": {"id": "cell" + identity + "_prodId", "text": symbol}}}
                      for identity, symbol in (("1", "005930"), ("2", "000660"))]}
        for change in ("order", "symbol", "id"):
            with self.subTest(change=change):
                after = deepcopy(before)
                if change == "order":
                    after["rows"].reverse()
                else:
                    after["rows"][0]["cells"]["prodId"]["text" if change == "symbol" else "id"] = "different"
                self.page.grid_states = [before, after] * 3
                with patch("backend.timefolio_browser.Path.read_text", return_value="종목\n005930\n000660\n"):
                    with self.assertRaisesRegex(MirrorBlocked, "changed_during_csv"):
                        self.browser._grid(FakeLocator(self.page, "grid"), "orders")

    def test_snapshot_reads_latest_job_receipts_and_connects_all_required_grids(self):
        self.browser.identity = Mock(return_value="account")
        self.browser._main = Mock()
        self.browser._date = Mock(return_value="2026-10-12")
        self.browser._grid = Mock(side_effect=lambda table, kind, **kwargs: {"kind": kind, **kwargs})
        jobs = [{"receipt": receipt("new", status="completed", filled_quantity=10)}]
        self.browser.jobs_reader = lambda: jobs
        with patch("backend.timefolio_dom.parse_snapshot", side_effect=lambda payload, *args: deepcopy(payload)) as parse:
            first = self.browser.snapshot(["005930"])
            jobs[0]["receipt"]["filled_quantity"] = 12
            second = self.browser.snapshot(["005930"])
        self.assertEqual(first["known_receipts"]["new"]["filled_quantity"], 10)
        self.assertEqual(second["known_receipts"]["new"]["filled_quantity"], 12)
        self.assertEqual({table["kind"] for table in second["tables"]},
                         {"positions", "targets", "unaccepted", "orders"})
        self.assertEqual(parse.call_count, 2)
        self.assertNotIn("stock_checks", second)
        self.browser.identity.assert_not_called()
        self.open.assert_not_called()

    def test_receipt_snapshot_exports_only_order_grids(self):
        self.browser.identity = Mock(side_effect=AssertionError("cached identity required"))
        self.browser._main = Mock()
        self.browser._date = Mock(return_value="2026-10-12")
        self.browser._grid = Mock(side_effect=lambda table, kind, **kwargs: {"kind": kind, **kwargs})
        with patch("backend.timefolio_dom.parse_receipts", side_effect=lambda payload, *args: deepcopy(payload)):
            out = self.browser.receipt_snapshot()
        self.assertEqual([table["kind"] for table in out["tables"]], ["unaccepted", "orders"])
        self.browser._main.assert_called_once_with(receipts_only=True)
        self.assertFalse(any("섹터" in c or "설정" in c for c in self.page.clicks))

    def test_lookup_reuses_supplied_observation_without_ui(self):
        self.browser.receipt_snapshot = Mock(side_effect=AssertionError("extra UI read"))
        observed = {"verified": True, "account_key": "account", "contest": CONTEST, "orders": [receipt()]}
        job = {"symbol": "005930", "proposal": proposal(), "receipt": receipt()}
        self.assertEqual(self.browser.lookup(job, snapshot=observed)["order_id"], "new")
        self.assertEqual(self.page.clicks, [])

    def test_unverified_observation_cannot_confirm_receipt(self):
        observed = {"verified": False, "account_key": "account", "contest": CONTEST, "orders": [receipt()]}
        with self.assertRaisesRegex(MirrorUnknown, "receipt_unverified"):
            self.browser.lookup({"proposal": proposal()}, snapshot=observed)
        self.assertEqual(self.page.clicks, [])

    def test_changed_portfolio_blocks_submit_before_click(self):
        self.page.portfolio_id = "another-pf"
        with self.assertRaisesRegex(MirrorBlocked, "binding_changed"):
            self.browser.submit(proposal())
        self.assertEqual(self.submit_clicks(), 0)

    def test_login_screen_invalidates_cached_identity_and_portfolio(self):
        for path in ("/Auth/Login", "/logout"):
            self.browser._account_key, self.browser._portfolio_id = "account", "pf13"
            self.page.url = ORIGIN + path
            with self.assertRaisesRegex(MirrorBlocked, "login_required"):
                self.browser._origin()
            self.assertIsNone(self.browser._account_key)
            self.assertIsNone(self.browser._portfolio_id)

    def test_changed_contest_blocks_submit_before_click(self):
        self.page.contest = "Training"
        with self.assertRaisesRegex(MirrorBlocked, "binding_changed"):
            self.browser.submit(proposal())
        self.assertEqual(self.submit_clicks(), 0)

    def test_submit_visible_business_rejection_keeps_reason_without_private_values(self):
        self.page.alert_messages = ["개별 종목 한도 초과 user@example.com 계좌:12345678"]
        self.browser.receipt_snapshot = Mock(side_effect=AssertionError("must preserve rejection before navigation"))
        with self.assertRaises(MirrorRejected) as caught:
            self.browser.submit(proposal())
        self.assertIn("종목 한도 초과", str(caught.exception))
        self.assertNotIn("user@example.com", str(caught.exception))
        self.assertNotIn("12345678", str(caught.exception))
        self.assertEqual(self.submit_clicks(), 1)

    def test_submit_connectivity_error_remains_unknown(self):
        self.page.alert_messages = ["네트워크 연결 시간 초과"]
        self.browser.receipt_snapshot = Mock(side_effect=AssertionError("unclear error remains visible"))
        with self.assertRaises(MirrorUnknown):
            self.browser.submit(proposal())
        self.assertEqual(self.submit_clicks(), 1)

    def test_failed_product_validation_never_prepares_or_submits(self):
        self.browser._prepared = None
        self.page.validation_ok = False
        with self.assertRaisesRegex(MirrorBlocked, "stock_validation_failed"):
            self.browser.prepare(proposal())
        self.assertIsNone(self.browser._prepared)
        self.assertEqual(self.submit_clicks(), 0)

    def test_receipt_matching_rejects_unverified_old_or_wrong_weight(self):
        for rows in ([receipt("old")], [receipt(verified=False)], [receipt(weight=2.01)], [receipt(side="sell")]):
            with self.subTest(rows=rows), self.assertRaises(MirrorUnknown):
                match_receipt(proposal(), rows)

    def test_virtual_order_rows_collected_in_display_order_and_scroll_restored(self):
        rows = [{"cells": {"prodId": {"id": f"cell{i}_prodId", "text": "005930"}}} for i in range(3)]
        position = {"top": 40}
        def evaluate(script, *args):
            if script == GRID_DOM:
                visible = rows[:2] if position["top"] < 75 else rows[1:]
                return {"headers": [{"id": "prodId", "text": "종목"}], "rows": deepcopy(visible),
                        "filtered": False, "count_text": "전체 3"}
            original = position["top"]
            if args[0] is not None:
                position["top"] = args[0]
            return {"top": position["top"], "original": original, "max": 100, "height": 100}
        table = SimpleNamespace(evaluate=evaluate)
        result = self.browser._grid_dom(table, expected=3)
        self.assertEqual(result["rows"], rows)
        self.assertEqual(position["top"], 40)
        self.assertEqual(self.submit_clicks(), 0)

    def test_virtual_rows_allow_fills_but_reject_reordered_overlap(self):
        for reordered in (False, True):
            with self.subTest(reordered=reordered):
                position = {"top": 40}
                def evaluate(script, *args):
                    if script == GRID_DOM:
                        ids = [0, 1] if position["top"] < 75 else ([1, 0, 2] if reordered else [1, 2])
                        rows = [{"cells": {"prodId": {"id": f"cell{i}_prodId", "text": "005930"},
                                           "cumQty": {"id": f"cell{i}_cumQty", "text": str(position["top"])}}} for i in ids]
                        return {"headers": [{"id": c, "text": c} for c in ("prodId", "cumQty")],
                                "rows": rows, "filtered": False, "count_text": "전체 3"}
                    original = position["top"]
                    if args[0] is not None:
                        position["top"] = args[0]
                    return {"top": position["top"], "original": original, "max": 100, "height": 100}
                table = SimpleNamespace(evaluate=evaluate)
                if reordered:
                    with self.assertRaisesRegex(MirrorBlocked, "changed_during_scroll"):
                        self.browser._grid_dom(table, expected=3)
                else:
                    result = self.browser._grid_dom(table, expected=3)
                    self.assertEqual(len(result["rows"]), 3)
                    self.assertEqual(result["rows"][1]["cells"]["cumQty"]["text"], "100")
                self.assertEqual(position["top"], 40)

    def test_virtual_order_change_is_not_silently_joined_to_csv(self):
        position = {"top": 40}
        def evaluate(script, *args):
            if script == GRID_DOM:
                return {"headers": [{"id": "prodId", "text": "종목"}],
                        "rows": [{"cells": {"prodId": {"id": "cell123_prodId", "text": "005930" if position["top"] < 75 else "000660"}}}],
                        "filtered": False, "count_text": "전체 3"}
            original = position["top"]
            if args[0] is not None:
                position["top"] = args[0]
            return {"top": position["top"], "original": original, "max": 100, "height": 100}
        with self.assertRaisesRegex(MirrorBlocked, "changed_during_scroll"):
            self.browser._grid_dom(SimpleNamespace(evaluate=evaluate), expected=3)
        self.assertEqual(position["top"], 40)


class TimefolioGridDOMBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import Error, sync_playwright
        except ModuleNotFoundError as error:
            if error.name and error.name.startswith("playwright"):
                raise unittest.SkipTest("Optional Playwright is not installed") from None
            raise
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        try:
            cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)
        except Error as error:
            if "Executable doesn't exist" in str(error) or "distribution 'chrome' is not found" in str(error):
                raise unittest.SkipTest("Optional Chrome is not installed") from None
            raise
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.route("**/*", lambda route: route.abort())
        self.page = context.new_page()
        self.page.set_content("""<div class="space-y-1"><table id="outer">
          <thead><tr>
            <th><button><span>종목</span><span style="background-color: yellow;"></span></button></th>
            <th><button><span>잔고 / 비중 / 평가액</span></button></th>
            <th><button><span>손익률 / 손익</span></button></th>
          </tr><tr>
            <th id="prodId"><button>코드<span id="filter" style="background-color: yellow;"></span></button></th>
          </tr></thead><tbody><tr><td id="nested"></td></tr></tbody>
        </table></div>""")

    def test_group_header_labels_with_empty_filter_badges_are_unfiltered(self):
        result = self.page.locator("#outer").evaluate(GRID_DOM)
        self.assertFalse(result["filtered"])
        self.assertEqual(result["headers"], [{"id": "prodId", "text": "코드"}])

    def test_nonempty_yellow_filter_badge_is_filtered(self):
        self.page.locator("#filter").evaluate("e => e.textContent = '005930'")
        self.assertTrue(self.page.locator("#outer").evaluate(GRID_DOM)["filtered"])

    def test_filter_in_nested_table_does_not_filter_parent(self):
        self.page.locator("#nested").evaluate("""e => e.innerHTML = `<table id="inner">
          <thead><tr><th id="nestedCode"><button>코드
            <span style="background-color: yellow;">005930</span>
          </button></th></tr></thead><tbody></tbody></table>`""")
        self.assertFalse(self.page.locator("#outer").evaluate(GRID_DOM)["filtered"])
        self.assertTrue(self.page.locator("#inner").evaluate(GRID_DOM)["filtered"])

    def test_completed_local_ui_request_closes_without_unhandled_task_warning(self):
        script = textwrap.dedent(r'''
            import gc
            from playwright.sync_api import sync_playwright
            from backend.timefolio_browser import TimefolioBrowser

            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome", headless=True)
                context = browser.new_context(service_workers="block")
                html = """<button id="orders" onclick="fetch('/api/Portfolio/Orders')">Orders</button>"""
                def fulfill(route):
                    fixture = route.request.url.endswith("/fixture")
                    route.fulfill(status=200, content_type="text/html" if fixture else "application/json",
                                  body=html if fixture else "{}")
                context.route("**/*", fulfill)
                page = context.new_page()
                page.goto("https://contest.timefolio.net/fixture")
                adapter = TimefolioBrowser("unused-local-fixture-profile")
                adapter.page = page
                adapter._ui_query("/Portfolio/Orders", page.locator("#orders").click)
                context.close()
                browser.close()
                gc.collect()
            gc.collect()
        ''')
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Task exception was never retrieved", result.stderr)
        self.assertNotIn("Target closed", result.stderr)


if __name__ == "__main__":
    unittest.main()
