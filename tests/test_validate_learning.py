"""Small, deterministic tests of the offline serial/parallel experiment."""
import unittest
import math
from unittest.mock import patch

from scripts.validate_learning import (_comparison_history, _comparison_window,
                                       compare_parallel, COMPARISON_SCENARIOS)


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
        data = _comparison_window(3107, 1, 'null', 0.)
        data['invalid'] = [True] * 16
        with patch('scripts.validate_learning._comparison_window', return_value=data) as generated, \
                patch('scripts.validate_learning.evaluate_candidate', side_effect=AssertionError('invalid candidate evaluated')):
            result = _comparison_history(99, 5, 'null', 2, 199)
        self.assertEqual(generated.call_count, 5)
        self.assertEqual(result['invalid_slots'], 10)
        self.assertEqual(result['next_window'], 6)
        self.assertEqual(result['discovery_days'], 360)
        self.assertEqual(result['switches'], 0)

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

    def test_no_evidence_and_screening_runs_cannot_enable_parallel(self):
        def history(seed, windows, scenario, count, repetitions):
            return {'campaign_false_rate': 0., 'false_campaigns': [], 'discovery_days': 360,
                    'first_discovery': None, 'invalid_slots': 0, 'mc_uncertain': 0}
        with patch('scripts.validate_learning._comparison_history', side_effect=history):
            result = compare_parallel(histories=2, windows=5, repetitions=199)
        self.assertFalse(result['adopt_parallel'])
        self.assertFalse(result['production_mc'])
        self.assertEqual(set(result['outcomes']), set(COMPARISON_SCENARIOS))
        self.assertEqual(result['rule']['minimum_discovery_days_saved'], 6)
        with self.assertRaises(ValueError):
            compare_parallel(histories=1, windows=5)


if __name__ == '__main__':
    unittest.main()
