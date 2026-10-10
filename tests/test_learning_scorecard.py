"""Offline cumulative comparisons with the real replay engine and temporary DB."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from backend.experiment_store import ExperimentStore
from backend.learning import LearningService
from backend.learning_account import LearningAccount, owned_book, FEE, TAX
from backend.learning_policy import BASELINE_POLICY, portfolio_metrics
from tests.test_learning_policy import frames, policy as rules


LIMITS = {'budget': '10000', 'order_cap': '2500', 'daily_buy_limit': '10000'}
MODEL = {'mode': 'observed_quotes', 'slippage_bps': 0, 'stress_slippage_bps': 0}


class ScorecardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'test.sqlite3'
        self.store = ExperimentStore(self.path)
        self.account = LearningAccount(self.store)
        self.policy = {**LIMITS, 'fingerprint': 'test-account'}
        self.sample = frames(4, price=100)
        for index, frame in enumerate(self.sample):
            frame['benchmark_prices'] = {'KOSPI': 100 + index * 5}
            frame['captured_at'] = frame['observed_at']
            frame['execution_quotes'] = {'005930': {
                'price': 100, 'eligible': True,
                'observed_at': frame['as_of'] + 'T09:05:00+09:00'}}
        self.current = datetime.fromisoformat(self.sample[0]['observed_at'])
        self.service = self.make_service(self.store)
        self.store.save_settings({'enabled': False, 'user_paused': True})

    def make_service(self, store):
        service = LearningService(store, enabled=True, now=lambda: self.current)
        self.addCleanup(service.close)
        return service

    def cash_book(self, index, equity):
        return {'as_of': self.sample[index]['as_of'], 'cash': str(equity),
                'equity': str(equity), 'budget': LIMITS['budget'], 'positions': [],
                'reserved_cash': '0', 'pending_entries': [],
                'pending_orders': 0, 'unresolved_orders': 0}

    def update(self, index, book, *, service=None, persist=True):
        service = service or self.service
        self.current = datetime.fromisoformat(self.sample[index]['observed_at'])
        self.account.observe(self.policy, book, self.current.isoformat(), daily=True)
        state = service.state()
        service._scorecard(state, self.sample[:index + 1], LIMITS, MODEL,
                           self.policy['fingerprint'], book)
        if persist:
            service._save(state)
        return state

    def held_book(self):
        return owned_book(self.policy, [{
            'symbol': '005930', 'strategy_id': 'inherited', 'quantity': 10,
            'cost': '1000', 'average_price': '100',
            'entry_date': self.sample[0]['as_of'],
            'exit_policy': {'holding_sessions': 20, 'stop_loss_pct': 0, 'take_profit_pct': 0}
        }], [], [], {'005930': 100}, as_of=self.sample[0]['as_of'])

    def set_price(self, index, price):
        self.sample[index]['rows'][0].update(open=price, close=price, high=price + 1, low=price - 1)
        self.sample[index]['execution_quotes']['005930']['price'] = price

    def test_existing_ai_losses_are_rebased_only_for_the_common_comparison_window(self):
        # The actual account is already down 20% before comparison starts.
        first = self.update(0, self.cash_book(0, 8000))
        self.assertAlmostEqual(self.account.state('test-account')['return_pct'], -20)
        self.assertAlmostEqual(first['scorecard']['ai_return_pct'], 0)
        later = self.update(1, self.cash_book(1, 8800))
        self.assertAlmostEqual(later['scorecard']['ai_return_pct'], 10)
        self.assertAlmostEqual(self.account.state('test-account')['return_pct'], -12)
        self.assertAlmostEqual(later['scorecard']['baseline_return_pct'], 0)
        self.assertAlmostEqual(later['scorecard']['market_return_pct'], 5)
        self.assertEqual(later['scorecard_capital'], '8000')
        self.assertEqual(later['scorecard']['cash_return_pct'], 0)
        self.assertEqual(later['scorecard']['start_day'], self.sample[0]['as_of'])

    def test_missing_baseline_quote_keeps_error_but_ai_market_and_daily_rows_advance(self):
        self.sample[0]['baseline_signals'] = [{
            'strategy_id': 'trend-breakout-v1', 'symbol': '005930',
            'action': 'buy', 'status': 'ready'}]
        first = self.update(0, self.cash_book(0, 10000))
        self.sample[1]['execution_quotes'] = {}
        invalid = self.update(1, self.cash_book(1, 10200))
        self.assertEqual(invalid['scorecard']['status'], 'invalid')
        self.assertEqual(invalid['scorecard']['error'], 'execution_quote_missing')
        self.assertIsNone(invalid['scorecard']['baseline_return_pct'])
        later = self.update(2, self.cash_book(2, 10700))
        self.assertEqual(later['scorecard']['status'], 'invalid')
        self.assertEqual(later['scorecard']['error'], 'execution_quote_missing')
        self.assertIsNone(later['scorecard']['baseline_return_pct'])
        self.assertAlmostEqual(later['scorecard']['ai_return_pct'], 7)
        self.assertAlmostEqual(later['scorecard']['market_return_pct'], 10)
        self.assertEqual(later['scorecard_book'], first['scorecard_book'])
        self.assertEqual(later['scorecard_day'], self.sample[2]['as_of'])
        with self.store.connect() as db:
            import json
            rows = db.execute('SELECT payload FROM learning_daily ORDER BY day').fetchall()
        self.assertEqual(len(rows), 3)
        self.assertAlmostEqual(json.loads(rows[-1][0])['comparisons']['ai_return_pct'], 7)
        self.assertIsNone(json.loads(rows[-1][0])['comparisons']['baseline_return_pct'])

    def test_baseline_carries_inherited_holdings_cash_and_loss_through_all_windows(self):
        book = self.held_book()
        original = deepcopy(book)
        self.set_price(1, 80)
        self.set_price(2, 90)
        self.update(0, book)
        first_loss = self.update(1, self.cash_book(1, 9500))
        later = self.update(2, self.cash_book(2, 9700))
        full = portfolio_metrics(BASELINE_POLICY, self.sample[:3], LIMITS,
                                 initial_state=original, execution_model=MODEL)
        self.assertTrue(full['valid'], full['error'])
        self.assertEqual(later['scorecard_book'], full['final_state'])
        self.assertEqual(Decimal(later['scorecard_book']['cash']), Decimal('9000'))
        self.assertEqual(later['scorecard_book']['positions'][0]['quantity'], 10)
        self.assertEqual(later['scorecard_book']['positions'][0]['strategy_id'], 'inherited')
        self.assertEqual(Decimal(later['scorecard_book']['positions'][0]['cost']), Decimal('1000'))
        self.assertEqual(later['scorecard_capital'], original['equity'])
        expected = (Decimal('9000') + Decimal('900') * (1 - FEE - TAX)) / Decimal(original['equity']) - 1
        self.assertAlmostEqual(later['scorecard']['baseline_return_pct'], float(expected * 100))
        self.assertLess(first_loss['scorecard']['baseline_return_pct'], later['scorecard']['baseline_return_pct'])
        self.assertLess(later['scorecard']['baseline_return_pct'], 0)
        self.assertEqual(book, original)

    def test_restart_and_policy_promotion_keep_the_original_comparison_origin(self):
        book = self.held_book()
        self.set_price(1, 80)
        self.set_price(2, 90)
        self.update(0, book)
        before = self.update(1, self.cash_book(1, 9500))
        before['champion'] = {'policy': rules(), 'adopted_at': self.current.isoformat()}
        before['last_change'] = {'at': self.current.isoformat(), 'reason': 'paper_provisional'}
        self.service._save(before)
        restored = self.make_service(ExperimentStore(self.path))
        self.assertEqual(restored.state()['scorecard'], before['scorecard'])
        after = self.update(2, self.cash_book(2, 9700), service=restored)
        for key in ('scorecard_start', 'scorecard_capital', 'scorecard_ai_unit',
                    'scorecard_limits', 'scorecard_model'):
            self.assertEqual(after[key], before[key])
        self.assertLess(after['scorecard']['baseline_return_pct'], 0)
        self.assertAlmostEqual(after['scorecard']['ai_return_pct'], (9700 / float(book['equity']) - 1) * 100)
        self.assertEqual(after['champion'], before['champion'])
        self.assertFalse(self.store.setting('enabled'))
        self.assertTrue(self.store.setting('user_paused'))
        self.assertEqual(self.store.orders(), [])

    def test_new_engine_invalidates_only_baseline_and_preserves_original_evidence(self):
        self.update(0, self.cash_book(0, 10000))
        before = self.update(1, self.cash_book(1, 9800))
        restored = self.make_service(ExperimentStore(self.path))
        restored.version = 'changed-engine-for-test'
        after = self.update(2, self.cash_book(2, 9900), service=restored)
        self.assertEqual(after['scorecard']['status'], 'invalid')
        self.assertIsNone(after['scorecard']['baseline_return_pct'])
        self.assertIn('버전 변경', after['scorecard']['error'])
        for key in ('scorecard_engine', 'scorecard_book', 'scorecard_start', 'scorecard_capital',
                    'scorecard_ai_unit', 'scorecard_limits', 'scorecard_model'):
            self.assertEqual(after[key], before[key])
        later = self.update(3, self.cash_book(3, 10100), service=restored)
        self.assertAlmostEqual(later['scorecard']['ai_return_pct'], 1)
        self.assertAlmostEqual(later['scorecard']['market_return_pct'], 15)
        self.assertEqual(later['scorecard']['error'], after['scorecard']['error'])
        self.assertIsNone(later['scorecard']['baseline_return_pct'])
        self.assertEqual(later['scorecard_start'], self.sample[0]['as_of'])


if __name__ == '__main__':
    unittest.main()
