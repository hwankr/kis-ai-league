"""Offline persistence and race checks for daily policy decisions; no order APIs."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.daily_policy import DailyPolicyService
from backend.experiment_store import ExperimentStore
from backend.kis import KST
from backend.learning import LearningService
from backend.learning_policy import build_frame, policy_id
from tests.test_learning_policy import policy


class DailyPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'experiments.sqlite3'
        self.store = ExperimentStore(self.path)
        self.current = datetime(2026, 10, 12, 16, 30, tzinfo=KST)
        # LearningService owns the policy archive schema used during activation.
        learner = LearningService(self.store, enabled=False, now=lambda: self.current)
        self.addCleanup(learner.close)
        self.parent = policy(name='기존 정책')
        self.candidate = deepcopy(self.parent)
        self.candidate['name'] = '청산 개선 후보'
        self.candidate['strategies'][0]['holding_sessions'] = 4
        self.limits = {'fingerprint': 'account-a', 'budget': '1000000',
                       'order_cap': '250000', 'daily_buy_limit': '500000'}
        self.store.save_settings({'policy': {**self.limits, 'account_id': 'competition',
                                            'execution_strategy': 'all-strategies-v1'},
                                  'enabled': False, 'user_paused': True,
                                  'learning': {'trial': {'id': 'frozen-trial', 'decision': 'waiting'}},
                                  'mirror_intent': {'enabled': False}})
        self.data = {'as_of': '2026-10-12', 'observed_at': self.current.isoformat(), 'rows': [],
                     'histories': {}, 'calendars': {'KOSPI': ['2026-10-12']},
                     'benchmarks': {'KOSPI': {'2026-10-12': 100}}}
        self.frames = [build_frame(self.data)]
        self.context = {'initial_state': {'as_of': '2026-10-12', 'cash': '1000000',
                                          'pending_orders': [], 'unresolved_orders': []}}
        self.proposer = Mock(return_value={'policy': self.candidate, 'change_family': 'exit',
                                          'rationale': '청산 보유기간 개선', 'method': 'test'})
        self.result = {'decision': 'apply', 'reason': '개발 자료 개선', 'metrics': None, 'changes': []}
        self.evaluator = Mock(side_effect=lambda *args: deepcopy(self.result))
        self.service = self.make_service()

    def make_service(self, *, enabled=True, store=None):
        return DailyPolicyService(store or self.store, enabled=enabled, now=lambda: self.current,
                                  fallback_policy=lambda: deepcopy(self.parent), evaluator=self.evaluator)

    def process(self, *, data=None, limits=None, context=None, service=None):
        (service or self.service).process(data or self.data, self.frames, limits or self.limits, [],
                                          context=context or self.context, proposer=self.proposer)

    def prepared_run(self, pending=None, **changes):
        pending = pending or self.service.pending()
        signals = [{'strategy_id': 'momentum', 'symbol': '005930', 'action': 'buy'}]
        return {'id': 'prepared-run', 'as_of': pending['signal_day'], 'status': 'ready',
                'created_at': self.current.isoformat(), 'learning_policy': deepcopy(pending['policy']),
                'signals': signals, 'analysis': {'decisions': deepcopy(signals)}, **changes}

    def prepare(self):
        self.process()
        pending = self.service.pending()
        self.assertTrue(self.service.prepare(pending, self.prepared_run(pending)))
        return self.service.pending()

    def next_session(self):
        self.current = datetime(2026, 10, 13, 9, 5, tzinfo=KST)

    def allow_orders(self):
        self.store.save_settings({'enabled': True, 'user_paused': False})

    def controls(self):
        with self.store.connect() as db:
            return dict(db.execute("SELECT key,value FROM settings WHERE key NOT LIKE 'daily_policy:%'"))

    def test_preparation_and_activation_survive_restart_without_changing_orders_or_controls(self):
        old_run = {'id': 'existing-run', 'as_of': '2026-10-09', 'created_at': '2026-10-09T16:30:00+09:00',
                   'status': 'ready', 'signals': [], 'learning_policy': deepcopy(self.parent)}
        self.store.add_run(old_run)
        order = {'id': 'existing-order', 'created_at': '2026-10-12T10:00:00+09:00',
                 'fingerprint': 'account-a', 'status': 'partial', 'filled_quantity': 1,
                 'quantity': 2, 'symbol': '005930', 'side': 'buy', 'exit_policy': {'holding_sessions': 5}}
        self.assertTrue(self.store.reserve_order(order, 'existing-intent'))
        controls = self.controls()
        pending = self.prepare()
        self.assertEqual(self.controls(), controls)
        self.assertEqual(self.service.policy(), self.parent)
        self.assertEqual(self.store.runs(), [old_run])
        restored_store = ExperimentStore(self.path)
        restored = self.make_service(store=restored_store)
        self.assertEqual(restored.pending(), pending)
        self.allow_orders()
        controls = self.controls()
        self.next_session()
        self.assertTrue(restored.activate(pending, effective_day='2026-10-13'))
        restarted = self.make_service(store=ExperimentStore(self.path))
        self.assertEqual(restarted.policy(), self.candidate)
        self.assertIsNone(restarted.pending())
        self.assertEqual(restarted.active()['revision'], 1)
        self.assertEqual(restarted.snapshot()['active']['effective_day'], '2026-10-13')
        self.assertEqual(restarted.history()[0]['activation']['run_id'], 'prepared-run')
        self.assertEqual(restored_store.runs()[-1], old_run)
        self.assertEqual(restored_store.runs()[0]['analysis']['decisions'], restored_store.runs()[0]['signals'])
        self.assertEqual(restored_store.runs(details=False)[0]['id'], 'prepared-run')
        self.assertEqual(self.store.orders(), [order])
        self.assertEqual(self.controls(), controls)
        self.assertFalse(restarted.activate(pending, effective_day='2026-10-13'))
        self.assertEqual(len(self.store.runs()), 2)

    def test_same_day_refresh_and_restart_never_generate_another_candidate(self):
        self.process()
        first = self.service.history()[0]
        self.current += timedelta(minutes=20)
        refreshed = {**self.data, 'observed_at': self.current.isoformat()}
        self.process(data=refreshed)
        self.process(data=refreshed, service=self.make_service(store=ExperimentStore(self.path)))
        self.proposer.assert_called_once()
        self.evaluator.assert_called_once()
        self.assertEqual(self.service.history(), [first])

    def test_same_day_keep_decision_is_durable_and_cannot_be_rerolled(self):
        self.result['decision'] = 'keep'
        self.process()
        self.result['decision'] = 'apply'
        self.process(service=self.make_service())
        self.proposer.assert_called_once()
        self.assertEqual(self.service.snapshot()['status'], 'keep')
        self.assertIsNone(self.service.pending())
        self.assertEqual(self.service.policy(), self.parent)

    def test_evaluation_failure_preserves_model_response_for_retry_without_reprompting(self):
        self.evaluator.side_effect = RuntimeError('offline evaluator failure')
        with self.assertRaisesRegex(RuntimeError, 'offline evaluator'):
            self.process()
        failed = self.service.history()[0]
        self.assertEqual(failed['proposal']['policy'], self.candidate)
        self.assertEqual(failed['decision'], 'error')
        self.assertIsNone(self.service.pending())
        self.assertEqual(self.service.policy(), self.parent)
        self.evaluator.side_effect = lambda *args: deepcopy(self.result)
        self.current += timedelta(minutes=1)
        self.process(service=self.make_service(store=ExperimentStore(self.path)))
        self.proposer.assert_called_once()
        self.assertEqual(self.service.history()[0]['created_at'], failed['created_at'])
        self.assertEqual(self.service.history()[0]['proposal'], failed['proposal'])
        self.assertEqual(self.service.snapshot()['status'], 'prepared')

    def test_failed_proposal_persistence_never_evaluates_or_changes_current_policy(self):
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_decision BEFORE INSERT ON daily_policy_decisions "
                       "BEGIN SELECT RAISE(ABORT, 'decision write failed'); END")
        controls = self.controls()
        with self.assertRaises(sqlite3.IntegrityError):
            self.process()
        self.evaluator.assert_not_called()
        self.assertEqual(self.service.history(), [])
        self.assertIsNone(self.service.pending())
        self.assertEqual(self.service.policy(), self.parent)
        self.assertEqual(self.controls(), controls)
        self.assertEqual(self.service.snapshot()['status'], 'error')

    def test_index_failure_rolls_back_published_run_pointer_and_decision_together(self):
        pending = self.prepare()
        before_state, before_history = self.service.state(), self.service.history()
        self.allow_orders()
        self.next_session()
        with patch.object(self.store, '_index_run', side_effect=RuntimeError('index failure')):
            with self.assertRaisesRegex(RuntimeError, 'index failure'):
                self.service.activate(pending, effective_day='2026-10-13')
        self.assertEqual(self.store.runs(), [])
        self.assertEqual(self.store.runs(details=False), [])
        self.assertEqual(self.service.state(), before_state)
        self.assertEqual(self.service.history(), before_history)
        self.assertTrue(self.service.activate(pending, effective_day='2026-10-13'))

    def test_late_archive_failure_also_rolls_back_run_index_and_active_pointer(self):
        pending = self.prepare()
        before_state, before_history = self.service.state(), self.service.history()
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_policy BEFORE INSERT ON learning_policies "
                       "BEGIN SELECT RAISE(ABORT, 'policy archive failed'); END")
        self.allow_orders()
        self.next_session()
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.activate(pending, effective_day='2026-10-13')
        self.assertEqual(self.store.runs(), [])
        self.assertEqual(self.store.runs(details=False), [])
        self.assertEqual(self.service.state(), before_state)
        self.assertEqual(self.service.history(), before_history)

    def test_account_limit_or_parent_change_during_evaluation_blocks_publication(self):
        for race in ('account', 'limits', 'parent', 'revision'):
            with self.subTest(race=race):
                self.setUp()
                def change_then_return(*args):
                    if race in {'account', 'limits'}:
                        settings = self.store.setting('policy')
                        settings['fingerprint' if race == 'account' else 'order_cap'] = 'account-b' if race == 'account' else '100000'
                        self.store.save_setting('policy', settings)
                    elif race == 'parent':
                        self.parent['strategies'][0]['holding_sessions'] = 3
                    else:
                        state = self.service.state()
                        state['revision'] = 2
                        self.store.save_setting(self.service._key('account-a'), state)
                    return deepcopy(self.result)
                self.evaluator.side_effect = change_then_return
                self.process()
                self.assertIsNone(self.service.pending())
                with self.store.connect() as db:
                    result = db.execute('SELECT payload FROM daily_policy_decisions').fetchone()[0]
                self.assertIn('"decision":"blocked"', result)
                self.assertEqual(self.store.runs(), [])

    def test_stale_pending_is_rejected_after_account_limit_or_parent_changes(self):
        for race in ('account', 'limits', 'parent', 'revision'):
            with self.subTest(race=race):
                self.setUp()
                pending = self.prepare()
                if race in {'account', 'limits'}:
                    settings = self.store.setting('policy')
                    settings['fingerprint' if race == 'account' else 'daily_buy_limit'] = 'account-b' if race == 'account' else '200000'
                    self.store.save_setting('policy', settings)
                elif race == 'parent':
                    self.parent['strategies'][0]['holding_sessions'] = 3
                else:
                    state = self.service.state()
                    state['revision'] = 3
                    self.store.save_setting(self.service._key('account-a'), state)
                self.allow_orders()
                self.next_session()
                self.assertFalse(self.service.activate(pending, effective_day='2026-10-13'))
                self.assertEqual(self.store.runs(), [])

    def test_prepared_signal_set_is_first_complete_run_and_not_replaced_on_retry(self):
        pending = self.prepare()
        frozen = self.service.pending()['prepared_run']
        changed = self.prepared_run(pending, signals=[], analysis={'decisions': []})
        self.assertTrue(self.service.prepare(pending, changed))
        self.assertFalse(self.service.prepare(pending, {**changed, 'id': 'replacement-run'}))
        self.assertEqual(self.service.pending()['prepared_run'], frozen)

    def test_invalid_or_future_preparation_and_late_activation_are_rejected(self):
        self.process()
        self.allow_orders()
        pending = self.service.pending()
        invalid = [self.prepared_run(pending, status='error'), self.prepared_run(pending, as_of='2026-10-09'),
                   self.prepared_run(pending, learning_policy=self.parent), self.prepared_run(pending, created_at='2026-10-12T17:00:00+09:00'),
                   self.prepared_run(pending, created_at='2026-10-12T16:30:00')]
        for run in invalid:
            with self.subTest(run=run):
                self.assertFalse(self.service.prepare(pending, run))
        self.assertIsNone(self.service.pending().get('prepared_run'))
        self.assertFalse(self.service.activate(pending, effective_day='2026-10-12'))
        self.next_session()
        self.assertTrue(self.service.prepare(pending, self.prepared_run(pending)))
        self.assertFalse(self.service.activate(pending, effective_day='2026-10-13'))
        self.assertEqual(self.store.runs(), [])

    def test_disabled_or_unresolved_book_never_requests_proposal(self):
        self.process(service=self.make_service(enabled=False))
        for key in ('pending_orders', 'unresolved_orders'):
            context = deepcopy(self.context)
            context['initial_state'][key] = [{'id': 'unresolved'}]
            self.process(context=context)
        self.proposer.assert_not_called()
        self.assertIsNone(self.service.pending())
        self.assertEqual(self.service.history(), [])

    def test_changed_market_after_preparation_invalidates_next_session_application(self):
        pending = self.prepare()
        changed = deepcopy(self.data)
        changed['benchmarks']['KOSPI']['2026-10-12'] = 101
        self.process(data=changed)
        self.assertEqual(self.service.snapshot()['status'], 'blocked')
        self.allow_orders()
        self.next_session()
        self.assertFalse(self.service.activate(pending, effective_day='2026-10-13'))
        self.assertEqual(self.service.policy(), self.parent)
        self.assertEqual(self.store.runs(), [])

    def test_failed_evaluation_cannot_retry_with_changed_research_limits_or_engine(self):
        for changed in ('research', 'limits', 'engine'):
            with self.subTest(changed=changed):
                self.setUp()
                self.evaluator.side_effect = RuntimeError('evaluation failure')
                with self.assertRaises(RuntimeError):
                    self.process()
                frozen = self.service.history()[0]
                if changed == 'research':
                    self.frames[0]['benchmark_prices']['KOSPI'] += 1
                elif changed == 'limits':
                    self.limits['order_cap'] = '100000'
                    self.store.save_setting('policy', {**self.store.setting('policy'), 'order_cap': '100000'})
                else:
                    self.service.engine_version = 'changed-engine'
                self.evaluator.side_effect = lambda *args: deepcopy(self.result)
                self.process()
                self.proposer.assert_called_once()
                self.evaluator.assert_called_once()
                self.assertEqual(self.service.history(), [frozen])
                self.assertEqual(self.service.snapshot()['status'], 'blocked')
                self.assertIsNone(self.service.pending())

    def test_new_engine_cannot_prepare_or_activate_previous_engine_plan(self):
        pending = self.prepare()
        self.service.engine_version = 'new-engine'
        self.assertFalse(self.service.prepare(pending, self.prepared_run(pending)))
        self.allow_orders()
        self.next_session()
        self.assertFalse(self.service.activate(pending, effective_day='2026-10-13'))
        self.assertEqual(self.store.runs(), [])
        self.assertEqual(self.service.policy(), self.parent)

    def test_stopping_during_evaluation_preserves_response_without_publishing_candidate(self):
        stopping = False
        self.service.stopping = lambda: stopping
        def evaluate_then_stop(*args):
            nonlocal stopping
            stopping = True
            return deepcopy(self.result)
        self.evaluator.side_effect = evaluate_then_stop
        self.process()
        self.assertIsNone(self.service.pending())
        self.assertEqual(self.service.policy(), self.parent)
        self.assertEqual(self.store.runs(), [])
        self.assertEqual(self.service.history()[0]['proposal']['policy'], self.candidate)
        self.assertEqual(self.service.history()[0]['decision'], 'error')
        self.process()
        self.proposer.assert_called_once()
        self.evaluator.assert_called_once()
        stopping = False
        self.evaluator.side_effect = lambda *args: deepcopy(self.result)
        self.process()
        self.proposer.assert_called_once()
        self.assertEqual(self.service.snapshot()['status'], 'prepared')

    def test_forged_pending_identity_cannot_prepare_or_activate(self):
        pending = self.prepare()
        self.allow_orders()
        for key, value in (('fingerprint', 'account-b'), ('decision_id', 'other-decision'),
                           ('parent_id', 'other-parent'), ('candidate_id', 'other-candidate'),
                           ('revision', 99), ('market_input_hash', 'other-market')):
            with self.subTest(key=key):
                stale = {**pending, key: value}
                self.assertFalse(self.service.prepare(stale, self.prepared_run(pending)))
                self.next_session()
                self.assertFalse(self.service.activate(stale, effective_day='2026-10-13'))
        self.assertEqual(self.store.runs(), [])

    def test_snapshot_omits_incomplete_failure_metrics(self):
        self.result.update(decision='blocked', reason='자료 부족', metrics={'basis': 'development_only'})
        self.process()
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot['status'], 'blocked')
        self.assertIsNone(snapshot['last_decision']['metrics'])
        self.assertEqual(snapshot['last_decision']['candidate_name'], self.candidate['name'])
        self.assertIsNone(snapshot['active'])
        self.assertIsNone(snapshot['pending'])

    def test_old_account_request_cannot_remove_current_account_pending_plan(self):
        protected = {'status': 'prepared', 'error': None,
                     'pending': {'decision_id': 'account-b-decision', 'fingerprint': 'account-b'}}
        self.store.save_setting(self.service._key('account-b'), protected)
        self.store.save_setting('policy', {**self.store.setting('policy'), 'fingerprint': 'account-b'})
        self.process()  # Frozen request still belongs to account A.
        self.proposer.assert_not_called()
        self.assertEqual(self.service.state(), protected)

    def test_old_account_evaluation_error_cannot_overwrite_new_account_status(self):
        protected = {'status': 'prepared', 'error': None,
                     'pending': {'decision_id': 'account-b-decision', 'fingerprint': 'account-b'}}
        self.store.save_setting(self.service._key('account-b'), protected)
        def switch_account_then_fail(*args):
            self.store.save_setting('policy', {**self.store.setting('policy'), 'fingerprint': 'account-b'})
            raise RuntimeError('old account evaluation failed')
        self.evaluator.side_effect = switch_account_then_fail
        with self.assertRaisesRegex(RuntimeError, 'old account evaluation'):
            self.process()
        self.assertEqual(self.service.state(), protected)

    def test_disabled_or_paused_intent_blocks_activation_but_preserves_preparation(self):
        pending = self.prepare()  # Research and signal preparation remain permitted while paused.
        self.allow_orders()
        self.next_session()
        state, history = self.service.state(), self.service.history()
        for intent in ({'enabled': False, 'user_paused': False}, {'enabled': True, 'user_paused': True}):
            with self.subTest(intent=intent):
                self.store.save_settings(intent)
                self.assertFalse(self.service.activate(pending, effective_day='2026-10-13'))
                self.assertEqual(self.service.state(), state)
                self.assertEqual(self.service.history(), history)
                self.assertEqual(self.store.runs(), [])
                self.assertEqual(self.service.policy(), self.parent)
                for key, expected in intent.items():
                    self.assertEqual(self.store.setting(key), expected)
        self.allow_orders()
        self.assertTrue(self.service.activate(pending, effective_day='2026-10-13'))


if __name__ == '__main__':
    unittest.main()
