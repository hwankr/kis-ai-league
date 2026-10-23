from email.message import Message
from io import BytesIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from backend.dashboard import DashboardHandler


class FailingWriter(BytesIO):
    def __init__(self, fail_at=None, error=None):
        super().__init__()
        self.attempts = []
        self.fail_at = fail_at
        self.error = error

    def write(self, value):
        self.attempts.append(value)
        if len(self.attempts) == self.fail_at:
            raise self.error
        return super().write(value)


class DashboardDisconnectTests(unittest.TestCase):
    routes = (
        ("GET", "/api/competition", "competition_snapshot", 200),
        ("GET", "/api/timefolio", "timefolio_snapshot", 200),
        ("GET", "/api/operations", "operations_snapshot", 200),
        ("GET", "/api/experiments", "experiment_snapshot", 200),
        ("GET", "/api/mirror", "experiment_service.mirror_snapshot", 200),
        ("GET", "/api/research", "research_service.snapshot", 200),
        ("GET", "/api/candidates", "candidate_service.snapshot", 200),
        ("GET", "/api/chart?symbol=005930", "chart_service.snapshot", 200),
        ("GET", "/api/market", "market_service.snapshot", 200),
        ("GET", "/api/accounts", "service.list_accounts", 200),
        ("GET", "/api/account", "service.snapshot", 200),
        ("POST", "/api/candidates", "candidate_service.start", 202),
        ("POST", "/api/operations", "operations_service.command", 202),
        ("POST", "/api/experiments", "experiment_service.command", 202),
    )

    def handler(self, method, path, operation, status, *, fail_at=None, error=None):
        handler = object.__new__(DashboardHandler)
        handler.command = method
        handler.path = path
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.close_connection = False
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:1"
        handler.headers["X-KIS-Dashboard"] = "1"
        handler.headers["Content-Type"] = "application/json"
        command = {} if path == "/api/candidates" else {
            "action": "mirror_start" if path == "/api/operations" else "analyze"}
        body = json.dumps(command).encode()
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = BytesIO(body)
        handler.connection = Mock()
        handler.wfile = FailingWriter(fail_at, error)
        handler.send_response = Mock(wraps=handler.send_response)
        payload = {"status": "running" if status == 202 else "ok", "busy": status == 202}
        handler.server = SimpleNamespace(
            hosts={"127.0.0.1:1"}, origins={"http://127.0.0.1:1"},
            service=Mock(), experiment_service=Mock(), operations_service=Mock(),
            candidate_service=Mock(), research_service=Mock(), chart_service=Mock(),
            market_service=Mock(), autonomous_monitor=None,
            competition_snapshot=Mock(), timefolio_snapshot=Mock(),
            experiment_snapshot=Mock(side_effect=lambda value: value),
            operations_snapshot=Mock(side_effect=lambda value: value),
        )
        service = handler.server
        for name in operation.split("."):
            service = getattr(service, name)
        service.side_effect = None
        service.return_value = payload
        return handler, service

    def dispatch(self, handler, method):
        getattr(handler, "do_" + method)()

    def statuses(self, handler):
        return [call.args[0] for call in handler.send_response.call_args_list]

    def test_header_and_body_disconnects_end_each_route_without_error_response_or_repeat(self):
        for route in self.routes:
            for fail_at in (1, 2):
                for exception in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    with self.subTest(route=route[:2], stage=fail_at, exception=exception.__name__):
                        handler, service = self.handler(*route, fail_at=fail_at, error=exception("client left"))
                        self.dispatch(handler, route[0])
                        service.assert_called_once()
                        self.assertEqual(self.statuses(handler), [route[3]])
                        self.assertEqual(len(handler.wfile.attempts), fail_at)
                        self.assertTrue(handler.close_connection)
                        # A later fallback must not append another response to this socket.
                        handler.json_response(503, {"error": "fallback"})
                        self.assertEqual(self.statuses(handler), [route[3]])
                        self.assertEqual(len(handler.wfile.attempts), fail_at)

    def test_unexpected_transmission_errors_propagate_without_retrying_response(self):
        for route in self.routes:
            for fail_at in (1, 2):
                with self.subTest(route=route[:2], stage=fail_at):
                    error = OSError("unrelated transport failure")
                    handler, service = self.handler(*route, fail_at=fail_at, error=error)
                    with self.assertRaises(OSError) as caught:
                        self.dispatch(handler, route[0])
                    self.assertIs(caught.exception, error)
                    service.assert_called_once()
                    self.assertEqual(self.statuses(handler), [route[3]])
                    self.assertEqual(len(handler.wfile.attempts), fail_at)
                    self.assertTrue(handler.close_connection)

    def test_business_failure_before_response_still_returns_one_503(self):
        for route in self.routes:
            with self.subTest(route=route[:2]):
                handler, service = self.handler(*route)
                service.side_effect = RuntimeError("private implementation details")
                self.dispatch(handler, route[0])
                service.assert_called_once()
                self.assertEqual(self.statuses(handler), [503])
                self.assertEqual(len(handler.wfile.attempts), 2)
                result = json.loads(handler.wfile.attempts[-1])
                self.assertIn("error", result)
                self.assertNotIn("private implementation details", result["error"])

    def test_connection_error_from_business_work_keeps_the_existing_error_response(self):
        for route in self.routes:
            for exception in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                with self.subTest(route=route[:2], exception=exception.__name__):
                    handler, service = self.handler(*route)
                    service.side_effect = exception("upstream connection failed")
                    self.dispatch(handler, route[0])
                    service.assert_called_once()
                    expected = 400 if route[0] == "POST" and route[1] != "/api/candidates" else 503
                    self.assertEqual(self.statuses(handler), [expected])
                    self.assertEqual(len(handler.wfile.attempts), 2)

    def test_success_response_cannot_be_followed_by_a_second_response(self):
        handler, service = self.handler(*self.routes[0])
        self.dispatch(handler, "GET")
        handler.json_response(503, {"error": "late failure"})
        service.assert_called_once()
        self.assertEqual(self.statuses(handler), [200])
        self.assertEqual(len(handler.wfile.attempts), 2)
        self.assertTrue(handler.close_connection)

    def test_response_state_is_reset_for_the_next_request(self):
        handler, service = self.handler(*self.routes[0])
        request = b"GET /api/competition HTTP/1.1\r\nHost: 127.0.0.1:1\r\nX-KIS-Dashboard: 1\r\n\r\n"
        handler.rfile = BytesIO(request * 2)
        handler.handle_one_request()
        handler.handle_one_request()
        self.assertEqual(self.statuses(handler), [200, 200])
        self.assertEqual(service.call_count, 2)
        self.assertEqual(len(handler.wfile.attempts), 4)


if __name__ == "__main__":
    unittest.main()
