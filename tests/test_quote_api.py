from contextlib import contextmanager
import json
import multiprocessing
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from backend.kis import KisError, PaperClient, Settings
from backend.request_gate import file_lock, request_spacing


def _spacing_worker(path, ready, start):
    ready.put(("ready", 0))
    start.wait(10)
    with request_spacing(Path(path), interval=0.12):
        ready.put(("started", time.monotonic()))
        time.sleep(0.04)
        ready.put(("ended", time.monotonic()))


def _token_worker(path, ready, start):
    client = PaperClient(Settings("test-key", "test-secret"), Path(path))

    def issue(*args, **kwargs):
        ready.put(("issued", 0))
        time.sleep(0.08)
        return {"access_token": "test-token", "expires_in": 86400}, {}

    client._request = issue
    ready.put(("ready", 0))
    start.wait(10)
    ready.put(("token", client.token()))


def _hold_lock_worker(path, ready):
    with file_lock(Path(path)):
        ready.put("locked")
        time.sleep(30)


class QuoteApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.client = PaperClient(Settings("test-key", "test-secret"), self.root / "token.json")
        self.output = {"stck_prpr": "75000", "prdy_ctrt": "-1.5000",
                       "acml_vol": "123456", "acml_tr_pbmn": "9000000000",
                       "stck_shrn_iscd": "005930"}

    def quote(self, output):
        with patch.object(self.client, "_get", return_value=({"output": output}, {})):
            return self.client.quote("005930")

    def test_complete_current_quote_has_cumulative_values_without_invented_trade_time(self):
        output = {**self.output, "private": "private-secret", "d250_hgpr_date": "20250101",
                  "bstp_kor_isnm": "업종명", "iscd_stat_cls_code": "55"}
        result = self.quote(output)
        self.assertEqual(result, {"environment": "paper", "market": "KRX", "symbol": "005930",
                                  "price": "75000", "change_percent": "-1.5", "volume": "123456",
                                  "cumulative_turnover": "9000000000"})
        self.assertNotIn("private-secret", json.dumps(result))

    def test_current_quote_allows_zero_volume_and_turnover_and_absent_optional_symbol(self):
        output = {key: value for key, value in self.output.items() if key != "stck_shrn_iscd"}
        output.update(acml_vol="0", acml_tr_pbmn="0", prdy_ctrt="+0.00")
        self.assertEqual(self.quote(output)["volume"], "0")
        self.assertEqual(self.quote(output)["cumulative_turnover"], "0")
        self.assertEqual(self.quote(output)["change_percent"], "0")

    def test_required_numeric_fields_do_not_default_missing_or_malformed_values_to_zero(self):
        for field in ("stck_prpr", "prdy_ctrt", "acml_vol", "acml_tr_pbmn"):
            for value in (None, "", "NaN", "Infinity", "1e3", "private-secret", [], True, 123,
                          "1" * 41):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(KisError) as caught:
                        self.quote({**self.output, field: value})
                    self.assertNotIn("private-secret", str(caught.exception))
            with self.subTest(missing=field), self.assertRaises(KisError):
                self.quote({key: value for key, value in self.output.items() if key != field})

    def test_price_must_be_positive_and_cumulative_values_cannot_be_negative(self):
        for field, value in (("stck_prpr", "0"), ("stck_prpr", "-1"), ("acml_vol", "-1"),
                             ("acml_vol", "1.5"), ("acml_tr_pbmn", "-1")):
            with self.subTest(field=field, value=value), self.assertRaises(KisError):
                self.quote({**self.output, field: value})

    def test_mismatched_or_invalid_output_fails(self):
        for output in (None, [], {}, {**self.output, "stck_shrn_iscd": "000660"},
                       {**self.output, "stck_shrn_iscd": None}):
            with self.subTest(output=output), self.assertRaises(KisError):
                self.quote(output)

    def test_invalid_symbol_fails_without_auth_or_network(self):
        for symbol in (None, 5930, "5930", " 005930", "005930&x=1", "００５９３０"):
            with self.subTest(symbol=symbol), patch.object(self.client, "_get") as get:
                with self.assertRaises(KisError):
                    self.client.quote(symbol)
                get.assert_not_called()

    def test_network_gate_is_inside_existing_guard_and_released_on_error(self):
        events = []

        @contextmanager
        def guard():
            events.append("outer-enter")
            try:
                yield
            finally:
                events.append("outer-exit")

        @contextmanager
        def spacing(path):
            events.append("gate-enter")
            try:
                yield
            finally:
                events.append("gate-exit")

        self.client.request_guard = guard
        with patch("backend.kis.request_spacing", side_effect=spacing), \
                patch.object(self.client, "_http_request", side_effect=KisError("조회 실패")):
            with self.assertRaises(KisError):
                self.client._request("/test")
        self.assertEqual(events, ["outer-enter", "gate-enter", "gate-exit", "outer-exit"])

    def test_gate_errors_are_sanitized_and_do_not_send_request(self):
        with patch("backend.kis.request_spacing", side_effect=OSError("private-secret")), \
                patch.object(self.client, "_http_request") as request:
            with self.assertRaises(KisError) as caught:
                self.client._perform_request("/test")
            self.assertNotIn("private-secret", str(caught.exception))
            request.assert_not_called()
        with patch("backend.kis.file_lock", side_effect=TimeoutError("private-secret")), \
                patch.object(self.client, "_cached_or_issue_token") as issue:
            with self.assertRaises(KisError) as caught:
                self.client.token()
            self.assertNotIn("private-secret", str(caught.exception))
            issue.assert_not_called()


class RequestGateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.context = multiprocessing.get_context("spawn")

    def start(self, target, *args):
        process = self.context.Process(target=target, args=args)
        process.start()

        def cleanup():
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

        self.addCleanup(cleanup)
        return process

    def test_request_spacing_is_shared_between_independent_processes(self):
        queue, start = self.context.Queue(), self.context.Event()
        workers = [self.start(_spacing_worker, str(self.root / "request.lock"), queue, start)
                   for _ in range(2)]
        self.assertEqual([queue.get(timeout=10)[0] for _ in workers], ["ready", "ready"])
        start.set()
        events = [queue.get(timeout=10) for _ in range(4)]
        for process in workers:
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual([name for name, _ in events], ["started", "ended", "started", "ended"])
        self.assertGreaterEqual(events[2][1] - events[1][1], 0.10)

    def test_independent_clients_issue_one_token_then_recheck_shared_cache(self):
        queue, start = self.context.Queue(), self.context.Event()
        workers = [self.start(_token_worker, str(self.root / "token.json"), queue, start)
                   for _ in range(2)]
        self.assertEqual([queue.get(timeout=10)[0] for _ in workers], ["ready", "ready"])
        start.set()
        results = [queue.get(timeout=10) for _ in range(3)]
        for process in workers:
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(results.count(("issued", 0)), 1)
        self.assertEqual(results.count(("token", "test-token")), 2)

    def test_nonblocking_lock_detects_live_process_and_recovers_after_process_exit(self):
        path = self.root / "collector.lock"
        queue = self.context.Queue()
        worker = self.start(_hold_lock_worker, str(path), queue)
        self.assertEqual(queue.get(timeout=10), "locked")
        with self.assertRaises(BlockingIOError):
            with file_lock(path, blocking=False):
                self.fail("Acquired a held lock")
        worker.terminate()
        worker.join(timeout=5)
        with file_lock(path, blocking=False):
            self.assertTrue(path.exists())
        self.assertTrue(path.exists())

    def test_failed_request_preserves_spacing_and_lock_is_reusable(self):
        path = self.root / "request.lock"
        with self.assertRaises(RuntimeError):
            with request_spacing(path, interval=0.03):
                raise RuntimeError("request failure")
        ended = time.monotonic()
        with request_spacing(path, interval=0.03):
            self.assertGreaterEqual(time.monotonic() - ended, 0.025)

    def test_corrupt_request_state_recovers_with_a_safe_delay(self):
        path = self.root / "request.lock"
        path.write_bytes(b"\0NaN")
        started = time.monotonic()
        with request_spacing(path, interval=0.03):
            self.assertGreaterEqual(time.monotonic() - started, 0.025)


if __name__ == "__main__":
    unittest.main()
