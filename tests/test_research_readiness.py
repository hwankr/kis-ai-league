"""Transient source failures wait before freezing; accepted decisions never reroll."""
from copy import deepcopy
from datetime import datetime, timedelta
import unittest

from backend.research_readiness import WAITING, input_readiness
from tests.test_daily_learning import DailyLearningTests
from tests.test_learning import LIMITS, raw_input


class ResearchReadinessTests(unittest.TestCase):
    setUp = DailyLearningTests.setUp

    def ready(self):
        day = self.data['as_of']
        period = {'start_date': day, 'end_date': day, 'as_of': day}
        self.context.update(
            competition_context={'status': 'ok', 'reliable': True,
                'basis': 'whole_account_broker_fills', 'period': period,
                'pace': {'status': 'behind', 'gross_amount': 100000},
                'metrics': {key: {'value': 1, 'target': 5}
                            for key in ('trading_days', 'symbols', 'api_orders')}},
            competition_observation={'status': 'ok', 'account_id': 'competition',
                'coverage': 'complete', 'stale': False, 'period': deepcopy(period),
                'updated_at': self.now.isoformat()})

    def process(self):
        return self.learner.process(self.data, [], LIMITS, context=self.context)

    def rows(self, table):
        with self.store.connect() as db:
            return db.execute('SELECT * FROM ' + table + ' ORDER BY 1').fetchall()

    def next_session_after_frozen_anchor(self):
        self.ready()
        self.process()
        anchor_frames = self.learner.frames()
        self.assertEqual(len(anchor_frames), 1)
        self.assertEqual(len(self.rows('daily_policy_contexts')), 1)
        self.assertEqual(self.learner.daily.history(), [])
        self.assertEqual(self.calls, [])
        self.data = raw_input(1)
        self.now = datetime.fromisoformat(self.data['observed_at'])
        self.context['initial_state']['as_of'] = self.data['as_of']
        return anchor_frames

    def test_failure_then_recovery_freezes_once_and_preserves_responses(self):
        anchor_frames = self.next_session_after_frozen_anchor()
        prior_observations = self.rows('daily_research_preparation_observations')
        self.ready()
        original_controls = {key: self.store.setting(key) for key in ('policy', 'enabled', 'user_paused')}
        self.context['competition_context']['reliable'] = False
        self.assertEqual(self.process(), WAITING)
        self.assertEqual(self.learner.frames(), anchor_frames)
        self.assertEqual(self.learner.daily.history(), [])
        self.assertEqual(self.calls, [])
        failed = self.rows('daily_research_preparation_observations')
        self.assertEqual(len(failed), len(prior_observations) + 1)
        self.now += timedelta(minutes=1)
        self.ready()
        self.process()
        frozen = self.rows('daily_policy_decisions')
        self.assertEqual(len(frozen), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.learner.daily.history()[0]['evaluation_context']['initial_state']['as_of'],
                         anchor_frames[0]['as_of'])
        saved = self.rows('daily_research_preparation_observations')
        self.assertEqual(len(saved), len(prior_observations) + 2)
        self.assertTrue(all(row in saved for row in failed))
        self.context['competition_context']['reliable'] = False
        self.process()
        self.ready()
        self.process()
        self.assertEqual(self.rows('daily_policy_decisions'), frozen)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.orders(), [])
        self.assertEqual({key: self.store.setting(key) for key in original_controls}, original_controls)

    def test_worker_retries_same_source_after_bounded_preparation_wait(self):
        anchor_frames = self.next_session_after_frozen_anchor()
        self.ready()
        self.context['competition_context']['reliable'] = False
        self.learner.enqueue(self.data, [], LIMITS, source_id='unchanged', context=deepcopy(self.context))
        worker = self.learner.worker
        if worker:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertIsNone(self.learner.attempted)
        self.assertEqual(self.learner.retry_after, self.now + timedelta(seconds=60))
        self.assertEqual(self.learner.frames(), anchor_frames)
        self.assertEqual(self.learner.daily.history(), [])
        self.ready()
        self.learner.enqueue(self.data, [], LIMITS, source_id='unchanged', context=self.context)
        self.assertEqual(self.calls, [])
        self.now += timedelta(seconds=61)
        self.ready()
        self.learner.enqueue(self.data, [], LIMITS, source_id='unchanged', context=self.context)
        worker = self.learner.worker
        if worker:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.learner.daily.history()), 1)
        self.assertEqual(self.learner.daily.history()[0]['evaluation_context']['initial_state']['as_of'],
                         anchor_frames[0]['as_of'])

    def test_missing_stale_wrong_period_future_and_incomplete_inputs_wait(self):
        self.ready()
        good = deepcopy(self.context)
        cases = [('competition_context', 'reliable', False),
                 ('competition_context', 'metrics', {}),
                 ('competition_observation', 'coverage', 'partial'),
                 ('competition_observation', 'stale', True),
                 ('competition_observation', 'account_id', 'other'),
                 ('competition_observation', 'period', {}),
                 ('competition_observation', 'updated_at', self.now.replace(hour=15).isoformat()),
                 ('competition_observation', 'updated_at', (self.now - timedelta(seconds=181)).isoformat()),
                 ('competition_observation', 'updated_at', (self.now + timedelta(seconds=1)).isoformat())]
        for part, key, value in cases:
            with self.subTest(part=part, key=key, value=value):
                altered = deepcopy(good)
                altered[part][key] = value
                self.assertEqual(input_readiness(altered, self.data['as_of'], self.now)['status'], 'waiting')
        self.context['research_observation'] = {'version': 'research-quote-batch-v1', 'status': 'collecting_execution'}
        self.assertEqual(self.process(), WAITING)
        self.assertEqual(self.learner.frames(), [])

    def test_new_contract_freezes_anchor_without_cash_fallback_decision(self):
        self.ready()
        self.context['research_observation'] = {'version': 'research-quote-batch-v1', 'id': 'batch',
                                               'status': 'complete'}
        self.process()
        self.assertEqual(len(self.learner.frames()), 1)
        self.assertEqual(self.learner.frames()[0]['research_observation'], self.context['research_observation'])
        self.assertEqual(self.learner.daily.history(), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.learner.daily.state()['status'], 'waiting')
        self.assertIn('동결 보유', self.learner.daily.state()['error'])

    def test_completed_old_engine_blocked_is_untouched_by_deployment(self):
        self.learner.daily.evaluator = lambda *args, **kwargs: {'decision': 'blocked', 'reason': 'missing', 'metrics': None}
        self.process()
        frozen = {name: self.rows(name) for name in ('learning_frames', 'daily_policy_decisions',
                                                    'daily_policy_contexts')}
        self.learner.daily.engine_version = 'new-contract-engine'
        self.context.update(research_observation={'status': 'missing'}, competition_context={'status': 'error'})
        self.data['rows'][0]['learning_eligible'] = False
        self.process()
        self.assertEqual({name: self.rows(name) for name in frozen}, frozen)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.learner.daily.pending())


if __name__ == '__main__':
    unittest.main()
