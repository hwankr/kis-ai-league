"""Controlled factor packets authorize no orders and never rewrite old exits."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from backend.daily_evaluation import evaluate_daily_candidate, select_daily_candidate
from backend.daily_inputs import freeze_award_context
from backend.learning_evaluation import daily_metrics
from backend.learning_policy import BASELINE_POLICY, policy_id, validate_policy
from backend.policy_capacity import capacity_packet, capacity_metadata, validate_capacity_member
from backend.policy_transition import transition_preview
from tests.test_daily_evaluation import evidence, observed_frames
from tests.test_learning_policy import policy, frames, LIMITS
from tests.test_policy_transition import fixture, lot, LIMITS as BOOK_LIMITS
from tests.test_policy_award import common_plan, paths, progress


def packet_pool(parent, levels=(8,), bases=None):
    packets, entries = [], {}
    for base in bases or [parent]:
        for level in levels:
            packet = capacity_packet(base, top_n=level, champion=parent)
            packets.append(packet)
            for identity in packet['unique_ids']:
                if identity == policy_id(parent):
                    continue
                role = next(role for role, value in packet['member_ids'].items() if value == identity)
                item = entries.setdefault(identity, {'policy': packet['members'][role], 'capacity_packets': []})
                item['capacity_packets'].append(capacity_metadata(packet, identity))
    return packets, list(entries.values())


class CapacityPacketTests(unittest.TestCase):
    def setUp(self):
        self.parent = policy(max_positions=5)
        self.packets, self.pool = packet_pool(self.parent)
        self.packet = self.packets[0]
        self.joint = next(item for item in self.pool if policy_id(item['policy']) == self.packet['member_ids']['AE'])

    def evaluate(self, proposal=None, pool=None):
        return evaluate_daily_candidate(self.parent, proposal or self.joint, frames(3), LIMITS,
            candidate_pool=self.pool if pool is None else pool,
            evaluator=Mock(side_effect=[evidence([0., 0.]), evidence([.001, .002])]))

    def test_packet_changes_only_predeclared_fields_and_preserves_input(self):
        original = deepcopy(self.parent)
        packet = capacity_packet(self.parent)
        self.assertEqual(packet['members']['B'], validate_policy(self.parent))
        self.assertEqual(packet['members']['A']['max_positions'], 20)
        self.assertEqual(packet['members']['A']['strategies'][0]['top_n'], 8)
        self.assertEqual(packet['members']['E']['strategies'][0]['holding_sessions'], 2)
        for role, value in packet['members'].items():
            for key in ('cash_reserve', 'entry_slippage_bps', 'cancel_after_minutes'):
                self.assertEqual(value[key], self.parent[key])
            for key in ('weight', 'conditions', 'rank_by', 'descending', 'stop_loss_pct', 'take_profit_pct'):
                self.assertEqual(value['strategies'][0][key], self.parent['strategies'][0][key])
        self.assertEqual(self.parent, original)
        self.assertEqual(packet, capacity_packet(self.parent))

    def test_levels_are_predeclared_and_controls_can_be_shared_without_duplicate_ids(self):
        packets, pool = packet_pool(self.parent, (8, 16))
        self.assertEqual(len(pool), 5)
        self.assertEqual(len({policy_id(item['policy']) for item in pool}), len(pool))
        shared = next(item for item in pool if policy_id(item['policy']) == packets[0]['member_ids']['E'])
        self.assertEqual(len(shared['capacity_packets']), 2)
        self.assertEqual(len(validate_capacity_member(self.parent, shared, pool)['packets']), 2)
        for level in (0, 4, 20, True, '8'):
            with self.assertRaisesRegex(ValueError, 'invalid_capacity_level'):
                capacity_packet(self.parent, top_n=level)

    def test_one_session_and_degenerate_controls_are_not_extended_or_duplicated(self):
        parent = policy(allocation_mode='equal_eligible')
        parent['strategies'][0].update(top_n=20, holding_sessions=1)
        packet = capacity_packet(parent)
        self.assertEqual(len(packet['unique_ids']), 1)
        self.assertEqual(packet['degenerate_roles'], [['B', 'A', 'E', 'AE']])
        self.assertEqual(packet['members']['AE']['strategies'][0]['holding_sessions'], 1)

    def test_signal_extension_cap_is_shortened_without_changing_mode_or_stops(self):
        self.parent['strategies'][0].update(exit_mode='signal_extension', max_holding_sessions=10,
                                            stop_loss_pct=3., take_profit_pct=12.)
        result = capacity_packet(self.parent)['members']['AE']['strategies'][0]
        self.assertEqual((result['holding_sessions'], result['max_holding_sessions']), (2, 2))
        self.assertEqual((result['exit_mode'], result['stop_loss_pct'], result['take_profit_pct']),
                         ('signal_extension', 3., 12.))

    def test_fixed_exit_optional_unused_maximum_is_preserved(self):
        self.parent['strategies'][0].update(exit_mode='fixed', max_holding_sessions=10)
        value = capacity_packet(self.parent)['members']['E']['strategies'][0]
        self.assertEqual((value['holding_sessions'], value['max_holding_sessions']), (2, 10))
        self.assertEqual(value['exit_mode'], 'fixed')

    def test_shared_metadata_cannot_refer_to_a_partially_present_second_packet(self):
        packets, pool = packet_pool(self.parent, (8, 16))
        pool = [item for item in pool if policy_id(item['policy']) != packets[1]['member_ids']['A']]
        joint = next(item for item in pool if policy_id(item['policy']) == packets[0]['member_ids']['AE'])
        self.assertEqual(self.evaluate(joint, pool)['metrics']['error'], 'invalid_capacity_contract')

    def test_indirect_shared_controls_also_require_exact_metadata_and_limits(self):
        for change in ('metadata', 'limits'):
            packets, pool = packet_pool(self.parent, (8, 16))
            control = next(item for item in pool if policy_id(item['policy']) == packets[1]['member_ids']['A'])
            if change == 'metadata':
                control['capacity_packets'].append({**control['capacity_packets'][0], 'packet_hash': 'fake'})
            else:
                control['limits'] = {**LIMITS, 'budget': '20000'}
            joint = next(item for item in pool if policy_id(item['policy']) == packets[0]['member_ids']['AE'])
            self.assertEqual(self.evaluate(joint, pool)['metrics']['error'], 'invalid_capacity_contract', change)

    def test_valid_compound_has_explicit_family_and_complete_contract(self):
        result = self.evaluate()
        self.assertEqual(result['decision'], 'apply')
        self.assertEqual(result['metrics']['change_family'], 'capacity')
        self.assertEqual(result['metrics']['capacity_contract']['base_id'], policy_id(self.parent))
        self.assertTrue(result['metrics']['capacity_contract']['existing_lot_exits_preserved'])

    def test_standalone_joint_or_missing_control_is_blocked_before_replay(self):
        for pool in (None, [self.joint], self.pool[:-1]):
            evaluate = Mock()
            value = evaluate_daily_candidate(self.parent, self.joint, frames(3), LIMITS,
                                             candidate_pool=pool, evaluator=evaluate)
            self.assertEqual(value['metrics']['error'], 'invalid_capacity_contract')
            evaluate.assert_not_called()

    def test_forged_metadata_is_not_authorization_even_after_self_hash_changes(self):
        for key, value in (('version', 'future'), ('packet_hash', 'fake'), ('base_id', 'fake'),
                           ('actual_parent_id', 'fake'), ('top_n', 16), ('roles', ['A']), ('extra', True)):
            pool = deepcopy(self.pool)
            joint = next(item for item in pool if policy_id(item['policy']) == self.packet['member_ids']['AE'])
            joint['capacity_packets'][0][key] = value
            self.assertEqual(self.evaluate(joint, pool)['metrics']['error'], 'invalid_capacity_contract', key)

    def test_control_metadata_and_limits_are_checked_too(self):
        for change in ('missing_metadata', 'forged_extra_packet', 'limits'):
            pool = deepcopy(self.pool)
            control = next(item for item in pool if policy_id(item['policy']) == self.packet['member_ids']['A'])
            if change == 'missing_metadata':
                control.pop('capacity_packets')
            elif change == 'limits':
                control['limits'] = {**LIMITS, 'budget': '20000'}
            else:
                control['capacity_packets'].append({**control['capacity_packets'][0], 'packet_hash': 'fake'})
            joint = next(item for item in pool if policy_id(item['policy']) == self.packet['member_ids']['AE'])
            self.assertEqual(self.evaluate(joint, pool)['metrics']['error'], 'invalid_capacity_contract', change)

    def test_foreign_base_or_signal_stop_weight_gap_and_arbitrary_combination_rejected(self):
        for change in ('base', 'conditions', 'stop', 'weight', 'gap', 'holding'):
            pool = deepcopy(self.pool)
            joint = next(item for item in pool if policy_id(item['policy']) == self.packet['member_ids']['AE'])
            if change == 'base':
                other = deepcopy(self.parent)
                other['name'] = 'foreign base'
                packet = capacity_packet(other)
                joint['capacity_packets'] = [capacity_metadata(packet, packet['member_ids']['AE'])]
            elif change == 'conditions':
                joint['policy']['strategies'][0]['conditions'] = [{'feature': 'volume_ratio', 'op': 'gt', 'value': 2}]
            elif change == 'stop':
                joint['policy']['strategies'][0]['stop_loss_pct'] = 2
            elif change == 'weight':
                joint['policy']['strategies'][0]['weight'] = .5
            elif change == 'gap':
                joint['policy']['entry_slippage_bps'] = 50
            else:
                joint['policy']['strategies'][0]['holding_sessions'] = 1
            self.assertEqual(self.evaluate(joint, pool)['metrics']['error'], 'invalid_capacity_contract', change)

    def test_compound_without_metadata_stays_blocked_and_single_family_stays_unchanged(self):
        result = self.evaluate({'policy': self.joint['policy']})
        self.assertEqual(result['metrics']['error'], 'multiple_or_unsupported_changes')
        result = self.evaluate({'policy': self.packet['members']['E']})
        self.assertEqual(result['decision'], 'apply')
        self.assertEqual(result['metrics']['change_family'], 'exit')

    def test_own_limit_override_remains_blocked(self):
        proposal = {**self.joint, 'limits': {**LIMITS, 'daily_buy_limit': '9999'}}
        self.assertEqual(self.evaluate(proposal)['metrics']['error'], 'proposal_changes_limits')

    def test_legacy_bootstrap_does_not_need_compound_exception(self):
        result = evaluate_daily_candidate(BASELINE_POLICY, {'policy': self.packet['members']['AE']},
            observed_frames(3), LIMITS, evaluator=Mock(side_effect=[evidence([0., 0.]), evidence([.001, .002])]))
        self.assertEqual(result['decision'], 'apply')
        self.assertEqual(result['metrics']['change_family'], 'bootstrap')

    def test_select_passes_actual_pool_and_audits_all_controls(self):
        def evaluate(value, supplied, limits, **kwargs):
            gains = 0 if policy_id(value) == policy_id(self.parent) else .001
            return evidence([gains, gains])
        proposal = {**deepcopy(self.joint), 'candidate_pool': deepcopy(self.pool)}
        before = deepcopy((self.parent, proposal, LIMITS))
        with patch('backend.daily_evaluation.portfolio_metrics', side_effect=evaluate):
            _, result = select_daily_candidate(self.parent, proposal, frames(3), LIMITS)
        self.assertEqual(result['decision'], 'apply')
        controls = result['metrics']['capacity_controls'][0]['controls']
        self.assertEqual(set(controls), {'B', 'A', 'E', 'AE'})
        self.assertEqual(controls['B']['basis'], 'incumbent_reference')
        self.assertTrue(all(controls[role]['decision'] == 'apply' for role in ('A', 'E', 'AE')))
        self.assertEqual((self.parent, proposal, LIMITS), before)

    def test_failed_later_scope_packet_cannot_erase_measured_incumbent_audit(self):
        base = deepcopy(self.parent)
        base['candidate_scope'] = 'liquid_top20'
        packets, pool = packet_pool(self.parent, bases=[self.parent, base])
        def evaluate(value, supplied, limits, **kwargs):
            if value.get('candidate_scope') == 'liquid_top20':
                raise ValueError('candidate_scope_metadata_missing')
            gain = 0. if policy_id(value) == policy_id(self.parent) else .001
            return evidence([gain, gain])
        parent_audits = []
        for ordered in (pool, list(reversed(pool))):
            proposal = {**deepcopy(self.joint), 'candidate_pool': deepcopy(ordered)}
            with patch('backend.daily_evaluation.portfolio_metrics', side_effect=evaluate):
                _, result = select_daily_candidate(self.parent, proposal, frames(3), LIMITS)
            controls = next(packet['controls'] for packet in result['metrics']['capacity_controls']
                            if packet['packet_hash'] == packets[0]['hash'])
            parent_audits.append(controls['B'])
            self.assertEqual(controls['B']['metrics']['champion'], result['metrics']['champion'])
            self.assertIn(controls['B']['source_candidate_id'], packets[0]['unique_ids'][1:])
            self.assertTrue(all(controls[role]['decision'] == 'apply' for role in ('A', 'E', 'AE')))
        self.assertEqual(parent_audits[0], parent_audits[1])

    def test_compound_is_blocked_when_a_control_computation_fails(self):
        for failed_role in ('A', 'E'):
            def evaluate(value, supplied, limits, **kwargs):
                identity = policy_id(value)
                if identity == self.packet['member_ids'][failed_role]:
                    raise ValueError('missing_control_prices')
                gain = .005 if identity == self.packet['member_ids']['AE'] else .001
                return evidence([0. if identity == policy_id(self.parent) else gain] * 2)
            proposal = {**deepcopy(self.joint), 'candidate_pool': deepcopy(self.pool)}
            with patch('backend.daily_evaluation.portfolio_metrics', side_effect=evaluate):
                selected, result = select_daily_candidate(self.parent, proposal, frames(3), LIMITS)
            controls = result['metrics']['capacity_controls'][0]['controls']
            self.assertNotEqual(policy_id(selected['policy']), self.packet['member_ids']['AE'])
            self.assertEqual(controls[failed_role]['decision'], 'blocked')
            self.assertEqual(controls['AE']['decision'], 'blocked')
            self.assertEqual(controls['AE']['metrics']['error'], 'capacity_control_evidence_unavailable')

    def test_valid_keep_controls_still_support_the_compound_comparison(self):
        def evaluate(value, supplied, limits, **kwargs):
            identity = policy_id(value)
            gain = .002 if identity == policy_id(self.parent) else .001
            if identity == self.packet['member_ids']['AE']:
                gain = .003
            return evidence([gain, gain])
        proposal = {**deepcopy(self.joint), 'candidate_pool': deepcopy(self.pool)}
        with patch('backend.daily_evaluation.portfolio_metrics', side_effect=evaluate):
            selected, result = select_daily_candidate(self.parent, proposal, frames(3), LIMITS)
        controls = result['metrics']['capacity_controls'][0]['controls']
        self.assertEqual(policy_id(selected['policy']), self.packet['member_ids']['AE'])
        self.assertEqual(result['decision'], 'apply')
        self.assertEqual([controls[role]['decision'] for role in ('A', 'E', 'AE')], ['keep', 'keep', 'apply'])

    def test_control_and_compound_require_the_same_matched_parent_evidence(self):
        parent_calls = 0
        def evaluate(value, supplied, limits, **kwargs):
            nonlocal parent_calls
            if policy_id(value) == policy_id(self.parent):
                parent_calls += 1
                gain = .001 if parent_calls == 3 else 0.
            else:
                gain = .005
            return evidence([gain, gain])
        proposal = {**deepcopy(self.joint), 'candidate_pool': deepcopy(self.pool)}
        with patch('backend.daily_evaluation.portfolio_metrics', side_effect=evaluate):
            _, result = select_daily_candidate(self.parent, proposal, frames(3), LIMITS)
        control = result['metrics']['capacity_controls'][0]['controls']['AE']
        self.assertEqual(control['decision'], 'blocked')
        self.assertEqual(control['metrics']['error'], 'capacity_control_evidence_unavailable')

    def test_old_lot_exit_and_global_limits_remain_unchanged(self):
        frame, context = fixture([lot('000001', age=1)])
        original = deepcopy((frame, context, BOOK_LIMITS))
        preview = transition_preview(self.packet['members']['AE'], frame, BOOK_LIMITS, context)
        self.assertEqual(preview['status'], 'ready')
        self.assertEqual(preview['exits'], [])
        self.assertEqual(preview['retained_positions'][0]['exit_policy']['holding_sessions'], 5)
        self.assertTrue(all(item['exit_policy']['holding_sessions'] == 2 for item in preview['entries']))
        self.assertEqual((frame, context, BOOK_LIMITS), original)

    def signal_base(self, family='conditions'):
        base = deepcopy(self.parent)
        base['name'] = 'signal control'
        if family == 'conditions':
            base['strategies'][0]['conditions'] = [{'feature': 'volume_ratio', 'op': 'gt', 'value': 0}]
        else:
            base['strategies'][0]['rank_by'] = 'volume_ratio'
        return base

    def test_signal_base_allocation_exit_combinations_require_both_complete_packets(self):
        for family in ('conditions', 'ranking'):
            base = self.signal_base(family)
            packets, pool = packet_pool(self.parent, bases=[self.parent, base])
            self.assertEqual(len(pool), 7)
            for role in ('A', 'E', 'AE'):
                item = next(item for item in pool if policy_id(item['policy']) == packets[1]['member_ids'][role])
                result = self.evaluate(item, pool)
                self.assertEqual(result['metrics']['change_family'], 'capacity')
                contract = result['metrics']['capacity_contract']
                self.assertEqual(contract['actual_parent_id'], policy_id(self.parent))
                self.assertEqual({p['base_id'] for p in contract['control_packets']},
                                 {policy_id(self.parent), policy_id(base)})
                for control_id in {policy_id(row['policy']) for row in pool} - {policy_id(item['policy'])}:
                    incomplete = [row for row in pool if policy_id(row['policy']) != control_id]
                    self.assertEqual(self.evaluate(item, incomplete)['metrics']['error'], 'invalid_capacity_contract')

    def test_signal_packet_requires_actual_parent_packet_at_same_allocation_level(self):
        base = self.signal_base()
        _, left = packet_pool(self.parent, (8,))
        packets, right = packet_pool(self.parent, (16,), bases=[base])
        item = next(row for row in right if policy_id(row['policy']) == packets[0]['member_ids']['AE'])
        self.assertEqual(self.evaluate(item, left + right)['metrics']['error'], 'invalid_capacity_contract')
        _, left = packet_pool(self.parent, (16,))
        self.assertEqual(self.evaluate(item, left + right)['decision'], 'apply')

    def test_signal_base_cannot_hide_another_behavior_or_strategy_identity_change(self):
        for change in ('weight', 'reserve', 'slots', 'gap', 'stop', 'take', 'hold', 'mode', 'optional_max',
                       'ranking_too', 'id', 'name'):
            base = self.signal_base()
            strategy = base['strategies'][0]
            if change == 'reserve':
                base['cash_reserve'], strategy['weight'] = .1, .9
            elif change == 'slots':
                base['max_positions'] = 6
            elif change == 'gap':
                base['entry_slippage_bps'] = 50
            else:
                key, value = {'weight': ('weight', .9), 'stop': ('stop_loss_pct', 1),
                    'take': ('take_profit_pct', 1), 'hold': ('holding_sessions', 4),
                    'mode': ('exit_mode', 'fixed'), 'optional_max': ('max_holding_sessions', 10),
                    'ranking_too': ('rank_by', 'volume_ratio'), 'id': ('id', 'other'), 'name': ('name', 'other')}[change]
                strategy[key] = value
            packets, pool = packet_pool(self.parent, bases=[self.parent, base])
            item = next(row for row in pool if policy_id(row['policy']) == packets[1]['member_ids']['AE'])
            self.assertEqual(self.evaluate(item, pool)['metrics']['error'], 'invalid_capacity_contract', change)

    def test_signal_base_cannot_reorder_strategy_slots(self):
        parent = deepcopy(self.parent)
        parent['strategies'][0]['weight'] = .5
        parent['strategies'].append({**deepcopy(parent['strategies'][0]), 'id': 'other', 'name': 'other'})
        base = deepcopy(parent)
        base['strategies'].reverse()
        base['strategies'][0]['conditions'] = [{'feature': 'volume_ratio', 'op': 'gt', 'value': 0}]
        packets, pool = packet_pool(parent, bases=[parent, base])
        item = next(row for row in pool if policy_id(row['policy']) == packets[1]['member_ids']['AE'])
        with self.assertRaisesRegex(ValueError, 'invalid_capacity_contract'):
            validate_capacity_member(parent, item, pool)

    def test_all_linked_original_control_computations_are_required_independent_of_pool_order(self):
        packets, pool = packet_pool(self.parent, bases=[self.parent, self.signal_base()])
        target_id = packets[1]['member_ids']['A']
        item = next(row for row in pool if policy_id(row['policy']) == target_id)
        for failed_id in {policy_id(row['policy']) for row in pool} - {target_id}:
            for ordered in (pool, list(reversed(pool))):
                def evaluate(value, supplied, limits, **kwargs):
                    identity = policy_id(value)
                    if identity == failed_id:
                        raise ValueError('control_computation_missing')
                    return evidence([0. if identity == policy_id(self.parent) else .001] * 2)
                proposal = {**deepcopy(item), 'candidate_pool': deepcopy(ordered)}
                with patch('backend.daily_evaluation.portfolio_metrics', side_effect=evaluate):
                    _, result = select_daily_candidate(self.parent, proposal, frames(3), LIMITS)
                audit = next(packet for packet in result['metrics']['capacity_controls']
                             if packet['base_id'] == packets[1]['base_id'])
                self.assertEqual(audit['controls']['A']['decision'], 'blocked')
                self.assertEqual(audit['controls']['A']['metrics']['error'], 'capacity_control_evidence_unavailable')

    def test_losing_unqualified_controls_allow_joint_award_success_but_failed_computations_do_not(self):
        frame, source = fixture([lot(f'{index:06d}') for index in range(8)])
        context = freeze_award_context(**source, latest_frame=frame, competition_context=progress(), limits=BOOK_LIMITS)
        history = [deepcopy(frame) for _ in range(3)]
        for item, day in zip(history, ('2026-10-12', '2026-10-13', '2026-10-14')):
            item['as_of'], item['observed_at'] = day, day + 'T16:05:00+09:00'
        packets, pool = packet_pool(self.parent, bases=[self.parent, self.signal_base()])
        joint_id = packets[1]['member_ids']['AE']
        joint = next(item for item in pool if policy_id(item['policy']) == joint_id)
        for failed_role in (None, 'B', 'A', 'E'):
            def evaluate(value, supplied, limits, **kwargs):
                identity = policy_id(value)
                if failed_role and identity == packets[1]['member_ids'][failed_role]:
                    raise ValueError('control_computation_missing')
                # Even the winning AE loses in historical return comparison;
                # the separate same-path award proof supplies the promotion.
                return daily_metrics([0. if identity == policy_id(self.parent) else -.001] * 2,
                                     first_day='2026-10-12')
            def scenarios(value, *args, **kwargs):
                return paths() if policy_id(value) == joint_id else paths(gross=0, equity=9900)
            proposal = {**deepcopy(joint), 'candidate_pool': deepcopy(pool)}
            with patch('backend.daily_evaluation.portfolio_metrics', side_effect=evaluate), patch(
                    'backend.policy_award_scenarios.scenario_plan', return_value=common_plan()), patch(
                    'backend.policy_award_scenarios.award_scenarios', side_effect=scenarios):
                selected, result = select_daily_candidate(self.parent, proposal, history, BOOK_LIMITS, award_context=context)
            audit = result['metrics']['award_selection']
            if failed_role is None:
                self.assertEqual((result['decision'], audit['reason']), ('apply', 'joint_qualification_robust'))
                self.assertEqual(policy_id(selected['policy']), joint_id)
                controls = next(packet for packet in result['metrics']['capacity_controls']
                                if packet['base_id'] == packets[1]['base_id'])['controls']
                self.assertTrue(all(controls[role]['decision'] == 'keep' for role in ('B', 'A', 'E')))
            else:
                self.assertNotEqual(result['decision'], 'apply', failed_role)
                joint_result = next(item for item in audit['outcomes'] if item['candidate_id'] == joint_id)
                self.assertEqual(joint_result['return_gate_decision'], 'blocked')


if __name__ == '__main__':
    unittest.main()
