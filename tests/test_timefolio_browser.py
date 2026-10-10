"""Pure fake DOM/download tests. Never opens Playwright or contacts a website."""
from copy import deepcopy
from datetime import datetime
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
        self.download = SimpleNamespace(path=lambda: "offline-fake.csv", delete=Mock())

    def get_by_role(self, role, name=None, **kwargs):
        label = getattr(name, "pattern", name) or ""
        return FakeLocator(self, "role:" + role + ":" + label)

    def locator(self, selector, **kwargs):
        return FakeLocator(self, selector)

    def evaluate(self, expression):
        return None

    def expect_response(self, predicate, **kwargs):
        urls = [ORIGIN + "/Portfolio/ValidateProduct?d=" + self.fields["date"]
                + "&prodId=A" + self.search + "&entry=" + self.fields["side"]]
        urls += [ORIGIN + "/Portfolio/" + name for name in ("Session", "Orders", "Universe")]
        candidates = [SimpleNamespace(url=url, request=SimpleNamespace(method="GET"),
                      ok=self.validation_ok if "ValidateProduct" in url else True, finished=lambda: None) for url in urls]
        response = next((candidate for candidate in candidates if predicate(candidate)), None)
        if response is None:
            raise TimeoutError("fake validation did not match requested product/date/side")
        self.responses.append(response.url)
        event = SimpleNamespace(value=response)

        class Context:
            def __enter__(self):
                return event

            def __exit__(self, *args):
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


if __name__ == "__main__":
    unittest.main()
