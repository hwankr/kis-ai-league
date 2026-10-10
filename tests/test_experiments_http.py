from http.client import HTTPConnection
import json
import os
import threading
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.dashboard import DashboardServer
from backend.kis import KisError


class ExperimentsHttpTests(unittest.TestCase):
    def setUp(self):
        self.experiments = Mock()
        self.experiments.snapshot.return_value = {"busy": False, "environment": "paper"}
        self.experiments.command.return_value = {"busy": True, "environment": "paper"}
        self.server = DashboardServer(0, service=Mock(), market_service=Mock(), chart_service=Mock(),
                                      candidate_service=Mock(), experiment_service=self.experiments)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method="GET", body=None, headers=None, path="/api/experiments"):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {
                "X-KIS-Dashboard": "1", "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_get_does_not_run_analysis_or_orders(self):
        self.assertEqual(self.request()[0], 200)
        self.experiments.snapshot.assert_called_once_with()
        self.experiments.command.assert_not_called()
        self.experiments.tick.assert_not_called()

    def test_health_is_read_only_local_and_contains_process_identity(self):
        status, payload = self.request(path="/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["pid"], os.getpid())
        self.assertEqual(payload["status"], "ok")
        self.experiments.command.assert_not_called()
        self.experiments.snapshot.assert_not_called()
        self.assertEqual(self.request(path="/api/health", headers={"X-KIS-Dashboard": "0"})[0], 403)
        self.assertEqual(self.request(path="/api/health?force=1")[0], 400)

    def test_mirror_feed_is_read_only_and_passes_bounded_cursor(self):
        payload = {"status": "observing", "submission_enabled": False, "events": [], "next_cursor": 17}
        self.experiments.mirror_snapshot.return_value = payload
        self.assertEqual(self.request(path="/api/mirror?after=17&limit=2"), (200, payload))
        self.experiments.mirror_snapshot.assert_called_once_with(after=17, limit=2)
        self.experiments.command.assert_not_called()
        self.experiments.tick.assert_not_called()
        self.experiments.snapshot.assert_not_called()
        self.assertEqual(self.request("POST", '{"action":"start"}', path="/api/mirror")[0], 404)

    def test_mirror_feed_rejects_cross_origin_and_invalid_pagination(self):
        for bad in ({"Origin": "https://evil.invalid"}, {"Host": "evil.invalid"},
                    {"Sec-Fetch-Site": "cross-site"}, {"X-KIS-Dashboard": "0"}):
            headers = {"X-KIS-Dashboard": "1", **bad}
            self.assertEqual(self.request(path="/api/mirror", headers=headers)[0], 403)
        for query in ("after=-1", "after=", "after=1&after=2", "after=1.5", "limit=0", "limit=201",
                      "limit=1&limit=2", "account=paper", "after=9223372036854775808", "after=" + "1" * 20):
            with self.subTest(query=query):
                self.assertEqual(self.request(path="/api/mirror?" + query)[0], 400)
        self.experiments.mirror_snapshot.assert_not_called()

    def test_mirror_feed_reports_source_conflict_and_hides_unexpected_details(self):
        self.experiments.mirror_snapshot.side_effect = KisError("연동 체결 조회 조건 또는 원장 상태를 확인하세요.")
        self.assertEqual(self.request(path="/api/mirror")[0], 400)
        self.experiments.mirror_snapshot.side_effect = RuntimeError("private-token")
        status, payload = self.request(path="/api/mirror")
        self.assertEqual(status, 503)
        self.assertNotIn("private", json.dumps(payload))

    def test_question_answer_is_routed_once_and_returns_current_autonomy(self):
        monitor = Mock()
        monitor.snapshot.return_value = {"status": "attention", "issues": []}
        self.server.autonomous_monitor = monitor
        body = {"action": "answer", "id": "order:1", "answer": "retry"}
        status, payload = self.request("POST", json.dumps(body))
        self.assertEqual(status, 200)
        self.assertEqual(payload["autonomy"]["status"], "attention")
        monitor.answer.assert_called_once_with(body)
        self.experiments.command.assert_not_called()

    def test_health_size_does_not_grow_with_order_issue_history(self):
        monitor = Mock()
        monitor.stalled.return_value = False
        monitor.snapshot.return_value = {"status": "healthy", "issues": [
            {"state": "resolved", "message": "older order detail" * 100} for _ in range(1000)]}
        self.server.autonomous_monitor = monitor
        status, payload = self.request(path="/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertLess(len(json.dumps(payload)), 2048)
        self.assertEqual(payload["autonomy"]["open_issue_count"], 0)
        monitor.snapshot.assert_called_once_with(include_history=False)

    def test_async_command_is_forwarded_exactly_once(self):
        self.assertEqual(self.request("POST", '{"action":"analyze"}')[0], 202)
        self.experiments.command.assert_called_once_with({"action": "analyze"})

    def test_cross_origin_and_missing_header_cannot_mutate(self):
        for bad in ({"Origin": "https://evil.invalid"}, {"Host": "evil.invalid"},
                    {"Sec-Fetch-Site": "cross-site"}, {"X-KIS-Dashboard": "0"}):
            for method in ("GET", "POST"):
                headers = {"X-KIS-Dashboard": "1", "Content-Type": "application/json", **bad}
                self.assertEqual(self.request(method, '{"action":"start"}', headers)[0], 403)
        self.experiments.command.assert_not_called()

    def test_queries_and_invalid_or_large_bodies_never_reach_service(self):
        self.assertEqual(self.request(path="/api/experiments?refresh=1")[0], 400)
        self.assertEqual(self.request("POST", "{}", path="/api/experiments?start=1")[0], 404)
        for body in ("", "{broken", " " * 8193):
            self.assertEqual(self.request("POST", body)[0], 400)
        self.assertEqual(self.request("POST", "{}", {"X-KIS-Dashboard": "1", "Content-Type": "text/plain"})[0], 400)
        self.experiments.command.assert_not_called()

    def test_errors_report_validation_but_never_raw_exception(self):
        self.experiments.command.side_effect = KisError("실험 한도를 입력하세요.")
        self.assertEqual(self.request("POST", '{"action":"start"}'), (400, {"error": "실험 한도를 입력하세요."}))
        self.experiments.command.side_effect = RuntimeError("private-token")
        status, payload = self.request("POST", '{"action":"start"}')
        self.assertEqual(status, 503)
        self.assertNotIn("private", json.dumps(payload))

    def test_port_conflict_does_not_initialize_or_pause_experiments(self):
        with patch("backend.experiments.ExperimentService") as factory:
            with self.assertRaises(OSError):
                DashboardServer(self.server.server_port)
            factory.assert_not_called()

    def test_different_port_cannot_initialize_second_owner_for_same_experiment_state(self):
        with tempfile.TemporaryDirectory() as directory, patch("backend.dashboard.ROOT", Path(directory)), \
             patch("backend.dashboard.AccountDirectory"), patch("backend.dashboard.MarketService"), \
             patch("backend.dashboard.ChartService"), patch("backend.dashboard.CandidateService"), \
             patch("backend.experiments.ExperimentService") as factory, \
             patch("backend.forward_observer.ForwardObserver") as research_factory:
            research = Mock(minimum_sessions=63)
            research_factory.return_value = research
            factory.return_value.close.return_value = True
            first = DashboardServer(0)
            try:
                with self.assertRaises(BlockingIOError):
                    DashboardServer(0)
                factory.assert_called_once_with()
                research_factory.assert_called_once_with()
            finally:
                first.server_close()


if __name__ == "__main__":
    unittest.main()
