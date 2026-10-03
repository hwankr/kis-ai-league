import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import Mock

from backend.dashboard import (AccountDirectory, AccountService, DashboardServer,
                               UnknownAccount, normalize_balance)
from backend.history import AccountHistory, series_for_profile
from backend.kis import KisError, load_profiles


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
                     "/.local/account-history.sqlite3", "/.local/account-history.sqlite3-journal",
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
        self.history_path = Path(self.directory.name) / "private" / "history.sqlite3"
        self.service = AccountDirectory(self.path, self.factory, self.clock, sleep=Mock(),
                                        history_path=self.history_path)

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

    def test_history_records_only_fresh_successes_including_unchanged_balances(self):
        first = self.service.snapshot("paper")
        self.assertEqual(first["history"]["total_count"], 1)
        self.assertEqual(first["history"]["points"], [{
            "observed_at": first["updated_at"], "total_value": "697000", "cash": "500000",
        }])
        self.assertEqual(self.service.snapshot("paper")["history"], first["history"])
        self.clock.return_value = 20
        self.clients["paper"].balance.side_effect = KisError("조회 실패")
        self.assertEqual(self.service.snapshot("paper")["history"], first["history"])
        self.assertEqual(self.service.snapshot("paper")["history"], first["history"])
        self.clients["paper"].balance.side_effect = None
        self.clock.return_value = 31
        recovered = self.service.snapshot("paper")
        self.assertEqual(recovered["history"]["total_count"], 2)
        self.assertEqual([point["total_value"] for point in recovered["history"]["points"]],
                         ["697000", "697000"])
        self.assertRegex(first["updated_at"], r"\.\d{6}\+00:00$")
        with closing(sqlite3.connect(self.history_path)) as connection:
            snapshot = json.loads(connection.execute(
                "SELECT snapshot_json FROM observations ORDER BY id LIMIT 1").fetchone()[0])
        self.assertEqual(snapshot, normalize_balance(sample_balance()))

    def test_parallel_requests_save_one_observation_per_actual_query(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(self.service.snapshot, ["paper", "competition"] * 4))
        self.assertTrue(all(result["history"]["total_count"] == 1 for result in results))
        for client in self.clients.values():
            client.balance.assert_called_once()
        self.assertEqual(self.service.snapshot("paper")["history"]["points"][0]["total_value"],
                         "697000")
        self.assertEqual(self.service.snapshot("competition")["history"]["points"][0]["total_value"],
                         "100000000")

    def test_history_survives_restart_without_restoring_old_balance_as_fresh(self):
        first = self.service.snapshot("paper")
        self.clients["paper"].balance.side_effect = KisError("재시작 후 조회 실패")
        restarted = AccountDirectory(self.path, self.factory, self.clock, sleep=Mock(),
                                     history_path=self.history_path)
        result = restarted.snapshot("paper")
        self.assertEqual(result["history"], first["history"])
        self.assertEqual(result["summary"], {})
        self.assertIsNone(result["updated_at"])
        self.assertFalse(result["stale"])
        self.clients["paper"].balance.side_effect = None
        self.clock.return_value = 21
        self.assertEqual(restarted.snapshot("paper")["history"]["total_count"], 2)

    def test_each_identity_change_starts_separate_history_and_rename_keeps_it(self):
        first = self.service.snapshot("competition")
        renamed = self.competition.replace("대회 모의투자", "새 대회 이름")
        self.path.write_text(self.legacy + renamed, encoding="utf-8")
        result = self.service.snapshot("competition")
        self.assertEqual(result["account"]["name"], "새 대회 이름")
        self.assertEqual(result["history"], first["history"])
        self.clients["competition"].balance.side_effect = KisError("변경 계좌 조회 실패")
        for before, after in (("contest-key", "new-key"), ("contest-secret", "new-secret"),
                              ("87654321", "11112222"), ("product_code = '01'", "product_code = '02'")):
            with self.subTest(field=before):
                self.path.write_text(self.legacy + renamed.replace(before, after), encoding="utf-8")
                result = self.service.snapshot("competition")
                self.assertEqual(result["history"], {"points": [], "total_count": 0, "error": None})
                self.assertEqual(result["summary"], {})
        self.path.write_text(self.legacy + renamed, encoding="utf-8")
        self.assertEqual(self.service.snapshot("competition")["history"], first["history"])

    def test_same_credentials_under_different_profiles_still_have_separate_history(self):
        self.service.snapshot("paper")
        same_identity = self.competition.replace("contest-key", "paper-key").replace(
            "contest-secret", "paper-secret").replace("87654321", "01234567")
        self.path.write_text(self.legacy + same_identity, encoding="utf-8")
        contest = self.service.snapshot("competition")
        self.assertEqual(contest["history"]["total_count"], 1)
        self.assertEqual(contest["history"]["points"][0]["total_value"], "100000000")

    def test_initial_error_and_incomplete_profiles_do_not_create_observations(self):
        self.clients["paper"].balance.side_effect = KisError("첫 조회 실패")
        result = self.service.snapshot("paper")
        self.assertEqual(result["history"], {"points": [], "total_count": 0, "error": None})
        store = Mock()
        self.path.write_text(self.legacy + self.competition.replace("contest-secret", ""),
                             encoding="utf-8")
        service = AccountDirectory(self.path, self.factory, self.clock, sleep=Mock(), history_store=store)
        self.assertEqual(service.snapshot("competition")["history"], result["history"])
        store.read.assert_not_called()
        store.record.assert_not_called()
        self.clients["competition"].balance.assert_not_called()

    def test_history_write_failure_keeps_fresh_balance_and_safe_separate_error(self):
        store = Mock(wraps=AccountHistory(self.history_path))
        service = AccountDirectory(self.path, self.factory, self.clock, sleep=Mock(), history_store=store)
        first = service.snapshot("paper")
        self.clock.return_value = 20
        store.record.side_effect = OSError("private-key-account-and-path")
        result = service.snapshot("paper")
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["error"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["summary"], first["summary"])
        self.assertEqual(result["history"]["points"], first["history"]["points"])
        self.assertIsNotNone(result["history"]["error"])
        self.assertNotIn("private-key", json.dumps(result))
        self.assertEqual(service.snapshot("paper")["history"], result["history"])
        self.assertEqual(store.record.call_count, 2)
        store.record.side_effect = None
        self.clock.return_value = 30
        recovered = service.snapshot("paper")
        self.assertIsNone(recovered["history"]["error"])
        self.assertEqual(recovered["history"]["total_count"], 2)

    def test_corrupt_database_does_not_fail_or_leak_through_account_response(self):
        self.history_path.parent.mkdir()
        self.history_path.write_text("invalid-sqlite-private-value", encoding="utf-8")
        result = self.service.snapshot("paper")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["summary"]["total_value"], "697000")
        self.assertIsNotNone(result["history"]["error"])
        self.assertNotIn("invalid-sqlite", json.dumps(result))

    def test_config_change_during_query_cannot_return_previous_series(self):
        started, release = threading.Event(), threading.Event()
        original = load_profiles(self.path).select("paper")
        old_series = series_for_profile(original)
        new_client = Mock()
        new_balance = sample_balance()
        new_balance["summary"]["tot_evlu_amt"] = "900000"
        new_client.balance.return_value = new_balance
        def old_balance():
            started.set()
            if not release.wait(3):
                raise RuntimeError("test query timeout")
            return sample_balance()
        self.clients["paper"].balance.side_effect = old_balance
        self.factory.side_effect = lambda profile: (new_client if profile.settings.app_key == "new-key"
                                                   else self.clients[profile.id])
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(self.service.snapshot, "paper")
            try:
                self.assertTrue(started.wait(3))
                self.path.write_text(self.legacy.replace("paper-key", "new-key") + self.competition,
                                     encoding="utf-8")
            finally:
                release.set()
            result = pending.result(timeout=5)
        self.assertEqual(result["summary"]["total_value"], "900000")
        self.assertEqual(result["history"]["total_count"], 1)
        self.assertEqual(result["history"]["points"][0]["total_value"], "900000")
        self.assertEqual(AccountHistory(self.history_path).read(old_series)["points"][0]["total_value"],
                         "697000")

    def test_default_account_change_during_query_returns_new_default_series(self):
        def change_default():
            self.path.write_text("default_account = 'competition'\n" + self.legacy + self.competition,
                                 encoding="utf-8")
            return sample_balance()
        self.clients["paper"].balance.side_effect = change_default
        result = self.service.snapshot()
        self.assertEqual(result["account"]["id"], "competition")
        self.assertEqual(result["summary"]["total_value"], "100000000")
        self.assertEqual(result["history"]["total_count"], 1)

    def test_profile_becoming_incomplete_during_query_returns_empty_history(self):
        def incomplete():
            self.path.write_text(self.legacy.replace("paper-secret", "") + self.competition,
                                 encoding="utf-8")
            return sample_balance()
        self.clients["paper"].balance.side_effect = incomplete
        result = self.service.snapshot("paper")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["summary"], {})
        self.assertEqual(result["history"], {"points": [], "total_count": 0, "error": None})

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
