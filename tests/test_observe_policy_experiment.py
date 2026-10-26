"""Synthetic continuity and fail-closed checks, not market performance evidence."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.daily_inputs import freeze_award_context
from scripts.compare_signal_holding import candidates, canonical, prepare
from scripts.observe_policy_experiment import advance, save_artifact, seal
from tests.test_compare_signal_holding import incumbent
from tests.test_learning_policy import LIMITS, frames, row


def fixture(count=7):
    data = frames(count)
    for index, frame in enumerate(data):
        price = 100 + index
        frame['rows'] = [row(price=price)]
        frame['execution_quotes'] = {'005930': {'price': price, 'eligible': True, 'buy_eligible': True,
            'observed_at': frame['as_of'] + 'T09:10:00+09:00'}}
        frame['protection_quotes'] = {'005930': {'price': price,
            'observed_at': frame['as_of'] + 'T09:05:00+09:00'}}
    book = {'as_of': data[0]['as_of'], 'cash': '10000', 'reserved_cash': '0', 'positions': [],
        'pending_orders': [], 'unresolved_orders': [],
        'account_protection': {'active': False, 'limit_pct': 15, 'peak': '1', 'units': '10000'}}
    context = freeze_award_context(signal_day=data[0]['as_of'], observed_at=data[0]['observed_at'],
        latest_frame=data[0], initial_state=book, limits=LIMITS, execution_model={}, competition_context=None)
    hypotheses, _ = candidates(incumbent(), [data[0]])
    request = {'context': context, 'champion': incumbent(), 'candidates': hypotheses,
               'limits': deepcopy(LIMITS), 'recorded_engine_versions': {'latest': 'observed-engine'}}
    return request, data


def observation(data):
    return {'learning_frames': [{'payload': deepcopy(frame)} for frame in data],
            'daily_policy_contexts': [{'payload': {'engine_version': 'observed-engine',
                'limits': deepcopy(LIMITS), 'initial_state': {'as_of': frame['as_of'], 'cash': '123'}}}
                for frame in data]}


def instant(frame):
    return datetime.fromisoformat(frame['as_of'] + 'T17:00:00+09:00')


def reseal_book(request, book):
    context = request['context']
    request['context'] = freeze_award_context(**{**{key: context[key] for key in
        ('signal_day', 'observed_at', 'latest_frame', 'execution_model', 'competition_context', 'limits')},
        'initial_state': book})


class ContinuousPolicyExperimentTests(unittest.TestCase):
    def test_old_cursor_retry_reuses_identical_artifact_and_never_overwrites(self):
        request, data = fixture(2)
        experiment, state = seal(request, now=instant(data[0]))
        result = advance(experiment, state, [observation([data[1]])], now=instant(data[1]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ('state-' + result['hash'] + '.json.gz')
            first = save_artifact(path, result)
            original = path.read_bytes()
            retry = advance(experiment, state, [observation([data[1]])], now=instant(data[1]))
            self.assertEqual(save_artifact(path, retry), first)
            self.assertEqual(path.read_bytes(), original)
            with self.assertRaisesRegex(ValueError, 'existing_artifact_differs'):
                save_artifact(path, state)
            self.assertEqual(path.read_bytes(), original)

    def test_prepare_rejects_real_session_gap_before_candidate_results(self):
        request, data = fixture(3)
        frozen = {'research_inputs': {'champion': incumbent(), 'limits': LIMITS, 'frames': [data[0]]},
                  'evaluation_context': {**request['context'], 'engine_version': 'observed-engine'}}
        latest = observation([data[2]])
        context = latest['daily_policy_contexts'][0]['payload']
        context.update(observed_at=data[2]['observed_at'], execution_model={})
        context['initial_state'] = {**deepcopy(request['context']['initial_state']), 'as_of': data[2]['as_of']}
        latest.update(runs=[{'payload': {'learning_policy': incumbent()}}],
                      daily_research_preparation_observations=[{'payload': {'competition_context': None}}])
        with self.assertRaisesRegex(ValueError, 'missing_observed_sessions'):
            prepare(frozen, latest)
        latest['learning_frames'][0]['payload'] = data[1]
        context['initial_state']['as_of'] = data[1]['as_of']
        context['observed_at'] = data[1]['observed_at']
        self.assertEqual(len(prepare(frozen, latest)['observed_lane']['frames']), 2)

    def test_batch_equals_daily_replay_with_restart_and_duplicate_retry(self):
        request, data = fixture()
        experiment, initial = seal(request, now=instant(data[0]))
        original = deepcopy((request, experiment, initial, data))
        self.assertEqual(initial['intervals'], 0)
        self.assertEqual(initial['results'], {})
        batch = advance(experiment, initial, [observation(data[1:])], now=instant(data[-1]))
        current = initial
        for frame in data[1:]:
            current = advance(experiment, current, [observation([frame])], now=instant(frame))
            current = json.loads(canonical(current))
            retried = advance(experiment, current, [observation([frame])], now=instant(frame))
            self.assertEqual(current, retried)
        self.assertEqual(batch, current)
        self.assertEqual(batch['intervals'], len(data) - 1)
        self.assertEqual((request, experiment, initial, data), original)
        full = batch['results']['current_hold2']['full']
        partial = batch['results']['current_hold2']['partial_delayed']['normal']
        self.assertNotEqual(full['normal']['final_state'], full['cost_stress']['final_state'])
        self.assertGreater(partial['diagnostics']['scenario_delayed_sells'], 0)
        self.assertGreater(partial['diagnostics']['partial_orders'], 0)
        self.assertNotEqual(batch['results']['current_hold1']['full']['normal']['final_state'],
                            batch['results']['current_hold4']['full']['normal']['final_state'])

    def test_later_actual_book_never_resets_shadow_cash_lots_or_protection(self):
        request, data = fixture(3)
        experiment, state = seal(request, now=instant(data[0]))
        first = advance(experiment, state, [observation([data[1]])], now=instant(data[1]))
        source = observation([data[2]])
        reference = advance(experiment, first, [source], now=instant(data[2]))
        source['daily_policy_contexts'][0]['payload']['initial_state'].update(cash='999999999',
            positions=[{'symbol': '000660', 'quantity': 100000}], account_protection={'active': True})
        self.assertEqual(advance(experiment, first, [source], now=instant(data[2])), reference)

    def test_original_lot_exit_rule_survives_all_candidate_holding_rules(self):
        request, data = fixture(5)
        for frame in data:
            frame['rows'][0]['learning_eligible'] = False
        book = deepcopy(request['context']['initial_state'])
        book.update(cash='9000', positions=[{'symbol': '005930', 'strategy_id': 'momentum', 'quantity': 10,
            'entry_price': '100', 'last_close': '100', 'cost': '1000', 'held_sessions': 1,
            'exit_policy': {'holding_sessions': 3, 'stop_loss_pct': 0, 'take_profit_pct': 0}}])
        request['context']['latest_frame'] = data[0]
        reseal_book(request, book)
        experiment, state = seal(request, now=instant(data[0]))
        result = advance(experiment, state, [observation(data[1:])], now=instant(data[-1]))
        curves = [result['results'][f'current_hold{hold}']['full']['normal']['equity_curve'] for hold in (1, 2, 4)]
        self.assertEqual(curves[0], curves[1])
        self.assertEqual(curves[1], curves[2])
        self.assertEqual(result['results']['current_hold1']['full']['normal']['trading_activity']['sell_fill_events'], 1)

    def test_protection_and_unsold_exit_intent_persist_over_restart(self):
        request, data = fixture(4)
        book = deepcopy(request['context']['initial_state'])
        book.update(cash='1000', positions=[{'symbol': '005930', 'strategy_id': 'momentum', 'quantity': 90,
            'entry_price': '100', 'last_close': '100', 'cost': '9000', 'held_sessions': 0,
            'exit_policy': {'holding_sessions': 4, 'stop_loss_pct': 0, 'take_profit_pct': 0}}])
        reseal_book(request, book)
        for frame in data[1:]:
            frame['rows'] = [row(price=70)]
            frame['execution_quotes']['005930']['price'] = 70
            frame['protection_quotes']['005930']['price'] = 70
        experiment, state = seal(request, now=instant(data[0]))
        first = advance(experiment, state, [observation([data[1]])], now=instant(data[1]))
        partial = first['results']['current_hold2']['partial_delayed']['normal']['final_state']
        self.assertTrue(partial['account_protection']['active'])
        self.assertTrue(partial['positions'][0]['exit_pending'])
        self.assertEqual(partial['positions'][0]['quantity'], 90)
        restarted = json.loads(canonical(first))
        continued = advance(experiment, restarted, [observation(data[2:])], now=instant(data[-1]))
        self.assertEqual(continued, advance(experiment, state, [observation(data[1:])], now=instant(data[-1])))
        for environments in continued['results']['current_hold2'].values():
            for value in environments.values():
                self.assertTrue(value['final_state']['account_protection']['active'])
                self.assertEqual(value['trading_activity']['buy_fill_events'], 0)

    def test_gap_rejects_whole_update_and_keeps_original_anchor(self):
        request, data = fixture(4)
        experiment, state = seal(request, now=instant(data[0]))
        original = deepcopy(state)
        with self.assertRaisesRegex(ValueError, 'missing_observed_sessions'):
            advance(experiment, state, [observation([data[1], data[3]])], now=instant(data[-1]))
        self.assertEqual(state, original)
        self.assertEqual(state['frames'][0], experiment['anchor_frame'])

    def test_missing_quote_blocks_but_observed_untradeability_remains_unfilled(self):
        request, data = fixture(2)
        experiment, state = seal(request, now=instant(data[0]))
        missing = deepcopy(data[1])
        missing['execution_quotes'] = {}
        with self.assertRaisesRegex(ValueError, 'invalid_shadow_replay:.*execution_quote_missing'):
            advance(experiment, state, [observation([missing])], now=instant(data[1]))
        data[1]['execution_quotes']['005930']['eligible'] = False
        result = advance(experiment, state, [observation([data[1]])], now=instant(data[1]))
        value = result['results']['current_hold2']['full']['normal']
        self.assertEqual(value['trading_activity']['buy_fill_events'], 0)
        self.assertGreater(value['diagnostics']['unfilled_orders'], 0)

    def test_changed_policy_book_cost_limits_or_state_fail_integrity_checks(self):
        request, data = fixture(2)
        experiment, state = seal(request, now=instant(data[0]))
        for field in ('policies', 'initial_state', 'execution_models', 'limits', 'sources'):
            changed = deepcopy(experiment)
            changed[field] = {}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'experiment_hash_changed'):
                advance(changed, state, [observation([data[1]])], now=instant(data[1]))
        changed = deepcopy(state)
        changed['frames'][0]['rows'][0]['close'] = 1
        with self.assertRaisesRegex(ValueError, 'state_hash_changed'):
            advance(experiment, changed, [observation([data[1]])], now=instant(data[1]))

    def test_changed_replay_or_observation_engine_stops_continuation(self):
        request, data = fixture(2)
        experiment, state = seal(request, now=instant(data[0]))
        with patch('scripts.observe_policy_experiment.sources', return_value={'engine': 'changed'}):
            with self.assertRaisesRegex(ValueError, 'experiment_engine_or_tools_changed'):
                advance(experiment, state, [observation([data[1]])], now=instant(data[1]))
        source = observation([data[1]])
        source['daily_policy_contexts'][0]['payload']['engine_version'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'observation_engine_or_limits_changed'):
            advance(experiment, state, [source], now=instant(data[1]))

    def test_corrections_to_recorded_prices_are_rejected_even_on_retry(self):
        request, data = fixture(2)
        experiment, state = seal(request, now=instant(data[0]))
        current = advance(experiment, state, [observation([data[1]])], now=instant(data[1]))
        data[1]['execution_quotes']['005930']['price'] += 1
        with self.assertRaisesRegex(ValueError, 'recorded_observation_changed'):
            advance(experiment, current, [observation([data[1]])], now=instant(data[1]))

    def test_cannot_backdate_seal_or_consume_future_or_incomplete_frames(self):
        request, data = fixture(2)
        with self.assertRaisesRegex(ValueError, 'seal_requires_current_completed_session'):
            seal(request, now=instant(data[1]))
        experiment, state = seal(request, now=instant(data[0]))
        with self.assertRaisesRegex(ValueError, 'future_observation'):
            advance(experiment, state, [observation([data[1]])], now=instant(data[0]))
        data[1]['observed_at'] = data[1]['as_of'] + 'T15:59:00+09:00'
        with self.assertRaisesRegex(ValueError, 'incomplete_observed_session'):
            advance(experiment, state, [observation([data[1]])], now=instant(data[1]))

    def test_limits_and_risk_controls_cannot_change_when_sealing(self):
        request, data = fixture(2)
        request['candidates']['current_hold4']['cash_reserve'] = .3
        with self.assertRaisesRegex(ValueError, 'experiment_policy_controls_changed'):
            seal(request, now=instant(data[0]))


if __name__ == '__main__':
    unittest.main()
