"""Offline regression for independently due stages and scoped frozen-entry recovery."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import unittest
from unittest.mock import patch
from backend.experiments import OperationalBlock
from backend.kis import KisError
from backend import learning_compatibility as compatibility
from backend.learning_policy import BASELINE_POLICY, policy_id
from tests import test_experiments as experiment_fixtures

class IndependentSchedulingTests(unittest.TestCase):
    # Reuse the existing fake-broker fixture without inheriting its test methods.
    setUp = experiment_fixtures.ExperimentsTests.setUp
    make_service = experiment_fixtures.ExperimentsTests.make_service
    enable = experiment_fixtures.ExperimentsTests.enable
    submit = experiment_fixtures.ExperimentsTests.submit
    fill = experiment_fixtures.ExperimentsTests.fill
    add_run = experiment_fixtures.ExperimentsTests.add_run

    def set_failure(self, operation, count=6):
        error = OperationalBlock('fixture frozen entry issue', 'entry_selection') if operation == 'analyze' else KisError('fixture technical failure')
        for _ in range(count):
            self.service._failure(error, operation)
        self.current += timedelta(seconds=31)
        self.service.next_tick = 0

    def test_analysis_retry_does_not_delay_real_reconcile_exit_risk_or_research(self):
        self.service.store.save_setting('policy', dict(self.service._policy(), budget='300000',
                                                      order_cap='100000', daily_buy_limit='200000'))
        first = self.submit(5, symbol='005930', key='first')
        self.fill(5, status='filled')
        first = self.service.store.orders()[0]
        first['exit_policy'] = {'holding_sessions': 1, 'stop_loss_pct': 0, 'take_profit_pct': 0}
        self.service.store.save_order(first)
        second = self.submit(3, symbol='000660', key='second')
        self.current += timedelta(days=1)
        self.broker.days.append(self.current.date().isoformat())
        self.broker.remote[1].update(filled_quantity=3, remaining_quantity=0,
                                    filled_amount='30000', average_price='10000', status='filled')
        self.broker.account['holdings'] = {symbol: {'quantity': quantity, 'sellable_quantity': quantity, 'price': '10000'}
                                          for symbol, quantity in [('005930', 5), ('000660', 3)]}
        self.broker.account['as_of'] = self.current.isoformat()
        self.service.learning.enabled = True
        self.set_failure('analyze')
        with patch.object(self.service, '_analyze') as analysis, \
                patch.object(self.service, '_observe_learning_quotes') as research, \
                patch.object(self.service.learning_account, 'observe', wraps=self.service.learning_account.observe) as risk, \
                patch.object(self.service, '_launch', side_effect=lambda operation: self.service._work(operation, {})):
            self.service.tick()
        current = {order['id']: order for order in self.service.store.orders()}
        self.assertEqual(current[second['id']]['filled_quantity'], 3)
        self.assertEqual(current[second['id']]['status'], 'filled')
        self.assertTrue(any(symbol == '005930' and side == 'sell' for symbol, side, _, _ in self.broker.submissions))
        risk.assert_called()
        self.assertEqual(risk.call_args.kwargs['limit'], 15)
        research.assert_called_once()
        analysis.assert_not_called()
        self.assertEqual(self.service._operation_failures()['analyze']['consecutive_failures'], 6)

    def test_cycle_technical_retry_is_preserved_while_analysis_can_progress(self):
        self.enable()
        self.set_failure('cycle')
        before = deepcopy(self.service._operation_failures()['cycle'])
        with patch.object(self.service, '_cycle') as cycle, \
                patch.object(self.service, '_analyze') as analysis, \
                patch.object(self.service, '_launch', side_effect=lambda operation: self.service._work(operation, {})):
            self.service.tick()
        cycle.assert_not_called()
        analysis.assert_called_once_with(automatic_retry=True)
        self.assertEqual(self.service._operation_failures()['cycle'], before)

    def test_research_technical_retry_preserves_cycle_and_its_own_timer(self):
        self.enable()
        self.service.learning.enabled = True
        self.set_failure('learning_quotes')
        before = deepcopy(self.service._operation_failures()['learning_quotes'])
        with patch.object(self.service, '_cycle') as cycle, \
                patch.object(self.service, '_observe_learning_quotes') as research, \
                patch.object(self.service, '_analyze') as analysis, \
                patch.object(self.service, '_launch', side_effect=lambda operation: self.service._work(operation, {})):
            self.service.tick()
        cycle.assert_called_once()
        research.assert_not_called()
        analysis.assert_called_once_with(automatic_retry=True)
        self.assertEqual(self.service._operation_failures()['learning_quotes'], before)

    def test_success_flag_alone_does_not_clear_entry_selection_or_other_blockers(self):
        self.service._failure(OperationalBlock('fixture invalid frozen input', 'entry_selection'), 'analyze')
        self.service._failure(OperationalBlock('fixture invalid executable run', 'entry_selection'), 'cycle')
        self.service._issue('balance_mismatch', 'fixture unverified holdings')
        owners = deepcopy(self.service._operation_failures())
        self.service._success('analyze')
        self.service._success('cycle')
        opened = {item['code'] for item in self.service.store.setting('issues') if item['state'] == 'open'}
        self.assertIn('entry_selection', opened)
        self.assertIn('balance_mismatch', opened)
        self.assertEqual(self.service._operation_failures(), owners)

    def test_stale_validation_cannot_clear_new_generation_or_changed_policy(self):
        for change in ('generation', 'policy'):
            with self.subTest(change=change):
                self.service._failure(OperationalBlock('old generation', 'entry_selection'), 'analyze')
                token = self.service._entry_validation_token('analyze')
                run = {'id': 'frozen-run', 'learning_policy': token['adopted']}
                if change == 'generation':
                    # Same wall-clock timestamp still creates a distinct generation.
                    self.service._failure(OperationalBlock('new generation', 'entry_selection'), 'analyze')
                else:
                    self.service.store.save_setting('policy', dict(self.service._policy(), budget='200000'))
                before = deepcopy(self.service._operation_failures())
                self.assertFalse(self.service._resolve_validated_entry_selection('analyze', token, run))
                self.assertEqual(self.service._operation_failures(), before)
                self.assertTrue(any(x['code'] == 'entry_selection' and x['state'] == 'open'
                                    for x in self.service.store.setting('issues')))

    def test_technical_failure_and_generic_success_preserve_unverified_entry_owner(self):
        self.service._failure(OperationalBlock('frozen entry invalid', 'entry_selection'), 'analyze')
        original = deepcopy(self.service._operation_failures()['analyze'])
        self.service._failure(KisError('session lookup failed'), 'analyze')
        self.service._success('analyze')
        self.assertEqual(self.service._operation_failures()['analyze'], original)
        self.assertTrue(any(x['code'] == 'entry_selection' and x['state'] == 'open'
                            for x in self.service.store.setting('issues')))


class ScopedEntryCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.run = {'id': 'frozen-run', 'learning_engine_version': 'old-engine',
                    'learning_policy': deepcopy(BASELINE_POLICY),
                    'signals': [{'symbol': '005930', 'action': 'buy'}],
                    'entry_selection': {'context': {'held': []}}}
        self.active = {'revision': 3, 'policy': deepcopy(BASELINE_POLICY),
                       'candidate_id': policy_id(BASELINE_POLICY)}
        digest = hashlib.sha256(json.dumps(self.run, ensure_ascii=False, sort_keys=True,
                                separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        self.pins = frozenset({('old-engine', 'new-engine', '2026-10-23', 'frozen-run', 3,
                               policy_id(BASELINE_POLICY), digest)})
        setting = patch.object(compatibility, 'LIVE_ENTRY_RECOVERY_UPGRADES', self.pins)
        setting.start()
        self.addCleanup(setting.stop)

    def accepted(self, run=None, engine='new-engine', day='2026-10-23', active=None):
        return compatibility.scoped_live_entry_version_matches(self.run if run is None else run, engine,
            execution_day=day, active=self.active if active is None else active)

    def test_exact_single_day_summary_is_accepted_without_generic_research_alias(self):
        self.assertTrue(self.accepted())
        self.assertFalse(compatibility.live_entry_version_matches(self.run, 'new-engine'))
        self.assertFalse(compatibility.version_matches('old-engine', 'new-engine'))
        self.assertFalse(compatibility.scorecard_version_matches('old-engine', 'new-engine'))
        self.assertEqual(self.run['learning_engine_version'], 'old-engine')

    def test_other_day_revision_policy_engine_run_or_summary_is_rejected(self):
        for day in ('2026-10-22', '2026-10-24'):
            self.assertFalse(self.accepted(day=day))
        for mutate in (lambda a: a.update(revision=4), lambda a: a.update(candidate_id='different'),
                       lambda a: a['policy'].update(cash_reserve=0.99)):
            changed = deepcopy(self.active)
            mutate(changed)
            self.assertFalse(self.accepted(active=changed))
        self.assertFalse(self.accepted(engine='later-engine'))
        for mutate in (lambda r: r.update(id='other-run'), lambda r: r.update(extra='added'),
                       lambda r: r['signals'][0].update(symbol='000150'),
                       lambda r: r['entry_selection']['context']['held'].append('403870'),
                       lambda r: r.update(learning_engine_version='other-old-engine')):
            changed = deepcopy(self.run)
            mutate(changed)
            self.assertFalse(self.accepted(run=changed))

    def test_full_run_and_transitive_transition_are_rejected(self):
        self.assertFalse(self.accepted(run=dict(self.run, analysis={})))
        with patch.object(compatibility, 'LIVE_ENTRY_RECOVERY_UPGRADES', self.pins | {
                ('new-engine', 'later-engine', '2026-10-23', 'frozen-run', 3,
                 policy_id(BASELINE_POLICY), next(iter(self.pins))[-1])}):
            self.assertFalse(self.accepted(engine='later-engine'))


