"""Offline persistence and race checks for daily policy decisions; no order APIs."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.daily_policy import DailyPolicyService, _hash, market_input_hash
from backend.experiment_store import ExperimentStore, encode
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
                'learning_engine_version': self.service.learning_engine_version,
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

    def preparations(self):
        with self.store.connect() as db:
            return {key: json.loads(payload) for key, payload in db.execute(
                'SELECT decision_id,payload FROM daily_policy_preparations')}

    def completed_next_day(self):
        self.current = datetime(2026, 10, 13, 16, 30, tzinfo=KST)
        self.data.update(as_of='2026-10-13', observed_at=self.current.isoformat(),
                         calendars={'KOSPI': ['2026-10-12', '2026-10-13']},
                         benchmarks={'KOSPI': {'2026-10-12': 100, '2026-10-13': 101}})
        self.context['initial_state']['as_of'] = '2026-10-13'
        self.frames.append(build_frame(self.data))

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

    def test_blocked_decision_survives_engine_change_and_restarts_without_research_or_preparation(self):
        prior = self.prepare()
        self.allow_orders()
        self.next_session()
        self.assertTrue(self.service.activate(prior, effective_day='2026-10-13'))
        active, preparations = self.service.active(), self.preparations()
        self.completed_next_day()
        self.proposer.reset_mock()
        self.evaluator.reset_mock()
        self.result.update(decision='blocked', reason='비교 자료 미확인',
                           metrics={'error': 'invalid_evaluation_input'})
        self.process()
        blocked = self.service.history()[0]
        self.assertEqual(blocked['decision'], 'blocked')
        self.assertEqual(blocked['metrics']['error'], 'invalid_evaluation_input')
        self.assertIn('research_inputs', blocked)
        self.assertIsNone(self.service.pending())

        def original_rows():
            with self.store.connect() as db:
                return {table: db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                        for table in ('settings', 'runs', 'run_index', 'learning_policies',
                                      'daily_policy_decisions', 'daily_policy_preparations')}

        originals = original_rows()
        # A fixed evaluator could now apply, but a completed blocked decision is
        # not a retryable error. A new engine must not reopen today's research.
        self.result.update(decision='apply', reason='수정된 평가', metrics=None)
        with patch('backend.daily_policy.source_version', return_value='changed-test-engine'):
            for restarted in (False, True, True):
                with self.subTest(restarted=restarted):
                    service = self.make_service(store=ExperimentStore(self.path)) if restarted else self.service
                    if restarted:
                        self.assertNotEqual(service.engine_version, blocked['engine_version'])
                        self.assertNotEqual(service.learning_engine_version, prior['prepared_run']['learning_engine_version'])
                    for _ in range(2):
                        self.current += timedelta(minutes=1)
                        self.process(data={**self.data, 'observed_at': self.current.isoformat()}, service=service)
                    self.assertEqual(service.history()[0], blocked)
                    self.assertEqual(service.state()['status'], 'blocked')
                    self.assertEqual(service.active(), active)
                    self.assertIsNone(service.pending())
                    self.assertEqual(self.preparations(), preparations)
                    self.assertEqual(original_rows(), originals)
        self.proposer.assert_called_once()
        self.evaluator.assert_called_once()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM daily_policy_decisions WHERE fingerprint=? AND day=?',
                                        ('account-a', self.data['as_of'])).fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM daily_policy_preparations WHERE day=?',
                                        (self.data['as_of'],)).fetchone()[0], 0)

    def test_pool_winner_is_persisted_and_prepared_with_matching_metadata(self):
        alternate = deepcopy(self.candidate)
        alternate['name'] = '수익 우위 후보'
        alternate['strategies'][0]['holding_sessions'] = 6
        original = {'policy': self.candidate, 'method': 'llm_development',
                    'development': {'net_return_pct': 2}, 'change_family': 'exit'}
        winning = {'policy': alternate, 'method': 'grammar_development',
                   'development': {'net_return_pct': 3}, 'change_family': 'exit'}
        self.proposer.return_value = {**original, 'candidate_pool': [original, winning]}
        def evaluate(parent, proposal, frames, limits):
            score = proposal['development']['net_return_pct']
            return {'decision': 'apply', 'reason': 'test', 'metrics': {
                'selection_return_pct': score, 'selection_stress_return_pct': score - .1}, 'changes': []}
        self.evaluator.side_effect = evaluate
        self.process()
        saved = self.service.history()[0]
        self.assertEqual(saved['candidate_name'], alternate['name'])
        self.assertEqual(saved['proposal']['method'], 'grammar_development')
        self.assertEqual(saved['proposal']['development'], {'net_return_pct': 3})
        self.assertEqual(saved['proposal']['candidate_pool'][0], original)
        self.assertEqual(saved['metrics']['selection']['candidates_evaluated'], 2)
        self.assertEqual(self.service.pending()['candidate_id'], policy_id(alternate))
        self.assertEqual(self.service.pending()['policy'], alternate)

    def test_frozen_evaluation_context_is_preserved_on_retry_and_cannot_change(self):
        self.context['daily_evaluation'] = {'maximum_drawdown_pct': 10}
        self.evaluator.side_effect = RuntimeError('offline failure')
        with self.assertRaises(RuntimeError):
            self.process()
        saved = self.service.history()[0]
        self.assertEqual(saved['evaluation_context'], {'maximum_drawdown_pct': 10})
        self.context['daily_evaluation']['maximum_drawdown_pct'] = 12
        self.evaluator.reset_mock()
        self.process()
        self.evaluator.assert_not_called()
        self.assertEqual(self.service.snapshot()['status'], 'blocked')
        self.assertEqual(self.service.history()[0], saved)

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
        history = self.service.history()
        changed = deepcopy(self.data)
        changed['benchmarks']['KOSPI']['2026-10-12'] = 101
        self.process(data=changed)
        self.assertEqual(self.service.snapshot()['status'], 'blocked')
        self.assertEqual(self.service.pending(), pending)
        self.assertEqual(self.preparations(), {pending['decision_id']: pending})
        self.assertEqual(self.service.history(), history)
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
                           ('revision', 99), ('market_input_hash', 'other-market'), ('signal_inputs', None)):
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

    def test_first_complete_preparation_is_archived_once_and_survives_activation(self):
        pending = self.prepare()
        archived = self.preparations()
        self.assertEqual(archived, {pending['decision_id']: pending})
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_archive_update BEFORE UPDATE ON daily_policy_preparations "
                       "BEGIN SELECT RAISE(ABORT, 'archive is immutable'); END")
            db.execute("CREATE TRIGGER reject_same_state BEFORE UPDATE ON settings "
                       "WHEN NEW.key LIKE 'daily_policy:%' AND NEW.value=OLD.value "
                       "BEGIN SELECT RAISE(ABORT, 'redundant state write'); END")
        self.assertTrue(self.service.prepare(pending, self.prepared_run(pending, signals=[])))
        self.service.note('prepared', fingerprint='account-a', decision_id=pending['decision_id'])
        self.assertEqual(self.preparations(), archived)
        self.assertEqual(self.service.pending(), pending)
        self.allow_orders()
        self.next_session()
        self.assertTrue(self.service.activate(pending, effective_day='2026-10-13'))
        self.assertEqual(self.preparations(), archived)

    def test_constructor_archives_existing_preparations_for_all_accounts_without_rewriting_state(self):
        pending = self.prepare()
        inactive = {**deepcopy(pending), 'decision_id': 'inactive-decision', 'fingerprint': 'account-b'}
        self.store.save_setting(self.service._key('account-b'), {'status': 'blocked', 'pending': inactive})
        with self.store.connect() as db:
            db.execute('DELETE FROM daily_policy_preparations')  # Simulate a pre-migration database.
            original_settings = dict(db.execute('SELECT key,value FROM settings'))
        original_history = self.service.history()
        self.make_service(store=ExperimentStore(self.path))
        self.make_service(store=ExperimentStore(self.path))
        self.assertEqual(self.preparations(), {pending['decision_id']: pending, inactive['decision_id']: inactive})
        with self.store.connect() as db:
            self.assertEqual(dict(db.execute('SELECT key,value FROM settings')), original_settings)
        self.assertEqual(self.service.history(), original_history)

    def test_preparation_archive_and_pending_commit_or_rollback_together(self):
        self.process()
        pending, state, history = self.service.pending(), self.service.state(), self.service.history()
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_preparation BEFORE INSERT ON daily_policy_preparations "
                       "BEGIN SELECT RAISE(ABORT, 'preparation archive failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.prepare(pending, self.prepared_run(pending))
        self.assertEqual(self.service.state(), state)
        self.assertEqual(self.service.history(), history)
        self.assertEqual(self.preparations(), {})
        with self.store.connect() as db:
            db.execute('DROP TRIGGER reject_preparation')
            db.execute("CREATE TRIGGER reject_pending BEFORE UPDATE ON settings "
                       "WHEN NEW.key LIKE 'daily_policy:%' BEGIN SELECT RAISE(ABORT, 'pending write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.prepare(pending, self.prepared_run(pending))
        self.assertEqual(self.service.state(), state)
        self.assertEqual(self.preparations(), {})

    def test_activation_archive_failure_preserves_legacy_pending_and_published_state(self):
        pending = self.prepare()
        with self.store.connect() as db:
            db.execute('DELETE FROM daily_policy_preparations')
            db.execute("CREATE TRIGGER reject_preparation BEFORE INSERT ON daily_policy_preparations "
                       "BEGIN SELECT RAISE(ABORT, 'preparation archive failure'); END")
        state, history = self.service.state(), self.service.history()
        self.allow_orders()
        self.next_session()
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.activate(pending, effective_day='2026-10-13')
        self.assertEqual(self.service.state(), state)
        self.assertEqual(self.service.history(), history)
        self.assertEqual(self.store.runs(), [])
        self.assertEqual(self.preparations(), {})

    def test_blocked_plan_survives_repeated_notes_and_cannot_be_reopened_by_retry_status(self):
        pending = self.prepare()
        self.service.note('blocked', '자료 변경', fingerprint='account-a', decision_id=pending['decision_id'])
        blocked, history = self.service.state(), self.service.history()
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_state_update BEFORE UPDATE ON settings "
                       "WHEN NEW.key LIKE 'daily_policy:%' BEGIN SELECT RAISE(ABORT, 'unexpected state write'); END")
        self.service.note('blocked', '자료 변경', fingerprint='account-a', decision_id=pending['decision_id'])
        for status in ('error', 'researching', 'waiting', 'prepared'):
            self.service.note(status, '늦은 재시도', fingerprint='account-a', decision_id=pending['decision_id'])
        self.assertFalse(self.service.prepare(pending, self.prepared_run(pending)))
        self.allow_orders()
        self.next_session()
        self.assertFalse(self.service.activate(pending, effective_day='2026-10-13'))
        self.assertEqual(self.service.state(), blocked)
        self.assertEqual(self.service.pending(), pending)
        self.assertEqual(self.service.history(), history)
        self.assertEqual(self.preparations(), {pending['decision_id']: pending})
        self.assertEqual(self.store.runs(), [])

    def test_transient_read_error_keeps_preparation_retryable(self):
        pending = self.prepare()
        self.service.note('error', '시장 자료 조회 실패', fingerprint='account-a', decision_id=pending['decision_id'],
                          details={'code': 'market_input_unavailable'})
        self.assertEqual(self.service.pending(), pending)
        self.assertEqual(self.service.snapshot()['status'], 'error')
        self.allow_orders()
        self.next_session()
        self.assertTrue(self.service.activate(pending, effective_day='2026-10-13'))
        self.assertEqual(self.preparations(), {pending['decision_id']: pending})
        self.assertIsNone(self.service.snapshot()['activation_check'])

    def test_new_completed_day_replaces_blocked_plan_but_archives_original_before_replacement(self):
        original = self.prepare()
        original_decision = self.service.history()[0]
        self.service.note('blocked', '원본 입력 변경', fingerprint='account-a', decision_id=original['decision_id'],
                          details={'code': 'signal_inputs_changed'})
        with self.store.connect() as db:
            db.execute('DELETE FROM daily_policy_preparations')
        self.completed_next_day()
        self.process()
        current = self.service.pending()
        self.assertNotEqual(current['decision_id'], original['decision_id'])
        self.assertEqual(self.service.state()['status'], 'prepared')
        self.assertIsNone(self.service.snapshot()['activation_check'])
        self.assertEqual(self.preparations(), {original['decision_id']: original})
        self.assertEqual(self.service.history()[1], original_decision)
        protected = self.service.state()
        for status in ('blocked', 'error'):
            self.service.note(status, '이전 심사의 늦은 결과', fingerprint='account-a', decision_id=original['decision_id'],
                              details={'code': 'stale_check'})
        self.assertEqual(self.service.state(), protected)
        self.assertTrue(self.service.prepare(current, self.prepared_run(current, id='next-prepared-run')))
        self.assertEqual(len(self.preparations()), 2)
        self.allow_orders()
        self.current = datetime(2026, 10, 14, 9, 5, tzinfo=KST)
        self.assertTrue(self.service.activate(self.service.pending(), effective_day='2026-10-14'))
        self.assertEqual(self.preparations()[original['decision_id']], original)

    def test_snapshot_explains_engine_mismatch_without_rewriting_original_records(self):
        self.process()
        self.assertTrue(self.service.prepare(self.service.pending(), self.prepared_run(
            learning_engine_version=self.service.learning_engine_version)))
        state, history, archive = self.service.state(), self.service.history(), self.preparations()
        original_daily = self.service.engine_version
        self.service.engine_version = 'changed-daily-engine'
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot['status'], 'blocked')
        self.assertIn('심사 엔진 버전', snapshot['error'])
        self.assertIsNotNone(snapshot['pending'])
        self.service.engine_version = original_daily
        self.service.learning_engine_version = 'changed-learning-engine'
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot['status'], 'blocked')
        self.assertIn('학습 엔진 버전', snapshot['error'])
        self.assertEqual(self.service.state(), state)
        self.assertEqual(self.service.history(), history)
        self.assertEqual(self.preparations(), archive)

    def test_snapshot_blocks_missing_prepared_learning_version_without_rewriting_legacy_record(self):
        self.process()
        run = self.prepared_run()
        run.pop('learning_engine_version')
        self.assertTrue(self.service.prepare(self.service.pending(), run))
        state, history, archive = self.service.state(), self.service.history(), self.preparations()
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot['status'], 'blocked')
        self.assertIn('학습 엔진 버전', snapshot['error'])
        self.assertEqual(self.service.state(), state)
        self.assertEqual(self.service.history(), history)
        self.assertEqual(self.preparations(), archive)

    def add_market_row(self, symbol='005930', *, eligible=True):
        self.data['rows'].append({'symbol': symbol, 'name': symbol, 'board': 'KOSPI',
                                  'learning_eligible': eligible})
        self.data['histories'][symbol] = {'2026-10-12': {'open': 10000, 'high': 10100, 'low': 9900,
                                                      'close': 10000, 'volume': 1000, 'turnover': 10000000}}

    def test_new_decision_and_pending_freeze_complete_signal_inputs_once(self):
        self.add_market_row()
        self.frames = [build_frame(self.data)]
        original_hash = market_input_hash(self.data)
        pending = self.prepare()
        original_record = self.service.history()[0]
        inputs = original_record['signal_inputs']
        self.assertEqual(inputs, pending['signal_inputs'])
        self.assertEqual(inputs['version'], 'daily-inputs-v1')
        self.assertEqual(inputs['hash'], original_hash)
        self.assertEqual(pending['market_input_hash'], original_hash)
        self.assertEqual(inputs['frame'], {key: value for key, value in build_frame(self.data).items() if key != 'observed_at'})
        self.assertTrue(inputs['frame']['rows'][0]['learning_eligible'])
        self.assertEqual(inputs['baseline_signals'], [])
        self.assertEqual(inputs['baseline_hash'], _hash([]))
        self.data['rows'][0]['learning_eligible'] = False
        self.add_market_row('000660')
        self.assertNotEqual(market_input_hash(self.data), original_hash)
        self.process(service=self.make_service(store=ExperimentStore(self.path)))
        self.assertEqual(self.service.pending(), pending)
        self.assertEqual(self.service.history(), [original_record])
        self.assertEqual(self.preparations(), {pending['decision_id']: pending})
        self.assertEqual(self.service.snapshot()['status'], 'prepared')
        self.proposer.assert_called_once()

    def test_error_retry_preserves_original_signal_inputs_and_full_research_digest(self):
        self.add_market_row()
        self.frames = [build_frame(self.data)]
        self.frames[0]['baseline_signals'] = [{'symbol': '005930', 'strategy_id': 'baseline', 'action': 'buy'}]
        frozen_research = deepcopy(self.frames)
        self.evaluator.side_effect = RuntimeError('retryable evaluation error')
        with self.assertRaises(RuntimeError):
            self.process()
        original = self.service.history()[0]
        self.data['rows'][0]['learning_eligible'] = False
        self.add_market_row('000660')
        self.evaluator.side_effect = lambda *args: deepcopy(self.result)
        self.process()
        current = self.service.history()[0]
        self.assertEqual(current['signal_inputs'], original['signal_inputs'])
        self.assertEqual(current['signal_inputs']['baseline_signals'], frozen_research[0]['baseline_signals'])
        self.assertEqual(current['signal_inputs']['baseline_hash'], _hash(frozen_research[0]['baseline_signals']))
        self.assertEqual(current['market_input_hash'], original['market_input_hash'])
        self.assertEqual(current['research_input_hash'], original['research_input_hash'])
        self.assertEqual(current['created_at'], original['created_at'])
        self.assertEqual(self.frames, frozen_research)
        self.assertEqual(self.service.pending()['signal_inputs'], original['signal_inputs'])
        self.proposer.assert_called_once()
        self.assertEqual(self.evaluator.call_count, 2)

    def test_new_contract_freezes_last_matching_day_baseline_and_its_hash(self):
        first = {'symbol': '005930', 'strategy_id': 'first-baseline', 'action': 'hold'}
        frozen = {'symbol': '000660', 'strategy_id': 'last-baseline', 'action': 'buy'}
        other_day = {'symbol': '035420', 'strategy_id': 'other-day', 'action': 'buy'}
        self.frames = [
            {**build_frame(self.data), 'as_of': '2026-10-09', 'baseline_signals': [other_day]},
            {**build_frame(self.data), 'baseline_signals': [first]},
            {**build_frame(self.data), 'baseline_signals': [frozen]},
        ]
        self.process()
        record, pending = self.service.history()[0], self.service.pending()
        self.assertEqual(record['signal_inputs']['baseline_signals'], [frozen])
        self.assertEqual(record['signal_inputs']['baseline_hash'], _hash([frozen]))
        self.assertEqual(record['signal_inputs'], pending['signal_inputs'])
        self.frames[2]['baseline_signals'][0]['action'] = 'hold'
        self.assertEqual(self.service.pending(), pending)
        self.assertEqual(self.service.history()[0], record)

    def test_new_contract_blocks_when_latest_signal_day_frame_is_absent(self):
        self.frames = [{**build_frame(self.data), 'as_of': '2026-10-09',
                        'baseline_signals': [{'symbol': '005930', 'action': 'buy'}]}]
        self.process()
        self.assertIsNone(self.service.pending())
        self.assertEqual(self.service.snapshot()['status'], 'blocked')
        self.proposer.assert_not_called()

    def test_legacy_retry_keeps_exact_hash_guard_and_never_gains_input_contract(self):
        self.add_market_row()
        self.frames = [build_frame(self.data)]
        self.evaluator.side_effect = RuntimeError('evaluation error')
        with self.assertRaises(RuntimeError):
            self.process()
        original = self.service.history()[0]
        original.pop('signal_inputs')
        with self.store.connect() as db:
            db.execute('UPDATE daily_policy_decisions SET payload=? WHERE id=?', (encode(original), original['id']))
        self.evaluator.side_effect = lambda *args: deepcopy(self.result)
        self.process()
        self.assertNotIn('signal_inputs', self.service.history()[0])
        self.assertNotIn('signal_inputs', self.service.pending())
        self.assertTrue(self.service.prepare(self.service.pending(), self.prepared_run()))
        pending, history = self.service.pending(), self.service.history()
        self.data['rows'][0]['learning_eligible'] = False
        self.process()
        self.assertEqual(self.service.state()['status'], 'blocked')
        self.assertEqual(self.service.pending(), pending)
        self.assertEqual(self.service.history(), history)
        self.assertNotIn('signal_inputs', self.preparations()[pending['decision_id']])

    def test_new_input_contract_still_blocks_changed_frozen_symbol_price(self):
        self.add_market_row()
        self.frames = [build_frame(self.data)]
        pending = self.prepare()
        original = self.service.history()[0]
        self.data['histories']['005930']['2026-10-12']['close'] = 10001
        self.process()
        self.assertEqual(self.service.state()['status'], 'blocked')
        self.assertEqual(self.service.pending(), pending)
        self.assertEqual(self.service.history(), [original])
        self.assertEqual(self.preparations(), {pending['decision_id']: pending})

    def test_activation_check_details_update_only_when_outcome_changes(self):
        pending = self.prepare()
        details = {'code': 'market_input_unavailable', 'expected': '2026-10-12', 'actual': None}
        self.service.note('error', '시장 자료 조회 실패', fingerprint='account-a',
                          decision_id=pending['decision_id'], details=details)
        original = self.service.state()
        self.assertEqual(self.service.snapshot()['activation_check'], {**details, 'checked_at': self.service._timestamp()})
        self.current += timedelta(minutes=1)
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_check_repeat BEFORE UPDATE ON settings "
                       "WHEN NEW.key LIKE 'daily_policy:%' BEGIN SELECT RAISE(ABORT, 'repeated outcome write'); END")
        self.service.note('error', '시장 자료 조회 실패', fingerprint='account-a',
                          decision_id=pending['decision_id'], details={**details, 'checked_at': 'ignored'})
        self.assertEqual(self.service.state(), original)
        with self.store.connect() as db:
            db.execute('DROP TRIGGER reject_check_repeat')
        changed = {**details, 'actual': '2026-10-09'}
        self.service.note('error', '시장 자료 조회 실패', fingerprint='account-a',
                          decision_id=pending['decision_id'], details=changed)
        self.assertEqual(self.service.snapshot()['activation_check'], {**changed, 'checked_at': self.service._timestamp()})
        self.current += timedelta(minutes=1)
        self.service.note('blocked', '준비 입력 변경', fingerprint='account-a',
                          decision_id=pending['decision_id'], details=changed)
        self.assertEqual(self.service.snapshot()['activation_check'], {**changed, 'checked_at': self.service._timestamp()})
        blocked = self.service.state()
        self.service.note('error', '늦은 조회 실패', fingerprint='account-a',
                          decision_id=pending['decision_id'], details=details)
        self.assertEqual(self.service.state(), blocked)
        self.assertEqual(self.service.pending(), pending)
        self.assertEqual(self.preparations(), {pending['decision_id']: pending})


if __name__ == '__main__':
    unittest.main()
