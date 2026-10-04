"""Synthetic tests only: no real development/validation/holdout outcomes read."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("candidate_analysis", HERE / "analyze.py")
a = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = a
spec.loader.exec_module(a)


def fixture(days=350):
    plan = json.loads((HERE / "plan.json").read_text(encoding="utf-8"))
    calendar = pd.bdate_range("2024-01-02", periods=days)
    t = np.arange(days)
    close = pd.DataFrame({"000001": 100*np.exp(t*.003 + .004*np.sin(t)),
                          "000002": 100*np.exp(t*.001 + .006*np.cos(t)),
                          "0126Z0": 100*np.exp(-t*.001 + .002*np.sin(t))}, index=calendar)
    prices = {"close": close, "open": close*.995, "high": close*1.01, "low": close*.99,
              "volume": close*0+1_000_000, "turnover": close*0+20_000_000_000}
    index = pd.DataFrame({field: 1000*np.exp(t*.0005) for field in a.FIELDS}, index=calendar)
    data = a.Dataset(prices, {"KOSPI": index, "KOSDAQ": index.copy()},
                     pd.Series({"000001": "KOSPI", "000002": "KOSDAQ", "0126Z0": "KOSDAQ"}),
                     {}, "fullhash", "prefixhash")
    return data, plan


def persist_fixture(directory, data):
    universe = [{"symbol": symbol, "board": board} for symbol, board in data.boards.items()]
    a.write_json(directory / "progress.json", {"status": "complete"})
    a.write_json(directory / "universe-snapshot.json", {"status": "verified", "rows": universe})
    a.write_json(directory / "manifest.json", {"stock_count": len(universe), "FID_ORG_ADJ_PRC": "0",
                                              "universe_sha256": "fixture"})
    for board, code in a.INDEX_CODES.items():
        rows = [dict(date=str(day.date()), **{f: str(row[f]) for f in a.FIELDS})
                for day, row in data.indices[board].iterrows()]
        a.write_json(directory / "indices" / f"{code}.json", dict(status="complete", board=board,
                     universe_sha256="fixture", rows=rows))
    for symbol, board in data.boards.items():
        rows = [dict(date=str(day.date()), **{f: str(data.prices[f].loc[day, symbol]) for f in a.FIELDS})
                for day in data.prices["close"].index]
        a.write_json(directory / "series" / f"{symbol}.json", dict(status="complete", board=board,
                     universe_sha256="fixture", rows=rows))


def compact_stage_fixture(directory):
    data, plan = fixture(180)
    dates = data.prices["close"].index
    plan["periods"] = {"development": [str(dates[61].date()), str(dates[99].date())],
                       "validation": [str(dates[100].date()), str(dates[139].date())],
                       "holdout": [str(dates[140].date()), str(dates[-1].date())]}
    plan["variants"] = [v for v in plan["variants"] if v["id"] in ("liquid_all", "rs20_top20")]
    plan["evaluation"]["uncertainty"]["replicates"] = 40
    plan["evaluation"]["sensitivity"] = {"liquidity_krw": [10_000_000_000], "shortlist": [20]}
    plan_path = directory / "plan.json"
    a.write_json(plan_path, plan)
    persist_fixture(directory, data)
    return data, plan_path


def adoption_fixture():
    _, plan = fixture(5)
    frozen = {"selected_variant": "rs60_top20"}
    summaries = [dict(variant="rs60_top20", period="holdout", horizon=5,
                      slippage_each_side=slippage, market=market,
                      mean_paired_difference=.005, paired_difference_ci95=[.001, .009], mean_net_return=.01)
                 for market, slippage in (("ALL", .001), ("KOSPI", .001), ("KOSDAQ", .001), ("ALL", .002))]
    return summaries, frozen, plan


class CandidateAnalysisTests(unittest.TestCase):
    def test_adoption_pass_uses_only_frozen_variant_and_holdout_primary_horizon(self):
        summaries, frozen, plan = adoption_fixture()
        original = copy.deepcopy((summaries, frozen, plan))
        unrelated = [dict(summaries[0], variant="trend_volume", mean_paired_difference=-1),
                     dict(summaries[0], period="validation", mean_paired_difference=-1),
                     dict(summaries[0], horizon=20, mean_paired_difference=-1)]
        result = a.adoption_decision(summaries + unrelated, frozen, plan)
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["adopted"])
        self.assertEqual(result["production_variant"], frozen["selected_variant"])
        self.assertEqual({row["status"] for row in result["checks"]}, {"pass"})
        self.assertFalse(result["reselection"])
        self.assertEqual((summaries, frozen, plan), original)

    def test_adoption_zero_fails_every_strict_boundary(self):
        cases = [(0, "mean_paired_difference", "all_primary_difference"),
                 (0, "paired_difference_ci95", "all_primary_ci_lower"),
                 (1, "mean_paired_difference", "kospi_primary_difference"),
                 (2, "mean_paired_difference", "kosdaq_primary_difference"),
                 (0, "mean_net_return", "all_primary_net_return"),
                 (3, "mean_net_return", "all_high_net_return")]
        for index, metric, check in cases:
            summaries, frozen, plan = adoption_fixture()
            summaries[index][metric] = [0, .009] if metric == "paired_difference_ci95" else 0
            with self.subTest(check=check):
                result = a.adoption_decision(summaries, frozen, plan)
                self.assertEqual(result["status"], "fail")
                self.assertFalse(result["adopted"])
                self.assertEqual(result["production_variant"], "liquid_top20")
                self.assertEqual(next(row for row in result["checks"] if row["id"] == check)["status"], "fail")

    def test_adoption_missing_or_invalid_ci_is_not_a_pass(self):
        for interval in (None, [], [.01], [float("nan"), .01], [.02, .01]):
            summaries, frozen, plan = adoption_fixture()
            summaries[0]["paired_difference_ci95"] = interval
            with self.subTest(interval=interval):
                result = a.adoption_decision(summaries, frozen, plan)
                self.assertEqual(result["status"], "missing")
                self.assertEqual(result["production_variant"], "liquid_top20")
                self.assertEqual(next(row for row in result["checks"] if row["id"] == "all_primary_ci_lower")["status"], "missing")

    def test_adoption_negative_market_or_cost_return_requires_fallback(self):
        for index, metric in ((1, "mean_paired_difference"), (2, "mean_paired_difference"),
                              (0, "mean_net_return"), (3, "mean_net_return")):
            summaries, frozen, plan = adoption_fixture()
            summaries[index][metric] = -.001
            with self.subTest(index=index, metric=metric):
                result = a.adoption_decision(summaries, frozen, plan)
                self.assertEqual(result["status"], "fail")
                self.assertEqual(result["production_variant"], "liquid_top20")
                self.assertEqual(result["basis"], "operational_default")

    def test_adoption_without_selection_or_required_summary_never_reselects(self):
        summaries, frozen, plan = adoption_fixture()
        result = a.adoption_decision(summaries, {"selected_variant": None}, plan)
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["reason"], "No frozen variant was selected")
        self.assertEqual(result["production_variant"], "liquid_top20")
        self.assertIn("검증됐다는 뜻은 아니다", result["fallback_note"])
        for rows in (summaries[:-1], summaries + [summaries[0]]):
            result = a.adoption_decision(rows, frozen, plan)
            self.assertEqual(result["status"], "missing")
            self.assertFalse(result["reselection"])
            self.assertEqual(result["production_variant"], "liquid_top20")

    def test_next_open_to_horizon_close_and_costs(self):
        data, plan = fixture(80)
        day = data.prices["close"].index[65]
        data.prices["open"].iloc[66, 0] = 100
        data.prices["close"].iloc[70, 0] = 110
        costs = plan["costs"]
        actual = a.forward_returns(data, 5, .001, costs).loc[day, "000001"]
        expected = 110*.999*(1-costs["sell_fee_rate"]-costs["sell_tax_rate"])/(100*1.001*(1+costs["buy_fee_rate"]))-1
        self.assertAlmostEqual(actual, expected)
        # Horizon 1 exits on the same day as the D+1 opening entry.
        expected1 = data.prices["close"].iloc[66, 0] / 100 - 1
        self.assertAlmostEqual(a.forward_returns(data, 1, 0, dict(buy_fee_rate=0, sell_fee_rate=0,
                              sell_tax_rate=0)).loc[day, "000001"], expected1)

    def test_missing_stock_day_is_not_compressed_or_zero_filled(self):
        data, plan = fixture(90)
        dates = data.prices["close"].index
        for field in a.FIELDS:
            data.prices[field].loc[dates[66], "000001"] = np.nan
        outcomes = a.forward_returns(data, 5, .001, plan["costs"])
        self.assertTrue(np.isnan(outcomes.loc[dates[65], "000001"]))
        f = a.features(data, plan)
        self.assertFalse(f["history61"].loc[dates[67], "000001"])
        self.assertTrue(f["history61"].loc[dates[67], "000002"])
        # A missing entry stays unknown even though the following stock row exists.
        selected = pd.DataFrame(True, index=dates, columns=data.boards.index)
        daily = a.daily_cohorts(selected, selected, outcomes, str(dates[65].date()),
                               str(dates[75].date()), 5, data.boards, "ALL")
        self.assertEqual(daily.loc[dates[65], "missing_count"], 1)
        self.assertEqual(daily.loc[dates[65], "observed_count"], 2)
        self.assertAlmostEqual(daily.loc[dates[65], "difference"], 0)

    def test_future_mutation_does_not_change_past_selection_or_metrics(self):
        data, plan = fixture()
        cutoff = data.prices["close"].index[300]
        altered = copy.deepcopy(data)
        for field in a.FIELDS:
            altered.prices[field].loc[altered.prices[field].index > cutoff] *= 50
        for values in altered.indices.values():
            values.loc[values.index > cutoff] *= 20
        before, after = a.features(data, plan), a.features(altered, plan)
        for variant in plan["variants"]:
            old, _ = a.select_candidates(before, variant, plan)
            new, _ = a.select_candidates(after, variant, plan)
            pd.testing.assert_frame_equal(old.loc[:cutoff], new.loc[:cutoff])
        v = next(v for v in plan["variants"] if v["id"] == "rs60_top20")
        old, base = a.select_candidates(before, v, plan)
        new, base2 = a.select_candidates(after, v, plan)
        kwargs = dict(start=str(data.prices["close"].index[253].date()), end=str(cutoff.date()),
                      horizon=5, boards=data.boards, market="ALL")
        daily1 = a.daily_cohorts(old, base, a.forward_returns(data, 5, .001, plan["costs"]), **kwargs)
        daily2 = a.daily_cohorts(new, base2, a.forward_returns(altered, 5, .001, plan["costs"]), **kwargs)
        pd.testing.assert_frame_equal(daily1, daily2)
        self.assertEqual(daily1.index[-1], data.prices["close"].index[295])

    def test_all_predicates_precede_ranking_and_market_is_subset(self):
        data, plan = fixture(90)
        plan["maximum_shortlist"] = 1
        f = a.features(data, plan)
        f["volume_ratio"].iloc[-1] = [1.0, 2.0, 2.0]
        variant = next(v for v in plan["variants"] if v["id"] == "trend_volume")
        selected, baseline = a.select_candidates(f, variant, plan)
        self.assertEqual(selected.iloc[-1].to_dict(), {"000001": False, "000002": True, "0126Z0": False})
        dates = selected.index
        all_pick, base = a.select_candidates(f, next(v for v in plan["variants"] if v["id"] == "rs60_top20"), plan)
        daily = a.daily_cohorts(all_pick, base, a.forward_returns(data, 1, .001, plan["costs"]),
                               str(dates[65].date()), str(dates[-1].date()), 1, data.boards, "KOSDAQ")
        self.assertTrue(daily["selected_count"].eq(0).all())
        self.assertTrue(daily["selected_return"].isna().all())
        self.assertTrue(daily["baseline_count"].gt(0).all())

    def test_atr_includes_signal_day_and_volume_ratio_excludes_it(self):
        data, plan = fixture(90)
        data.prices["turnover"].iloc[-1, 0] = 60_000_000_000
        f = a.features(data, plan)
        self.assertAlmostEqual(f["volume_ratio"].iloc[-1, 0], 3)
        p = data.prices
        tr = pd.concat([p["high"]["000001"]-p["low"]["000001"],
                        (p["high"]["000001"]-p["close"]["000001"].shift()).abs(),
                        (p["low"]["000001"]-p["close"]["000001"].shift()).abs()], axis=1).max(axis=1)
        expected = (p["close"].iloc[-1, 0]-p["close"].iloc[-20:, 0].mean()) / tr.iloc[-14:].mean()
        self.assertAlmostEqual(f["extension_atr"].iloc[-1, 0], expected)

    def test_selection_tie_complexity_and_baseline_exclusion(self):
        _, plan = fixture(5)
        rows = []
        values = {"liquid_all": 10, "rs60_top20": .0017, "trend_rs60": .002, "trend_volume": .0021}
        for variant, value in values.items():
            for period in ("development", "validation"):
                rows.append(dict(variant=variant, period=period, market="ALL", horizon=5,
                                 slippage_each_side=.001, mean_paired_difference=value))
        self.assertEqual(a.choose_variant(rows, plan)["selected_variant"], "rs60_top20")
        rows = [dict(r, mean_paired_difference=-1) if r["period"] == "development" else r for r in rows]
        self.assertIsNone(a.choose_variant(rows, plan)["selected_variant"])

    def test_bootstrap_preserves_calendar_missing_positions(self):
        settings = dict(block_sessions=20, replicates=2000, seed=20261004, confidence=.95)
        values = np.full(80, .01)
        values[20:35] = np.nan
        ci = a.block_ci(values, settings)
        np.testing.assert_allclose(ci, [.01, .01])
        self.assertIsNone(a.block_ci(np.ones(19), settings))
        self.assertIsNone(a.block_ci(np.linspace(-.20, .20, 20), settings))
        sparse = np.full(80, np.nan)
        sparse[:20] = np.linspace(-.20, .20, 20)
        self.assertIsNone(a.block_ci(sparse, settings))
        self.assertIsNotNone(a.block_ci(np.linspace(-.20, .20, 21), settings))
        self.assertEqual(a.block_ci(values, settings), ci)

    def test_stage_guards_before_loading_any_returns(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            with self.assertRaisesRegex(ValueError, "must be frozen"):
                a.run("holdout", HERE / "plan.json", out, out)
            a.write_json(out / "selection-frozen.json", {})
            with self.assertRaisesRegex(ValueError, "already frozen"):
                a.run("development-validation", HERE / "plan.json", out, out)

    def test_synthetic_full_stages_freeze_hash_and_future_invariance(self):
        data, plan = fixture(430)
        dates = data.prices["close"].index
        plan["periods"] = {"development": [str(dates[253].date()), str(dates[319].date())],
                           "validation": [str(dates[320].date()), str(dates[379].date())],
                           "holdout": [str(dates[380].date()), str(dates[-1].date())]}
        plan["evaluation"]["uncertainty"]["replicates"] = 40
        plan["evaluation"]["sensitivity"] = {"liquidity_krw": [10_000_000_000], "shortlist": [20]}
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            out = directory / "analysis"
            plan_path = directory / "plan.json"
            a.write_json(plan_path, plan)
            persist_fixture(directory, data)
            first = a.run("development-validation", plan_path, directory, out)
            frozen_bytes = (out / "selection-frozen.json").read_bytes()
            frozen = a.read_json(out / "selection-frozen.json")
            self.assertIsNotNone(first["selected_variant"])
            self.assertFalse((out / "holdout.json").exists())
            before = a.read_json(out / "development-validation.json")
            changed = copy.deepcopy(data)
            for field in a.FIELDS:
                changed.prices[field].loc[dates[380]:] *= 3
            persist_fixture(directory, changed)
            out2 = directory / "synthetic-second-run"
            with self.assertRaisesRegex(ValueError, "already frozen"):
                a.run("development-validation", plan_path, directory, out2)
            # Future-invariance comparison needs a separate synthetic dataset, not a bypass of its ledger.
            independent = directory / "independent-synthetic-dataset"
            persist_fixture(independent, changed)
            a.run("development-validation", plan_path, independent, out2)
            after = a.read_json(out2 / "development-validation.json")
            self.assertEqual(before["summaries"], after["summaries"])
            self.assertEqual(before["development_validation_data_sha256"], after["development_validation_data_sha256"])
            self.assertNotEqual(before["source_data_sha256"], after["source_data_sha256"])
            # A revised historical input is rejected, even with an unchanged plan.
            changed.prices["turnover"].iloc[300, 0] += 1
            persist_fixture(directory, changed)
            with self.assertRaisesRegex(ValueError, "observations changed"):
                a.run("holdout", plan_path, directory, out)
            changed.prices["turnover"].iloc[300, 0] -= 1
            persist_fixture(directory, changed)
            held = a.run("holdout", plan_path, directory, out)
            self.assertEqual(held["selected_variant"], frozen["selected_variant"])
            self.assertIn("adoption", a.read_json(out / "holdout.json"))
            self.assertEqual((out / "selection-frozen.json").read_bytes(), frozen_bytes)
            with self.assertRaisesRegex(ValueError, "already evaluated"):
                a.run("holdout", plan_path, directory, out)
            with self.assertRaisesRegex(ValueError, "already evaluated"):
                a.run("holdout", plan_path, directory, directory / "other-holdout-output")

    def test_failed_artifact_writes_resume_same_inputs_without_reselection(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            data, plan_path = compact_stage_fixture(directory)
            out = directory / "analysis"
            original_write = a.write_json

            def fail_sensitivity(path, value, exclusive=False):
                if path.name == "sensitivity-development-validation.json":
                    raise OSError("synthetic output interruption")
                return original_write(path, value, exclusive)

            with mock.patch.object(a, "write_json", side_effect=fail_sensitivity):
                with self.assertRaisesRegex(OSError, "output interruption"):
                    a.run("development-validation", plan_path, directory, out)
            ledger = a.read_json(directory / a.LEDGER_NAME)
            frozen = copy.deepcopy(ledger["frozen_selection"])
            self.assertEqual(ledger["stages"]["development-validation"]["status"], "failed")
            self.assertIsNotNone(frozen["selected_variant"])
            with self.assertRaisesRegex(ValueError, "original output directory"):
                a.run("development-validation", plan_path, directory, directory / "bypass")
            altered = copy.deepcopy(data)
            altered.prices["turnover"].iloc[80, 0] += 1
            persist_fixture(directory, altered)
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                a.run("development-validation", plan_path, directory, out)
            persist_fixture(directory, data)
            with mock.patch.object(a, "choose_variant", side_effect=AssertionError("must not reselect")):
                a.run("development-validation", plan_path, directory, out)
            ledger = a.read_json(directory / a.LEDGER_NAME)
            self.assertEqual(ledger["frozen_selection"], frozen)
            self.assertEqual([row["status"] for row in ledger["stages"]["development-validation"]["attempts"]],
                             ["failed", "failed", "complete"])

            def fail_holdout_daily(path, value, exclusive=False):
                if path.name == "holdout-daily.json":
                    raise OSError("synthetic holdout write interruption")
                return original_write(path, value, exclusive)

            with mock.patch.object(a, "write_json", side_effect=fail_holdout_daily):
                with self.assertRaisesRegex(OSError, "holdout write interruption"):
                    a.run("holdout", plan_path, directory, out)
            self.assertTrue((out / "holdout.json").exists())
            self.assertEqual(a.read_json(directory / a.LEDGER_NAME)["stages"]["holdout"]["status"], "failed")
            a.run("holdout", plan_path, directory, out)
            ledger = a.read_json(directory / a.LEDGER_NAME)
            self.assertEqual([row["status"] for row in ledger["stages"]["holdout"]["attempts"]],
                             ["failed", "complete"])
            self.assertEqual(ledger["frozen_selection"], frozen)

    def test_dataset_lock_rejects_concurrent_run_and_releases_after_error(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with a.dataset_run_lock(directory):
                with self.assertRaisesRegex(ValueError, "already running"):
                    a.run("development-validation", HERE / "plan.json", directory, directory / "out")
            with self.assertRaisesRegex(ValueError, "must be frozen"):
                a.run("holdout", HERE / "plan.json", directory, directory / "out")


if __name__ == "__main__":
    unittest.main()
