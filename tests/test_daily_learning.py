"""Daily research uses the production worker without rewriting frozen trials."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

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
            proposer=propose, daily_evaluator=lambda *args: {'decision': 'apply', 'reason': 'test', 'metrics': None})
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

    def test_scorecard_migration_preserves_start_book_but_does_not_validate_old_trial(self):
        from backend.learning_compatibility import version_matches
        self.process()
        state = self.learner.state()
        original_start = state['scorecard_start']
        old = '3c4aab6471f3d2453474a289cb7cb8c74f8cbf015d7f97d2f872639ef5f94655'
        state['scorecard_engine'] = old
        self.store.save_setting('learning', state)
        self.data = raw_input(1)
        self.now = datetime.fromisoformat(self.data['observed_at'])
        self.context['initial_state']['as_of'] = self.data['as_of']
        self.process()
        updated = self.learner.state()
        self.assertEqual(updated['scorecard']['status'], 'ready')
        self.assertEqual(updated['scorecard_start'], original_start)
        self.assertEqual(updated['scorecard_engine'], old)
        self.assertFalse(version_matches(old, self.learner.version))


if __name__ == '__main__':
    unittest.main()
