from datetime import date
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.kis import KisError, PaperClient, Settings, validate_execution_range


def execution(**changes):
    return {"ord_dt": "20261002", "ord_gno_brno": "00001", "odno": "0000000123",
            "pdno": "005930", "prdt_name": "삼성전자", "sll_buy_dvsn_cd": "02",
            "tot_ccld_qty": "2", "avg_prvs": "70000.5000", "tot_ccld_amt": "140001",
            "ord_tmd": "091530", "ord_qty": "5", "rmn_qty": "3", **changes}


def response(rows, continuation="D", cursor=("", "")):
    return ({"output1": rows, "output2": {}, "ctx_area_fk100": cursor[0],
             "ctx_area_nk100": cursor[1]}, {"tr_cont": continuation})


class ExecutionApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = PaperClient(Settings("sample-key", "sample-secret", "12345678"),
                                  Path(self.directory.name) / "token.json")
        self.today = patch("backend.kis._execution_today", return_value=date(2026, 10, 3))
        self.today.start()
        self.addCleanup(self.today.stop)

    def query(self):
        return self.client.executions("2026-09-04", "2026-10-03")

    def test_normalizes_cumulative_partial_fill_and_drops_private_fields(self):
        raw = execution(cano="12345678", ctac_tlno="01012345678", inqr_ip_addr="1.2.3.4",
                        app_secret="sample-secret")
        with patch.object(self.client, "_get", return_value=response([raw])) as get:
            result = self.query()
        self.assertEqual(result, {"environment": "paper", "executions": [{
            "order_date": "2026-10-02", "branch_id": "00001", "order_id": "0000000123",
            "symbol": "005930", "name": "삼성전자", "side": "buy", "quantity": "2",
            "price": "70000.5", "amount": "140001", "order_time": "09:15:30",
        }]})
        path, tr_id, params, continuation = get.call_args.args
        self.assertEqual(path, "/uapi/domestic-stock/v1/trading/inquire-daily-ccld")
        self.assertEqual(tr_id, "VTTC0081R")
        self.assertEqual(params["CCLD_DVSN"], "00")
        self.assertEqual(params["SLL_BUY_DVSN_CD"], "00")
        self.assertEqual(params["EXCG_ID_DVSN_CD"], "ALL")
        self.assertEqual(params["INQR_STRT_DT"], "20260904")
        self.assertEqual(continuation, "")
        for private in ("12345678", "01012345678", "1.2.3.4", "sample-secret"):
            self.assertNotIn(private, json.dumps(result))

    def test_zero_fill_excluded_but_partial_fill_on_cancelled_order_retained(self):
        rows = [execution(tot_ccld_qty="0", avg_prvs="0", tot_ccld_amt="0"),
                execution(odno="0000000124", cncl_yn="Y", sll_buy_dvsn_cd="01", ord_tmd="")]
        with patch.object(self.client, "_get", return_value=response(rows)):
            result = self.query()["executions"]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["side"], "sell")
        self.assertIsNone(result[0]["order_time"])

    def test_pages_include_all_rows_and_replace_cumulative_duplicates_without_sum(self):
        pages = [response([execution()], "F", ("search ", "next ")),
                 response([execution(tot_ccld_qty="5", avg_prvs="70100", tot_ccld_amt="350500"),
                           execution(odno="0000000124", sll_buy_dvsn_cd="01")],
                          "M", ("search", "last")),
                 response([execution(odno="0000000125")], "E")]
        calls = []

        def get(path, tr_id, params, continuation):
            calls.append((tr_id, dict(params), continuation))
            return pages[len(calls) - 1]

        with patch.object(self.client, "_get", side_effect=get):
            rows = self.query()["executions"]
        self.assertEqual(len(rows), 3)
        original = next(row for row in rows if row["order_id"] == "0000000123")
        self.assertEqual((original["quantity"], original["price"], original["amount"]),
                         ("5", "70100", "350500"))
        self.assertEqual([call[2] for call in calls], ["", "N", "N"])
        self.assertEqual(calls[1][1]["CTX_AREA_FK100"], "search")
        self.assertEqual(calls[1][1]["CTX_AREA_NK100"], "next")
        self.assertEqual(calls[2][1]["CTX_AREA_NK100"], "last")

    def test_order_number_reuse_across_dates_and_branches_keeps_distinct_rows(self):
        rows = [execution(), execution(ord_dt="20261001"), execution(ord_gno_brno="00002")]
        with patch.object(self.client, "_get", return_value=response(rows)):
            self.assertEqual(len(self.query()["executions"]), 3)

    def test_conflicting_identity_and_regressing_cumulative_quantity_fail_whole_query(self):
        for changes in ({"pdno": "000660"}, {"sll_buy_dvsn_cd": "01"}, {"tot_ccld_qty": "1"}):
            with self.subTest(changes=changes), patch.object(self.client, "_get", return_value=
                    response([execution(), execution(**changes)])):
                with self.assertRaisesRegex(KisError, "중복 주문"):
                    self.query()

    def test_successful_empty_is_distinct_from_failed_or_missing_output(self):
        with patch.object(self.client, "_get", return_value=response([])):
            self.assertEqual(self.query()["executions"], [])
        for body in ({}, {"output1": None, "output2": {}}, {"output1": [], "output2": []}):
            with self.subTest(body=body), patch.object(self.client, "_get", return_value=(body, {})):
                with self.assertRaises(KisError):
                    self.query()
        with patch.object(self.client, "_get", side_effect=KisError("조회 실패")):
            with self.assertRaisesRegex(KisError, "조회 실패"):
                self.query()

    def test_second_page_failure_does_not_return_first_page(self):
        with patch.object(self.client, "_get", side_effect=[
                response([execution()], "M", ("cursor", "next")), KisError("두 번째 페이지 실패")]):
            with self.assertRaisesRegex(KisError, "두 번째 페이지"):
                self.query()

    def test_repeated_empty_malformed_cursor_and_unknown_continuation_fail(self):
        for page in (response([], "M", ("same", "same")), response([], "F"),
                     response([], "M", (None, "next")), response([], "unknown")):
            with self.subTest(page=page), patch.object(self.client, "_get", return_value=page):
                with self.assertRaisesRegex(KisError, "연속조회"):
                    self.query()

    def test_page_safety_limit_fails_instead_of_truncating(self):
        count = 0

        def get(*args):
            nonlocal count
            count += 1
            return response([], "M", ("cursor", str(count)))

        with patch.object(self.client, "_get", side_effect=get):
            with self.assertRaisesRegex(KisError, "한도"):
                self.query()
        self.assertEqual(count, 1000)

    def test_historical_query_splits_at_official_month_boundary_with_fresh_cursors(self):
        pages = [response([execution(ord_dt="20260630")], "F", ("old", "next")),
                 response([execution(ord_dt="20260629")]),
                 response([execution(ord_dt="20260701")])]
        calls = []

        def get(path, tr_id, params, continuation):
            calls.append((tr_id, dict(params), continuation))
            return pages[len(calls) - 1]

        with patch.object(self.client, "_get", side_effect=get):
            rows = self.client.executions("2026-06-01", "2026-07-31")["executions"]
        self.assertEqual(len(rows), 3)
        self.assertEqual([call[0] for call in calls], ["VTSC9215R", "VTSC9215R", "VTTC0081R"])
        self.assertEqual(calls[0][1]["INQR_END_DT"], "20260630")
        self.assertEqual(calls[2][1]["INQR_STRT_DT"], "20260701")
        self.assertEqual(calls[2][1]["INQR_END_DT"], "20260731")
        self.assertEqual(calls[2][1]["CTX_AREA_NK100"], "")
        self.assertEqual(calls[2][2], "")

    def test_historical_only_and_year_rollover_use_correct_tr(self):
        with patch("backend.kis._execution_today", return_value=date(2026, 1, 31)), \
                patch.object(self.client, "_get", return_value=response([])) as get:
            self.client.executions("2025-09-01", "2025-09-30")
            self.assertEqual(get.call_args.args[1], "VTSC9215R")
            self.client.executions("2025-10-01", "2025-10-31")
            self.assertEqual(get.call_args.args[1], "VTTC0081R")

    def test_range_validation_and_missing_account_prevent_network(self):
        invalid = [("20261001", "2026-10-03"), ("2026-02-30", "2026-03-01"),
                   (None, "2026-10-03"), ("2026-10-02", "2026-10-01"),
                   ("2026-07-05", "2026-10-03"), ("2026-10-03", "2026-10-04")]
        with patch.object(self.client, "_get") as get:
            for start, end in invalid:
                with self.subTest(start=start, end=end), self.assertRaises(KisError):
                    self.client.executions(start, end)
            self.client.settings.account = ""
            with self.assertRaises(KisError):
                self.query()
            get.assert_not_called()
        self.assertEqual(validate_execution_range("2026-07-06", "2026-10-03"),
                         (date(2026, 7, 6), date(2026, 10, 3)))

    def test_invalid_populated_rows_fail_without_exposing_raw_values(self):
        invalid = [None, execution(ord_dt="20260230"), execution(ord_dt="20260801"),
                   execution(ord_gno_brno=""), execution(odno="private-secret"),
                   execution(pdno="<script>"), execution(sll_buy_dvsn_cd=[]),
                   execution(sll_buy_dvsn_cd="99"), execution(prdt_name="private-secret\n"),
                   execution(tot_ccld_qty="NaN"), execution(tot_ccld_qty="-1"),
                   execution(tot_ccld_qty=None), execution(avg_prvs="Infinity"),
                   execution(avg_prvs="0"), execution(tot_ccld_amt="0"),
                   execution(tot_ccld_amt="1e6"), execution(ord_tmd="250000")]
        for row in invalid:
            with self.subTest(row=row), patch.object(self.client, "_get", return_value=
                    response([execution(), row])):
                with self.assertRaises(KisError) as caught:
                    self.query()
                self.assertNotIn("private-secret", str(caught.exception))

    def test_request_guard_surrounds_each_network_request_and_releases_on_error(self):
        events = []

        @contextmanager
        def guard():
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        def request(*args, **kwargs):
            events.append("network")
            self.assertEqual(events[-2], "enter")
            raise KisError("조회 실패")

        self.client.request_guard = guard
        with patch.object(self.client, "_perform_request", side_effect=request):
            with self.assertRaises(KisError):
                self.client._request("/oauth2/tokenP", body={})
            with self.assertRaises(KisError):
                self.client._request("/uapi/domestic-stock/v1/trading/inquire-daily-ccld")
        self.assertEqual(events, ["enter", "network", "exit"] * 2)


if __name__ == "__main__":
    unittest.main()
