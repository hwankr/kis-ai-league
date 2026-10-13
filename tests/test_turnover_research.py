"""Trading activity is evidence, never a turnover reward or order permission."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from backend.learning_policy import BASELINE_POLICY, portfolio_metrics
from backend.learning_proposals import proposal_context
from backend.learning_research import MAX_CANDIDATES, propose_candidate, public_competition_context
from tests.test_learning_policy import policy, frames, LIMITS
from tests.test_learning_research import rule, metrics


def competition(**changes):
    return {"status": "ok", "coverage": "complete", "stale": False,
            "period": {"start_date": "2026-10-12", "end_date": "2026-11-20", "as_of": "2026-10-13"},
            "pace": {"status": "behind", "calendar_basis": "weekday_estimate", "remaining_sessions": 28,
                     "target_gross_amount": "500000000", "gross_amount": "4767100",
                     "remaining_gross_amount": "495232900", "required_daily_gross_amount": "17686889.29"},
            **changes}


MODEL = {"slippage_bps": 0, "stress_slippage_bps": 0, "fee_rate": 0, "stress_fee_rate": 0, "tax_rate": 0}


class TradingActivityTests(unittest.TestCase):
    def test_counts_only_gross_fills_with_zero_fees_and_no_terminal_liquidation(self):
        short = portfolio_metrics(policy(), frames(3), LIMITS, execution_model=MODEL)
        self.assertTrue(short['valid'], short['error'])
        activity = short['trading_activity']
        self.assertEqual(activity['gross_buy_amount'], 10000)
        self.assertEqual(activity['gross_sell_amount'], 0)
        self.assertEqual(activity['gross_amount'], 10000)
        self.assertEqual(activity['trading_days'], 1)
        self.assertEqual(activity['symbols'], 1)
        self.assertEqual(activity['buy_fill_events'], 1)
        self.assertEqual(activity['sell_fill_events'], 0)
        self.assertEqual(activity['intervals'], 2)
        self.assertEqual(activity['mean_daily_gross_amount'], 5000)
        self.assertEqual(activity['average_exposure_pct'], 100)
        full = portfolio_metrics(policy(), frames(7), LIMITS, execution_model=MODEL)
        self.assertEqual(full['trading_activity']['gross_amount'], 20000)
        self.assertEqual(full['trading_activity']['trading_days'], 2)
        self.assertEqual(full['trading_activity'], full['stress_trading_activity'])

    def test_partial_fills_count_executed_quantity_and_separate_fill_events(self):
        result = portfolio_metrics(policy(), frames(8), LIMITS, execution_model={**MODEL, 'fill_ratio': .5})
        self.assertTrue(result['valid'], result['error'])
        activity = result['trading_activity']
        self.assertEqual(activity['gross_buy_amount'], 5000)
        self.assertEqual(activity['gross_sell_amount'], 3700)
        self.assertEqual(activity['sell_fill_events'], 2)
        self.assertEqual(activity['trading_days'], 3)
        self.assertEqual(activity['gross_amount'], sum(point['gross_buy_amount'] + point['gross_sell_amount']
                                                      for point in result['equity_curve']))
        self.assertGreater(result['final_state']['positions'][0]['quantity'], 0)

    def test_replay_continuation_does_not_recount_inherited_holdings_or_missing_fills(self):
        observations = frames(7)
        first = portfolio_metrics(policy(), observations[:3], LIMITS, execution_model=MODEL)
        second = portfolio_metrics(policy(), observations[2:], LIMITS, execution_model=MODEL,
                                   initial_state=first['final_state'])
        self.assertTrue(second['valid'], second['error'])
        self.assertEqual(second['trading_activity']['gross_buy_amount'], 0)
        self.assertEqual(second['trading_activity']['gross_sell_amount'], 10000)
        for frame in observations:
            frame['rows'][0]['volume'] = 0
        blocked = portfolio_metrics(policy(), observations, LIMITS, execution_model=MODEL)
        self.assertEqual(blocked['trading_activity']['gross_amount'], 0)
        self.assertEqual(blocked['trading_activity']['trading_days'], 0)
        invalid = portfolio_metrics(policy(), [], LIMITS)
        self.assertIsNone(invalid['trading_activity'])


class CompetitionResearchTests(unittest.TestCase):
    def test_public_context_drops_identity_and_handles_unknown_progress(self):
        source = competition(account={'secret': 'DO_NOT_SEND'}, error='DO_NOT_SEND')
        source['pace']['secret'] = 'DO_NOT_SEND'
        clean = public_competition_context(source)
        self.assertTrue(clean['reliable'])
        self.assertNotIn('DO_NOT_SEND', json.dumps(clean))
        stale = public_competition_context(competition(stale=True))
        self.assertFalse(stale['reliable'])
        self.assertEqual(stale['pace']['status'], 'unavailable')
        self.assertIsNone(stale['pace']['gross_amount'])
        unknown = public_competition_context({'status': 'unavailable'})
        self.assertFalse(unknown['reliable'])
        self.assertIsNone(unknown['pace']['gross_amount'])
        self.assertIsNone(public_competition_context(None))

    def test_behind_adds_bounded_allocation_candidates_without_changing_conditions_or_limits(self):
        parent = rule()
        original = deepcopy(parent)
        observations = frames(1)
        result = propose_candidate(parent, observations, LIMITS, [], competition_context=competition())
        pool = result['candidate_pool']
        self.assertLessEqual(len(pool), MAX_CANDIDATES)
        widened = [item for item in pool if item['policy'].get('allocation_mode') == 'equal_eligible'
                   and item['policy']['strategies'][0]['top_n'] >= 8]
        self.assertTrue(widened)
        change = widened[0]
        self.assertEqual(change['change_family'], 'allocation')
        self.assertEqual(change['policy']['cash_reserve'], parent['cash_reserve'])
        for key in ('conditions', 'holding_sessions', 'stop_loss_pct', 'take_profit_pct', 'weight'):
            self.assertEqual(change['policy']['strategies'][0][key], parent['strategies'][0][key])
        self.assertEqual(parent, original)
        self.assertNotIn('limits', change['policy'])
        expanded = [item for item in pool if item['policy'].get('allocation_mode') == 'equal_eligible'
                    and item['policy']['max_positions'] == 20 and item['policy']['strategies'][0]['top_n'] >= 16]
        self.assertTrue(expanded)
        self.assertEqual(expanded[0]['change_family'], 'allocation')
        self.assertEqual(expanded[0]['policy']['cash_reserve'], parent['cash_reserve'])
        for key in ('conditions', 'holding_sessions', 'stop_loss_pct', 'take_profit_pct', 'weight'):
            self.assertEqual(expanded[0]['policy']['strategies'][0][key], parent['strategies'][0][key])
        ordinary = propose_candidate(parent, observations, LIMITS, [])
        stale = propose_candidate(parent, observations, LIMITS, [], competition_context=competition(stale=True))
        self.assertEqual(ordinary['policy'], stale['policy'])
        self.assertEqual([item['policy'] for item in ordinary['candidate_pool']],
                         [item['policy'] for item in stale['candidate_pool']])
        self.assertFalse(stale['competition_context']['reliable'])
        self.assertTrue(result['competition_context']['reliable'])

    def test_legacy_also_compares_diversified_bootstrap_and_preserves_return_scoring(self):
        def evaluate(candidate, observations, limits):
            value = metrics(net=1, stress=.5, drawdown=1) if candidate.get('allocation_mode') else metrics()
            value['trading_activity'] = {'basis': 'modelled_fills', 'gross_amount': 1e20 if candidate.get('allocation_mode') else 1}
            return value
        with patch('backend.learning_research.portfolio_metrics', side_effect=evaluate):
            result = propose_candidate(BASELINE_POLICY, frames(3), LIMITS, [], competition_context=competition())
        self.assertTrue(any(item['policy'].get('allocation_mode') == 'equal_eligible'
                            for item in result['candidate_pool']))
        self.assertNotIn('allocation_mode', result['policy'])
        self.assertEqual(result['development']['net_return_pct'], 10)

    def test_llm_receives_allowlisted_progress_and_proposal_identity_freezes_it(self):
        llm = Mock()
        llm.generate.return_value = rule()
        raw = competition(account={'number': 'DO_NOT_SEND'})
        propose_candidate(BASELINE_POLICY, frames(1), LIMITS, [], llm, competition_context=raw)
        payload = llm.generate.call_args.args[1]
        self.assertEqual(payload['competition_context'], public_competition_context(raw))
        self.assertEqual(payload['allocation_limits']['budget'], float(LIMITS['budget']))
        self.assertNotIn('DO_NOT_SEND', json.dumps(payload))
        args = (BASELINE_POLICY, frames(1), LIMITS, [], {}, None, '2026-10-13T08:00:00+00:00')
        before = proposal_context(*args, competition_context=raw)
        raw['pace']['gross_amount'] = '5000000'
        after = proposal_context(*args, competition_context=raw)
        self.assertNotEqual(before['research_input_hash'], after['research_input_hash'])
        self.assertEqual(before['competition_context']['pace']['gross_amount'], 4767100)


if __name__ == '__main__':
    unittest.main()
