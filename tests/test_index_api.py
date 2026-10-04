from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.kis import KisError, PaperClient, Settings


class IndexApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.client = PaperClient(Settings("test-key", "test-secret"), Path(directory.name) / "token.json")
        self.rows = [{"stck_bsop_date": "20261002", "bstp_nmix_prpr": "7003.74",
                      "bstp_nmix_oprc": "6938.27", "bstp_nmix_hgpr": "7011.04",
                      "bstp_nmix_lwpr": "6927.88", "acml_vol": "239009",
                      "acml_tr_pbmn": "15981136", "mod_yn": "N"}]

    def test_default_kospi_uses_official_daily_api_and_preserves_raw_rows(self):
        with patch.object(self.client, "_get", return_value=({"output2": self.rows}, {})) as get:
            result = self.client.index_daily("2026-07-01", "2026-10-02")
        self.assertIs(result, self.rows)
        get.assert_called_once_with("/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
                                    "FHKUP03500100", {
                                        "FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": "0001",
                                        "FID_INPUT_DATE_1": "20260701", "FID_INPUT_DATE_2": "20261002",
                                        "FID_PERIOD_DIV_CODE": "D",
                                    })

    def test_kosdaq_uses_same_api_with_its_market_code(self):
        with patch.object(self.client, "_get", return_value=({"output2": self.rows}, {})) as get:
            self.client.index_daily("2026-10-02", "2026-10-02", "1001")
        self.assertEqual(get.call_args.args[2]["FID_INPUT_ISCD"], "1001")

    def test_invalid_symbols_fail_before_network(self):
        for symbol in (None, [], {}, 1, "2001", "0001&x=1", "０００１"):
            with self.subTest(symbol=symbol), patch.object(self.client, "_get") as get:
                with self.assertRaises(KisError):
                    self.client.index_daily("2026-07-01", "2026-10-02", symbol)
                get.assert_not_called()

    def test_invalid_dates_and_reversed_range_fail_before_network(self):
        for start, end in (("20260701", "2026-10-02"), ("2026-07-01", "20261002"),
                           ("2026-02-30", "2026-10-02"), ("2026-07-01", "2026-13-02"),
                           (None, "2026-10-02"), ("2026-07-01", []),
                           ("2026-10-03", "2026-10-02")):
            with self.subTest(start=start, end=end), patch.object(self.client, "_get") as get:
                with self.assertRaises(KisError):
                    self.client.index_daily(start, end)
                get.assert_not_called()

    def test_missing_or_malformed_output_fails_without_echoing_response(self):
        for data in ({}, {"output2": None}, {"output2": {}}, {"output2": "private-secret"},
                     {"output2": [None]}, {"output2": [self.rows[0], "private-secret"]}):
            with self.subTest(data=data), patch.object(self.client, "_get", return_value=(data, {})):
                with self.assertRaises(KisError) as caught:
                    self.client.index_daily("2026-07-01", "2026-10-02")
                self.assertNotIn("private-secret", str(caught.exception))

    def test_empty_rows_are_returned_for_service_to_handle(self):
        with patch.object(self.client, "_get", return_value=({"output2": []}, {})):
            self.assertEqual(self.client.index_daily("2026-07-01", "2026-10-02"), [])

    def test_request_uses_existing_guard_and_shared_spacing(self):
        events = []

        @contextmanager
        def guard():
            events.append("guard-enter")
            try:
                yield
            finally:
                events.append("guard-exit")

        @contextmanager
        def spacing(path):
            events.append("spacing-enter")
            try:
                yield
            finally:
                events.append("spacing-exit")

        def respond(*args, **kwargs):
            events.append("request")
            return {"rt_cd": "0", "output2": self.rows}, {}

        self.client.request_guard = guard
        with patch.object(self.client, "token", return_value="test-token"), \
                patch("backend.kis.request_spacing", side_effect=spacing), \
                patch.object(self.client, "_http_request", side_effect=respond):
            self.assertEqual(self.client.index_daily("2026-07-01", "2026-10-02"), self.rows)
        self.assertEqual(events, ["guard-enter", "spacing-enter", "request", "spacing-exit", "guard-exit"])

    def test_api_failure_propagates_without_returning_partial_rows(self):
        with patch.object(self.client, "_get", side_effect=KisError("KIS 연결 실패.")):
            with self.assertRaisesRegex(KisError, "KIS 연결 실패"):
                self.client.index_daily("2026-07-01", "2026-10-02")


if __name__ == "__main__":
    unittest.main()
