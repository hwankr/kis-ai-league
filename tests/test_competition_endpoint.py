import json
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import Mock

from backend.dashboard import DashboardServer


class CompetitionEndpointTests(unittest.TestCase):
    def setUp(self):
        self.server = DashboardServer(0, service=Mock())
        self.server.competition_snapshot = Mock(return_value={"kis": {"status": "ok"}, "timefolio": {"status": "waiting"}})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path="/api/competition", headers=None):
        client = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            client.request("GET", path, headers=headers if headers is not None else {"X-KIS-Dashboard": "1"})
            result = client.getresponse()
            return result.status, json.loads(result.read())
        finally:
            client.close()

    def test_only_same_origin_and_fixed_contest_period_are_accepted(self):
        self.assertEqual(self.request(headers={})[0], 403)
        self.assertEqual(self.request(headers={"X-KIS-Dashboard": "1", "Origin": "https://other.invalid"})[0], 403)
        self.assertEqual(self.request("/api/competition?account=paper")[0], 400)
        self.assertEqual(self.request("/api/competition?start=2026-11-01")[0], 400)
        self.server.competition_snapshot.assert_not_called()
        code, payload = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(payload["timefolio"]["status"], "waiting")

    def test_exception_does_not_expose_local_data(self):
        self.server.competition_snapshot.side_effect = RuntimeError("secret-account")
        code, payload = self.request()
        self.assertEqual(code, 503)
        self.assertEqual(payload, {"error": "대회 요건 조회 실패"})
