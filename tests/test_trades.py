from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend.history import AccountHistory, HistorySeries, series_for_profile
from backend.kis import AccountProfile, Settings
from backend.trades import ExecutionHistory


class ExecutionHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "private" / "account-history.sqlite3"
        self.store = ExecutionHistory(self.path)
        self.profile = AccountProfile("paper", "모의 계좌", Settings(
            "fake-app-key", "fake-app-secret", "12345678", "01"))
        self.series = series_for_profile(self.profile)
        self.start, self.end = "2026-10-01", "2026-10-03"
        self.timestamp = "2026-10-03T09:00:00.123456+09:00"
        self.saved_timestamp = "2026-10-03T00:00:00.123456+00:00"
        self.row = {"order_date": "2026-10-02", "order_id": "0000000001",
                    "branch_id": "00001", "symbol": "005930", "name": "시험 종목",
                    "side": "buy", "quantity": "2", "price": "60100.125",
                    "amount": "120200.25", "order_time": "09:01:02"}

    def record(self, rows, timestamp=None):
        self.store.record(self.series, self.start, self.end,
                          timestamp or self.timestamp, rows)

    def read(self):
        return self.store.read(self.series, self.start, self.end)

    def test_repeated_sync_replaces_cumulative_partial_fills_without_double_counting(self):
        self.record([self.row])
        self.record([self.row])
        self.assertEqual(self.read()["total_count"], 1)
        complete = {**self.row, "quantity": "5", "price": "60200.25", "amount": "301001.25"}
        self.record([complete], "2026-10-03T09:05:00+09:00")
        self.assertEqual(self.read(), {"trades": [complete], "total_count": 1,
                                      "updated_at": "2026-10-03T00:05:00.000000+00:00"})

    def test_order_identity_includes_date_and_branch(self):
        rows = [self.row, {**self.row, "order_date": "2026-10-03"},
                {**self.row, "branch_id": "00002"}]
        self.record(rows)
        self.assertEqual(self.read()["trades"], [rows[1], rows[2], rows[0]])

    def test_missing_rows_are_retained_after_later_empty_or_partial_sync(self):
        second = {**self.row, "order_id": "0000000002", "side": "sell"}
        self.record([self.row, second])
        self.record([self.row])
        self.record([], "2026-10-03T09:05:00+09:00")
        self.assertEqual(self.read()["trades"], [second, self.row])
        self.assertEqual(self.read()["updated_at"], "2026-10-03T00:05:00.000000+00:00")

    def test_overlapping_sync_cannot_regress_quantity_or_change_order_identity(self):
        self.record([self.row])
        for field, value in (("quantity", "1"), ("symbol", "000660"), ("side", "sell")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.record(self.series, "2026-10-02", self.end,
                                  "2026-10-03T09:05:00+09:00",
                                  [{**self.row, "order_id": "new"}, {**self.row, field: value}])
            self.assertEqual(self.read(), {"trades": [self.row], "total_count": 1,
                                          "updated_at": self.saved_timestamp})
            self.assertIsNone(self.store.read(self.series, "2026-10-02", self.end)["updated_at"])

    def test_equal_quantity_can_replace_revised_average_price_and_amount(self):
        self.record([self.row])
        revised = {**self.row, "quantity": "2.0", "price": "60100.5", "amount": "120201"}
        self.record([revised], "2026-10-03T09:05:00+09:00")
        self.assertEqual(self.read(), {"trades": [revised], "total_count": 1,
                                      "updated_at": "2026-10-03T00:05:00.000000+00:00"})

    def test_conflicting_duplicate_rows_in_one_sync_leave_no_saved_rows_or_success(self):
        with self.assertRaises(ValueError):
            self.record([self.row, {**self.row, "quantity": "1"}])
        self.assertEqual(self.read(), {"trades": [], "total_count": 0, "updated_at": None})

    def test_empty_success_is_persisted_and_distinct_from_never_synced(self):
        self.assertEqual(self.read(), {"trades": [], "total_count": 0, "updated_at": None})
        self.record([])
        restarted = ExecutionHistory(self.path)
        self.assertEqual(restarted.read(self.series, self.start, self.end),
                         {"trades": [], "total_count": 0, "updated_at": self.saved_timestamp})
        self.assertIsNone(restarted.read(self.series, self.start, self.start)["updated_at"])

    def test_reopen_preserves_exact_decimal_strings_but_drops_raw_fields(self):
        self.record([{**self.row, "raw_response": {"app_key": "DO-NOT-SAVE"}}])
        restarted = ExecutionHistory(self.path)
        self.assertEqual(restarted.read(self.series, self.start, self.end),
                         {"trades": [self.row], "total_count": 1, "updated_at": self.saved_timestamp})
        with closing(sqlite3.connect(self.path)) as connection:
            dump = "\n".join(connection.iterdump())
        for private in ("DO-NOT-SAVE", "fake-app-key", "fake-app-secret", "12345678"):
            self.assertNotIn(private, dump)
        self.assertNotIn("fingerprint", str(self.read()))

    def test_profile_environment_and_fingerprint_each_isolate_rows_and_sync_status(self):
        self.record([self.row])
        series_values = [HistorySeries("competition", "paper", self.series.fingerprint),
                         HistorySeries("paper", "other-environment", self.series.fingerprint),
                         HistorySeries("paper", "paper", "other-fingerprint")]
        for series in series_values:
            with self.subTest(series=series):
                self.assertEqual(self.store.read(series, self.start, self.end),
                                 {"trades": [], "total_count": 0, "updated_at": None})

    def test_key_account_product_change_isolates_but_profile_rename_keeps_history(self):
        self.record([self.row])
        for field in ("app_key", "app_secret", "account", "product_code"):
            changed = replace(self.profile, settings=replace(self.profile.settings, **{field: "changed"}))
            with self.subTest(field=field):
                result = self.store.read(series_for_profile(changed), self.start, self.end)
                self.assertEqual(result, {"trades": [], "total_count": 0, "updated_at": None})
        renamed = replace(self.profile, name="이름 변경")
        self.assertEqual(self.store.read(series_for_profile(renamed), self.start, self.end), self.read())

    def test_range_is_inclusive_sorted_newest_and_sync_timestamp_matches_exact_range(self):
        rows = [{**self.row, "order_date": self.start, "order_time": None},
                self.row, {**self.row, "order_date": self.end, "order_time": "15:20:00"},
                {**self.row, "order_date": self.end, "order_id": "0000000002", "order_time": "09:05:00"}]
        self.record(rows)
        self.assertEqual(self.read()["trades"], [rows[2], rows[3], rows[1], rows[0]])
        result = self.store.read(self.series, "2026-10-02", "2026-10-02")
        self.assertEqual(result, {"trades": [self.row], "total_count": 1, "updated_at": None})

    def test_sql_failure_rolls_back_replacements_inserts_and_sync_timestamp(self):
        self.record([self.row])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""CREATE TRIGGER reject_order BEFORE INSERT ON executions
                WHEN NEW.order_id = 'reject' BEGIN SELECT RAISE(FAIL, 'simulated failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.record([{**self.row, "quantity": "3"},
                         {**self.row, "order_id": "new"},
                         {**self.row, "order_id": "reject"}], "2026-10-03T09:05:00+09:00")
        self.assertEqual(self.read(), {"trades": [self.row], "total_count": 1,
                                      "updated_at": self.saved_timestamp})

    def test_invalid_later_row_cannot_partially_save_a_sync(self):
        self.record([self.row])
        invalid = {**self.row, "order_id": "other", "quantity": "NaN"}
        with self.assertRaises(ValueError):
            self.record([{**self.row, "quantity": "3"}, invalid], "2026-10-03T09:05:00+09:00")
        self.assertEqual(self.read(), {"trades": [self.row], "total_count": 1,
                                      "updated_at": self.saved_timestamp})

    def test_rejects_bad_range_timestamp_or_normalized_row(self):
        for start, end in (("20261001", self.end), (self.end, self.start), ("2026-02-30", self.end)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.store.record(self.series, start, end, self.timestamp, [])
        with self.assertRaises(ValueError):
            self.record([], "2026-10-03T09:00:00")
        for field, value in (("order_date", "2026-09-30"), ("order_id", ""),
                             ("branch_id", None), ("symbol", ""), ("name", None),
                             ("side", "01"), ("quantity", "0"), ("quantity", 1),
                             ("price", "Infinity"), ("amount", "-1"), ("order_time", "090102")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.record([{**self.row, field: value}])
        self.assertEqual(self.read(), {"trades": [], "total_count": 0, "updated_at": None})

    def test_read_does_not_silently_truncate_a_large_range(self):
        rows = [{**self.row, "order_id": f"{index:010}"} for index in range(2003)]
        self.record(rows)
        result = self.read()
        self.assertEqual(result["total_count"], 2003)
        self.assertEqual(len(result["trades"]), 2003)

    def test_existing_balance_history_remains_usable_in_same_database(self):
        account_history = AccountHistory(self.path)
        snapshot = {"summary": {"total_value": "10000000", "cash": "8000000"}, "holdings": []}
        account_history.record(self.series, self.timestamp, snapshot, "observation-1")
        previous = account_history.read(self.series)
        self.record([self.row])
        self.assertEqual(account_history.read(self.series), previous)
        account_history.record(self.series, self.timestamp, snapshot, "observation-2")
        self.assertEqual(self.read()["trades"], [self.row])

    def test_multiple_instances_can_safely_write_distinct_orders(self):
        stores = [ExecutionHistory(self.path) for _ in range(4)]

        def write(index):
            stores[index % len(stores)].record(self.series, self.start, self.end, self.timestamp,
                                               [{**self.row, "order_id": str(index // 2)}])

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(write, range(40)))
        self.assertEqual(self.read()["total_count"], 20)


if __name__ == "__main__":
    unittest.main()
