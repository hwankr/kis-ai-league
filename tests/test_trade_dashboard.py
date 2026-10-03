import json
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from backend.dashboard import AccountDirectory, DashboardServer, UnknownAccount
from backend.history import series_for_profile
from backend.kis import KisError, load_profiles
from backend.trades import ExecutionHistory
from test_dashboard import sample_balance


START, END = "2026-09-04", "2026-10-03"


def fill(**changes):
    return {"order_date": "2026-10-02", "order_id": "123", "branch_id": "00001",
            "symbol": "005930", "name": "시험 종목", "side": "buy", "quantity": "2",
            "price": "60000", "amount": "120000", "order_time": "09:01:02", **changes}


class TradeDashboardTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name) / "config.toml"
        self.content = ("app_key='test-key'\napp_secret='test-secret'\naccount='12345678'\n"
                        "[accounts.league]\nname='대회 계좌'\napp_key='league-key'\n"
                        "app_secret='league-secret'\naccount='87654321'\n")
        self.config.write_text(self.content, encoding="utf-8")
        self.path = Path(folder.name) / "history.sqlite3"
        self.store = ExecutionHistory(self.path)
        self.clock = Mock(return_value=10)
        self.clients = {key: Mock() for key in ("paper", "league")}
        for client in self.clients.values():
            client.balance.return_value = sample_balance()
            client.executions.return_value = {"environment": "paper", "executions": [fill()]}
        self.factory = Mock(side_effect=lambda profile: self.clients[profile.id])
        self.directory = self.make_directory()

    def make_directory(self, store=None):
        return AccountDirectory(self.config, self.factory, self.clock, sleep=Mock(),
                                history_path=self.path, execution_store=store or self.store)

    def trades(self, account="paper", start=START, end=END):
        return self.directory.trades(account, start, end)

    def test_repeated_and_partial_fills_update_one_order_while_balance_is_independent(self):
        first = self.trades()
        self.assertEqual(first["total_count"], 1)
        self.assertEqual(self.trades(), first)
        self.clients["paper"].executions.assert_called_once_with(START, END)
        self.clock.return_value = 20
        self.clients["paper"].executions.return_value["executions"] = [fill(quantity="5", price="60600", amount="303000")]
        updated = self.trades()
        self.assertEqual(updated["total_count"], 1)
        self.assertEqual(updated["trades"][0]["quantity"], "5")
        self.assertEqual(updated["trades"][0]["price"], "60600")
        balance = self.directory.snapshot("paper")
        self.assertEqual(balance["status"], "ok")
        self.assertEqual(balance["history"]["total_count"], 1)

    def test_failure_preserves_saved_rows_and_timestamp_across_restart_then_recovers(self):
        first = self.trades()
        self.directory = self.make_directory()
        self.clients["paper"].executions.side_effect = KisError("체결 조회 실패")
        failed = self.trades()
        self.assertEqual(failed["status"], "error")
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["trades"], first["trades"])
        self.assertEqual(failed["updated_at"], first["updated_at"])
        self.assertEqual(self.directory.snapshot("paper")["status"], "ok")
        self.clock.return_value = 30
        self.clients["paper"].executions.side_effect = None
        self.assertEqual(self.trades()["status"], "ok")

    def test_initial_error_and_successful_empty_are_distinct(self):
        self.clients["paper"].executions.side_effect = RuntimeError("private-account-and-key")
        failed = self.trades()
        self.assertEqual(failed["status"], "error")
        self.assertFalse(failed["stale"])
        self.assertIsNone(failed["updated_at"])
        self.assertNotIn("private-account", json.dumps(failed))
        self.clock.return_value = 30
        self.clients["paper"].executions.side_effect = None
        self.clients["paper"].executions.return_value["executions"] = []
        empty = self.trades()
        self.assertEqual(empty["status"], "ok")
        self.assertIsNotNone(empty["updated_at"])
        self.assertEqual(empty["trades"], [])

    def test_write_failure_is_not_fresh_success_and_does_not_change_saved_data(self):
        store = Mock(wraps=self.store)
        self.directory = self.make_directory(store)
        first = self.trades()
        self.clock.return_value = 20
        store.record.side_effect = OSError("private-path")
        self.clients["paper"].executions.return_value["executions"] = [fill(quantity="8")]
        result = self.trades()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["trades"], first["trades"])
        self.assertEqual(result["updated_at"], first["updated_at"])
        self.assertNotIn("private-path", json.dumps(result))

    def test_corrupt_store_has_error_not_empty_success(self):
        self.path.write_text("private-corrupt-database", encoding="utf-8")
        result = self.trades()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["trades"], [])
        self.assertIsNone(result["updated_at"])
        self.assertNotIn("private-corrupt", json.dumps(result))

    def test_decreased_cumulative_fill_keeps_previous_values_and_reports_conflict(self):
        first = self.trades()
        self.clock.return_value = 20
        self.clients["paper"].executions.return_value["executions"] = [fill(quantity="1")]
        result = self.trades()
        self.assertEqual(result["status"], "error")
        self.assertIn("누적값", result["error"])
        self.assertEqual(result["trades"], first["trades"])
        self.assertEqual(result["updated_at"], first["updated_at"])

    def test_parallel_requests_use_one_query_and_ranges_have_separate_cache(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: self.trades(), range(6)))
        self.assertTrue(all(result["total_count"] == 1 for result in results))
        self.clients["paper"].executions.assert_called_once()
        self.clients["paper"].executions.return_value["executions"] = []
        other = self.trades(start="2026-09-01", end="2026-09-02")
        self.assertEqual(other["trades"], [])
        self.assertIsNotNone(other["updated_at"])
        self.assertEqual(self.trades()["total_count"], 1)

    def test_identity_changes_clear_values_and_incomplete_accounts_do_not_query(self):
        self.trades()
        for before, after in (("test-key", "new-key"), ("test-secret", "new-secret"), ("12345678", "11112222")):
            with self.subTest(field=before):
                self.config.write_text(self.content.replace(before, after), encoding="utf-8")
                self.clients["paper"].executions.side_effect = KisError("새 설정 조회 실패")
                result = self.trades()
                self.assertEqual(result["trades"], [])
                self.assertIsNone(result["updated_at"])
                self.assertFalse(result["stale"])
        self.config.write_text(self.content.replace("test-secret", ""), encoding="utf-8")
        calls = self.clients["paper"].executions.call_count
        self.assertEqual(self.trades()["trades"], [])
        self.assertEqual(self.clients["paper"].executions.call_count, calls)

    def test_account_and_range_changes_during_query_never_reuse_wrong_identity(self):
        started, release = threading.Event(), threading.Event()
        old_series = series_for_profile(load_profiles(self.config).select("paper"))
        new_client = Mock()
        new_client.executions.return_value = {"executions": [fill(name="새 설정 종목", quantity="9")]}
        def old_query(*args):
            started.set()
            self.assertTrue(release.wait(3))
            return {"executions": [fill()]}
        self.clients["paper"].executions.side_effect = old_query
        self.factory.side_effect = lambda profile: new_client if profile.settings.app_key == "new-key" else self.clients[profile.id]
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(self.trades)
            try:
                self.assertTrue(started.wait(3))
                self.config.write_text(self.content.replace("test-key", "new-key"), encoding="utf-8")
            finally:
                release.set()
            result = pending.result(timeout=5)
        self.assertEqual(result["trades"][0]["quantity"], "9")
        self.assertEqual(self.store.read(old_series, START, END)["trades"][0]["quantity"], "2")

    def test_removed_profile_has_no_default_fallback(self):
        self.trades("league")
        self.config.write_text(self.content.split("[accounts.league]")[0], encoding="utf-8")
        with self.assertRaises(UnknownAccount):
            self.trades("league")

    def test_http_date_validation_same_origin_and_error_status(self):
        server = DashboardServer(0, self.directory)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(query, headers=None):
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                conn.request("GET", "/api/trades" + query, headers=headers or {})
                res = conn.getresponse()
                return res.status, json.loads(res.read())
            finally:
                conn.close()
        query = f"?account=paper&start={START}&end={END}"
        headers = {"X-KIS-Dashboard": "1"}
        self.assertEqual(request(query)[0], 403)
        self.assertEqual(request(query, {**headers, "Origin": "https://bad.invalid"})[0], 403)
        for invalid in ("", "?start=2026-09-01", query + "&start=2026-09-02", query + "&extra=1",
                        "?start=2026-02-30&end=2026-03-01", "?start=2026-10-03&end=2026-09-04",
                        "?start=2026-01-01&end=2026-10-03"):
            self.assertEqual(request(invalid, headers)[0], 400)
        self.assertEqual(request(query, headers)[0], 200)
        self.assertEqual(request(query.replace("paper", "missing"), headers)[0], 404)
        self.clock.return_value = 30
        self.clients["paper"].executions.side_effect = KisError("업무 오류")
        status, failed = request(query, headers)
        self.assertEqual(status, 502)
        self.assertTrue(failed["stale"])


if __name__ == "__main__":
    unittest.main()
