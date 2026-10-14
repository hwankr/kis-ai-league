"""Complete, deterministic signal/age packets; no operating account or service."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from backend.daily_evaluation import evaluate_daily_candidate, select_daily_candidate
from backend.learning_policy import BASELINE_POLICY, policy_id, validate_policy
from backend.learning_proposals import template_proposal_source
from backend.learning_research import _behavior_id, _grammar, _summaries, propose_candidate, _public_feedback
from backend.policy_capacity import (HOLD_LEVELS, SIGNAL_FAMILIES, SIGNAL_HOLD_VERSION,
    signal_hold_packet, signal_hold_metadata, validate_capacity_member)
from tests.test_award_research import account_context
from backend.learning_evaluation import daily_metrics
from tests.test_learning_research import rule, frames, metrics, LIMITS


def packet_pool(parent, source):
    packet = signal_hold_packet(parent, source)
    entries = []
    for identity in packet['candidate_ids']:
        role = next(role for role, value in packet['member_ids'].items() if value == identity)
        entries.append({'policy': deepcopy(packet['members'][role]),
                        'capacity_packets': [signal_hold_metadata(packet, identity)],
                        'proposal_source': template_proposal_source(packet, identity)})
    return packet, entries


class SignalHoldPacketTests(unittest.TestCase):
    def setUp(self):
        self.parent, self.frames = rule(), frames(3)
        self.packet, self.pool = packet_pool(self.parent, self.frames)

    def test_fixed_seventeen_roles_are_complete_with_at_most_sixteen_candidates(self):
        expected = {'P', 'C', 'C1', 'C2', 'C4'} | {
            f'T_{family}_{hold}' for family in SIGNAL_FAMILIES for hold in HOLD_LEVELS}
        self.assertEqual(set(self.packet['member_ids']), expected)
        self.assertEqual(len(self.pool), 16)
        self.assertEqual(self.packet['actual_parent_id'], policy_id(self.parent))
        self.assertEqual(self.packet['members']['P'], validate_policy(self.parent))
        self.assertEqual(set(self.packet['member_ids'].values()), {policy_id(self.parent), *self.packet['candidate_ids']})
        self.assertIn('not_full_factorial', self.packet['comparison_design'])

    def test_exact_frames_determine_thresholds_without_performance_or_provider_input(self):
        before = deepcopy((self.parent, self.frames))
        first = list(_grammar(BASELINE_POLICY, _summaries(self.frames), 'defensive'))[:4]
        for family, original in zip(SIGNAL_FAMILIES, first):
            for hold in HOLD_LEVELS:
                generated = self.packet['members'][f'T_{family}_{hold}']['strategies'][0]
                for key in ('conditions', 'rank_by', 'descending'):
                    self.assertEqual(generated[key], original['strategies'][0][key])
        self.assertEqual(signal_hold_packet(self.parent, self.frames), self.packet)
        self.assertEqual((self.parent, self.frames), before)

    def test_risk_money_scope_and_strategy_identity_are_preserved(self):
        parent = deepcopy(self.parent)
        parent['candidate_scope'] = 'liquid_all'
        parent['strategies'][0]['weight'] = .45
        parent['strategies'].append({**deepcopy(parent['strategies'][0]), 'id': 'second', 'name': 'second'})
        packet = signal_hold_packet(parent, self.frames)
        for role, value in packet['members'].items():
            for key in ('cash_reserve', 'entry_slippage_bps', 'cancel_after_minutes', 'candidate_scope'):
                self.assertEqual(value[key], parent[key])
            for original, candidate in zip(parent['strategies'], value['strategies']):
                for key in ('id', 'name', 'weight', 'stop_loss_pct', 'take_profit_pct'):
                    self.assertEqual(candidate[key], original[key])
            if role != 'P':
                self.assertTrue(value['held_before_top_n'])
                self.assertTrue(value['no_same_day_reentry'])
                self.assertEqual(value['allocation_mode'], 'equal_eligible')
                self.assertEqual(value['max_positions'], 20)
                self.assertTrue(all(item['top_n'] == 8 for item in value['strategies']))

    def test_extension_maximum_is_bounded_for_new_age_axis_only(self):
        parent = deepcopy(self.parent)
        parent['strategies'][0].update(exit_mode='signal_extension', max_holding_sessions=9)
        packet = signal_hold_packet(parent, self.frames)
        self.assertEqual(packet['members']['C']['strategies'][0]['max_holding_sessions'], 9)
        for role, value in packet['members'].items():
            if role not in ('P', 'C'):
                strategy = value['strategies'][0]
                self.assertEqual(strategy['max_holding_sessions'], strategy['holding_sessions'])
                self.assertEqual(strategy['exit_mode'], 'signal_extension')
        self.assertEqual(parent['strategies'][0]['max_holding_sessions'], 9)

    def test_behavior_degeneracy_aliases_actual_parent_and_has_no_duplicate_candidates(self):
        parent = deepcopy(self.packet['members']['C1'])
        parent['strategies'][0].update(exit_mode='fixed', max_holding_sessions=9)
        packet, pool = packet_pool(parent, self.frames)
        self.assertEqual(packet['member_ids']['P'], packet['member_ids']['C1'])
        self.assertEqual(len({_behavior_id(item['policy']) for item in pool}), len(pool))
        self.assertLessEqual(len(pool), 16)
        for item in pool:
            validate_capacity_member(parent, item, pool, frames=self.frames, limits=LIMITS)

    def test_every_member_validates_from_the_same_complete_pool_and_exact_parent(self):
        for item in self.pool:
            contract = validate_capacity_member(self.parent, item, self.pool, frames=self.frames, limits=LIMITS)
            self.assertEqual(contract['version'], SIGNAL_HOLD_VERSION)
            self.assertEqual(contract['control_packets'][0]['member_ids'], self.packet['member_ids'])
            self.assertTrue(contract['existing_lot_exits_preserved'])
        self.assertEqual(validate_capacity_member(self.parent, self.pool[-1], list(reversed(self.pool)),
                                                 frames=self.frames, limits=LIMITS)['candidate_id'], policy_id(self.pool[-1]['policy']))

    def test_missing_duplicate_extra_control_or_source_forgery_is_rejected(self):
        for index in range(len(self.pool)):
            incomplete = [item for i, item in enumerate(self.pool) if i != index]
            with self.assertRaisesRegex(ValueError, 'invalid_capacity_contract'):
                validate_capacity_member(self.parent, incomplete[-1], incomplete, frames=self.frames, limits=LIMITS)
        cases = [self.pool + [self.pool[0]]]
        for change in ('metadata', 'source', 'limits', 'risk', 'parent'):
            altered = deepcopy(self.pool)
            if change == 'metadata':
                altered[0]['capacity_packets'][0]['input_hash'] = 'self_asserted'
            elif change == 'source':
                altered[0]['proposal_source']['source'] = 'llm'
            elif change == 'limits':
                altered[0]['limits'] = {**LIMITS, 'daily_buy_limit': '900000'}
            elif change == 'risk':
                altered[0]['policy']['strategies'][0]['stop_loss_pct'] = 3
            else:
                altered[0]['capacity_packets'][0]['actual_parent_id'] = 'foreign'
            cases.append(altered)
        for pool in cases:
            with self.assertRaisesRegex(ValueError, 'invalid_capacity_contract'):
                validate_capacity_member(self.parent, pool[-1], pool, frames=self.frames, limits=LIMITS)

    def test_changed_input_metadata_or_price_invalidates_predeclared_packet(self):
        for field in ('observed_at', 'price', 'feature'):
            source = deepcopy(self.frames)
            if field == 'observed_at':
                source[0]['observed_at'] = source[0]['observed_at'].replace('08:00', '09:00')
            elif field == 'price':
                source[0]['rows'][0]['close'] += 1
            else:
                source[0]['rows'][0]['features']['volume_ratio'] += 1
            with self.assertRaisesRegex(ValueError, 'invalid_capacity_contract'):
                validate_capacity_member(self.parent, self.pool[-1], self.pool, frames=source, limits=LIMITS)
        with self.assertRaisesRegex(ValueError, 'signal_hold_requires_rules_parent'):
            signal_hold_packet(BASELINE_POLICY, self.frames)
        with self.assertRaisesRegex(ValueError, 'signal_hold_feature_summary_missing'):
            signal_hold_packet(self.parent, [{**self.frames[0], 'rows': []}])
        for feature in ('close_sma20_pct', 'close_sma60_pct'):
            source = deepcopy(self.frames)
            for frame in source:
                for row in frame['rows']:
                    row['features'].pop(feature)
            with self.assertRaisesRegex(ValueError, 'signal_hold_feature_summary_missing'):
                signal_hold_packet(self.parent, source)

    def test_same_hold_control_must_compute_but_can_lose_and_unrelated_failure_is_local(self):
        selected = {**deepcopy(self.pool[-1]), 'candidate_pool': deepcopy(self.pool)}
        chosen = policy_id(selected['policy'])
        control = self.packet['member_ids']['C4']
        unrelated = self.packet['member_ids']['T_trend_1']
        for failed in (None, control, unrelated, policy_id(self.parent)):
            def evaluate(value, source, limits, **kwargs):
                if policy_id(value) == failed:
                    raise ValueError('missing_control_price')
                change = 0. if policy_id(value) == policy_id(self.parent) else .002 if policy_id(value) == chosen else -.001
                return daily_metrics([change] * 2, first_day=self.frames[0]['as_of'])
            with patch('backend.daily_evaluation.portfolio_metrics', side_effect=evaluate):
                _, result = select_daily_candidate(self.parent, selected, self.frames, LIMITS)
            controls = result['metrics']['capacity_controls'][0]['controls']
            self.assertEqual(set(controls), set(self.packet['member_ids']))
            if failed in (None, unrelated):
                self.assertEqual(result['decision'], 'apply')
                self.assertEqual(controls['C4']['decision'], 'keep')
                self.assertLess(controls['C4']['metrics']['candidate']['net_return_pct'], 0)
            else:
                self.assertEqual(result['decision'], 'blocked')
                self.assertEqual(result['metrics']['error'], 'evaluation_failed' if failed == policy_id(self.parent)
                                 else 'capacity_control_evidence_unavailable')

    def test_order_and_role_aliases_do_not_change_required_controls_or_selection(self):
        chosen = self.packet['member_ids']['T_liquid_4']
        selections = []
        def evaluate(value, source, limits, **kwargs):
            change = .002 if policy_id(value) == chosen else 0.
            return daily_metrics([change] * 2, first_day=self.frames[0]['as_of'])
        for pool in (self.pool, list(reversed(self.pool))):
            proposal = next(item for item in pool if policy_id(item['policy']) == chosen)
            contract = validate_capacity_member(self.parent, proposal, pool, frames=self.frames, limits=LIMITS)
            self.assertEqual(contract['control_packets'][0]['required_roles'], ['P', 'C4', 'T_liquid_4'])
            with patch('backend.daily_evaluation.portfolio_metrics', side_effect=evaluate):
                selected, result = select_daily_candidate(self.parent, {**proposal, 'candidate_pool': pool}, self.frames, LIMITS)
            self.assertEqual(result['decision'], 'apply')
            selections.append(policy_id(selected['policy']))
        self.assertEqual(selections, [chosen, chosen])


class SignalHoldResearchTests(unittest.TestCase):
    def test_future_frame_or_observation_cannot_supply_thresholds_for_an_earlier_decision(self):
        parent, source = rule(), frames(3)
        context = {**account_context(), 'signal_day': source[-2]['as_of']}
        with self.assertRaisesRegex(ValueError, 'signal_hold_input_after_decision'):
            propose_candidate(parent, source, LIMITS, [], award_context=context)
        context = {**account_context(), 'observed_at': source[-2]['observed_at']}
        with self.assertRaisesRegex(ValueError, 'signal_hold_input_after_decision'):
            propose_candidate(parent, source, LIMITS, [], award_context=context)
        source[0]['observed_at'] = '2027-01-01T00:00:00+00:00'
        with self.assertRaisesRegex(ValueError, 'invalid_signal_hold_frame_dates'):
            signal_hold_packet(parent, source)
        with self.assertRaisesRegex(ValueError, 'invalid_signal_hold_frame_dates'):
            signal_hold_packet(parent, list(reversed(frames(3))))

    def test_runtime_rules_branch_emits_fixed_bank_before_evaluating_candidates(self):
        parent, source = rule(), frames(3)
        source_copy = deepcopy(source)
        packet = signal_hold_packet(parent, source)
        expected_ids = set(packet['candidate_ids'])
        outputs = []
        for reverse in (False, True):
            def evaluate(policy, *_):
                rank = sorted(expected_ids | {policy_id(parent)}).index(policy_id(policy))
                return metrics(net=-rank if reverse else rank)
            with patch('backend.learning_research.portfolio_metrics', side_effect=evaluate):
                result = propose_candidate(parent, source, LIMITS, [], award_context=account_context())
            outputs.append(result)
            self.assertEqual({policy_id(item['policy']) for item in result['candidate_pool']}, expected_ids)
            self.assertEqual(result['search_plan']['version'], 'fixed-signal-hold-search-v1')
            self.assertFalse(result['search_plan']['llm_packet_used'])
            self.assertTrue(all(item['change_family'] == 'capacity' for item in result['candidate_pool']))
            for item in result['candidate_pool']:
                validate_capacity_member(parent, item, result['candidate_pool'], frames=source, limits=LIMITS)
        self.assertEqual(outputs[0]['search_plan'], outputs[1]['search_plan'])
        self.assertEqual(source, source_copy)

    def test_llm_observation_is_preserved_but_never_claimed_as_template_source(self):
        parent, source = rule(), frames(1)
        llm_policy = deepcopy(parent)
        llm_policy['strategies'][0]['conditions'] = [{'feature': 'volume_ratio', 'op': 'gt', 'value': .75}]
        provider, recorded = Mock(), []
        provider.generate.return_value = llm_policy
        result = propose_candidate(parent, source, LIMITS, [], provider, award_context=account_context(),
                                   on_llm_proposal=recorded.append)
        self.assertEqual(recorded, [validate_policy(llm_policy)])
        self.assertFalse(result['search_plan']['llm_packet_used'])
        self.assertTrue(result['search_plan']['llm_observation_callback_completed'])
        for item in result['candidate_pool']:
            source = item['proposal_source']
            self.assertEqual(source['source'], 'deterministic_signal_hold_packet')
            self.assertFalse(source['llm_generated'])
            self.assertNotIn('research_input_hash', source)
            self.assertEqual(source['candidate_id'], policy_id(item['policy']))
        self.assertEqual(_public_feedback([result])[0]['proposal_source']['llm_generated'], False)


if __name__ == '__main__':
    unittest.main()
