from http.client import HTTPConnection
import json
import threading
import unittest
from unittest.mock import Mock

from backend.dashboard import DashboardServer


class CandidatesHttpTests(unittest.TestCase):
    def setUp(self):
        self.accounts, self.market, self.charts, self.candidates = (Mock() for _ in range(4))
        self.candidates.snapshot.return_value = {"status": "idle", "rows": [], "error": None}
        self.candidates.start.return_value = {"status": "running", "rows": [], "error": None}
        self.server = DashboardServer(0, self.accounts, market_service=self.market,
                                      chart_service=self.charts, candidate_service=self.candidates)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method="GET", path="/api/candidates", body=None, headers=None):
        if headers is None:
            headers = {"X-KIS-Dashboard": "1", "Content-Type": "application/json"}
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.headers), json.loads(response.read())
        finally:
            connection.close()

    def test_get_only_reads_snapshot_and_never_starts_collection_or_account_requests(self):
        status, headers, result = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(result["status"], "idle")
        self.candidates.snapshot.assert_called_once_with()
        self.candidates.start.assert_not_called()
        self.assertEqual(self.accounts.mock_calls, [])
        self.assertEqual(self.market.mock_calls, [])
        self.assertEqual(self.charts.mock_calls, [])

    def test_post_empty_object_starts_async_collection(self):
        status, _, result = self.request("POST", body="{}")
        self.assertEqual(status, 202)
        self.assertEqual(result["status"], "running")
        self.candidates.start.assert_called_once_with()
        self.candidates.snapshot.assert_not_called()

    def test_post_returns_blocked_status_without_claiming_async_work(self):
        self.candidates.start.return_value = {"status": "error", "rows": [], "error": "목록 확인 필요"}
        status, _, result = self.request("POST", body="{}")
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "error")

    def test_get_and_post_enforce_origin_host_and_dashboard_header(self):
        for method in ("GET", "POST"):
            for changes in ({"X-KIS-Dashboard": "0"}, {"Origin": "https://other.invalid"},
                            {"Sec-Fetch-Site": "cross-site"}, {"Host": "other.invalid"}):
                headers = {"X-KIS-Dashboard": "1", "Content-Type": "application/json", **changes}
                with self.subTest(method=method, changes=changes):
                    self.assertEqual(self.request(method, body="{}" if method == "POST" else None,
                                                  headers=headers)[0], 403)
            with self.subTest(method=method, missing_header=True):
                self.assertEqual(self.request(method, headers={})[0], 403)
        self.candidates.snapshot.assert_not_called()
        self.candidates.start.assert_not_called()

    def test_same_origin_post_and_get_are_accepted(self):
        headers = {"X-KIS-Dashboard": "1", "Content-Type": "application/json",
                   "Origin": f"http://127.0.0.1:{self.server.server_port}", "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(self.request(headers=headers)[0], 200)
        self.assertEqual(self.request("POST", body="{}", headers=headers)[0], 202)

    def test_query_parameters_and_other_post_routes_cannot_trigger_collection(self):
        for query in ("refresh=1", "account=paper", "symbol=005930", "x=", "x"):
            with self.subTest(query=query):
                self.assertEqual(self.request(path="/api/candidates?" + query)[0], 400)
                self.assertEqual(self.request("POST", "/api/candidates?" + query, "{}")[0], 404)
        self.assertEqual(self.request("POST", "/api/account", "{}")[0], 404)
        self.candidates.snapshot.assert_not_called()
        self.candidates.start.assert_not_called()

    def test_post_body_size_type_content_and_transfer_encoding_are_restricted(self):
        cases = [("", {}), ("[]", {}), ("null", {}), ("true", {}), ('{"refresh":true}', {}),
                 ("{broken", {}), (" " * 65, {}), ("{}", {"Content-Type": "text/plain"}),
                 ("{}", {"Transfer-Encoding": "chunked"}),
                 ("{}", {"Content-Length": "-1"}), ("{}", {"Content-Length": "invalid"})]
        for body, changes in cases:
            headers = {"X-KIS-Dashboard": "1", "Content-Type": "application/json", **changes}
            with self.subTest(body=body, changes=changes):
                self.assertEqual(self.request("POST", body=body, headers=headers)[0], 400)
        self.candidates.start.assert_not_called()

    def test_unexpected_service_errors_are_sanitized_for_both_methods(self):
        self.candidates.snapshot.side_effect = RuntimeError("private-account-token")
        self.candidates.start.side_effect = RuntimeError("private-account-token")
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                status, _, result = self.request(method, body="{}" if method == "POST" else None)
                self.assertEqual(status, 503)
                self.assertNotIn("private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
