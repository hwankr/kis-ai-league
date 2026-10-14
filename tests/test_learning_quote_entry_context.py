"""Quote coverage follows each frozen virtual book without changing its signals."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from backend.experiments import ALL_STRATEGIES, ExperimentService
from backend.learning_policy import BASELINE_POLICY
from tests.test_learning_policy import policy, row


def book(*held, pending=()):
    return {'positions': [{'symbol': symbol} for symbol in held],
            'reserved_cash': '0', 'reserved_entries': [],
            'pending_entries': [{'symbol': symbol} for symbol in pending]}


class LearningQuoteEntryContextTests(unittest.TestCase):
    def observe(self, state, *, seen=(), runs=None, active=None):
        frame = {'as_of': '2026-10-14', 'captured_at': '2026-10-14T16:10:00+09:00',
                 'rows': [row(f'{number:06d}') for number in range(1, 9)],
                 'baseline_signals': []}
        for number, item in enumerate(frame['rows']):
            item['features']['return_5d_pct'] = 100 - number
        control = {'execution_strategy': ALL_STRATEGIES, 'fingerprint': 'fixture'}
        saved = []
        service = SimpleNamespace(
            now=lambda: datetime.fromisoformat('2026-10-15T10:00:00+09:00'),
            learning=SimpleNamespace(frames=lambda _: [frame], state=lambda: state,
                                     active_policy=lambda: active or BASELINE_POLICY),
            store=SimpleNamespace(runs=lambda *args, **kwargs: runs or [],
                setting=lambda *args: {'day': '2026-10-15', 'fingerprint': 'fixture',
                                      'quotes': {symbol: {} for symbol in seen}}),
            _entry_selection_book=lambda: book(), _owned_orders=lambda _: [],
            _portfolio=lambda _: ([], []),
            _broker=Mock(return_value=SimpleNamespace(
                session_days=lambda *args: ['2026-10-15'], snapshot=lambda: {'holdings': {}})),
            _live_quote=lambda broker, symbol: 100,
            _save_learning_quote=lambda symbol, price, **kwargs: saved.append(symbol))
        original = deepcopy(state)
        ExperimentService._observe_learning_quotes(service, control)
        self.assertEqual(state, original)
        return saved, service

    def test_virtual_book_backfill_is_observed_even_when_live_book_needs_only_first_rank(self):
        candidate = policy(held_before_top_n=True, no_same_day_reentry=True)
        state = {'trial': {'policy': candidate, 'incumbent': BASELINE_POLICY,
                          'initial_state': book('000001'), 'metrics': {}}}
        saved, _ = self.observe(state, seen=['000001'])
        self.assertEqual(saved, ['000002'])

    def test_each_normal_stress_and_scenario_book_includes_its_own_context_and_pending(self):
        candidate = policy(held_before_top_n=True)
        state = {'trial': {'policy': candidate, 'incumbent': BASELINE_POLICY,
            'initial_state': {'scenarios': {'delayed': {
                'normal': book('000001', pending=['000007']),
                'stress': book('000001', '000002')}}},
            'metrics': {'challenger': {'final_state': book('000001', '000002', '000003'),
                'stress_final_state': None, 'scenario_results': {'partial': {
                    'final_state': None, 'stress_final_state': book(pending=['000008'])}}}}}}
        saved, _ = self.observe(state, seen=['000001', '000002', '000003'])
        self.assertEqual(saved, ['000004', '000007', '000008'])

    def test_champion_metrics_use_incumbent_policy_context(self):
        candidate = policy(held_before_top_n=True)
        state = {'trial': {'policy': BASELINE_POLICY, 'incumbent': candidate,
            'metrics': {'champion': {'final_state': book('000001'), 'stress_final_state': None}}}}
        saved, _ = self.observe(state, seen=['000001'])
        self.assertEqual(saved, ['000002'])

    def test_scorecard_pending_and_stored_run_are_retained_with_five_quote_limit(self):
        state = {'scorecard_book': book('000001', pending=['000002', '000003', '000004', '000005'])}
        runs = [{'as_of': '2026-10-14', 'signals': [{'symbol': '000006'}, {'symbol': '000007'}]},
                {'as_of': '2026-10-13', 'signals': [{'symbol': '000008'}]}]
        saved, _ = self.observe(state, runs=runs)
        self.assertEqual(saved, ['000001', '000002', '000003', '000004', '000005'])
        saved, _ = self.observe(state, runs=runs, seen=saved)
        self.assertEqual(saved, ['000006', '000007'])

    def test_seen_virtual_quotes_do_not_query_broker_again(self):
        state = {'trial': {'policy': policy(held_before_top_n=True),
                          'initial_state': book('000001')}}
        saved, service = self.observe(state, seen=['000001', '000002'])
        self.assertEqual(saved, [])
        service._broker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
