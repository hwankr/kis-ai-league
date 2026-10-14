"""Offline immutable selection inputs and preparation/activation linkage."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.daily_inputs import (freeze_award_context, freeze_research_inputs,
                                 validate_award_context, validate_research_inputs)
from backend.daily_policy import _hash
from backend.experiment_store import encode
from backend.experiments import ExperimentService
from backend.learning_proposals import proposal_context, proposal_input_preimage
from tests import test_daily_policy as fixtures


class AwardInputContractTests(unittest.TestCase):
    setUp = fixtures.DailyPolicyTests.setUp
    make_service = fixtures.DailyPolicyTests.make_service
    process = fixtures.DailyPolicyTests.process
    prepared_run = fixtures.DailyPolicyTests.prepared_run
    next_session = fixtures.DailyPolicyTests.next_session
    allow_orders = fixtures.DailyPolicyTests.allow_orders
    preparations = fixtures.DailyPolicyTests.preparations

    def context_value(self, competition=None):
        return freeze_award_context(signal_day=self.data['as_of'], observed_at=self.current.isoformat(),
            latest_frame=self.frames[-1], initial_state=self.context['initial_state'],
            execution_model={'mode': 'daily_open'}, competition_context=competition,
            limits={key: self.limits[key] for key in ('budget', 'order_cap', 'daily_buy_limit')})

    def select(self, *args, **kwargs):
        return deepcopy(args[1]), deepcopy(self.result)

    def write_record(self, record):
        with self.store.connect() as db:
            db.execute('UPDATE daily_policy_decisions SET payload=? WHERE id=?', (encode(record), record['id']))

    def test_contract_and_complete_preimage_are_independent_copies_with_exact_hashes(self):
        context = self.context_value({'status': 'unavailable'})
        self.assertTrue(validate_award_context(context))
        self.assertIsInstance(context['competition_context'], dict)
        request = freeze_research_inputs(self.parent, self.frames, context['limits'], [{'decision': 'keep'}], context)
        self.assertTrue(validate_research_inputs(request))
        self.frames[0]['benchmark_prices']['KOSPI'] += 1
        self.context['initial_state']['cash'] = '1'
        context['initial_state']['cash'] = '2'
        self.assertTrue(validate_research_inputs(request))
        self.assertFalse(validate_award_context(context))
        request['history'][0]['decision'] = 'apply'
        self.assertFalse(validate_research_inputs(request))

    def test_unknown_competition_is_explicit_and_non_contest_is_none(self):
        self.assertIsNone(self.context_value()['competition_context'])
        unknown = self.context_value({'status': 'unavailable'})['competition_context']
        self.assertFalse(unknown['reliable'])
        self.assertEqual(unknown['pace']['status'], 'unavailable')

    def test_current_book_cannot_be_frozen_before_close_or_for_another_day(self):
        for change in ({'observed_at': '2026-10-12T15:59:59+09:00'},
                       {'signal_day': '2026-10-09'}, {'observed_at': '2026-10-12T16:30:00'}):
            value = self.context_value()
            value.update(change)
            value['hash'] = _hash({key: item for key, item in value.items() if key != 'hash'})
            self.assertFalse(validate_award_context(value))

    def test_request_is_saved_before_provider_and_same_context_reaches_selection(self):
        captured = {}
        def propose(parent, frames, limits, history, llm=None, *, award_context):
            row = self.service.history()[0]
            self.assertIsNone(row['proposal'])
            self.assertTrue(validate_research_inputs(row['research_inputs']))
            self.assertEqual(row['award_context'], award_context)
            captured['proposal'] = deepcopy(award_context)
            award_context['initial_state']['cash'] = '1'
            return {'policy': self.candidate, 'change_family': 'exit'}
        self.proposer = propose
        with patch('backend.daily_evaluation.select_daily_candidate', side_effect=self.select) as selected:
            self.process()
        row, pending = self.service.history()[0], self.service.pending()
        self.assertEqual(captured['proposal'], selected.call_args.kwargs['award_context'])
        self.assertEqual(row['award_context'], pending['award_context'])
        self.assertEqual(row['research_request_hash'], row['research_inputs']['hash'])
        self.assertEqual(row['comparison_seed']['initial_state'], row['award_context']['initial_state'])
        self.assertEqual(row['comparison_seed']['observed_at'], row['award_context']['observed_at'])
        self.assertEqual(row['award_context']['latest_frame'], row['research_inputs']['frames'][-1])
        self.assertNotIn('research_inputs', self.service.state()['last_decision'])
        self.assertNotIn('research_inputs', self.service.history(include_inputs=False)[0])
        self.assertEqual(self.service.get_decision(row['id']), row)

    def test_retry_reuses_first_book_competition_history_and_request_without_reprompting(self):
        self.context['competition_context'] = {'status': 'unavailable'}
        with patch('backend.daily_evaluation.select_daily_candidate', side_effect=RuntimeError('retry')):
            with self.assertRaisesRegex(RuntimeError, 'retry'):
                self.process()
        first = self.service.history()[0]
        self.current += timedelta(minutes=1)
        self.context['initial_state']['cash'] = '999'
        self.context['competition_context'] = None
        with patch('backend.daily_evaluation.select_daily_candidate', side_effect=self.select):
            self.service.process(self.data, self.frames, self.limits, [{'new': 'history'}],
                                 context=self.context, proposer=self.proposer)
        current = self.service.history()[0]
        for key in ('research_inputs', 'research_request_hash', 'award_context', 'award_context_hash', 'book_hash'):
            self.assertEqual(current[key], first[key])
        self.proposer.assert_called_once()

    def test_provider_failure_keeps_request_for_retry(self):
        self.proposer.side_effect = RuntimeError('provider unavailable')
        with self.assertRaisesRegex(RuntimeError, 'provider unavailable'):
            self.process()
        first = self.service.history()[0]
        self.assertIsNone(first['proposal'])
        self.context['initial_state']['cash'] = '2'
        self.proposer.side_effect = None
        with patch('backend.daily_evaluation.select_daily_candidate', side_effect=self.select):
            self.process()
        self.assertEqual(self.service.history()[0]['research_inputs'], first['research_inputs'])
        self.assertEqual(self.service.pending()['comparison_seed']['initial_state']['cash'], '1000000')

    def test_legacy_error_is_not_retrofitted_and_fixed_signature_callback_remains_supported(self):
        with patch('backend.daily_evaluation.select_daily_candidate', side_effect=RuntimeError('retry')):
            with self.assertRaises(RuntimeError):
                self.process()
        record = self.service.history()[0]
        for key in ('research_inputs', 'research_request_hash', 'award_context', 'award_context_hash'):
            record.pop(key)
        self.write_record(record)
        with patch('backend.daily_evaluation.select_daily_candidate', side_effect=self.select) as selected:
            self.process()
        self.assertNotIn('award_context', self.service.pending())
        self.assertNotIn('award_context', selected.call_args.kwargs)

    def test_new_custom_callback_without_keyword_still_freezes_selection_context(self):
        def legacy_callback(parent, frames, limits, history, llm=None):
            return {'policy': self.candidate}
        self.proposer = legacy_callback
        with patch('backend.daily_evaluation.select_daily_candidate', side_effect=self.select) as selected:
            self.process()
        self.assertTrue(validate_award_context(selected.call_args.kwargs['award_context']))

    def test_prepare_restart_activate_preserves_request_link_and_original_archive(self):
        with patch('backend.daily_evaluation.select_daily_candidate', side_effect=self.select):
            self.process()
        pending = self.service.pending()
        self.assertTrue(self.service.prepare(pending, self.prepared_run()))
        pending = self.service.pending()
        for key in ('award_context_hash', 'research_request_hash'):
            self.assertEqual(pending['prepared_run'][key], pending[key])
        archived = self.preparations()
        request = deepcopy(self.service.history()[0]['research_inputs'])
        restarted = self.make_service()
        self.allow_orders()
        self.next_session()
        self.assertTrue(restarted.activate(pending, effective_day='2026-10-13'))
        self.assertEqual(self.preparations(), archived)
        self.assertEqual(self.service.history()[0]['research_inputs'], request)
        self.assertEqual(self.store.runs()[0]['research_request_hash'], request['hash'])

    def test_changed_preimage_or_supplied_run_hash_cannot_prepare(self):
        with patch('backend.daily_evaluation.select_daily_candidate', side_effect=self.select):
            self.process()
        pending = self.service.pending()
        self.assertFalse(self.service.prepare(pending, self.prepared_run(research_request_hash='forged')))
        record = self.service.history()[0]
        record['research_inputs']['history'].append({'changed': True})
        self.write_record(record)
        self.assertFalse(self.service.prepare(pending, self.prepared_run()))
        self.assertEqual(self.service.snapshot()['activation_check']['code'], 'award_inputs_mismatch')
        self.assertEqual(self.service.pending(), pending)
        self.assertEqual(self.store.runs(), [])

    def test_changed_prepared_run_hash_cannot_activate_or_replace_archive(self):
        with patch('backend.daily_evaluation.select_daily_candidate', side_effect=self.select):
            self.process()
        self.assertTrue(self.service.prepare(self.service.pending(), self.prepared_run()))
        archived = self.preparations()
        state = self.service.state()
        state['pending']['prepared_run']['award_context_hash'] = 'forged'
        self.store.save_setting(self.service._key('account-a'), state)
        self.allow_orders()
        self.next_session()
        self.assertFalse(self.service.activate(state['pending'], effective_day='2026-10-13'))
        self.assertEqual(self.service.state()['status'], 'blocked')
        self.assertEqual(self.preparations(), archived)
        self.assertEqual(self.store.runs(), [])

    def test_proposal_preimages_recompute_both_hashes_without_provider_secrets(self):
        award = self.context_value({'status': 'unavailable'})
        provider = SimpleNamespace(public_config=lambda: {'provider': 'test', 'api_key': 'secret'})
        value = proposal_context(self.parent, self.frames, award['limits'], [],
            {'fingerprint': 'account-a', 'engine_version': 'current'}, provider, self.current.isoformat(),
            competition_context=award['competition_context'], award_context=award)
        request = freeze_research_inputs(self.parent, self.frames, award['limits'], [], award)
        decision = {'id': value['daily_decision_id'], 'research_inputs': request}
        self.assertEqual(value['research_input_hash'], _hash(proposal_input_preimage(value, decision)))
        self.assertEqual(value['research_request_hash'], request['hash'])
        self.assertNotIn('research_input_preimage', value)
        self.assertNotIn('research_inputs', value)
        self.assertNotIn('secret', json.dumps(value))

    def test_runtime_uses_frozen_context_without_reading_competition_again(self):
        service = ExperimentService.__new__(ExperimentService)
        service.store = self.store
        service.now = lambda: self.current
        service.learning = SimpleNamespace(state=lambda: {}, daily=self.service)
        service._competition_research_context = Mock(side_effect=AssertionError('unexpected requery'))
        award = self.context_value({'status': 'unavailable'})
        with patch('backend.learning_research.propose_candidate', return_value={'policy': self.candidate}) as propose:
            service._propose_learning(self.parent, self.frames, award['limits'], [], award_context=award)
        service._competition_research_context.assert_not_called()
        self.assertEqual(propose.call_args.kwargs['award_context'], award)
        self.assertEqual(self.store.orders(), [])

    def test_actual_proposer_wiring_links_llm_preimage_to_daily_request_and_prepared_run(self):
        runtime = ExperimentService.__new__(ExperimentService)
        runtime.store = self.store
        runtime.now = lambda: self.current
        runtime.learning = SimpleNamespace(state=lambda: {}, daily=self.service)
        runtime._competition_research_context = Mock(side_effect=AssertionError('requery'))
        self.context['competition_context'] = {'status': 'unavailable'}
        def propose(champion, frames, limits, history, llm=None, **kwargs):
            kwargs['on_llm_proposal'](self.candidate)
            return {'policy': self.candidate, 'change_family': 'exit'}
        with patch('backend.learning_research.propose_candidate', side_effect=propose), \
                patch('backend.daily_evaluation.select_daily_candidate', side_effect=self.select):
            self.service.process(self.data, self.frames, self.limits, [{'prior': 'research'}],
                                 context=self.context, proposer=runtime._propose_learning)
        self.assertTrue(self.service.prepare(self.service.pending(), self.prepared_run()))
        with self.store.connect() as db:
            observations = [json.loads(row[0]) for row in db.execute('SELECT payload FROM learning_proposals')]
        self.assertEqual(len(observations), 1)
        proposal, decision, pending = observations[0], self.service.history()[0], self.service.pending()
        self.assertEqual(self.service.get_decision(proposal['daily_decision_id']), decision)
        self.assertEqual(proposal['research_input_hash'], _hash(proposal_input_preimage(proposal, decision)))
        self.assertNotIn('research_inputs', proposal)
        self.assertEqual(proposal['research_request_hash'], pending['prepared_run']['research_request_hash'])
        runtime._competition_research_context.assert_not_called()
        self.assertEqual(self.store.orders(), [])

    def test_account_change_before_provider_keeps_original_decision_link_and_blocks_adoption(self):
        runtime = ExperimentService.__new__(ExperimentService)
        runtime.store = self.store
        runtime.now = lambda: self.current
        runtime.learning = SimpleNamespace(state=lambda: {'fingerprint': 'account-a'}, daily=self.service)
        def before_provider(parent, frames, limits, history, llm=None, *, award_context, research_identity):
            policy = self.store.setting('policy')
            self.store.save_setting('policy', {**policy, 'fingerprint': 'account-b'})
            return runtime._propose_learning(parent, frames, limits, history, llm=llm,
                award_context=award_context, research_identity=research_identity)
        def propose(champion, frames, limits, history, llm=None, **kwargs):
            kwargs['on_llm_proposal'](self.candidate)
            return {'policy': self.candidate, 'change_family': 'exit'}
        with patch('backend.learning_research.propose_candidate', side_effect=propose), \
                patch('backend.daily_evaluation.select_daily_candidate', side_effect=self.select):
            self.service.process(self.data, self.frames, self.limits, [],
                                 context=self.context, proposer=before_provider)
        with self.store.connect() as db:
            observation = json.loads(db.execute('SELECT payload FROM learning_proposals').fetchone()[0])
            decision = json.loads(db.execute('SELECT payload FROM daily_policy_decisions').fetchone()[0])
        self.assertEqual(observation['fingerprint'], 'account-a')
        self.assertEqual(observation['daily_decision_id'], decision['id'])
        self.assertEqual(observation['research_input_hash'], _hash(proposal_input_preimage(observation, decision)))
        self.assertEqual(decision['decision'], 'blocked')
        self.assertIsNone(self.service.pending())
        self.assertEqual(self.store.orders(), [])

    def test_rehashed_request_cannot_substitute_a_different_latest_frame(self):
        award = self.context_value()
        request = freeze_research_inputs(self.parent, self.frames, award['limits'], [], award)
        request['frames'][-1]['benchmark_prices']['KOSPI'] += 1
        request['hash'] = _hash({key: value for key, value in request.items() if key != 'hash'})
        self.assertFalse(validate_research_inputs(request))

    def test_snapshot_uses_actual_observation_time_and_future_timestamp_blocks_before_provider(self):
        self.context['observed_at'] = (self.current - timedelta(minutes=2)).isoformat()
        with patch('backend.daily_evaluation.select_daily_candidate', side_effect=self.select):
            self.process()
        record = self.service.history()[0]
        self.assertEqual(record['award_context']['observed_at'], self.context['observed_at'])
        self.assertNotEqual(record['award_context']['observed_at'], record['created_at'])
        self.setUp()
        self.context['observed_at'] = (self.current + timedelta(minutes=2)).isoformat()
        self.process()
        self.assertEqual(self.service.snapshot()['status'], 'blocked')
        self.proposer.assert_not_called()

    def test_book_cash_reservation_and_optional_budget_must_be_consistent_finite_numbers(self):
        for change in ({'budget': '999'}, {'cash': 'NaN'}, {'cash': '-1'}, {'reserved_cash': '1000001'}):
            value = self.context_value()
            value['initial_state'].update(change)
            value['hash'] = _hash({key: item for key, item in value.items() if key != 'hash'})
            self.assertFalse(validate_award_context(value), change)


if __name__ == '__main__':
    unittest.main()
