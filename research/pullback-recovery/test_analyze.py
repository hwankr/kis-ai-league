"""Synthetic protocol tests; no historical prices or outcomes are read."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("pullback_analysis_test", HERE / "analyze.py")
a = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = a
spec.loader.exec_module(a)


def fixture(length=125, signal_pos=70):
    days = pd.bdate_range("2024-01-02", periods=length)
    close = pd.DataFrame({"000001": 100. + np.arange(length),
                          "000002": 150. + np.arange(length),
                          "000003": 200. + np.arange(length)}, index=days)
    peak = close.iloc[signal_pos-3, 0]
    close.iloc[signal_pos-2, 0] = peak-2
    close.iloc[signal_pos-1, 0] = peak-4
    close.iloc[signal_pos, 0] = peak-1
    prices = {"open": close.copy(), "high": close+.5, "low": close-.5,
              "close": close.copy(), "volume": close*0+1_000_000.,
              "turnover": close*0+20_000_000_000.}
    index = pd.DataFrame({"close": 1000.+np.arange(length)}, index=days)
    data = SimpleNamespace(prices=prices, indices={"KOSPI": index, "KOSDAQ": index.copy()},
                           boards={"000001": "KOSPI", "000002": "KOSPI", "000003": "KOSDAQ"})
    plan = a.read(HERE / "plan.json")
    plan["periods"] = {"development": [str(days[65].date()), str(days[90].date())],
                       "validation": [str(days[91].date()), str(days[110].date())],
                       "reused_audit": [str(days[111].date()), str(days[-1].date())]}
    plan["uncertainty"]["replicates"] = 100
    return data, plan


def first_window():
    closes = np.r_[np.arange(100., 160.), 157., 155., 158.]
    return np.column_stack([closes, closes+.5, closes-.5, closes,
                            np.full(63, 1e6), np.full(63, 2e10)])


def shared(window):
    days = [str(d.date()) for d in pd.bdate_range("2024-01-02", periods=len(window))]
    bars = {day: dict(zip(a.FIELDS, row)) for day, row in zip(days, window)}
    return a.evaluate_pullback(bars, days)


class PullbackResearchTests(unittest.TestCase):
    def test_sixty_three_sessions_and_two_independent_formulas(self):
        window = first_window()
        self.assertEqual(a.direct_rule(window), (True, True, True))
        self.assertTrue(shared(window)["signal"])
        self.assertFalse(shared(window[1:])["eligible"])
        frame = pd.DataFrame(window, columns=a.FIELDS)
        prices = {key: frame[[key]].rename(columns={key: "x"}) for key in a.FIELDS}
        values = a.vector_rule(prices)
        self.assertEqual(tuple(bool(x.iloc[-1, 0]) for x in values), (True, True, True))

    def test_strict_recovery_and_pullback_equalities_do_not_signal(self):
        for case in ("recovery", "pullback"):
            with self.subTest(case=case):
                window = first_window()
                if case == "recovery":
                    window[-1, 0:4] = [window[-2, 1], window[-2, 1]+.5,
                                       window[-2, 1]-.5, window[-2, 1]]
                else:
                    window[-2, 0:4] = window[-3, 0:4]
                self.assertFalse(shared(window)["signal"])
                self.assertFalse(a.direct_rule(window)[2])

    def test_missing_any_required_bar_is_ineligible(self):
        window = first_window()
        window[0, 3] = np.nan
        self.assertFalse(shared(window)["eligible"])
        self.assertEqual(a.direct_rule(window), (False, False, False))

    def test_asof_prefix_is_unchanged_by_future_prices(self):
        data, _ = fixture()
        original = a.vector_rule(data.prices)
        changed = copy.deepcopy(data)
        for field in a.FIELDS:
            changed.prices[field].iloc[71:] = np.nan
        altered = a.vector_rule(changed.prices)
        for left, right in zip(original, altered):
            pd.testing.assert_frame_equal(left.iloc[:71], right.iloc[:71])
        frame = pd.DataFrame({f: data.prices[f]["000001"] for f in a.FIELDS})
        days = [str(d.date()) for d in frame.index]
        bars = {d: dict(zip(a.FIELDS, row)) for d, row in zip(days, frame.to_numpy())}
        before = a.evaluate_pullback(bars, days[:71])
        for day in days[71:]:
            bars[day] = {f: float('nan') for f in a.FIELDS}
        self.assertEqual(before, a.evaluate_pullback(bars, days[:71]))

    def test_all_shortlisted_candidates_have_formula_audit(self):
        data, _ = fixture()
        prepared = a.prepare(data)
        self.assertEqual(prepared[-1]["shortlist_stock_dates_checked"], 65*3)
        self.assertEqual(prepared[-1]["mismatches"], 0)
        self.assertEqual(prepared[-1]["insufficient_or_invalid_63_sessions"], 2*3)

    def test_same_market_controls_include_signal_and_exclude_other_market(self):
        data, plan = fixture()
        study = a.Study(data, plan)
        summaries, signals, controls, daily = study.evaluate("development", .001)
        day = str(study.calendar[70].date())
        members = [r["symbol"] for r in controls if r["signal_date"] == day]
        self.assertEqual(members, ["000001", "000002"])
        self.assertEqual([r["symbol"] for r in signals if r["signal_date"] == day], ["000001"])
        row = next(r for r in daily if r["date"] == day and r["market"] == "ALL")
        expected = np.mean([study.trade(s, 70, .001, "development")["net_return"] for s in members])
        self.assertAlmostEqual(row["control"], expected)
        self.assertAlmostEqual(row["edge"], row["net"]-expected)

    def test_market_edge_weights_are_observed_signal_counts(self):
        def row(count, net, control):
            result = {k: 0 for k in ("signals", "controls", "observed_controls", "boundary", "signal_data_unknown",
                                     "control_data_unknown", "signal_fill_unverifiable", "control_fill_unverifiable")}
            result.update(observed_signals=count, net=net, control=control, edge=net-control)
            return result
        result = a.aggregate_markets([row(1, .10, .04), row(3, .20, .08)])
        self.assertAlmostEqual(result["net"], .175)
        self.assertAlmostEqual(result["control"], .07)
        self.assertAlmostEqual(result["edge"], .105)

    def test_next_open_clock_and_independent_cost_identity(self):
        data, plan = fixture()
        study = a.Study(data, plan)
        result = study.trade("000001", 70, .001, "development")
        self.assertEqual(result["entry_pos"], 71)
        self.assertEqual(result["exit_pos"], 76)
        entry = data.prices["open"].iloc[71]["000001"]
        exit_price = data.prices["open"].iloc[76]["000001"]
        expected = exit_price*.999*(1-.000140527-.002)/(entry*1.001*(1+.000140527))-1
        self.assertAlmostEqual(result["net_return"], expected)

    def test_period_boundary_never_becomes_zero_return(self):
        data, plan = fixture()
        study = a.Study(data, plan)
        result = study.trade("000001", 85, .001, "development")
        self.assertEqual(result["classification"], "boundary")
        self.assertIsNone(result["net_return"])

    def test_zero_volume_is_execution_uncertain_not_unknown_data(self):
        data, plan = fixture()
        data.prices["volume"].iloc[71, 0] = 0
        study = a.Study(data, plan)
        result = study.trade("000001", 70, .001, "development")
        self.assertEqual(result["classification"], "fill_unverifiable")
        self.assertIsNone(result["net_return"])
        data.prices["open"].iloc[71, 0] = np.nan
        result = a.Study(data, plan).trade("000001", 70, .001, "development")
        self.assertEqual(result["classification"], "data_unknown")

    def test_nonoverlap_releases_at_exit_close_and_blocks_unknown(self):
        rows = [dict(symbol="x", signal_date=f"2024-01-{p:02d}", signal_pos=p,
                     exit_pos=p+6, classification="observed") for p in (1, 3, 7, 14)]
        result = a.nonoverlap(rows)
        self.assertEqual([r["nonoverlap_entry"] for r in result], [True, False, True, True])
        rows[0]["classification"] = "fill_unverifiable"
        result = a.nonoverlap(rows)
        self.assertEqual([r["nonoverlap_entry"] for r in result], [True, False, False, False])

    def test_calendar_blocks_preserve_no_signal_gaps(self):
        settings = {"block_sessions": 20, "replicates": 100, "seed": 9,
                    "confidence": .95, "small_sample_days": 30}
        values = np.r_[np.full(25, .1), np.full(30, np.nan), np.full(25, -.1)]
        means = a.bootstrap_means(values, settings)
        rng = np.random.default_rng(9)
        starts = rng.integers(0, 61, size=(100, 4))
        expected = []
        for draw in starts:
            sample = np.concatenate([values[s:s+20] for s in draw])
            finite = sample[np.isfinite(sample)]
            expected.append(np.mean(finite) if len(finite) else np.nan)
        np.testing.assert_allclose(means, expected, equal_nan=True)
        self.assertFalse(np.allclose(means, a.bootstrap_means(values[np.isfinite(values)], settings)))
        self.assertIsNotNone(a.calendar_ci(values, settings))
        self.assertIsNone(a.calendar_ci(np.r_[np.ones(29), np.full(50, np.nan)], settings))

    def test_freeze_is_exclusive_and_detects_changed_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder)/"source", Path(folder)/"run"
            source.mkdir(); (source/"indices").mkdir(); (source/"series").mkdir()
            for name in ("manifest.json", "progress.json", "universe-snapshot.json"):
                (source/name).write_text("{}", encoding="utf8")
            a.freeze(source, output)
            a.verify_freeze(source, output)
            with self.assertRaises(FileExistsError):
                a.freeze(source, output)
            (source/"manifest.json").write_text('{"changed": true}', encoding="utf8")
            with self.assertRaisesRegex(ValueError, "Frozen"):
                a.verify_freeze(source, output)

    def test_gate_separates_fill_uncertainty_from_unknown_data_without_adopting(self):
        plan = a.read(HERE / "plan.json")
        rows = []
        for period in ("development", "validation"):
            for slip in plan["costs"]["slippage"]:
                for market in ("ALL", "KOSPI", "KOSDAQ"):
                    rows.append(dict(period=period, slip=slip, market=market, signal_days=30,
                        counts=dict(observed=60, data_unknown=0, fill_unverifiable=1, unclosed=0),
                        control_counts=dict(data_unknown=0, fill_unverifiable=1, unclosed=0),
                        net_day={"mean": .01}, edge_day={"mean": .002},
                        nonoverlap_net={"mean": .009}, edge_ci95=[.001, .003]))
        result = a.research_gate(rows, plan)
        self.assertEqual(result["status"], "research_candidate")
        self.assertTrue(result["execution_review_required"])
        self.assertFalse(result["adopted"])
        self.assertFalse(result["order_enabled"])
        rows[0]["counts"]["data_unknown"] = 1
        result = a.research_gate(rows, plan)
        self.assertEqual(result["status"], "unadopted")
        self.assertIn("development/0.001/unknown_data", result["failures"])


if __name__ == "__main__":
    unittest.main()
