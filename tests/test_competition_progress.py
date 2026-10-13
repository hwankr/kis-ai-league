from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.competition_progress import KisCompetitionProgress, calculate_kis_progress
from backend.dashboard import AccountDirectory
from backend.history import series_for_profile
from backend.kis import AccountProfile, KST, Settings


NOW = datetime(2026, 10, 19, 16, tzinfo=KST)


def trade(index=1, **changes):
    return {"order_date": "2026-10-12", "branch_id": "00001", "order_id": f"{index:010}",
            "symbol": "005930", "name": "시험 종목", "side": "buy", "quantity": "2",
            "price": "100", "amount": "200", "order_time": "09:10:00", **changes}


def accepted(row, **changes):
    return {**{key: row[key] for key in ("order_date", "branch_id", "order_id", "symbol", "side")},
            "fingerprint": "current", "quantity": int(row["quantity"]), "filled_quantity": 0,
            "status": "partial", "accepted_at": row["order_date"] + "T09:00:00+09:00", **changes}


def source(rows=None, **changes):
    return {"account": {"id": "competition", "name": "대회"}, "environment": "paper", "status": "ok",
            "start_date": "2026-10-12", "end_date": "2026-10-19", "trades": rows or [],
            "updated_at": "2026-10-19T16:00:00+09:00", "stale": False, "error": None, **changes}


class CalculationTests(unittest.TestCase):
    def calculate(self, snapshot, orders=(), now=NOW):
        return calculate_kis_progress(snapshot, list(orders), fingerprint="current", now=now)

    def test_exact_thresholds_include_manual_buys_sells_and_all_symbols(self):
        rows = [trade(i, order_date=f"2026-10-{12 + i:02}", symbol=f"{i:06}", amount="100000000",
                      side="sell" if i % 2 else "buy") for i in range(1, 6)]
        result = self.calculate(source(rows), [accepted(row) for row in rows])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coverage"], "complete")
        self.assertEqual([metric["value"] for metric in result["metrics"].values()], [5, "500000000", 5, 5])
        self.assertTrue(all(metric["met"] for metric in result["metrics"].values()))
        self.assertTrue(result["metrics"]["api_orders"]["lower_bound"])
        manual = self.calculate(source(rows))
        self.assertEqual(manual["metrics"]["gross_amount"]["value"], "500000000")
        self.assertEqual(manual["metrics"]["api_orders"]["value"], 0)

    def test_partial_fill_is_one_order_and_largest_cumulative_amount_wins(self):
        first = trade(quantity="1", amount="100.25")
        latest = trade(quantity="3", amount="300.75")
        for rows in ([first, latest, latest], [latest, first]):
            with self.subTest(rows=rows):
                result = self.calculate(source(rows), [accepted(latest), accepted(latest)])
                self.assertEqual(result["metrics"]["gross_amount"]["value"], "300.75")
                self.assertEqual(result["metrics"]["api_orders"]["value"], 1)

    def test_duplicate_identity_requires_consistent_symbol_side_and_cumulative_amount(self):
        for change in ({"symbol": "000660"}, {"side": "sell"}, {"amount": "201"},
                       {"quantity": "3", "amount": "100"}, {"quantity": "3"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.calculate(source([trade(), trade(**change)]))

    def test_order_identity_includes_date_and_branch(self):
        rows = [trade(), trade(order_date="2026-10-13"), trade(branch_id="00002")]
        result = self.calculate(source(rows), [accepted(row) for row in rows])
        self.assertEqual(result["metrics"]["gross_amount"]["value"], "600")
        self.assertEqual(result["metrics"]["trading_days"]["value"], 2)
        self.assertEqual(result["metrics"]["api_orders"]["value"], 3)

    def test_api_count_requires_matching_current_account_and_acknowledged_identity(self):
        row = trade()
        variations = [{"fingerprint": "old"}, {"accepted_at": None}, {"accepted_at": "2026-10-12T09:00:00"},
                      {"accepted_at": "2026-10-13T09:00:00+09:00"}, {"accepted_at": "2027-10-12T09:00:00+09:00"},
                      {"submission_skipped": True}, {"request_sent": False}, {"symbol": "000660"},
                      {"side": "sell"}, {"branch_id": "2"}, {"order_id": "2"}, {"quantity": 1},
                      {"quantity": True}, {"quantity": "2"}]
        for change in variations:
            with self.subTest(change=change):
                result = self.calculate(source([row]), [accepted(row, **change)])
                self.assertEqual(result["metrics"]["api_orders"]["value"], 0)
        # The fresh whole-account query proves the fill even before local reconciliation.
        result = self.calculate(source([row]), [accepted(row, status="submitted", filled_quantity=0)])
        self.assertEqual(result["metrics"]["api_orders"]["value"], 1)

    def test_unfilled_orders_and_orders_outside_contest_or_future_do_not_count(self):
        rows = [trade(1, quantity="0", amount="0"), trade(2, order_date="2026-10-11"),
                trade(3, order_date="2026-10-20"), trade(4)]
        result = self.calculate(source(rows), [accepted(row) for row in rows])
        self.assertEqual(result["metrics"]["gross_amount"]["value"], "200")
        self.assertEqual(result["metrics"]["api_orders"]["value"], 1)

    def test_unsynced_empty_data_is_unknown_but_successful_empty_data_is_zero(self):
        missing = self.calculate(source(status="error", updated_at=None))
        self.assertEqual(missing["coverage"], "none")
        self.assertFalse(missing["stale"])
        self.assertTrue(all(metric["value"] is None and metric["met"] is None
                            for metric in missing["metrics"].values()))
        empty = self.calculate(source())
        self.assertEqual(empty["status"], "ok")
        self.assertEqual(empty["metrics"]["gross_amount"]["value"], "0")

    def test_partial_local_coverage_retains_known_values_without_claiming_complete_query(self):
        result = self.calculate(source([trade()], status="error", updated_at=None))
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["stale"])
        self.assertEqual(result["metrics"]["gross_amount"]["value"], "200")

    def test_failed_refresh_retains_previous_full_sync_and_totals(self):
        result = self.calculate(source([trade()], status="error", stale=True, error="private-error"))
        self.assertEqual(result["coverage"], "complete")
        self.assertTrue(result["stale"])
        self.assertEqual(result["updated_at"], "2026-10-19T16:00:00+09:00")
        self.assertEqual(result["metrics"]["gross_amount"]["value"], "200")
        self.assertNotIn("private-error", json.dumps(result))

    def test_missing_or_broken_api_ledger_only_hides_api_metric(self):
        for orders in (None, {}):
            with self.subTest(orders=orders):
                result = calculate_kis_progress(source([trade()]), orders, fingerprint="current", now=NOW)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["metrics"]["gross_amount"]["value"], "200")
                self.assertIsNone(result["metrics"]["api_orders"]["value"])
                self.assertIsNone(result["metrics"]["api_orders"]["met"])

    def test_wrong_account_period_or_environment_is_rejected(self):
        for change in ({"account": {"id": "paper"}}, {"environment": "live"},
                       {"start_date": "2026-10-13"}, {"end_date": "2026-10-18"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.calculate(source([trade()], **change))

    def test_malformed_amount_cannot_produce_a_false_threshold(self):
        for amount in ("NaN", "Infinity", "-1", "broken", True):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.calculate(source([trade(amount=amount)]))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.profile = AccountProfile("competition", "대회", Settings("key", "secret", "12345678", "01"))
        self.accounts = Mock()
        self.accounts.trades.side_effect = lambda account, start, end: source([trade()], start_date=start, end_date=end)
        self.reader = Mock(return_value=[])
        self.profiles = Mock(side_effect=lambda: self.profile)
        self.service = KisCompetitionProgress(self.accounts, self.reader, now=lambda: NOW,
                                             profile_reader=self.profiles)

    def test_full_period_query_does_not_use_default_or_selected_account(self):
        self.service.snapshot()
        self.accounts.trades.assert_called_once_with("competition", "2026-10-12", "2026-10-19")

    def test_fresh_sync_uses_post_query_clock_when_request_crosses_daily_cutoff(self):
        current = [datetime.fromisoformat("2026-10-13T15:59:59+09:00")]
        self.service.now = lambda: current[0]
        def query(account, start, end):
            current[0] = datetime.fromisoformat("2026-10-13T16:00:02+09:00")
            return source([trade()], start_date=start, end_date=end, updated_at=current[0].isoformat())
        self.accounts.trades.side_effect = query
        result = self.service.snapshot()
        self.assertEqual(result["pace"]["status"], "behind")
        self.assertEqual(result["pace"]["completed_sessions"], 2)
        self.assertEqual(result["pace"]["as_of"], current[0].isoformat())
        self.assertEqual(self.accounts.trades.call_count, 1)

    def test_midnight_during_query_retries_with_new_contest_period(self):
        current = [datetime.fromisoformat("2026-10-13T23:59:59+09:00")]
        self.service.now = lambda: current[0]
        def query(account, start, end):
            current[0] = datetime.fromisoformat("2026-10-14T00:00:02+09:00")
            return source([trade()], start_date=start, end_date=end, updated_at=current[0].isoformat())
        self.accounts.trades.side_effect = query
        result = self.service.snapshot()
        self.assertEqual([call.args[2] for call in self.accounts.trades.call_args_list],
                         ["2026-10-13", "2026-10-14"])
        self.assertEqual(result["period"]["as_of"], "2026-10-14")
        self.assertEqual(result["pace"]["status"], "behind")
        self.assertEqual(result["pace"]["completed_sessions"], 2)

    def test_contest_start_during_read_retries_instead_of_reporting_prestart_zero(self):
        self.service.now = Mock(side_effect=[datetime.fromisoformat(value) for value in
            ("2026-10-11T23:59:59+09:00", "2026-10-12T00:00:01+09:00", "2026-10-12T00:00:02+09:00")])
        self.accounts.trades.side_effect = lambda account, start, end: source(
            [trade()], start_date=start, end_date=end, updated_at="2026-10-12T00:00:01+09:00")
        result = self.service.snapshot()
        self.accounts.trades.assert_called_once_with("competition", "2026-10-12", "2026-10-12")
        self.assertEqual(result["phase"], "active")
        self.assertEqual(result["metrics"]["gross_amount"]["value"], "200")

    def test_contest_end_during_query_retries_phase_with_same_clamped_period(self):
        current = [datetime.fromisoformat("2026-11-20T23:59:59+09:00")]
        self.service.now = lambda: current[0]
        def query(account, start, end):
            current[0] = datetime.fromisoformat("2026-11-21T00:00:02+09:00")
            return source([trade()], start_date=start, end_date=end, updated_at=current[0].isoformat())
        self.accounts.trades.side_effect = query
        result = self.service.snapshot()
        self.assertEqual(self.accounts.trades.call_count, 2)
        self.assertTrue(all(call.args[2] == "2026-11-20" for call in self.accounts.trades.call_args_list))
        self.assertEqual(result["phase"], "ended")
        self.assertEqual(result["pace"]["status"], "ended_shortfall")

    def test_start_boundary_uses_kst_and_avoids_invalid_prestart_broker_query(self):
        self.service.now = lambda: datetime.fromisoformat("2026-10-11T14:59:59+00:00")
        result = self.service.snapshot()
        self.assertEqual(result["phase"], "not_started")
        self.assertEqual(result["metrics"]["gross_amount"]["value"], "0")
        self.accounts.trades.assert_not_called()
        self.reader.assert_not_called()
        self.service.now = lambda: datetime.fromisoformat("2026-10-11T15:00:00+00:00")
        self.assertEqual(self.service.snapshot()["phase"], "active")
        self.accounts.trades.assert_called_once_with("competition", "2026-10-12", "2026-10-12")

    def test_contest_end_is_inclusive_and_postcontest_query_is_clamped(self):
        for instant, phase in (("2026-11-20T14:59:59+00:00", "active"),
                               ("2026-11-20T15:00:00+00:00", "ended")):
            with self.subTest(instant=instant):
                self.accounts.trades.reset_mock()
                self.service.now = lambda: datetime.fromisoformat(instant)
                result = self.service.snapshot()
                self.assertEqual(result["phase"], phase)
                self.accounts.trades.assert_called_once_with("competition", "2026-10-12", "2026-11-20")

    def test_account_change_during_trade_query_retries_with_new_ledger_scope(self):
        old = self.profile
        new = AccountProfile("competition", "새 대회", Settings("new-key", "new-secret", "87654321", "01"))
        self.profiles.side_effect = [old, new, new, new]
        self.reader.return_value = [accepted(trade(), fingerprint=series_for_profile(old).fingerprint)]
        result = self.service.snapshot()
        self.assertEqual(self.accounts.trades.call_count, 2)
        self.assertEqual(result["metrics"]["api_orders"]["value"], 0)

    def test_repeated_account_changes_never_return_mixed_data(self):
        other = AccountProfile("competition", "새 대회", Settings("new-key", "new-secret", "87654321", "01"))
        self.profiles.side_effect = [self.profile, other] * 3
        result = self.service.snapshot()
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["metrics"]["gross_amount"]["value"])

    def test_ledger_failure_keeps_broker_totals_and_does_not_expose_exception(self):
        self.reader.side_effect = RuntimeError("private-ledger-details")
        result = self.service.snapshot()
        self.assertEqual(result["metrics"]["gross_amount"]["value"], "200")
        self.assertIsNone(result["metrics"]["api_orders"]["value"])
        self.assertNotIn("private-ledger-details", json.dumps(result))

    def test_missing_account_or_unconfigured_profile_does_not_query_broker(self):
        self.profile.settings.app_secret = ""
        result = self.service.snapshot()
        self.assertEqual(result["status"], "error")
        self.accounts.trades.assert_not_called()
        self.profiles.side_effect = RuntimeError("secret")
        self.assertNotIn("secret", json.dumps(self.service.snapshot()))

    def test_real_directory_reloads_current_account_and_preserves_previous_sync_on_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            config = ("[accounts.competition]\nname='대회'\napp_key='fake-key'\n"
                      "app_secret='fake-secret'\naccount='12345678'\nproduct_code='01'\n")
            path.write_text(config, encoding="utf-8")
            client = Mock()
            client.executions.return_value = {"executions": [trade()]}
            clock = Mock(return_value=10)
            accounts = AccountDirectory(path, Mock(return_value=client), clock, sleep=Mock(),
                                        history_path=Path(folder) / "history.sqlite3")
            service = KisCompetitionProgress(accounts, lambda: [], now=lambda: NOW)
            with patch("backend.kis._execution_today", return_value=NOW.date()):
                first = service.snapshot()
                self.assertEqual(first["metrics"]["gross_amount"]["value"], "200")
                clock.return_value = 20
                client.executions.side_effect = RuntimeError("offline")
                stale = service.snapshot()
                self.assertTrue(stale["stale"])
                self.assertEqual(stale["updated_at"], first["updated_at"])
                path.write_text(config.replace("12345678", "87654321"), encoding="utf-8")
                changed = service.snapshot()
                self.assertFalse(changed["stale"])
                self.assertIsNone(changed["metrics"]["gross_amount"]["value"])


if __name__ == "__main__":
    unittest.main()
