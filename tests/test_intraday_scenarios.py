"""Intraday evidence remains separate from its real completed-close source."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import unittest

from backend.daily_inputs import freeze_award_context, validate_award_context
from backend.intraday_scenarios import (award_scenarios, freeze_intraday_context,
    scenario_plan, transition_preview, validate_intraday_context, _hash, _projection)
from backend.intraday_scenarios import _prefix
from backend.learning_policy import FEE, TAX
from backend.policy_award_scenarios import _path
from tests.test_policy_award import progress
from tests.test_policy_award_scenarios import inputs
from tests.test_policy_transition import candidate, lot, LIMITS


def fixture(*, held=False, horizon=4):
    positions = [lot('900001', age=2, entry_date='2026-10-12')] if held else []
    frames, original = inputs(horizon=horizon + 1, count=36, positions=positions)
    backwards, day = [], date(2026, 10, 14)
    while len(backwards) < len(frames):
        if day.weekday() < 5:
            backwards.append(day.isoformat())
        day -= timedelta(days=1)
    calendar = list(reversed(backwards))
    for index, item in enumerate(frames):
        item.update(as_of=calendar[index], observed_at=calendar[index]+'T16:05:00+09:00',
                    session_days=calendar[:index+1])
    original['latest_frame']['session_days'] = calendar
    frames[-1] = deepcopy(original['latest_frame'])
    contest = progress()
    contest['period']['end_date'] = '2026-10-21' if horizon == 4 else '2026-11-20'
    contest['pace']['remaining_sessions'] = horizon + 1
    source = freeze_award_context(signal_day='2026-10-14', observed_at='2026-10-14T16:05:00+09:00',
        latest_frame=frames[-1], initial_state=original['initial_state'],
        execution_model={'mode': 'daily_open'}, competition_context=contest, limits=LIMITS)
    current = deepcopy(source['initial_state'])
    current['as_of'] = '2026-10-15'
    for position in current['positions']:
        position['held_sessions'] += 1
        position['last_close'] = '105'
    current['equity'] = str(Decimal(current['cash']) + sum((Decimal(position['last_close'])
        * position['quantity'] * (1-FEE-TAX) for position in current['positions']), Decimal(0)))
    current['account_protection']['peak'] = '1.01'
    contest = deepcopy(source['competition_context'])
    contest['period']['as_of'] = '2026-10-15'
    contest['pace']['remaining_sessions'] = horizon
    value = freeze_intraday_context(source_award_context=source, current_book=current,
        observed_at='2026-10-15T11:30:00+09:00', activation_day='2026-10-15',
        competition_context=contest, session_days=frames[-1]['session_days'] + ['2026-10-15'],
        broker_calendar_evidence={'date': '2026-10-15', 'is_trading_day': True,
            'next_session': '2026-10-16', 'source': 'broker_read_only_fixture',
            'observed_at': '2026-10-15T11:29:00+09:00'}, today_order_count=0, today_fill_count=0,
        current_quotes={row['symbol']: {'price': 105 if held else row['close'],
            'observed_at': '2026-10-15T11:29:50+09:00', 'eligible': True, 'volume': 10000}
            for row in frames[-1]['rows']})
    return frames, value


def rehash(value):
    value['hash'] = _hash({key: item for key, item in value.items() if key != 'hash'})


class IntradayScenarioTests(unittest.TestCase):
    def test_separate_context_preserves_source_and_rejects_daily_validation(self):
        frames, context = fixture(held=True)
        before = deepcopy((frames, context))
        self.assertTrue(validate_intraday_context(context))
        self.assertFalse(validate_award_context(context))
        source, book, future = _projection(context)
        self.assertEqual((source['signal_day'], context['latest_frame']['as_of']), ('2026-10-14',)*2)
        self.assertEqual(book['as_of'], '2026-10-15')
        self.assertEqual(book['positions'][0]['held_sessions'], 3)
        self.assertEqual(book['positions'][0]['last_close'], '100')
        self.assertEqual(book['account_protection'], context['current_book']['account_protection'])
        self.assertEqual((future[0], future[-1]), ('2026-10-16', '2026-10-21'))
        self.assertEqual((frames, context), before)

    def test_26_24_forward_sessions_exclude_activation_day_and_end_at_deadline(self):
        frames, context = fixture(horizon=26)
        value = award_scenarios(candidate(reserve=0), frames, LIMITS, context)
        self.assertEqual(value['status'], 'ready', value)
        self.assertEqual({item['horizon'] for item in value['scenarios']}, {26, 24})
        self.assertEqual({item['assumed_start'] for item in value['scenarios']}, {'2026-10-16'})
        self.assertEqual({item['assumed_end'] for item in value['scenarios']}, {'2026-11-18', '2026-11-20'})
        self.assertEqual(value['projection']['actual_ledger_gross_added'], 0)
        self.assertTrue(value['projection']['intraday_fill_effects_included'])
        self.assertEqual(value['scenario_count'], 24)

    def test_reference_capacity_is_separate_from_modelled_quote_prefix(self):
        frames, context = fixture()
        preview = transition_preview(candidate(reserve=0), frames[-1], LIMITS, context)
        self.assertEqual(preview['status'], 'ready')
        self.assertGreater(preview['entry_count'], 0)
        self.assertEqual(preview['basis'], 'observed_quote_conditional_remaining_session_capacity')
        self.assertFalse(preview['scenario_fill_evidence'])
        result = award_scenarios(candidate(reserve=0), frames, LIMITS, context)
        self.assertEqual(result['status'], 'ready', result)
        self.assertTrue(all(item['assumed_start'] == '2026-10-16' for item in result['scenarios']))
        self.assertGreater(result['intraday_prefixes']['normal']['normal']['gross_buy_amount'], 0)
        self.assertEqual(result['projection']['actual_ledger_gross_added'], 0)

    def test_economic_mutations_are_rejected_even_when_rehashed(self):
        _, original = fixture(held=True)
        mutations = [lambda c: c.update(today_order_count=1), lambda c: c.update(today_fill_count=1),
            lambda c: c['current_book'].update(cash='9000'),
            lambda c: c['current_book']['positions'][0].update(quantity=2),
            lambda c: c['current_book']['positions'][0]['exit_policy'].update(holding_sessions=1),
            lambda c: c['current_book']['positions'][0].update(held_sessions=4),
            lambda c: c['current_book'].update(pending_orders=1),
            lambda c: c['competition_context']['pace'].update(gross_amount=999999),
            lambda c: c['current_book']['account_protection'].update(limit_pct=20),
            lambda c: c['current_book']['account_protection'].update(peak='0.9')]
        for mutation in mutations:
            value = deepcopy(original)
            mutation(value)
            value['initial_state'] = deepcopy(value['current_book'])
            value['current_book_hash'] = _hash(value['current_book'])
            rehash(value)
            self.assertFalse(validate_intraday_context(value), mutation)

    def test_wrong_calendar_horizon_and_future_observation_are_rejected(self):
        _, original = fixture()
        for mutate in (lambda c: c['competition_context']['pace'].update(remaining_sessions=5),
            lambda c: c['broker_calendar_evidence'].update(is_trading_day=False),
            lambda c: c['broker_calendar_evidence'].update(next_session='2026-10-19'),
            lambda c: c.update(observed_at='2026-10-15T16:05:00+09:00')):
            value = deepcopy(original)
            mutate(value)
            rehash(value)
            self.assertFalse(validate_intraday_context(value))

    def test_source_frame_and_plan_hashes_are_bound_and_inputs_unmodified(self):
        frames, context = fixture()
        original = deepcopy((frames, context))
        plan = scenario_plan(frames, context)
        self.assertEqual(plan['status'], 'ready', plan)
        self.assertEqual(plan['projection']['source_frame_hash'], _hash(frames[-1]))
        invalid = deepcopy(plan)
        invalid['schedules'][0]['horizon'] += 1
        rehash(invalid)
        rejected = award_scenarios(candidate(), frames, LIMITS, context, plan=invalid)
        self.assertEqual(rejected['reason'], 'intraday_scenario_plan_mismatch')
        self.assertEqual((frames, context), original)

    def test_model_anchor_is_explicit_and_does_not_relabel_original_observation(self):
        frames, context = fixture(held=True)
        original = deepcopy(frames[-1])
        source, book, _ = _projection(context)
        path = _path(candidate(), frames[:5], frames[-1], book, simulation_anchor_day='2026-10-15')
        self.assertEqual(frames[-1], original)
        self.assertEqual(path[0]['as_of'], '2026-10-15')
        self.assertFalse(path[0]['simulation_anchor']['observed_close'])
        self.assertEqual(path[0]['simulation_anchor']['source_as_of'], '2026-10-14')
        self.assertEqual(path[0]['rows'], original['rows'])

    def test_existing_exit_and_current_protection_survive_projection(self):
        frames, context = fixture(held=True)
        original_rule = deepcopy(context['current_book']['positions'][0]['exit_policy'])
        changed = candidate(reserve=0)
        changed['strategies'][0]['holding_sessions'] = 1
        preview = transition_preview(changed, frames[-1], LIMITS, context)
        self.assertEqual(preview['retained_positions'][0]['exit_policy'], original_rule)
        context['current_book']['account_protection']['active'] = True
        context['initial_state'] = deepcopy(context['current_book'])
        context['current_book_hash'] = _hash(context['current_book'])
        rehash(context)
        preview = transition_preview(changed, frames[-1], LIMITS, context)
        self.assertEqual(preview['entry_count'], 0)
        self.assertTrue(preview['protection']['active'])

    def test_account_blockers_prevent_ready_capacity_and_scenarios(self):
        frames, context = fixture()
        context['account_blockers'] = ['broker_unconfirmed']
        rehash(context)
        self.assertEqual(transition_preview(candidate(), frames[-1], LIMITS, context)['status'], 'unavailable')
        self.assertEqual(award_scenarios(candidate(), frames, LIMITS, context)['status'], 'unavailable')

    def test_prefix_integer_quantity_cost_and_new_lot_clock_carry_into_forward_book(self):
        _, context = fixture()
        value = candidate(reserve=0)
        metric, _, _ = _prefix(value, LIMITS, context, partial_fills=False)
        lot_after = metric['final_state']['positions'][0]
        self.assertEqual(lot_after['quantity'], 9)  # 1,000 per order, 100 quote plus commission.
        self.assertEqual(lot_after['held_sessions'], 0)
        self.assertEqual(Decimal(lot_after['cost']), Decimal(900)*(1+FEE))
        self.assertEqual(Decimal(metric['final_state']['cash']), Decimal(10000)-Decimal(900)*(1+FEE))
        self.assertEqual(Decimal(metric['stress_final_state']['cash']), Decimal(10000)-Decimal(900)*(1+FEE+Decimal('.001')))
        partial, _, _ = _prefix(value, LIMITS, context, partial_fills=True)
        self.assertEqual(partial['final_state']['positions'][0]['quantity'], 4)
        self.assertEqual(partial['final_state']['positions'][0]['held_sessions'], 0)

    def test_today_prefix_fills_are_added_once_and_actual_progress_is_untouched(self):
        frames, context = fixture()
        actual = deepcopy(context['competition_context'])
        value = candidate(reserve=0)
        value['strategies'][0]['holding_sessions'] = 6
        result = award_scenarios(value, frames, LIMITS, context)
        self.assertEqual(result['status'], 'ready', result)
        normal = next(item for item in result['scenarios'] if item['id'] == 'first_full_normal')
        self.assertEqual(normal['gross_buy_amount'], 900)
        self.assertEqual(normal['trading_days'], 1)
        self.assertEqual(normal['first_buy_session'], 0)
        self.assertFalse(normal['intraday_modelled_fills_are_actual'])
        self.assertEqual(context['competition_context'], actual)
        self.assertEqual(normal['final_holding_count'], 1)

    def test_quote_gap_blocks_today_without_reselecting_other_symbols(self):
        frames, context = fixture()
        context['current_quotes']['900001']['price'] = 102  # frozen close 100, 1% limit.
        context['current_quotes_hash'] = _hash(context['current_quotes'])
        rehash(context)
        preview = transition_preview(candidate(reserve=0), frames[-1], LIMITS, context)
        self.assertEqual(preview['status'], 'ready', preview)
        self.assertGreater(preview['reference_close_capacity']['entry_count'], 0)
        self.assertEqual(preview['entry_count'], 0)
        self.assertEqual(preview['actual_orders_sent'], 0)
        self.assertGreater(preview['execution_diagnostics']['limit_blocked'], 0)
        self.assertEqual(Decimal(preview['cash_after_entries']), Decimal(context['current_book']['cash']))
        self.assertLess(Decimal(preview['reference_close_capacity']['cash_after_entries']),
                        Decimal(preview['cash_after_entries']))

    def test_preview_cash_and_protection_mark_use_the_quote_prefix_not_prior_close_plan(self):
        frames, context = fixture()
        context['current_quotes']['900001']['price'] = 99
        context['current_quotes_hash'] = _hash(context['current_quotes'])
        rehash(context)
        policy = candidate(reserve=0)
        preview = transition_preview(policy, frames[-1], LIMITS, context)
        metric, _, _ = _prefix(policy, LIMITS, context, partial_fills=False)
        final = metric['final_state']
        self.assertEqual(preview['cash_after_entries'], final['cash'])
        self.assertNotEqual(preview['cash_after_entries'], preview['reference_close_capacity']['cash_after_entries'])
        expected_mark = Decimal(final['cash']) + sum((Decimal(item['last_close']) * item['quantity']
            * (1-FEE-TAX) for item in final['positions']), Decimal(0))
        expected_drawdown = float((1-expected_mark/Decimal(final['account_protection']['units'])
            /Decimal(final['account_protection']['peak']))*100)
        self.assertAlmostEqual(preview['protection']['drawdown_pct'], expected_drawdown, places=10)

    def test_quote_freshness_is_bound_to_snapshot_and_missing_selected_quote_fails(self):
        frames, context = fixture()
        context['current_quotes']['900001']['observed_at'] = '2026-10-15T11:26:59+09:00'
        context['current_quotes_hash'] = _hash(context['current_quotes'])
        rehash(context)
        self.assertFalse(validate_intraday_context(context))
        frames, context = fixture()
        context['latest_frame']['rows'].append(deepcopy(context['latest_frame']['rows'][0]))
        # Raw source mutation cannot be repaired by only replacing the top hash.
        rehash(context)
        self.assertFalse(validate_intraday_context(context))

    def test_path_input_hashes_are_paired_across_policies_despite_different_fill_books(self):
        frames, context = fixture()
        parent = candidate(reserve=0)
        child = candidate(reserve=0, mode='equal_eligible')
        child['strategies'][0]['holding_sessions'] = 1
        a = award_scenarios(parent, frames, LIMITS, context)
        b = award_scenarios(child, frames, LIMITS, context)
        self.assertEqual((a['status'], b['status']), ('ready', 'ready'))
        self.assertEqual(a['scenario_plan_hash'], b['scenario_plan_hash'])
        self.assertEqual(a['start_book_hash'], b['start_book_hash'])
        self.assertEqual([item['path_hash'] for item in a['scenarios']], [item['path_hash'] for item in b['scenarios']])


if __name__ == '__main__':
    unittest.main()
