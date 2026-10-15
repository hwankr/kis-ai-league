"""Revalidate native measured controls before accepting an intraday receipt."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from backend.daily_evaluation import select_daily_candidate, validate_capacity_result_records
from backend.learning_evaluation import daily_metrics
from backend.learning_policy import policy_id
from tests.test_learning_research import LIMITS, frames, rule
from tests.test_signal_hold_packet import packet_pool


class IntradayEvidenceControlTests(unittest.TestCase):
    def setUp(self):
        self.parent, self.frames = rule(), frames(3)
        self.packet, self.pool = packet_pool(self.parent, self.frames)
        self.chosen = self.packet['member_ids']['T_liquid_4']
        self.control = self.packet['member_ids']['C4']

    def native_records(self, failed=None):
        """Use the real daily evaluator and dependency pass; omit path replay."""
        captured = []

        def evaluate(value, source, limits, **kwargs):
            identity = policy_id(value)
            if identity == failed:
                raise ValueError('missing_control_price')
            change = 0. if identity == policy_id(self.parent) else .002 if identity == self.chosen else -.001
            return daily_metrics([change] * 2, first_day=self.frames[0]['as_of'])

        def capture(champion, records, source, limits, context):
            captured.extend(deepcopy(records))
            return None, {'status': 'keep', 'reason': 'no_supported_plan', 'outcomes': []}

        proposal = next(item for item in self.pool if policy_id(item['policy']) == self.chosen)
        with patch('backend.daily_evaluation.portfolio_metrics', side_effect=evaluate), \
                patch('backend.policy_award.select_award_record', side_effect=capture):
            select_daily_candidate(self.parent, {**proposal, 'candidate_pool': self.pool},
                                   self.frames, LIMITS, award_context={'competition_context': {}})
        self.assertEqual(len(captured), 16)
        return captured

    def result(self, records, identity):
        return next(record[2] for record in records if record[1] == identity)

    def validate(self, records):
        before = deepcopy(records)
        result = validate_capacity_result_records(self.parent, records, self.frames, LIMITS)
        self.assertEqual(records, before)
        return result

    def test_losing_measured_control_and_unrelated_error_remain_valid(self):
        for failed in (None, self.packet['member_ids']['T_trend_1']):
            with self.subTest(failed=failed):
                records = self.native_records(failed)
                self.assertEqual(self.result(records, self.chosen)['decision'], 'apply')
                control = self.result(records, self.control)
                self.assertEqual(control['decision'], 'keep')
                self.assertLess(control['metrics']['candidate']['net_return_pct'], 0)
                self.assertTrue(self.validate(records))

    def test_native_missing_control_blocks_candidate_but_forged_apply_is_rejected(self):
        records = self.native_records(self.control)
        blocked = self.result(records, self.chosen)
        self.assertEqual(blocked['decision'], 'blocked')
        self.assertEqual(blocked['metrics']['error'], 'capacity_control_evidence_unavailable')
        self.assertTrue(self.validate(records))
        restored = self.result(self.native_records(), self.chosen)
        blocked.clear()
        blocked.update(deepcopy(restored))
        with self.assertRaisesRegex(ValueError, 'capacity_result_controls_unverified'):
            self.validate(records)

    def test_forged_contract_cannot_remove_required_control(self):
        records = self.native_records()
        contract = self.result(records, self.chosen)['metrics']['capacity_contract']['control_packets'][0]
        contract['required_roles'].remove('C4')
        with self.assertRaisesRegex(ValueError, 'capacity_result_contract_changed'):
            self.validate(records)

    def test_control_must_have_identical_measured_parent(self):
        records = self.native_records()
        self.result(records, self.control)['metrics']['champion']['net_return_pct'] += .01
        with self.assertRaisesRegex(ValueError, 'capacity_result_controls_unverified'):
            self.validate(records)

    def test_capacity_family_cannot_be_hidden_to_skip_required_controls(self):
        for family in (None, 'conditions'):
            with self.subTest(family=family):
                records = self.native_records(self.control)
                chosen = self.result(records, self.chosen)
                chosen.clear()
                chosen.update(deepcopy(self.result(self.native_records(), self.chosen)))
                if family is None:
                    chosen['metrics'].pop('change_family')
                else:
                    chosen['metrics']['change_family'] = family
                with self.assertRaisesRegex(ValueError, 'capacity_result_'):
                    self.validate(records)

    def test_unknown_decision_cannot_skip_control_checks_and_remain_award_eligible(self):
        records = self.native_records(self.control)
        chosen = self.result(records, self.chosen)
        chosen.clear()
        chosen.update(deepcopy(self.result(self.native_records(), self.chosen)))
        chosen['decision'] = 'pending'
        with self.assertRaisesRegex(ValueError, 'capacity_result_'):
            self.validate(records)


if __name__ == '__main__':
    unittest.main()
