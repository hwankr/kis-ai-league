import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path
import tempfile
from unittest.mock import Mock

from backend.dashboard import (AccountDirectory, AccountService, DashboardServer,
                               UnknownAccount, normalize_balance)
from backend.kis import KisError


def sample_balance():
    return {"summary": {
        "dnca_tot_amt": "500000", "scts_evlu_amt": "202000",
        "tot_evlu_amt": "697000", "pchs_amt_smtl_amt": "200000",
        "evlu_pfls_smtl_amt": "2000",
    }, "holdings": [{
        "pdno": "005930", "prdt_name": "테스트 종목", "hldg_qty": "2",
        "pchs_avg_pric": "100000", "prpr": "101000", "pchs_amt": "200000",
        "evlu_amt": "202000", "evlu_pfls_amt": "2000", "evlu_pfls_rt": "1.00",
    }, {"pdno": "000660", "hldg_qty": "0"}]}


class AccountTests(unittest.TestCase):
    def test_numbers_keep_api_total_and_exclude_closed_positions(self):
        result = normalize_balance(sample_balance())
        self.assertEqual(result["summary"]["total_value"], "697000")
        self.assertEqual(result["summary"]["unrealized_return_pct"], "1.0000")
        self.assertEqual(len(result["holdings"]), 1)
        self.assertEqual(result["holdings"][0]["symbol"], "005930")
        self.assertEqual(result["holdings"][0]["pnl"], "2000")

    def test_missing_values_are_not_zero_and_zero_basis_has_no_return(self):
        balance = sample_balance()
        balance["summary"].update(dnca_tot_amt=None, scts_evlu_amt="NaN",
                                  pchs_amt_smtl_amt="0", evlu_pfls_smtl_amt="0")
        result = normalize_balance(balance)["summary"]
        self.assertIsNone(result["cash"])
        self.assertIsNone(result["securities_value"])
        self.assertIsNone(result["unrealized_return_pct"])
        self.assertEqual(result["unrealized_pnl"], "0")

    def test_failed_refresh_preserves_last_success_and_recovers(self):
        clock = Mock(return_value=10)
        client = Mock()
        client.balance.side_effect = [sample_balance(), KisError("KIS 연결 실패"), sample_balance()]
        service = AccountService(lambda: client, clock)
        first = service.snapshot()
        clock.return_value = 20
        failed = service.snapshot()
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["updated_at"], first["updated_at"])
        self.assertEqual(failed["summary"], first["summary"])
        service.snapshot()
        self.assertEqual(client.balance.call_count, 2)
        clock.return_value = 31
        recovered = service.snapshot()
        self.assertEqual(recovered["status"], "ok")
        self.assertFalse(recovered["stale"])
        self.assertIsNone(recovered["error"])

    def test_initial_failure_is_unavailable_and_hides_unexpected_exception(self):
        client = Mock()
        client.balance.side_effect = RuntimeError("secret-account-and-token")
        result = AccountService(lambda: client).snapshot()
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["updated_at"])
        self.assertEqual(result["summary"], {})
        self.assertNotIn("secret-account", json.dumps(result))

    def test_parallel_readers_share_one_broker_request(self):
        client = Mock()
        client.balance.return_value = sample_balance()
        service = AccountService(lambda: client, clock=lambda: 10)
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: service.snapshot(), range(6)))
        self.assertTrue(all(result["status"] == "ok" for result in results))
        client.balance.assert_called_once()


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = Mock()
        cls.service.snapshot.return_value = {"status": "ok", "environment": "paper"}
        cls.service.list_accounts.return_value = {
            "default_account": "paper",
            "accounts": [{"id": "paper", "name": "일반 모의투자", "configured": True}],
        }
        cls.server = DashboardServer(0, cls.service)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def request(self, path, headers=None, method="GET"):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.headers), response.read()
        finally:
            connection.close()

    def test_api_is_no_store_and_requires_same_origin_header(self):
        status, headers, body = self.request("/api/account", {"X-KIS-Dashboard": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(json.loads(body)["environment"], "paper")
        self.assertEqual(self.request("/api/account")[0], 403)
        self.assertEqual(self.request("/api/account", {
            "X-KIS-Dashboard": "1", "Origin": "https://other.invalid"})[0], 403)
        self.assertEqual(self.request("/api/account", {
            "X-KIS-Dashboard": "1", "Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_rebinding_and_private_file_requests_are_blocked(self):
        for path in ("/config.local.toml", "/.local/kis-token.json",
                     "/../config.local.toml", "/backend/kis.py"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request("/", {"Host": "other.invalid"})[0], 403)
        self.assertEqual(self.request("/api/account", method="POST")[0], 501)

    def test_account_list_and_selection_require_same_origin_and_keep_requested_id(self):
        headers = {"X-KIS-Dashboard": "1"}
        status, _, body = self.request("/api/accounts", headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["accounts"][0]["id"], "paper")
        self.assertEqual(self.request("/api/accounts")[0], 403)
        self.assertEqual(self.request("/api/accounts", {
            **headers, "Origin": "https://other.invalid"})[0], 403)
        self.assertEqual(self.request("/api/account?account=competition", headers)[0], 200)
        self.service.snapshot.assert_called_with("competition")
        for path in ("/api/account?account=", "/api/account?account=paper&account=other",
                     "/api/account?invalid=paper", "/api/accounts?account=paper"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, headers)[0], 400)


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.toml"
        self.legacy = ("app_key = 'paper-key'\napp_secret = 'paper-secret'\n"
                       "account = '01234567'\nproduct_code = '01'\n")
        self.competition = ("\n[accounts.competition]\nname = '대회 모의투자'\n"
                            "app_key = 'contest-key'\napp_secret = 'contest-secret'\n"
                            "account = '87654321'\nproduct_code = '01'\n")
        self.path.write_text(self.legacy + self.competition, encoding="utf-8")
        self.clock = Mock(return_value=10)
        self.clients = {"paper": Mock(), "competition": Mock()}
        self.clients["paper"].balance.return_value = sample_balance()
        contest_balance = sample_balance()
        contest_balance["summary"]["tot_evlu_amt"] = "100000000"
        self.clients["competition"].balance.return_value = contest_balance
        self.factory = Mock(side_effect=lambda profile: self.clients[profile.id])
        self.service = AccountDirectory(self.path, self.factory, self.clock, sleep=Mock())

    def test_account_list_contains_only_public_metadata(self):
        listing = self.service.list_accounts()
        self.assertEqual(listing["default_account"], "paper")
        self.assertEqual([row["id"] for row in listing["accounts"]], ["paper", "competition"])
        for row in listing["accounts"]:
            self.assertEqual(set(row), {"id", "name", "configured"})
        text = json.dumps(listing)
        for private in ("paper-key", "paper-secret", "contest-key", "01234567", "87654321"):
            self.assertNotIn(private, text)
        self.factory.assert_not_called()

    def test_switching_isolates_balances_errors_and_previous_snapshots(self):
        paper = self.service.snapshot()
        contest = self.service.snapshot("competition")
        self.assertEqual(paper["account"]["id"], "paper")
        self.assertEqual(paper["summary"]["total_value"], "697000")
        self.assertEqual(contest["summary"]["total_value"], "100000000")
        self.clock.return_value = 20
        self.clients["competition"].balance.side_effect = KisError("테스트 조회 실패")
        failed = self.service.snapshot("competition")
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["updated_at"], contest["updated_at"])
        self.assertEqual(failed["summary"], contest["summary"])
        self.assertEqual(self.service.snapshot("paper")["status"], "ok")
        self.assertEqual(self.service.snapshot("paper")["summary"], paper["summary"])

    def test_accounts_can_be_added_and_removed_without_restart(self):
        self.path.write_text(self.legacy, encoding="utf-8")
        self.assertEqual(len(self.service.list_accounts()["accounts"]), 1)
        self.path.write_text(self.legacy + self.competition, encoding="utf-8")
        self.assertEqual(len(self.service.list_accounts()["accounts"]), 2)
        self.service.snapshot("competition")
        self.path.write_text(self.legacy, encoding="utf-8")
        with self.assertRaises(UnknownAccount):
            self.service.snapshot("competition")
        self.assertEqual(self.service.snapshot()["account"]["id"], "paper")
        self.assertNotIn("competition", self.service.services)

    def test_replacing_an_account_or_key_discards_its_previous_data(self):
        self.service.snapshot("paper")
        self.path.write_text(self.legacy.replace("01234567", "11112222") + self.competition,
                             encoding="utf-8")
        self.clients["paper"].balance.side_effect = KisError("새 계좌 연결 실패")
        changed = self.service.snapshot("paper")
        self.assertEqual(changed["summary"], {})
        self.assertIsNone(changed["updated_at"])
        self.assertFalse(changed["stale"])
        self.assertEqual(self.factory.call_count, 2)

    def test_incomplete_profile_never_queries_or_reuses_previous_snapshot(self):
        self.service.snapshot("competition")
        self.path.write_text(self.legacy + self.competition.replace("contest-secret", ""),
                             encoding="utf-8")
        result = self.service.snapshot("competition")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["summary"], {})
        self.assertIsNone(result["updated_at"])
        self.assertFalse(self.service.list_accounts()["accounts"][1]["configured"])
        self.clients["competition"].balance.assert_called_once()
        self.assertEqual(self.service.snapshot("paper")["status"], "ok")

    def test_parallel_profiles_cache_independently_and_unknown_has_no_fallback(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            ids = ["paper", "competition"] * 3
            results = list(executor.map(self.service.snapshot, ids))
        self.assertEqual([result["account"]["id"] for result in results], ids)
        for client in self.clients.values():
            client.balance.assert_called_once()
        with self.assertRaises(UnknownAccount):
            self.service.snapshot("../missing")

    def test_http_errors_do_not_fall_back_to_another_account(self):
        server = DashboardServer(0, self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", "/api/account?account=missing",
                               headers={"X-KIS-Dashboard": "1"})
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            self.assertNotIn("summary", json.loads(response.read()))
            connection.close()
            self.factory.assert_not_called()
            self.path.write_text("app_secret = 'private-invalid-value", encoding="utf-8")
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", "/api/accounts", headers={"X-KIS-Dashboard": "1"})
            response = connection.getresponse()
            self.assertEqual(response.status, 503)
            self.assertNotIn(b"private-invalid-value", response.read())
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
