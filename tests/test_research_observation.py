"""Isolated research quote batches never grant or delay live order permission."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import sqlite3
import unittest
from unittest.mock import Mock, patch

from backend.experiment_store import ExperimentStore, encode
from backend.experiments import ExperimentService
from backend.research_observation import PREFIX, VERSION, _save, context, current_scope, observe, scope_for


class ResearchObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ExperimentStore(Path(self.temp.name) / 'fixture.sqlite3')
        with self.store.connect() as db:
            db.executescript('''
                CREATE TABLE learning_frames (day TEXT PRIMARY KEY,digest TEXT,payload TEXT);
                CREATE TABLE daily_policy_contexts (fingerprint TEXT,day TEXT,payload TEXT);
                CREATE TABLE daily_policy_followups (id TEXT,fingerprint TEXT,payload TEXT);
            ''')
        self.now = datetime.fromisoformat('2026-10-21T09:05:00+09:00')
        self.symbols = [f'{i:06d}' for i in range(1, 7)]
        self.frame = {'as_of': '2026-10-20', 'captured_at': '2026-10-20T16:10:00+09:00',
                      'rows': [{'symbol': s} for s in self.symbols],
                      'execution_quotes': {}, 'protection_quotes': {}}
        self.policy = {'fingerprint': 'fixture', 'execution_strategy': 'all'}
        self.live = {'positions': [], 'reserved_entries': []}
        self.state = {}
        self.calls = []
        self.holdings = {}
        self.owned = []
        self.eligible = True
        self.quote_time = None
        self.store.save_setting('policy', self.policy)
        self.live_quotes = {'day': '2026-10-21', 'fingerprint': 'fixture', 'quotes': {
            '000001': {'price': '9999', 'observed_at': '2026-10-21T09:04:00+09:00'}}}
        self.store.save_settings({'learning_quotes': self.live_quotes,
                                  'learning_protection_quotes': self.live_quotes})
        self.broker = SimpleNamespace(quote=self.quote, market_session=self.minute,
            session_days=lambda *args: ['2026-10-20', '2026-10-21'],
            snapshot=Mock(side_effect=lambda: {'holdings': deepcopy(self.holdings)}))
        self.service = SimpleNamespace(store=self.store, now=lambda: self.now,
            learning=SimpleNamespace(frames=lambda *args: [deepcopy(self.frame)],
                                     state=lambda: deepcopy(self.state),
                                     version='learning-fixture',
                                     daily=SimpleNamespace(enabled=True, engine_version='daily-fixture')),
            _entry_selection_book=lambda: deepcopy(self.live),
            _broker=lambda policy: self.broker, _market_open=lambda: self.now.hour < 15,
            _policy=lambda: self.store.setting('policy'), _owned_orders=lambda policy: [],
            _portfolio=lambda orders: (deepcopy(self.owned), []),
            _timestamp=lambda: self.now.isoformat(), stopping=False)

    def quote(self, symbol):
        # A broker call must not run under the batch persistence transaction.
        other = sqlite3.connect(self.store.path, timeout=0)
        try:
            other.execute('BEGIN IMMEDIATE')
            other.rollback()
        finally:
            other.close()
        self.now += timedelta(seconds=1)
        self.calls.append(('quote', symbol))
        return {'price': '10000', 'eligible': self.eligible,
                'as_of': self.quote_time or self.now.isoformat()}

    def minute(self, symbol):
        self.calls.append(('minute', symbol))
        return {'session_date': self.now.date().isoformat(),
                'last_trade_at': self.now.isoformat(), 'volume': 10}

    def complete(self):
        for _ in range(20):
            observe(self.service, self.policy)
            value = context(self.service, self.policy)
            if value['research_observation']['status'] == 'complete':
                return value
        self.fail('fixture batch did not complete')

    def test_five_quote_budget_barrier_then_fresh_execution_preserves_live_first_quotes(self):
        observe(self.service, self.policy)
        value = context(self.service, self.policy)
        self.assertEqual(value['research_observation']['status'], 'collecting_protection')
        self.assertEqual(value['protection_quotes'], {})
        self.assertEqual(value['execution_quotes'], {})
        self.assertEqual(len(self.calls), 5)
        self.assertFalse(any(kind == 'minute' for kind, _ in self.calls))
        observe(self.service, self.policy)
        self.assertEqual(sum(kind == 'quote' for kind, _ in self.calls), 10)
        value = self.complete()
        barrier = datetime.fromisoformat(value['research_observation']['protection_completed_at'])
        self.assertTrue(all(datetime.fromisoformat(q['observed_at']) <= barrier
                            for q in value['protection_quotes'].values()))
        self.assertTrue(all(datetime.fromisoformat(q['observed_at']) >= barrier
                            for q in value['execution_quotes'].values()))
        self.assertEqual(sum(kind == 'quote' for kind, _ in self.calls), 12)
        self.assertEqual(self.store.setting('learning_quotes'), self.live_quotes)
        self.assertEqual(self.store.setting('learning_protection_quotes'), self.live_quotes)
        before = deepcopy(value)
        observe(self.service, self.policy)
        self.assertEqual(context(self.service, self.policy), before)
        self.assertEqual(sum(kind == 'quote' for kind, _ in self.calls), 12)

    def test_prior_quoted_descendants_and_outside_remaining_books_are_in_scope_without_replay(self):
        historical = deepcopy(self.frame)
        historical.update(as_of='2026-10-19', captured_at='2026-10-19T16:10:00+09:00',
            execution_quotes={'207940': {'price': '1428000'}, '003670': {'price': '188200'}})
        books = {'scenarios': {'partial': {'stress': {'positions': [{'symbol': '999991'}]},
            'normal': {'pending_entries': [{'symbol': '999992'}]}}}}
        scope = scope_for(day='2026-10-21', fingerprint='fixture', frames=[historical, self.frame], books=books)
        self.assertTrue({'207940', '003670', '999991', '999992'} <= set(scope['required']))
        before = deepcopy((historical, self.frame, books))
        with patch('backend.learning_policy.portfolio_metrics', side_effect=AssertionError('no replay')):
            self.state = books
            self.frame['protection_quotes'] = historical['execution_quotes']
            self.complete()
        self.assertEqual((historical, before[1], books), before)

    def test_scope_change_starts_new_batch_and_preserves_completed_original(self):
        old = self.complete()
        old_id = old['research_observation']['id']
        old_raw = self.store.setting(PREFIX + old_id)
        self.frame['rows'].append({'symbol': '999999'})
        value = context(self.service, self.policy)
        self.assertNotEqual(value['research_observation']['id'], old_id)
        self.assertEqual(value['research_observation']['status'], 'missing')
        self.assertEqual(value['execution_quotes'], {})
        observe(self.service, self.policy)
        self.assertEqual(self.store.setting(PREFIX + old_id), old_raw)

    def test_incomplete_batch_resumes_after_restart_and_old_mark_age_does_not_prevent_completion(self):
        observe(self.service, self.policy)
        old_scope = current_scope(self.service, self.policy)
        self.now += timedelta(hours=1)
        # Re-created store object, same immutable source and durable batch.
        self.service.store = ExperimentStore(self.store.path)
        value = self.complete()
        self.assertEqual(value['research_observation']['id'], old_scope['id'])
        self.assertEqual(sum(kind == 'quote' for kind, _ in self.calls), 12)

    def test_cached_execution_before_barrier_is_rejected_without_replacing_live_evidence(self):
        self.frame['rows'] = [{'symbol': '000001'}]
        observe(self.service, self.policy, budget=1)
        self.quote_time = '2026-10-21T09:05:00+09:00'
        with self.assertRaisesRegex(ValueError, 'before_protection_barrier'):
            observe(self.service, self.policy)
        value = context(self.service, self.policy)
        self.assertEqual(value['research_observation']['status'], 'collecting_execution')
        self.assertEqual(value['execution_quotes'], {})
        self.quote_time = None
        self.complete()
        self.assertEqual(self.store.setting('learning_quotes'), self.live_quotes)

    def test_ineligible_execution_is_explicit_and_cannot_borrow_protection_permission(self):
        self.frame['rows'] = [{'symbol': '000001'}]
        self.eligible = False
        value = self.complete()
        self.assertEqual(value['protection_quotes']['000001']['price'], '10000')
        self.assertFalse(value['execution_quotes']['000001']['eligible'])
        self.assertIsNone(value['execution_quotes']['000001']['price'])
        self.assertFalse(any(kind == 'minute' for kind, _ in self.calls))

    def test_external_holdings_exclude_buys_and_live_book_cash_quantity_do_not_reset_coverage(self):
        self.frame['rows'] = [{'symbol': '000001'}, {'symbol': '000002'}]
        self.holdings = {'000001': {'quantity': 4}, '000002': {'quantity': 3}}
        self.owned = [{'symbol': '000002', 'quantity': 3}]
        self.live = {'positions': deepcopy(self.owned), 'cash': '1000'}
        initial = current_scope(self.service, self.policy)
        self.live['cash'] = '2000'
        self.live['positions'][0]['quantity'] = 2
        self.assertEqual(current_scope(self.service, self.policy), initial)
        value = self.complete()
        self.assertFalse(value['execution_quotes']['000001']['buy_eligible'])
        self.assertTrue(value['execution_quotes']['000002']['buy_eligible'])

    def test_source_revision_and_new_account_never_reuse_old_complete_batch(self):
        old = self.complete()
        self.frame['execution_quotes']['207940'] = {'price': '1428000'}
        self.assertEqual(context(self.service, self.policy)['research_observation']['status'], 'missing')
        new_policy = {**self.policy, 'fingerprint': 'other'}
        self.store.save_setting('policy', new_policy)
        new = context(self.service, new_policy)
        self.assertNotEqual(new['research_observation']['id'], old['research_observation']['id'])
        self.assertEqual(new['execution_quotes'], {})

    def test_account_switch_during_network_does_not_commit_old_observation(self):
        scope = current_scope(self.service, self.policy)
        original = self.broker.quote
        def switched(symbol):
            value = original(symbol)
            self.store.save_setting('policy', {**self.policy, 'fingerprint': 'other'})
            return value
        self.broker.quote = switched
        observe(self.service, self.policy)
        batch = self.store.setting(PREFIX + scope['id'])
        self.assertEqual(batch['protection_quotes'], {})

    def test_same_account_limits_change_during_network_does_not_commit_old_scope(self):
        scope = current_scope(self.service, self.policy)
        original = self.broker.quote
        def changed(symbol):
            value = original(symbol)
            self.store.save_setting('policy', {**self.policy, 'daily_buy_limit': '900000'})
            return value
        self.broker.quote = changed
        observe(self.service, self.policy)
        self.assertEqual(self.store.setting(PREFIX + scope['id'])['protection_quotes'], {})
        self.assertIsNone(current_scope(self.service, self.policy))

    def test_transaction_rechecks_controls_and_active_revision_before_commit(self):
        scope = current_scope(self.service, self.policy)
        self.store.save_setting('policy', {**self.policy, 'order_cap': '1'})
        with self.assertRaisesRegex(ValueError, 'scope_conflict'):
            _save(self.store, scope)
        self.store.save_setting('policy', self.policy)
        self.store.save_setting('daily_policy:fixture', {'revision': 4})
        with self.assertRaisesRegex(ValueError, 'scope_conflict'):
            _save(self.store, scope)
        self.assertIsNone(self.store.setting(PREFIX + scope['id']))

    def test_engine_and_active_revision_changes_never_resume_old_complete_batch(self):
        old = self.complete()
        original = self.store.setting(PREFIX + old['research_observation']['id'])
        for change in ('learning', 'daily', 'active'):
            with self.subTest(change=change):
                if change == 'learning':
                    self.service.learning.version = 'learning-next'
                elif change == 'daily':
                    self.service.learning.daily.engine_version = 'daily-next'
                else:
                    self.store.save_setting('daily_policy:fixture', {'revision': 4})
                value = context(self.service, self.policy)
                self.assertEqual(value['research_observation']['status'], 'missing')
                self.assertEqual(value['execution_quotes'], {})
        self.assertEqual(self.store.setting(PREFIX + old['research_observation']['id']), original)

    def test_missing_mark_rotates_attempts_without_unlocking_execution(self):
        original = self.broker.quote
        def missing(symbol):
            value = original(symbol)
            if symbol == '000001':
                raise ValueError('missing price')
            return value
        self.broker.quote = missing
        with self.assertRaisesRegex(ValueError, 'missing price'):
            observe(self.service, self.policy)
        observe(self.service, self.policy)
        value = context(self.service, self.policy)['research_observation']
        self.assertEqual(value['protection_count'], 5)
        self.assertEqual(value['execution_count'], 0)
        self.assertEqual(value['status'], 'collecting_protection')
        self.assertFalse(any(kind == 'minute' for kind, _ in self.calls))

    def test_invalid_complete_batch_is_not_exposed_to_learning(self):
        value = self.complete()
        key = PREFIX + value['research_observation']['id']
        original = self.store.setting(key)
        cases = [None, {'status': 'complete'}, {**original, 'protection_quotes': None},
                 {**original, 'protection_quotes': {}},
                 {**original, 'protection_completed_at': '2026-10-21T09:00:00'}]
        for bad in cases:
            with self.subTest(value=bad):
                self.store.save_setting(key, bad)
                result = context(self.service, self.policy)
                self.assertEqual(result['research_observation']['status'], 'unavailable')
                self.assertEqual(result['execution_quotes'], {})
                self.assertEqual(result['protection_quotes'], {})

    def test_stale_future_naive_or_wrong_day_quotes_never_unlock_protection(self):
        for quote_time in ('2026-10-21T09:01:00+09:00', '2026-10-21T09:06:00+09:00',
                           '2026-10-21T09:05:00', '2026-10-20T09:05:00+09:00'):
            with self.subTest(quote_time=quote_time):
                self.quote_time = quote_time
                with self.assertRaisesRegex(ValueError, 'invalid_research_observation_time'):
                    observe(self.service, self.policy, budget=1)
                value = context(self.service, self.policy)['research_observation']
                self.assertEqual(value['protection_count'], 0)
                self.assertEqual(value['execution_count'], 0)

    def test_market_close_during_network_does_not_commit_quote(self):
        scope = current_scope(self.service, self.policy)
        original = self.broker.quote
        def closed(symbol):
            value = original(symbol)
            self.now = self.now.replace(hour=16)
            return {**value, 'as_of': self.now.isoformat()}
        self.broker.quote = closed
        observe(self.service, self.policy)
        self.assertEqual(self.store.setting(PREFIX + scope['id'])['protection_quotes'], {})

    def test_real_batch_freezes_learning_frame_and_all_four_paired_scenarios(self):
        from backend.learning import LearningService
        from backend.learning_policy import build_frame, portfolio_metrics
        from tests.test_learning_policy import ExecutionProfileTests, policy as rules
        symbols = ['000001', '207940', '003670']
        days, day = [], date(2026, 7, 1)
        while day <= date(2026, 10, 21):
            if day.weekday() < 5:
                days.append(day)
            day += timedelta(days=1)
        histories = {symbol: {day: {'open': 10000, 'high': 10100, 'low': 9900,
            'close': 10000, 'volume': 1000, 'turnover': 10000000} for day in days} for symbol in symbols}
        def data(day):
            return {'as_of': day, 'observed_at': day + 'T16:05:00+09:00',
                'rows': [{'symbol': symbol, 'board': 'KOSPI', 'learning_eligible': symbol == '000001'}
                         for symbol in symbols], 'histories': histories,
                'calendars': {'KOSPI': days}, 'benchmarks': {'KOSPI': {d: 1000 for d in days}}}
        self.frame = build_frame(data('2026-10-20'))
        self.frame.update(captured_at='2026-10-20T16:10:00+09:00',
            execution_quotes={symbol: {'price': '10000', 'eligible': True,
                'observed_at': '2026-10-20T09:05:00+09:00'} for symbol in symbols})
        with self.store.connect() as db:
            db.execute('INSERT INTO learning_frames VALUES (?,?,?)',
                       ('2026-10-20', 'fixture', encode(self.frame)))
        positions = [{'symbol': symbol, 'strategy_id': 'old', 'quantity': 1, 'cost': '10000',
            'entry_price': 10000, 'last_close': 10000, 'held_sessions': 1,
            'exit_policy': {'holding_sessions': 2, 'stop_loss_pct': 0, 'take_profit_pct': 0}}
            for symbol in symbols[1:]]
        original_book = {'as_of': '2026-10-20', 'cash': '980000', 'equity': '1000000', 'positions': positions,
            'account_protection': {'units': '1000000', 'peak': '1', 'active': False, 'limit_pct': 15}}
        self.live = deepcopy(original_book)
        model = ExecutionProfileTests().profile()
        learner = LearningService(self.store, enabled=True, daily_enabled=True, now=lambda: self.now,
            proposer=Mock(side_effect=AssertionError('no strategy decision required')))
        self.service.learning = learner
        batch = self.complete()
        prior_raw = self.store.setting(PREFIX + batch['research_observation']['id'])
        self.now = datetime.fromisoformat('2026-10-21T16:05:00+09:00')
        book = {**deepcopy(original_book), 'as_of': '2026-10-21'}
        limits = {'fingerprint': 'fixture', 'budget': '1000000', 'order_cap': '300000',
                  'daily_buy_limit': '500000'}
        learner.process(data('2026-10-21'), [], limits,
                        context={**batch, 'initial_state': book, 'execution_model': model})
        frames = learner.frames()
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[-1]['protection_quotes'], batch['protection_quotes'])
        self.assertEqual(frames[-1]['execution_quotes'], batch['execution_quotes'])
        self.assertEqual(frames[-1]['research_observation'], batch['research_observation'])
        self.assertEqual(learner.daily.history(), [])  # First compatible anchor only.
        result = portfolio_metrics(rules(), frames, limits, initial_state=original_book, execution_model=model)
        self.assertTrue(result['valid'], result)
        self.assertEqual(set(result['scenario_results']), {'empirical', 'partial_fill', 'delayed_sell', 'adverse_buy'})
        for scenario in result['scenario_results'].values():
            self.assertTrue(scenario['valid'], scenario)
            self.assertEqual(len(scenario['equity_curve']), 2)
            self.assertEqual(len(scenario['stress_equity_curve']), 2)
            self.assertTrue(scenario['final_state']['account_protection'])
            self.assertTrue(scenario['stress_final_state']['account_protection'])
        # A copied old ordering or missing descendant must still fail closed.
        for failure, symbol in [('protection_quote_after_decision', '000001'),
                                ('protection_quote_missing', '207940')]:
            broken = deepcopy(frames)
            if failure.endswith('after_decision'):
                broken[-1]['execution_quotes'][symbol]['observed_at'] = '2026-10-21T09:00:00+09:00'
            else:
                del broken[-1]['protection_quotes'][symbol]
            failed = portfolio_metrics(rules(), broken, limits, initial_state=original_book, execution_model=model)
            self.assertFalse(failed['valid'])
            self.assertEqual(failed['error'], failure)
        self.assertEqual(self.store.setting(PREFIX + batch['research_observation']['id']), prior_raw)
        self.assertEqual(self.store.setting('learning_quotes'), self.live_quotes)
        self.assertEqual(self.store.orders(), [])

    def test_daily_runtime_dispatch_never_calls_legacy_first_quote_collector(self):
        self.service._observe_legacy_learning_quotes = Mock(side_effect=AssertionError('legacy collector'))
        from backend.experiments import ALL_STRATEGIES
        control = {**self.policy, 'execution_strategy': ALL_STRATEGIES}
        with patch('backend.research_observation.observe') as collected:
            ExperimentService._observe_learning_quotes(self.service, control)
        collected.assert_called_once_with(self.service, control)


if __name__ == '__main__':
    unittest.main()
