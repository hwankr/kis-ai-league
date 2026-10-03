from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.dashboard import AccountDirectory
from backend.kis import KisError, PaperClient, _execution_row, client_for_profile


class TradeBrokerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        config = root / "config.toml"
        config.write_text("app_key='sample-key'\napp_secret='sample-secret'\n"
                          "account='12345678'\n", encoding="utf-8")
        self.now = 100.0
        self.sleeps = []
        self.clients = []

        def factory(profile):
            client = client_for_profile(profile, root / "tokens")
            self.clients.append(client)
            return client

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.now += seconds

        self.accounts = AccountDirectory(config, client_factory=factory,
                                         clock=lambda: self.now, sleep=sleep,
                                         history_path=root / "history.sqlite3")
        self.today = patch("backend.kis._execution_today", return_value=date(2026, 10, 3))
        self.today.start()
        self.addCleanup(self.today.stop)

    def test_balance_runs_between_execution_pages_and_clients_share_request_spacing(self):
        page_available, balance_finished = threading.Event(), threading.Event()
        calls = []
        row = {"ord_dt": "20261002", "ord_gno_brno": "00001", "odno": "0000000123",
               "pdno": "005930", "prdt_name": "삼성전자", "sll_buy_dvsn_cd": "02",
               "tot_ccld_qty": "2", "avg_prvs": "70000", "tot_ccld_amt": "140000"}

        def perform(client, path, *, params=None, **kwargs):
            self.assertIsNotNone(client.request_guard)
            self.assertTrue(self.accounts.broker_lock.locked())
            calls.append((path.rsplit("/", 1)[1], self.now))
            if path.endswith("inquire-balance"):
                return {"rt_cd": "0", "output1": [], "output2": [{
                    "tot_evlu_amt": "10000000", "dnca_tot_amt": "10000000",
                    "scts_evlu_amt": "0"}]}, {"tr_cont": "D"}
            if not params["CTX_AREA_NK100"]:
                return {"rt_cd": "0", "output1": [row], "output2": {},
                        "ctx_area_fk100": "search", "ctx_area_nk100": "next"}, {"tr_cont": "F"}
            return {"rt_cd": "0", "output1": [{**row, "odno": "0000000124"}],
                    "output2": {}}, {"tr_cont": "D"}

        def normalize(raw, start, end):
            if raw["odno"] == "0000000123":
                # 실제 페이지 응답 이후 처리 중 잔고 요청을 넣는다. 전체 조회 잠금을
                # 잡아 둔 회귀가 생기면 잔고가 완료되지 못해 테스트가 실패한다.
                page_available.set()
                if not balance_finished.wait(3):
                    raise AssertionError("Balance blocked until execution pagination finishes")
            return _execution_row(raw, start, end)

        with patch.object(PaperClient, "token", return_value="test-token"), \
                patch.object(PaperClient, "_perform_request", perform), \
                patch("backend.kis._execution_row", side_effect=normalize), \
                ThreadPoolExecutor(max_workers=2) as executor:
            trades = executor.submit(self.accounts.trades, "paper", "2026-10-01", "2026-10-03")
            self.assertTrue(page_available.wait(3))

            def balance():
                try:
                    return self.accounts.snapshot("paper")
                finally:
                    balance_finished.set()

            balance_result = executor.submit(balance).result(timeout=3)
            trade_result = trades.result(timeout=3)

        self.assertEqual(balance_result["status"], "ok")
        self.assertEqual(trade_result["status"], "ok")
        self.assertEqual(trade_result["total_count"], 2)
        self.assertEqual(len(self.clients), 2)
        self.assertIsNot(self.clients[0], self.clients[1])
        self.assertTrue(all(isinstance(client, PaperClient) for client in self.clients))
        self.assertEqual(calls, [("inquire-daily-ccld", 100.0), ("inquire-balance", 101.0),
                                 ("inquire-daily-ccld", 102.0)])
        self.assertEqual(self.sleeps, [0, 1, 1])
        self.assertFalse(self.accounts.broker_lock.locked())

    def test_failed_trade_request_releases_shared_guard_and_preserves_spacing(self):
        calls = []

        def perform(client, path, **kwargs):
            calls.append((path.rsplit("/", 1)[1], self.now))
            if path.endswith("inquire-daily-ccld"):
                raise KisError("체결 조회 실패")
            return {"rt_cd": "0", "output1": [], "output2": [{"tot_evlu_amt": "10000000"}]}, {}

        with patch.object(PaperClient, "token", return_value="test-token"), \
                patch.object(PaperClient, "_perform_request", perform):
            trade_result = self.accounts.trades("paper", "2026-10-01", "2026-10-03")
            balance_result = self.accounts.snapshot("paper")
        self.assertEqual(trade_result["status"], "error")
        self.assertEqual(trade_result["error"], "체결 조회 실패")
        self.assertEqual(balance_result["status"], "ok")
        self.assertEqual(calls, [("inquire-daily-ccld", 100.0), ("inquire-balance", 101.0)])
        self.assertFalse(self.accounts.broker_lock.locked())

    def test_simultaneous_first_balance_and_trades_share_one_token_issuance(self):
        auth_started, second_token_requested = threading.Event(), threading.Event()
        token_count_lock = threading.Lock()
        calls = []
        original_token = PaperClient.token
        token_count = 0

        def token(client):
            nonlocal token_count
            with token_count_lock:
                token_count += 1
                if token_count == 2:
                    second_token_requested.set()
            return original_token(client)

        def perform(client, path, **kwargs):
            calls.append((path.rsplit("/", 1)[1], self.now))
            if path == "/oauth2/tokenP":
                auth_started.set()
                if not second_token_requested.wait(3):
                    raise AssertionError("The second client did not attempt concurrent authentication")
                return {"access_token": "test-token", "expires_in": 86400}, {}
            if path.endswith("inquire-balance"):
                return {"rt_cd": "0", "output1": [], "output2": [{"tot_evlu_amt": "10000000"}]}, {}
            return {"rt_cd": "0", "output1": [], "output2": {}}, {}

        with patch.object(PaperClient, "token", token), \
                patch.object(PaperClient, "_perform_request", perform), \
                ThreadPoolExecutor(max_workers=2) as executor:
            trades = executor.submit(self.accounts.trades, "paper", "2026-10-01", "2026-10-03")
            self.assertTrue(auth_started.wait(3))
            balance = executor.submit(self.accounts.snapshot, "paper")
            trade_result, balance_result = trades.result(timeout=3), balance.result(timeout=3)

        self.assertEqual(trade_result["status"], "ok")
        self.assertEqual(balance_result["status"], "ok")
        self.assertEqual(len(self.clients), 2)
        self.assertEqual(self.clients[0].cache_path, self.clients[1].cache_path)
        self.assertCountEqual([path for path, _ in calls],
                              ["tokenP", "inquire-daily-ccld", "inquire-balance"])
        self.assertEqual([moment for _, moment in calls], [100.0, 101.0, 102.0])
        self.assertEqual(self.sleeps, [0, 1, 1])


if __name__ == "__main__":
    unittest.main()
