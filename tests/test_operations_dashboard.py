import json
from http.client import HTTPConnection
import threading
import unittest
from unittest.mock import Mock

from backend.dashboard import DashboardServer


class OperationsDashboardTests(unittest.TestCase):
    def test_background_tick_runs_without_http_and_close_stops_monitor(self):
        operations = Mock()
        server = DashboardServer(0, service=Mock(), operations_service=operations)
        try:
            server.service_actions()
            operations.tick.assert_not_called()
            server.automatic_research = True
            server.research_service = server.experiment_service = None
            server.service_actions()
            operations.tick.assert_called_once()
            server.shutdown_requested = True
            server.service_actions()
            operations.tick.assert_called_once()
        finally:
            server.server_close()
        operations.close.assert_called_once()

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
        self.payload = {"mirror": {"status": "stopped", "updated_at": None, "reason": None,
                                    "can_start": True, "can_stop": False}, "busy": False, "error": None}
        self.operations.snapshot.side_effect = self.operations.command.side_effect = None
        self.operations.snapshot.return_value = self.operations.command.return_value = self.payload
        self.headers = {"X-KIS-Dashboard": "1", "Content-Type": "application/json"}

    def request(self, method="GET", path="/api/operations", body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=self.headers if headers is None else headers)
            response = connection.getresponse()
            return response.status, dict(response.headers), json.loads(response.read())
        finally:
            connection.close()

    def test_get_is_read_only_no_store_and_exposes_only_public_health(self):
        status, headers, payload = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(payload["mirror"], self.payload["mirror"])
        self.assertEqual(set(payload["dashboard"]), {"status", "updated_at"})
        self.assertEqual(payload["dashboard"]["status"], "ok")
        self.operations.command.assert_not_called()
        self.experiments.command.assert_not_called()

    def test_get_and_post_require_local_host_origin_and_dashboard_header(self):
        invalid = [{}, {**self.headers, "Host": "other.invalid"},
                   {**self.headers, "Origin": "https://other.invalid"},
                   {**self.headers, "Sec-Fetch-Site": "cross-site"}]
        for method in ("GET", "POST"):
            for headers in invalid:
                with self.subTest(method=method, headers=headers):
                    self.assertEqual(self.request(method, body='{"action":"mirror_start"}', headers=headers)[0], 403)
        self.operations.snapshot.assert_not_called()
        self.operations.command.assert_not_called()

    def test_queries_and_unknown_paths_cannot_trigger_operations(self):
        self.assertEqual(self.request(path="/api/operations?pid=123")[0], 400)
        self.assertEqual(self.request("POST", "/api/operations?force=1", '{"action":"mirror_stop"}')[0], 404)
        self.operations.snapshot.assert_not_called()
        self.operations.command.assert_not_called()

    def test_only_exact_start_and_stop_commands_are_forwarded(self):
        for action in ("mirror_start", "mirror_stop"):
            status, _, payload = self.request("POST", body=json.dumps({"action": action}))
            self.assertEqual(status, 200)
            self.assertIn("dashboard", payload)
            self.operations.command.assert_called_with({"action": action})
        self.experiments.command.assert_not_called()
        self.operations.command.return_value = {**self.payload, "busy": True}
        self.assertEqual(self.request("POST", body='{"action":"mirror_start"}')[0], 202)

    def test_invalid_commands_cannot_supply_process_arguments_or_change_order_settings(self):
        bodies = [None, True, [], "mirror_start", {}, {"action": "start"}, {"action": "pause"},
                  {"action": {"command": "mirror_start"}}, {"action": ["mirror_start"]},
                  {"action": "mirror_stop", "pid": 123}, {"action": "mirror_start", "path": "other"},
                  {"action": "mirror_start", "enabled": True}]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", body=json.dumps(body))[0], 400)
        self.operations.command.assert_not_called()

    def test_request_framing_is_bounded_json_without_transfer_encoding(self):
        body = '{"action":"mirror_start"}'
        for headers, raw in [({**self.headers, "Content-Type": "text/plain"}, body),
                             (self.headers, " " * 513), (self.headers, "{"),
                             ({**self.headers, "Content-Length": "0"}, ""),
                             ({**self.headers, "Transfer-Encoding": "chunked"}, body)]:
            with self.subTest(headers=headers, length=len(raw)):
                self.assertEqual(self.request("POST", body=raw, headers=headers)[0], 400)
        self.operations.command.assert_not_called()

    def test_unavailable_or_failed_service_does_not_expose_exception_details(self):
        self.server.operations_service = None
        try:
            self.assertEqual(self.request()[0], 503)
            self.assertEqual(self.request("POST", body='{"action":"mirror_start"}')[0], 503)
        finally:
            self.server.operations_service = self.operations
        for method in ("GET", "POST"):
            self.operations.snapshot.side_effect = self.operations.command.side_effect = RuntimeError("private-token")
            status, _, result = self.request(method, body='{"action":"mirror_stop"}')
            self.assertEqual(status, 503)
            self.assertNotIn("private-token", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
