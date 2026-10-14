"""Historical analogue scenarios use in-memory fixtures only."""
from copy import deepcopy
from decimal import Decimal
import json
import unittest

from backend.learning_policy import BASELINE_POLICY, LEGACY_IDS, FEE, TAX
from backend.policy_award_scenarios import award_scenarios, scenario_plan, _hash, _path
from tests.test_learning_policy import days, row
from tests.test_policy_transition import candidate, fixture, lot, LIMITS


def inputs(*, horizon=4, count=12, positions=(), eligible=True):
    latest, context = fixture(positions, rows=[row('900001', learning_eligible=eligible)])
    context.update(latest_frame=deepcopy(latest), limits=deepcopy(LIMITS),
                   competition_context={'pace': {'remaining_sessions': horizon},
                                        'period': {'end_date': '2026-11-20'},
                                        'metrics': {'symbols': {'value': 8}, 'api_orders': {'value': 8}}})
    calendar = days(count)
    history = [{'as_of': day, 'observed_at': day+'T16:05:00+09:00',
                'session_days': calendar[:index+1], 'rows': [row('900001', price=50, learning_eligible=eligible)],
                'baseline_signals': []} for index, day in enumerate(calendar)]
    return history, context


def run(policy=None, **values):
    frames, context = inputs(**values)
    return award_scenarios(policy or candidate(reserve=0, top_n=1), frames, LIMITS, context)


def path(result, environment='normal', window='first', schedule='full'):
    return next(value for value in result['scenarios'] if value['id'] == f'{window}_{schedule}_{environment}')


class AwardScenarioTests(unittest.TestCase):
    def test_fixed_family_uses_one_book_and_incremental_activity(self):
        result = run()
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['scenario_count'], 24)
        self.assertEqual({value['horizon'] for value in result['scenarios']}, {4, 2})
        self.assertEqual(len({value['start_book_hash'] for value in result['scenarios']}), 1)
        normal = path(result)
        self.assertEqual(normal['trading_days'], 1)
        self.assertEqual(normal['symbols'], 1)  # Not eight actual + one scenario.
        self.assertIsNone(normal['api_orders'])
        self.assertEqual(normal['first_buy_session'], 1)
        self.assertEqual(normal['gross_sell_amount'], 0)
        self.assertEqual(normal['gross_amount'], normal['gross_buy_amount'])

    def test_current_anchor_signal_not_historical_anchor_or_repeated_latest_signal(self):
        frames, context = inputs(horizon=4, eligible=False)
        context['latest_frame']['rows'][0]['learning_eligible'] = True
        value = candidate(reserve=0, top_n=1)
        value['strategies'][0]['holding_sessions'] = 1
        result = award_scenarios(value, frames, LIMITS, context)
        current = path(result)
        self.assertEqual(current['buy_fill_events'], 1)
        self.assertEqual(current['sell_fill_events'], 1)
        self.assertEqual(current['trading_days'], 2)
        self.assertEqual(current['first_buy_session'], 1)

    def test_later_historical_signals_keep_their_sequence(self):
        frames, context = inputs(horizon=4, eligible=False)
        frames[1]['rows'][0]['learning_eligible'] = True
        value = candidate(reserve=0, top_n=1)
        result = award_scenarios(value, frames, LIMITS, context)
        self.assertEqual(path(result)['first_buy_session'], 2)
        self.assertIsNone(path(result, window='recent')['first_buy_session'])

    def test_historical_selection_dates_preserve_scope_values_without_becoming_future_evidence(self):
        frames, context = inputs()
        for frame in [context['latest_frame'], *frames]:
            frame['rows'][0]['selection'] = {'as_of': frame['as_of'], 'complete_61': True,
                'positive_volume_20': True, 'turnover_20d_mean': 20_000_000_000}
        value = candidate(reserve=0, top_n=1)
        value['candidate_scope'] = 'liquid_all'
        shifted = _path(value, frames[:5], context['latest_frame'], context['initial_state'])
        self.assertEqual(shifted[1]['rows'][0]['selection'], frames[1]['rows'][0]['selection'])
        self.assertNotEqual(shifted[1]['as_of'], shifted[1]['rows'][0]['selection']['as_of'])
        result = award_scenarios(value, frames, LIMITS, context)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(path(result)['first_buy_session'], 1)

    def test_scaling_is_per_symbol_ohlc_and_features_are_not_recalculated(self):
        frames, context = inputs(horizon=2)
        latest = context['latest_frame']
        latest['rows'].append(row('900002', price=300))
        for item in frames:
            item['rows'].append(row('900002', price=10))
        frames[1]['rows'][0].update(open=54, low=53, high=56, close=55)
        frames[1]['rows'][1].update(open=12, low=11, high=14, close=13)
        result = _path(candidate(), frames[:3], latest, context['initial_state'])
        self.assertEqual(result[0]['rows'], latest['rows'])
        self.assertEqual([item['close'] for item in result[1]['rows']], [110, 390])
        self.assertEqual([item['open'] for item in result[1]['rows']], [108, 360])
        self.assertEqual(result[1]['rows'][0]['features'], frames[1]['rows'][0]['features'])
        self.assertEqual(result[1]['as_of'], '2026-10-15')
        self.assertEqual(frames[1]['as_of'], '2026-07-02')

    def test_current_lot_age_and_old_exit_policy_survive_historical_transplant(self):
        position = lot('900001', age=4, quantity=4)
        result = run(positions=[position], eligible=False)
        current = path(result)
        self.assertEqual(current['gross_buy_amount'], 0)
        self.assertAlmostEqual(current['gross_sell_amount'], 4 * 100 * .999)
        self.assertEqual(current['final_holding_count'], 0)
        delayed = path(result, 'partial_delayed')
        self.assertGreater(delayed['final_holding_count'], 0)
        self.assertLess(delayed['gross_sell_amount'], current['gross_sell_amount'])

    def test_active_protection_is_inherited_and_no_new_entries_are_invented(self):
        frames, context = inputs(positions=[lot('900001')])
        context['initial_state']['account_protection']['active'] = True
        result = award_scenarios(candidate(reserve=0), frames, LIMITS, context)
        self.assertEqual(result['status'], 'ready')
        self.assertTrue(all(item['final_protection']['active'] for item in result['scenarios']))
        self.assertTrue(all(item['gross_buy_amount'] == 0 for item in result['scenarios']))

    def test_final_equity_marks_unsold_shares_without_terminal_trade_or_tax(self):
        position = lot('900001', age=0, quantity=4)
        frames, context = inputs(horizon=2, positions=[position], eligible=False)
        for frame in frames[1:]:
            frame['rows'][0].update(open=55, low=54, high=56, close=55)
        result = award_scenarios(candidate(reserve=0), frames, LIMITS, context)
        current = path(result)
        self.assertEqual(current['gross_amount'], 0)
        self.assertEqual(current['trading_days'], 0)
        self.assertEqual(current['final_holding_count'], 1)
        self.assertAlmostEqual(current['final_equity'], float(Decimal(context['initial_state']['cash']) + 440))
        self.assertEqual(current['taxes_paid'], 0)

    def test_zero_signals_is_ready_zero_but_missing_data_is_unknown(self):
        zero = run(eligible=False)
        self.assertEqual(zero['status'], 'ready')
        self.assertTrue(all(item['gross_amount'] == 0 for item in zero['scenarios']))
        self.assertTrue(all(item['first_buy_session'] is None for item in zero['scenarios']))
        missing = run(horizon=20)
        self.assertEqual(missing['status'], 'unavailable')
        self.assertTrue(all(item['gross_amount'] is None for item in missing['scenarios']))
        self.assertTrue(all(item['reason'] == 'insufficient_historical_horizon' for item in missing['scenarios']))

    def test_missing_holding_path_price_never_drops_or_liquidates_the_lot(self):
        frames, context = inputs(positions=[lot('900001')])
        frames[1]['rows'] = [row('900002')]
        result = award_scenarios(candidate(), frames, LIMITS, context)
        with self.assertRaisesRegex(ValueError, 'holding_path_price_missing'):
            _path(candidate(), frames[:5], context['latest_frame'], context['initial_state'])
        self.assertEqual(path(result)['status'], 'ready')
        self.assertEqual(path(result)['source_start_index'], 2)
        self.assertEqual(result['scenario_plan']['schedules'][0]['excluded_reason_counts'],
                         {'holding_path_price_missing': 2})
        self.assertEqual(result['start_book_hash'], _hash(context['initial_state']))

    def test_new_historical_symbol_without_current_normalization_price_is_unavailable(self):
        frames, context = inputs()
        frames[1]['rows'].append(row('900002'))
        result = award_scenarios(candidate(), frames, LIMITS, context)
        self.assertEqual(path(result)['status'], 'ready')
        self.assertEqual(path(result)['source_start_index'], 2)
        self.assertEqual(result['scenario_plan']['schedules'][0]['excluded_reason_counts'],
                         {'normalization_price_missing': 2})

    def test_legacy_missing_observed_signals_is_not_a_cash_only_parent(self):
        frames, context = inputs()
        context['latest_frame']['baseline_signals'] = [
            {'strategy_id': identity, 'symbol': '900001', 'status': 'ready', 'action': 'hold'}
            for identity in LEGACY_IDS]
        result = award_scenarios(BASELINE_POLICY, frames, LIMITS, context)
        self.assertEqual(result['status'], 'unavailable')
        self.assertTrue(all(item['reason'] == 'historical_legacy_signals_unavailable' for item in result['scenarios']))
        for frame in frames:
            frame['baseline_signals'] = deepcopy(context['latest_frame']['baseline_signals'])
        ready = award_scenarios(BASELINE_POLICY, frames, LIMITS, context)
        self.assertEqual(ready['status'], 'ready')
        self.assertTrue(all(item['gross_amount'] == 0 for item in ready['scenarios']))

    def test_incomplete_current_book_or_unknown_orders_cannot_start_scenarios(self):
        for key, value in [('pending_orders', 1), ('unresolved_orders', 1), ('account_protection', None)]:
            frames, context = inputs()
            context['initial_state'][key] = value
            with self.subTest(key=key):
                result = award_scenarios(candidate(), frames, LIMITS, context)
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(result['scenarios'], [])

    def test_missing_session_and_future_observation_are_not_shifted_away(self):
        for change in ('gap', 'future', 'calendar'):
            frames, context = inputs()
            if change == 'gap':
                frames.pop(1)
            elif change == 'future':
                frames[1]['observed_at'] = '2026-10-15T16:00:00+09:00'
            else:
                for frame in frames:
                    frame.pop('session_days')
                frames.pop(1)
            with self.subTest(change=change):
                result = award_scenarios(candidate(), frames, LIMITS, context)
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(result['scenarios'], [])

    def test_schedule_cannot_trade_past_contest_end(self):
        frames, context = inputs(horizon=4)
        context['competition_context']['period']['end_date'] = '2026-10-19'
        result = award_scenarios(candidate(), frames, LIMITS, context)
        self.assertEqual(result['reason'], 'remaining_sessions_exceed_competition_period')
        context['competition_context']['period']['end_date'] = '2026-10-20'
        result = award_scenarios(candidate(), frames, LIMITS, context)
        self.assertEqual(result['status'], 'ready')
        self.assertTrue(all(item['assumed_end'] <= '2026-10-20' for item in result['scenarios']))

    def test_current_signal_without_historical_anchor_price_is_unavailable(self):
        frames, context = inputs()
        for frame in frames:
            frame['rows'] = [row('900002', price=50)]
        context['latest_frame']['rows'].append(row('900002', learning_eligible=False))
        result = award_scenarios(candidate(reserve=0), frames, LIMITS, context)
        self.assertEqual(path(result)['status'], 'unavailable')
        self.assertEqual(path(result)['reason'], 'no_complete_common_window')
        self.assertEqual(result['scenario_plan']['schedules'][0]['excluded_reason_counts'],
                         {'current_universe_path_price_missing': 8})
        self.assertIsNone(path(result)['gross_amount'])

    def test_plan_selects_fixed_quality_windows_without_price_performance(self):
        frames, context = inputs(horizon=4, count=15)
        for frame in frames[:4]:
            frame['missing_symbols'] = ['900002']
        plan = scenario_plan(frames, context)
        full = plan['schedules'][0]
        self.assertEqual(full['available_window_count'], 7)
        self.assertEqual([item['source_start_index'] for item in full['selected']], [4, 7, 10])
        self.assertEqual(full['excluded_reason_counts'], {'incomplete_historical_frame': 4})
        self.assertFalse(plan['performance_inputs_used'])
        self.assertFalse(plan['independent_paths'])
        self.assertTrue(plan['overlap'])
        changed = deepcopy(frames)
        for index, frame in enumerate(changed):
            price = 10 + index * 7
            frame['rows'][0].update(open=price, low=price, high=price, close=price)
            frame['rows'][0]['features']['volume_ratio'] = index
        alternate = scenario_plan(changed, context)
        self.assertEqual([item['source_start_index'] for item in alternate['schedules'][0]['selected']], [4, 7, 10])
        self.assertNotEqual(plan['history_hash'], alternate['history_hash'])
        self.assertNotEqual(plan['hash'], alternate['hash'])

    def test_common_plan_is_shared_across_policies_and_keeps_missing_legacy_evidence(self):
        frames, context = inputs()
        context['latest_frame']['baseline_signals'] = [
            {'strategy_id': identity, 'symbol': '900001', 'status': 'ready', 'action': 'hold'}
            for identity in LEGACY_IDS]
        plan = scenario_plan(frames, context)
        first = award_scenarios(candidate(top_n=1), frames, LIMITS, context, plan=plan)
        second = award_scenarios(candidate(top_n=3), frames, LIMITS, context, plan=plan)
        legacy = award_scenarios(BASELINE_POLICY, frames, LIMITS, context, plan=plan)
        self.assertEqual(first['scenario_plan_hash'], second['scenario_plan_hash'])
        self.assertEqual(first['scenario_plan_hash'], legacy['scenario_plan_hash'])
        self.assertEqual([item['path_hash'] for item in first['scenarios']],
                         [item['path_hash'] for item in second['scenarios']])
        self.assertTrue(all(item['reason'] == 'historical_legacy_signals_unavailable'
                            for item in legacy['scenarios']))

    def test_sparse_complete_history_is_not_counted_as_three_distinct_paths(self):
        frames, context = inputs(horizon=4, count=6)
        result = award_scenarios(candidate(), frames, LIMITS, context)
        schedule = result['scenario_plan']['schedules'][0]
        self.assertEqual(schedule['available_window_count'], 2)
        self.assertEqual([item['status'] for item in schedule['selected']], ['ready', 'unavailable', 'ready'])
        self.assertEqual(path(result, window='middle')['reason'], 'insufficient_distinct_quality_windows')
        self.assertIsNone(path(result, window='middle')['final_equity'])
        self.assertEqual(result['ready_scenarios'], 20)
        self.assertEqual(result['status'], 'unavailable')

    def test_plan_rejects_version_hash_and_source_mismatch(self):
        frames, context = inputs()
        original = scenario_plan(frames, context)
        for kind, expected in [('version', 'scenario_plan_version_mismatch'),
                               ('hash', 'scenario_plan_hash_mismatch'),
                               ('source', 'scenario_plan_source_mismatch')]:
            plan = deepcopy(original)
            if kind == 'version':
                plan['version'] = 'unknown-version'
            elif kind == 'hash':
                plan['schedules'][0]['selected'][0]['source_start_index'] = 1
            else:
                plan['start_book_hash'] = 'different-book'
                plan['hash'] = _hash({key: value for key, value in plan.items() if key != 'hash'})
            result = award_scenarios(candidate(), frames, LIMITS, context, plan=plan)
            self.assertEqual(result['reason'], expected)
            self.assertEqual(result['scenarios'], [])
        self.assertEqual(original, scenario_plan(frames, context))

    def test_incomplete_symbol_history_is_recorded_and_never_backfilled(self):
        frames, context = inputs(horizon=4, count=15, positions=[lot('900001')])
        for frame in frames[:4]:
            frame['missing_symbols'] = ['900001', '900002']
        before = deepcopy((frames, context))
        plan = scenario_plan(frames, context)
        self.assertEqual(plan['frame_coverage'][0]['missing_symbol_count'], 2)
        self.assertEqual(plan['frame_coverage'][0]['missing_current_holdings'], ['900001'])
        self.assertEqual(plan['frame_coverage'][0]['reason'], 'incomplete_historical_frame')
        self.assertEqual((frames, context), before)

    def test_legacy_ineligible_quota_difference_is_unavailable_not_overstated(self):
        frames, context = inputs()
        for frame in [context['latest_frame'], *frames]:
            frame['rows'].append(row('900002', learning_eligible=False))
            frame['baseline_signals'] = [
                {'strategy_id': identity, 'symbol': symbol, 'status': 'ready',
                 'action': 'buy' if identity == LEGACY_IDS[0] else 'hold'}
                for identity in LEGACY_IDS for symbol in ['900001', '900002']]
        result = award_scenarios(BASELINE_POLICY, frames, LIMITS, context)
        self.assertTrue(all(item['reason'] == 'legacy_eligibility_quota_unsupported'
                            for item in result['scenarios']))
        self.assertTrue(all(item['gross_amount'] is None for item in result['scenarios']))

    def test_same_path_cost_stress_and_partial_delay_are_separate_outcomes(self):
        result = run()
        normal, stressed, partial = (path(result, name) for name in ('normal', 'cost_stress', 'partial_delayed'))
        self.assertEqual(normal['path_hash'], stressed['path_hash'])
        self.assertLess(stressed['final_equity'], normal['final_equity'])
        self.assertLess(partial['gross_buy_amount'], normal['gross_buy_amount'])
        self.assertGreaterEqual(stressed['max_drawdown_pct'], normal['max_drawdown_pct'])
        normal_diagnostics, partial_diagnostics = normal['execution_diagnostics'], partial['execution_diagnostics']
        self.assertEqual(normal_diagnostics['partial_orders'], 0)
        self.assertGreater(partial_diagnostics['partial_orders'], 0)
        self.assertEqual(normal_diagnostics['fees_paid'], normal['fees_paid'])
        self.assertGreater(stressed['execution_diagnostics']['fees_paid'], normal_diagnostics['fees_paid'])
        self.assertEqual(normal_diagnostics['final_holding_count'], normal['final_holding_count'])

    def test_cost_profile_is_explicit_bounded_and_changes_real_cash_cost(self):
        frames, context = inputs()
        value = candidate(reserve=0)
        normal = award_scenarios(value, frames, LIMITS, context)
        changed = award_scenarios(value, frames, LIMITS, context,
                                  cost_profile={'fee_rate': '.001', 'stress_fee_rate': '.002'})
        self.assertLess(path(changed)['final_equity'], path(normal)['final_equity'])
        invalid = award_scenarios(value, frames, LIMITS, context, cost_profile={'fill_ratio': 1})
        self.assertEqual(invalid['status'], 'unavailable')

    def test_input_immutability_and_determinism(self):
        frames, context = inputs()
        value = candidate(reserve=0)
        before = deepcopy((value, frames, LIMITS, context))
        first = award_scenarios(value, frames, LIMITS, context)
        self.assertEqual(first, award_scenarios(value, frames, LIMITS, context))
        self.assertEqual((value, frames, LIMITS, context), before)
        json.dumps(first, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
