"""Small, deterministic tests of the offline serial/parallel experiment."""
import unittest
import math
from unittest.mock import patch

from scripts.validate_learning import (_comparison_history, _comparison_window,
                                       _conditional_truth, _history_cluster_interval,
                                       _paired_interval, compare_parallel,
                                       validate_loops, COMPARISON_SCENARIOS)


class ParallelValidationTests(unittest.TestCase):
    def test_development_priority_is_shared_and_future_does_not_rank_candidates(self):
        a = _comparison_window(3107, 1, 'positive', 0.)
        b = _comparison_window(3107, 1, 'positive', .5)
        self.assertEqual(a['ranked'], b['ranked'])
        self.assertEqual(a['candidate_noise'], b['candidate_noise'])
        self.assertEqual(len(a['ranked']), 16)
        self.assertEqual(set(a['ranked']), set(range(16)))
        self.assertEqual(len(a['candidate_noise'][0]), 60)

    def test_all_invalid_candidates_consume_windows_and_keep_their_alpha(self):
        data = _comparison_window(3107, 1, 'positive', 0.)
        data['invalid'] = [True] * 16
        data['qualities'] = [.0006] * 16
        with patch('scripts.validate_learning._comparison_window', return_value=data) as generated, \
                patch('scripts.validate_learning.evaluate_candidate', side_effect=AssertionError('invalid candidate evaluated')):
            result = _comparison_history(99, 5, 'positive', 2, 199)
        self.assertEqual(generated.call_count, 5)
        self.assertEqual(result['invalid_slots'], 10)
        self.assertEqual(result['next_window'], 6)
        self.assertEqual(result['discovery_days'], 360)
        self.assertEqual(result['switches'], 0)
        self.assertEqual(result['beneficial_scheduled'], 10)
        self.assertEqual(result['beneficial_valid'], 0)
        self.assertEqual(result['beneficial_detected'], 0)

    def test_both_pass_uses_frozen_priority_and_restart_keeps_window_number(self):
        data = _comparison_window(3107, 1, 'positive', 0.)
        data['ranked'][:2] = [1, 0]
        data['invalid'] = [False] * 16
        data['qualities'][1], data['qualities'][0] = .0007, .001
        seen = []
        def evaluate(a, b, spec):
            seen.append((spec['sequence'], spec['candidate_index'], spec['test_alpha']))
            return {'decision': 'promote', 'statistics': {}}
        with patch('scripts.validate_learning._comparison_window', return_value=data), \
                patch('scripts.validate_learning.evaluate_candidate', side_effect=evaluate):
            result = _comparison_history(99, 5, 'positive', 2, 199)
        self.assertEqual(result['quality'], .0007)
        self.assertEqual(result['first_discovery'], 60)
        self.assertEqual(seen, [(window, index, .01) for window in range(1, 6) for index in (1, 2)])

    def test_exact_one_percent_null_boundary_is_not_counted_as_true_discovery(self):
        data = _comparison_window(3107, 1, 'boundary', 0.)
        data['invalid'] = [False] * 16
        with patch('scripts.validate_learning._comparison_window', return_value=data), \
                patch('scripts.validate_learning.evaluate_candidate', return_value={'decision': 'promote', 'statistics': {}}):
            result = _comparison_history(99, 5, 'boundary', 1, 199)
        self.assertIsNone(result['first_discovery'])
        self.assertEqual(result['false_switches'], 5)
        self.assertEqual(result['campaign_false_rate'], 1)

    def test_boundary_removes_selected_ar_forecast_mean_not_the_observed_future(self):
        a = _comparison_window(3107, 1, 'boundary', 0.)
        b = _comparison_window(3107, 1, 'boundary', .01)
        expected_change = -math.sqrt(1 - .15) * .01 * sum(.35 ** day for day in range(1, 61)) / 60
        for first, later in zip(a['conditional_noise_shifts'], b['conditional_noise_shifts']):
            self.assertAlmostEqual(later - first, expected_change)
            delta = math.log1p(.01) / 60
            self.assertAlmostEqual((delta - first) + first, delta)
        self.assertEqual(a['ranked'], b['ranked'])
        self.assertEqual(a['candidate_noise'], b['candidate_noise'])

    def test_forced_boundary_promotion_carries_corrected_quality_and_noise(self):
        generated = []
        def window(seed, sequence, scenario, prior_noise, horizon):
            data = _comparison_window(seed, sequence, scenario, prior_noise, horizon)
            data['invalid'] = [False] * 16
            generated.append((prior_noise, data))
            return data
        with patch('scripts.validate_learning._comparison_window', side_effect=window), \
                patch('scripts.validate_learning.evaluate_candidate', return_value={'decision': 'promote', 'statistics': {}}):
            result = _comparison_history(99, 5, 'boundary', 1, 199)
        quality, noise = 0., 0.
        for prior_noise, data in generated:
            self.assertEqual(prior_noise, noise)
            index = data['ranked'][0]
            quality += math.log1p(.01) / 60 - data['conditional_noise_shifts'][index]
            noise = data['last_noise'][index]
        self.assertAlmostEqual(result['quality'], quality)
        self.assertEqual(result['noise'], noise)
        self.assertEqual(result['false_switches'], 5)
        self.assertEqual(result['beneficial_scheduled'], 0)

    def test_reversal_mean_improvement_is_distinct_from_post_regime_damage(self):
        truth = _conditional_truth(60, .0006, 0., 0., 0., 0., reversal_day=40)
        self.assertAlmostEqual(truth['relative_daily_log'], .0002)
        self.assertTrue(truth['beneficial'])
        self.assertAlmostEqual(truth['post_regime_effect'], -.0006)
        data = _comparison_window(3107, 1, 'reversal', 0.)
        data['ranked'] = list(range(16))
        data['invalid'] = [False] * 16
        data['initial_state'].update(market=0., common=0., candidate_noise=[0.] * 16, incumbent_noise=0.)
        with patch('scripts.validate_learning._comparison_window', return_value=data), \
                patch('scripts.validate_learning.evaluate_candidate', return_value={'decision': 'promote', 'statistics': {}}):
            result = _comparison_history(99, 5, 'reversal', 1, 199)
        self.assertEqual(result['false_switches'], 0)
        self.assertEqual(result['beneficial_detected'], 5)
        self.assertEqual(result['first_discovery'], 60)
        self.assertEqual(result['post_regime_count'], 5)
        self.assertEqual(result['post_regime_negative'], 1)
        self.assertAlmostEqual(result['quality'], -.0006)

    def test_conditional_truth_uses_selected_ar_forecast_and_cash_hypothesis(self):
        ar_improvement = _conditional_truth(60, 0., 0., .03, 0., 0.)
        self.assertTrue(ar_improvement['beneficial'])
        self.assertAlmostEqual(ar_improvement['relative_daily_log'], .03 * sum(.35 ** day for day in range(1, 61)) / 60)
        cash_loss = _conditional_truth(60, 0., 0., .03, 0., -.15)
        self.assertGreater(cash_loss['relative_daily_log'], math.log1p(.01) / 60)
        self.assertFalse(cash_loss['beneficial'])
        self.assertLess(cash_loss['cash_daily_log'], 0.)
        stressed_cash_loss = _conditional_truth(60, -.00048, -.001, 0., 0., 0.)
        self.assertGreater(stressed_cash_loss['cash_daily_log'], 0.)
        self.assertFalse(stressed_cash_loss['beneficial'])
        self.assertLess(stressed_cash_loss['stress_cash_daily_log'], 0.)

    def test_overlap_forecasts_cancel_first_twenty_days_only_against_incumbent(self):
        truth = _conditional_truth(60, .0002, 0., 1., 0., .02, common=.03,
                                   common_scale=.5, own_scale=.8, overlap_days=20)
        first = sum(.35 ** day for day in range(1, 21))
        rest = sum(.35 ** day for day in range(21, 61))
        self.assertAlmostEqual(truth['relative_daily_log'], (40 * .0002 + .8 * rest) / 60)
        self.assertAlmostEqual(truth['cash_daily_log'], .0005 + (first * (.02 + .03) +
                               rest * (.02 + .5 * .03 + .8) + 40 * .0002) / 60)
        self.assertFalse(truth['beneficial'])

    def test_legacy_reversal_uses_same_truth_and_carries_post_regime_quality(self):
        regimes = []
        def trial(rng, horizon, scenario, incumbent_quality, incumbent_noise):
            quality = .0006
            truth = _conditional_truth(horizon, quality, incumbent_quality, 0., 0., 0.,
                                       reversal_day=40 if scenario == 'regime' else None)
            if scenario == 'regime':
                regimes.append((incumbent_quality, incumbent_noise))
            return ({}, {}, quality, .001, .002, False, truth)
        with patch('scripts.validate_learning._trial', side_effect=trial), \
                patch('scripts.validate_learning.evaluate_candidate', return_value={'decision': 'promote'}):
            result = validate_loops(histories=1, trials=5, repetitions=199, horizons=(60,))
        regime = result['outcomes']['60']['regime']
        self.assertEqual(regime['false_switches'], 0)
        self.assertEqual(regime['beneficial_scheduled'], 5)
        self.assertEqual(regime['post_regime']['negative_effects'], 1)
        self.assertEqual(regimes, [(0., 0.)] + [(-.0006, .002)] * 4)

    def test_no_evidence_and_screening_runs_cannot_enable_parallel(self):
        def history(seed, windows, scenario, count, repetitions):
            return {'campaign_false_rate': 0., 'false_campaigns': [], 'discovery_days': 360,
                    'first_discovery': None, 'invalid_slots': 0, 'mc_uncertain': 0,
                    'beneficial_scheduled': 0, 'beneficial_valid': 0, 'beneficial_detected': 0,
                    'post_regime_count': 0, 'post_regime_negative': 0, 'post_regime_effect_sum': 0.}
        with patch('scripts.validate_learning._comparison_history', side_effect=history):
            result = compare_parallel(histories=2, windows=5, repetitions=199)
        self.assertFalse(result['comparison_conditions_pass'])
        self.assertFalse(result['production_mc'])
        self.assertEqual(set(result['outcomes']), set(COMPARISON_SCENARIOS))
        self.assertEqual(result['rule']['minimum_discovery_days_saved'], 6)
        with self.assertRaises(ValueError):
            compare_parallel(histories=1, windows=5)

    def test_zero_observations_preserve_risk_and_paired_difference_uncertainty(self):
        upper = 1 - .025 ** (1 / 32)
        self.assertEqual(_history_cluster_interval([0.] * 32), [0., upper])
        self.assertEqual(_paired_interval([0.] * 32), [-upper, upper])
        self.assertGreater(upper, .10)
        # Do not round a slightly failing bound into an adoption threshold.
        self.assertGreater(_history_cluster_interval([0.] * 35)[1], .10)

    def test_identical_interior_savings_do_not_prove_a_precise_population_mean(self):
        self.assertEqual(_paired_interval([10.] * 32, bounds=(-600., 600.)), [-600., 600.])

    def test_discovery_rule_requires_six_days_in_confidence_lower_bound(self):
        def history(seed, windows, scenario, count, repetitions):
            return {'campaign_false_rate': 0., 'false_campaigns': [],
                    'discovery_days': 360 if count == 1 else 350, 'first_discovery': None,
                    'invalid_slots': 0, 'mc_uncertain': 0,
                    'beneficial_scheduled': 0, 'beneficial_valid': 0, 'beneficial_detected': 0,
                    'post_regime_count': 0, 'post_regime_negative': 0, 'post_regime_effect_sum': 0.}
        for lower, expected in ((1., False), (6., True)):
            def interval(values, *, bounds=(-1., 1.)):
                return [0., .01] if bounds == (-1., 1.) else [lower, 20.]
            with self.subTest(lower=lower), \
                    patch('scripts.validate_learning._comparison_history', side_effect=history), \
                    patch('scripts.validate_learning._paired_interval', side_effect=interval):
                result = compare_parallel(histories=2, windows=5, repetitions=199)
                self.assertEqual(result['outcomes']['positive']['days_saved'], 10.)
                self.assertEqual(result['discovery_pass'], expected)

    def test_declared_or_reviewed_development_seeds_cannot_support_new_validation(self):
        def history(seed, windows, scenario, count, repetitions):
            return {'campaign_false_rate': 0., 'false_campaigns': [],
                    'discovery_days': 360 if count == 1 else 350, 'first_discovery': None,
                    'invalid_slots': 0, 'mc_uncertain': 0,
                    'beneficial_scheduled': 0, 'beneficial_valid': 0, 'beneficial_detected': 0,
                    'post_regime_count': 0, 'post_regime_negative': 0, 'post_regime_effect_sum': 0.}
        def interval(values, *, bounds=(-1., 1.)):
            return [0., .01] if bounds == (-1., 1.) else [6., 20.]
        for seed, development, separated in ((1234, (1234,), False), (941027, (), False), (5678, (1234,), True)):
            with self.subTest(seed=seed), \
                    patch('scripts.validate_learning._comparison_history', side_effect=history), \
                    patch('scripts.validate_learning._history_cluster_interval', return_value=[0., .05]), \
                    patch('scripts.validate_learning._paired_interval', side_effect=interval):
                result = compare_parallel(histories=2, windows=5, seed=seed, development_seeds=development)
                self.assertEqual(result['seed_separated_from_declared_development'], separated)
                self.assertEqual(result['comparison_conditions_pass'], separated)
                self.assertNotIn('adopt_parallel', result)
                self.assertNotIn('held_out_seed', result)
                self.assertEqual(len(result['source_sha256']), 64)


if __name__ == '__main__':
    unittest.main()
