"""Daily research uses the production worker without rewriting frozen trials."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from backend.experiment_store import ExperimentStore
from backend.learning import LearningService
from tests.test_learning import LIMITS, candidate, raw_input


class DailyLearningTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = ExperimentStore(Path(directory.name) / 'test.sqlite3')
        self.data = raw_input(0)
        self.now = datetime.fromisoformat(self.data['observed_at'])
        self.calls = []
        self.store.save_settings({'policy': {**LIMITS, 'fingerprint': 'account'},
                                 'enabled': False, 'user_paused': True})
        def propose(parent, frames, limits, history, llm=None):
            self.calls.append(deepcopy((parent, frames, history)))
            return {'policy': candidate(), 'change_family': 'bootstrap'}
        self.learner = LearningService(self.store, enabled=True, daily_enabled=True, now=lambda: self.now,
            proposer=propose, daily_evaluator=lambda *args, **kwargs: {'decision': 'apply', 'reason': 'test', 'metrics': None})
        self.addCleanup(self.learner.close)
        self.context = {'initial_state': {'as_of': self.data['as_of'], 'cash': '1000000',
            'equity': '1000000', 'positions': [], 'pending_orders': [], 'unresolved_orders': []}}

    def process(self):
        self.learner.process(self.data, [], LIMITS, context=self.context)

    def test_daily_research_preserves_frozen_trial_and_permissions(self):
        trial = {'id': 'original', 'created_at': '2026-01-01', 'candidate_id': 'candidate',
                 'policy': candidate(), 'decision': 'waiting', 'evaluation_spec': {'horizon': 60}}
        self.store.save_setting('learning', {'trial': trial})
        with self.store.connect() as db:
            from backend.experiment_store import encode
            db.execute('INSERT INTO learning_trials VALUES (?,?,?)', ('original', '2026-01-01', encode(trial)))
        self.process()
        self.assertEqual(self.learner.state()['trial'], trial)
        self.assertEqual(self.learner.history()[0], trial)
        self.assertIsNotNone(self.learner.daily.pending())
        self.assertFalse(self.store.setting('enabled'))
        self.assertTrue(self.store.setting('user_paused'))
        self.assertEqual(self.store.orders(), [])

    def test_next_completed_day_researches_even_with_frozen_trial(self):
        self.process()
        first = self.learner.daily.pending()['decision_id']
        self.data = raw_input(1)
        self.now = datetime.fromisoformat(self.data['observed_at'])
        self.context['initial_state']['as_of'] = self.data['as_of']
        self.process()
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(self.learner.daily.pending()['decision_id'], first)
        self.assertEqual(len(self.learner.daily.history()), 2)

    def test_daily_outcomes_reach_next_research_without_replacing_development_feedback(self):
        from backend.learning_research import _feedback, _public_feedback
        from tests.test_learning_research import metrics
        original = self.learner.proposer
        def propose(*args, **kwargs):
            result = original(*args, **kwargs)
            result['development'] = {**metrics(12, 11, 1), 'sessions': 120}
            return result
        self.learner.proposer = propose
        for index, decision in enumerate(('keep', 'blocked', 'error', 'apply')):
            with self.subTest(decision=decision):
                self.data = raw_input(index)
                self.now = datetime.fromisoformat(self.data['observed_at'])
                self.context['initial_state']['as_of'] = self.data['as_of']
                final = {'basis': 'development_only', 'intervals': 1,
                         'champion': {'net_return_pct': -2., 'stress_return_pct': -2.1},
                         'candidate': {'net_return_pct': -1., 'stress_return_pct': -1.1},
                         'loss_reduction_only': True}
                if decision == 'blocked':
                    final = {'basis': 'development_only', 'error': 'insufficient_observed_baseline'}
                self.learner.daily.evaluator = Mock(return_value={
                    'decision': decision, 'reason': decision + ' reason', 'metrics': final})
                if decision == 'error':
                    self.learner.daily.evaluator.side_effect = RuntimeError('temporary evaluator failure')
                    with self.assertRaises(RuntimeError):
                        self.process()
                else:
                    self.process()
                saved = self.learner.daily.history()
                history = self.learner._research_history()
                entry = history[1]
                self.assertEqual(entry['decision'], decision)
                self.assertEqual(entry['daily_evaluation']['metrics'], saved[0]['metrics'])
                self.assertEqual(entry['daily_evaluation']['reason'], saved[0]['reason'])
                self.assertEqual(entry['daily_evaluation']['as_of'], self.data['as_of'])
                self.assertNotIn('metrics', entry)
                self.assertEqual(_feedback(history), 'exploit')
                public = _public_feedback([entry])[0]
                self.assertEqual(public['decision'], decision)
                self.assertEqual(public['daily_evaluation']['decision'], decision)
                self.assertEqual(public['development']['net_return_pct'], 12)
                self.assertNotIn('metrics', public)
                self.assertEqual(public['activation']['status'], 'pending' if decision == 'apply' else 'not_activated')
                if decision in ('keep', 'apply'):
                    self.assertEqual(public['daily_evaluation']['metrics']['intervals'], 1)
                    self.assertEqual(public['daily_evaluation']['metrics']['candidate']['net_return_pct'], -1)
                elif decision == 'error':
                    self.assertIsNone(public['daily_evaluation']['metrics'])
                entry['daily_evaluation']['metrics'] = {'changed': True}
                self.assertEqual(self.learner.daily.history(), saved)
                if index:
                    delivered = self.calls[-1][2][1]
                    self.assertEqual(delivered['daily_evaluation']['decision'], ('keep', 'blocked', 'error')[index - 1])

    def test_daily_activation_feedback_distinguishes_preparation_from_real_activation(self):
        self.process()
        daily = self.learner.daily
        pending = daily.pending()
        decision = deepcopy(daily.history()[0])
        self.assertEqual(self.learner._research_history()[1]['activation']['status'], 'pending')
        run = {'id': 'prepared-test', 'as_of': pending['signal_day'], 'status': 'ready',
               'created_at': self.now.isoformat(), 'learning_policy': pending['policy'], 'signals': []}
        self.assertTrue(daily.prepare(pending, run))
        self.assertEqual(self.learner._research_history()[1]['activation']['status'], 'prepared')
        self.assertEqual(daily.history()[0], decision)
        self.now = datetime.fromisoformat(raw_input(1)['observed_at'])
        self.store.save_settings({'enabled': True, 'user_paused': False})
        self.assertTrue(daily.activate(daily.pending(), effective_day=self.now.date().isoformat()))
        history = self.learner._research_history()
        self.assertEqual(history[1]['decision'], 'apply')
        self.assertEqual(history[1]['activation']['status'], 'activated')
        self.assertEqual(history[1]['activation']['effective_day'], self.now.date().isoformat())
        saved = daily.history()[0]
        saved.pop('activation')
        self.assertEqual(saved, decision)
        self.assertEqual(self.store.orders(), [])

    def test_daily_apply_without_activation_evidence_is_unknown_not_pending_or_rejected(self):
        self.process()
        self.data = raw_input(1)
        self.now = datetime.fromisoformat(self.data['observed_at'])
        self.context['initial_state']['as_of'] = self.data['as_of']
        self.process()
        history = self.learner._research_history()
        self.assertEqual(history[1]['activation']['status'], 'pending')
        self.assertEqual(history[2]['decision'], 'apply')
        self.assertEqual(history[2]['activation']['status'], 'unknown')

    def test_settled_book_unlocks_same_input_but_repeat_and_restart_do_not_reroll(self):
        self.context['initial_state']['pending_orders'] = ['unsettled']
        self.process()
        self.assertEqual(self.calls, [])
        self.context['initial_state']['pending_orders'] = []
        self.process()
        original = self.learner.frames()
        self.now += timedelta(minutes=3)
        self.data['observed_at'] = self.now.isoformat()
        self.process()
        restored = LearningService(self.store, enabled=True, daily_enabled=True, now=lambda: self.now,
                                   proposer=self.learner.proposer)
        self.addCleanup(restored.close)
        restored.process(self.data, [], LIMITS, context=self.context)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(restored.frames(), original)
        self.assertIsNotNone(restored.daily.pending())

    def test_changed_same_day_input_preserves_original_and_cancels_pending(self):
        self.process()
        original = self.learner.frames()
        self.data['histories']['005930'][self.data['as_of']]['volume'] += 1
        self.process()
        self.assertEqual(self.learner.frames(), original)
        self.assertIsNone(self.learner.daily.pending())
        self.assertEqual(self.learner.daily.snapshot()['status'], 'blocked')

    def test_next_morning_reusing_previous_close_does_not_erase_prepared_plan(self):
        self.context['execution_quotes'] = {'005930': {'price': '10000'}}
        self.process()
        original = self.learner.frames()
        pending = self.learner.daily.pending()
        self.now += timedelta(days=1)
        self.context = {}
        self.process()
        self.assertEqual(self.learner.frames(), original)
        self.assertEqual(self.learner.daily.pending(), pending)

    def test_proposer_failure_retries_on_same_day_after_backoff(self):
        original = self.learner.proposer
        def fail(*args, **kwargs):
            raise RuntimeError('transient')
        self.learner.proposer = fail
        self.learner.enqueue(self.data, [], LIMITS, source_id='one', context=self.context)
        self.learner.worker.join(3)
        self.assertEqual(self.learner.daily.snapshot()['status'], 'error')
        self.assertIsNotNone(self.learner.retry_after)
        self.learner.proposer = original
        self.now += timedelta(minutes=6)
        self.learner.enqueue(self.data, [], LIMITS, source_id='one', context=self.context)
        self.learner.worker.join(3)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNotNone(self.learner.daily.pending())

    def test_replay_change_preserves_anchor_but_invalidates_old_scorecard_and_trial(self):
        from backend.learning_compatibility import scorecard_version_matches, version_matches
        self.process()
        state = self.learner.state()
        original_start = state['scorecard_start']
        original_book = deepcopy(state['scorecard_book'])
        old = '3c4aab6471f3d2453474a289cb7cb8c74f8cbf015d7f97d2f872639ef5f94655'
        state['scorecard_engine'] = old
        self.store.save_setting('learning', state)
        self.data = raw_input(1)
        self.now = datetime.fromisoformat(self.data['observed_at'])
        self.context['initial_state']['as_of'] = self.data['as_of']
        self.process()
        updated = self.learner.state()
        self.assertEqual(updated['scorecard']['status'], 'invalid')
        self.assertIsNone(updated['scorecard']['baseline_return_pct'])
        self.assertEqual(updated['scorecard_start'], original_start)
        self.assertEqual(updated['scorecard_book'], original_book)
        self.assertEqual(updated['scorecard_engine'], old)
        self.assertFalse(scorecard_version_matches(old, self.learner.version))
        self.assertFalse(version_matches(old, self.learner.version))


if __name__ == '__main__':
    unittest.main()
