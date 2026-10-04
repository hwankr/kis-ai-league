from http.client import HTTPConnection
import json
import threading
import unittest
from unittest.mock import Mock

from backend.dashboard import DashboardServer


class ChartHttpTests(unittest.TestCase):
    def setUp(self):
        self.accounts = Mock()
        self.charts = Mock()
        self.charts.snapshot.return_value = {
            "status": "ok", "symbol": "005930", "interval": "day", "bars": [],
            "as_of": None, "error": None, "stale": False,
        }
        self.server = DashboardServer(0, self.accounts, chart_service=self.charts)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("GET", path, headers=headers if headers is not None else {"X-KIS-Dashboard": "1"})
            response = connection.getresponse()
            return response.status, dict(response.headers), json.loads(response.read())
        finally:
            connection.close()

    def test_chart_uses_source_service_without_reading_account_balances(self):
        status, headers, payload = self.get("/api/chart?symbol=005930")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(payload["symbol"], "005930")
        self.charts.snapshot.assert_called_once_with("005930", "day")
        self.accounts.snapshot.assert_not_called()
        self.accounts.list_accounts.assert_not_called()
        self.get("/api/chart?symbol=000660&interval=5m")
        self.charts.snapshot.assert_called_with("000660", "5m")

    def test_explicit_refresh_bypasses_chart_freshness(self):
        status, _, _ = self.get("/api/chart?symbol=005930&interval=15m&refresh=1")
        self.assertEqual(status, 200)
        self.charts.snapshot.assert_called_once_with("005930", "15m", force=True)

    def test_alphanumeric_symbol_is_forwarded_without_modification(self):
        self.charts.snapshot.return_value["symbol"] = "0126Z0"
        status, _, result = self.get("/api/chart?symbol=0126Z0")
        self.assertEqual(status, 200)
        self.assertEqual(result["symbol"], "0126Z0")
        self.charts.snapshot.assert_called_once_with("0126Z0", "day")

    def test_invalid_parameters_never_reach_chart_service(self):
        for query in ("", "symbol=", "symbol=12345", "symbol=005930&symbol=000660",
                      "symbol=005930&interval=1m", "symbol=005930&interval=",
                      "symbol=005930&interval=day&interval=5m",
                      "symbol=005930&account=paper", "symbol=005930&date=20261002",
                      "symbol=005930&refresh=", "symbol=005930&refresh=0",
                      "symbol=005930&refresh=true", "symbol=005930&refresh=1&refresh=1",
                      "symbol=0126z0", "symbol=0126_Z", "symbol=%200126Z0", "symbol=0126Z00"):
            with self.subTest(query=query):
                self.assertEqual(self.get("/api/chart?" + query)[0], 400)
        self.charts.snapshot.assert_not_called()

    def test_chart_enforces_same_origin_and_custom_header(self):
        for headers in ({}, {"X-KIS-Dashboard": "1", "Origin": "https://other.invalid"},
                        {"X-KIS-Dashboard": "1", "Sec-Fetch-Site": "cross-site"},
                        {"X-KIS-Dashboard": "1", "Host": "other.invalid"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.get("/api/chart?symbol=005930", headers)[0], 403)
        self.charts.snapshot.assert_not_called()

    def test_stale_bars_survive_service_failure_and_unexpected_errors_are_safe(self):
        self.charts.snapshot.return_value = {
            "status": "error", "symbol": "005930", "interval": "day",
            "bars": [{"time": "2026-10-02", "close": "276000"}],
            "error": "KIS 연결 실패", "stale": True,
        }
        status, _, payload = self.get("/api/chart?symbol=005930")
        self.assertEqual(status, 503)
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["bars"][0]["close"], "276000")
        self.charts.snapshot.side_effect = RuntimeError("private-token-and-account")
        status, _, payload = self.get("/api/chart?symbol=005930")
        self.assertEqual(status, 503)
        self.assertNotIn("private", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
