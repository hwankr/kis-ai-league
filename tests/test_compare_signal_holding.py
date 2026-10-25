"""Offline analysis invariants; synthetic fixtures are not performance evidence."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from backend.learning_policy import portfolio_metrics, validate_policy
from scripts.compare_signal_holding import (branch_states, candidates, canonical, cohort_attribution,
                                           controls, coverage, curve_diagnostics, digest, metric_with_signals,
                                           save_new, scenarios, summarize_rows, terminal_diagnostics)
from tests.test_learning_policy import LIMITS, frames, policy


def incumbent():
    value = policy(cash_reserve=.4, allocation_mode='equal_eligible',
                   held_before_top_n=True, no_same_day_reentry=True)
    value['strategies'][0].update(weight=.6, top_n=8, holding_sessions=2,
                                  stop_loss_pct=5, take_profit_pct=11)
    return validate_policy(value)


class SignalHoldingComparisonTests(unittest.TestCase):
    def test_request_coverage_hash_survives_json_roundtrip(self):
        data = frames(3)
        data[1]['rows'] *= 10
        data[2]['rows'] *= 8
        value = coverage(data)
        self.assertEqual(digest(value), digest(json.loads(canonical(value))))

    def test_twenty_hypotheses_preserve_money_and_risk_controls(self):
        parent, data = incumbent(), frames(3)
        original = deepcopy(parent)
        hypotheses, _ = candidates(parent, data)
        self.assertEqual(len(hypotheses), 20)
        self.assertEqual(hypotheses['current_hold2'], parent)
        self.assertEqual(parent, original)
        for family in ('current', 'trend', 'relative', 'recovery', 'liquid'):
            expected = hypotheses[family + '_hold2']['strategies'][0]
            for hold in (1, 2, 4):
                candidate = hypotheses[f'{family}_hold{hold}']
                self.assertEqual(controls(candidate), controls(parent))
                strategy = candidate['strategies'][0]
                for key in ('conditions', 'rank_by', 'descending'):
                    self.assertEqual(strategy[key], expected[key])
                self.assertEqual(strategy['holding_sessions'], hold)
                self.assertNotIn('exit_mode', strategy)
            extension = hypotheses[family + '_extend2to4']['strategies'][0]
            self.assertEqual(extension['holding_sessions'], 2)
            self.assertEqual(extension['max_holding_sessions'], 4)
            self.assertEqual(extension['exit_mode'], 'signal_extension')

    def test_signal_audit_does_not_change_economic_replay(self):
        parent, data = incumbent(), frames(5)
        plain = portfolio_metrics(parent, data, LIMITS)
        audited = metric_with_signals(parent, data, LIMITS)
        audit = audited.pop('signal_audit')
        self.assertEqual(audited, plain)
        self.assertEqual(len(audit['normal']), len(data))
        self.assertEqual(len(audit['cost_stress']), len(data))
        self.assertEqual(audit['normal'][0]['eligible_signals'], 1)

    def test_nav_does_not_deduct_a_hypothetical_terminal_sale(self):
        data = frames(2)
        metric = portfolio_metrics(incumbent(), data, LIMITS,
                                   execution_model={'mode': 'daily_open', 'slippage_bps': 0})
        self.assertTrue(metric['valid'])
        result = curve_diagnostics(metric, False)
        final = metric['final_state']
        expected = Decimal(final['cash']) + sum(Decimal(p['last_close']) * p['quantity'] for p in final['positions'])
        self.assertAlmostEqual(result['equity_curve'][-1]['close_marked_equity'], float(expected))
        self.assertGreater(result['net_return_pct'], metric['net_return_pct'])
        self.assertEqual(metric['diagnostics']['normal']['taxes_paid'], 0)

    def test_current_lot_exit_is_preserved_across_new_holding_rules(self):
        data = frames(5)
        for frame in data:
            frame['rows'][0]['learning_eligible'] = False
        book = {'as_of': data[0]['as_of'], 'cash': '9000', 'reserved_cash': '0',
                'positions': [{'symbol': '005930', 'strategy_id': 'momentum', 'quantity': 10,
                    'entry_price': '100', 'last_close': '100', 'cost': '1000', 'held_sessions': 1,
                    'exit_policy': {'holding_sessions': 3, 'stop_loss_pct': 0, 'take_profit_pct': 0}}],
                'account_protection': {'active': False, 'limit_pct': 15, 'peak': '1', 'units': '10000'}}
        original = deepcopy(book)
        hypotheses, _ = candidates(incumbent(), data)
        outputs = [metric_with_signals(hypotheses[f'current_hold{hold}'], data, LIMITS,
                                      initial_state=book) for hold in (1, 2, 4)]
        self.assertTrue(all(value['valid'] for value in outputs))
        self.assertEqual(book, original)
        self.assertEqual(outputs[0]['equity_curve'], outputs[1]['equity_curve'])
        self.assertEqual(outputs[1]['equity_curve'], outputs[2]['equity_curve'])
        self.assertEqual(outputs[0]['trading_activity']['sell_fill_events'], 1)

    def test_evidence_writer_never_overwrites_an_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'result.json'
            save_new(target, {'first': True})
            with self.assertRaises(FileExistsError):
                save_new(target, {'second': True})

    def test_incomplete_paths_never_produce_a_reweighted_comparable_mean(self):
        rows = [{'id': 'known', 'status': 'ready', 'environment': 'normal', 'schedule': 'full',
                 'net_return_pct': 5, 'final_equity': 105, 'max_drawdown_pct': 1},
                {'id': 'bad', 'status': 'unavailable', 'reason': 'holding_path_price_missing'}]
        result = summarize_rows(rows, ['known', 'bad', 'absent'])
        self.assertFalse(result['comparable'])
        self.assertIsNone(result['mean'])
        self.assertNotIn('normal', result)
        self.assertEqual(result['ready_paths'], 1)
        self.assertEqual(result['missing_or_unavailable'], [
            {'id': 'bad', 'reason': 'holding_path_price_missing'}, {'id': 'absent', 'reason': 'path_absent'}])
        self.assertEqual(result['known_path_diagnostics'][0]['net_return_pct'], 5)

    def test_complete_summary_keeps_horizons_and_cost_environments_separate(self):
        rows = []
        for schedule, horizon in (('full', 20), ('shortened', 18)):
            for environment in ('normal', 'cost_stress', 'partial_delayed', 'partial_delayed_cost_stress'):
                for index, window in enumerate(('first', 'middle', 'recent')):
                    item = dict.fromkeys(('net_return_pct', 'return_from_current_book_pct',
                        'return_from_original_capital_pct', 'current_book_start_nav', 'final_equity',
                        'max_drawdown_pct', 'average_exposure_pct', 'turnover_pct_of_start_nav',
                        'fees_paid', 'taxes_paid', 'gross_buy_amount', 'gross_sell_amount',
                        'next_entry_signals_total', 'buy_attempts', 'limit_blocked'), index + 1)
                    item.update(id=f'{window}_{schedule}_{environment}', status='ready', schedule=schedule,
                                horizon=horizon, environment=environment, protection_triggered=False)
                    rows.append(item)
        result = summarize_rows(rows, [row['id'] for row in rows])
        self.assertTrue(result['comparable'])
        self.assertEqual(result['ready_paths'], 24)
        self.assertEqual(result['normal']['full']['mean']['net_return_pct'], 2)
        self.assertEqual(result['normal']['full']['paths'], 3)
        self.assertEqual(result['cost_stress']['shortened']['horizon'], 18)
        for row in rows:
            if row['schedule'] == 'shortened':
                row['horizon'], row['average_exposure_pct'] = 0, None
        terminal = summarize_rows(rows, [row['id'] for row in rows])
        self.assertIsNone(terminal['normal']['shortened']['mean']['average_exposure_pct'])

    def test_exit_cohort_preserves_other_lots_and_environment_specific_books(self):
        position = {'symbol': '005930', 'strategy_id': 'momentum', 'quantity': 10,
                    'entry_price': '100', 'cost': '1000', 'held_sessions': 0,
                    'exit_policy': {'holding_sessions': 2, 'stop_loss_pct': 5, 'take_profit_pct': 11}}
        states = {'normal': {'cash': '9000', 'positions': [deepcopy(position)]},
                  'stress': {'cash': '8000', 'positions': [deepcopy(position)]}}
        original = deepcopy(states)
        changed = branch_states(states, {'normal': ['005930'], 'stress': []}, 'extend2to4', incumbent())
        self.assertEqual(states, original)
        self.assertEqual(changed['stress'], original['stress'])
        self.assertEqual(changed['normal']['cash'], original['normal']['cash'])
        self.assertEqual(changed['normal']['positions'][0]['exit_policy']['max_holding_sessions'], 4)

    def test_cohort_prefixes_match_full_replay_and_terminal_entries_remain_unidentified(self):
        data = frames(5)
        for frame in data:
            frame['rows'][0]['learning_eligible'] = True
        book = {'as_of': data[-1]['as_of'], 'cash': '10000', 'reserved_cash': '0', 'positions': [],
                'account_protection': {'active': False, 'limit_pct': 15, 'peak': '1', 'units': '10000'}}
        hypotheses, _ = candidates(incumbent(), data)
        request = {'frames': data, 'limits': LIMITS, 'candidates': hypotheses,
                   'context': {'latest_frame': data[-1], 'initial_state': book}}
        plan = {'schedules': [{'schedule': 'full', 'horizon': 4,
                              'selected': [{'window': 'first', 'source_start_index': 0}]}]}
        results = {}
        for family in ('current', 'trend', 'relative', 'recovery', 'liquid'):
            parent = hypotheses[family + '_hold2']
            path = scenarios._path(parent, data, data[-1], book)
            metric = portfolio_metrics(parent, path, LIMITS, initial_state=book,
                                       execution_model={'mode': 'daily_open', **scenarios._costs(None)})
            self.assertTrue(metric['valid'])
            rows = [{'id': 'first_full_' + ('cost_stress' if stress else 'normal'), 'status': 'ready',
                     **curve_diagnostics(metric, stress, LIMITS['budget'])} for stress in (False, True)]
            results[family + '_hold2'] = {'evidence': {'scenarios': rows}}
        result = cohort_attribution(request, plan, results)
        self.assertTrue(result['overlapping_deltas_must_not_be_summed'])
        self.assertTrue(all(p['baseline_prefixes_match_account_final_state_and_curve'] for p in result['paths']))
        cases = result['paths'][0]['cohort_cases']
        self.assertGreater(len(cases), 0)
        self.assertTrue(all(v[env]['gross_buy_amount'] == 0 for c in cases for v in c['variants'].values()
                            for env in ('normal', 'stress')))
        self.assertTrue(any(c['remaining_intervals'] == 0 for c in cases))
        terminal = next(c for c in cases if c['remaining_intervals'] == 0)
        self.assertEqual({v['normal']['final_equity'] for v in terminal['variants'].values()},
                         {terminal['variants']['hold2']['normal']['final_equity']})

    def test_zero_schedule_has_no_average_exposure_or_new_entry_cohort(self):
        book = {'as_of': '2026-07-01', 'cash': '9000', 'positions': [],
                'account_protection': {'active': False, 'limit_pct': 15, 'peak': '1', 'units': '10000'}}
        value = terminal_diagnostics({'final_equity': 9000, 'execution_diagnostics': {}}, book, LIMITS)
        self.assertEqual(value['return_from_current_book_pct'], 0)
        self.assertAlmostEqual(value['return_from_original_capital_pct'], -10)
        self.assertIsNone(value['average_exposure_pct'])
        self.assertFalse(value['holding_effect_identified'])
        hypotheses, _ = candidates(incumbent(), frames(3))
        request = {'limits': LIMITS, 'context': {'initial_state': book}, 'candidates': hypotheses}
        plan = {'schedules': [{'schedule': 'shortened', 'horizon': 0,
                              'selected': [{'window': 'first', 'source_start_index': None}]}]}
        result = cohort_attribution(request, plan, {})
        self.assertEqual(len(result['paths']), 5)
        self.assertTrue(all(p['cohort_cases'] == [] and p['reason'] == 'no_following_intervals' for p in result['paths']))


if __name__ == '__main__':
    unittest.main()
