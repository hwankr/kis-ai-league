"""Synthetic research-integrity regression tests; no historical outcomes are read."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SPEC = importlib.util.spec_from_file_location("daily_trend_analysis_test_target", HERE / "analyze.py")
analysis = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analysis
SPEC.loader.exec_module(analysis)


def market_data(days=150, symbols=2):
    calendar = pd.bdate_range("2024-01-02", periods=days)
    names = [f"{i + 1:06d}" for i in range(symbols)]
    close = pd.DataFrame({name: 100 + np.arange(days) * .2 for name in names}, index=calendar)
    prices = {"open": close.copy(), "high": close + .1, "low": close - .1,
              "close": close, "volume": close * 0 + 1_000_000,
              "turnover": close * 0 + 20_000_000_000}
    indices = {market: pd.DataFrame({"close": 100 + np.arange(days) * .02}, index=calendar)
               for market in ("KOSPI", "KOSDAQ")}
    boards = pd.Series({name: "KOSPI" if i % 2 == 0 else "KOSDAQ" for i, name in enumerate(names)})
    return SimpleNamespace(prices=prices, indices=indices, boards=boards)


def small_plan(calendar, end=None):
    return {
        "periods": {"development": [calendar[0].date().isoformat(), calendar[end or -1].date().isoformat()]},
        "costs": {"buy_fee": 0.0, "sell_fee": 0.0, "sell_tax": 0.0, "slippage": [.001, .002]},
        "diagnostics": {"entry_sensitivity_lookbacks": [10, 20, 60]},
        "uncertainty": {"block_sessions": 2, "replicates": 20, "seed": 7,
                        "confidence": .95, "selection_confidence": .99},
    }


def synthetic_study(signal_positions, end=25, missing=None):
    """Keep selection fixed so tests isolate execution pairing and calendar rules."""
    data = market_data(days=30, symbols=1)
    calendar = data.prices["close"].index
    symbol = data.boards.index[0]
    for name, value in (("open", 100.), ("high", 102.), ("low", 98.), ("close", 100.)):
        data.prices[name].loc[:, :] = value
    if missing is not None:
        for name in ("open", "high", "low", "close"):
            data.prices[name].iloc[missing, 0] = np.nan
    template = data.prices["close"]
    shortlist = template.notna()
    signals = template.notna() & False
    for position in signal_positions:
        signals.iloc[position, 0] = True
    features = {"market_up": template.notna(), "volume_ratio": template * 0 + 1.,
                "extension_atr": template * 0 + .5}
    atr = template * 0 + 2.
    levels = {20: template * 0 + 101.}
    frames = {symbol: pd.DataFrame({name: values[symbol] for name, values in data.prices.items()})}
    prepared = (features, shortlist, atr, levels, {20: signals}, frames)
    with patch.object(analysis, "prepare", return_value=prepared):
        return analysis.Study(data, small_plan(calendar, end))


def all_summary(summaries):
    return next(row for row in summaries if row["market"] == "ALL")


class DailyTrendAnalysisTests(unittest.TestCase):
    def test_code_digest_covers_same_named_files_and_external_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "research/daily-trend/analyze.py", root / "research/daily-trend/engine.py",
                     root / "research/candidate-screen/analyze.py", root / "research/candidate-screen/plan.json"]
            for number, path in enumerate(paths):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"original {number}", encoding="utf-8")
            with patch.object(analysis, "ROOT", root), patch.object(analysis, "HERE", paths[0].parent):
                original = analysis.code_digest()
                for path in paths:
                    with self.subTest(path=path.relative_to(root)):
                        content = path.read_text(encoding="utf-8")
                        path.write_text(content + " changed", encoding="utf-8")
                        self.assertNotEqual(original, analysis.code_digest())
                        path.write_text(content, encoding="utf-8")
                        self.assertEqual(original, analysis.code_digest())

    def test_prepare_prefix_unchanged_by_unseen_or_poisoned_future(self):
        data = market_data()
        plan = small_plan(data.prices["close"].index)
        cutoff = 105
        prefix = deepcopy(data)
        prefix.prices = {name: values.iloc[:cutoff].copy() for name, values in data.prices.items()}
        prefix.indices = {name: values.iloc[:cutoff].copy() for name, values in data.indices.items()}
        poisoned = deepcopy(data)
        for values in poisoned.prices.values():
            values.iloc[cutoff:, :] *= 100
        for values in poisoned.indices.values():
            values.iloc[cutoff:, :] *= .01
        expected = analysis.prepare(prefix, plan)
        self.assertTrue(expected[4][20].to_numpy().any(), "Fixture must contain actual breakout signals")
        for variant in (data, poisoned):
            actual = analysis.prepare(variant, plan)
            for position in (0, 3, 4, 5):
                for key, values in expected[position].items():
                    pd.testing.assert_frame_equal(values, actual[position][key].iloc[:cutoff])
            for position in (1, 2):
                pd.testing.assert_frame_equal(expected[position], actual[position].iloc[:cutoff])

    def test_historical_shortlist_is_ranked_on_each_signal_date(self):
        data = market_data(days=140, symbols=21)
        first, last = data.boards.index[0], data.boards.index[-1]
        turnover = data.prices["turnover"]
        turnover.loc[:, :] = 30_000_000_000.
        turnover.loc[:, first], turnover.loc[:, last] = 40_000_000_000., 15_000_000_000.
        turnover.iloc[75:, turnover.columns.get_loc(first)] = 15_000_000_000.
        turnover.iloc[75:, turnover.columns.get_loc(last)] = 40_000_000_000.
        _, shortlist, _, _, signals, _ = analysis.prepare(data, small_plan(turnover.index))
        self.assertEqual(int(shortlist.iloc[65].sum()), 20)
        self.assertTrue(shortlist.iloc[65][first])
        self.assertFalse(shortlist.iloc[65][last])
        self.assertTrue(signals[20].iloc[65][first])
        self.assertFalse(shortlist.iloc[-1][first])
        self.assertTrue(shortlist.iloc[-1][last])

    def test_fixed_maturity_boundary_excludes_early_and_time_exits_equally(self):
        study = synthetic_study([4], end=8)
        for rule in ({"id": "failed5", "exit": "failed_breakout", "max_hold": 5},
                     {"id": "time5", "exit": "time", "max_hold": 5}):
            with self.subTest(rule=rule["id"]):
                summaries, records, _ = study.evaluate("development", rule, 0.)
                record = records[0]
                self.assertEqual(record["status"], "closed")
                self.assertTrue(record["boundary"])
                self.assertFalse(record["paired_observed"])
                self.assertIsNone(record["baseline_net"])
                self.assertEqual(record["pair_exclusion"], "scheduled_boundary")
                self.assertEqual(all_summary(summaries)["completed"], 0)
                self.assertEqual(all_summary(summaries)["unresolved_mature"], 0)
                if rule["id"] == "failed5":
                    self.assertLess(record["exit_date"], study.plan["periods"]["development"][1])

    def test_early_exit_with_unknown_time_baseline_remains_unresolved(self):
        study = synthetic_study([3], end=20, missing=6)
        summaries, records, daily = study.evaluate(
            "development", {"id": "failed5", "exit": "failed_breakout", "max_hold": 5}, 0.)
        record = records[0]
        self.assertEqual(record["status"], "closed")
        self.assertEqual(record["baseline_status"], "unknown")
        self.assertEqual(record["baseline_unknown_reason"], "missing_ohlc")
        self.assertFalse(record["boundary"])
        self.assertFalse(record["paired_observed"])
        self.assertEqual(record["pair_exclusion"], "baseline_unknown")
        summary = all_summary(summaries)
        self.assertEqual(summary["completed"], 0)
        self.assertEqual(summary["unresolved_mature"], 1)
        self.assertEqual(summary["baseline_unresolved_mature"], 1)
        self.assertTrue(daily.loc[daily.market.eq("ALL"), "net"].isna().all())

    def test_unknown_entry_blocks_later_nonoverlap_entries(self):
        study = synthetic_study([3, 6], end=25, missing=4)
        summaries, records, _ = study.evaluate(
            "development", {"id": "time5", "exit": "time", "max_hold": 5}, 0.)
        first, later = records
        self.assertEqual(first["status"], "unknown")
        self.assertIsNone(first["entry_pos"])
        self.assertTrue(first["nonoverlap_entry"])
        self.assertEqual(later["status"], "closed")
        self.assertTrue(later["paired_observed"])
        self.assertFalse(later["nonoverlap_entry"])
        self.assertEqual(all_summary(summaries)["nonoverlap_count"], 0)

    def test_every_selection_gate_rejects_unsupported_rule(self):
        plan = {"rules": [{"id": "time5"}], "costs": {"slippage": [.001, .002]},
                "selection": {"minimum_events": 60, "minimum_signal_days": 30}}
        rows = [dict(period=period, rule="time5", lookback=20, slip=slip, market=market,
                     net_day={"mean": .02}, edge_day={"mean": .01}, completed=60, signal_days=30,
                     unresolved_mature=0, baseline_unresolved_mature=0,
                     nonoverlap_net={"mean": .015}, edge_ci_selection=[.001, .02])
                for period in ("development", "validation") for slip in (.001, .002)
                for market in ("ALL", "KOSPI", "KOSDAQ")]
        supported = analysis.select(rows, plan)
        self.assertEqual(supported["selected_rule"], "time5")
        self.assertFalse(supported["live_trading_enabled"])
        self.assertIsNone(supported["allocation"])
        self.assertIsNone(supported["maximum_positions"])
        failures = [
            ("development", "ALL", "net_day", {"mean": 0.}, "positive_net"),
            ("development", "ALL", "edge_day", {"mean": 0.}, "positive_edge"),
            ("development", "ALL", "completed", 59, "sample"),
            ("development", "ALL", "signal_days", 29, "sample"),
            ("development", "ALL", "unresolved_mature", 1, "complete"),
            ("development", "ALL", "baseline_unresolved_mature", 1, "complete"),
            ("development", "ALL", "nonoverlap_net", {"mean": 0.}, "nonoverlap"),
            ("validation", "ALL", "edge_ci_selection", [0., .02], "selection_adjusted_ci"),
            ("validation", "KOSPI", "edge_day", {"mean": 0.}, "edge_or_sample"),
            ("validation", "KOSDAQ", "signal_days", 9, "edge_or_sample"),
        ]
        for period, market, key, value, reason in failures:
            with self.subTest(gate=key, market=market):
                changed = deepcopy(rows)
                target = next(row for row in changed
                              if row["period"] == period and row["market"] == market and row["slip"] == .001)
                target[key] = value
                decision = analysis.select(changed, plan)
                self.assertIsNone(decision["selected_rule"])
                self.assertEqual(decision["status"], "no_supported_rule")
                self.assertTrue(any(reason in item for item in decision["checks"][0]["failures"]))

    def test_changed_provenance_rejected_before_overwriting_prior_outputs(self):
        old_provenance = {"plan_sha256": "old", "code_sha256": "code", "prefix_sha256": "prefix",
                          "data_sha256": "data"}
        data = SimpleNamespace(prefix_hash="prefix", data_hash="data")
        for freeze_name in ("develop-provenance.json", "selection.json"):
            with self.subTest(source=freeze_name), TemporaryDirectory() as directory:
                output = Path(directory)
                freeze = output / freeze_name
                content = old_provenance if freeze_name.startswith("develop") else {"provenance": old_provenance}
                freeze.write_text(json.dumps(content), encoding="utf-8")
                summary = output / "develop-summary.json"
                summary.write_text("prior result must survive", encoding="utf-8")
                before = {path.name: path.read_bytes() for path in output.iterdir()}
                args = SimpleNamespace(stage="develop", data="synthetic-unused", output=directory)
                with patch.object(analysis.candidate, "load_data", return_value=data), \
                        patch.object(analysis, "digest", return_value="changed"), \
                        patch.object(analysis, "code_digest", return_value="code"), \
                        patch.object(analysis, "Study") as study:
                    with self.assertRaisesRegex(ValueError, "plan_sha256 changed"):
                        analysis.run(args)
                    study.assert_not_called()
                self.assertEqual(before, {path.name: path.read_bytes() for path in output.iterdir()})


if __name__ == "__main__":
    unittest.main()
