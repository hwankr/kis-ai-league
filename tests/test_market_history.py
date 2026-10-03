from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend.market_history import MarketHistory


class MarketHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "private" / "market-history.sqlite3"
        self.store = MarketHistory(self.path)
        self.quote = {"symbol": "005930", "environment": "paper", "market": "KRX",
                      "price": "60100.125", "change_percent": "-1.25", "volume": "123456789",
                      "cumulative_turnover": "9007199254740993.12"}
        self.time = "2026-10-03T09:00:00.123456+09:00"
        self.utc = "2026-10-03T00:00:00.123456+00:00"
        self.later = "2026-10-03T09:01:00+09:00"

    def saved(self):
        return self.store.read(["005930"])["quotes"][0]

    def test_persistence_decimal_precision_timezone_and_only_normalized_fields(self):
        quote = {**self.quote, "app_key": "secret-app-key", "app_secret": "secret-app-secret",
                 "account": "12345678", "raw": {"private": "raw-response"}}
        self.store.record_quote(quote, self.time)
        restarted = MarketHistory(self.path)
        saved = restarted.read(["005930"])
        self.assertEqual(saved, {"quotes": [{**self.quote, "observed_at": self.utc,
                                             "last_attempt_at": self.utc, "error": None,
                                             "total_count": 1}], "total_count": 1})
        with closing(sqlite3.connect(self.path)) as connection:
            dump = "\n".join(connection.iterdump())
        for private in ("secret-app-key", "secret-app-secret", "12345678'", "raw-response"):
            self.assertNotIn(private, dump)
        self.assertNotIn("observation_id", json.dumps(saved))

    def test_retry_same_observation_is_idempotent_equal_new_polls_are_kept(self):
        self.store.record_quote(self.quote, self.time, "poll-1")
        self.store.record_quote(self.quote, self.time, "poll-1")
        self.store.record_quote(self.quote, self.time, "poll-2")
        self.assertEqual(self.saved()["total_count"], 2)
        self.store.record_quote(self.quote, self.time)
        self.assertEqual(self.saved()["total_count"], 3)

    def test_retry_conflicting_observation_cannot_mutate_or_silently_discard(self):
        self.store.record_quote(self.quote, self.time, "poll-1")
        for quote, timestamp in (({**self.quote, "price": "999"}, self.time), (self.quote, self.later)):
            with self.subTest(quote=quote, timestamp=timestamp), self.assertRaises(ValueError):
                self.store.record_quote(quote, timestamp, "poll-1")
        self.assertEqual(self.saved()["price"], self.quote["price"])
        self.assertEqual(self.saved()["total_count"], 1)

    def test_source_and_symbol_isolation_do_not_depend_on_account_or_key(self):
        self.store.record_quote(self.quote, self.time, "poll-1")
        self.store.record_quote({**self.quote, "environment": "live", "price": "70000"}, self.time, "poll-1")
        self.store.record_quote({**self.quote, "market": "NXT", "price": "70001"}, self.time, "poll-1")
        self.store.record_quote({**self.quote, "symbol": "000660", "price": "70002"}, self.time, "poll-1")
        self.assertEqual(self.saved()["total_count"], 1)
        self.assertEqual(self.store.read(["005930"], environment="live")["quotes"][0]["price"], "70000")
        self.assertEqual(self.store.read(["005930"], market="NXT")["quotes"][0]["price"], "70001")
        self.assertEqual(self.store.read(["000660"])["quotes"][0]["price"], "70002")

    def test_failure_keeps_last_quote_and_success_clears_error_atomically(self):
        self.store.record_quote(self.quote, self.time)
        self.store.record_attempt("005930", self.later, "시세 조회에 실패했습니다.")
        saved = self.saved()
        self.assertEqual(saved["price"], self.quote["price"])
        self.assertEqual(saved["observed_at"], self.utc)
        self.assertEqual(saved["last_attempt_at"], "2026-10-03T00:01:00.000000+00:00")
        self.assertEqual(saved["error"], "시세 조회에 실패했습니다.")
        self.assertEqual(saved["total_count"], 1)
        self.store.record_quote(self.quote, "2026-10-03T09:02:00+09:00")
        self.assertIsNone(self.saved()["error"])
        self.assertEqual(self.saved()["total_count"], 2)

    def test_failure_before_any_success_is_distinct_from_no_attempt(self):
        self.store.record_attempt("005930", self.time, "시세 조회에 실패했습니다.")
        result = self.store.read(["005930", "000660"])
        self.assertEqual(result["total_count"], 0)
        for quote in result["quotes"]:
            self.assertIsNone(quote["price"])
            self.assertIsNone(quote["observed_at"])
        self.assertEqual(result["quotes"][0]["error"], "시세 조회에 실패했습니다.")
        self.assertIsNone(result["quotes"][1]["last_attempt_at"])
        self.assertIsNone(result["quotes"][1]["error"])

    def test_older_failure_success_and_retry_cannot_regress_latest_attempt(self):
        self.store.record_quote(self.quote, self.time, "poll-1")
        self.store.record_attempt("005930", self.later, "시세 조회에 실패했습니다.")
        self.store.record_quote(self.quote, self.time, "poll-1")
        self.store.record_quote(self.quote, self.time, "late-response")
        self.store.record_attempt("005930", self.time, "이전 요청 오류")
        self.assertEqual(self.saved()["error"], "시세 조회에 실패했습니다.")
        self.store.record_quote(self.quote, "2026-10-03T09:02:00+09:00")
        self.store.record_attempt("005930", self.later, "늦게 도착한 오류")
        self.assertIsNone(self.saved()["error"])

    def test_latest_quote_follows_observed_time_instead_of_insertion_order(self):
        self.store.record_quote({**self.quote, "price": "70000"}, self.later)
        self.store.record_quote(self.quote, self.time)
        self.assertEqual(self.saved()["price"], "70000")
        self.assertEqual(self.saved()["total_count"], 2)

    def test_invalid_quote_never_appends_or_changes_last_attempt(self):
        self.store.record_quote(self.quote, self.time)
        before = self.saved()
        invalid = [(field, None) for field in ("symbol", "price", "change_percent", "volume", "cumulative_turnover")]
        invalid += [("symbol", "5930"), ("symbol", "１２３４５６"), ("price", "0"), ("price", "-1"),
                    ("price", "NaN"), ("price", "Infinity"), ("price", "1e999999"), ("price", 60100),
                    ("price", "6_100"), ("change_percent", "NaN"), ("volume", "1.5"),
                    ("volume", "-1"), ("cumulative_turnover", "-1"), ("market", "unknown")]
        for field, value in invalid:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.store.record_quote({**self.quote, field: value}, self.later)
        self.assertEqual(self.saved(), before)

    def test_naive_or_missing_timestamp_is_rejected_before_writing(self):
        for timestamp in ("2026-10-03T09:00:00", None, "bad"):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                self.store.record_quote(self.quote, timestamp)
        self.assertEqual(self.saved()["total_count"], 0)

    def test_quote_and_attempt_commit_roll_back_together_on_storage_error(self):
        self.store.record_quote(self.quote, self.time)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""CREATE TRIGGER deny_attempt BEFORE INSERT ON market_attempts
                                  BEGIN SELECT RAISE(ABORT, 'test storage failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record_quote({**self.quote, "price": "70000"}, self.later)
        self.assertEqual(self.saved()["total_count"], 1)
        self.assertEqual(self.saved()["price"], self.quote["price"])

    def test_all_requested_symbols_return_in_order_including_missing_and_duplicates(self):
        symbols = [f"{index:06d}" for index in range(250)]
        self.store.record_quote({**self.quote, "symbol": symbols[-1]}, self.time)
        result = self.store.read([*symbols, symbols[-1]])
        self.assertEqual([quote["symbol"] for quote in result["quotes"]], symbols)
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["quotes"][-1]["price"], self.quote["price"])

    def test_multiple_store_instances_concurrent_writes_and_reads(self):
        stores = [MarketHistory(self.path) for _ in range(4)]
        self.store.read(["005930"])
        def work(index):
            store = stores[index % len(stores)]
            store.record_quote(self.quote, self.time, f"poll-{index // 2}")
            result = store.read(["005930"])
            self.assertGreaterEqual(result["total_count"], 1)
            self.assertEqual(result["quotes"][0]["total_count"], result["total_count"])
            self.assertEqual(result["quotes"][0]["last_attempt_at"], self.utc)
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(work, range(60)))
        self.assertEqual(self.saved()["total_count"], 30)

    def collector(self, state="running", heartbeat=None, **overrides):
        options = {"collector_id": "run-1", "interval_seconds": 60, "symbols": ["005930", "000660"]}
        self.store.set_collector(state, heartbeat or self.time, self.later, **(options | overrides))

    def test_collector_never_started_live_stale_future_and_stopped_states(self):
        self.assertEqual(self.store.get_collector(self.time)["state"], "not_started")
        self.collector()
        live = self.store.get_collector("2026-10-03T09:00:20+09:00")
        self.assertEqual(live["state"], "running")
        self.assertEqual(live["interval_seconds"], 60)
        self.assertEqual(live["symbols"], ["005930", "000660"])
        self.assertEqual(live["next_run_at"], "2026-10-03T00:01:00.000000+00:00")
        self.assertEqual(live["collector_id"], "run-1")
        stale = self.store.get_collector("2026-10-03T09:00:21+09:00")
        self.assertEqual(stale["state"], "stale")
        self.assertIsNone(stale["next_run_at"])
        self.assertEqual(self.store.get_collector("2026-10-03T08:59:50+09:00")["state"], "stale")
        self.collector("stopped", self.later)
        self.assertEqual(self.store.get_collector(self.later)["state"], "stopped")
        self.assertIsNone(self.store.get_collector(self.later)["next_run_at"])

    def test_collector_persists_restart_and_ignores_old_heartbeat(self):
        self.collector(heartbeat=self.later, error="설정을 확인하세요.")
        self.collector("stopped")
        restarted = MarketHistory(self.path).get_collector(self.later)
        self.assertEqual(restarted["state"], "running")
        self.assertEqual(restarted["error"], "설정을 확인하세요.")
        self.assertEqual(restarted["symbols"], ["005930", "000660"])

    def test_invalid_attempt_and_collector_inputs_do_not_change_status(self):
        for error in (None, "", "a" * 301, {"raw": "secret"}):
            with self.subTest(error=error), self.assertRaises(ValueError):
                self.store.record_attempt("005930", self.time, error)
        for interval in (0, -1, 1.5, True):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                self.collector(interval_seconds=interval)
        self.assertIsNone(self.saved()["last_attempt_at"])
        self.assertEqual(self.store.get_collector(self.time)["state"], "not_started")


if __name__ == "__main__":
    unittest.main()
