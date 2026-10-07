from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import unittest

from backend.research_rules import evaluate_pullback


def fixture():
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(63)]
    closes = [Decimal(100 + i) for i in range(60)] + [Decimal(155), Decimal(153), Decimal(160)]
    bars = {day: {"open": close, "high": close + 1, "low": close - 1,
                  "close": close, "volume": Decimal(100), "turnover": close * 100}
            for day, close in zip(days, closes)}
    return days, bars


class ResearchRuleTests(unittest.TestCase):
    def test_confirmed_recovery(self):
        days, bars = fixture()
        self.assertEqual(evaluate_pullback(bars, days),
                         {"eligible": True, "trend": True, "signal": True, "reason": "signal"})

    def test_asof_calendar_excludes_future_and_input_not_mutated(self):
        days, bars = fixture()
        original = deepcopy(bars)
        future = days[-1] + timedelta(days=1)
        bars[future] = {key: Decimal("999999") for key in bars[days[0]]}
        self.assertEqual(evaluate_pullback(bars, days), evaluate_pullback(original, days))
        del bars[future]
        self.assertEqual(bars, original)

    def test_needs_63_actual_calendar_bars(self):
        days, bars = fixture()
        self.assertFalse(evaluate_pullback(bars, days[1:])["eligible"])
        del bars[days[2]]
        self.assertEqual(evaluate_pullback(bars, days)["reason"], "invalid_or_missing_bar")

    def test_strict_prior_high_boundary(self):
        days, bars = fixture()
        bars[days[-1]]["close"] = bars[days[-2]]["high"]
        bars[days[-1]]["low"] = bars[days[-1]]["close"] - 1
        self.assertEqual(evaluate_pullback(bars, days)["reason"], "no_recovery")

    def test_requires_two_declines(self):
        days, bars = fixture()
        bars[days[-2]] = deepcopy(bars[days[-3]])
        self.assertEqual(evaluate_pullback(bars, days)["reason"], "no_pullback")

    def test_strict_moving_average_boundaries(self):
        days, bars = fixture()
        for day in days[:-3]:
            bars[day].update(open=Decimal(155), close=Decimal(155), high=Decimal(156), low=Decimal(154))
        self.assertEqual(evaluate_pullback(bars, days)["reason"], "no_trend")

    def test_nonfinite_negative_and_invalid_ohlc_rejected(self):
        for field, value in [("close", "NaN"), ("volume", "-1"), ("turnover", "Infinity"), ("low", "999")]:
            with self.subTest(field=field):
                days, bars = fixture()
                bars[days[0]][field] = Decimal(value)
                self.assertFalse(evaluate_pullback(bars, days)["eligible"])

    def test_calendar_duplicate_or_out_of_order(self):
        days, bars = fixture()
        self.assertEqual(evaluate_pullback(bars, days[::-1])["reason"], "invalid_calendar")
        days[1] = days[0]
        self.assertEqual(evaluate_pullback(bars, days)["reason"], "invalid_calendar")


if __name__ == "__main__":
    unittest.main()
