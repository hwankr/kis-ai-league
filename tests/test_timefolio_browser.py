"""Fake adapter tests and optional local HTML checks. Never contacts a website."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.kis import KST
from backend.mirror_runtime import MirrorBlocked, MirrorRejected, MirrorUnknown
from backend.timefolio_browser import CONTEST, GRID_DOM, GUIDELINE_DOM, ORIGIN, TimefolioBrowser, match_receipt
from backend.timefolio_dom import parse_receipts


NOW = datetime(2026, 10, 12, 10, 0, tzinfo=KST)

# Official visible guideline DOM verified on 2026-10-12; no remote data is loaded.
GUIDELINE_HTML = """<button>가이드라인 위반</button><table class="simple-tab guideline-tab"><tbody>
 <tr><td title="주간 순자산 대비 평균 거래대금 비율">주간 회전율<span class="badge">?</span></td>
 <td>매주 5% 이상 유지</td><td><span style="color: red;">금주 2.3%</span></td><td></td></tr>
 <tr><td>주간회전율 위반</td><td>위반 3번까지 허용<br>4번 이상 순위/시상 제외</td>
 <td>금주 제외 (확정)<br>누적<span style="color: red;">0</span>번 위반</td>
 <td>금주 포함 (예상)<br>누적<span style="color: black;">4</span>번 위반</td></tr>
 </tbody></table>"""


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
        self.page.wait_calls.append(self.key)
        if self.key in self.page.wait_errors:
            raise self.page.wait_errors[self.key]
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
        if "validationErrors" in expression:
            return {**deepcopy(self.page.fields), "errors": deepcopy(self.page.validation_errors)}
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
        self.wait_calls, self.wait_errors = [], {}
        self.counts = {"role:dialog:": 0, 'table:has(> thead th[id="avgPx"])': 0,
                       'input[type="password"]:visible': 0}
        self.search = "005930"
        self.click_error = None
        self.csv_message = "'주문_내역.csv' 로 다운로드할까요?"
        self.dialog_handler = None
        self.last_dialog = None
        self.validation_ok = True
        self.validation_status = 200
        self.validation_errors = []
        self.validation_render = None
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
        if ("requestAnimationFrame" in expression and self.validation_render
                and self.selected_response and "ValidateProduct" in self.selected_response.url):
            self.validation_render()
        return None

    def expect_response(self, predicate, **kwargs):
        urls = [ORIGIN + "/Portfolio/ValidateProduct?d=" + self.fields["date"]
                + "&prodId=A" + self.search + "&entry=" + self.fields["side"]]
        urls += [ORIGIN + "/Portfolio/" + name for name in ("Session", "Orders", "Universe")]
        candidates = [SimpleNamespace(url=url, request=SimpleNamespace(method="GET"),
                      ok=self.validation_ok if "ValidateProduct" in url else True,
                      status=self.validation_status if "ValidateProduct" in url else 200, finished=lambda: None) for url in urls]
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
    def test_liveness_detects_closed_page_or_disconnected_browser_without_navigation(self):
        browser = TimefolioBrowser(Path("unused-profile"))
        self.assertFalse(browser.is_alive())
        browser.context, browser.page = Mock(), Mock()
        browser.page.is_closed.return_value = False
        browser.context.browser.is_connected.return_value = True
        self.assertTrue(browser.is_alive())
        browser.page.is_closed.return_value = True
        self.assertFalse(browser.is_alive())
        browser.page.is_closed.return_value = False
        browser.context.browser.is_connected.return_value = False
        self.assertFalse(browser.is_alive())
        browser.context.browser = None
        self.assertFalse(browser.is_alive())
        browser.page.goto.assert_not_called()

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

    def test_product_validation_accepts_optional_api_prefix_for_buy_and_sell(self):
        for prefix in ("", "/api"):
            for side, entry in (("buy", "true"), ("sell", "false")):
                with self.subTest(prefix=prefix, side=side):
                    self.page.fields["side"] = entry
                    url = ORIGIN + prefix + "/Portfolio/ValidateProduct?d=2026-10-12&prodId=A005930&entry=" + entry
                    self.page.response_candidates = [SimpleNamespace(
                        url=url, request=SimpleNamespace(method="GET"), ok=True)]
                    suggestion = Mock()
                    self.browser._select_validated(FakeLocator(self.page, "dialog"), suggestion, "005930", side=side)
                    suggestion.click.assert_called_once_with()
                    self.assertEqual(self.page.responses[-1], url)
        self.assertEqual(self.submit_clicks(), 0)

    def test_product_validation_rejects_wrong_origin_method_date_symbol_side_and_path(self):
        for prefix in ("", "/api"):
            path = prefix + "/Portfolio/ValidateProduct"
            for side, entry, wrong_entry in (("buy", "true", "false"), ("sell", "false", "true")):
                self.page.fields["side"] = entry
                query = "?d=2026-10-12&prodId=A005930&entry=" + entry
                for url, method in (
                    ("https://example.invalid" + path + query, "GET"),
                    ("http://contest.timefolio.net" + path + query, "GET"),
                    (ORIGIN + path + query, "POST"),
                    (ORIGIN + path + query.replace("2026-10-12", "2026-10-11"), "GET"),
                    (ORIGIN + path + query.replace("A005930", "A000660"), "GET"),
                    (ORIGIN + path + query.replace("entry=" + entry, "entry=" + wrong_entry), "GET"),
                    (ORIGIN + path + query + "&prodId=A000660", "GET"),
                    (ORIGIN + path.replace("ValidateProduct", "Other") + query, "GET"),
                ):
                    with self.subTest(prefix=prefix, side=side, url=url, method=method):
                        self.page.response_candidates = [SimpleNamespace(
                            url=url, request=SimpleNamespace(method=method), ok=True)]
                        with self.assertRaises(TimeoutError):
                            self.browser._select_validated(FakeLocator(self.page, "dialog"), Mock(), "005930", side=side)
        self.assertEqual(self.page.responses, [])
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

    def test_resume_after_contest_end_during_form_verification_never_clicks(self):
        current = proposal(session_date="2026-11-30")
        self.browser._prepared = current
        self.browser.now = lambda: NOW.replace(month=11, day=30)
        def verify(_proposal):
            self.browser.now = lambda: NOW.replace(month=12, day=1)
        self.browser._verify_form = Mock(side_effect=verify)
        with self.assertRaisesRegex(MirrorBlocked, "outside_order_session"):
            self.browser.submit(current)
        self.browser._verify_form.assert_called_once_with(current)
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

    def test_root_english_login_is_reported_before_account_reads_or_orders(self):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        self.page.wait_errors['role:link:설정'] = BrowserTimeout('account navigation unavailable')
        self.page.url = ORIGIN + "/"
        self.page.counts['input[type="password"]:visible'] = 1
        self.page.counts['role:button:로그인'] = 0
        self.browser._account_key, self.browser._portfolio_id = "account", "pf13"
        with self.assertRaisesRegex(MirrorBlocked, "timefolio_login_required"):
            self.browser.account_snapshot()
        self.assertIsNone(self.browser._account_key)
        self.assertIsNone(self.browser._portfolio_id)
        self.assertIsNone(self.browser._prepared)
        self.assertEqual(self.submit_clicks(), 0)

    def test_changed_contest_blocks_submit_before_click(self):
        self.page.contest = "Training"
        with self.assertRaisesRegex(MirrorBlocked, "binding_changed"):
            self.browser.submit(proposal())
        self.assertEqual(self.submit_clicks(), 0)

    def test_session_restoration_form_waits_for_account_navigation(self):
        self.page.url = ORIGIN + '/'
        self.page.counts['input[type="password"]:visible'] = 1
        self.browser._account_key, self.browser._portfolio_id = 'account', 'pf13'
        self.assertEqual(self.browser.identity(), 'account')
        self.assertIn('role:link:설정', self.page.wait_calls)
        self.assertEqual(self.browser._portfolio_id, 'pf13')
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

    def _business_validation(self, *, errors=None, changes=None):
        self.page.validation_ok, self.page.validation_status = False, 400
        messages = ["5일 평균거래대금 부족 (거래대금 30억 미만)"] if errors is None else errors
        def render():
            self.assertTrue(any(request is self.page.selected_response.request for request in self.page.finished_requests))
            self.page.validation_errors = list(messages)
            self.page.fields.update(changes or {})
        self.page.validation_render = render

    def test_buy_business_400_rejects_after_completion_even_when_site_switches_to_sell(self):
        for final_side in ("true", "false"):
            with self.subTest(final_side=final_side):
                self.page.validation_errors = []
                self._business_validation(changes={"side": final_side})
                with self.assertRaisesRegex(MirrorRejected, "5일 평균거래대금 부족"):
                    self.browser.prepare(proposal())
                self.assertIsNone(self.browser._prepared)
                self.assertEqual(self.submit_clicks(), 0)
                self.assertIn("entry=true", self.page.responses[-1])

    def test_buy_400_unrecognized_or_mixed_error_stays_blocked(self):
        for errors in ([], ["종목 거래 제한"], ["요청 횟수 제한"], ["인증 권한 부족"], ["허용 종목입니다"],
                       ["5일 평균거래대금 부족 (거래대금 30억 미만)", "서버 오류"]):
            with self.subTest(errors=errors):
                self.page.validation_errors = []
                self._business_validation(errors=errors)
                with self.assertRaisesRegex(MirrorBlocked, "stock_validation_failed"):
                    self.browser.prepare(proposal())
                self.assertIsNone(self.browser._prepared)
                self.assertEqual(self.submit_clicks(), 0)

    def test_buy_400_stale_business_error_cannot_reject_current_request(self):
        self.page.validation_errors = ["5일 평균거래대금 부족 (거래대금 30억 미만)"]
        self._business_validation()
        with self.assertRaisesRegex(MirrorBlocked, "stock_validation_failed"):
            self.browser.prepare(proposal())
        self.assertEqual(self.submit_clicks(), 0)

    def test_buy_400_changed_or_ambiguous_form_identity_stays_blocked(self):
        for field, value in (("symbol", "A000660"), ("date", "2026-10-13"), ("symbol", None),
                             ("date", None), ("side", None), ("side", ""), ("side", "invalid")):
            with self.subTest(field=field, value=value):
                self.page.fields.update(symbol="A005930", date="2026-10-12", side="true")
                self.page.validation_errors = []
                self._business_validation(changes={field: value})
                with self.assertRaisesRegex(MirrorBlocked, "form_changed"):
                    self.browser.prepare(proposal())
                self.assertIsNone(self.browser._prepared)
                self.assertEqual(self.submit_clicks(), 0)

    def test_buy_400_changed_binding_is_not_a_business_rejection(self):
        self._business_validation()
        render = self.page.validation_render
        def changed():
            render()
            self.page.contest = "Training"
        self.page.validation_render = changed
        with self.assertRaisesRegex(MirrorBlocked, "binding_changed"):
            self.browser.prepare(proposal())
        self.assertEqual(self.submit_clicks(), 0)

    def test_product_nonbusiness_http_failure_never_waits_or_rejects(self):
        self.page.validation_ok = False
        self.page.validation_errors = ["5일 평균거래대금 부족 (거래대금 30억 미만)"]
        self.page.finish_error = AssertionError("nonbusiness HTTP failure must not wait")
        for status in (None, 200, 401, 403, 404, 409, 422, 429, 500, 503):
            with self.subTest(status=status):
                self.page.validation_status = status
                with self.assertRaisesRegex(MirrorBlocked, "stock_validation_failed"):
                    self.browser._select_validated(FakeLocator(self.page, "dialog"), FakeLocator(self.page, "li"), "005930")
        self.assertEqual(self.page.finished_requests, [])
        self.assertEqual(self.submit_clicks(), 0)

    def test_buy_business_400_completion_timeout_remains_retryable_blocker(self):
        self._business_validation()
        self.page.finish_error = TimeoutError("exact validation request body did not finish")
        with self.assertRaises(TimeoutError):
            self.browser._select_validated(FakeLocator(self.page, "dialog"), FakeLocator(self.page, "li"), "005930")
        self.assertEqual(self.page.validation_errors, [])
        self.assertEqual(self.submit_clicks(), 0)

    def test_buy400_matches_original_request_not_concurrent_sell200(self):
        buy = SimpleNamespace(url=ORIGIN + "/api/Portfolio/ValidateProduct?d=2026-10-12&prodId=A005930&entry=true",
                              request=SimpleNamespace(method="GET"), ok=False, status=400)
        sell = SimpleNamespace(url=ORIGIN + "/api/Portfolio/ValidateProduct?d=2026-10-12&prodId=A005930&entry=false",
                               request=SimpleNamespace(method="GET"), ok=True, status=200)
        self.page.response_candidates = [sell, buy]
        self.page.finished_candidates = [sell.request, buy.request]
        self._business_validation(changes={"side": "false"})
        with self.assertRaises(MirrorRejected):
            self.browser._select_validated(FakeLocator(self.page, "dialog"), FakeLocator(self.page, "li"), "005930")
        self.assertIs(self.page.selected_response, buy)
        self.assertIs(self.page.finished_requests[-1], buy.request)
        self.page.fields["side"], self.page.validation_errors = "true", []
        self.page.finished_candidates = [sell.request]
        with self.assertRaises(TimeoutError):
            self.browser._select_validated(FakeLocator(self.page, "dialog"), FakeLocator(self.page, "li"), "005930")
        self.assertEqual(self.submit_clicks(), 0)

    def test_preselection_wrong_side_and_sell400_stay_blocked(self):
        self.page.fields["side"] = "false"
        with self.assertRaisesRegex(MirrorBlocked, "form_changed"):
            self.browser._select_validated(FakeLocator(self.page, "dialog"), FakeLocator(self.page, "li"), "005930")
        self.assertEqual(self.page.responses, [])
        self._business_validation()
        with self.assertRaisesRegex(MirrorBlocked, "stock_validation_failed"):
            self.browser._select_validated(FakeLocator(self.page, "dialog"), FakeLocator(self.page, "li"), "005930", side="sell")
        self.assertEqual(self.page.finished_requests, [])
        self.assertEqual(self.submit_clicks(), 0)

    def test_preselection_stale_missing_or_invalid_day_never_selects_or_rejects(self):
        self._business_validation()
        for day in (None, "", "2026-10-11", "2026-10-13", "20261012"):
            with self.subTest(day=day):
                self.page.fields["date"] = day
                with self.assertRaisesRegex(MirrorBlocked, "form_changed"):
                    self.browser._select_validated(FakeLocator(self.page, "dialog"), FakeLocator(self.page, "li"), "005930")
        self.assertEqual(self.page.responses, [])
        self.assertEqual(self.submit_clicks(), 0)

    def test_successful_validation_side_switch_cannot_prepare_an_order(self):
        self.page.validation_render = lambda: self.page.fields.update(side="false")
        with self.assertRaisesRegex(MirrorBlocked, "form_changed"):
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
                    with self.assertRaisesRegex(MirrorBlocked, "changed_during_scroll") as caught:
                        self.browser._grid_dom(table, expected=3)
                    trace = caught.exception.collection_grid
                    self.assertEqual(trace[-1]["seen_indices"], [1, 0, -1])
                    self.assertEqual(trace[-1]["known"], 2)
                    self.assertNotIn("005930", str(trace))
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


class _ReadRemountPage(FakePage):
    """A post-submit view whose Orders link cannot complete its click."""
    def __init__(self):
        super().__init__()
        self.navigations = []
        self.order_link_blocked = True
        self.after_navigation = None
        self.waits = []

    def goto(self, url, **kwargs):
        self.navigations.append((url, kwargs))
        self.url = url
        self.order_link_blocked = False
        if self.after_navigation:
            self.after_navigation()

    def get_by_role(self, role, name=None, **kwargs):
        locator = super().get_by_role(role, name=name, **kwargs)
        if role == "link" and name == "주문" and self.order_link_blocked:
            locator.click = Mock(side_effect=TimeoutError("synthetic post-submit Orders link is blocked"))
        return locator

    def wait_for_function(self, expression):
        self.waits.append(expression)


class TimefolioReadRemountTests(unittest.TestCase):
    def setUp(self):
        prevent_open = patch.object(TimefolioBrowser, "open", side_effect=AssertionError("No browser may open in tests"))
        self.addCleanup(prevent_open.stop)
        prevent_open.start()
        self.browser = TimefolioBrowser("unused-offline-profile", now=lambda: NOW, sleep=lambda _: None)
        self.page = _ReadRemountPage()
        self.browser.page = self.page
        self.browser._account_key = "account"
        self.browser._portfolio_id = "pf13"

    def test_receipt_view_reloads_exact_origin_without_clicking_blocked_link(self):
        self.browser._main(receipts_only=True)
        self.assertEqual(self.page.navigations, [(ORIGIN, {"wait_until": "domcontentloaded"})])
        self.assertNotIn("role:link:주문", self.page.clicks)
        self.assertEqual(self.page.waits, [])

    def test_full_view_keeps_nav_validation_after_reload(self):
        self.browser._main(receipts_only=False)
        self.assertEqual(len(self.page.navigations), 1)
        self.assertEqual(len(self.page.waits), 1)
        self.assertIn("NAV", self.page.waits[0])

    def test_document_ready_before_app_mount_waits_for_view_before_binding(self):
        for receipts_only, table in ((True, "table:has(th#w2o)"), (False, "table:has(th#pos)")):
            with self.subTest(receipts_only=receipts_only):
                visible = []
                self.page.after_navigation = lambda: self.page.counts.update({"select": 0})

                def locator(selector, **kwargs):
                    result = FakePage.locator(self.page, selector, **kwargs)
                    if selector == table:
                        result.wait_for = lambda **kw: visible.append((table, kw))
                    return result

                def get_by_role(role, name=None, **kwargs):
                    result = FakePage.get_by_role(self.page, role, name=name, **kwargs)
                    if role == "button" and getattr(name, "pattern", name) == "신규 주문":
                        def app_mounted(**kw):
                            self.assertEqual(visible, [(table, {"state": "visible"})])
                            visible.append(("new_order", kw))
                            self.page.counts["select"] = 1
                        result.wait_for = app_mounted
                    return result

                with patch.object(self.page, "locator", side_effect=locator), \
                     patch.object(self.page, "get_by_role", side_effect=get_by_role):
                    self.browser._main(receipts_only=receipts_only)
                self.assertEqual(visible[-1], ("new_order", {"state": "visible"}))
                self.assertEqual(self.browser._portfolio_id, "pf13")

    def test_login_page_before_reload_blocks_without_navigation(self):
        self.page.url = ORIGIN + "/auth/login"
        with self.assertRaisesRegex(MirrorBlocked, "timefolio_login_required"):
            self.browser._main(receipts_only=True)
        self.assertEqual(self.page.navigations, [])

    def test_login_redirect_after_reload_blocks_and_clears_binding(self):
        self.page.after_navigation = lambda: setattr(self.page, "url", ORIGIN + "/auth/login")
        with self.assertRaisesRegex(MirrorBlocked, "timefolio_login_required"):
            self.browser._main(receipts_only=True)
        self.assertIsNone(self.browser._account_key)

    def test_changed_portfolio_after_reload_does_not_rebind(self):
        self.page.after_navigation = lambda: setattr(self.page, "portfolio_id", "different")
        with self.assertRaisesRegex(MirrorBlocked, "destination_binding_changed"):
            self.browser._main(receipts_only=True)
        self.assertEqual(self.browser._portfolio_id, "pf13")

    def test_reload_timeout_after_submit_never_repeats_submission(self):
        self.browser._prepared = proposal()
        self.page.goto = Mock(side_effect=TimeoutError("synthetic document GET timeout"))
        with self.assertRaises(TimeoutError):
            self.browser.submit(proposal())
        self.assertEqual(self.page.clicks.count('button[type="submit"]'), 1)
        with self.assertRaises(MirrorBlocked):
            self.browser.submit(proposal())
        self.assertEqual(self.page.clicks.count('button[type="submit"]'), 1)

    def test_postsubmit_reload_confirms_only_exact_receipt_with_one_submission(self):
        self.browser._prepared = proposal()
        observed = []
        def receipt_snapshot():
            self.browser._main(receipts_only=True)
            # The first observation has a wrong CSV weight. A second read can
            # resolve the exact receipt, but must never cause a second submit.
            observed.append(True)
            row = receipt(weight="2.01" if len(observed) == 1 else "2.00")
            return {"verified": True, "account_key": "account", "contest": CONTEST,
                    "session_date": "2026-10-12", "orders": [row]}
        self.browser.receipt_snapshot = receipt_snapshot
        self.assertEqual(self.browser.submit(proposal())["order_id"], "new")
        self.assertEqual(self.page.clicks.count('button[type="submit"]'), 1)
        self.assertEqual(len(self.page.navigations), 2)


# Synthetic 38-row order history with the observed 21 -> 36 -> 38 window shape.
_VIRTUAL_TAIL_COLUMNS = ("genT", "prodId", "sgn", "wei", "cumQty")
_VIRTUAL_TAIL_VALUES = [["2026-10-19 09:00", f"A{i:06d}", "1", "1", "0"] for i in range(38)]
_VIRTUAL_TAIL_FULL = {
    "headers": [{"id": key, "text": key} for key in _VIRTUAL_TAIL_COLUMNS],
    "rows": [{"cells": {key: {"id": f"cell{i}_{key}", "text": value}
                        for key, value in zip(_VIRTUAL_TAIL_COLUMNS, values)}}
             for i, values in enumerate(_VIRTUAL_TAIL_VALUES)],
    "filtered": False, "count_text": "전체 38"}
_VIRTUAL_TAIL_CSV = "\n".join([",".join(_VIRTUAL_TAIL_COLUMNS)] +
                                [",".join(values) for values in _VIRTUAL_TAIL_VALUES])


class DelayedTail(FakeLocator):
    def __init__(self, page, *, delay=2, growing=False, corruption=None):
        super().__init__(page, "table")
        self.top, self.original = 40, 40
        self.extent = 299 if growing else 426
        self.delay, self.growing, self.corruption = delay, growing, corruption
        self.bottom_reads = 0
        self.positions = []

    def evaluate(self, script, *args):
        if script != GRID_DOM:
            original = self.top
            if args[0] is not None:
                self.top = min(args[0], self.extent)
                self.positions.append(self.top)
                if self.top == 0:
                    self.bottom_reads = 0
            return {"top": self.top, "original": original, "max": self.extent, "height": 399}
        result = deepcopy(_VIRTUAL_TAIL_FULL)
        if self.growing and self.top == 299:
            self.extent = 426
            result["rows"] = result["rows"][19:36]
        elif self.top >= 426:
            self.bottom_reads += 1
            end = 38 if self.bottom_reads >= self.delay else 36
            result["rows"] = result["rows"][19:end]
            if end == 38:
                if self.corruption == "fixed":
                    result["rows"][0]["cells"]["prodId"]["text"] = "A999999"
                elif self.corruption == "duplicate":
                    result["rows"][1] = deepcopy(result["rows"][0])
                elif self.corruption == "reorder":
                    result["rows"][0:2] = reversed(result["rows"][0:2])
                elif self.corruption == "new_tail_reorder":
                    result["rows"][-2:] = reversed(result["rows"][-2:])
        else:
            result["rows"] = result["rows"][:21]
        return result


class LaggedOrderWindow(FakeLocator):
    """Observed 55-row geometry: top 0 can still render the previous tail 36..54."""
    def __init__(self, page, *, delay=2, corruption=None):
        super().__init__(page, "table")
        self.top, self.original, self.reads = 769, 769, 0
        self.delay, self.corruption = delay, corruption
        columns = _VIRTUAL_TAIL_COLUMNS
        self.values = [["2026-10-21 09:00", f"A{i:06d}", "1", "1", "0"] for i in range(55)]
        self.full = {"headers": [{"id": key, "text": key} for key in columns],
                     "rows": [{"index": i, "cells": {key: {"id": f"cell{i}_{key}", "text": value}
                                                        for key, value in zip(columns, values)}}
                              for i, values in enumerate(self.values)],
                     "filtered": False, "count_text": "전체 55"}
        self.csv = "\n".join([",".join(columns)] + [",".join(values) for values in self.values])

    def evaluate(self, script, *args):
        if script != GRID_DOM:
            original = self.top
            if args[0] is not None and int(args[0]) != self.top:
                self.top, self.reads = min(int(args[0]), 769), 0
            return {"top": self.top, "original": original, "max": 769, "height": 399}
        self.reads += 1
        result = deepcopy(self.full)
        if self.reads <= self.delay:
            start, end = (36, 55) if self.top == 0 else ((27, 51) if self.top == 769 else (0, 21))
        else:
            start, end = ((0, 21) if self.top == 0 else (12, 36) if self.top < 598
                          else (27, 51) if self.top < 769 else (36, 55))
        result["rows"] = result["rows"][start:end]
        if self.reads > self.delay and self.top == 299:
            if self.corruption == "reorder":
                result["rows"][0]["cells"], result["rows"][1]["cells"] = (
                    result["rows"][1]["cells"], result["rows"][0]["cells"])
            elif self.corruption == "fixed":
                result["rows"][0]["cells"]["prodId"]["text"] = "A999999"
            elif self.corruption == "gap":
                result["rows"] = result["rows"][13:]
        return result


class VirtualWindowLagTests(unittest.TestCase):
    def make(self, **kwargs):
        waits = []
        browser = TimefolioBrowser("unused-offline-profile", sleep=waits.append)
        browser.page = FakePage()
        browser.page.dialog_handler = browser._dialog
        return browser, LaggedOrderWindow(browser.page, **kwargs), waits

    def test_observed_stale_tail_and_delayed_middle_wait_before_accumulating_rows(self):
        browser, table, waits = self.make()
        result = browser._grid_dom(table, 55)
        self.assertEqual(result["rows"], table.full["rows"])
        self.assertTrue(waits)
        self.assertEqual(table.top, table.original)
        self.assertNotIn('button[type="submit"]', browser.page.clicks)

    def test_lagged_complete_grid_still_joins_csv_in_original_order(self):
        browser, table, _ = self.make()
        with patch.object(Path, "read_text", return_value=table.csv):
            grid = browser._grid(table, "orders")
        self.assertTrue(grid["complete"])
        self.assertEqual(grid["rows"], table.full["rows"])
        self.assertEqual(table.top, table.original)

    def test_head_must_settle_within_five_reads_without_accepting_permanent_tail(self):
        browser, table, waits = self.make(delay=5)
        with self.assertRaisesRegex(MirrorBlocked, "render_unsettled"):
            browser._grid_dom(table, 55)
        self.assertEqual(waits, [.05] * 4)
        self.assertEqual(table.top, table.original)

    def test_real_order_change_fixed_field_change_or_missing_middle_stays_blocked(self):
        for corruption in ("reorder", "fixed", "gap"):
            with self.subTest(corruption=corruption):
                browser, table, _ = self.make(corruption=corruption)
                with self.assertRaises(MirrorBlocked):
                    browser._grid_dom(table, 55)
                self.assertEqual(table.top, table.original)


class VirtualTailTests(unittest.TestCase):
    def make(self, browser_class=TimefolioBrowser, **kwargs):
        waits = []
        browser = browser_class("unused-offline-profile", sleep=waits.append)
        page = FakePage()
        page.dialog_handler = browser._dialog
        browser.page = page
        return browser, DelayedTail(page, **kwargs), waits

    def test_observed_36_of_38_tail_delay_is_reproduced_and_recovered(self):
        new, table, waits = self.make()
        result = new._grid_dom(table, 38)
        self.assertEqual(result["rows"], _VIRTUAL_TAIL_FULL["rows"])
        self.assertEqual(waits, [.05])
        self.assertEqual(table.top, table.original)

    def test_tail_can_settle_on_last_bounded_observation(self):
        browser, table, waits = self.make(delay=5)
        self.assertEqual(browser._grid_dom(table, 38)["rows"], _VIRTUAL_TAIL_FULL["rows"])
        self.assertEqual(waits, [.05] * 4)
        self.assertEqual(table.top, table.original)

    def test_ready_tail_needs_no_added_wait(self):
        browser, table, waits = self.make(delay=1)
        self.assertEqual(browser._grid_dom(table, 38)["rows"], _VIRTUAL_TAIL_FULL["rows"])
        self.assertEqual(waits, [])

    def test_extent_growth_after_render_does_not_use_old_bottom(self):
        browser, table, waits = self.make(delay=1, growing=True)
        self.assertEqual(browser._grid_dom(table, 38)["rows"], _VIRTUAL_TAIL_FULL["rows"])
        self.assertIn(426, table.positions)
        self.assertEqual(table.top, table.original)

    def test_permanent_missing_tail_remains_incomplete_and_unverified(self):
        browser, table, waits = self.make(delay=999)
        with patch.object(Path, "read_text", return_value=_VIRTUAL_TAIL_CSV):
            grid = browser._grid(table, "orders")
        self.assertFalse(grid["complete"])
        self.assertEqual(len(grid["rows"]), 36)
        self.assertLessEqual(len(waits), 16)
        self.assertEqual(table.top, table.original)
        parsed = parse_receipts({"url": ORIGIN, "account_key": "account",
            "contest": CONTEST, "session_date": "2026-10-19", "tables": [grid]},
            "account", CONTEST)
        self.assertFalse(parsed["verified"])

    def test_late_reorder_duplicate_or_fixed_field_change_is_blocked(self):
        for corruption in ("reorder", "duplicate", "fixed"):
            with self.subTest(corruption=corruption):
                browser, table, _ = self.make(corruption=corruption)
                with self.assertRaisesRegex(MirrorBlocked, "changed_during_scroll"):
                    browser._grid_dom(table, 38)
                self.assertEqual(table.top, table.original)

    def test_new_tail_wrong_order_is_still_rejected_by_csv_dom_join(self):
        browser, table, _ = self.make(corruption="new_tail_reorder")
        with patch.object(Path, "read_text", return_value=_VIRTUAL_TAIL_CSV):
            grid = browser._grid(table, "orders")
        self.assertTrue(grid["complete"])
        parsed = parse_receipts({"url": ORIGIN, "account_key": "account",
            "contest": CONTEST, "session_date": "2026-10-19", "tables": [grid]},
            "account", CONTEST)
        self.assertFalse(parsed["verified"])
        self.assertTrue(any("행 순서 불일치" in error for error in parsed["errors"]))



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

    def _selected_order_form(self):
        self.page.set_content("""<div role="dialog" aria-label="신규 주문">
          <input type="text" placeholder="삼성SDI" value="A006400">
          <input name="d" type="date" value="2026-10-12">
          <input type="number" step="0.01" value="2.00">
          <input type="radio" name="매수도" value="true" checked>
          <input type="radio" name="prcTy" value="Opp" checked>
          <input type="radio" name="isSlice" value="false" checked>
          <input type="number" max="10" value="5">
        </div>""")
        adapter = TimefolioBrowser("unused-local-form-profile")
        adapter.page = self.page
        adapter._binding = Mock()
        return adapter

    def test_selected_stock_placeholder_is_a_label_and_actual_symbol_is_verified(self):
        adapter = self._selected_order_form()
        result = adapter._verify_form(proposal(symbol="006400"))
        self.assertTrue(result["verified"])
        self.assertEqual(result["symbol"], "006400")

    def test_validation_form_reads_identity_and_only_visible_red_errors_together(self):
        adapter = self._selected_order_form()
        self.page.locator('[role="dialog"]').evaluate("""e=>e.insertAdjacentHTML('beforeend', `<fieldset>
          <div style="color:red">5일 평균거래대금 부족 (거래대금 30억 미만)</div>
          <div style="color:red;display:none">hidden-display</div>
          <div style="color:red;visibility:hidden">hidden-visibility</div>
          <div style="color:green">not-error</div></fieldset>`)""")
        dialog = self.page.get_by_role("dialog", name="신규 주문")
        self.assertEqual(adapter._validation_form(dialog), {"symbol": "A006400", "date": "2026-10-12", "side": "true",
            "errors": ["5일 평균거래대금 부족 (거래대금 30억 미만)"]})
        self.page.locator('[role="dialog"]').evaluate("e=>e.insertAdjacentHTML('beforeend','<input placeholder=other value=A005930>')")
        self.assertIsNone(adapter._validation_form(dialog)["symbol"])

    def test_account_snapshot_reads_nav_and_full_positions_without_receipt_only_fallback(self):
        from tests.test_timefolio_account import observation
        source = observation()
        self.page.set_content("""<fieldset><legend>NAV</legend><span>1,000.08</span>
          <span>0.08 (0.01%)</span><span>327위 / 2071명</span></fieldset>""" + GUIDELINE_HTML)
        adapter = TimefolioBrowser("unused-local-account-profile")
        adapter.page, adapter._account_key = self.page, source["account_key"]
        adapter._main, adapter._binding = Mock(), Mock()
        adapter._date = Mock(return_value="2026-10-12")
        adapter._table = Mock()
        adapter._grid = Mock(side_effect=lambda table, kind: next(t for t in source["tables"] if t["kind"] == kind))
        adapter._receipt_tables = Mock(return_value=[t for t in source["tables"] if t["kind"] in {"orders", "order_details"}])
        # A local fixture is the only page; supply the expected origin as data, never navigate remotely.
        with patch("backend.timefolio_account.parse_snapshot", return_value={"verified": True, "session_date": "2026-10-12", "orders": []}):
            result = adapter.account_snapshot()
            self.assertEqual(result["summary"]["nav"], "1000.08")
            self.assertEqual(result["summary"]["rank"], 327)
            self.assertEqual(result["holdings"][0]["market_value"], "300")
            self.assertEqual(result["turnover"], {"current_pct": "2.3", "official_status": "below",
                                                  "missed_weeks": 0, "source": "official_guideline"})
            adapter._main.assert_called_once_with(receipts_only=False)
            self.page.locator("fieldset").evaluate("e=>e.after(e.cloneNode(true))")
            with self.assertRaisesRegex(ValueError, "account_nav_missing"):
                adapter.account_snapshot()
        with self.assertRaisesRegex(ValueError, "account_observation_requires_positions"):
            adapter._observation(account_view=True, receipts_only=True)

    def test_guideline_turnover_color_is_separate_from_confirmed_and_projected_counts(self):
        self.page.set_content(GUIDELINE_HTML.replace('color: red;">금주 2.3%', 'color: black;">금주 5.0%'))
        self.assertEqual(self.page.evaluate(GUIDELINE_DOM), {
            "current_text": "금주 5.0%", "current_color": "black", "confirmed_text": "금주 제외 (확정) 누적0번 위반"})

    def test_guideline_missing_ambiguous_or_changed_cells_do_not_supply_guessed_values(self):
        self.page.set_content("<p>주간 회전율 9.9%</p>")
        self.assertIsNone(self.page.evaluate(GUIDELINE_DOM))
        self.page.set_content(GUIDELINE_HTML + GUIDELINE_HTML)
        self.assertIsNone(self.page.evaluate(GUIDELINE_DOM))
        self.page.set_content(GUIDELINE_HTML.replace("주간 순자산 대비 평균 거래대금 비율", "다른 기준"))
        self.assertEqual(self.page.evaluate(GUIDELINE_DOM)["current_text"], "")
        self.page.set_content(GUIDELINE_HTML.replace("금주 제외 (확정)", "금주 포함 (예상)"))
        from backend.timefolio_account import _guideline
        self.assertIsNone(_guideline(self.page.evaluate(GUIDELINE_DOM))["missed_weeks"])

    def test_selected_form_rejects_wrong_missing_or_ambiguous_stock_and_missing_required_inputs(self):
        for change in (
            "document.querySelector('input[placeholder]').value='A005930'",
            "document.querySelector('input[placeholder]').remove()",
            "document.querySelector('[role=dialog]').appendChild(document.querySelector('input[placeholder]').cloneNode())",
            "document.querySelector('input[name=d]').remove()",
            "document.querySelector('input[step]').remove()",
            "document.querySelector('input[step]').value=''",
        ):
            with self.subTest(change=change):
                adapter = self._selected_order_form()
                self.page.evaluate(change)
                with self.assertRaisesRegex(MirrorBlocked, "destination_form_changed"):
                    adapter._verify_form(proposal(symbol="006400"))

    def test_receipt_tables_wait_for_expanded_details_rendered_on_animation_frames(self):
        self.page.set_content("""<button>가이드라인 위반</button>
          <table><thead><tr><th id="prodId">종목</th><th id="cancel">취소</th></tr></thead></table>
          <table id="targets"><thead><tr><th id="w2o">미체결</th></tr></thead>
          <tbody><tr><td data-colid="prodId">A006400</td><td>
          <button onclick="requestAnimationFrame(() => requestAnimationFrame(() => {
            document.querySelector('#targets tbody').insertAdjacentHTML('beforeend',
              '<tr><td colspan=2><table><thead><tr><th id=avgPx>평균가</th></tr></thead>' +
              '<tbody><tr><td>Done</td></tr></tbody></table></td></tr>');
          }))">모두 열기</button></td></tr></tbody></table>
          <table><thead><tr><th id="genT">주문시각</th></tr></thead></table>""")
        adapter = TimefolioBrowser("unused-local-receipt-profile")
        adapter.page = self.page
        adapter._ui_query = Mock()
        adapter._grid = Mock(side_effect=lambda table, kind, **kwargs: {"kind": kind, **kwargs})
        self.assertEqual(self.page.locator('table:has(> thead th[id="avgPx"])').count(), 0)
        tables = adapter._receipt_tables()
        self.assertEqual(tables, [{"kind": "unaccepted"},
                                 {"kind": "order_details", "parent_symbol": "006400"},
                                 {"kind": "orders"}])

    def test_receipt_tables_repeatedly_isolate_unaccepted_grid_from_seven_expanded_details(self):
        symbols = ["006400", "036930", "042700", "043260", "096770", "240810", "403870"]
        rows = "".join(f'<tr data-symbol="{symbol}"><td data-colid="prodId">A{symbol}</td></tr>' for symbol in symbols)
        self.page.set_content("""<button>가이드라인 위반</button>
          <table id="unaccepted"><thead><tr><th id="prodId">종목</th><th id="cancel">취소</th></tr></thead></table>
          <table id="targets"><thead><tr><th id="w2o">미체결<button onclick="
            if(!document.querySelector('#targets').dataset.expanded) {
              document.querySelector('#targets').dataset.expanded='1';
              requestAnimationFrame(() => Array.from(document.querySelectorAll('[data-symbol]')).forEach(row =>
                row.insertAdjacentHTML('afterend', '<tr><td><table><thead><tr>' +
                  '<th id=ls>조건</th><th id=state>상태</th><th id=avgPx>평균가</th><th id=cancel>정지</th>' +
                  '</tr></thead><tbody><tr><td>Done</td></tr></tbody></table></td></tr>')));
            }">모두 열기</button></th></tr></thead><tbody>""" + rows + """</tbody></table>
          <table><thead><tr><th id="genT">주문시각</th></tr></thead></table>""")
        adapter = TimefolioBrowser("unused-local-repeated-receipt-profile")
        adapter.page = self.page
        adapter._ui_query = Mock()
        adapter._grid = Mock(side_effect=lambda table, kind, **kwargs: {"kind": kind, **kwargs})
        self.assertEqual(self.page.locator('table:has(> thead th[id="cancel"])').count(), 1)
        for _ in range(3):
            tables = adapter._receipt_tables()
            self.assertEqual(self.page.locator('table:has(> thead th[id="cancel"])').count(), 8)
            self.assertEqual(adapter._table("cancel").get_attribute("id"), "unaccepted")
            self.assertEqual([row["parent_symbol"] for row in tables if row["kind"] == "order_details"], symbols)
            self.assertEqual(len(tables), 9)

    def test_receipt_tables_expand_new_order_rows_after_all_was_already_open(self):
        self.page.set_content("""<button>가이드라인 위반</button>
          <table><thead><tr><th id="prodId">종목</th><th id="cancel">취소</th></tr></thead></table>
          <table id="targets"><thead><tr><th id="w2o">미체결</th><th>
            <button id="expand" onclick="toggleAll()">모두 열기</button>
          </th></tr></thead><tbody>
            <tr data-has-detail data-symbol="006400"><td data-colid="prodId">A006400</td></tr>
          </tbody></table>
          <table><thead><tr><th id="genT">주문시각</th></tr></thead></table>
          <script>
          let expandAll=false;
          function toggleAll() {
            expandAll=!expandAll;
            document.querySelector('#expand').textContent=expandAll?'모두 닫기':'모두 열기';
            document.querySelectorAll('[data-detail]').forEach(row=>row.remove());
            if(expandAll) requestAnimationFrame(()=>requestAnimationFrame(()=>{
              document.querySelectorAll('[data-has-detail]').forEach(row=>
                row.insertAdjacentHTML('afterend', '<tr data-detail><td colspan=2><table>'+
                  '<thead><tr><th id="avgPx">평균가</th></tr></thead>'+
                  '<tbody><tr><td>Done 29</td></tr></tbody></table></td></tr>'));
            }));
          }
          </script>""")
        adapter = TimefolioBrowser("unused-local-new-row-profile")
        adapter.page = self.page
        adapter._ui_query = Mock()
        adapter._grid = Mock(side_effect=lambda table, kind, **kwargs: {"kind": kind, **kwargs})
        tables = adapter._receipt_tables()
        self.assertEqual([row['parent_symbol'] for row in tables if row['kind'] == 'order_details'],
                         ['006400'])
        self.assertEqual(self.page.locator('#expand').inner_text(), "모두 닫기")
        # The site's expansion effect follows the toggle, not incoming rows.
        self.page.evaluate("""() => document.querySelector('#targets tbody').insertAdjacentHTML('beforeend',
          '<tr data-has-detail data-symbol="373220"><td data-colid="prodId">A373220</td></tr>')""")
        self.assertEqual(self.page.locator('table:has(> thead th[id="avgPx"])').count(), 1)
        for _ in range(2):
            tables = adapter._receipt_tables()
            self.assertEqual([row['parent_symbol'] for row in tables if row['kind'] == 'order_details'],
                             ['006400', '373220'])

    def test_repeated_account_and_receipt_observations_remount_orders_and_refresh_full_period(self):
        html = """<button onclick="selectTab('guide')">가이드라인 위반</button>
          <button onclick="selectTab('orders')">주문 내역</button>
          <table><thead><tr><th id="prodId">종목</th><th id="cancel">취소</th></tr></thead></table>
          <table><thead><tr><th id="w2o">미체결</th></tr></thead></table>
          <table><thead><tr><th id="pos">보유</th></tr></thead></table>
          <fieldset><legend>NAV</legend><span>1000.00</span><span>0 (0%)</span><span>1위 / 2명</span></fieldset>
          <div id="content"></div>
          <script>
          let active='guide', period;
          async function refresh(p) {
            const response=await fetch('/api/Portfolio/Orders?period='+p);
            const data=await response.json();
            document.querySelector('#orders').dataset.generation=data.generation;
            document.querySelector('#orders').dataset.status=data.status;
          }
          function selectTab(tab) {
            if(active===tab) return;
            active=tab;
            document.querySelector('#content').innerHTML='';
            if(tab==='orders') {
              period='D';
              document.querySelector('#content').innerHTML='<label for="period_A" onclick="allPeriod()">전체 기간</label>'+
                '<table id="orders"><thead><tr><th id="genT">주문시각</th></tr></thead></table>';
              refresh(period);
            }
          }
          function allPeriod() { if(period!=='A') { period='A'; refresh(period); } }
          </script>"""
        requests = []
        def fulfill(route):
            if route.request.url == ORIGIN + "/fixture":
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
            elif route.request.url.startswith(ORIGIN + "/api/Portfolio/Orders?period="):
                requests.append(route.request.url.rsplit("=", 1)[1])
                route.fulfill(status=200, content_type="application/json",
                              body='{"generation":%d,"status":"%s"}' % (
                                  len(requests), "Done" if len(requests) >= 6 else "Working"))
            else:
                route.abort()
        self.page.route("**/*", fulfill)
        self.page.goto(ORIGIN + "/fixture")
        adapter = TimefolioBrowser("unused-local-tab-profile")
        adapter.page, adapter._account_key = self.page, "account"
        adapter._main, adapter._binding = Mock(), Mock()
        adapter._date = Mock(return_value="2026-10-12")
        adapter._grid = Mock(side_effect=lambda table, kind, **kwargs: {
            "kind": kind, **kwargs, **({"generation": table.get_attribute("data-generation"),
                                       "status": table.get_attribute("data-status")} if kind == "orders" else {})})
        with patch("backend.timefolio_account.parse_account_snapshot", side_effect=lambda payload, *args: payload), \
             patch("backend.timefolio_dom.parse_receipts", side_effect=lambda payload, *args: payload):
            for number, observe in enumerate((adapter.account_snapshot, adapter.receipt_snapshot,
                                               adapter.account_snapshot), 1):
                result = observe()
                orders = next(table for table in result["tables"] if table["kind"] == "orders")
                self.assertEqual(orders["generation"], str(number * 2))
                self.assertEqual(orders["status"], "Done" if number == 3 else "Working")
        self.assertEqual(requests, ["D", "A"] * 3)

    def test_lost_persistent_browser_is_detected_and_cleanup_is_idempotent(self):
        with tempfile.TemporaryDirectory() as profile:
            context = self.playwright.chromium.launch_persistent_context(profile, channel="chrome", headless=True)
            context.route("**/*", lambda route: route.abort())
            adapter = TimefolioBrowser(profile)
            adapter.context, adapter.page = context, context.pages[0]
            try:
                self.assertTrue(adapter.is_alive())
                context.browser.close()
                self.assertFalse(adapter.is_alive())
            finally:
                adapter.close()
            adapter.close()

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
