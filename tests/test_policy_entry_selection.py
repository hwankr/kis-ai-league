"""Opt-in entry selection uses a frozen book; existing lot rules stay frozen."""
from copy import deepcopy
from decimal import Decimal
import unittest

from backend.learning_policy import (BASELINE_POLICY, canonical_entry_context,
    policy_id, portfolio_metrics, position_exit_due, signals, validate_policy)
from tests.test_learning_policy import frames, policy, row


LIMITS = {'budget': '10000', 'daily_buy_limit': '1000', 'order_cap': '1000'}


def fixture(count=3, *, quantity=1, exit_pending=False):
    values = frames(count)
    for frame in values:
        frame['rows'] = [row('000001'), row('000002'), row('000003')]
        for item, score in zip(frame['rows'], (30, 20, 10)):
            item['features']['return_5d_pct'] = score
    book = {'as_of': values[0]['as_of'], 'cash': str(10000-quantity*100), 'reserved_cash': '0',
        'reserved_entries': [], 'positions': [{'symbol': '000001', 'strategy_id': 'momentum',
            'quantity': quantity, 'cost': str(quantity*100), 'entry_price': '100', 'last_close': '100',
            'held_sessions': 0, 'exit_pending': exit_pending,
            'exit_policy': {'holding_sessions': 20, 'stop_loss_pct': 0, 'take_profit_pct': 0}}],
        'account_protection': {'units': '10000', 'peak': '1', 'active': False, 'limit_pct': 15}}
    return values, book


class EntrySelectionContractTests(unittest.TestCase):
    def test_omitted_fields_preserve_existing_policy_hashes(self):
        self.assertEqual(policy_id(policy()), 'f38c7eade182b96b88e1aba9725538ccce28d21366c2a0cfb12f31d375faf6d6')
        self.assertEqual(policy_id(BASELINE_POLICY), '08417ee8437473db8c2e91106692e031f8e07b1cc17427c485e82f6bf171766a')
        self.assertNotIn('held_before_top_n', validate_policy(policy()))
        self.assertNotIn('no_same_day_reentry', validate_policy(policy()))

    def test_explicit_false_is_preserved_and_behaves_like_omission(self):
        values, book = fixture()
        old = policy(max_positions=2)
        explicit = {**deepcopy(old), 'held_before_top_n': False, 'no_same_day_reentry': False}
        canonical = validate_policy(explicit)
        self.assertIs(canonical['held_before_top_n'], False)
        self.assertIs(canonical['no_same_day_reentry'], False)
        self.assertNotEqual(policy_id(old), policy_id(explicit))
        original = portfolio_metrics(old, values, LIMITS, initial_state=book)
        current = portfolio_metrics(explicit, values, LIMITS, initial_state=book)
        self.assertTrue(original['valid'], original['error'])
        self.assertTrue(current['valid'], current['error'])
        for key in ('trading_activity', 'stress_trading_activity', 'equity_curve', 'stress_equity_curve',
                    'diagnostics', 'net_return_pct', 'stress_return_pct', 'closed_trades'):
            self.assertEqual(original[key], current[key])

    def test_options_are_boolean_and_legacy_remains_fixed(self):
        for field in ('held_before_top_n', 'no_same_day_reentry'):
            for value in (None, 0, 1, 'true', [], {}):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, 'invalid_'+field):
                    validate_policy({**policy(), field: value})
            for value in (False, True):
                with self.assertRaisesRegex(ValueError, 'legacy_policy_is_fixed'):
                    validate_policy({**BASELINE_POLICY, field: value})

    def test_context_canonicalizes_symbols_without_mutating_book(self):
        _, book = fixture()
        book['positions'] += [{'symbol': '000002'}, {'symbol': '000001'}]
        book['reserved_entries'] = [{'symbol': '000003'}, {'symbol': '000002'}]
        original = deepcopy(book)
        self.assertEqual(canonical_entry_context(book),
            {'held_symbols': ['000001', '000002'], 'reserved_symbols': ['000002', '000003']})
        self.assertEqual(book, original)
        book.pop('reserved_entries')
        book['reserved_cash'] = '1'
        with self.assertRaisesRegex(ValueError, 'reserved_entries_required'):
            canonical_entry_context(book)

    def test_opt_in_requires_explicit_valid_context(self):
        values, _ = fixture()
        candidate = policy(held_before_top_n=True)
        with self.assertRaisesRegex(ValueError, 'entry_context_required'):
            signals(candidate, values[0])
        for context in ({}, {'held_symbols': []}, {'held_symbols': [], 'reserved_symbols': [], 'other': []},
                        {'held_symbols': set(), 'reserved_symbols': []}):
            with self.assertRaisesRegex(ValueError, 'invalid_entry_context'):
                signals(candidate, values[0], entry_context=context)
        with self.assertRaisesRegex(ValueError, 'duplicate_entry_context_symbol'):
            signals(candidate, values[0], entry_context={'held_symbols': ['000001', '000001'], 'reserved_symbols': []})

    def test_excludes_held_and_reserved_before_ranking_top_n(self):
        values, book = fixture()
        book['reserved_entries'] = [{'symbol': '000002', 'strategy_id': 'momentum', 'cost': '100'}]
        candidate = policy(held_before_top_n=True, allocation_mode='score_rank')
        frozen = deepcopy([candidate, values, book])
        result = signals(candidate, values[0], entry_context=canonical_entry_context(book))
        self.assertEqual([(item['symbol'], item['rank']) for item in result], [('000003', 1)])
        self.assertEqual([candidate, values, book], frozen)
        self.assertEqual(signals(policy(), values[0])[0]['symbol'], '000001')

    def test_full_frame_metadata_validation_is_not_hidden_by_exclusion(self):
        values, book = fixture()
        for item in values[0]['rows'][1:]:
            item['selection'] = {'complete_61': True, 'positive_volume_20': True, 'turnover_20d_mean': 20_000_000_000}
        candidate = policy(held_before_top_n=True, candidate_scope='liquid_all')
        with self.assertRaisesRegex(ValueError, 'candidate_scope_metadata_missing'):
            signals(candidate, values[0], entry_context=canonical_entry_context(book))

    def test_held_filter_does_not_expand_restricted_scope(self):
        frame = frames(1)[0]
        frame['rows'] = [row(f'{index:06d}', selection={'complete_61': True, 'positive_volume_20': True,
            'turnover_20d_mean': (100-index)*1_000_000_000}) for index in range(1, 22)]
        candidate = policy(held_before_top_n=True, candidate_scope='liquid_top20')
        context = {'held_symbols': [f'{index:06d}' for index in range(1, 21)], 'reserved_symbols': []}
        self.assertEqual(signals(candidate, frame, entry_context=context), [])


class EntrySelectionReplayTests(unittest.TestCase):
    def test_close_book_refills_unheld_and_keeps_holding_price_and_exit_rule(self):
        values, book = fixture()
        original_exit = deepcopy(book['positions'][0]['exit_policy'])
        candidate = policy(max_positions=2, held_before_top_n=True, no_same_day_reentry=True,
                           allocation_mode='equal_eligible')
        candidate['strategies'][0]['holding_sessions'] = 1
        result = portfolio_metrics(candidate, values[:2], LIMITS, initial_state=book)
        self.assertTrue(result['valid'], result['error'])
        held = {item['symbol']: item for item in result['final_state']['positions']}
        self.assertEqual(set(held), {'000001', '000002'})
        self.assertEqual(held['000001']['exit_policy'], original_exit)
        self.assertEqual(held['000001']['quantity'], 1)
        self.assertEqual(held['000002']['exit_policy']['holding_sessions'], 1)

    def test_reservation_is_excluded_before_top_n_and_keeps_its_slot(self):
        values, book = fixture(2)
        book.update(positions=[], cash='10000', reserved_cash='100',
            reserved_entries=[{'symbol': '000001', 'strategy_id': 'momentum', 'cost': '100'}])
        candidate = policy(max_positions=2, held_before_top_n=True, allocation_mode='equal_eligible')
        result = portfolio_metrics(candidate, values, LIMITS, initial_state=book)
        self.assertTrue(result['valid'], result['error'])
        self.assertEqual([item['symbol'] for item in result['final_state']['positions']], ['000002'])
        self.assertEqual(result['final_state']['reserved_cash'], '100')
        self.assertEqual(result['final_state']['reserved_entries'], book['reserved_entries'])

    def test_positive_sale_blocks_same_day_buy_but_allows_later_day(self):
        for mode in ('legacy', 'equal_eligible', 'score_rank'):
            values, book = fixture(exit_pending=True)
            candidate = policy(max_positions=1, allocation_mode=mode, no_same_day_reentry=True)
            result = portfolio_metrics(candidate, values, LIMITS, initial_state=book)
            self.assertTrue(result['valid'], result['error'])
            self.assertGreater(result['equity_curve'][1]['gross_sell_amount'], 0)
            self.assertEqual(result['equity_curve'][1]['gross_buy_amount'], 0)
            self.assertGreater(result['equity_curve'][2]['gross_buy_amount'], 0)
            candidate.pop('no_same_day_reentry')
            original = portfolio_metrics(candidate, values, LIMITS, initial_state=book)
            self.assertGreater(original['equity_curve'][1]['gross_buy_amount'], 0)

    def test_partial_delayed_sale_retains_basis_slot_and_protection(self):
        values, book = fixture(quantity=7, exit_pending=True)
        candidate = policy(max_positions=1, held_before_top_n=True, no_same_day_reentry=True,
                           allocation_mode='equal_eligible')
        result = portfolio_metrics(candidate, values, LIMITS, initial_state=book,
            execution_model={'mode': 'daily_open', 'fill_ratio': .5, 'sell_delay_sessions': 1})
        self.assertTrue(result['valid'], result['error'])
        self.assertEqual(result['trading_activity']['buy_fill_events'], 0)
        self.assertEqual(result['final_state']['positions'][0]['quantity'], 4)
        self.assertEqual(Decimal(result['final_state']['positions'][0]['cost']), 400)
        self.assertTrue(result['final_state']['positions'][0]['exit_pending'])
        self.assertEqual(Decimal(result['final_state']['account_protection']['units']), 10000)
        self.assertEqual(Decimal(result['final_state']['account_protection']['limit_pct']), 15)

    def test_saved_pending_uses_same_frozen_book_context(self):
        values, book = fixture()
        candidate = policy(max_positions=2, held_before_top_n=True)
        book['policy_id'] = policy_id(candidate)
        book['pending_entries'] = signals(candidate, values[0], entry_context=canonical_entry_context(book))
        result = portfolio_metrics(candidate, values, LIMITS, initial_state=book)
        self.assertTrue(result['valid'], result['error'])
        stale = deepcopy(book)
        stale['pending_entries'] = signals(candidate, values[0], entry_context={'held_symbols': [], 'reserved_symbols': []})
        result = portfolio_metrics(candidate, values, LIMITS, initial_state=stale)
        self.assertFalse(result['valid'])
        self.assertEqual(result['error'], 'initial_pending_signals_changed')

    def test_existing_signal_extension_uses_raw_rules_not_entry_exclusion(self):
        values, book = fixture()
        frozen = policy(held_before_top_n=True, no_same_day_reentry=True)
        frozen['strategies'][0].update(exit_mode='signal_extension', holding_sessions=1, max_holding_sessions=4)
        rule = signals(frozen, values[0], entry_context={'held_symbols': [], 'reserved_symbols': []})[0]['exit_policy']
        position = book['positions'][0]
        position.update(exit_policy=rule, held_sessions=1, last_close='110')
        for frame in values:
            frame['rows'][0].update(open=110, high=111, low=109, close=110)
        self.assertFalse(position_exit_due(rule, symbol='000001', held_sessions=2, entry_price='100',
            last_close='110', quantity=1, cost='100', frame=values[0]))
        candidate = policy(max_positions=1, held_before_top_n=True, no_same_day_reentry=True)
        candidate['strategies'][0]['holding_sessions'] = 1
        result = portfolio_metrics(candidate, values, LIMITS, initial_state=book)
        self.assertTrue(result['valid'], result['error'])
        self.assertEqual(result['trading_activity']['sell_fill_events'], 0)
        self.assertEqual(result['final_state']['positions'][0]['held_sessions'], 3)
        self.assertEqual(result['final_state']['positions'][0]['exit_policy']['continuation_policy'], validate_policy(frozen))
        self.assertTrue(position_exit_due(rule, symbol='000001', held_sessions=4, entry_price='100',
            last_close='110', quantity=1, cost='100', frame=values[0]))


if __name__ == '__main__':
    unittest.main()
