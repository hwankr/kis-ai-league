from copy import deepcopy
import math
import unittest
from unittest.mock import patch

from backend.learning_evaluation import (
    daily_metrics, evaluate_candidate, evaluate_scenarios, make_evaluation_spec, paired_block_bootstrap,
)


class EvaluationTests(unittest.TestCase):
    def test_fixed_return_intervals_and_trade_count_independence(self):
        spec = make_evaluation_spec(1, 40, bootstrap_repetitions=199)
        before = evaluate_candidate(daily_metrics([.0001] * 39), daily_metrics([.001] * 39), spec)
        self.assertEqual(before["decision"], "waiting")
        a, b = daily_metrics([.0001] * 40), daily_metrics([.001] * 40, [.0009] * 40)
        b.update(closed_trades=0)
        result = evaluate_candidate(a, b, spec)
        self.assertEqual(result["decision"], "promote")
        b.update(closed_trades=100000)
        self.assertEqual(result, evaluate_candidate(a, b, spec))
        self.assertEqual(result["phase"], "paper_provisional")
        self.assertEqual(evaluate_candidate({"valid": False, "error": "insufficient_frames"},
                                           {"valid": False, "error": "insufficient_frames"}, spec)["decision"], "waiting")

    def test_identical_economic_behavior_never_promotes(self):
        observed = daily_metrics([.002, -.001] * 20)
        with patch("backend.learning_evaluation.paired_block_bootstrap", side_effect=AssertionError("unnecessary")):
            self.assertEqual(evaluate_candidate(observed, observed, make_evaluation_spec(1, 40))["decision"], "keep")

    def test_delayed_evaluation_cannot_select_a_better_or_worse_end_date(self):
        spec = make_evaluation_spec(2, 40, bootstrap_repetitions=199)
        first = evaluate_candidate(daily_metrics([0.] * 40), daily_metrics([.001] * 40), spec)
        delayed = evaluate_candidate(daily_metrics([0.] * 50), daily_metrics([.001] * 40 + [-.1] * 10), spec)
        self.assertEqual(first, delayed)

    def test_frozen_spec_and_inputs_are_not_mutated(self):
        a, b, spec = daily_metrics([0.] * 40), daily_metrics([.001] * 40), make_evaluation_spec(1, 40, bootstrap_repetitions=199)
        original = deepcopy([a, b, spec])
        self.assertEqual(evaluate_candidate(a, b, spec), evaluate_candidate(a, b, spec))
        self.assertEqual([a, b, spec], original)
        changed = {**spec, "horizon": 60}
        self.assertEqual(evaluate_candidate(a, b, changed)["decision"], "invalid")

    def test_dates_finiteness_and_scenarios_must_match(self):
        a = daily_metrics([.0001] * 40)
        for change in ("dates", "nan", "length", "valid"):
            b = daily_metrics([.001] * 40)
            if change == "dates":
                b["equity_curve"][5]["as_of"] = "1999-01-01"
            elif change == "nan":
                b["daily_log_returns"][5] = float("nan")
            elif change == "length":
                b["stress_daily_log_returns"].pop()
            else:
                b["valid"] = False
            self.assertEqual(evaluate_candidate(a, b, make_evaluation_spec(1, 40))["decision"], "invalid")

    def test_positive_relative_result_alone_does_not_beat_cash(self):
        result = evaluate_candidate(daily_metrics([-.003] * 40), daily_metrics([-.001] * 40), make_evaluation_spec(1, 40))
        self.assertEqual(result["decision"], "keep")
        self.assertFalse(result["statistics"]["economic_pass"])

    def test_risk_and_recent_reversal_have_independent_vetoes(self):
        spec = make_evaluation_spec(1, 60)
        reversal = evaluate_candidate(daily_metrics([0.] * 60), daily_metrics([.004] * 40 + [-.001] * 20), spec)
        self.assertEqual(reversal["decision"], "keep")
        self.assertFalse(reversal["statistics"]["recent_pass"])
        risky = evaluate_candidate(daily_metrics([0.] * 60), daily_metrics([-.02] * 15 + [.012] * 45), spec)
        self.assertFalse(risky["statistics"]["risk_pass"])
        self.assertEqual(risky["decision"], "keep")

    def test_block_dependence_and_all_components_must_pass(self):
        spec = make_evaluation_spec(1, 40, bootstrap_repetitions=999)
        values = [.005] * 20 + [-.001] * 20
        components = {"candidate": values, "cash": [.01] * 40}
        bootstrap = paired_block_bootstrap(components, {"candidate": math.log1p(.01) / 40, "cash": 0}, spec)
        self.assertGreater(bootstrap["p_max"], spec["test_alpha"])
        self.assertGreater(bootstrap["minimum_p_resolution"], 0)
        self.assertEqual(bootstrap, paired_block_bootstrap(components, {"candidate": math.log1p(.01) / 40, "cash": 0}, spec))

    def test_campaigns_do_not_silently_reset_or_exhaust_lifetime_alpha(self):
        specs = [make_evaluation_spec(index) for index in (1, 5, 6, 1001)]
        self.assertEqual([(spec["campaign"], spec["campaign_trial"]) for spec in specs], [(1, 1), (1, 5), (2, 1), (201, 1)])
        self.assertTrue(all(spec["test_alpha"] == .02 for spec in specs))
        self.assertAlmostEqual(sum(make_evaluation_spec(index)["test_alpha"] for index in range(1, 6)), .1)
        self.assertNotEqual(specs[0]["id"], specs[2]["id"])

    def test_mc_precision_is_frozen_and_parallel_candidates_share_window_budget(self):
        one = make_evaluation_spec(1)
        two = [make_evaluation_spec(1, candidate_count=2, candidate_index=i) for i in (1, 2)]
        self.assertEqual(one['mc_error'], .005)
        self.assertEqual(two[0]['mc_error'], .0025)
        self.assertGreater(two[0]['bootstrap_repetitions'], one['bootstrap_repetitions'])
        self.assertEqual(two[0]['bootstrap_repetitions'], two[0]['planned_bootstrap_repetitions'])
        self.assertEqual(sum(item['test_alpha'] for item in two), one['window_alpha'])
        self.assertEqual(two[0]['seed'], two[1]['seed'])
        self.assertNotEqual(two[0]['id'], two[1]['id'])
        changed = {**one, 'mc_error': .01}
        self.assertEqual(evaluate_candidate(daily_metrics([0.] * 60), daily_metrics([.001] * 60), changed)['decision'], 'invalid')

    def test_mc_interval_crossing_boundary_cannot_promote_even_below_point_threshold(self):
        a, b, spec = daily_metrics([0.] * 60), daily_metrics([.001] * 60), make_evaluation_spec(1)
        with patch('backend.learning_evaluation.paired_block_bootstrap', return_value={
                'p_max': .019, 'mc_lower_max': .015, 'mc_upper_max': .024}):
            result = evaluate_candidate(a, b, spec)
        self.assertEqual(result['decision'], 'keep')
        self.assertTrue(result['statistics']['mc_uncertain'])

    def test_one_percent_boundary_itself_is_not_sufficient_improvement(self):
        spec = make_evaluation_spec(1)
        for level in (0., .0001, .001, .005, .05):
            result = evaluate_candidate(daily_metrics([level] * 60),
                                        daily_metrics([level + math.log1p(.01) / 60] * 60), spec)
            self.assertEqual(result['decision'], 'keep')

    def test_robust_evaluation_requires_all_frozen_scenarios_and_execution_support(self):
        spec = make_evaluation_spec(1, 40, scenario_ids=['base', 'adverse'], bootstrap_repetitions=199)
        a, b = daily_metrics([0.] * 40), daily_metrics([.001] * 40)
        scenarios = [{'id': key, 'incumbent': deepcopy(a), 'candidate': deepcopy(b)} for key in spec['scenario_ids']]
        original = deepcopy(scenarios)
        supported = {'sufficient': True, 'reasons': []}
        result = evaluate_scenarios(scenarios, spec, support=supported)
        self.assertEqual(result['decision'], 'promote')
        self.assertEqual(scenarios, original)
        self.assertEqual(evaluate_scenarios(scenarios, spec,
            support={'sufficient': False, 'reasons': ['unseen_sell_liquidity']})['decision'], 'keep')
        self.assertEqual(evaluate_scenarios(scenarios[:1], spec, support=supported)['decision'], 'invalid')
        scenarios[1]['candidate'] = daily_metrics([-.001] * 40)
        self.assertEqual(evaluate_scenarios(scenarios, spec, support=supported)['decision'], 'keep')
        scenarios[1]['candidate'] = {'valid': False, 'error': 'execution_quote_missing'}
        self.assertEqual(evaluate_scenarios(scenarios, spec, support=supported)['decision'], 'invalid')

    def test_robust_scenarios_cannot_use_different_valid_date_windows(self):
        spec = make_evaluation_spec(1, 40, scenario_ids=['base', 'late'], bootstrap_repetitions=199)
        scenarios = [{'id': key, 'incumbent': daily_metrics([0.] * 40, first_day=day),
                      'candidate': daily_metrics([.001] * 40, first_day=day)}
                     for key, day in [('base', '2020-01-01'), ('late', '2020-02-01')]]
        self.assertEqual(evaluate_scenarios(scenarios, spec, support={'sufficient': True})['decision'], 'invalid')

    def test_synthetic_loop_consumes_invalid_slots_without_immediate_retry(self):
        from scripts.validate_learning import validate_loops
        path = daily_metrics([0.] * 40)
        truth = {'beneficial': False, 'post_regime_effect': None}
        with patch("scripts.validate_learning._trial", return_value=(path, path, 0., 0., 0., True, truth)) as trial:
            report = validate_loops(histories=2, trials=5, repetitions=199, horizons=(40,))
        self.assertEqual(trial.call_count, 2 * 5 * 3)
        self.assertIsNone(report["selected_horizon"])
        for scenario in report["outcomes"]["40"].values():
            self.assertEqual(scenario["invalid_slots_consumed"], 10)
            self.assertEqual(scenario["any_false_switch_rate"], 0)


if __name__ == "__main__":
    unittest.main()
