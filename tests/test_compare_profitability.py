"""Research reproducibility, read-only access, and diagnostic integrity."""
from copy import deepcopy
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.compare_profitability import (add_buy_cost_instrumentation, digest,
    paired_delta, raw_frame, read_json, readonly, sensitivity_terms, strip_options, write_json)


class ComparisonEvidenceTests(unittest.TestCase):
    def test_readonly_connection_cannot_mutate_the_input_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE source(value INTEGER)')
                db.execute('INSERT INTO source VALUES (7)')
            db = readonly(path)
            try:
                self.assertEqual(db.execute('SELECT value FROM source').fetchone()[0], 7)
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute('UPDATE source SET value=8')
            finally:
                db.close()

    def test_frozen_compressed_json_is_content_deterministic(self):
        value = {'as_of': '2026-10-12', 'value': ['한글', 1.5, None]}
        with tempfile.TemporaryDirectory() as directory:
            a, b = (Path(directory) / name for name in ('a.json.gz', 'b.json.gz'))
            write_json(a, value)
            write_json(b, value)
            self.assertEqual(a.read_bytes(), b.read_bytes())
            self.assertEqual(read_json(a), value)
            self.assertEqual(digest(value), digest(read_json(b)))

    def test_raw_frame_truncates_every_input_to_decision_close(self):
        source = {'calendars': {'KOSPI': ['2026-10-07', '2026-10-08', '2026-10-12'],
                                'KOSDAQ': ['2026-10-07', '2026-10-08', '2026-10-12']},
                  'histories': {'005930': {'2026-10-07': {'close': 100}, '2026-10-08': {'close': 101},
                                            '2026-10-12': {'close': 99999}}},
                  'benchmarks': {'KOSPI': {'2026-10-07': 1000, '2026-10-08': 1001, '2026-10-12': 99999}}}
        frame = {'as_of': '2026-10-08', 'rows': [{'symbol': '005930', 'board': 'KOSPI', 'name': 'A', 'learning_eligible': True}]}
        original = deepcopy(source)
        result = raw_frame(source, frame)
        self.assertEqual(result['calendars']['KOSPI'], ['2026-10-07', '2026-10-08'])
        self.assertNotIn('2026-10-12', result['histories']['005930'])
        self.assertNotIn('2026-10-12', result['benchmarks']['KOSPI'])
        self.assertEqual(source, original)

    def test_baseline_options_removed_without_changing_core_policy(self):
        policy = {'kind': 'rules', 'allocation_mode': 'score_rank', 'candidate_scope': 'liquid_all',
                  'strategies': [{'id': 'x', 'holding_sessions': 5, 'exit_mode': 'signal_extension',
                                  'max_holding_sessions': 20}]}
        original = deepcopy(policy)
        result = strip_options(policy)
        self.assertEqual(result, {'kind': 'rules', 'strategies': [{'id': 'x', 'holding_sessions': 5}]})
        self.assertEqual(policy, original)

    def test_missing_or_invalid_simulations_cannot_be_reported_as_improvements(self):
        result = paired_delta({'valid': False, 'error': 'execution_quote_missing'}, {'valid': True})
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('net_return_pct_delta_pp', result)

    def test_cost_amendment_overrides_side_specific_fees_without_rewriting_frozen_plan(self):
        plan = {'timefolio_cost_sensitivity': {'fee_rate': .002, 'tax_rate': 0,
                                              'slippage_bps': 10, 'stress_slippage_bps': 20, 'note': 'original'}}
        original = deepcopy(plan)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            amendment = {'execution_model': {'fee_rate': .001, 'tax_rate': .002,
                                             'slippage_bps': 10, 'stress_slippage_bps': 20},
                         'note': 'Buy fee 0.1%; sell fee 0.1% plus tax 0.2%.'}
            write_json(output / 'cost-model-amendment.json', amendment)
            model, note, source = sensitivity_terms(output, plan)
        self.assertEqual(model['fee_rate'], .001)
        self.assertEqual(model['fee_rate'] + model['tax_rate'], .003)
        self.assertAlmostEqual(2 * model['fee_rate'] + model['tax_rate'], .004)
        self.assertEqual(source, amendment)
        self.assertEqual(note, amendment['note'])
        self.assertEqual(plan, original)

    def test_baseline_trace_does_not_change_returns_or_positions(self):
        import backend.learning_policy as module
        policy = {'kind': 'rules', 'name': 'test', 'cash_reserve': 0., 'max_positions': 20,
                  'entry_slippage_bps': 100, 'cancel_after_minutes': 10,
                  'strategies': [{'id': 'x', 'name': 'x', 'weight': 1., 'conditions': [],
                                  'rank_by': 'return_5d_pct', 'descending': True, 'top_n': 1,
                                  'holding_sessions': 2, 'stop_loss_pct': 0., 'take_profit_pct': 0.}]}
        dates = ['2026-10-06', '2026-10-07', '2026-10-08', '2026-10-12']
        frames = [{'as_of': day, 'observed_at': day + 'T16:05:00+09:00', 'session_days': dates[:index + 1],
                   'rows': [{'symbol': '005930', 'name': 'A', 'board': 'KOSPI', 'open': 100,
                             'close': 100, 'high': 101, 'low': 99, 'volume': 10000,
                             'learning_eligible': index == 0, 'features': {'return_5d_pct': 1.}}],
                   'baseline_signals': []} for index, day in enumerate(dates)]
        limits = {'budget': '10000', 'order_cap': '10000', 'daily_buy_limit': '10000'}
        old_function = module._replay
        before = module.portfolio_metrics(policy, frames, limits)
        self.assertTrue(before['valid'], before['error'])
        try:
            add_buy_cost_instrumentation(module)
            after = module.portfolio_metrics(policy, frames, limits)
        finally:
            module._replay = old_function
        self.assertGreater(after['equity_curve'][1]['buy_cost'], 0)
        for result in (before, after):
            for key in ('equity_curve', 'stress_equity_curve'):
                for point in result[key]:
                    point.pop('buy_cost', None)
                    point.pop('buy_cost_exact', None)
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
