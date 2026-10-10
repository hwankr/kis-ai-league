"""Offline owned-capital accounting and protective paper execution."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
import json
from unittest.mock import patch

from backend.experiment_store import ExperimentStore
from backend.learning_account import (LearningAccount, owned_book, execution_model, execution_profile,
                                     confirmed_execution_orders, FEE, TAX)
from backend.learning_policy import portfolio_metrics
from backend.kis import KisError
from tests import test_experiments as fixtures
from tests.test_learning_policy import frames, policy as rules


class AccountTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ExperimentStore(Path(temporary.name) / 'test.sqlite3')
        self.account = LearningAccount(self.store)
        self.policy = {'fingerprint': 'owned', 'budget': '1000000'}

    def book(self, equity, budget=1000000):
        return {'as_of': '2026-10-06', 'cash': str(equity), 'equity': str(equity),
                'budget': str(budget), 'positions': [], 'pending_orders': 0, 'unresolved_orders': 0}

    def test_external_account_cash_is_not_in_the_owned_equity(self):
        position = {'symbol': '005930', 'strategy_id': 'old', 'quantity': 10,
            'cost': '100000', 'average_price': '10000', 'entry_date': '2026-10-02', 'exit_pending': True,
            'exit_policy': {'holding_sessions': 9, 'stop_loss_pct': 4, 'take_profit_pct': 8}}
        book = owned_book(self.policy, [position], [{'realized_pnl': '-3000'}], [], {'005930': 11000},
                          as_of='2026-10-06', session_days=['2026-10-02', '2026-10-06'])
        self.assertEqual(Decimal(book['cash']), 897000)
        self.assertEqual(Decimal(book['equity']), 897000 + 110000 * (1 - FEE - TAX))
        self.assertEqual(book['positions'][0]['held_sessions'], 1)
        self.assertTrue(book['positions'][0]['exit_pending'])
        self.assertEqual(book['positions'][0]['exit_policy'], position['exit_policy'])

    def test_capital_changes_issue_units_without_erasing_losses(self):
        first = self.account.observe(self.policy, self.book(900000), '2026-10-06T10:00:00+09:00')
        later = self.account.observe(self.policy, self.book(1900000, 2000000), '2026-10-06T11:00:00+09:00')
        self.assertAlmostEqual(first['return_pct'], -10)
        self.assertAlmostEqual(later['return_pct'], -10)
        self.assertEqual(later['peak'], first['peak'])

    def test_capital_flows_use_current_pre_flow_value_after_unobserved_pnl(self):
        for budget, equity, expected in ((1100000, 900000, -20), (900000, 700000, -20),
                                         (1100000, 1300000, 20)):
            with self.subTest(budget=budget, equity=equity):
                policy = {**self.policy, 'fingerprint': f'{budget}:{equity}'}
                self.account.observe(policy, self.book(1000000), '2026-10-06T10:00:00+09:00')
                later = self.account.observe(policy, self.book(equity, budget), '2026-10-06T11:00:00+09:00')
                self.assertAlmostEqual(later['return_pct'], expected)
                self.assertAlmostEqual(float(later['unit']), 1 + expected / 100)
                self.assertEqual(later['risk']['active'], expected < -15)

    def test_daily_record_deduplicates_and_protection_survives_restart_and_gains(self):
        self.account.observe(self.policy, self.book(1000000), '2026-10-06T10:00:00+09:00')
        breach = self.account.observe(self.policy, self.book(800000), '2026-10-06T11:00:00+09:00', daily=True)
        self.assertTrue(breach['risk']['active'])
        restored = LearningAccount(self.store)
        recovered = restored.observe(self.policy, self.book(1100000), '2026-10-06T16:00:00+09:00', daily=True)
        self.assertTrue(recovered['risk']['active'])
        self.assertEqual(restored.snapshot(self.policy)['observations'], 1)
        self.assertEqual(recovered['max_drawdown_pct'], 20)

    def test_explicit_protection_reset_preserves_cumulative_performance(self):
        before = self.account.observe(self.policy, self.book(800000), '2026-10-06T10:00:00+09:00')
        self.account.reset_protection(self.policy, '2026-10-06T11:00:00+09:00')
        after = self.account.state('owned')
        self.assertFalse(after['risk']['active'])
        for key in ('return_pct', 'max_drawdown_pct', 'peak', 'equity'):
            self.assertEqual(before[key], after[key])

    def test_fingerprints_do_not_share_risk_or_performance(self):
        self.account.observe(self.policy, self.book(800000), '2026-10-06T10:00:00+09:00')
        self.assertIsNone(self.account.snapshot({**self.policy, 'fingerprint': 'other'}))

    def test_execution_calibration_excludes_unresolved_and_respects_sides(self):
        orders = [{'status': 'cancelled', 'side': 'buy', 'filled_quantity': 5, 'quantity': 10} for _ in range(5)]
        orders += [{'status': 'unknown', 'side': 'sell', 'filled_quantity': 0, 'quantity': 100} for _ in range(30)]
        orders += [{'status': 'cancelled', 'side': 'buy', 'filled_quantity': 0, 'quantity': 10,
                    'submission_skipped': True} for _ in range(5)]
        model = execution_model(orders)
        self.assertEqual(model['estimates']['buy_fill_ratio'], .5)
        self.assertIsNone(model['estimates']['sell_fill_ratio'])
        self.assertEqual(model['status'], 'insufficient')
        sample = frames(3, price=10000)
        for frame in sample:
            frame['execution_quotes'] = {'005930': {'price': frame['rows'][0]['open'], 'eligible': True,
                'observed_at': frame['as_of'] + 'T09:05:00+09:00'}}
        result = portfolio_metrics(rules(), sample, {'budget': '1000000', 'order_cap': '250000', 'daily_buy_limit': '500000'},
                                   execution_model=model)
        self.assertTrue(result['valid'], result['error'])
        self.assertFalse(result['promotion_supported'])

    def test_no_observations_never_become_a_full_fill_estimate(self):
        profile = execution_profile([])
        self.assertEqual(profile, execution_model([]))
        self.assertEqual(profile['estimates'], {'buy_fill_ratio': None, 'sell_fill_ratio': None})
        self.assertEqual(profile['basis']['buy_orders'], 0)
        self.assertIsNone(profile['support']['buy'])
        self.assertEqual([row['id'] for row in profile['scenarios']],
                         ['partial_fill', 'delayed_sell', 'adverse_buy'])
        self.assertEqual(profile['scenarios'][0]['model']['fill_ratio'], .5)
        self.assertTrue(all(row['assumption'] for row in profile['scenarios']))

    def test_terminal_zero_fills_count_but_unsent_unknown_and_inconsistent_do_not(self):
        base = {'status': 'cancelled', 'side': 'buy', 'quantity': 10, 'filled_quantity': 0, 'request_sent': True}
        orders = [base, {**base, 'status': 'rejected'}, {**base, 'filled_quantity': 5},
                  {**base, 'status': 'filled', 'filled_quantity': 10},
                  {**base, 'request_sent': False}, {**base, 'submission_skipped': True},
                  {**base, 'status': 'partial'}, {**base, 'status': 'unknown'},
                  {**base, 'status': 'filled'}, {**base, 'filled_quantity': 11},
                  {**base, 'quantity': True}]
        profile = execution_profile(orders)
        self.assertEqual(profile['basis']['buy_orders'], 4)
        self.assertEqual(profile['estimates']['buy_fill_ratio'], .375)

    def test_legacy_zero_fill_status_needs_transmission_evidence(self):
        legacy = {'status': 'rejected', 'side': 'buy', 'quantity': 10, 'filled_quantity': 0}
        accepted = [{**legacy, 'order_id': '123'}, {**legacy, 'accepted_at': '2026-07-01T09:10:00+09:00'},
                    {**legacy, 'request_sent': True}, {**legacy, 'submission_skipped': False},
                    {**legacy, 'status': 'cancelled', 'filled_quantity': 2}]
        orders = [legacy, {**legacy, 'status': 'cancelled'}, *accepted,
                  {**legacy, 'request_sent': False}, {**legacy, 'order_id': '123', 'submission_skipped': True}]
        self.assertEqual(confirmed_execution_orders(orders), accepted)
        self.assertEqual(execution_profile(orders)['basis']['buy_orders'], len(accepted))
        self.assertEqual(execution_profile(orders)['estimates']['buy_fill_ratio'], .04)

    def test_context_ranges_are_frozen_by_side_and_use_minute_bar_volume(self):
        base = {'status': 'cancelled', 'quantity': 10, 'filled_quantity': 5,
                'decision_price': 100, 'decision_volume': 300,
                'decision_observed_at': '2026-07-01T00:10:00+00:00'}
        orders = [{**base, 'side': 'buy'}, {**base, 'side': 'buy', 'decision_price': 110,
                  'decision_volume': 900, 'decision_observed_at': '2026-07-01T10:20:00+09:00'},
                  {**base, 'side': 'sell', 'filled_quantity': 2}]
        original = deepcopy(orders)
        profile = execution_profile(orders)
        self.assertEqual(profile['status'], 'estimated')
        self.assertEqual(profile['support']['buy'], {'price_min': 100., 'price_max': 110.,
            'minute_min': 550, 'minute_max': 620, 'volume_min': 300., 'volume_max': 900.})
        self.assertEqual(profile['support']['sell']['price_max'], 100)
        self.assertEqual(profile['estimates']['sell_fill_ratio'], .2)
        self.assertEqual(orders, original)
        self.assertEqual(profile, json.loads(json.dumps(profile)))
        self.assertEqual(profile['scenarios'][0]['id'], 'empirical')

    def test_prediction_error_uses_only_preorder_predictions_on_confirmed_terminal_orders(self):
        base = {'status': 'cancelled', 'side': 'buy', 'quantity': 10, 'filled_quantity': 5,
                'created_at': '2026-07-01T09:10:00+09:00'}
        prediction = {'version': 1, 'fill_ratio': .8, 'created_at': base['created_at']}
        orders = [{**base, 'execution_prediction': prediction},
                  {**base, 'execution_prediction': {**prediction, 'fill_ratio': None}},
                  {**base, 'execution_prediction': {**prediction, 'created_at': '2026-07-02T09:10:00+09:00'}},
                  {**base, 'execution_prediction': prediction, 'submission_skipped': True},
                  {**base, 'execution_prediction': prediction, 'status': 'partial'}]
        profile = execution_profile(orders)
        self.assertEqual(profile['prediction_basis'], {'buy': 1, 'sell': 0})
        self.assertAlmostEqual(profile['prediction_mae']['buy'], .3)
        self.assertIsNone(profile['prediction_mae']['sell'])

    def test_bad_context_does_not_fabricate_support_or_discard_known_fill_ratio(self):
        base = {'status': 'cancelled', 'quantity': 10, 'filled_quantity': 5,
                'decision_price': 100, 'decision_volume': 300,
                'decision_observed_at': '2026-07-01T09:10:00'}  # timezone missing
        profile = execution_profile([{**base, 'side': side} for side in ('buy', 'sell')])
        self.assertEqual(profile['status'], 'estimated')
        self.assertEqual(profile['estimates']['sell_fill_ratio'], .5)
        self.assertEqual(profile['support'], {'buy': None, 'sell': None})


class ProtectiveExecutionTests(unittest.TestCase):
    setUp = fixtures.ExperimentsTests.setUp
    make_service = fixtures.ExperimentsTests.make_service
    enable = fixtures.ExperimentsTests.enable
    fill = fixtures.ExperimentsTests.fill
    submit = fixtures.ExperimentsTests.submit
    add_run = fixtures.ExperimentsTests.add_run

    def test_unknown_eligibility_is_not_recorded_as_a_confirmed_unfillable_quote(self):
        self.service.learning.enabled = True
        self.service._configure({**self.policy, 'execution_strategy': 'all-strategies-v1'})
        self.broker.quote = lambda symbol: {'eligible': False, 'reason': 'status_unknown'}
        with self.assertRaises(KisError):
            self.service._symbol_quote(self.broker, '005930')
        self.assertFalse(self.service.store.setting('learning_quotes', {}).get('quotes'))

    def test_loss_protection_sells_owned_only_and_blocks_new_buys(self):
        self.submit(quantity=8)
        self.fill(8, status='filled')
        self.broker.account['holdings'] = {
            '005930': {'quantity': 8, 'sellable_quantity': 8, 'price': '7000'},
            '000660': {'quantity': 100, 'sellable_quantity': 100, 'price': '100000'}}
        self.broker.price = '7000'
        self.add_run()
        self.service._cycle()
        self.assertEqual(self.broker.submissions[-1], ('005930', 'sell', 8, '7000'))
        risk = self.service.learning_account.snapshot(self.service._policy())['risk']
        self.assertTrue(risk['active'])
        self.assertGreater(risk['drawdown_pct'], 15)
        before = len(self.broker.submissions)
        self.service._submit(self.service._policy(), self.broker, fixtures.decision('000660'), 'buy', 1,
                             Decimal('10000'), 'blocked', run_id='run1')
        self.assertEqual(len(self.broker.submissions), before)

    def test_loss_protection_cancels_pending_buys_before_any_new_exposure(self):
        self.submit(quantity=8)
        self.fill(8, status='filled')
        second = self.submit(quantity=1, symbol='000660', key='pending')
        self.broker.account['holdings']['005930'] = {'quantity': 8, 'sellable_quantity': 8, 'price': '7000'}
        self.service._cycle()
        self.assertEqual(self.broker.cancellations[0][0], second['order_id'])
        self.assertTrue(self.service.learning_account.snapshot(self.service._policy())['risk']['active'])

    def test_user_pause_keeps_protective_sales_stopped_and_reset_does_not_start(self):
        policy = self.service._policy()
        book = {'budget': policy['budget'], 'equity': '70000', 'cash': '70000',
                'as_of': '2026-10-06', 'pending_orders': 0, 'unresolved_orders': 0}
        self.service.learning_account.observe(policy, book, self.current.isoformat())
        self.service.command({'action': 'pause'})
        self.service._cycle()
        self.assertEqual(self.broker.submissions, [])
        self.service.command({'action': 'reset_risk'})
        self.assertFalse(self.service.learning_account.snapshot(policy)['risk']['active'])
        self.assertFalse(self.service.store.setting('enabled'))
        self.assertTrue(self.service.store.setting('user_paused'))

    def test_reset_with_owned_positions_is_rejected(self):
        self.submit(quantity=1)
        self.fill(1, status='filled')
        self.service.command({'action': 'pause'})
        with self.assertRaises(KisError):
            self.service.command({'action': 'reset_risk'})


if __name__ == '__main__':
    unittest.main()
