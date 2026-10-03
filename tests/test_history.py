from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend.history import AccountHistory, HistorySeries, MAX_HISTORY_POINTS, series_for_profile
from backend.kis import AccountProfile, Settings


class HistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "private" / "account-history.sqlite3"
        self.store = AccountHistory(self.path)
        self.profile = AccountProfile("paper", "모의 계좌", Settings(
            "fake-app-key", "fake-app-secret", "12345678", "01"))
        self.series = series_for_profile(self.profile)
        self.snapshot = {"summary": {"total_value": "10000000.0001", "cash": "8000000.02"},
                         "holdings": [{"symbol": "005930", "quantity": "20", "name": "시험 종목"}]}
        self.timestamp = "2026-10-03T09:00:00.123456+09:00"

    def test_persistence_precision_timezone_snapshot_and_private_identity(self):
        self.store.record(self.series, self.timestamp, self.snapshot, "query-1")
        restarted = AccountHistory(self.path)
        result = restarted.read(self.series)
        self.assertEqual(result, {"points": [{"observed_at": "2026-10-03T00:00:00.123456+00:00",
                                             "total_value": "10000000.0001", "cash": "8000000.02"}],
                                  "total_count": 1})
        with closing(sqlite3.connect(self.path)) as connection:
            saved = connection.execute("SELECT snapshot_json FROM observations").fetchone()[0]
            dump = "\n".join(connection.iterdump())
        self.assertEqual(json.loads(saved), self.snapshot)
        for private in ("fake-app-key", "fake-app-secret", "12345678"):
            self.assertNotIn(private, dump)
        self.assertNotIn(self.series.fingerprint, json.dumps(result))

    def test_retrying_same_observation_is_idempotent_but_equal_new_queries_are_kept(self):
        for _ in range(2):
            self.store.record(self.series, self.timestamp, self.snapshot, "query-1")
        self.store.record(self.series, self.timestamp, self.snapshot, "query-2")
        self.assertEqual(self.store.read(self.series)["total_count"], 2)

    def test_profile_environment_and_fingerprint_each_isolate_history(self):
        self.store.record(self.series, self.timestamp, self.snapshot)
        other_series = [HistorySeries("competition", "paper", self.series.fingerprint),
                        HistorySeries("paper", "different-environment", self.series.fingerprint),
                        HistorySeries("paper", "paper", "different-fingerprint")]
        for series in other_series:
            with self.subTest(series=series):
                self.assertEqual(self.store.read(series), {"points": [], "total_count": 0})

    def test_missing_values_stay_missing_and_naive_time_is_rejected(self):
        self.snapshot["summary"] = {"cash": None, "total_value": None}
        self.store.record(self.series, self.timestamp, self.snapshot)
        result = self.store.read(self.series)["points"][0]
        self.assertIsNone(result["total_value"])
        self.assertIsNone(result["cash"])
        with self.assertRaises(ValueError):
            self.store.record(self.series, "2026-10-03T00:00:00", self.snapshot)
        self.assertEqual(self.store.read(self.series)["total_count"], 1)

    def test_multiple_store_instances_safely_write_concurrently(self):
        stores = [AccountHistory(self.path) for _ in range(4)]
        def write(index):
            stores[index % len(stores)].record(self.series, self.timestamp, self.snapshot,
                                               f"query-{index // 2}")
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(write, range(40)))
        self.assertEqual(self.store.read(self.series)["total_count"], 20)

    def test_latest_2000_points_are_ascending_without_deleting_full_history(self):
        self.store.read(self.series)
        start = datetime(2026, 10, 3, tzinfo=timezone.utc)
        rows = [(f"q-{index}", self.series.profile_id, self.series.environment,
                 self.series.fingerprint, (start + timedelta(seconds=index)).isoformat(timespec="microseconds"),
                 str(index), "0", json.dumps(self.snapshot))
                for index in reversed(range(MAX_HISTORY_POINTS + 3))]
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executemany("""INSERT INTO observations
                (observation_id, profile_id, environment, fingerprint, observed_at,
                 total_value, cash, snapshot_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", rows)
        result = self.store.read(self.series)
        self.assertEqual(result["total_count"], MAX_HISTORY_POINTS + 3)
        self.assertEqual(len(result["points"]), MAX_HISTORY_POINTS)
        self.assertEqual([point["total_value"] for point in result["points"]],
                         [str(index) for index in range(3, MAX_HISTORY_POINTS + 3)])


if __name__ == "__main__":
    unittest.main()
