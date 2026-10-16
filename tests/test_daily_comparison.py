"""Frozen replay anchors and after-activation diagnostics; no live account calls."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.daily_comparison import (ensure_tables, evaluation_context, freeze_context,
                                     followup_summaries, observation_books, update_followups)
from backend.experiment_store import ExperimentStore, encode
from backend.learning_policy import BASELINE_POLICY, LEGACY_IDS, portfolio_metrics
from tests.test_learning_policy import LIMITS, frames, policy


class DailyComparisonTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = ExperimentStore(Path(directory.name) / 'experiment.sqlite3')
        ensure_tables(self.store)
        self.frames = frames(4)
        self.day = self.frames[0]['as_of']
        self.book = {'as_of': self.day, 'cash': '10000', 'reserved_cash': '0',
                     'positions': [], 'pending_orders': [], 'unresolved_orders': []}
        self.model = {'mode': 'daily_open'}
        self.observed = self.day + 'T16:05:00+09:00'
        self.version = 'test-engine-v1'

    def freeze(self, *, fingerprint='a', frame=None, book=None, model=None, limits=None,
               observed=None, version=None):
        freeze_context(self.store, fingerprint, frame or self.frames[0], book or self.book,
                       model or self.model, limits or LIMITS, observed or self.observed, version or self.version)

    def contexts(self):
        with self.store.connect() as db:
            return [tuple(row) for row in db.execute('SELECT fingerprint,day,payload FROM daily_policy_contexts ORDER BY fingerprint,day')]

    def record(self):
        candidate = policy(name='candidate')
        candidate['strategies'][0]['holding_sessions'] = 1
        return {'id': 'daily-1', 'fingerprint': 'a', 'effective_day': self.frames[1]['as_of'],
                'adopted_at': self.frames[1]['as_of'] + 'T09:05:00+09:00', 'engine_version': self.version,
                'comparison_seed': {'signal_day': self.day, 'observed_at': self.observed,
                    'initial_state': deepcopy(self.book), 'execution_model': deepcopy(self.model),
                    'limits': deepcopy(LIMITS), 'parent_policy': policy(name='unchanged'),
                    'candidate_policy': candidate}}

    def save(self, record):
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO daily_policy_followups VALUES (?,?,?)',
                       (record['id'], record['fingerprint'], encode(record)))

    def stored(self):
        with self.store.connect() as db:
            return json.loads(db.execute('SELECT payload FROM daily_policy_followups WHERE id=?', ('daily-1',)).fetchone()[0])

    def test_first_settled_close_is_immutable_including_book_model_and_version(self):
        original = deepcopy((self.book, self.model, LIMITS))
        self.freeze()
        saved = self.contexts()
        self.freeze(book={**self.book, 'cash': '9000'}, model={'mode': 'daily_open', 'buy_fill_ratio': .5},
                    version='changed-engine')
        self.assertEqual(self.contexts(), saved)
        self.assertEqual((self.book, self.model, LIMITS), original)

    def test_only_same_day_after_close_settled_aware_observations_can_freeze(self):
        for observed in (self.day + 'T15:59:59+09:00', self.day + 'T16:05:00',
                         self.frames[1]['as_of'] + 'T16:05:00+09:00', 'invalid'):
            self.freeze(observed=observed)
            self.assertEqual(self.contexts(), [])
        for key in ('pending_orders', 'unresolved_orders'):
            self.freeze(book={**self.book, key: ['unsettled']})
            self.assertEqual(self.contexts(), [])
        self.freeze(book={**self.book, 'as_of': self.frames[1]['as_of']})
        self.assertEqual(self.contexts(), [])
        self.freeze(observed=self.day + 'T07:00:00+00:00')  # 16:00 KST boundary.
        self.assertEqual(len(self.contexts()), 1)

    def test_anchor_lookup_separates_account_version_limits_and_requires_future_interval(self):
        self.freeze()
        self.freeze(fingerprint='b', book={**self.book, 'cash': '8000'})
        self.assertIsNone(evaluation_context(self.store, 'a', self.frames[:1], LIMITS, self.version))
        self.assertIsNone(evaluation_context(self.store, 'other', self.frames, LIMITS, self.version))
        self.assertIsNone(evaluation_context(self.store, 'a', self.frames, LIMITS, 'other-version'))
        self.assertIsNone(evaluation_context(self.store, 'a', self.frames, {**LIMITS, 'budget': '20000'}, self.version))
        found = evaluation_context(self.store, 'a', self.frames, LIMITS, self.version)
        self.assertEqual(found['initial_state'], self.book)
        self.assertEqual(evaluation_context(self.store, 'b', self.frames, LIMITS, self.version)['initial_state']['cash'], '8000')
        found['initial_state']['cash'] = '1'
        self.assertEqual(evaluation_context(self.store, 'a', self.frames, LIMITS, self.version)['initial_state']['cash'], '10000')

    def test_current_book_is_not_retroactively_assigned_to_older_frames(self):
        latest = self.frames[-1]
        self.freeze(frame=latest, book={**self.book, 'as_of': latest['as_of']}, observed=latest['observed_at'])
        self.assertIsNone(evaluation_context(self.store, 'a', self.frames, LIMITS, self.version))
        self.freeze()
        selected = evaluation_context(self.store, 'a', self.frames, LIMITS, self.version)
        self.assertEqual(selected['initial_state']['as_of'], self.frames[0]['as_of'])

    def test_observation_books_separates_account_engine_and_every_limit(self):
        self.freeze()
        other_book = {**self.book, 'cash': '8000'}
        self.freeze(fingerprint='b', book=other_book)
        for fingerprint in (None, '', 'other'):
            with self.subTest(fingerprint=fingerprint):
                self.assertEqual(observation_books(self.store, fingerprint, self.day, LIMITS, self.version), [])
        self.assertEqual(observation_books(self.store, 'a', self.day, LIMITS, 'other-version'), [])
        for key in ('budget', 'order_cap', 'daily_buy_limit'):
            with self.subTest(limit=key):
                changed = {**LIMITS, key: str(int(LIMITS[key]) + 1)}
                self.assertEqual(observation_books(self.store, 'a', self.day, changed, self.version), [])
        numeric_limits = {key: int(value) for key, value in LIMITS.items()}
        self.assertEqual(observation_books(self.store, 'a', self.day, numeric_limits, self.version), [self.book])
        self.assertEqual(observation_books(self.store, 'b', self.day, LIMITS, self.version), [other_book])

    def test_observation_books_includes_same_day_but_excludes_future_and_incompatible_contexts(self):
        self.freeze()
        for index, changes in ((1, {'version': 'old-engine'}),
                               (2, {'limits': {**LIMITS, 'budget': '20000'}}), (3, {})):
            frame = self.frames[index]
            self.freeze(frame=frame, book={**self.book, 'as_of': frame['as_of']},
                        observed=frame['observed_at'], **changes)
        saved = self.contexts()
        self.assertEqual(observation_books(self.store, 'a', '0001-01-01', LIMITS, self.version), [])
        self.assertEqual(observation_books(self.store, 'a', self.day, LIMITS, self.version), [self.book])
        self.assertEqual(observation_books(self.store, 'a', self.frames[2]['as_of'], LIMITS, self.version), [self.book])
        found = observation_books(self.store, 'a', self.frames[3]['as_of'], LIMITS, self.version)
        self.assertEqual([book['as_of'] for book in found], [self.frames[3]['as_of'], self.day])
        self.assertEqual(self.contexts(), saved)

    def test_observation_books_are_deep_copies_and_keep_original_payloads(self):
        book = {**deepcopy(self.book),
                'positions': [{'symbol': '005930', 'quantity': 2,
                               'exit_policy': {'holding_sessions': 5, 'stop_loss_pct': 0}}],
                'reserved_entries': [{'symbol': '000660', 'quantity': 1}],
                'pending_entries': [{'symbol': '035420', 'quantity': 1}],
                'account_protection': {'units': '10000', 'peak': '1.1', 'active': False, 'limit_pct': 15}}
        original = deepcopy(book)
        self.freeze(book=book)
        frame = self.frames[1]
        self.freeze(frame=frame, book={**self.book, 'as_of': frame['as_of']},
                    observed=frame['observed_at'], version='old-engine')
        saved = self.contexts()
        found = observation_books(self.store, 'a', frame['as_of'], LIMITS, self.version)
        self.assertEqual(found, [original])
        found[0]['positions'][0]['exit_policy']['holding_sessions'] = 1
        found[0]['reserved_entries'].clear()
        found[0]['pending_entries'][0]['symbol'] = '000001'
        found[0]['account_protection']['peak'] = '99'
        found.append({'positions': []})
        self.assertEqual(observation_books(self.store, 'a', frame['as_of'], LIMITS, self.version), [original])
        self.assertEqual(self.contexts(), saved)
        self.assertEqual(book, original)

    def test_observation_books_retains_frozen_holding_after_later_actual_book_is_empty(self):
        frozen = {**deepcopy(self.book), 'positions': [{'symbol': '005930', 'quantity': 2,
                    'exit_policy': {'holding_sessions': 5, 'stop_loss_pct': 0, 'take_profit_pct': 0}}]}
        self.freeze(book=frozen)
        later = self.frames[1]
        sold_book = {**deepcopy(self.book), 'as_of': later['as_of']}
        self.freeze(frame=later, book=sold_book, observed=later['observed_at'])
        saved = self.contexts()
        found = observation_books(self.store, 'a', later['as_of'], LIMITS, self.version)
        self.assertEqual(found, [sold_book, frozen])
        self.assertEqual({item['symbol'] for book in found for item in book['positions']}, {'005930'})
        self.assertEqual(found[0]['positions'], [])
        self.assertEqual(self.contexts(), saved)

    def test_followup_matches_direct_paired_replay_and_preserves_immutable_seed(self):
        record = self.record()
        self.save(record)
        expected = [portfolio_metrics(record['comparison_seed'][key], self.frames, LIMITS,
                    initial_state=self.book, execution_model=self.model)
                    for key in ('parent_policy', 'candidate_policy')]
        update_followups(self.store, 'a', self.frames, self.version)
        saved = self.stored()
        self.assertEqual(saved['comparison_seed'], record['comparison_seed'])
        comparison = saved['comparison']
        self.assertEqual(comparison['status'], 'ready')
        self.assertEqual(comparison['basis'], 'post_activation_paired_model')
        self.assertEqual(comparison['intervals'], 3)
        self.assertAlmostEqual(comparison['delta_net_return_pp'], expected[1]['net_return_pct'] - expected[0]['net_return_pct'])
        self.assertEqual(comparison['candidate']['costs']['fees_paid'], expected[1]['diagnostics']['normal']['fees_paid'])
        with patch('backend.daily_comparison.portfolio_metrics', side_effect=AssertionError('unchanged evidence')):
            update_followups(self.store, 'a', self.frames, self.version)
        self.assertEqual(self.stored(), saved)
        self.assertEqual(followup_summaries(self.store, 'b'), [])
        summary = followup_summaries(self.store, 'a')[0]
        self.assertNotIn('comparison_seed', summary)
        self.assertNotIn('initial_state', summary)

    def test_anchor_missing_or_no_interval_never_retains_an_old_ready_result(self):
        self.save(self.record())
        update_followups(self.store, 'a', self.frames, self.version)
        update_followups(self.store, 'a', self.frames[1:], self.version)
        self.assertEqual(self.stored()['comparison']['error'], 'comparison_anchor_missing')
        update_followups(self.store, 'a', self.frames[:1], self.version)
        self.assertEqual(self.stored()['comparison']['status'], 'waiting')
        self.assertNotIn('candidate', self.stored()['comparison'])

    def test_activation_session_and_engine_mismatch_are_unavailable(self):
        record = self.record()
        record['effective_day'] = self.frames[2]['as_of']
        self.save(record)
        update_followups(self.store, 'a', self.frames, self.version)
        self.assertEqual(self.stored()['comparison']['error'], 'activation_session_mismatch')
        self.save(self.record())
        update_followups(self.store, 'a', self.frames, 'new-engine')
        self.assertEqual(self.stored()['comparison']['error'], 'evaluation_version_changed')

    def test_seed_time_book_and_activation_date_are_checked_before_replay(self):
        for kind in ('before_close', 'at_open', 'naive', 'after_activation', 'book_day', 'pending', 'activation_day'):
            record = self.record()
            seed = record['comparison_seed']
            if kind == 'before_close':
                seed['observed_at'] = self.day + 'T15:59:59+09:00'
            elif kind == 'at_open':
                seed['observed_at'] = self.frames[1]['as_of'] + 'T09:00:00+09:00'
            elif kind == 'naive':
                seed['observed_at'] = self.day + 'T16:05:00'
            elif kind == 'after_activation':
                seed['observed_at'] = self.frames[1]['as_of'] + 'T09:06:00+09:00'
            elif kind == 'book_day':
                seed['initial_state']['as_of'] = self.frames[1]['as_of']
            elif kind == 'pending':
                seed['initial_state']['pending_orders'] = ['unsettled']
            else:
                record['adopted_at'] = self.frames[2]['as_of'] + 'T09:05:00+09:00'
            self.save(record)
            with patch('backend.daily_comparison.portfolio_metrics') as evaluate:
                update_followups(self.store, 'a', self.frames, self.version)
                evaluate.assert_not_called()
            self.assertEqual(self.stored()['comparison']['status'], 'unavailable', kind)

    def test_activation_metadata_change_cannot_reuse_old_cached_result(self):
        self.save(self.record())
        update_followups(self.store, 'a', self.frames, self.version)
        record = self.stored()
        record['adopted_at'] = self.frames[2]['as_of'] + 'T09:05:00+09:00'
        self.save(record)
        update_followups(self.store, 'a', self.frames, self.version)
        self.assertEqual(self.stored()['comparison']['error'], 'activation_date_mismatch')

    def test_missing_internal_frame_and_replay_failure_are_explicit(self):
        self.save(self.record())
        update_followups(self.store, 'a', [self.frames[0], self.frames[1], self.frames[3]], self.version)
        self.assertEqual(self.stored()['comparison']['error'], 'missing_session')
        with patch('backend.daily_comparison.portfolio_metrics', side_effect=RuntimeError('private exception details')):
            update_followups(self.store, 'a', self.frames, self.version)
        self.assertEqual(self.stored()['comparison']['error'], 'comparison_evaluation_failed')
        self.assertNotIn('private', json.dumps(self.stored()['comparison']))

    def test_profile_summarizes_every_paired_scenario_and_retains_representative_identity(self):
        record = self.record()
        record['comparison_seed']['execution_model'] = {'scenarios': [{'id': 'base'}, {'id': 'adverse'}]}
        self.save(record)
        def result(net):
            return {'valid': True, 'net_return_pct': net, 'stress_return_pct': net - .1,
                    'max_drawdown_pct': 1, 'stress_drawdown_pct': 2, 'closed_trades': 1,
                    'diagnostics': {'normal': {'fees_paid': 10, 'taxes_paid': 20}}}
        left, right = result(1), result(2)
        left.update(representative_scenario='base', promotion_supported=True,
                    scenario_results={'base': result(1), 'adverse': result(.5)})
        right.update(representative_scenario='base', promotion_supported=False,
                     scenario_results={'base': result(2), 'adverse': result(-.5)})
        with patch('backend.daily_comparison.portfolio_metrics', side_effect=[left, right]):
            update_followups(self.store, 'a', self.frames, self.version)
        comparison = self.stored()['comparison']
        self.assertEqual(comparison['status'], 'ready')
        self.assertEqual(comparison['delta_net_return_pp'], 1)
        self.assertEqual(comparison['worst_delta_net_return_pp'], -1)
        self.assertEqual(comparison['best_delta_net_return_pp'], 1)
        self.assertEqual(comparison['representative_scenario'], 'base')
        self.assertEqual(comparison['scenario_count'], 2)
        self.assertFalse(comparison['execution_supported'])
        self.assertEqual(set(comparison['scenario_comparisons']), {'base', 'adverse'})
        from backend.learning_research import _public_feedback
        public = _public_feedback([{'forward_comparison': comparison}])[0]['forward_comparison']
        self.assertEqual(public['worst_delta_net_return_pp'], -1)
        self.assertEqual(public['scenario_count'], 2)
        self.assertNotIn('scenario_comparisons', public)

    def test_legacy_followup_needs_real_previous_signals_but_not_unexecuted_final_signals(self):
        record = self.record()
        record['comparison_seed']['parent_policy'] = deepcopy(BASELINE_POLICY)
        self.save(record)
        for frame in self.frames:
            frame['captured_at'] = frame['observed_at']
            frame['baseline_signals'] = [{'strategy_id': identity, 'symbol': '005930',
                                         'action': 'hold', 'status': 'ready'} for identity in LEGACY_IDS]
        self.frames[-1]['baseline_signals'] = []
        update_followups(self.store, 'a', self.frames, self.version)
        self.assertEqual(self.stored()['comparison']['status'], 'ready')
        for kind in ('missing', 'incomplete', 'late', 'development', 'no_capture'):
            observed = deepcopy(self.frames)
            if kind == 'missing':
                observed[1]['baseline_signals'] = []
            elif kind == 'incomplete':
                observed[1]['baseline_signals'].pop()
            elif kind == 'late':
                observed[1]['captured_at'] = observed[2]['as_of'] + 'T09:00:00+09:00'
            elif kind == 'development':
                observed[1]['role'] = 'development_only'
            else:
                observed[1].pop('captured_at')
            with patch('backend.daily_comparison.portfolio_metrics') as replay:
                update_followups(self.store, 'a', observed, self.version)
                replay.assert_not_called()
            self.assertEqual(self.stored()['comparison']['error'], 'legacy_baseline_signals_unavailable', kind)


if __name__ == '__main__':
    unittest.main()
