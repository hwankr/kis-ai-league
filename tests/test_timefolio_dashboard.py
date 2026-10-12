import json
from http.client import HTTPConnection
import threading
import unittest
from unittest.mock import Mock

from backend.dashboard import DashboardServer


class TimefolioDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.operations = Mock()
        cls.experiments = Mock()
        cls.server = DashboardServer(0, service=Mock(), experiment_service=cls.experiments,
                                     operations_service=cls.operations)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.operations.reset_mock()
        self.experiments.reset_mock()
        self.payload = {"status": "waiting", "stale": True, "error": None,
                        "updated_at": None, "last_attempt_at": None,
                        "refresh_interval_seconds": 60, "account": None,
                        "summary": None, "holdings": [], "orders": []}
        self.server.timefolio_snapshot = Mock(return_value=self.payload)
        self.headers = {"X-KIS-Dashboard": "1"}

    def request(self, method="GET", path="/api/timefolio", headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, headers=self.headers if headers is None else headers)
            response = connection.getresponse()
            return response.status, dict(response.headers), json.loads(response.read())
        finally:
            connection.close()

    def test_get_returns_cached_snapshot_without_order_commands(self):
        status, headers, payload = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(payload, self.payload)
        self.server.timefolio_snapshot.assert_called_once_with()
        self.operations.command.assert_not_called()
        self.experiments.command.assert_not_called()

    def test_requires_dashboard_header_local_host_and_origin(self):
        for headers in ({}, {**self.headers, "Host": "other.invalid"},
                        {**self.headers, "Origin": "https://other.invalid"},
                        {**self.headers, "Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], 403)
        self.server.timefolio_snapshot.assert_not_called()

    def test_parameters_and_post_cannot_start_collection_or_change_account(self):
        for query in ("refresh=1", "account=paper", "account=", "force=true"):
            with self.subTest(query=query):
                self.assertEqual(self.request(path=f"/api/timefolio?{query}")[0], 400)
        self.assertEqual(self.request(method="POST")[0], 404)
        self.server.timefolio_snapshot.assert_not_called()
        self.operations.command.assert_not_called()
        self.experiments.command.assert_not_called()

    def test_unexpected_read_error_does_not_expose_private_data(self):
        self.server.timefolio_snapshot.side_effect = RuntimeError("private-account-token")
        status, _, payload = self.request()
        self.assertEqual(status, 503)
        self.assertNotIn("private-account-token", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
