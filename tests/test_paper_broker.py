from copy import deepcopy
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch
import unittest

from backend.kis import AccountProfile, KisError, PaperClient, Settings
from backend.paper_broker import BrokerRejected, BrokerUnknown, PAPER_URL, PaperBroker


NOW = datetime(2026, 10, 5, 1, 0, 20, tzinfo=timezone.utc)


def order(**changes):
    return {"ord_dt": "20261005", "ord_gno_brno": "01234", "odno": "0000000123",
            "orgn_odno": "0000000000", "pdno": "005930", "sll_buy_dvsn_cd": "02",
            "ord_qty": "5", "tot_ccld_qty": "0", "rmn_qty": "5", "cncl_cfrm_qty": "0",
            "rjct_qty": "0", "ord_unpr": "70000", "avg_prvs": "0", "tot_ccld_amt": "0",
            "ord_tmd": "095900", "cncl_yn": "N", **changes}


def page(rows, *, summary=None, continuation="", cursor=("", "")):
    return ({"rt_cd": "0", "output1": rows, "output2": {} if summary is None else summary,
             "ctx_area_fk100": cursor[0], "ctx_area_nk100": cursor[1]}, {"tr_cont": continuation})


def ack(**changes):
    return {"rt_cd": "0", "output": {"ODNO": "0000000123", "KRX_FWDG_ORD_ORGNO": "01234",
                                       "ORD_TMD": "100020", **changes}}, {}


def balance_row(**changes):
    return {"pdno": "005930", "hldg_qty": "5", "ord_psbl_qty": "3", "pchs_avg_pric": "70000.00",
            "prpr": "71000", **changes}


def minute(**changes):
    return {"stck_bsop_date": "20261005", "stck_cntg_hour": "100000", "cntg_vol": "20",
            "stck_prpr": "70100", "stck_oprc": "70000", "stck_hgpr": "70200", "stck_lwpr": "69900", **changes}


class PaperBrokerTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings("test-key", "test-secret", "12345678", "01")
        self.profile = AccountProfile("paper", "모의", self.settings)
        self.client = Mock(spec=PaperClient)
        self.client.settings = self.settings
        self.client.token.return_value = "test-token"
        self.client._request.return_value = ack()
        self.client._get.return_value = page([])
        self.broker = PaperBroker(self.profile, client=self.client, now=lambda: NOW)
        today_patch = patch("backend.kis._execution_today", return_value=date(2026, 10, 5))
        today_patch.start()
        self.addCleanup(today_patch.stop)

    def test_construct_and_read_do_not_send_orders(self):
        self.broker.orders("2026-10-05", "2026-10-05")
        self.client._request.assert_not_called()

    def test_paper_url_is_fixed_and_profile_mismatch_fails(self):
        with patch("backend.kis.PAPER_URL", "https://live.invalid"):
            with self.assertRaises(KisError):
                self.broker.submit("005930", "buy", 1, "70000")
        self.client.settings = Settings("other", "secret", "87654321", "01")
        with self.assertRaises(KisError):
            self.broker.orders("2026-10-05", "2026-10-05")
        self.client._request.assert_not_called()
        self.assertEqual(PAPER_URL, "https://openapivts.koreainvestment.com:29443")

    def test_cash_limit_submission_uses_paper_ids_and_exact_string_fields(self):
        for side, tr_id in (("buy", "VTTC0012U"), ("sell", "VTTC0011U")):
            result = self.broker.submit("005930", side, 2, "070000")
            call = self.client._request.call_args
            self.assertEqual(call.args, ("/uapi/domestic-stock/v1/trading/order-cash",))
            self.assertEqual(call.kwargs["headers"]["tr_id"], tr_id)
            self.assertEqual(call.kwargs["body"], {
                "CANO": "12345678", "ACNT_PRDT_CD": "01", "PDNO": "005930", "ORD_DVSN": "00",
                "ORD_QTY": "2", "ORD_UNPR": "70000", "EXCG_ID_DVSN_CD": "KRX",
                "SLL_TYPE": "01" if side == "sell" else "", "CNDT_PRIC": ""})
            self.assertEqual(result, {"order_id": "0000000123", "branch_id": "01234", "order_time": "10:00:20"})

    def test_invalid_order_parameters_fail_before_auth(self):
        cases = [("005930", "buy", qty, "70000") for qty in (True, 0, -1, 1.2, "1", 1_000_000_000)]
        cases += [("005930", "buy", 1, price) for price in (None, 0, -1, "0", "1.5", "1e5", True, "NaN")]
        cases += [(symbol, "buy", 1, "70000") for symbol in (None, "0059300", "00593a", "１２３４５６")]
        cases += [("005930", None, 1, "70000")]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.broker.submit(*args)
        self.client.token.assert_not_called()
        self.client._request.assert_not_called()

    def test_preflight_auth_failure_is_definite_no_submission(self):
        self.client.token.side_effect = KisError("test-secret")
        with self.assertRaises(BrokerRejected) as caught:
            self.broker.submit("005930", "buy", 1, 70000)
        self.assertNotIn("test-secret", str(caught.exception))
        self.client._request.assert_not_called()

    def test_business_rejection_exposes_only_safe_code(self):
        self.client._request.side_effect = KisError("KIS 업무 응답 오류 (APBK0918)")
        # Only the exact currently sanitized PaperClient format is considered definite.
        with self.assertRaises(BrokerUnknown):
            self.broker.submit("005930", "buy", 1, 70000)
        self.client._request.side_effect = KisError("KIS 업무 응답 오류 (EGW00123)")
        with self.assertRaises(BrokerRejected) as caught:
            self.broker.submit("005930", "buy", 1, 70000)
        self.assertEqual(caught.exception.code, "EGW00123")

    def test_raw_business_rejection_is_not_retried(self):
        self.client._request.return_value = ({"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "test-secret"}, {})
        with self.assertRaises(BrokerRejected) as caught:
            self.broker.submit("005930", "buy", 1, 70000)
        self.assertEqual(caught.exception.code, "EGW00123")
        self.assertNotIn("test-secret", str(caught.exception))
        self.client._request.assert_called_once()

    def test_transport_or_http_errors_are_unknown_and_never_retried(self):
        for error in (KisError("KIS 요청 실패 (HTTP 500)."), TimeoutError("test-secret"),
                      OSError("test-secret"), KisError("KIS 응답을 읽을 수 없습니다.")):
            self.client._request.reset_mock()
            self.client._request.side_effect = error
            with self.subTest(error=type(error)), self.assertRaises(BrokerUnknown) as caught:
                self.broker.submit("005930", "buy", 1, 70000)
            self.assertNotIn("test-secret", str(caught.exception))
            self.client._request.assert_called_once()

    def test_success_with_missing_or_malformed_ack_is_unknown(self):
        results = [({}, {}), ({"rt_cd": "0"}, {}), ack(ODNO="0"), ack(ORD_TMD="250000"),
                   ack(KRX_FWDG_ORD_ORGNO="secret"), ([], {})]
        for result in results:
            self.client._request.reset_mock()
            self.client._request.return_value = result
            with self.subTest(result=result), self.assertRaises(BrokerUnknown):
                self.broker.submit("005930", "buy", 1, 70000)
            self.client._request.assert_called_once()

    def test_all_order_states_and_cumulative_fills_preserved(self):
        rows = [order(), order(odno="124", tot_ccld_qty="2", rmn_qty="3", avg_prvs="69900", tot_ccld_amt="139800"),
                order(odno="125", tot_ccld_qty="5", rmn_qty="0", avg_prvs="69900", tot_ccld_amt="349500"),
                order(odno="126", cncl_yn="Y", cncl_cfrm_qty="5", rmn_qty="0"),
                order(odno="127", rjct_qty="5", rmn_qty="0"),
                order(odno="128", cncl_yn="Y", cncl_cfrm_qty="3", rmn_qty="0", tot_ccld_qty="2",
                      avg_prvs="69900", tot_ccld_amt="139800")]
        self.client._get.return_value = page(rows)
        result = self.broker.orders("2026-10-05", "2026-10-05")
        self.assertEqual([row["status"] for row in result], ["open", "partial", "filled", "cancelled", "rejected", "cancelled"])
        self.assertEqual(result[-1]["filled_quantity"], 2)
        self.assertEqual(result[-1]["cancelled_quantity"], 3)
        self.assertIsNone(result[0]["original_order_id"])
        call = self.client._get.call_args
        self.assertEqual(call.args[1], "VTTC0081R")
        self.assertEqual(call.args[2]["CCLD_DVSN"], "00")

    def test_all_pages_read_without_adding_duplicate_cumulative_fills(self):
        self.client._get.side_effect = [page([order()], continuation="F", cursor=("one", "")),
                                       page([order(tot_ccld_qty="2", rmn_qty="3", avg_prvs="70000", tot_ccld_amt="140000")])]
        result = self.broker.orders("2026-10-05", "2026-10-05")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["filled_quantity"], 2)
        self.assertEqual(self.client._get.call_args_list[1].args[3], "N")

    def test_unknown_cursor_repeated_cursor_and_page_limit_return_no_partial_orders(self):
        cases = [[page([order()], continuation="Q")],
                 [page([order()], continuation="M", cursor=("", ""))],
                 [page([order()], continuation="M", cursor=("a", "b"))] * 2]
        for pages in cases:
            self.client._get.side_effect = pages
            with self.subTest(pages=len(pages)), self.assertRaises(KisError):
                self.broker.orders("2026-10-05", "2026-10-05")
        self.client._get.side_effect = [page([order()], continuation="M", cursor=("a", "b"))]
        with patch("backend.paper_broker.MAX_PAGES", 1), self.assertRaises(KisError):
            self.broker.orders("2026-10-05", "2026-10-05")

    def test_second_page_error_returns_no_partial_orders(self):
        self.client._get.side_effect = [page([order()], continuation="M", cursor=("a", "b")), KisError("unavailable")]
        with self.assertRaises(KisError):
            self.broker.orders("2026-10-05", "2026-10-05")

    def test_conflicting_duplicate_orders_fail_closed(self):
        for changes in ({"pdno": "000660"}, {"ord_qty": "6"}, {"ord_unpr": "70001"},
                        {"ord_tmd": "100000"}, {"sll_buy_dvsn_cd": "01"}):
            self.client._get.return_value = page([order(), order(**changes)])
            with self.subTest(changes=changes), self.assertRaises(KisError):
                self.broker.orders("2026-10-05", "2026-10-05")

    def test_malformed_order_values_fail_closed_including_zero_filled_rows(self):
        for changes in ({"ord_dt": "20261006"}, {"rmn_qty": "-1"}, {"cncl_yn": ""}, {"rjct_qty": None},
                        {"tot_ccld_qty": "5"}, {"ord_tmd": ""}, {"avg_prvs": "1"}, {"rmn_qty": "0"}):
            self.client._get.return_value = page([order(**changes)])
            with self.subTest(changes=changes), self.assertRaises(KisError):
                self.broker.orders("2026-10-05", "2026-10-05")

    def test_missing_cancellation_quantity_never_defaults_or_uses_misspelled_field(self):
        for filled in (False, True):
            for legacy_value in (None, "0"):
                raw = order()
                del raw["cncl_cfrm_qty"]
                if legacy_value is not None:
                    raw["cnc_cfrm_qty"] = legacy_value
                if filled:
                    raw.update(tot_ccld_qty="5", rmn_qty="0", avg_prvs="69900", tot_ccld_amt="349500")
                self.client._get.return_value = page([raw])
                with self.subTest(filled=filled, legacy_value=legacy_value), self.assertRaises(KisError):
                    self.broker.orders("2026-10-05", "2026-10-05")
        self.client._request.assert_not_called()

    def test_invalid_cancellation_quantity_never_falls_back_to_misspelled_field(self):
        for value in (None, "", "-1", "1.5", "NaN", "1e0", "inf", 0, False, []):
            self.client._get.return_value = page([order(cncl_cfrm_qty=value, cnc_cfrm_qty="0")])
            with self.subTest(value=value), self.assertRaises(KisError):
                self.broker.orders("2026-10-05", "2026-10-05")
        self.client._request.assert_not_called()

    def test_cancellation_quantity_must_reconcile_with_fills_and_order_quantity(self):
        for cancelled in ("2", "4"):
            self.client._get.return_value = page([order(
                cncl_yn="Y", cncl_cfrm_qty=cancelled, rmn_qty="0", tot_ccld_qty="2",
                avg_prvs="69900", tot_ccld_amt="139800")])
            with self.subTest(cancelled=cancelled), self.assertRaises(KisError):
                self.broker.orders("2026-10-05", "2026-10-05")
        self.client._request.assert_not_called()

    def test_snapshot_preserves_cash_holdings_and_actual_sellable_quantity(self):
        self.client._get.side_effect = [page([balance_row()], summary=[{"dnca_tot_amt": "1000000", "tot_evlu_amt": "2000000"}],
                                           continuation="M", cursor=("a", "b")),
                                       page([balance_row(pdno="000660", hldg_qty="1", ord_psbl_qty="0")],
                                            summary=[{"dnca_tot_amt": "1000000", "tot_evlu_amt": "2000100"}])]
        result = self.broker.snapshot()
        self.assertEqual(result["cash"], "1000000")
        self.assertEqual(result["total_value"], "2000100")
        self.assertEqual(len(result["holdings"]), 2)
        self.assertEqual(result["holdings"]["005930"]["sellable_quantity"], 3)
        self.assertEqual(result["as_of"], NOW.isoformat())

    def test_snapshot_unknown_continuation_bad_quantities_and_duplicates_fail_closed(self):
        summary = [{"dnca_tot_amt": "1000000", "tot_evlu_amt": "2000000"}]
        for response in (page([balance_row()], summary=summary, continuation="Z"),
                         page([balance_row(ord_psbl_qty="6")], summary=summary),
                         page([balance_row(), balance_row()], summary=summary),
                         page([balance_row()], summary=[{"dnca_tot_amt": "NaN", "tot_evlu_amt": "2000000"}])):
            self.client._get.return_value = response
            with self.subTest(response=response), self.assertRaises(KisError):
                self.broker.snapshot()

    def test_buyability_uses_unlevered_cash_and_caps_at_limit_price(self):
        self.client._get.return_value = ({"rt_cd": "0", "output": {"nrcvb_buy_amt": "140001", "nrcvb_buy_qty": "4",
                                            "max_buy_amt": "9000000", "max_buy_qty": "100"}}, {})
        result = self.broker.buyability("005930", "70000")
        self.assertEqual(result["quantity"], 2)
        self.assertEqual(result["cash"], "140001")
        call = self.client._get.call_args
        self.assertEqual(call.args[1], "VTTC8908R")
        self.assertEqual(call.args[2]["ORD_DVSN"], "01")
        self.assertEqual(call.args[2]["CMA_EVLU_AMT_ICLD_YN"], "N")
        self.assertEqual(call.args[2]["OVRS_ICLD_YN"], "N")

    def test_buyability_does_not_fallback_to_margin_or_missing_values(self):
        self.client._get.return_value = ({"rt_cd": "0", "output": {"max_buy_amt": "9000000", "max_buy_qty": "100"}}, {})
        with self.assertRaises(KisError):
            self.broker.buyability("005930", "70000")

    def test_quote_eligibility_requires_every_status_flag(self):
        normal = {"symbol": "005930", "status": "ok", "current_price": "70000", "unknown_fields": [],
                  "temp_halted": False, "managed": False, "liquidation": False, "investment_caution": False,
                  "short_overheated": False, "warning_code": "00"}
        self.client.stock_status.return_value = normal
        result = self.broker.quote("005930")
        self.assertTrue(result["eligible"])
        for changes in ({"managed": True}, {"warning_code": "02"}, {"temp_halted": None},
                        {"current_price": None}, {"symbol": "000660"}, {"unknown_fields": ["x"]}):
            self.client.stock_status.return_value = {**normal, **changes}
            with self.subTest(changes=changes):
                self.assertFalse(self.broker.quote("005930")["eligible"])

    def test_cancel_rechecks_remaining_and_sends_single_cancel_all(self):
        self.client._get.return_value = page([order(tot_ccld_qty="2", rmn_qty="3", avg_prvs="70000", tot_ccld_amt="140000")])
        self.broker.cancel("0000000123", "01234", "005930", 5)
        body = self.client._request.call_args.kwargs["body"]
        self.assertEqual(body["ORGN_ODNO"], "0000000123")
        self.assertEqual(body["QTY_ALL_ORD_YN"], "Y")
        self.assertEqual(body["ORD_QTY"], "0")
        self.assertEqual(body["RVSE_CNCL_DVSN_CD"], "02")
        self.assertEqual(self.client._request.call_args.kwargs["headers"]["tr_id"], "VTTC0013U")
        self.client._request.assert_called_once()

    def test_cancel_never_targets_missing_wrong_symbol_or_increased_remaining(self):
        for rows in ([], [order(pdno="000660")], [order(ord_qty="6", rmn_qty="6")],
                     [order(tot_ccld_qty="5", rmn_qty="0", avg_prvs="70000", tot_ccld_amt="350000")]):
            self.client._get.return_value = page(rows)
            with self.subTest(rows=rows), self.assertRaises(BrokerRejected):
                self.broker.cancel("0000000123", "01234", "005930", 5)
        self.client._request.assert_not_called()

    def test_cancel_unknown_outcome_is_not_retried(self):
        self.client._get.return_value = page([order()])
        self.client._request.side_effect = TimeoutError()
        with self.assertRaises(BrokerUnknown):
            self.broker.cancel("0000000123", "01234", "005930", 5)
        self.client._request.assert_called_once()

    def test_market_session_uses_latest_positive_volume_bar_not_retrieval_time(self):
        self.client.chart_minutes.return_value = {"rt_cd": "0", "output1": {"stck_shrn_iscd": "005930"},
             "output2": [minute(stck_cntg_hour="095900"), minute(cntg_vol="0")]}
        result = self.broker.market_session("005930")
        self.assertEqual(result, {"session_date": "2026-10-05", "last_trade_at": "2026-10-05T09:59:00+09:00",
                                  "price": "70100", "volume": 20})
        self.client.chart_minutes.assert_called_once_with("005930", "100020")

    def test_market_session_exposes_previous_date_for_root_freshness_check(self):
        self.client.chart_minutes.return_value = {"rt_cd": "0", "output1": {},
             "output2": [minute(stck_bsop_date="20261002", stck_cntg_hour="153000")]}
        self.assertEqual(self.broker.market_session("005930")["session_date"], "2026-10-02")

    def test_market_session_rejects_future_bars_bad_ohlc_and_no_trades(self):
        for rows in ([], [minute(cntg_vol="0")], [minute(stck_cntg_hour="100100")],
                     [minute(stck_hgpr="50000")], [minute(stck_bsop_date="20261006")],
                     [minute(), minute(cntg_vol="21")]):
            self.client.chart_minutes.return_value = {"rt_cd": "0", "output1": {}, "output2": rows}
            with self.subTest(rows=rows), self.assertRaises(KisError):
                self.broker.market_session("005930")

    def test_session_days_short_chunks_prevent_silent_100_row_truncation(self):
        def get(start, end, symbol):
            self.assertEqual(symbol, "0001")
            self.assertLessEqual((date.fromisoformat(end) - date.fromisoformat(start)).days, 29)
            return [{"stck_bsop_date": start.replace("-", ""), "bstp_nmix_prpr": "7000", "acml_vol": "10"}]
        self.client.index_daily.side_effect = get
        result = self.broker.session_days("2026-08-01", "2026-10-02")
        self.assertEqual(result, ["2026-08-01", "2026-08-31", "2026-09-30"])
        self.assertEqual(self.client.index_daily.call_count, 3)

    def test_session_days_missing_invalid_duplicate_and_zero_volume_fail_closed(self):
        valid = {"stck_bsop_date": "20261002", "bstp_nmix_prpr": "7000", "acml_vol": "10"}
        for rows in ([], [valid, valid], [{**valid, "acml_vol": "0"}], [{**valid, "stck_bsop_date": "20261005"}]):
            self.client.index_daily.return_value = rows
            with self.subTest(rows=rows), self.assertRaises(KisError):
                self.broker.session_days("2026-10-01", "2026-10-02")


if __name__ == "__main__":
    unittest.main()
