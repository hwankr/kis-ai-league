from datetime import datetime
from decimal import Decimal
import unittest

from backend.competition_pacing import MONEY_FIELDS, calculate_turnover_pacing
from backend.competition_progress import calculate_kis_progress
from backend.kis import KST


class TurnoverPacingTests(unittest.TestCase):
    def pace(self, instant="2026-10-13T16:43:00+09:00", amounts=None, **changes):
        return calculate_turnover_pacing(
            now=datetime.fromisoformat(instant), start_date="2026-10-12", end_date="2026-11-20",
            daily_amounts=amounts or {}, reliable=changes.pop("reliable", True), **changes)

    def test_actual_account_arithmetic_uses_both_completed_days(self):
        result = self.pace(amounts={"2026-10-12": "4767100"})
        self.assertEqual(result["calendar_basis"], "weekday_estimate")
        self.assertEqual((result["total_sessions"], result["completed_sessions"], result["remaining_sessions"]),
                         (30, 2, 28))
        self.assertEqual(result["status"], "behind")
        self.assertEqual(result["gross_amount"], "4767100")
        self.assertEqual(result["remaining_gross_amount"], "495232900")
        self.assertEqual(result["expected_gross_amount"], "33333333")
        self.assertEqual(result["shortfall_gross_amount"], "28566234")
        self.assertEqual(result["required_daily_gross_amount"], "17686890")
        self.assertEqual(result["next_5_required_gross_amount"], "88434447")
        self.assertEqual(result["recent_5_sessions"], 2)
        self.assertEqual(result["recent_5_gross_amount"], "4767100")
        self.assertEqual(result["projected_final_gross_amount"], "71506500")

    def test_intraday_fills_reduce_remaining_but_not_observed_pace(self):
        result = self.pace("2026-10-13T15:59:59+09:00",
                           {"2026-10-12": "10000000", "2026-10-13": "20000000"})
        self.assertEqual((result["completed_sessions"], result["remaining_sessions"]), (1, 29))
        self.assertEqual(result["gross_amount"], "30000000")
        self.assertEqual(result["remaining_gross_amount"], "470000000")
        self.assertEqual(result["recent_5_gross_amount"], "10000000")
        self.assertEqual(result["projected_final_gross_amount"], "300000000")
        closed = self.pace("2026-10-13T16:00:00+09:00",
                           {"2026-10-12": "10000000", "2026-10-13": "20000000"})
        self.assertEqual(closed["completed_sessions"], 2)
        self.assertEqual(closed["projected_final_gross_amount"], "450000000")

    def test_recent_weekdays_include_zero_days_and_exclude_older_fills(self):
        result = self.pace("2026-10-20T16:00:00+09:00", {
            "2026-10-12": "90000000", "2026-10-13": "10000000", "2026-10-14": "2000000",
            "2026-10-19": "3000000"})
        self.assertEqual(result["completed_sessions"], 7)
        self.assertEqual(result["recent_5_sessions"], 5)
        self.assertEqual(result["recent_5_gross_amount"], "5000000")

    def test_weekend_does_not_consume_a_session(self):
        saturday = self.pace("2026-10-17T18:00:00+09:00")
        sunday = self.pace("2026-10-18T18:00:00+09:00")
        self.assertEqual((saturday["completed_sessions"], saturday["remaining_sessions"]), (5, 25))
        self.assertEqual(saturday["completed_sessions"], sunday["completed_sessions"])

    def test_unreliable_source_never_returns_money_estimates(self):
        result = self.pace(amounts={"2026-10-12": "500000000"}, reliable=False)
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(all(result[field] is None for field in MONEY_FIELDS))

    def test_query_before_completed_session_cutoff_is_not_a_final_total(self):
        for updated in ("2026-10-13T15:59:59+09:00", "2026-10-12T16:00:00+09:00",
                        "2026-10-13T16:44:00+09:00", "2026-10-13T16:43:00", "invalid"):
            with self.subTest(updated=updated):
                result = self.pace(source_updated_at=updated)
                self.assertEqual(result["status"], "unavailable")
        self.assertEqual(self.pace(source_updated_at="2026-10-13T16:00:00+09:00")["status"], "behind")
        self.assertEqual(self.pace("2026-10-18T18:00:00+09:00",
                                  source_updated_at="2026-10-16T16:00:00+09:00")["status"], "behind")

    def test_precontest_has_zero_elapsed_and_no_projected_pace(self):
        result = self.pace("2026-10-11T23:59:59+09:00")
        self.assertEqual(result["status"], "not_started")
        self.assertEqual(result["completed_sessions"], 0)
        self.assertEqual(result["remaining_sessions"], 30)
        self.assertEqual(result["required_daily_gross_amount"], "16666667")
        self.assertIsNone(result["projected_final_gross_amount"])

    def test_deadline_has_no_division_by_zero_or_fictitious_trading_days(self):
        for instant in ("2026-11-20T16:00:00+09:00", "2026-12-01T12:00:00+09:00"):
            with self.subTest(instant=instant):
                result = self.pace(instant, {"2026-10-12": "100000000"})
                self.assertEqual(result["status"], "ended_shortfall")
                self.assertEqual(result["remaining_sessions"], 0)
                self.assertEqual(result["next_5_sessions"], 0)
                self.assertIsNone(result["required_daily_gross_amount"])
                self.assertIsNone(result["next_5_required_gross_amount"])
        final_morning = self.pace("2026-11-20T09:00:00+09:00")
        self.assertEqual(final_morning["next_5_sessions"], 1)
        self.assertEqual(final_morning["next_5_required_gross_amount"], "500000000")

    def test_met_target_never_requests_negative_turnover(self):
        for instant in ("2026-10-13T16:00:00+09:00", "2026-11-21T09:00:00+09:00"):
            result = self.pace(instant, {"2026-10-12": "501000000"})
            self.assertEqual(result["status"], "met")
            self.assertEqual(result["remaining_gross_amount"], "0")
            self.assertEqual(result["shortfall_gross_amount"], "0")

    def test_outside_period_and_future_amounts_do_not_reduce_requirement(self):
        result = self.pace(amounts={"2026-10-11": "500000000", "2026-10-14": "500000000",
                                    "2026-11-21": "500000000"})
        self.assertEqual(result["gross_amount"], "0")
        self.assertEqual(result["remaining_gross_amount"], "500000000")

    def test_utc_clock_uses_korean_completion_cutoff(self):
        before = self.pace("2026-10-13T06:59:59+00:00")
        after = self.pace("2026-10-13T07:00:00+00:00")
        self.assertEqual((before["completed_sessions"], after["completed_sessions"]), (1, 2))
        with self.assertRaises(ValueError):
            self.pace("2026-10-13T16:00:00")

    def test_decimal_rounding_does_not_understate_required_amount(self):
        result = self.pace(amounts={"2026-10-12": Decimal("4767100.25")})
        self.assertEqual(result["remaining_gross_amount"], "495232900")
        self.assertGreaterEqual(Decimal(result["required_daily_gross_amount"]) * 28,
                                Decimal("500000000") - Decimal("4767100.25"))


class ProgressPacingIntegrationTests(unittest.TestCase):
    def progress(self, **changes):
        row = {"order_date": "2026-10-12", "branch_id": "00001", "order_id": "1",
               "symbol": "005930", "side": "buy", "quantity": "2", "amount": "10000000"}
        source = {"account": {"id": "competition"}, "environment": "paper", "status": "ok",
                  "start_date": "2026-10-12", "end_date": "2026-10-13",
                  "updated_at": "2026-10-13T16:01:00+09:00", "trades": [row, row], **changes}
        return calculate_kis_progress(source, [], fingerprint="test", now=datetime(2026, 10, 13, 17, tzinfo=KST))

    def test_pace_uses_deduplicated_whole_account_fills(self):
        result = self.progress()
        self.assertEqual(result["pace"]["gross_amount"], "10000000")
        self.assertEqual(result["pace"]["gross_amount"], result["metrics"]["gross_amount"]["value"])

    def test_partial_or_stale_retains_existing_total_but_hides_pace(self):
        for changes in ({"updated_at": None}, {"status": "error"}, {"stale": True},
                        {"updated_at": "2026-10-13T15:00:00+09:00"}):
            with self.subTest(changes=changes):
                result = self.progress(**changes)
                self.assertEqual(result["metrics"]["gross_amount"]["value"], "10000000")
                self.assertEqual(result["pace"]["status"], "unavailable")
                self.assertIsNone(result["pace"]["required_daily_gross_amount"])


if __name__ == "__main__":
    unittest.main()
