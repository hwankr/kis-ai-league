"""Terminal-horizon regressions use synthetic fixtures without services or storage."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import math
import unittest

from backend.daily_inputs import freeze_award_context
from backend.policy_award import (
    _family_status, _return_objective, joint_qualification, select_award_record, verify_award_record_evidence,
)
from backend import intraday_scenarios
from backend.learning_policy import FEE, TAX, policy_id
from backend.policy_award_scenarios import award_scenarios, scenario_plan, _hash, TERMINAL_BASIS
from tests.test_policy_award import progress, outcome
from tests.test_policy_award_scenarios import inputs
from tests.test_policy_transition import candidate, lot, LIMITS
from tests.test_intraday_scenarios import fixture as intraday_fixture


def terminal_paths(result):
    return [item for item in result['scenarios'] if item['schedule'] == 'shortened']


def native_inputs(horizon):
    frames, context = inputs(horizon=horizon, count=12)
    latest = context['latest_frame']
    current, calendar = date.fromisoformat(latest['as_of']), []
    while len(calendar) < len(frames):
        if current.weekday() < 5:
            calendar.append(current.isoformat())
        current -= timedelta(days=1)
    calendar.reverse()
    for index, frame in enumerate(frames):
        frame.update(as_of=calendar[index], observed_at=calendar[index]+'T16:05:00+09:00',
            session_days=calendar[:index+1])
        price = 40 + index * 4
        frame['rows'][0].update(open=price-4, high=price*1.02, low=(price-4)*.99, close=price)
    latest['session_days'] = calendar
    latest['rows'][0].update(open=frames[-2]['rows'][0]['close'], low=frames[-2]['rows'][0]['close']*.99)
    frames[-1] = deepcopy(latest)
    actual = progress()
    actual['pace']['remaining_sessions'] = horizon
    frozen = freeze_award_context(signal_day=latest['as_of'], observed_at=latest['observed_at'],
        latest_frame=latest, initial_state=context['initial_state'], execution_model=context['execution_model'],
        competition_context=actual, limits=LIMITS)
    return frames, frozen


class TerminalAwardTests(unittest.TestCase):
    def assert_complete_family(self, horizon):
        frames, context = inputs(horizon=horizon, count=12)
        plan = scenario_plan(frames, context)
        paths = award_scenarios(candidate(reserve=0, top_n=1), frames, LIMITS, context, plan=plan)
        actual = progress()
        actual['pace']['remaining_sessions'] = horizon
        qualification = joint_qualification(paths, actual, LIMITS['budget'])
        family = _family_status(paths, qualification, plan, 15.)

        self.assertEqual(plan['status'], 'ready', plan['reason'])
        self.assertEqual(paths['status'], 'ready', paths['reason'])
        self.assertEqual((paths['scenario_count'], paths['ready_scenarios']), (24, 24))
        self.assertEqual(qualification['groups']['core']['scenario_count'], 12)
        self.assertEqual(qualification['groups']['reserve']['scenario_count'], 12)
        self.assertEqual(family, 'ready')
        objective = _return_objective(paths, family)
        self.assertIsNotNone(objective)
        self.assertTrue(math.isfinite(objective))

    def test_one_remaining_session_has_complete_family_and_finite_objective(self):
        self.assert_complete_family(1)

    def test_two_remaining_sessions_have_complete_family_and_finite_objective(self):
        self.assert_complete_family(2)

    def test_three_remaining_sessions_keep_complete_family(self):
        self.assert_complete_family(3)

    def test_four_remaining_sessions_keep_complete_family(self):
        self.assert_complete_family(4)

    def test_no_remaining_sessions_keeps_current_policy(self):
        _, context = inputs(horizon=0, count=12)
        latest = context['latest_frame']
        actual = progress()
        actual['pace']['remaining_sessions'] = 0
        frozen = freeze_award_context(signal_day=latest['as_of'], observed_at=latest['observed_at'],
            latest_frame=latest, initial_state=context['initial_state'],
            execution_model=context['execution_model'], competition_context=actual, limits=LIMITS)

        selected, audit = select_award_record(candidate(), [], [latest], LIMITS, frozen)

        self.assertIsNone(selected)
        self.assertEqual(audit['status'], 'keep')
        self.assertEqual(audit['reason'], 'contest_has_no_remaining_sessions')
        self.assertNotIn('scenario_plan', audit)

    def test_zero_schedule_marks_book_without_advancing_exit_or_cost_state(self):
        position = lot('900001', age=4, quantity=4, exit_pending=True,
            exit_fill_remainder='.25', exit_delay_remaining=1)
        frames, context = inputs(horizon=2, positions=[position], eligible=False)
        before = deepcopy((frames, context))
        result = award_scenarios(candidate(reserve=0), frames, LIMITS, context)
        self.assertEqual(result['status'], 'ready', result)
        book = context['initial_state']
        expected = float(Decimal(book['cash']) + Decimal('400'))
        for item in terminal_paths(result):
            self.assertEqual(item['final_equity'], expected)
            self.assertEqual(item['final_state_hash'], _hash(book))
            self.assertEqual(item['final_protection'], book['account_protection'])
            self.assertTrue(all(value == 0 for value in item['future_activity'].values()))
            self.assertEqual((item['gross_amount'], item['fees_paid'], item['taxes_paid'], item['api_orders']), (0, 0, 0, 0))
            self.assertEqual((item['max_drawdown_pct'], item['first_buy_session']), (0, None))
            self.assertEqual(item['basis'], TERMINAL_BASIS)
        self.assertEqual((frames, context), before)
        self.assertTrue(any(item['final_state_hash'] != _hash(book) for item in result['scenarios'] if item['schedule'] == 'full'))

    def test_terminal_drawdown_keeps_cumulative_peak_separate_from_path_drawdown(self):
        frames, context = inputs(horizon=1)
        context['initial_state']['account_protection']['peak'] = '1.1'
        result = award_scenarios(candidate(), frames, LIMITS, context)
        for item in terminal_paths(result):
            self.assertEqual(item['max_drawdown_pct'], 0)
            self.assertAlmostEqual(item['cumulative_protection_drawdown_pct'], (1-1/1.1)*100)
            self.assertFalse(item['cumulative_protection_active'])
            self.assertEqual(item['final_protection']['peak'], '1.1')

    def test_terminal_aliases_keep_original_mean_weights_and_ignore_order(self):
        for horizon in (1, 2):
            frames, context = inputs(horizon=horizon)
            result = award_scenarios(candidate(), frames, LIMITS, context)
            plan = result['scenario_plan']
            short = plan['schedules'][1]
            self.assertEqual((short['horizon'], short['unique_path_count'], short['source_windows']), (0, 1, []))
            self.assertTrue(all(item['alias_of'] == 'first_shortened' for item in short['selected']))
            qualification = joint_qualification(result, progress(), LIMITS['budget'])
            family = _family_status(result, qualification, plan, 15)
            full = [item['final_equity'] for item in result['scenarios']
                    if item['schedule'] == 'full' and item['environment'] in ('normal', 'cost_stress')]
            self.assertAlmostEqual(_return_objective(result, family), (math.fsum(full)/6 + 10000)/2)
            reordered = deepcopy(result)
            reordered['scenarios'].reverse()
            reordered_plan = deepcopy(plan)
            for schedule in reordered_plan['schedules']:
                schedule['selected'].reverse()
            self.assertEqual(_family_status(reordered, qualification, reordered_plan, 15), family)
            self.assertEqual(_return_objective(reordered, family), _return_objective(result, family))

    def test_missing_full_history_is_not_replaced_by_terminal_aliases(self):
        frames, context = inputs(horizon=1, count=3)
        result = award_scenarios(candidate(), frames, LIMITS, context)
        self.assertEqual(result['status'], 'unavailable')
        qualification = joint_qualification(result, progress(), LIMITS['budget'])
        self.assertEqual(_family_status(result, qualification, result['scenario_plan'], 15), 'scenario_coverage_unknown')
        self.assertEqual(len([item for item in terminal_paths(result) if item['status'] == 'ready']), 12)

    def test_terminal_path_cannot_hide_invalid_or_unsettled_current_book(self):
        for mutate in (lambda c: c['initial_state'].update(unresolved_orders=1),
                       lambda c: c['initial_state'].update(pending_orders=1),
                       lambda c: c['initial_state']['positions'][0].update(last_close='99'),
                       lambda c: c['latest_frame'].update(missing_symbols=['900001'])):
            frames, context = inputs(horizon=2, positions=[lot('900001')])
            mutate(context)
            result = award_scenarios(candidate(), frames, LIMITS, context)
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['scenarios'], [])

    def test_cumulative_protection_blocks_terminal_family_and_selector(self):
        frames, context = inputs(horizon=1)
        context['initial_state']['account_protection']['peak'] = '1.2'
        result = award_scenarios(candidate(), frames, LIMITS, context)
        qualification = joint_qualification(result, progress(), LIMITS['budget'])
        self.assertEqual(_family_status(result, qualification, result['scenario_plan'], 15), 'scenario_risk_limit')
        frames, context = native_inputs(1)
        changed = deepcopy(context)
        changed['initial_state']['account_protection']['active'] = True
        changed['hash'] = _hash({key: value for key, value in changed.items() if key != 'hash'})
        selected, audit = select_award_record(candidate(), [], frames, LIMITS, changed)
        self.assertIsNone(selected)
        self.assertEqual(audit['reason'], 'account_protection_active')

    def test_native_selector_and_receipt_preserve_unscaled_terminal_improvement(self):
        for horizon in (1, 2):
            frames, context = native_inputs(horizon)
            parent, proposed = candidate(reserve=0), candidate(reserve=0, top_n=1)
            parent['strategies'][0]['conditions'] = [{'feature': 'return_5d_pct', 'op': 'gt', 'value': 2}]
            records = [({'policy': proposed}, policy_id(proposed), outcome())]
            selected, audit = select_award_record(parent, records, frames, LIMITS, context)
            self.assertEqual(selected, 0, (audit['reason'], [item.get('promotion_reason') for item in audit['outcomes']]))
            self.assertEqual(audit['status'], 'apply')
            self.assertIsNotNone(audit['incumbent_full_worst_final_equity'])
            detail = audit['outcomes'][0]
            self.assertIsNotNone(detail['full_worst_final_equity'])
            full = [item['final_equity'] for item in detail['scenario_evidence']['scenarios']
                    if item['schedule'] == 'full' and item['environment'] in ('normal', 'cost_stress')]
            self.assertAlmostEqual(audit['objective_improvement'], (math.fsum(full)/6-10000)/2)
            self.assertEqual(verify_award_record_evidence(parent, records, frames, LIMITS, context, audit), (selected, audit))

    def test_terminal_alias_tampering_and_old_plan_are_rejected(self):
        frames, context = inputs(horizon=1)
        result = award_scenarios(candidate(), frames, LIMITS, context)
        qualification = joint_qualification(result, progress(), LIMITS['budget'])
        changed = deepcopy(result)
        terminal_paths(changed)[0]['final_equity'] += 1
        self.assertEqual(_family_status(changed, qualification, result['scenario_plan'], 15), 'scenario_coverage_unknown')
        old = deepcopy(result['scenario_plan'])
        old['version'] = 'quality-only-common-windows-v1'
        old['hash'] = _hash({key: value for key, value in old.items() if key != 'hash'})
        self.assertEqual(award_scenarios(candidate(), frames, LIMITS, context, plan=old)['reason'], 'scenario_plan_version_mismatch')

    def test_intraday_zero_future_keeps_each_prefix_variant_once(self):
        for horizon in (1, 2):
            frames, context = intraday_fixture(horizon=horizon)
            before = deepcopy((frames, context))
            policy = candidate(reserve=0, top_n=1)
            result = intraday_scenarios.award_scenarios(policy, frames, LIMITS, context)
            self.assertEqual((result['status'], result['ready_scenarios']), ('ready', 24), result)
            qualification = joint_qualification(result, context['competition_context'], LIMITS['budget'])
            self.assertEqual(_family_status(result, qualification, result['scenario_plan'], 15), 'ready')
            hashes = set()
            for item in terminal_paths(result):
                before_outcome = item['intraday_prefix']
                for key in ('final_equity', 'max_drawdown_pct', 'gross_amount', 'trading_days', 'symbols',
                            'fees_paid', 'taxes_paid', 'buy_fill_events', 'sell_fill_events', 'final_state_hash', 'final_protection'):
                    self.assertEqual(item[key], before_outcome[key], (horizon, item['id'], key))
                self.assertEqual(item['first_buy_session'], 0)
                self.assertIsNone(item['api_orders'])
                self.assertEqual(item['post_prefix_book_hash'], item['final_state_hash'])
                self.assertTrue(all(value == 0 for value in item['future_activity'].values()))
                self.assertIsNone(item['assumed_start'])
                self.assertEqual(item['assumed_end'], context['activation_day'])
                hashes.add(item['post_prefix_book_hash'])
            self.assertGreater(len(hashes), 1)
            self.assertEqual(len({item['path_hash'] for item in terminal_paths(result)}), 1)
            self.assertEqual((frames, context), before)

    def test_intraday_prefix_keeps_initial_protection_drawdown_after_recovery(self):
        frames, context = intraday_fixture(horizon=1)
        result = intraday_scenarios.award_scenarios(candidate(reserve=0), frames, LIMITS, context)
        for item in terminal_paths(result):
            self.assertGreaterEqual(item['cumulative_protection_drawdown_pct'], (1-1/1.01)*100-1e-9)
            self.assertEqual(item['max_drawdown_pct'], item['intraday_prefix']['max_drawdown_pct'])

    def test_intraday_zero_actual_horizon_is_still_rejected(self):
        frames, context = intraday_fixture(horizon=1)
        source = context['source_award_context']
        source['competition_context']['pace']['remaining_sessions'] = 1
        source['competition_context']['period']['end_date'] = context['activation_day']
        source['hash'] = _hash({key: value for key, value in source.items() if key != 'hash'})
        context['source_award_context_hash'] = source['hash']
        context['competition_context']['pace']['remaining_sessions'] = 0
        context['competition_context']['period']['end_date'] = context['activation_day']
        context['hash'] = _hash({key: value for key, value in context.items() if key != 'hash'})
        self.assertFalse(intraday_scenarios.validate_intraday_context(context))
        result = intraday_scenarios.award_scenarios(candidate(), frames, LIMITS, context)
        self.assertEqual(result['reason'], 'intraday_forward_horizon_mismatch')

    def test_native_candidate_order_does_not_change_terminal_selection(self):
        frames, context = native_inputs(2)
        parent = candidate(reserve=0)
        parent['strategies'][0]['conditions'] = [{'feature': 'return_5d_pct', 'op': 'gt', 'value': 2}]
        records = []
        for holding in (1, 2):
            policy = candidate(reserve=0, top_n=1)
            policy['strategies'][0]['holding_sessions'] = holding
            records.append(({'policy': policy}, policy_id(policy), outcome()))
        first, _ = select_award_record(parent, records, frames, LIMITS, context)
        reversed_records = list(reversed(records))
        second, _ = select_award_record(parent, reversed_records, frames, LIMITS, context)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(records[first][1], reversed_records[second][1])

    def test_intraday_terminal_receipt_cannot_add_prefix_activity_or_relax_protection(self):
        frames, context = intraday_fixture(horizon=1)
        result = intraday_scenarios.award_scenarios(candidate(reserve=0), frames, LIMITS, context)
        qualification = joint_qualification(result, context['competition_context'], LIMITS['budget'])
        for change in (lambda item: item.update(gross_amount=item['gross_amount']+1),
                       lambda item: item.update(fees_paid=item['fees_paid']+1),
                       lambda item: item.update(final_state_hash='different'),
                       lambda item: item.update(cumulative_protection_limit_pct=20),
                       lambda item: item.update(horizon=False)):
            tampered = deepcopy(result)
            # Change all aliases together: prefix/contract checks, not only alias
            # equality, must reject the altered evidence.
            for item in terminal_paths(tampered):
                change(item)
            self.assertEqual(_family_status(tampered, qualification, result['scenario_plan'], 15),
                             'scenario_coverage_unknown')
        tampered_plan = deepcopy(result['scenario_plan'])
        tampered_plan['remaining_sessions'] = True
        self.assertEqual(_family_status(result, qualification, tampered_plan, 15), 'scenario_coverage_unknown')

    def test_intraday_terminal_preserves_delayed_partial_exit_postbooks(self):
        frames, context = intraday_fixture(horizon=2, held=True)
        source = context['source_award_context']
        for book in (source['initial_state'], context['current_book'], context['initial_state']):
            book['positions'][0].update(exit_pending=True, exit_fill_remainder='.25', exit_delay_remaining=1)
        source['hash'] = _hash({key: value for key, value in source.items() if key != 'hash'})
        context['source_award_context_hash'] = source['hash']
        context['current_book_hash'] = _hash(context['current_book'])
        context['hash'] = _hash({key: value for key, value in context.items() if key != 'hash'})
        self.assertTrue(intraday_scenarios.validate_intraday_context(context))
        original = deepcopy((frames, context))
        policy = candidate(reserve=0)
        result = intraday_scenarios.award_scenarios(policy, frames, LIMITS, context)
        self.assertEqual(result['status'], 'ready', result.get('reason'))
        prefixes = {partial: intraday_scenarios._prefix(policy, LIMITS, context, partial_fills=partial)[0]
                    for partial in (False, True)}
        for item in terminal_paths(result):
            partial = item['environment'].startswith('partial_')
            key = 'stress_final_state' if 'cost_stress' in item['environment'] else 'final_state'
            self.assertEqual(item['final_state_hash'], _hash(prefixes[partial][key]))
            self.assertEqual(item['final_protection'], prefixes[partial][key]['account_protection'])
        self.assertEqual((frames, context), original)


if __name__ == '__main__':
    unittest.main()
