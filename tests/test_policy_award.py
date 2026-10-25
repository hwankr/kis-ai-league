"""Selection effects, joint eligibility, and explicit zero-entry exceptions."""
from copy import deepcopy
import math
import unittest
from unittest.mock import patch

from backend.daily_inputs import freeze_award_context
from backend.learning_policy import policy_id
from backend.policy_award import (joint_qualification, select_award_record, separate_contest_status,
                                 verify_award_record_evidence, _family_status, _activity_preservation,
                                 _return_objective, NAV_EPSILON)
from backend.learning_research import public_competition_context
from tests.test_policy_transition import fixture, lot, candidate, LIMITS
from tests.test_turnover_research import competition


def progress():
    source = competition(metrics={key: {"value": value} for key, value in
                                  (("trading_days", 2), ("symbols", 8), ("api_orders", 8))})
    source["period"]["as_of"] = "2026-10-14"
    return public_competition_context(source)


def paths(*, gross=500000000, equity=11000, first_buy=1):
    return {"status": "ready", 'scenario_plan_hash': 'same-plan', "scenarios": [
        {"id": f'{window}_{schedule}_{environment}', "environment": environment,
         'scenario_plan_hash': 'same-plan', 'start_book_hash': 'same-book',
         'path_hash': window + '_' + schedule, 'source_start_index': index,
         'horizon': 27 if schedule == 'full' else 25,
         "gross_amount": gross, "trading_days": 3, "symbols": 2,
         "api_orders": None, "final_equity": equity, "first_buy_session": first_buy, "max_drawdown_pct": 2}
        for index, window in enumerate(('first', 'middle', 'recent')) for schedule in ('full', 'shortened')
        for environment in ('normal', 'cost_stress', 'partial_delayed', 'partial_delayed_cost_stress')]}


def common_plan():
    return {'status': 'ready', 'hash': 'same-plan', 'start_book_hash': 'same-book',
            'schedules': [{'schedule': schedule, 'selected': [
                {'window': window, 'source_start_index': index} for index, window in enumerate(('first', 'middle', 'recent'))]}
                for schedule in ('full', 'shortened')]}


def outcome(net=1., stress=.8, drawdown=1., parent_net=0., parent_stress=0., parent_drawdown=2.):
    def metric(a, b, c):
        return {"net_return_pct": a, "stress_return_pct": b,
                "max_drawdown_pct": c, "stress_drawdown_pct": c}
    return {"decision": "apply", "metrics": {"candidate": metric(net, stress, drawdown),
            "champion": metric(parent_net, parent_stress, parent_drawdown), "maximum_drawdown_pct": 15.}}


class JointQualificationTests(unittest.TestCase):
    def test_volume_and_profit_must_qualify_on_the_same_path(self):
        supplied = paths()
        for index, row in enumerate(supplied['scenarios']):
            row['final_equity' if index % 2 else 'gross_amount'] = 9900 if index % 2 else 0
        value = joint_qualification(supplied, progress(), LIMITS["budget"])
        self.assertEqual(value["status"], "none_met")
        self.assertEqual(value["qualifying_scenarios"], 0)
        self.assertIsNone(value["win_probability"])

    def test_turnover_beyond_target_has_no_additional_reward(self):
        first = joint_qualification(paths(), progress(), LIMITS["budget"])
        second = joint_qualification(paths(gross=900000000), progress(), LIMITS["budget"])
        self.assertEqual((first["grade"], first["worst_shortfall"]), (second["grade"], second["worst_shortfall"]))

    def test_missing_api_and_distinct_symbols_never_become_assumed_qualification(self):
        actual = progress()
        actual["metrics"]["api_orders"]["value"] = 2
        self.assertEqual(joint_qualification(paths(), actual, LIMITS["budget"])["status"], "unknown")
        actual["metrics"]["api_orders"]["value"] = 8
        actual["metrics"]["symbols"]["value"] = 3
        supplied = paths()
        for item in supplied["scenarios"]:
            item["symbols"] = 3
        value = joint_qualification(supplied, actual, LIMITS["budget"])
        self.assertEqual(value["status"], "none_met")
        self.assertEqual(value["scenarios"][0]["values"]["symbols"], 3)

    def test_unevaluated_path_and_missing_money_are_unknown_not_zero(self):
        for change in ({"status": "unavailable"}, {"gross_amount": None}, {"final_equity": None}):
            supplied = paths()
            supplied["scenarios"][0].update(change)
            self.assertEqual(joint_qualification(supplied, progress(), LIMITS["budget"])["status"], "unknown")

    def test_partial_history_keeps_known_checks_but_cannot_claim_family_qualification(self):
        supplied = paths()
        supplied["status"] = "unavailable"
        supplied["scenarios"][1].update(status="unavailable", final_equity=None)
        value = joint_qualification(supplied, progress(), LIMITS["budget"])
        self.assertEqual(value["status"], "unknown")
        self.assertEqual((value["evaluated_scenarios"], value["qualifying_scenarios"]), (23, 23))
        self.assertIsNone(value["worst_shortfall"])

    def test_contests_have_separate_costs_and_no_cross_promotion(self):
        shadow = separate_contest_status()
        self.assertFalse(shadow["affects_kis_selection"])
        self.assertFalse(shadow["mirror_intent_changed"])
        self.assertEqual(shadow["rules"]["fee_rate"], .001)
        raw = competition(contest="timefolio_rfm_13")
        self.assertFalse(public_competition_context(raw)["reliable"])

    def test_known_failed_checks_survive_other_unknown_conditions(self):
        actual = progress()
        actual['metrics']['api_orders']['value'] = 2
        value = joint_qualification(paths(gross=0, equity=9900), actual, LIMITS['budget'])
        first = value['scenarios'][0]
        self.assertEqual(value['status'], 'unknown')
        self.assertEqual(value['known_failed_scenarios'], 24)
        self.assertEqual(value['unknown_coverage_scenarios'], 24)
        self.assertEqual(value['unresolved_joint_scenarios'], 0)
        self.assertEqual(first['known_failed_conditions'], ['final_equity', 'gross_amount'])
        self.assertEqual(first['unknown_conditions'], ['api_orders'])
        self.assertEqual(first['final_equity_shortfall_amount'], 100)
        self.assertEqual(first['normalized_deficits']['final_equity'], .01)
        self.assertEqual(first['gross_shortfall_amount'], 500000000 - actual['pace']['gross_amount'])
        self.assertIsNone(first['normalized_deficits']['api_orders'])

    def test_core_and_reserve_keep_their_own_joint_checks(self):
        supplied = paths()
        for row in supplied['scenarios']:
            if row['environment'].startswith('partial_'):
                row['gross_amount'] = 0
        value = joint_qualification(supplied, progress(), LIMITS['budget'])
        self.assertEqual(value['status'], 'some_met')
        self.assertEqual(value['groups']['core']['status'], 'all_met')
        self.assertEqual(value['groups']['reserve']['status'], 'none_met')
        self.assertEqual(value['groups']['core']['scenario_count'], 12)
        self.assertGreater(value['groups']['reserve']['gross_shortfall_amount'], 0)
        self.assertEqual(value['groups']['reserve']['final_equity_shortfall_amount'], 0)

    def test_unavailable_source_cannot_reuse_its_positive_numeric_results(self):
        supplied = paths()
        supplied['scenarios'][0]['status'] = 'unavailable'
        value = joint_qualification(supplied, progress(), LIMITS['budget'])
        self.assertIsNone(value['scenarios'][0]['values']['gross_amount'])
        self.assertIsNone(value['scenarios'][0]['values']['final_equity'])
        self.assertEqual(value['source_ready_scenarios'], 23)

    def test_execution_diagnostics_are_numeric_allowlist_not_arbitrary_fields(self):
        supplied = paths()
        supplied['scenarios'][0]['execution_diagnostics'] = {
            'buy_attempts': 2, 'fees_paid': 3.5, 'unfilled_orders': None,
            'limit_blocked': True, 'arbitrary': 'not forwarded'}
        value = joint_qualification(supplied, progress(), LIMITS['budget'])
        diagnostics = value['scenarios'][0]['execution_diagnostics']
        self.assertEqual(diagnostics['buy_attempts'], 2)
        self.assertEqual(diagnostics['fees_paid'], 3.5)
        self.assertIsNone(diagnostics['unfilled_orders'])
        self.assertIsNone(diagnostics['limit_blocked'])
        self.assertNotIn('arbitrary', diagnostics)

    def test_session_zero_requires_explicit_intraday_prefix_fill_evidence(self):
        supplied, plan = paths(first_buy=0), common_plan()
        qualification = joint_qualification(supplied, progress(), LIMITS['budget'])
        self.assertEqual(_family_status(supplied, qualification, plan, 15), 'scenario_coverage_unknown')
        plan['version'] = 'intraday-quality-only-common-windows-v2'
        for row in supplied['scenarios']:
            row.update(intraday_modelled_fills_are_actual=False, intraday_prefix={'gross_buy_amount': 100})
        self.assertEqual(_family_status(supplied, qualification, plan, 15), 'ready')
        supplied['scenarios'][0]['intraday_prefix']['gross_buy_amount'] = 0
        self.assertEqual(_family_status(supplied, qualification, plan, 15), 'scenario_coverage_unknown')


class AwardSelectionTests(unittest.TestCase):
    def setup_context(self):
        frame, context = fixture([lot(f"{index:06d}") for index in range(8)])
        context = freeze_award_context(**context, latest_frame=frame, competition_context=progress(), limits=LIMITS)
        return frame, context

    def choose(self, parent, policies, outcomes, scenarios=None):
        frame, context = self.setup_context()
        records = [({"policy": policy}, policy_id(policy), result) for policy, result in zip(policies, outcomes)]
        with patch('backend.policy_award_scenarios.scenario_plan', return_value=common_plan()), patch(
                   "backend.policy_award_scenarios.award_scenarios", side_effect=scenarios or
                   (lambda policy, *args, **kwargs: paths(gross=0, equity=9900, first_buy=None) if policy == parent else paths())):
            return select_award_record(parent, records, [frame], LIMITS, context)

    def no_signal_parent(self):
        parent = candidate(maximum=20)
        parent["strategies"][0]["conditions"] = [{"feature": "volume_ratio", "op": "gt", "value": 100}]
        return parent

    def test_changed_latest_frame_blocks_selection_before_any_scenario_replay(self):
        frame, context = self.setup_context()
        frame["rows"][0]["close"] += 1
        with patch("backend.policy_award_scenarios.award_scenarios") as replay:
            selected, audit = select_award_record(self.no_signal_parent(), [], [frame], LIMITS, context)
        self.assertIsNone(selected)
        self.assertEqual(audit["status"], "blocked")
        replay.assert_not_called()

    def test_custom_evaluator_missing_metrics_returns_blocked_instead_of_crashing(self):
        from backend.daily_evaluation import select_daily_candidate
        frame, context = self.setup_context()
        _, result = select_daily_candidate(self.no_signal_parent(), {"policy": candidate(maximum=20)},
            [frame], LIMITS, evaluator=lambda *args: {"decision": "keep", "metrics": None}, award_context=context)
        self.assertEqual(result["decision"], "blocked")
        self.assertIn("award_selection", result["metrics"])

    def test_blocked_candidate_cannot_supply_the_incumbent_return_anchor(self):
        blocked = outcome(parent_net=100., parent_stress=100.)
        blocked["decision"] = "blocked"
        selected, audit = self.choose(self.no_signal_parent(),
            [candidate(maximum=19), candidate(maximum=20)], [blocked, outcome()])
        self.assertEqual(selected, 1)
        self.assertEqual(audit["status"], "apply")

    def test_only_error_free_keep_or_apply_can_be_candidate_or_parent_anchor(self):
        for status in ('error', 'unknown', None, '', False, 'apply_with_error'):
            invalid = outcome(parent_net=100, parent_stress=100)
            invalid['decision'] = 'apply' if status == 'apply_with_error' else status
            if status == 'apply_with_error':
                invalid['metrics']['error'] = 'invalid_portfolio_evaluation'
            with self.subTest(status=status):
                selected, audit = self.choose(self.no_signal_parent(), [candidate(maximum=19)], [invalid])
                self.assertIsNone(selected)
                self.assertEqual(audit['reason'], 'paired_return_evidence_unavailable')
                selected, audit = self.choose(self.no_signal_parent(),
                    [candidate(maximum=19), candidate(maximum=20)], [invalid, outcome()])
                self.assertEqual(selected, 1)
                self.assertFalse(audit['outcomes'][0]['eligible'])
                self.assertTrue(audit['outcomes'][0]['qualification_risk'])

    def test_missing_remaining_sessions_is_unknown_not_finished(self):
        frame, source = fixture([lot(f"{index:06d}") for index in range(8)])
        actual = progress()
        actual["pace"].pop("remaining_sessions")
        context = freeze_award_context(**source, latest_frame=frame, competition_context=actual, limits=LIMITS)
        selected, audit = select_award_record(self.no_signal_parent(), [], [frame], LIMITS, context)
        self.assertIsNone(selected)
        self.assertEqual((audit["status"], audit["reason"]), ("blocked", "contest_progress_unavailable"))

    def test_high_backtest_return_with_eight_held_six_slots_is_not_promoted(self):
        def scenarios(policy, *args, **kwargs):
            return paths() if policy == candidate(maximum=20) else paths(gross=0, equity=9900, first_buy=None)
        selected, audit = self.choose(self.no_signal_parent(), [candidate(maximum=6), candidate(maximum=20)],
                                      [outcome(net=2., stress=1.8), outcome()], scenarios)
        self.assertEqual(selected, 1)
        self.assertFalse(audit["outcomes"][0]["eligible"])
        self.assertEqual(audit["outcomes"][0]["promotion_reason"], "return_objective_not_improved")
        self.assertEqual(audit["selected_qualification"], "all_met")

    def test_lower_relative_return_qualified_candidate_beats_ineligible_return_leader(self):
        parent = self.no_signal_parent()
        first, second = candidate(maximum=20), candidate(maximum=19)
        def scenarios(value, *args, **kwargs):
            return paths(gross=500000000 if value == second else 0,
                         equity=11000 if value == second else 10000 if value == first else 9900)
        selected, audit = self.choose(parent, [first, second], [outcome(net=2., stress=1.8), outcome(net=1.99, stress=1.79)], scenarios)
        self.assertEqual(selected, 1)
        self.assertEqual(audit["outcomes"][1]["promotion_reason"], "return_improvement")
        self.assertTrue(audit['outcomes'][0]['eligible'])

    def test_scalar_worst_returns_cannot_hide_a_loss_in_risk_reduction_exception(self):
        supplied = outcome(net=.5, stress=.5)
        supplied["decision"] = "keep"
        supplied["metrics"]["scenario_comparisons"] = {
            "A": outcome(net=.9, stress=.9, parent_net=1., parent_stress=1.)["metrics"],
            "B": outcome(net=.5, stress=.5)["metrics"]}
        selected, audit = self.choose(candidate(maximum=20), [candidate(maximum=6)], [supplied],
                                      lambda *args, **kwargs: paths(gross=0, equity=9900, first_buy=None))
        self.assertIsNone(selected)
        self.assertFalse(audit['outcomes'][0]['risk_reduction_evidence'])

    def test_equal_objective_keeps_incumbent_despite_improved_qualification(self):
        parent, first, second = self.no_signal_parent(), candidate(maximum=20), candidate(maximum=19)
        def scenarios(value, *args, **kwargs):
            return paths(gross=500000000 if value == second else 0)
        selected, _ = self.choose(parent, [first, second], [outcome(), outcome()], scenarios)
        self.assertIsNone(selected)

    def test_risk_reduction_is_diagnostic_and_cannot_replace_return_improvement(self):
        selected, audit = self.choose(candidate(maximum=20), [candidate(maximum=6)], [outcome()],
                                      lambda *args, **kwargs: paths(gross=0, equity=9900, first_buy=None))
        self.assertIsNone(selected)
        self.assertTrue(audit['outcomes'][0]['risk_reduction_evidence'])
        self.assertEqual(audit["outcomes"][0]["transition"]["entry_count"], 0)
        self.assertFalse(audit['outcomes'][0]['eligible'])

    def test_defensive_name_and_zero_buys_alone_do_not_demonstrate_risk_reduction(self):
        value = candidate(maximum=6)
        value["name"] = "defensive"
        selected, audit = self.choose(self.no_signal_parent(), [value], [outcome()],
                                      lambda *args, **kwargs: paths(gross=0, equity=9900, first_buy=None))
        self.assertIsNone(selected)
        self.assertFalse(audit["outcomes"][0]["risk_reduction_evidence"])

    def test_zero_entry_risk_reduction_still_cannot_replace_objective_improvement(self):
        parent = candidate(maximum=20)
        selected, audit = self.choose(parent, [candidate(maximum=6)], [outcome()],
                                      lambda policy, *args, **kwargs: paths(gross=0 if policy == parent else 500000000, first_buy=None))
        self.assertIsNone(selected)
        self.assertEqual(audit['outcomes'][0]['promotion_reason'], 'return_objective_not_improved')
        for invalid in ('unknown', 0, -1, 100, True):
            selected, audit = self.choose(self.no_signal_parent(), [candidate(maximum=6)], [outcome()],
                                          lambda *args, **kwargs: paths(first_buy=invalid))
            self.assertIsNone(selected, invalid)
            self.assertEqual(audit['outcomes'][0]['scenario_status'], 'scenario_coverage_unknown')

    def test_supported_natural_exit_path_can_justify_no_immediate_entry(self):
        parent, value = self.no_signal_parent(), candidate(maximum=6)
        def scenarios(policy, *args, **kwargs):
            return paths(gross=0, equity=9900 if policy == parent else 9950, first_buy=3)
        selected, audit = self.choose(parent, [value], [outcome()], scenarios)
        self.assertEqual(selected, 0)
        self.assertEqual(audit["outcomes"][0]["transition_class"], "after_natural_exits")
        self.assertEqual(audit['reason'], 'return_challenge')
        self.assertEqual(audit['outcomes'][0]['plan_class'], 0)

    def test_exact_tie_prefers_incumbent_and_never_changes_old_lot_exits(self):
        frame, context = self.setup_context()
        original = deepcopy(context)
        value = candidate(maximum=20)
        with patch('backend.policy_award_scenarios.scenario_plan', return_value=common_plan()), patch(
                   "backend.policy_award_scenarios.award_scenarios", return_value=paths()):
            selected, audit = select_award_record(self.no_signal_parent(),
                [({"policy": value}, policy_id(value), outcome(net=0., stress=0.))], [frame], LIMITS, context)
        self.assertIsNone(selected)
        self.assertEqual(context, original)

    def test_all_candidates_infeasible_keeps_even_with_improved_backtest_returns(self):
        selected, audit = self.choose(self.no_signal_parent(), [candidate(maximum=20)], [outcome(net=100., stress=90.)],
                                      lambda *args, **kwargs: paths(gross=100000000, equity=9900))
        self.assertIsNone(selected)
        self.assertEqual(audit['reason'], 'no_supported_return_improvement')
        self.assertEqual(audit['outcomes'][0]['promotion_reason'], 'return_objective_not_improved')
        self.assertTrue(audit['qualification_risk'])
        self.assertTrue(audit['outcomes'][0]['qualification_risk'])
        self.assertTrue(audit['decision_needed'])
        self.assertFalse(audit['incumbent_has_supported_qualification_plan'])

    def test_absolute_qualified_candidate_can_have_lower_paired_returns_than_parent(self):
        selected, audit = self.choose(self.no_signal_parent(), [candidate(maximum=20)],
                                      [outcome(net=-2., stress=-3., parent_net=1., parent_stress=.5)])
        self.assertEqual(selected, 0)
        self.assertEqual(audit['reason'], 'return_improvement')
        self.assertEqual(audit['outcomes'][0]['relative_return_audit']['paired']['net_return_pct'], -3.)

    def test_conditional_plan_can_have_reserve_loss_without_worsening_parent_worst_nav(self):
        parent = self.no_signal_parent()
        def scenarios(policy, *args, **kwargs):
            value = paths() if policy != parent else paths(gross=0, equity=9900)
            for row in value['scenarios']:
                if row['environment'].startswith('partial_'):
                    row['gross_amount'] = 0
            return value
        selected, audit = self.choose(parent, [candidate(maximum=20)], [outcome()], scenarios)
        self.assertEqual(selected, 0)
        self.assertEqual(audit['reason'], 'return_improvement')
        def losses(policy, *args, **kwargs):
            value = scenarios(policy)
            value['scenarios'][2]['final_equity'] = 9900
            return value
        selected, audit = self.choose(parent, [candidate(maximum=20)], [outcome()], losses)
        self.assertEqual(selected, 0)
        self.assertEqual(audit['outcomes'][0]['promotion_reason'], 'return_improvement')

    def test_parent_unknown_cannot_establish_relative_risk_for_any_promotion(self):
        parent = self.no_signal_parent()
        def scenarios(policy, *args, **kwargs):
            value = paths()
            if policy == parent:
                value['status'] = 'unavailable'
                for row in value['scenarios']:
                    row.update(status='unavailable', gross_amount=None, final_equity=None)
            return value
        selected, audit = self.choose(parent, [candidate(maximum=20)], [outcome()], scenarios)
        self.assertIsNone(selected)
        self.assertEqual(audit['outcomes'][0]['promotion_reason'], 'paired_scenario_evidence_unavailable')
        self.assertEqual(audit['incumbent_scenario_status'], 'scenario_coverage_unknown')
        def insufficient(policy, *args, **kwargs):
            value = scenarios(policy)
            if policy != parent:
                for row in value['scenarios']:
                    row['gross_amount'] = 300000000
            return value
        selected, audit = self.choose(parent, [candidate(maximum=20)], [outcome()], insufficient)
        self.assertIsNone(selected)
        self.assertFalse(audit['outcomes'][0]['progress_evidence']['supported'])

    def test_unknown_or_mislabeled_path_cannot_be_removed_from_qualification(self):
        parent = self.no_signal_parent()
        for change in ('unavailable', 'remove', 'relabel', 'hash', 'risk'):
            def scenarios(policy, *args, **kwargs):
                value = paths() if policy != parent else paths(gross=0, equity=9900)
                if policy != parent:
                    if change == 'unavailable':
                        value['scenarios'][0].update(status='unavailable', gross_amount=None)
                    elif change == 'remove':
                        value['scenarios'].pop()
                    elif change == 'relabel':
                        value['scenarios'][0]['environment'], value['scenarios'][2]['environment'] = (
                            value['scenarios'][2]['environment'], value['scenarios'][0]['environment'])
                    elif change == 'hash':
                        value['scenarios'][0]['path_hash'] = 'different-source-path'
                    else:
                        value['scenarios'][0]['max_drawdown_pct'] = 15.1
                return value
            selected, audit = self.choose(parent, [candidate(maximum=20)], [outcome()], scenarios)
            self.assertIsNone(selected, change)
            self.assertFalse(audit['outcomes'][0]['eligible'])

    def test_progress_threshold_is_diagnostic_but_path_activity_cannot_regress(self):
        parent = self.no_signal_parent()
        actual = progress()
        threshold = math.ceil((500000000 - actual['pace']['gross_amount']) / actual['pace']['remaining_sessions'])
        for change in ('enough', 'too_small', 'one_worse', 'loss'):
            def scenarios(policy, *args, **kwargs):
                value = paths(gross=250000000 if policy == parent else 250000000 + threshold,
                              equity=11000 if policy == parent else 11050)
                if policy != parent:
                    if change == 'too_small':
                        for row in value['scenarios']:
                            row['gross_amount'] -= 1
                    elif change == 'one_worse':
                        value['scenarios'][0]['gross_amount'] = 249999999
                    elif change == 'loss':
                        value['scenarios'][0]['final_equity'] = 9999
                return value
            selected, audit = self.choose(parent, [candidate(maximum=20)], [outcome()], scenarios)
            self.assertEqual(audit['progress_threshold_amount'], threshold)
            if change in ('enough', 'too_small'):
                self.assertEqual(selected, 0)
                self.assertEqual(audit['reason'], 'return_challenge')
                self.assertFalse(audit['award_plan_supported'])
                self.assertEqual(audit['outcomes'][0]['progress_evidence']['supported'], change == 'enough')
            else:
                self.assertIsNone(selected, change)

    def test_higher_core_nav_conditional_plan_beats_robust_class(self):
        parent, robust, conditional = self.no_signal_parent(), candidate(maximum=20), candidate(maximum=19)
        def scenarios(policy, *args, **kwargs):
            value = paths(gross=0, equity=9900) if policy == parent else paths(equity=11000 if policy == robust else 12000)
            if policy == conditional:
                for row in value['scenarios']:
                    if row['environment'].startswith('partial_'):
                        row['gross_amount'] = 0
            return value
        selected, audit = self.choose(parent, [conditional, robust], [outcome(net=10.), outcome(net=-1.)], scenarios)
        self.assertEqual(selected, 0)
        self.assertEqual(audit['selected_plan_class'], 4)
        self.assertEqual(audit['selected_objective_value'], 12000)

    def test_higher_core_nav_precedes_smaller_turnover_deficit(self):
        parent, first, second = self.no_signal_parent(), candidate(maximum=20), candidate(maximum=19)
        def scenarios(policy, *args, **kwargs):
            return (paths(gross=250000000) if policy == parent else paths(gross=300000000, equity=12000)
                    if policy == first else paths(gross=350000000, equity=11000))
        selected, audit = self.choose(parent, [first, second], [outcome(net=10.), outcome()], scenarios)
        self.assertEqual(selected, 0)
        self.assertEqual(audit['reason'], 'return_challenge')

    def test_one_common_plan_is_audited_and_passed_to_all_replays(self):
        frame, context = self.setup_context()
        parent, proposal = self.no_signal_parent(), candidate(maximum=20)
        records = [({'policy': proposal}, policy_id(proposal), outcome())]
        def replay(policy, *args, **kwargs):
            self.assertIs(kwargs['plan'], plan)
            value = paths(gross=0, equity=9900) if policy == parent else paths()
            value['scenario_plan'] = plan
            return value
        plan = common_plan()
        with patch('backend.policy_award_scenarios.scenario_plan', return_value=plan) as build, patch(
                'backend.policy_award_scenarios.award_scenarios', side_effect=replay):
            selected, audit = select_award_record(parent, records, [frame], LIMITS, context)
        build.assert_called_once()
        self.assertEqual(selected, 0)
        self.assertEqual(audit['scenario_plan'], plan)
        self.assertNotIn('scenario_plan', audit['incumbent_scenarios'])
        self.assertNotIn('scenario_plan', audit['outcomes'][0]['scenario_evidence'])

    def test_partial_joint_challenge_promotes_without_requiring_all_paths_nonnegative(self):
        parent = self.no_signal_parent()
        def scenarios(policy, *args, **kwargs):
            value = paths(gross=100000000, equity=9700) if policy == parent else paths(equity=9750)
            if policy != parent:
                value['scenarios'][0]['final_equity'] = 10100
            return value
        selected, audit = self.choose(parent, [candidate(maximum=20)], [outcome()], scenarios)
        self.assertEqual(selected, 0)
        self.assertEqual(audit['reason'], 'return_challenge')
        self.assertEqual(audit['selected_plan_class'], 3)
        self.assertTrue(audit['qualification_risk'])
        self.assertFalse(audit['award_plan_supported'])
        self.assertEqual(audit['selected_qualification'], 'some_met')

    def test_progress_has_no_hidden_all_path_principal_floor(self):
        parent = self.no_signal_parent()
        def scenarios(policy, *args, **kwargs):
            return paths(gross=100000000, equity=9700) if policy == parent else paths(gross=300000000, equity=9750)
        selected, audit = self.choose(parent, [candidate(maximum=20)], [outcome()], scenarios)
        self.assertEqual(selected, 0)
        self.assertEqual(audit['reason'], 'return_challenge')
        self.assertEqual(audit['selected_qualification'], 'none_met')

    def test_better_worst_nav_does_not_hide_worse_matching_path(self):
        parent = self.no_signal_parent()
        def scenarios(policy, *args, **kwargs):
            value = paths(gross=100000000, equity=9700) if policy == parent else paths(gross=300000000, equity=9850)
            if policy == parent:
                value['scenarios'][0]['final_equity'] = 11000
            return value
        selected, audit = self.choose(parent, [candidate(maximum=20)], [outcome()], scenarios)
        self.assertEqual(selected, 0)
        risk = audit['outcomes'][0]['relative_risk_evidence']
        self.assertEqual(risk['worst_equity_improvement'], 150)
        self.assertEqual(risk['worst_same_path_equity_delta'], -1150)
        self.assertEqual(risk['same_path_equity_worsened'], ['first_full_normal'])

    def test_losing_parent_joint_witness_cannot_be_relabelled_as_progress(self):
        parent = self.no_signal_parent()
        def scenarios(policy, *args, **kwargs):
            value = paths(gross=100000000, equity=9700) if policy == parent else paths(equity=9750)
            row = value['scenarios'][0 if policy == parent else 4]
            row.update(gross_amount=500000000, final_equity=10100)
            return value
        selected, audit = self.choose(parent, [candidate(maximum=20)], [outcome()], scenarios)
        self.assertIsNone(selected)
        self.assertEqual(audit['outcomes'][0]['progress_evidence']['reason'], 'incumbent_core_joint_witness_lost')

    def test_qualified_absolute_plan_cannot_lower_incumbent_worst_nav(self):
        parent = self.no_signal_parent()
        selected, audit = self.choose(parent, [candidate(maximum=20)], [outcome()],
            lambda policy, *args, **kwargs: paths(gross=0, equity=12000) if policy == parent else paths(equity=11000))
        self.assertIsNone(selected)
        self.assertEqual(audit['outcomes'][0]['promotion_reason'], 'worst_equity_worsened')

    def test_sealed_replay_gate_verification_recomputes_decision_and_rejects_tampering(self):
        parent, policy = self.no_signal_parent(), candidate(maximum=20)
        selected, audit = self.choose(parent, [policy], [outcome()])
        frame, context = self.setup_context()
        records = [({'policy': policy}, policy_id(policy), outcome())]
        with patch('backend.policy_award_scenarios.scenario_plan', return_value=common_plan()), patch(
                'backend.policy_award_scenarios.award_scenarios') as replay:
            verified, reproduced = verify_award_record_evidence(parent, records, [frame], LIMITS, context, audit)
            self.assertEqual(verified, selected)
            self.assertEqual(reproduced, audit)
            replay.assert_not_called()
            for field, value in [('status', 'keep'), ('reason', 'qualified_progress'), ('selected_plan_class', 3)]:
                forged = deepcopy(audit)
                forged[field] = value
                with self.assertRaisesRegex(ValueError, 'award_evidence_decision_mismatch'):
                    verify_award_record_evidence(parent, records, [frame], LIMITS, context, forged)

    def test_zero_joint_witnesses_with_unchanged_activity_allow_return_challenge(self):
        parent = self.no_signal_parent()
        selected, audit = self.choose(parent, [candidate(maximum=20)], [outcome()],
            lambda policy, *args, **kwargs: paths(gross=100000000, equity=9700 if policy == parent else 9750))
        self.assertEqual(selected, 0)
        self.assertEqual(audit['reason'], 'return_challenge')
        self.assertEqual(audit['incumbent_qualification']['qualifying_scenarios'], 0)
        self.assertEqual(audit['selected_qualification'], 'none_met')
        self.assertFalse(audit['award_plan_supported'])
        self.assertTrue(audit['qualification_risk'])
        self.assertFalse(audit['outcomes'][0]['progress_evidence']['supported'])
        self.assertEqual(audit['objective_improvement'], 50)

    def test_each_core_activity_deficit_must_be_known_and_nonworse(self):
        parent = joint_qualification(paths(), progress(), LIMITS['budget'])
        for field in ('gross_amount', 'trading_days', 'symbols', 'api_orders'):
            for value in (1, None, float('nan'), True):
                with self.subTest(field=field, value=value):
                    proposed = deepcopy(parent)
                    proposed['scenarios'][0]['deficits'][field] = value
                    evidence = _activity_preservation(proposed, parent)
                    self.assertFalse(evidence['supported'])
                    self.assertTrue(evidence['worsened'] if value == 1 and value is not True else evidence['unknown'])

    def test_activity_below_target_is_allowed_if_deficit_does_not_grow(self):
        parent = joint_qualification(paths(gross=0), progress(), LIMITS['budget'])
        self.assertTrue(_activity_preservation(parent, parent)['supported'])
        proposed = deepcopy(parent)
        proposed['scenarios'][2]['deficits']['gross_amount'] += 100
        self.assertTrue(_activity_preservation(proposed, parent)['supported'])  # reserve is separately audited

    def test_return_objective_equal_weights_all_twelve_core_paths_only(self):
        value = paths()
        expected = []
        for index, row in enumerate(value['scenarios']):
            row['final_equity'] = 10000 + index if row['environment'] in {'normal', 'cost_stress'} else 999999
            if row['environment'] in {'normal', 'cost_stress'}:
                expected.append(row['final_equity'])
        result = _return_objective(value, 'ready')
        self.assertEqual(result, math.fsum(expected) / 12)
        value['scenarios'].reverse()
        self.assertEqual(_return_objective(value, 'ready'), result)
        self.assertIsNone(_return_objective(value, 'scenario_coverage_unknown'))

    def test_candidate_and_path_order_do_not_change_id_tie_break(self):
        parent, first, second = self.no_signal_parent(), candidate(maximum=20), candidate(maximum=19)
        expected = min(policy_id(first), policy_id(second))
        for policies in ([first, second], [second, first]):
            for reverse in (False, True):
                def scenarios(policy, *args, **kwargs):
                    value = paths(gross=0, equity=9900 if policy == parent else 9950)
                    if reverse:
                        value['scenarios'].reverse()
                    return value
                selected, audit = self.choose(parent, policies, [outcome(), outcome()], scenarios)
                self.assertEqual(policy_id(policies[selected]), expected)
                self.assertEqual(audit['selected_objective_value'], 9950)

    def test_only_existing_nav_epsilon_separates_improvement_from_incumbent_tie(self):
        parent = self.no_signal_parent()
        for improvement in (0, NAV_EPSILON / 2, NAV_EPSILON * 2):
            selected, audit = self.choose(parent, [candidate(maximum=20)], [outcome()],
                lambda policy, *args, **kwargs: paths(gross=0, equity=9900 if policy == parent else 9900 + improvement))
            self.assertEqual(selected, 0 if improvement > NAV_EPSILON else None)

    def test_actual_account_protection_stops_selection_before_scenario_replay(self):
        frame, context = self.setup_context()
        with patch('backend.policy_award.transition_preview', return_value={
                'status': 'ready', 'protection': {'active': True}}), patch(
                'backend.policy_award_scenarios.award_scenarios') as replay:
            selected, audit = select_award_record(self.no_signal_parent(), [], [frame], LIMITS, context)
        self.assertIsNone(selected)
        self.assertEqual(audit['reason'], 'account_protection_active')
        replay.assert_not_called()

    def test_paired_drawdown_cap_cannot_be_exceeded_even_with_high_objective(self):
        selected, audit = self.choose(self.no_signal_parent(), [candidate(maximum=20)], [outcome(drawdown=15.1)])
        self.assertIsNone(selected)
        self.assertEqual(audit['outcomes'][0]['promotion_reason'], 'drawdown_limit')

    def test_no_new_entry_or_later_buy_does_not_veto_valid_return_challenge(self):
        parent = candidate(maximum=20)
        selected, audit = self.choose(parent, [candidate(maximum=6)], [outcome()],
            lambda policy, *args, **kwargs: paths(gross=500000000,
                equity=9900 if policy == parent else 9950, first_buy=None))
        self.assertEqual(selected, 0)
        detail = audit['outcomes'][0]
        self.assertEqual(detail['transition']['status'], 'ready')
        self.assertEqual(detail['transition']['entry_count'], 0)
        self.assertEqual(detail['transition_class'], 'no_entry_evidence')
        self.assertTrue(detail['activity_preservation']['supported'])
        self.assertTrue(detail['core_witness_preservation']['supported'])
        self.assertTrue(detail['relative_risk_evidence']['supported'])
        self.assertEqual(audit['reason'], 'return_challenge')
        self.assertEqual(audit['objective_improvement'], 50)
        self.assertFalse(audit['award_plan_supported'])
        self.assertTrue(audit['qualification_risk'])


if __name__ == "__main__":
    unittest.main()
