"""New research may recover after absent observations; old evidence never moves."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.daily_comparison import ensure_tables, evaluation_context, freeze_context, update_followups
from backend.daily_evaluation import ANCHOR_SELECTION_VERSION, _evaluation_window, evaluate_daily_candidate
from backend.experiment_store import ExperimentStore, encode
from backend.learning import LearningService
from backend.learning_policy import BASELINE_POLICY, build_frame, portfolio_metrics
from tests.test_learning import raw_input
from tests.test_learning_policy import LIMITS, frames, policy


def book(day, cash='0', units='10000'):
    return {'as_of': day, 'cash': cash, 'equity': str(int(cash) + 10000), 'positions': [{
        'symbol': '005930', 'strategy_id': 'momentum', 'quantity': 1,
        'cost': '10000', 'entry_price': 10000, 'last_close': 10000, 'held_sessions': 1,
        'exit_policy': {'holding_sessions': 20, 'stop_loss_pct': 0, 'take_profit_pct': 0}}],
        'account_protection': {'units': units, 'peak': '1.02', 'active': False, 'limit_pct': 15}}


def quotes(day):
    return {'protection_quotes': {'005930': {'price': '10000', 'observed_at': day + 'T09:05:00+09:00'}},
            'execution_quotes': {'005930': {'price': '10000', 'observed_at': day + 'T09:06:00+09:00',
                                           'eligible': True, 'buy_eligible': True}}}


def candidate():
    value = policy()
    value['strategies'][0]['holding_sessions'] = 4
    return {'policy': value}


class AnchorRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ExperimentStore(Path(temporary.name) / 'fixture.sqlite3')
        ensure_tables(self.store)
        self.frames = frames(8, price=10000)
        for frame in self.frames:
            frame.update(quotes(frame['as_of']))
            frame['rows'][0]['learning_eligible'] = False
        self.version = 'current-engine'
        for frame in self.frames:
            freeze_context(self.store, 'account', frame, book(frame['as_of']),
                           {'mode': 'observed_quotes'}, LIMITS, frame['observed_at'], self.version)

    def select(self, values=None, **changes):
        return evaluation_context(self.store, changes.get('fingerprint', 'account'),
            values if values is not None else self.frames, changes.get('limits', LIMITS),
            changes.get('version', self.version))

    def evaluate(self, values, context):
        return evaluate_daily_candidate(policy(), candidate(), values, LIMITS, evaluation_context=context)

    def test_gap_recovers_on_three_future_days_with_same_frozen_lot_and_protection(self):
        for stop in (4, 5, 6):
            with self.subTest(stop=stop):
                recorded = self.frames[:1] + self.frames[2:stop]
                context = self.select(recorded)
                self.assertEqual(context['initial_state'], book(self.frames[2]['as_of']))
                audit = context['anchor_selection']
                self.assertEqual(audit['version'], ANCHOR_SELECTION_VERSION)
                self.assertEqual(audit['original_anchor'], self.frames[0]['as_of'])
                self.assertEqual(audit['missing_sessions'], [self.frames[1]['as_of']])
                self.assertEqual(audit['excluded_observed_range'], [self.frames[0]['as_of'], self.frames[0]['as_of']])
                self.assertEqual(audit['frame_days'], [frame['as_of'] for frame in self.frames[2:stop]])
                historical = deepcopy(self.frames[:stop])
                historical[1].update(role='development_only', protection_quotes={}, execution_quotes={})
                result = self.evaluate(historical, context)
                self.assertEqual(result['decision'], 'keep')
                self.assertEqual(result['metrics']['context_anchor'], self.frames[2]['as_of'])
                self.assertTrue(result['metrics']['account_protection_inherited'])
                selected, options, _ = _evaluation_window(historical, context)
                replay = portfolio_metrics(policy(), selected, LIMITS, **options)
                self.assertTrue(replay['valid'])
                self.assertEqual(context['initial_state']['positions'][0]['exit_policy']['holding_sessions'], 20)
        self.assertEqual(self.store.orders(), [])

    def test_second_gap_requires_new_anchor_and_future_observation(self):
        self.assertIsNone(self.select(self.frames[:1] + self.frames[2:3]))
        self.assertIsNone(self.select(self.frames[:1] + self.frames[2:4] + self.frames[5:6]))
        found = self.select(self.frames[:1] + self.frames[2:4] + self.frames[5:7])
        self.assertEqual(found['initial_state']['as_of'], self.frames[5]['as_of'])
        self.assertEqual(found['anchor_selection']['missing_sessions'],
                         [self.frames[index]['as_of'] for index in (1, 4)])

    def test_weekends_and_known_holidays_are_not_missing_sessions(self):
        values = deepcopy(self.frames)
        holiday = values[1]['as_of']
        values.pop(1)
        for frame in values:
            frame['session_days'] = [day for day in frame['session_days'] if day != holiday]
        found = self.select(values)
        self.assertEqual(found['initial_state']['as_of'], values[0]['as_of'])
        self.assertEqual(found['anchor_selection']['missing_sessions'], [])

    def test_calendar_union_retains_gap_when_later_calendars_are_truncated(self):
        values = deepcopy(self.frames[:1] + self.frames[2:6])
        for index in range(2, len(values)):
            values[index]['session_days'] = [values[index - 1]['as_of'], values[index]['as_of']]
        self.assertEqual(self.select(values)['initial_state']['as_of'], self.frames[2]['as_of'])
        for calendar in (None, [], ['invalid'], [values[-1]['as_of'], '2099-01-01'],
                         list(reversed(values[-1]['session_days'])), values[-1]['session_days'] * 2):
            altered = deepcopy(values)
            altered[-1]['session_days'] = calendar
            self.assertIsNone(self.select(altered))

    def test_unknown_or_conflicting_calendars_cannot_create_recovery(self):
        values = deepcopy(self.frames[:1] + self.frames[2:5])
        for frame in values:
            frame['session_days'] = [frame['as_of']]
        self.assertIsNone(self.select(values))
        values = deepcopy(self.frames[:5])
        values[-1]['session_days'].remove(values[1]['as_of'])
        self.assertIsNone(self.select(values))
        # If every source omits the same unobserved day, its existence is
        # unknown. Never infer a gap or move the anchor from that absence.
        values = deepcopy(self.frames[:1] + self.frames[2:5])
        for frame in values:
            frame['session_days'] = [day for day in frame['session_days'] if day != self.frames[1]['as_of']]
        found = self.select(values)
        self.assertEqual(found['initial_state']['as_of'], self.frames[0]['as_of'])
        self.assertEqual(found['anchor_selection']['missing_sessions'], [])

    def test_no_compatible_book_waits_without_cash_or_current_book_fallback(self):
        recorded = self.frames[:1] + self.frames[2:4]
        self.assertIsNone(self.select(recorded, version='new-engine'))
        self.assertIsNone(self.select(recorded, fingerprint='other-account'))
        for key in LIMITS:
            self.assertIsNone(self.select(recorded, limits={**LIMITS, key: '20000'}))
        with self.store.connect() as db:
            db.execute('DELETE FROM daily_policy_contexts WHERE day=?', (self.frames[2]['as_of'],))
        self.assertIsNone(self.select(recorded))  # Today's book has no following session.

    def test_late_or_unsettled_book_never_becomes_recovery_anchor(self):
        recorded = self.frames[:1] + self.frames[2:5]
        for changes in ({'observed_at': recorded[-1]['as_of'] + 'T09:00:00+09:00'},
                        {'initial_state': {**book(self.frames[2]['as_of']), 'pending_orders': ['pending']}},
                        {'initial_state': book(self.frames[3]['as_of'])}):
            with self.store.connect() as db:
                payload = json.loads(db.execute('SELECT payload FROM daily_policy_contexts WHERE day=?',
                                                (self.frames[2]['as_of'],)).fetchone()[0])
                payload.update(changes)
                db.execute('UPDATE daily_policy_contexts SET payload=? WHERE day=?',
                           (encode(payload), self.frames[2]['as_of']))
            self.assertIsNone(self.select(recorded))

    def test_bad_recorded_quote_does_not_trigger_another_anchor_search(self):
        values = deepcopy(self.frames[:4])
        values[1]['protection_quotes'] = {}
        context = self.select(values)
        self.assertEqual(context['initial_state']['as_of'], values[0]['as_of'])
        self.assertEqual(self.evaluate(values, context)['metrics']['error'], 'protection_quote_missing')
        for frame in values:
            frame['rows'][0]['close'] *= 2
        with patch('backend.daily_comparison.portfolio_metrics', side_effect=AssertionError('selection cannot replay')):
            changed = self.select(values)
            self.assertEqual(changed['initial_state'], context['initial_state'])
            self.assertEqual(changed['anchor_selection']['selected_anchor'], context['anchor_selection']['selected_anchor'])
            self.assertNotEqual(changed['anchor_selection']['frames_hash'], context['anchor_selection']['frames_hash'])

    def test_development_rows_cannot_replace_recorded_observations(self):
        values = deepcopy(self.frames[:4])
        values[1]['role'] = 'development_only'
        self.assertIsNone(self.select(values))
        values[-1]['role'] = 'development_only'
        self.assertIsNone(self.select(values))

    def test_selected_dates_are_bound_and_legacy_signal_requirements_stay_strict(self):
        context = self.select(self.frames[:4])
        shortened = self.frames[:2] + self.frames[3:4]
        self.assertEqual(self.evaluate(shortened, context)['metrics']['error'], 'invalid_anchor_selection')
        changed = deepcopy(self.frames[:4])
        changed[1]['rows'][0]['close'] += 1
        self.assertEqual(self.evaluate(changed, context)['metrics']['error'], 'invalid_anchor_selection')
        altered = deepcopy(context)
        altered['initial_state']['cash'] = '1'
        self.assertEqual(self.evaluate(self.frames[:4], altered)['metrics']['error'], 'invalid_anchor_selection')
        result = evaluate_daily_candidate(BASELINE_POLICY, candidate(), self.frames[:4], LIMITS,
                                           evaluation_context=context)
        self.assertEqual(result['metrics']['error'], 'insufficient_observed_baseline')

    def test_followup_seed_is_not_moved_to_recovered_research_anchor(self):
        day = self.frames[0]['as_of']
        record = {'id': 'original', 'fingerprint': 'account', 'engine_version': self.version,
                  'effective_day': self.frames[1]['as_of'],
                  'adopted_at': self.frames[1]['as_of'] + 'T09:05:00+09:00',
                  'comparison_seed': {'signal_day': day, 'observed_at': self.frames[0]['observed_at'],
                      'initial_state': book(day), 'execution_model': {'mode': 'observed_quotes'},
                      'limits': LIMITS, 'parent_policy': policy(), 'candidate_policy': candidate()['policy']}}
        with self.store.connect() as db:
            db.execute('INSERT INTO daily_policy_followups VALUES (?,?,?)', ('original', 'account', encode(record)))
        values = self.frames[:1] + self.frames[2:4]
        self.assertEqual(self.select(values)['initial_state']['as_of'], self.frames[2]['as_of'])
        update_followups(self.store, 'account', values, self.version)
        with self.store.connect() as db:
            saved = json.loads(db.execute('SELECT payload FROM daily_policy_followups').fetchone()[0])
        self.assertEqual(saved['comparison_seed'], record['comparison_seed'])
        self.assertEqual(saved['comparison']['status'], 'unavailable')


class LearningAnchorRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ExperimentStore(Path(temporary.name) / 'learning.sqlite3')
        self.store.save_settings({'policy': {**LIMITS, 'fingerprint': 'account'},
                                 'enabled': False, 'user_paused': True,
                                 'learning': {'champion': {'policy': policy()}}})
        self.index = 0
        self.now = datetime.fromisoformat(self.data(0)['observed_at'])
        self.learner = self.new_learner()

    def data(self, index):
        value = raw_input(index)
        value['rows'][0]['learning_eligible'] = False
        for bar in value['histories']['005930'].values():
            bar.update(open=10000, high=10001, low=9999, close=10000, volume=1000, turnover=10000000)
        return value

    def development(self, data):
        return [{**build_frame(self.data(index)), 'role': 'development_only'} for index in range(self.index + 1)]

    def new_learner(self):
        value = LearningService(self.store, enabled=True, daily_enabled=True, now=lambda: self.now,
                                proposer=lambda *args, **kwargs: candidate(), development_reader=self.development)
        self.addCleanup(value.close)
        return value

    def process(self, index, *, ready=True, changes=None):
        self.index = index
        data = self.data(index)
        self.now = datetime.fromisoformat(data['observed_at'])
        context = {'initial_state': book(data['as_of']), 'execution_model': {'mode': 'observed_quotes'},
                   'research_observation': {'status': 'complete' if ready else 'missing'}, **quotes(data['as_of'])}
        context.update(changes or {})
        return self.learner.process(data, [], LIMITS, context=context)

    def test_real_readiness_gap_recovers_after_restart_and_freezes_decisions(self):
        self.process(0)
        original_scorecard = {key: deepcopy(self.learner.state()[key]) for key in
                              ('scorecard_start', 'scorecard_capital', 'scorecard_limits', 'scorecard_engine')}
        self.process(1, ready=False)
        self.process(2)
        self.assertEqual(self.learner.daily.history(), [])
        self.learner.close()
        self.learner = self.new_learner()
        for index in (3, 4, 5):
            self.process(index)
            record = self.learner.daily.history()[0]
            self.assertEqual(record['decision'], 'keep')
            self.assertEqual(record['evaluation_context']['initial_state']['as_of'], self.data(2)['as_of'])
            self.assertEqual(record['evaluation_context']['anchor_selection']['original_anchor'], self.data(0)['as_of'])
            with self.store.connect() as db:
                before = [tuple(row) for row in db.execute('SELECT * FROM daily_policy_decisions ORDER BY day')]
            self.process(index)
            with self.store.connect() as db:
                after = [tuple(row) for row in db.execute('SELECT * FROM daily_policy_decisions ORDER BY day')]
            self.assertEqual(after, before)
        self.assertEqual(len(self.learner.daily.history()), 3)
        self.assertEqual({key: self.learner.state()[key] for key in original_scorecard}, original_scorecard)
        self.assertEqual(self.learner.state()['scorecard']['status'], 'invalid')
        self.assertEqual(self.store.orders(), [])
        self.assertFalse(self.store.setting('enabled'))
        self.assertTrue(self.store.setting('user_paused'))

    def test_missing_production_context_waits_without_cash_evaluation(self):
        with patch.object(self.learner.daily, 'process') as evaluate:
            self.process(0, changes={'research_observation': None})
        evaluate.assert_not_called()
        self.assertEqual(self.learner.daily.history(), [])
        self.assertEqual(self.learner.daily.snapshot()['status'], 'waiting')

    def test_error_retry_preserves_anchor_audit_and_does_not_reprompt(self):
        self.process(0)
        self.process(1, ready=False)
        self.process(2)
        proposer = Mock(return_value=candidate())
        self.learner.proposer = proposer
        with patch('backend.daily_evaluation.select_daily_candidate', side_effect=RuntimeError('evaluation interrupted')):
            with self.assertRaises(RuntimeError):
                self.process(3)
        failed = self.learner.daily.history()[0]
        self.assertEqual(failed['decision'], 'error')
        self.learner.close()
        self.learner = self.new_learner()
        self.learner.proposer = proposer
        self.process(3)
        completed = self.learner.daily.history()[0]
        self.assertEqual(completed['decision'], 'keep')
        for key in ('evaluation_context', 'research_input_hash', 'research_request_hash', 'comparison_seed'):
            self.assertEqual(completed[key], failed[key])
        proposer.assert_called_once()

    def test_recovery_does_not_rewrite_a_prior_blocked_decision(self):
        self.process(0)
        self.process(1, changes={'protection_quotes': {}})
        blocked = self.learner.daily.history()[0]
        self.assertEqual(blocked['decision'], 'blocked')
        with self.store.connect() as db:
            original = db.execute('SELECT payload FROM daily_policy_decisions WHERE id=?', (blocked['id'],)).fetchone()[0]
        self.process(2, ready=False)
        self.process(3)
        self.process(4)
        self.assertEqual(self.learner.daily.history()[0]['decision'], 'keep')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT payload FROM daily_policy_decisions WHERE id=?',
                                       (blocked['id'],)).fetchone()[0], original)


if __name__ == '__main__':
    unittest.main()
