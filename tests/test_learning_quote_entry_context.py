"""Quote coverage follows each frozen virtual book without changing its signals."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.experiments import ALL_STRATEGIES, ExperimentService, SymbolUnavailable
from backend.learning_policy import BASELINE_POLICY
from tests.test_learning_policy import policy, row


def book(*held, pending=()):
    return {'positions': [{'symbol': symbol} for symbol in held],
            'reserved_cash': '0', 'reserved_entries': [],
            'pending_entries': [{'symbol': symbol} for symbol in pending]}


class LearningQuoteEntryContextTests(unittest.TestCase):
    def observe(self, state, *, seen=(), runs=None, active=None, live=None, frozen=(), marked=None, unavailable=()):
        frame = {'as_of': '2026-10-14', 'captured_at': '2026-10-14T16:10:00+09:00',
                 'rows': [row(f'{number:06d}') for number in range(1, 9)],
                 'baseline_signals': []}
        for number, item in enumerate(frame['rows']):
            item['features']['return_5d_pct'] = 100 - number
        control = {'execution_strategy': ALL_STRATEGIES, 'fingerprint': 'fixture',
                   'budget': '10000', 'order_cap': '1000', 'daily_buy_limit': '5000'}
        saved = []
        marks = []
        def quote(broker, symbol):
            if symbol in unavailable:
                raise SymbolUnavailable('fixture')
            return 100
        service = SimpleNamespace(
            now=lambda: datetime.fromisoformat('2026-10-15T10:00:00+09:00'),
            learning=SimpleNamespace(frames=lambda _: [frame], state=lambda: state,
                                     daily=SimpleNamespace(enabled=True, engine_version='fixture-version'),
                                     active_policy=lambda: active or BASELINE_POLICY),
            store=SimpleNamespace(runs=lambda *args, **kwargs: runs or [],
                setting=lambda key, *args: {'day': '2026-10-15', 'fingerprint': 'fixture',
                    'quotes': {symbol: {} for symbol in
                        ((seen if marked is None else marked) if key == 'learning_protection_quotes' else seen)}}),
            _entry_selection_book=lambda: live or book(), _owned_orders=lambda _: [],
            _portfolio=lambda _: ([], []),
            _broker=Mock(return_value=SimpleNamespace(
                session_days=lambda *args: ['2026-10-15'], snapshot=lambda: {'holdings': {}})),
            _live_quote=quote,
            _decision_quote=lambda symbol, price: {'observed_at': '2026-10-15T10:00:00+09:00'},
            _timestamp=lambda: '2026-10-15T10:00:00+09:00',
            _save_protection_quotes=lambda value: marks.append(value),
            _save_learning_quote=lambda symbol, price=None, **kwargs: saved.append(symbol))
        original = deepcopy(state)
        with patch('backend.daily_comparison.observation_books', return_value=deepcopy(frozen)):
            ExperimentService._observe_legacy_learning_quotes(service, control)
        self.assertEqual(state, original)
        service.saved_marks = marks
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

    def test_live_held_excluded_from_signals_and_sold_frozen_holdings_are_observed_first(self):
        candidate = policy(held_before_top_n=True)
        saved, service = self.observe({}, active=candidate,
            live=book('000008'), frozen=[book('000007', pending=['000006'])])
        self.assertEqual(saved[:2], ['000007', '000008'])
        self.assertIn('000001', saved)
        self.assertIn('000006', saved)
        self.assertEqual([next(iter(item)) for item in service.saved_marks], saved)

    def test_early_execution_quote_does_not_count_as_a_missing_protection_observation(self):
        saved, _ = self.observe({}, seen=['000001'], marked=[], live=book('000001'))
        self.assertEqual(saved, ['000001'])

    def test_six_new_held_names_are_not_lost_to_top_n_or_five_quote_batch(self):
        symbols = ['011200', '028670', '031980', '066970', '067310', '078600']
        saved, _ = self.observe({}, live=book(*symbols), active=policy(held_before_top_n=True))
        self.assertEqual(saved, symbols[:5])
        following, _ = self.observe({}, live=book(*symbols), seen=saved,
                                    active=policy(held_before_top_n=True))
        self.assertEqual(following[0], symbols[-1])

    def test_unavailable_held_observation_remains_missing_without_starving_later_batches(self):
        symbols = [f'{number:06d}' for number in range(2, 8)]
        saved, service = self.observe({}, live=book(*symbols), unavailable=symbols)
        self.assertEqual(saved, symbols[:5])
        self.assertTrue(all(next(iter(mark.values()))['source'] == 'unavailable'
                            and next(iter(mark.values()))['price'] is None for mark in service.saved_marks))
        following, _ = self.observe({}, live=book(*symbols), seen=saved, marked=saved)
        self.assertEqual(following[0], symbols[-1])


if __name__ == '__main__':
    unittest.main()
