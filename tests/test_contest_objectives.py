"""Separate contest evidence and costs without adding a Timefolio executor."""
from pathlib import Path
import tempfile
import unittest

from backend.learning_policy import FEE, TAX
from backend.policy_award import CONTESTS, selection_contract, separate_contest_status
from backend.source_version import DAILY_SOURCES, LEARNING_SOURCES, source_version


class ContestObjectiveTests(unittest.TestCase):
    def test_kis_qualification_uses_its_own_targets_period_and_execution_costs(self):
        contract = selection_contract()
        rules = contract['rules']
        self.assertEqual(contract['contest_id'], 'kis_ai_2026')
        self.assertEqual(rules['targets'], {'gross_amount': 500000000, 'trading_days': 5,
                                          'symbols': 5, 'api_orders': 5})
        self.assertEqual(rules['end_date'], '2026-11-20')
        self.assertEqual(rules['fee_rate'], float(FEE))
        self.assertEqual(rules['sell_tax_rate'], float(TAX))
        self.assertIsNone(contract['return_tolerance_pp'])
        self.assertEqual(contract['risk_reduction_return_tolerance_pp'], 0)
        self.assertEqual(contract['relative_return_role'], 'paired_validity_and_opportunity_cost_audit')
        self.assertEqual(contract['turnover_above_target_reward'], 0)
        self.assertIsNone(contract['win_probability'])
        self.assertEqual(contract['target_rank_max'], 5)
        self.assertIsNone(contract['actual_rank'])
        self.assertIsNone(contract['fifth_place_return_pct'])
        self.assertFalse(contract['automatic_rank_defense'])
        self.assertEqual(contract['objective'], 'core_equal_weight_mean_final_nav')
        self.assertEqual(contract['return_objective']['path_count'], 12)
        self.assertEqual(contract['legacy_promotion_role'], 'diagnostic_only_not_eligibility_or_ranking')
        self.assertNotIn('partial_joint_gate', contract)
        self.assertEqual(contract['diagnostic_partial_joint_gate'],
                         'preserve_and_add_core_joint_witnesses_with_qualified_progress')

    def test_timefolio_needs_its_own_weekly_nav_cost_and_destination_limits(self):
        value = separate_contest_status()
        rules = value['rules']
        self.assertEqual(rules['end_date'], '2026-11-30')
        self.assertEqual(rules['weekly_turnover_pct'], 5)
        self.assertEqual(rules['excluded_after_missed_weeks'], 4)
        self.assertEqual(rules['fee_rate'], .001)
        self.assertNotEqual(rules['fee_rate'], CONTESTS['kis_ai_2026']['fee_rate'])
        self.assertEqual(rules['stock_exceptions'], {'005930': 40., '000660': 30.})
        self.assertEqual(rules['small_cap_total_weight_pct'], 30)
        self.assertIn('sector_cap', rules)
        self.assertNotIn('targets', rules)

    def test_timefolio_is_unavailable_shadow_and_cannot_change_kis_or_mirror_intent(self):
        prior = selection_contract()
        value = separate_contest_status()
        self.assertEqual(value['role'], 'shadow_only')
        self.assertEqual(value['status'], 'unavailable')
        self.assertIn('independent_ledger_weekly_nav_and_sector', value['reason'])
        self.assertFalse(value['affects_kis_selection'])
        self.assertFalse(value['mirror_intent_changed'])
        value['rules']['fee_rate'] = 0
        value['rules']['stock_exceptions']['005930'] = 100
        self.assertEqual(selection_contract(), prior)
        self.assertEqual(separate_contest_status()['rules']['fee_rate'], .001)
        self.assertEqual(separate_contest_status()['rules']['stock_exceptions']['005930'], 40)

    def test_rule_fee_and_scenario_changes_change_exact_daily_engine_identity(self):
        required = {'learning_proposals.py', 'policy_transition.py', 'policy_award.py', 'policy_award_scenarios.py',
                    'policy_capacity.py'}
        self.assertTrue(required <= set(DAILY_SOURCES))
        backend = Path(__file__).resolve().parents[1] / 'backend'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in DAILY_SOURCES:
                (root / name).write_bytes((backend / name).read_bytes())
            original = source_version(DAILY_SOURCES, root=root)
            for name in sorted(required):
                with self.subTest(source=name):
                    source = (root / name).read_bytes()
                    (root / name).write_bytes(source + b'\n# changed selection evidence\n')
                    self.assertNotEqual(source_version(DAILY_SOURCES, root=root), original)
                    (root / name).write_bytes(source)

    def test_selector_change_does_not_change_learning_engine_identity(self):
        backend = Path(__file__).resolve().parents[1] / 'backend'
        self.assertNotIn('policy_award.py', LEARNING_SOURCES)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in set(DAILY_SOURCES) | set(LEARNING_SOURCES):
                (root / name).write_bytes((backend / name).read_bytes())
            before = source_version(LEARNING_SOURCES, root=root)
            daily = source_version(DAILY_SOURCES, root=root)
            with (root / 'policy_award.py').open('ab') as stream:
                stream.write(b'\n# selector-only change\n')
            self.assertEqual(source_version(LEARNING_SOURCES, root=root), before)
            self.assertNotEqual(source_version(DAILY_SOURCES, root=root), daily)


if __name__ == '__main__':
    unittest.main()
