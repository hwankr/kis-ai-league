"""Daily provisional selection with synthetic data; no broker or storage calls."""
from copy import deepcopy
import json
import math
import unittest
from unittest.mock import Mock, patch

from backend.daily_evaluation import VERSION, evaluate_daily_candidate, select_daily_candidate
from backend.learning_evaluation import daily_metrics
from backend.learning_policy import BASELINE_POLICY, LEGACY_IDS, portfolio_metrics
from tests.test_learning_policy import LIMITS, frames, policy


def changed(parent=None):
    result = deepcopy(parent or policy())
    result["strategies"][0]["holding_sessions"] = 4
    return {"policy": result}


def evidence(normal, stress=None):
    return daily_metrics(normal, stress, first_day="2026-07-01")


def observed_frames(count):
    observations = frames(count)
    for frame in observations:
        frame["captured_at"] = frame["observed_at"]
        frame["baseline_signals"] = [{"strategy_id": identity, "symbol": "005930", "action": "hold", "status": "ready"}
                                     for identity in LEGACY_IDS]
    return observations


class DailyEvaluationTests(unittest.TestCase):
    def evaluate(self, a, b, *, parent=None, proposal=None, observations=None):
        parent = parent or policy()
        return evaluate_daily_candidate(parent, proposal or changed(parent), observations or frames(len(a["daily_log_returns"]) + 1),
                                        LIMITS, evaluator=Mock(side_effect=[a, b]))

    def test_two_intervals_can_support_provisional_change_without_closed_trades(self):
        a, b = evidence([0., 0.]), evidence([.001, .002], [.0008, .0018])
        b["closed_trades"] = 0
        result = self.evaluate(a, b)
        self.assertEqual(result["decision"], "apply")
        self.assertEqual(result["version"], VERSION)
        self.assertEqual(result["metrics"]["basis"], "development_only")
        self.assertEqual(result["metrics"]["intervals"], 2)
        self.assertEqual(result["metrics"]["recent_intervals"], 1)
        self.assertFalse(result["metrics"]["loss_reduction_only"])
        self.assertNotIn("p_value", result["metrics"])
        self.assertEqual(result["changes"][0]["path"], "strategies[0].holding_sessions")

    def test_one_return_interval_is_sufficient_for_provisional_comparison(self):
        result = self.evaluate(evidence([0.]), evidence([.001]))
        self.assertEqual(result["decision"], "apply")
        self.assertEqual(result["metrics"]["intervals"], 1)
        self.assertEqual(result["metrics"]["recent_intervals"], 1)

    def test_improved_but_still_losing_policy_is_explicitly_provisional(self):
        result = self.evaluate(evidence([-.02] * 6), evidence([-.005] * 6, [-.006] * 6))
        self.assertEqual(result["decision"], "apply")
        self.assertTrue(result["metrics"]["loss_reduction_only"])
        self.assertIn("손실 축소", result["reason"])
        self.assertLess(result["metrics"]["candidate"]["net_return_pct"], 0)

    def test_best_candidate_with_no_parent_advantage_keeps_current(self):
        result = self.evaluate(evidence([.005] * 6), evidence([.003] * 6))
        self.assertEqual(result["decision"], "keep")
        self.assertFalse(result["metrics"]["checks"]["normal_improvement"])

    def test_normal_gain_cannot_hide_stress_cost_loss(self):
        result = self.evaluate(evidence([.002] * 6), evidence([.003] * 6, [.001] * 6))
        self.assertEqual(result["decision"], "keep")
        self.assertTrue(result["metrics"]["checks"]["normal_improvement"])
        self.assertFalse(result["metrics"]["checks"]["stress_improvement"])

    def test_relative_drawdown_worsening_is_diagnostic_within_absolute_cap(self):
        result = self.evaluate(evidence([.001] * 6), evidence([.003] * 6, [-.005] + [.004] * 5))
        self.assertEqual(result["decision"], "apply")
        self.assertTrue(result["metrics"]["checks"]["stress_improvement"])
        self.assertFalse(result["metrics"]["checks"]["drawdown_not_worse"])
        self.assertTrue(result["metrics"]["checks"]["drawdown_within_limit"])

    def test_high_return_cannot_bypass_either_cost_drawdown_cap(self):
        for stress in (False, True):
            bad = [math.log(1.3), math.log(.84), math.log(1.3)]
            normal = [.01, .01, .01] if stress else bad
            result = self.evaluate(evidence([0.] * 3), evidence(normal, bad))
            self.assertEqual(result["decision"], "keep")
            self.assertTrue(result["metrics"]["checks"]["normal_improvement"])
            self.assertFalse(result["metrics"]["checks"]["drawdown_within_limit"])

    def test_frozen_book_uses_its_own_anchor_and_same_model_for_both_policies(self):
        observations = frames(4)
        context = {"initial_state": {"as_of": observations[1]["as_of"], "cash": "10000", "positions": []},
                   "execution_model": {"mode": "daily_open", "buy_fill_ratio": .5},
                   "observed_at": observations[1]["observed_at"]}
        original = deepcopy(context)
        calls = []
        def evaluate(candidate, supplied, limits, **options):
            calls.append(deepcopy((supplied, options)))
            options["initial_state"]["cash"] = "1"
            return daily_metrics([0., 0.] if len(calls) == 1 else [.001, .001], first_day=supplied[0]["as_of"])
        result = evaluate_daily_candidate(policy(), changed(), observations, LIMITS,
                                          evaluator=evaluate, evaluation_context=context)
        self.assertEqual(result["decision"], "apply")
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0][0], observations[1:])
        self.assertEqual(result["metrics"]["replay_basis"], "frozen_book_execution_model")
        self.assertEqual(context, original)

    def test_current_book_cannot_be_inserted_into_older_comparison(self):
        observations = frames(4)
        context = {"initial_state": {"as_of": observations[-1]["as_of"], "cash": "10000"},
                   "execution_model": {"mode": "daily_open"}, "observed_at": observations[-1]["observed_at"]}
        for kind in ("today", "late_capture", "missing_anchor", "pending"):
            supplied = deepcopy(context)
            if kind == "late_capture":
                supplied["initial_state"]["as_of"] = observations[0]["as_of"]
            elif kind == "missing_anchor":
                supplied["initial_state"]["as_of"] = "2020-01-01"
            elif kind == "pending":
                supplied["initial_state"]["pending_orders"] = ["unresolved"]
            evaluate = Mock()
            result = evaluate_daily_candidate(policy(), changed(), observations, LIMITS,
                                              evaluator=evaluate, evaluation_context=supplied)
            self.assertEqual(result["decision"], "blocked", kind)
            evaluate.assert_not_called()

    def test_comparison_cap_cannot_weaken_fifteen_percent_protection(self):
        for value in (15.1, 0, True, float("nan")):
            evaluate = Mock()
            result = evaluate_daily_candidate(policy(), changed(), frames(3), LIMITS,
                evaluator=evaluate, evaluation_context={"maximum_drawdown_pct": value})
            self.assertEqual(result["decision"], "blocked")
            evaluate.assert_not_called()

    def test_all_execution_scenarios_must_pass_instead_of_representative_only(self):
        observations = frames(4)
        context = {"initial_state": {"as_of": observations[0]["as_of"], "cash": "10000"},
                   "execution_model": {"scenarios": [{"id": "good"}, {"id": "bad"}]},
                   "observed_at": observations[0]["observed_at"]}
        parent = evidence([0.] * 3)
        candidate = evidence([.002] * 3)
        parent["scenario_results"] = {"good": evidence([0.] * 3), "bad": evidence([0.] * 3)}
        candidate["scenario_results"] = {"good": evidence([.002] * 3), "bad": evidence([-.001] * 3)}
        result = evaluate_daily_candidate(policy(), changed(), observations, LIMITS,
            evaluator=Mock(side_effect=[parent, candidate]), evaluation_context=context)
        self.assertEqual(result["decision"], "keep")
        self.assertTrue(result["metrics"]["checks"]["normal_improvement"])
        self.assertFalse(result["metrics"]["scenario_comparisons"]["bad"]["eligible"])

    def test_pool_selects_best_eligible_candidate_and_its_own_metadata(self):
        parent, first, second = policy(name="parent"), changed()["policy"], changed()["policy"]
        first["name"], second["name"] = "A", "B"
        second["strategies"][0]["holding_sessions"] = 6
        pool = [{"policy": first, "method": "llm_development", "development": {"score": 8}},
                {"policy": second, "method": "grammar_development", "development": {"score": 3}}]
        proposal = {**pool[0], "candidate_pool": pool}
        originals = deepcopy((parent, proposal))
        outcomes = {"parent": evidence([0., 0., 0.]),
                    "A": evidence([math.log(1.3), math.log(.8), math.log(1.3)]),
                    "B": evidence([.02, -.0025, .02])}
        calls = []
        def evaluate(parent, item, observations, limits):
            calls.append(item["policy"]["name"])
            return evaluate_daily_candidate(parent, item, observations, limits,
                evaluator=lambda candidate, *_: deepcopy(outcomes[candidate["name"]]))
        selected, result = select_daily_candidate(parent, proposal, frames(4), LIMITS, evaluator=evaluate)
        self.assertEqual(calls, ["A", "B"])
        self.assertEqual(result["decision"], "apply")
        self.assertEqual(selected["policy"], second)
        self.assertEqual(selected["method"], "grammar_development")
        self.assertEqual(selected["development"], {"score": 3})
        self.assertEqual(result["metrics"]["selection"]["candidates_evaluated"], 2)
        self.assertEqual(result["metrics"]["selection"]["eligible_candidates"], 1)
        self.assertFalse(result["metrics"]["selection"]["prospective"])
        self.assertEqual((parent, proposal), originals)

    def test_equal_returns_choose_same_policy_when_candidate_pool_order_changes(self):
        a, b = changed()["policy"], changed()["policy"]
        b["strategies"][0]["holding_sessions"] = 6
        first = {"policy": a, "method": "llm_development"}
        second = {"policy": b, "method": "grammar_development"}
        def evaluate(*args):
            return {"decision": "apply", "metrics": {"selection_return_pct": 2., "selection_stress_return_pct": 1.5}}
        winners = []
        for pool in ([first, second], [second, first]):
            selected, _ = select_daily_candidate(policy(), {**first, "candidate_pool": pool}, frames(3), LIMITS,
                                                 evaluator=evaluate)
            winners.append(selected["policy"])
        self.assertEqual(winners[0], winners[1])

    def test_return_challenge_uses_selected_pool_metadata_and_keeps_qualification_warning(self):
        from backend.learning_policy import policy_id
        first, second = changed(), changed()
        second["policy"]["strategies"][0]["holding_sessions"] = 6
        first["method"], second["method"] = "first", "second"
        pool = [first, second]
        award = {"status": "apply", "reason": "return_challenge", "eligible_candidates": 1,
                 "return_objective": {"statistic": "core_mean_final_equity", "probability": False},
                 "award_plan_supported": False, "qualification_risk": True,
                 "outcomes": [{"candidate_id": policy_id(item["policy"])} for item in pool]}
        # The valid daily comparison can be keep while the independently
        # qualified award objective supports a provisional return challenge.
        evaluate = Mock(return_value={"decision": "keep", "metrics": {}})
        with patch("backend.policy_award.select_award_record", return_value=(1, award)):
            selected, result = select_daily_candidate(policy(), {**first, "candidate_pool": pool},
                frames(3), LIMITS, evaluator=evaluate, award_context={"competition_context": {}})
        self.assertEqual(evaluate.call_count, 2)
        self.assertEqual(selected["method"], "second")
        self.assertEqual(result["decision"], "apply")
        self.assertIn("수상요건 미달 위험", result["reason"])
        selection = result["metrics"]["selection"]
        self.assertEqual(selection["selected_candidate_id"], policy_id(second["policy"]))
        self.assertFalse(selection["prospective"])
        self.assertFalse(result["metrics"]["award_selection"]["award_plan_supported"])
        self.assertEqual(selection["return_objective"], award["return_objective"])
        selection["return_objective"]["probability"] = True
        self.assertFalse(award["return_objective"]["probability"])

    def test_return_selector_keep_cannot_fall_back_to_daily_apply_candidate(self):
        from backend.learning_policy import policy_id
        award = {"status": "keep", "reason": "no_supported_return_improvement",
                 "eligible_candidates": 0, "return_objective": {}, "outcomes": []}
        evaluate = Mock(return_value={"decision": "apply", "metrics": {"selection_return_pct": 100}})
        with patch("backend.policy_award.select_award_record", return_value=(None, award)):
            _, result = select_daily_candidate(policy(), changed(), frames(3), LIMITS,
                evaluator=evaluate, award_context={"competition_context": {}})
        self.assertEqual(result["decision"], "keep")
        self.assertIn("현행 유지", result["reason"])
        self.assertTrue(result["metrics"]["selection"]["incumbent_selected"])
        self.assertEqual(result["metrics"]["selection"]["selected_candidate_id"], policy_id(policy()))

    def test_recent_relative_reversal_is_a_diagnostic_not_an_extra_gate(self):
        result = self.evaluate(evidence([.002] * 6), evidence([.004] * 4 + [.001] * 2))
        self.assertEqual(result["decision"], "apply")
        self.assertTrue(result["metrics"]["checks"]["stress_improvement"])
        self.assertFalse(result["metrics"]["checks"]["recent_improvement"])

    def test_recent_window_is_capped_at_twenty_intervals(self):
        result = self.evaluate(evidence([0.] * 90), evidence([.001] * 90))
        self.assertEqual(result["decision"], "apply")
        self.assertEqual(result["metrics"]["recent_intervals"], 20)

    def test_identical_or_cosmetically_changed_policy_needs_no_evaluation(self):
        parent = policy()
        same = deepcopy(parent)
        same["name"] = "another name"
        same["strategies"][0]["id"] = "another-id"
        same["strategies"][0]["name"] = "another strategy name"
        evaluate = Mock(side_effect=AssertionError("no evaluation needed"))
        result = evaluate_daily_candidate(parent, {"policy": same}, frames(3), LIMITS, evaluator=evaluate)
        self.assertEqual(result["decision"], "keep")
        self.assertEqual(result["changes"], [])
        evaluate.assert_not_called()

    def test_redundant_conditions_do_not_fake_behavioral_change(self):
        parent = policy()
        parent["strategies"][0]["conditions"] = [{"feature": "return_5d_pct", "op": "gt", "value": 2}]
        candidate = deepcopy(parent)
        candidate["strategies"][0]["conditions"].append({"feature": "return_5d_pct", "op": "gt", "value": 1})
        result = evaluate_daily_candidate(parent, {"policy": candidate}, frames(3), LIMITS)
        self.assertEqual(result["decision"], "keep")

    def test_multiple_families_and_strategy_slot_changes_are_blocked(self):
        for kind in ("families", "identity", "cancel", "limits"):
            proposal = changed()
            if kind == "families":
                proposal["policy"]["strategies"][0]["top_n"] = 2
            elif kind == "identity":
                proposal["policy"]["strategies"][0]["id"] = "different-slot"
            elif kind == "cancel":
                proposal["policy"]["cancel_after_minutes"] = 20
            else:
                proposal["limits"] = {**LIMITS, "budget": "20000"}
            evaluate = Mock()
            with self.subTest(kind=kind):
                result = evaluate_daily_candidate(policy(), proposal, frames(3), LIMITS, evaluator=evaluate)
                self.assertEqual(result["decision"], "blocked")
                evaluate.assert_not_called()

    def test_legacy_bootstrap_accepts_first_rules_policy_but_not_unmodeled_cancel_change(self):
        result = self.evaluate(evidence([0.] * 3), evidence([.001] * 3),
                               parent=BASELINE_POLICY, proposal={"policy": policy()}, observations=observed_frames(4))
        self.assertEqual(result["decision"], "apply")
        self.assertEqual(result["metrics"]["change_family"], "bootstrap")
        candidate = policy(cancel_after_minutes=20)
        result = evaluate_daily_candidate(BASELINE_POLICY, {"policy": candidate}, frames(4), LIMITS)
        self.assertEqual(result["decision"], "blocked")

    def test_legacy_historical_signals_are_not_invented_or_treated_as_cash(self):
        for kind in ("missing", "historical", "missing_strategy", "unknown", "naive_time", "no_capture"):
            observations = observed_frames(5)
            for frame in observations:
                if kind == "missing":
                    frame["baseline_signals"] = []
                elif kind == "historical":
                    frame["role"] = "development_only"
                elif kind == "missing_strategy":
                    frame["baseline_signals"].pop()
                elif kind == "unknown":
                    frame["baseline_signals"][-1]["status"] = "error"
                elif kind == "naive_time":
                    frame["captured_at"] = frame["as_of"] + "T16:05:00"
                else:
                    frame.pop("captured_at")
            evaluate = Mock()
            with self.subTest(kind=kind):
                result = evaluate_daily_candidate(BASELINE_POLICY, {"policy": policy()}, observations, LIMITS, evaluator=evaluate)
                self.assertEqual(result["decision"], "blocked")
                self.assertEqual(result["metrics"]["error"], "insufficient_observed_baseline")
                self.assertEqual(result["metrics"]["observed_baseline_frames"], 0)
                evaluate.assert_not_called()

    def test_legacy_comparison_uses_only_continuous_observed_suffix_for_both_policies(self):
        observations = observed_frames(7)
        observations[3]["baseline_signals"].pop()
        original = deepcopy(observations)
        records = []
        def evaluate(candidate, supplied, limits):
            records.append(deepcopy(supplied))
            return daily_metrics([0., 0.] if candidate["kind"] == "legacy" else [.001, .001],
                                 first_day=supplied[0]["as_of"])
        result = evaluate_daily_candidate(BASELINE_POLICY, {"policy": policy()}, observations, LIMITS, evaluator=evaluate)
        self.assertEqual(result["decision"], "apply")
        self.assertEqual(result["metrics"]["intervals"], 2)
        self.assertEqual(result["metrics"]["excluded_frames"], 4)
        self.assertEqual(result["metrics"]["start_day"], observations[4]["as_of"])
        self.assertEqual(records, [observations[4:], observations[4:]])
        self.assertEqual(observations, original)

    def test_latest_legacy_signal_gap_cannot_select_an_older_favorable_window(self):
        observations = observed_frames(7)
        observations[-1]["baseline_signals"] = []
        result = evaluate_daily_candidate(BASELINE_POLICY, {"policy": policy()}, observations, LIMITS)
        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(result["metrics"]["observed_baseline_frames"], 0)

    def test_missing_exchange_session_starts_a_new_observed_suffix(self):
        observations = observed_frames(6)
        del observations[2]
        calls = []
        def evaluate(candidate, supplied, limits):
            calls.append(deepcopy(supplied))
            return daily_metrics([0., 0.] if candidate["kind"] == "legacy" else [.001, .001],
                                 first_day=supplied[0]["as_of"])
        result = evaluate_daily_candidate(BASELINE_POLICY, {"policy": policy()}, observations, LIMITS, evaluator=evaluate)
        self.assertEqual(result["decision"], "apply")
        self.assertEqual(result["metrics"]["excluded_frames"], 2)
        self.assertEqual(result["metrics"]["observed_baseline_frames"], 3)
        self.assertEqual(calls, [observations[2:], observations[2:]])

    def test_legacy_signal_captured_at_or_after_following_open_cannot_form_an_interval(self):
        for clock in ("09:00:00+09:00", "09:00:01+09:00", "00:00:00+00:00"):
            observations = observed_frames(2)
            observations[0]["captured_at"] = observations[1]["as_of"] + "T" + clock
            evaluate = Mock()
            with self.subTest(clock=clock):
                result = evaluate_daily_candidate(BASELINE_POLICY, {"policy": policy()}, observations, LIMITS,
                                                  evaluator=evaluate)
                self.assertEqual(result["decision"], "blocked")
                self.assertEqual(result["metrics"]["observed_baseline_frames"], 1)
                self.assertEqual(result["metrics"]["error"], "insufficient_observed_baseline")
                evaluate.assert_not_called()

    def test_late_legacy_signal_discards_only_the_unavailable_prefix(self):
        observations = observed_frames(4)
        observations[1]["captured_at"] = observations[2]["as_of"] + "T09:01:00+09:00"
        calls = []
        def evaluate(candidate, supplied, limits):
            calls.append(deepcopy(supplied))
            return daily_metrics([0.] if candidate["kind"] == "legacy" else [.001], first_day=supplied[0]["as_of"])
        result = evaluate_daily_candidate(BASELINE_POLICY, {"policy": policy()}, observations, LIMITS, evaluator=evaluate)
        self.assertEqual(result["decision"], "apply")
        self.assertEqual(result["metrics"]["excluded_frames"], 2)
        self.assertEqual(calls, [observations[2:], observations[2:]])

    def test_legacy_signal_captured_before_next_open_including_weekend_remains_usable(self):
        for captured in ("2026-10-10T16:00:00+09:00", "2026-10-12T08:59:59+09:00"):
            observations = observed_frames(2)
            days = ["2026-10-08", "2026-10-12"]
            for index, frame in enumerate(observations):
                frame.update(as_of=days[index], observed_at=days[index] + "T16:05:00+09:00",
                             captured_at=days[index] + "T16:05:00+09:00", session_days=days[:index + 1])
            observations[0]["captured_at"] = captured
            def evaluate(candidate, supplied, limits):
                result = evidence([0.] if candidate["kind"] == "legacy" else [.001])
                for key in ("equity_curve", "stress_equity_curve"):
                    for point, day in zip(result[key], days):
                        point["as_of"] = day
                return result
            with self.subTest(captured=captured):
                result = evaluate_daily_candidate(BASELINE_POLICY, {"policy": policy()}, observations, LIMITS,
                                                  evaluator=evaluate)
                self.assertEqual(result["decision"], "apply")
                self.assertEqual(result["metrics"]["excluded_frames"], 0)

    def test_legacy_one_observation_cannot_form_a_return_interval(self):
        observations = observed_frames(5)
        observations[-2]["role"] = "development_only"
        result = evaluate_daily_candidate(BASELINE_POLICY, {"policy": policy()}, observations, LIMITS)
        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(result["metrics"]["observed_baseline_frames"], 1)
        self.assertIn("연속 관측 2개", result["reason"])

    def test_two_observed_legacy_frames_are_enough_to_compare(self):
        result = self.evaluate(evidence([0.]), evidence([.001]), parent=BASELINE_POLICY,
                               proposal={"policy": policy()}, observations=observed_frames(2))
        self.assertEqual(result["decision"], "apply")
        self.assertEqual(result["metrics"]["intervals"], 1)

    def test_rule_parent_still_uses_development_frames_without_legacy_decisions(self):
        observations = frames(4)
        for frame in observations:
            frame["role"] = "development_only"
        result = self.evaluate(evidence([0.] * 3), evidence([.001] * 3), observations=observations)
        self.assertEqual(result["decision"], "apply")
        self.assertEqual(result["metrics"]["intervals"], 3)

    def test_limits_are_identical_and_all_inputs_are_immutable(self):
        parent, proposal, observations, limits = policy(), changed(), frames(3), deepcopy(LIMITS)
        originals = deepcopy((parent, proposal, observations, limits))
        calls = []
        def evaluate(candidate, supplied, supplied_limits):
            calls.append(deepcopy((supplied, supplied_limits)))
            supplied.clear()
            supplied_limits["budget"] = "1"
            candidate.clear()
            return evidence([0., 0.] if len(calls) == 1 else [.001, .001])
        first = evaluate_daily_candidate(parent, proposal, observations, limits, evaluator=evaluate)
        self.assertEqual(first["decision"], "apply")
        self.assertEqual(calls[0], calls[1])
        self.assertEqual((parent, proposal, observations, limits), originals)
        json.dumps(first, allow_nan=False)

    def test_naive_insufficient_or_invalid_sources_block(self):
        cases = [[], frames(1)]
        unordered = frames(3)
        unordered[1]["as_of"] = unordered[0]["as_of"]
        cases.append(unordered)
        nonfinite = frames(3)
        nonfinite[0]["rows"][0]["features"]["turnover"] = float("nan")
        cases.append(nonfinite)
        for observations in cases:
            with self.subTest(observations=observations):
                self.assertEqual(evaluate_daily_candidate(policy(), changed(), observations, LIMITS)["decision"], "blocked")

    def test_unpaired_nonfinite_or_inconsistent_evaluator_output_blocks(self):
        for kind in ("dates", "length", "return", "equity", "summary", "summary_nan", "invalid", "extra_nan"):
            a, b = evidence([0.] * 3), evidence([.001] * 3)
            if kind == "dates":
                b = daily_metrics([.001] * 3, first_day="2026-07-02")
            elif kind == "length":
                b["stress_daily_log_returns"].pop()
            elif kind == "return":
                b["daily_log_returns"][1] = .5
            elif kind == "equity":
                b["equity_curve"][1]["equity"] = float("inf")
            elif kind == "summary":
                b["net_return_pct"] = 1000
            elif kind == "summary_nan":
                b["net_return_pct"] = float("nan")
            elif kind == "invalid":
                b["valid"] = False
            else:
                b["diagnostics"] = {"unused": float("nan")}
            with self.subTest(kind=kind):
                result = self.evaluate(a, b)
                self.assertEqual(result["decision"], "blocked")
                json.dumps(result, allow_nan=False)

    def test_evaluator_failure_is_blocked_without_exception_details(self):
        result = evaluate_daily_candidate(policy(), changed(), frames(3), LIMITS,
                                          evaluator=Mock(side_effect=RuntimeError("private debug information")))
        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(result["metrics"]["error"], "evaluation_failed")
        self.assertNotIn("private", json.dumps(result))

    def test_series_validation_errors_are_preserved_for_either_policy(self):
        errors = ("invalid_portfolio_evaluation", "invalid_daily_return", "missing_daily_returns",
                  "interval_count_mismatch", "missing_evaluation_dates", "invalid_evaluation_dates",
                  "cost_scenarios_not_aligned")
        for error in errors:
            for side in (0, 1):
                outputs = [evidence([0.]), evidence([.001])]
                raw = outputs[side]
                if error == "invalid_portfolio_evaluation":
                    raw["valid"] = False
                elif error == "invalid_daily_return":
                    raw["daily_log_returns"][0] = "private value"
                elif error == "missing_daily_returns":
                    raw["stress_daily_log_returns"] = []
                elif error == "interval_count_mismatch":
                    raw["intervals"] = 2
                elif error == "missing_evaluation_dates":
                    raw["equity_curve"].pop()
                elif error == "invalid_evaluation_dates":
                    raw["equity_curve"][1]["as_of"] = raw["equity_curve"][0]["as_of"]
                else:
                    raw["stress_equity_curve"][1]["as_of"] = "2026-07-03"
                with self.subTest(error=error, side=side):
                    result = self.evaluate(*outputs)
                    self.assertEqual(result["decision"], "blocked")
                    self.assertEqual(result["metrics"]["error"], error)
                    self.assertNotIn("private", json.dumps(result, allow_nan=False))

    def test_failed_replay_exposes_only_allowlisted_errors(self):
        safe = {"protection_quote_missing", "protection_quote_after_decision"}
        for error in (*sorted(safe), "private token=secret", "private_secret", ["private secret"]):
            for side in (0, 1):
                outputs = [evidence([0.]), evidence([.001])]
                outputs[side].update(valid=False, error=error)
                with self.subTest(error=error, side=side):
                    result = self.evaluate(*outputs)
                    expected = error if isinstance(error, str) and error in safe else "invalid_portfolio_evaluation"
                    self.assertEqual(result["decision"], "blocked")
                    self.assertEqual(result["metrics"]["error"], expected)
                    self.assertNotIn("private", json.dumps(result, allow_nan=False))

    def test_scenario_diagnostics_survive_unknown_primary_and_unpaired_results(self):
        observations = frames(2)
        context = {"initial_state": {"as_of": observations[0]["as_of"], "cash": "10000"},
                   "execution_model": {"scenarios": [{"id": "base"}, {"id": "adverse"}]},
                   "observed_at": observations[0]["observed_at"]}
        outcomes = [("private first", {"valid": False, "error": "private token=secret"}),
                    ("private missing", {"valid": False, "error": "protection_quote_missing"}),
                    ("private late", {"valid": False, "error": "protection_quote_after_decision"})]
        for valid in (False, True):
            for side in (0, 1):
                for reverse in (False, True):
                    outputs = [evidence([0.]), evidence([.001])]
                    outputs[1 - side]["scenario_results"] = {"base": evidence([0.]), "adverse": evidence([0.])}
                    outputs[side].update(valid=valid, error="private top-level",
                        scenario_results=dict(reversed(outcomes) if reverse else outcomes))
                    with self.subTest(valid=valid, side=side, reverse=reverse):
                        result = evaluate_daily_candidate(policy(), changed(), observations, LIMITS,
                            evaluator=Mock(side_effect=outputs), evaluation_context=context)
                        self.assertEqual(result["decision"], "blocked")
                        self.assertEqual(result["metrics"]["error"],
                            "unpaired_execution_scenarios" if valid else "invalid_portfolio_evaluation")
                        self.assertEqual(result["metrics"]["replay_diagnostics"], {
                            "stage": "portfolio_replay",
                            "codes": ["protection_quote_after_decision", "protection_quote_missing"]})
                        self.assertNotIn("private", json.dumps(result, allow_nan=False))

    def test_valid_replay_result_is_unchanged_by_unused_error_field(self):
        outputs = [evidence([0.]), evidence([.001])]
        expected = self.evaluate(*outputs)
        outputs[0]["error"] = "protection_quote_missing"
        outputs[1]["error"] = "private token=secret"
        self.assertEqual(self.evaluate(*outputs), expected)

    def test_two_frame_protected_replay_requires_quotes_in_every_scenario(self):
        from backend.learning_account import execution_profile
        observations = frames(2)
        for frame in observations:
            frame["rows"][0]["learning_eligible"] = False
        context = {
            "initial_state": {
                "as_of": observations[0]["as_of"], "cash": "9000", "positions": [
                    {"symbol": "005930", "strategy_id": "momentum", "quantity": 10,
                     "cost": "1000", "entry_price": "100", "last_close": "100", "held_sessions": 0,
                     "exit_policy": {"holding_sessions": 5, "stop_loss_pct": 0, "take_profit_pct": 0}}],
                "account_protection": {"units": "10000", "peak": "1", "active": False, "limit_pct": 15}},
            "execution_model": execution_profile([]),
            "observed_at": observations[0]["observed_at"]}
        before = deepcopy((observations, context, LIMITS))
        blocked = evaluate_daily_candidate(policy(), changed(), observations, LIMITS, evaluation_context=context)
        self.assertEqual(blocked["decision"], "blocked")
        self.assertEqual(blocked["metrics"]["error"], "protection_quote_missing")
        self.assertEqual((observations, context, LIMITS), before)

        observations[1]["execution_quotes"] = {"005930": {
            "price": 100, "volume": 1000, "eligible": True,
            "observed_at": observations[1]["as_of"] + "T09:10:00+09:00"}}
        before = deepcopy((observations, context, LIMITS))
        result = evaluate_daily_candidate(policy(), changed(), observations, LIMITS, evaluation_context=context)
        self.assertEqual(result["decision"], "keep")
        self.assertNotIn("error", result["metrics"])
        self.assertEqual(result["metrics"]["intervals"], 1)
        self.assertTrue(result["metrics"]["account_protection_inherited"])
        self.assertEqual(result["metrics"]["maximum_drawdown_pct"], 15)
        self.assertEqual(set(result["metrics"]["scenario_comparisons"]),
                         {"partial_fill", "delayed_sell", "adverse_buy"})
        for comparison in result["metrics"]["scenario_comparisons"].values():
            self.assertEqual(comparison["champion"], comparison["candidate"])
            self.assertFalse(comparison["eligible"])
        self.assertEqual((observations, context, LIMITS), before)

        for frame in observations:
            frame["rows"].append({**deepcopy(frame["rows"][0]), "symbol": "000660", "learning_eligible": True})
        observations[1]["execution_quotes"]["000660"] = {
            "price": 100, "volume": 1000, "eligible": True,
            "observed_at": observations[1]["as_of"] + "T09:05:00+09:00"}
        before = deepcopy((observations, context, LIMITS))
        blocked = evaluate_daily_candidate(policy(), changed(), observations, LIMITS, evaluation_context=context)
        self.assertEqual(blocked["decision"], "blocked")
        self.assertEqual(blocked["metrics"]["error"], "protection_quote_after_decision")
        self.assertEqual(blocked["metrics"]["replay_diagnostics"], {
            "stage": "portfolio_replay", "codes": ["protection_quote_after_decision"]})
        self.assertEqual((observations, context, LIMITS), before)

    def test_real_replay_uses_same_costs_and_does_not_require_60_sessions(self):
        observations = observed_frames(5)
        for index, frame in enumerate(observations):
            bar = frame["rows"][0]
            bar.update(open=100 + max(0, index - 1) * 5, close=100 + index * 5,
                       low=99 + max(0, index - 1) * 5, high=101 + index * 5)
        result = evaluate_daily_candidate(BASELINE_POLICY, {"policy": policy()}, observations, LIMITS)
        self.assertEqual(result["decision"], "apply")
        actual = portfolio_metrics(policy(), observations, LIMITS)
        self.assertAlmostEqual(result["metrics"]["candidate"]["net_return_pct"], actual["net_return_pct"])
        self.assertLess(result["metrics"]["candidate"]["stress_return_pct"], result["metrics"]["candidate"]["net_return_pct"])

    def test_rounding_noise_does_not_become_an_improvement(self):
        result = self.evaluate(evidence([.001] * 3), evidence([.001 + 1e-14] * 3))
        self.assertEqual(result["decision"], "keep")


if __name__ == "__main__":
    unittest.main()
