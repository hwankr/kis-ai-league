"""Frozen-book capacity, not forecasts or orders; all fixtures stay in memory."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
import unittest

from backend.learning_policy import BASELINE_POLICY, LEGACY_IDS, FEE, TAX
from backend.policy_transition import transition_preview
from tests.test_learning_policy import policy, row


DAY = '2026-10-14'
DAYS = ['2026-10-08', '2026-10-12', '2026-10-13', DAY]
LIMITS = {'budget': '10000', 'order_cap': '1000', 'daily_buy_limit': '2000'}


def lot(symbol, *, age=1, quantity=1, price=100, strategy='old', **changes):
    return {'symbol': symbol, 'strategy_id': strategy, 'quantity': quantity,
            'cost': str(Decimal(price) * quantity * (1 + FEE)), 'entry_price': str(price),
            'last_close': str(price), 'held_sessions': age, 'exit_pending': False,
            'exit_policy': {'holding_sessions': 5, 'stop_loss_pct': 0., 'take_profit_pct': 0.}, **changes}


def fixture(positions=(), *, cash=None, rows=None):
    positions = deepcopy(list(positions))
    values = ([row(item['symbol'], price=float(item['last_close']), learning_eligible=False) for item in positions]
              + [row('900001'), row('900002')]) if rows is None else deepcopy(rows)
    frame = {'as_of': DAY, 'observed_at': DAY + 'T16:05:00+09:00',
             'session_days': DAYS, 'rows': values, 'baseline_signals': []}
    cost = sum((Decimal(item['cost']) for item in positions), Decimal(0))
    cash = Decimal(cash) if cash is not None else Decimal(LIMITS['budget']) - cost
    equity = cash + sum((Decimal(item['last_close']) * item['quantity'] * (1 - FEE - TAX)
                         for item in positions), Decimal(0))
    book = {'as_of': DAY, 'budget': LIMITS['budget'], 'cash': str(cash), 'equity': str(equity),
            'reserved_cash': '0', 'positions': positions, 'pending_orders': 0, 'unresolved_orders': 0,
            'reserved_entries': [], 'account_protection': {'units': '10000', 'peak': '1',
                                                        'active': False, 'limit_pct': 15.}}
    return frame, {'signal_day': DAY, 'observed_at': DAY + 'T16:05:00+09:00',
                   'initial_state': book, 'execution_model': {'mode': 'daily_open'}}


def candidate(*, mode='legacy', maximum=6, reserve=.4, top_n=2):
    value = policy(cash_reserve=reserve, max_positions=maximum)
    value['strategies'][0].update(weight=1-reserve, top_n=top_n)
    if mode != 'legacy':
        value['allocation_mode'] = mode
    return value


class TransitionPreviewTests(unittest.TestCase):
    def preview(self, policy=None, positions=(), **values):
        frame, context = fixture(positions, **values)
        return transition_preview(policy or candidate(), frame, LIMITS, context)

    def test_eight_holdings_under_six_limit_have_no_new_capacity_or_forced_sales(self):
        result = self.preview(positions=[lot(f'{index:06d}') for index in range(8)])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual((result['slots_before'], result['slots_after'], result['entry_count']), (0, 0, 0))
        self.assertEqual(result['zero_entry_reason'], 'position_limit')
        self.assertEqual(result['gross_buy_amount'], '0')
        self.assertEqual(result['exits'], [])
        self.assertEqual(len(result['retained_positions']), 8)

    def test_three_due_exits_free_one_slot_only_after_their_assumed_fills(self):
        positions = [lot(f'{index:06d}', age=4 if index < 3 else 1) for index in range(8)]
        result = self.preview(positions=positions)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual((result['exit_count'], result['slots_after'], result['entry_count']), (3, 1, 1))
        self.assertEqual(Decimal(result['gross_sell_amount']), 300)
        self.assertEqual(result['without_exit_fills']['entry_count'], 0)
        self.assertTrue(result['requires_exit_fills'])
        self.assertTrue(all(item['completed_holding_sessions'] == 5 for item in result['exits']))

    def test_existing_entry_rule_is_not_replaced_by_candidate_exit(self):
        value = candidate()
        value['strategies'][0].update(holding_sessions=1, stop_loss_pct=1., take_profit_pct=1.)
        result = self.preview(value, positions=[lot('000001', age=1)])
        self.assertEqual(result['exits'], [])
        self.assertEqual(result['retained_positions'][0]['exit_policy']['holding_sessions'], 5)
        self.assertEqual(result['entries'][0]['exit_policy']['holding_sessions'], 1)

    def test_stop_uses_known_close_and_original_entry_price(self):
        position = lot('000001', price=100, entry_price='110', age=0,
                       exit_policy={'holding_sessions': 6, 'stop_loss_pct': 5., 'take_profit_pct': 11.})
        result = self.preview(positions=[position])
        self.assertEqual(result['exit_count'], 1)
        self.assertEqual(result['exits'][0]['completed_holding_sessions'], 1)

    def test_zero_signals_is_ready_without_inventing_future_signals(self):
        result = self.preview(rows=[row('900001', learning_eligible=False)])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['zero_entry_reason'], 'no_signal')
        self.assertEqual(result['entry_count'], 0)

    def test_held_signal_is_not_an_additional_position(self):
        result = self.preview(positions=[lot('900001')], rows=[row('900001')])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['zero_entry_reason'], 'already_held_or_reserved')
        self.assertEqual(result['entry_count'], 0)

    def test_incomplete_book_protection_and_unknown_orders_are_unavailable(self):
        for missing in ['cash', 'positions', 'pending_orders', 'unresolved_orders', 'account_protection']:
            frame, context = fixture()
            context['initial_state'].pop(missing)
            with self.subTest(missing=missing):
                self.assertEqual(transition_preview(candidate(), frame, LIMITS, context)['status'], 'unavailable')
        for missing in ['units', 'peak', 'active', 'limit_pct']:
            frame, context = fixture()
            context['initial_state']['account_protection'].pop(missing)
            with self.subTest(protection=missing):
                self.assertEqual(transition_preview(candidate(), frame, LIMITS, context)['status'], 'unavailable')
        for key in ['pending_orders', 'unresolved_orders']:
            frame, context = fixture()
            context['initial_state'][key] = 1
            result = transition_preview(candidate(), frame, LIMITS, context)
            self.assertEqual(result['status'], 'unavailable')
            self.assertIn(key, result['reasons'])

    def test_reserved_slots_are_reported_but_unsettled_orders_do_not_get_a_plan(self):
        frame, context = fixture()
        context['initial_state'].update(pending_orders=1, reserved_cash='100',
            reserved_entries=[{'symbol': '900001', 'strategy_id': 'momentum', 'cost': '100'}])
        result = transition_preview(candidate(), frame, LIMITS, context)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['reserved_slot_count'], 1)
        self.assertIsNone(result['entry_count'])

    def test_active_cumulative_protection_blocks_buys_and_keeps_original_state(self):
        frame, context = fixture([lot('000001')])
        context['initial_state']['account_protection']['active'] = True
        original = deepcopy(context)
        result = transition_preview(candidate(), frame, LIMITS, context)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['zero_entry_reason'], 'account_protection_active')
        self.assertEqual(result['exit_count'], 1)
        self.assertEqual(context, original)
        self.assertEqual(result['protection']['units'], '10000')
        self.assertEqual(result['protection']['peak'], '1')

    def test_known_valuation_drawdown_can_activate_existing_protection(self):
        frame, context = fixture([lot('000001')], cash='8000')
        result = transition_preview(candidate(), frame, LIMITS, context)
        self.assertTrue(result['protection']['active'])
        self.assertFalse(result['protection']['prior_active'])
        self.assertEqual(result['entry_count'], 0)

    def test_legacy_quotas_do_not_redistribute_ineligible_signal_allocation(self):
        frame, context = fixture()
        context['buy_eligibility'] = {'900001': False, '900002': True}
        # Order cap is 1000 here, so use a 2000 cap to make redistribution visible.
        limits = {**LIMITS, 'order_cap': '2000'}
        legacy = transition_preview(candidate(reserve=0), frame, limits, context)
        eligible = transition_preview(candidate(mode='equal_eligible', reserve=0), frame, limits, context)
        self.assertEqual(legacy['entries'][0]['quantity'], 9)
        self.assertEqual(eligible['entries'][0]['quantity'], 19)

    def test_cash_reserve_and_integer_fee_cost_are_hard_caps(self):
        frame, context = fixture(cash='4050')
        context['initial_state']['account_protection']['limit_pct'] = 100.
        result = transition_preview(candidate(), frame, LIMITS, context)
        self.assertEqual(result['entry_count'], 0)
        self.assertEqual(result['zero_entry_reason'], 'cash_or_allocation_limit')
        result = self.preview(candidate(reserve=0))
        self.assertLessEqual(Decimal(result['entry_cost']), Decimal(LIMITS['daily_buy_limit']))
        self.assertTrue(all(Decimal(item['estimated_cost']) <= Decimal(LIMITS['order_cap']) for item in result['entries']))

    def test_position_and_strategy_budget_remain_used_for_inherited_lots(self):
        frame, context = fixture([lot('000001', quantity=50, strategy='momentum')])
        context['initial_state']['account_protection']['limit_pct'] = 100.
        result = transition_preview(candidate(), frame, LIMITS, context)
        remaining = Decimal('6000') - Decimal(context['initial_state']['positions'][0]['cost'])
        self.assertLessEqual(Decimal(result['entry_cost']), remaining)
        self.assertGreater(result['entry_count'], 0)

    def test_next_actual_session_age_matches_live_calendar_without_guessing_weekdays(self):
        positions = [lot('000001', age=2, entry_date='2026-10-12'),
                     lot('000002', age=0, entry_date=DAY)]
        frame, context = fixture(positions)
        for item in context['initial_state']['positions']:
            item['exit_policy']['holding_sessions'] = item['held_sessions'] + 1
        unknown = transition_preview(candidate(), frame, LIMITS, context)
        self.assertEqual(unknown['next_session_basis'], 'next_actual_session')
        self.assertIsNone(unknown['next_session'])
        self.assertEqual([item['completed_holding_sessions'] for item in unknown['exits']], [3, 1])
        context['next_session'] = '2026-10-15'
        explicit = transition_preview(candidate(), frame, LIMITS, context)
        self.assertEqual(explicit['exits'], unknown['exits'])
        context['initial_state']['positions'][0]['held_sessions'] = 3
        self.assertEqual(transition_preview(candidate(), frame, LIMITS, context)['status'], 'unavailable')

    def test_invalid_or_mixed_snapshot_cannot_turn_into_zero_activity(self):
        changes = [lambda f,c: c.update(observed_at=DAY+'T15:59:00+09:00'),
                   lambda f,c: c.update(next_session=DAY),
                   lambda f,c: c.update(account_blockers=['account_mismatch']),
                   lambda f,c: c.update(buy_eligibility={'900001': 'yes'}),
                   lambda f,c: c['initial_state'].update(cash='NaN'),
                   lambda f,c: c['initial_state'].update(equity='1'),
                   lambda f,c: f['rows'].append(deepcopy(f['rows'][0])),
                   lambda f,c: f.update(rows=[]),
                   lambda f,c: f.update(missing_symbols=['900003']),
                   lambda f,c: f.update(observed_at='2026-10-15T16:00:00+09:00'),
                   lambda f,c: c.update(session_days=DAYS+['2026-10-15'])]
        for index, change in enumerate(changes):
            frame, context = fixture()
            change(frame, context)
            with self.subTest(index=index):
                result = transition_preview(candidate(), frame, LIMITS, context)
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(result['zero_entry_reason'], 'unavailable')
                self.assertIsNone(result['entry_count'])
                self.assertIsNone(result['gross_buy_amount'])
                self.assertIsNone(result['gross_sell_amount'])

    def test_award_context_fields_are_checked_and_every_input_is_immutable(self):
        frame, context = fixture([lot('000001')])
        context.update(version='award-v1', latest_frame=deepcopy(frame), limits=deepcopy(LIMITS),
                       competition_context={'status': 'ok'}, hash='owned-by-context-module')
        value = candidate()
        original = deepcopy((value, frame, LIMITS, context))
        result = transition_preview(value, frame, LIMITS, context)
        self.assertEqual(result['status'], 'ready')
        json.dumps(result, allow_nan=False)
        self.assertEqual((value, frame, LIMITS, context), original)
        context['limits']['budget'] = '20000'
        self.assertEqual(transition_preview(value, frame, LIMITS, context)['status'], 'unavailable')

    def test_legacy_parent_requires_observed_baseline_and_keeps_rotation_uncertainty(self):
        frame, context = fixture(rows=[row('900001')])
        self.assertEqual(transition_preview(BASELINE_POLICY, frame, LIMITS, context)['status'], 'unavailable')
        frame['baseline_signals'] = [{'symbol': '900001', 'strategy_id': strategy,
                                     'status': 'ready', 'action': 'buy'} for strategy in LEGACY_IDS]
        result = transition_preview(BASELINE_POLICY, frame, LIMITS, context)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['legacy_priority_range']['rotations'], 4)
        self.assertEqual(result['entry_count'], 1)
        self.assertEqual(result['entries'][0]['quantity'], 4)
        context['next_session'] = '2026-10-15'
        result = transition_preview(BASELINE_POLICY, frame, LIMITS, context)
        self.assertNotIn('legacy_priority_range', result)
        expected = LEGACY_IDS[date(2026,10,15).toordinal() % len(LEGACY_IDS)]
        self.assertEqual(result['entries'][0]['strategy_id'], expected)

    def test_legacy_counts_restricted_baseline_buy_before_live_eligibility(self):
        frame, context = fixture(rows=[row('900001', learning_eligible=False), row('900002')])
        frame['baseline_signals'] = [{'symbol': symbol, 'strategy_id': strategy, 'status': 'ready',
                                     'action': 'buy' if strategy == LEGACY_IDS[0] else 'hold'}
                                    for strategy in LEGACY_IDS for symbol in ['900001','900002']]
        result = transition_preview(BASELINE_POLICY, frame, LIMITS, context)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['entries'][0]['quantity'], 2)

    def test_lot_signal_extension_uses_its_frozen_policy_not_the_candidate(self):
        continuation = candidate(maximum=20, reserve=0, top_n=1)
        rule = {'holding_sessions': 2, 'stop_loss_pct': 0., 'take_profit_pct': 0.,
                'exit_mode': 'signal_extension', 'max_holding_sessions': 4,
                'continuation_policy': continuation, 'continuation_strategy': 'momentum'}
        position = lot('000001', age=1, price=110, entry_price='100', cost='100', exit_policy=rule)
        frame, context = fixture([position], rows=[row('000001', price=110)])
        result = transition_preview(candidate(), frame, LIMITS, context)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['exit_count'], 0)
        context['initial_state']['positions'][0]['held_sessions'] = 3
        result = transition_preview(candidate(), frame, LIMITS, context)
        self.assertEqual(result['exit_count'], 1)

    def test_eligible_allocation_deduplicates_strategy_votes_and_preserves_caps(self):
        value = candidate(mode='equal_eligible', reserve=0)
        value['strategies'][0]['weight'] = .5
        second = deepcopy(value['strategies'][0])
        second.update(id='other', weight=.5)
        value['strategies'].append(second)
        result = self.preview(value)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['entry_count'], 2)
        self.assertEqual(len({item['symbol'] for item in result['entries']}), 2)
        self.assertLessEqual(Decimal(result['entry_cost']), Decimal(LIMITS['daily_buy_limit']))

    def test_less_expensive_later_candidate_can_use_slot_when_legacy_first_cannot_afford_one(self):
        value = candidate(reserve=0, maximum=1)
        frame, context = fixture(rows=[row('900001', price=2000), row('900002', price=100)])
        result = transition_preview(value, frame, LIMITS, context)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['entries'][0]['symbol'], '900002')
        self.assertEqual(result['entry_count'], 1)

    def test_frame_identity_and_eligibility_are_required_snapshot_evidence(self):
        for key, value in [('symbol', 1), ('symbol', 'bad'), ('board', 'unknown'),
                           ('features', None), ('learning_eligible', 'yes')]:
            frame, context = fixture()
            frame['rows'][0][key] = value
            with self.subTest(key=key, value=value):
                result = transition_preview(candidate(), frame, LIMITS, context)
                self.assertEqual(result['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
