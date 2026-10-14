"""Frozen entry selection and exit compatibility in the pure transition preview."""
from copy import deepcopy
from decimal import Decimal
import unittest
from unittest.mock import patch

from backend.learning_policy import signals
from backend.policy_transition import transition_preview
from tests.test_learning_policy import row
from tests.test_policy_transition import LIMITS, candidate, fixture, lot


def ranked(symbol, score):
    value = row(symbol)
    value['features']['return_5d_pct'] = score
    return value


def entry_policy(**options):
    value = candidate(mode='equal_eligible', reserve=0, maximum=20, top_n=1)
    value.update(options)
    return value


class TransitionEntrySelectionTests(unittest.TestCase):
    def test_held_leader_is_excluded_before_top_n_only_when_opted_in(self):
        frame, context = fixture([lot('000001')],
            rows=[ranked('000001', 10), ranked('900001', 9), ranked('900002', 8)])
        original = deepcopy((frame, context, LIMITS))
        prior = transition_preview(entry_policy(), frame, LIMITS, context)
        opted = transition_preview(entry_policy(held_before_top_n=True), frame, LIMITS, context)
        self.assertEqual(prior['entry_count'], 0)
        self.assertEqual(prior['zero_entry_reason'], 'already_held_or_reserved')
        self.assertEqual([item['symbol'] for item in opted['entries']], ['900001'])
        self.assertEqual(opted['entry_context'], {'held_symbols': ['000001'], 'reserved_symbols': []})
        self.assertEqual((frame, context, LIMITS), original)

    def test_due_exit_does_not_reselect_the_frozen_top_n_after_freeing_slot(self):
        frame, context = fixture([lot('000001', age=4)],
            rows=[ranked('000001', 10), ranked('900001', 9)])
        policy = entry_policy(held_before_top_n=True, no_same_day_reentry=True)
        with patch('backend.policy_transition.signals', wraps=signals) as generate:
            result = transition_preview(policy, frame, LIMITS, context)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(generate.call_args.kwargs['entry_context'],
                         {'held_symbols': ['000001'], 'reserved_symbols': []})
        self.assertEqual([item['symbol'] for item in result['exits']], ['000001'])
        self.assertEqual([item['symbol'] for item in result['entries']], ['900001'])
        self.assertEqual([item['symbol'] for item in result['without_exit_fills']['entries']], ['900001'])
        self.assertEqual(result['signal_count'], 1)
        self.assertEqual(context['initial_state']['positions'][0]['quantity'], 1)

    def test_same_day_guard_blocks_old_lot_rebuy_without_rewriting_legacy_behavior(self):
        frame, context = fixture([lot('000001', age=4)], rows=[ranked('000001', 10)])
        legacy_behavior = transition_preview(entry_policy(), frame, LIMITS, context)
        explicit_false = transition_preview(entry_policy(held_before_top_n=False,
            no_same_day_reentry=False), frame, LIMITS, context)
        guarded = transition_preview(entry_policy(no_same_day_reentry=True), frame, LIMITS, context)
        self.assertEqual(legacy_behavior, explicit_false)
        self.assertEqual(legacy_behavior['entry_count'], 1)
        self.assertEqual(guarded['exit_count'], 1)
        self.assertEqual(guarded['entry_count'], 0)
        self.assertEqual(guarded['zero_entry_reason'], 'same_day_reentry')
        self.assertEqual(guarded['without_exit_fills']['zero_entry_reason'], 'already_held_or_reserved')

    def test_same_day_guard_preserves_unrelated_entry_and_all_cash_caps(self):
        frame, context = fixture([lot('000001', age=4)],
            rows=[ranked('000001', 10), ranked('900001', 9)])
        policy = entry_policy(no_same_day_reentry=True)
        policy['strategies'][0]['top_n'] = 2
        result = transition_preview(policy, frame, LIMITS, context)
        self.assertEqual([item['symbol'] for item in result['entries']], ['900001'])
        self.assertLessEqual(Decimal(result['entry_cost']), Decimal(LIMITS['daily_buy_limit']))
        self.assertTrue(all(Decimal(item['estimated_cost']) <= Decimal(LIMITS['order_cap'])
                            for item in result['entries']))
        self.assertEqual(result['exits'][0]['exit_policy']['holding_sessions'], 5)

    def test_old_signal_extension_uses_raw_frozen_lot_rule_with_opt_in_candidate(self):
        continuation = entry_policy(held_before_top_n=True, no_same_day_reentry=True)
        rule = {'holding_sessions': 2, 'stop_loss_pct': 0., 'take_profit_pct': 0.,
                'exit_mode': 'signal_extension', 'max_holding_sessions': 4,
                'continuation_policy': continuation, 'continuation_strategy': 'momentum'}
        position = lot('000001', age=1, price=100, entry_price='90', cost='90', exit_policy=rule)
        frame, context = fixture([position], rows=[ranked('000001', 10), ranked('900001', 9)])
        original = deepcopy(context)
        result = transition_preview(entry_policy(held_before_top_n=True), frame, LIMITS, context)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['exits'], [])
        self.assertEqual(result['retained_positions'][0]['exit_policy'], rule)
        self.assertEqual([item['symbol'] for item in result['entries']], ['900001'])
        self.assertEqual(context, original)
        context['initial_state']['positions'][0]['held_sessions'] = 3
        due = transition_preview(entry_policy(held_before_top_n=True), frame, LIMITS, context)
        self.assertEqual(due['exit_count'], 1)

    def test_pending_reservation_still_blocks_transition_instead_of_planning_around_it(self):
        frame, context = fixture(rows=[ranked('900001', 10), ranked('900002', 9)])
        context['initial_state'].update(pending_orders=1, reserved_cash='100',
            reserved_entries=[{'symbol': '900001', 'strategy_id': 'momentum', 'cost': '100'}])
        with patch('backend.policy_transition.signals', wraps=signals) as generate:
            result = transition_preview(entry_policy(held_before_top_n=True), frame, LIMITS, context)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('pending_orders', result['reasons'])
        self.assertEqual(result['reserved_slot_count'], 1)
        generate.assert_not_called()

    def test_current_ineligibility_does_not_replace_the_frozen_selected_symbol(self):
        frame, context = fixture([lot('000001')],
            rows=[ranked('000001', 10), ranked('900001', 9), ranked('900002', 8)])
        context['buy_eligibility'] = {'000001': True, '900001': False, '900002': True}
        result = transition_preview(entry_policy(held_before_top_n=True), frame, LIMITS, context)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['signal_count'], 1)
        self.assertEqual(result['entry_count'], 0)
        self.assertEqual(result['zero_entry_reason'], 'current_eligibility')

    def test_protection_precedes_entries_and_does_not_reset_the_frozen_book(self):
        frame, context = fixture([lot('000001')],
            rows=[ranked('000001', 10), ranked('900001', 9)])
        context['initial_state']['account_protection']['active'] = True
        original = deepcopy(context)
        result = transition_preview(entry_policy(held_before_top_n=True,
            no_same_day_reentry=True), frame, LIMITS, context)
        self.assertEqual(result['entry_count'], 0)
        self.assertEqual(result['zero_entry_reason'], 'account_protection_active')
        self.assertEqual(result['exit_count'], 1)
        self.assertEqual(result['protection']['units'], '10000')
        self.assertEqual(result['protection']['peak'], '1')
        self.assertEqual(context, original)


if __name__ == '__main__':
    unittest.main()
