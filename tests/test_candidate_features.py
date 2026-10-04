from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import math
import statistics
import unittest

from backend.candidate_features import InsufficientFeatureHistory, candidate_features
from backend.kis import KisError


def history(count=61):
    days, day = [], date(2026, 10, 2)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    stock = {day: {"open": Decimal(100 + i), "high": Decimal(102 + i),
                   "low": Decimal(97 + i), "close": Decimal(100 + i),
                   "volume": Decimal(10), "turnover": Decimal((i + 1) * 1000)}
             for i, day in enumerate(days)}
    benchmark = {day: Decimal(200 + i) for i, day in enumerate(days)}
    return stock, benchmark, days


class CandidateFeatureTests(unittest.TestCase):
    def test_core_values_match_independent_known_prices_and_sample_volatility(self):
        stock, benchmark, days = history()
        result = candidate_features(stock, benchmark, days)
        self.assertEqual(result["sma20"], "150.50")
        self.assertEqual(result["sma60"], "130.50")
        self.assertEqual(result["sma20_change_5d_pct"], "3.4364")
        self.assertEqual(result["return_60d_pct"], "60.0000")
        self.assertEqual(Decimal(result["excess_60d_pp"]), Decimal(30))
        self.assertEqual(result["median_turnover_20d"], "51500.00")
        self.assertEqual(result["atr14"], "5.0000")
        self.assertEqual(Decimal(result["extension_atr"]), Decimal("1.9"))
        self.assertEqual(Decimal(result["avg_turnover_20d"]), Decimal(51500))
        self.assertAlmostEqual(float(result["excess_20d_pp"]), (160 / 140 - 260 / 240) * 100)
        self.assertAlmostEqual(float(result["turnover_ratio"]), 61000 / 50500)
        self.assertTrue(result["market_up"])
        daily_returns = [1.0 / price for price in range(100, 160)]
        independent_risk = 0.30 / (statistics.stdev(daily_returns) * math.sqrt(60))
        self.assertAlmostEqual(float(result["risk_adjusted_rs60"]), independent_risk, places=6)
        self.assertTrue(result["trend"])
        self.assertEqual(result["zero_volume_days_20d"], 0)
        self.assertIsNone(result["excess_6m_skip1m_pp"])
        self.assertIsNone(result["excess_12to7m_pp"])

    def test_missing_middle_core_session_cannot_be_replaced_by_older_extra_data(self):
        stock, benchmark, days = history(253)
        for remove_from in ("stock", "benchmark"):
            values, market = deepcopy(stock), deepcopy(benchmark)
            target = values if remove_from == "stock" else market
            target[days[0] - timedelta(days=1)] = target.pop(days[-30])
            with self.subTest(remove_from=remove_from), self.assertRaises(InsufficientFeatureHistory):
                candidate_features(values, market, days)

    def test_too_few_market_sessions_fails_even_if_stock_has_extra_rows(self):
        stock, benchmark, days = history()
        with self.assertRaises(InsufficientFeatureHistory):
            candidate_features(stock, benchmark, days[-60:])

    def test_unsorted_duplicate_and_non_date_calendar_values_are_rejected(self):
        stock, benchmark, days = history()
        for invalid in (days[::-1], days[:-1] + [days[-2]], days[:-1] + ["2026-10-02"], None):
            with self.subTest(days=invalid), self.assertRaises(KisError):
                candidate_features(stock, benchmark, invalid)

    def test_future_observations_do_not_change_results_or_fail_validation(self):
        stock, benchmark, days = history(253)
        expected = candidate_features(stock, benchmark, days)
        original = deepcopy(stock)
        for offset in (1, 7, 30):
            future = days[-1] + timedelta(days=offset)
            stock[future] = {"close": Decimal("NaN")}
            benchmark[future] = Decimal("-999")
        self.assertEqual(candidate_features(stock, benchmark, days), expected)
        self.assertEqual({day: stock[day] for day in days}, original)

    def test_atr_includes_opening_gaps_and_exactly_fourteen_true_ranges(self):
        stock, benchmark, days = history()
        for row in stock.values():
            row.update(open=Decimal(100), high=Decimal(102), low=Decimal(98), close=Decimal(100))
        stock[days[-14]].update(open=Decimal(110), high=Decimal(112), low=Decimal(108), close=Decimal(110))
        # 기준일 포함 창: 상승갭 TR12, 다음날 하락갭 TR12, 나머지 12일 TR4.
        result = candidate_features(stock, benchmark, days)
        self.assertEqual(result["atr14"], "5.1429")
        self.assertAlmostEqual(float(result["extension_atr"]), -0.5 / (72 / 14))
        stock[days[-16]]["high"] = Decimal(1000)
        self.assertEqual(candidate_features(stock, benchmark, days)["atr14"], result["atr14"])

    def test_constant_prices_zero_volume_and_zero_turnover_keep_zero_denominators_null(self):
        stock, benchmark, days = history()
        for day, row in stock.items():
            row.update({key: Decimal(100) for key in ("open", "high", "low", "close")})
            row.update(volume=Decimal(0), turnover=Decimal(0))
            benchmark[day] = Decimal(200)
        result = candidate_features(stock, benchmark, days)
        self.assertEqual(result["atr14"], "0.0000")
        self.assertIsNone(result["extension_atr"])
        self.assertIsNone(result["risk_adjusted_rs60"])
        self.assertEqual(result["median_turnover_20d"], "0.00")
        self.assertEqual(result["zero_volume_days_20d"], 20)
        self.assertFalse(result["trend"])

    def test_zero_volume_count_only_uses_last_twenty_sessions(self):
        stock, benchmark, days = history()
        for day in (days[0], days[-21], days[-20], days[-1]):
            stock[day]["volume"] = Decimal(0)
        self.assertEqual(candidate_features(stock, benchmark, days)["zero_volume_days_20d"], 2)

    def test_trend_requires_strict_order_and_rising_twenty_day_average(self):
        stock, benchmark, days = history()
        for i, day in enumerate(days):
            close = Decimal(200 - i)
            stock[day].update(open=close, close=close, high=close + 2, low=close - 3)
        self.assertFalse(candidate_features(stock, benchmark, days)["trend"])
        stock, benchmark, days = history()
        # 마지막 가격이 충분히 높아도 5일 전 20일 평균보다 낮으면 추세로 표시하지 않는다.
        for i, day in enumerate(days):
            close = Decimal(100 if i < 36 else 200 if i < 41 else 120 if i < 60 else 140)
            stock[day].update(open=close, close=close, high=close + 1, low=close - 1)
        result = candidate_features(stock, benchmark, days)
        self.assertGreater(Decimal(140), Decimal(result["sma20"]))
        self.assertGreater(Decimal(result["sma20"]), Decimal(result["sma60"]))
        self.assertLess(Decimal(result["sma20_change_5d_pct"]), 0)
        self.assertFalse(result["trend"])

    def test_optional_windows_use_exact_endpoints_and_required_history_lengths(self):
        for count in (147, 148, 252, 253):
            stock, benchmark, days = history(count)
            result = candidate_features(stock, benchmark, days)
            with self.subTest(count=count):
                if count < 148:
                    self.assertIsNone(result["excess_6m_skip1m_pp"])
                else:
                    recent, earlier = count - 22, count - 148
                    expected = ((100 + recent) / (100 + earlier) - (200 + recent) / (200 + earlier)) * 100
                    self.assertAlmostEqual(float(result["excess_6m_skip1m_pp"]), expected, places=4)
                if count == 253:
                    self.assertEqual(Decimal(result["excess_12to7m_pp"]), Decimal(63))
                else:
                    self.assertIsNone(result["excess_12to7m_pp"])

    def test_ranking_scores_keep_differences_smaller_than_display_rounding(self):
        for key, offset, places in (("excess_60d_pp", 1, 4), ("risk_adjusted_rs60", 1, 6),
                                    ("excess_6m_skip1m_pp", 22, 4), ("excess_12to7m_pp", 127, 4)):
            stock, benchmark, days = history(253)
            before = candidate_features(stock, benchmark, days)
            stock[days[-offset]]["close"] += Decimal("0.0000000001")
            after = candidate_features(stock, benchmark, days)
            with self.subTest(key=key):
                self.assertNotEqual(Decimal(before[key]), Decimal(after[key]))
                self.assertEqual(format(Decimal(before[key]), f".{places}f"),
                                 format(Decimal(after[key]), f".{places}f"))

    def test_optional_window_missing_non_endpoint_day_is_not_compressed(self):
        stock, benchmark, days = history(253)
        stock.pop(days[-100])
        result = candidate_features(stock, benchmark, days)
        self.assertIsNone(result["excess_6m_skip1m_pp"])
        self.assertIsNone(result["excess_12to7m_pp"])
        self.assertTrue(result["trend"])
        stock, benchmark, days = history(253)
        benchmark.pop(days[5])
        result = candidate_features(stock, benchmark, days)
        self.assertIsNotNone(result["excess_6m_skip1m_pp"])
        self.assertIsNone(result["excess_12to7m_pp"])

    def test_invalid_ohlc_and_nonfinite_or_negative_amounts_fail_without_echoing_input(self):
        invalid = [("close", Decimal(0)), ("open", Decimal(-1)), ("high", Decimal(1)),
                   ("low", Decimal(1000)), ("close", Decimal("NaN")), ("close", Decimal("Infinity")),
                   ("volume", Decimal(-1)), ("turnover", Decimal(-1)), ("turnover", Decimal("NaN")),
                   ("close", "private-secret"), ("volume", False), ("turnover", None)]
        for field, value in invalid:
            stock, benchmark, days = history()
            stock[days[-20]][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(KisError) as caught:
                candidate_features(stock, benchmark, days)
            self.assertNotIn("private-secret", str(caught.exception))
        stock, benchmark, days = history()
        benchmark[days[-25]] = Decimal(0)
        with self.assertRaises(KisError):
            candidate_features(stock, benchmark, days)

    def test_invalid_values_in_complete_optional_history_are_rejected(self):
        stock, benchmark, days = history(253)
        stock[days[0]]["close"] = Decimal("NaN")
        with self.assertRaises(KisError):
            candidate_features(stock, benchmark, days)


if __name__ == "__main__":
    unittest.main()
