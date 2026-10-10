"""Synthetic paired attribution checks; no external market or model calls."""
from copy import deepcopy
import json
import unittest

from backend.learning_attribution import assess_hypothesis, attribute_trial, cash_quote_symbols, CONTRASTS, EFFECTS, VERSION
from backend.learning_evaluation import daily_metrics, make_evaluation_spec
from backend.learning_policy import BASELINE_POLICY
from tests.test_learning_research import LIMITS, rule


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.spec = make_evaluation_spec(1, bootstrap_repetitions=199, scenario_ids=("frozen",))
        self.frames = daily_metrics([0.] * self.spec["horizon"])["equity_curve"]
        self.trial = {"incumbent": deepcopy(BASELINE_POLICY), "policy": rule(), "limits": LIMITS,
            "initial_state": {"as_of": self.frames[0]["as_of"], "cash": "900000", "positions": []},
            "execution_model": {"status": "estimated", "scenarios": [{"id": "frozen"}]},
            "evaluation_spec": self.spec, "decision": "keep"}

    def evaluate(self, values, *, stress=None, scenario=None, supported=True, decision="keep"):
        calls = []
        def series(value):
            return [value] * self.spec["horizon"] if isinstance(value, (int, float)) else value
        def evaluator(policy, frames, limits, *, execution_model, initial_state):
            key = f'D{int(execution_model.get("mode") != "daily_open")}{int(initial_state is not None)}'
            calls.append((key, policy["kind"], frames, limits))
            candidate = policy["kind"] == "rules"
            normal = series(values[key]) if candidate else series(0.)
            costly = series((stress or values)[key]) if candidate else series(0.)
            result = daily_metrics(normal, costly)
            if key[1] == "1":
                result["promotion_supported"] = supported
                result["scenario_results"] = {"frozen": daily_metrics(series((scenario or values)[key]) if candidate else series(0.),
                    series((scenario or stress or values)[key]) if candidate else series(0.))}
            return result
        self.trial["decision"] = decision
        result = attribute_trial(self.trial, self.frames, evaluator=evaluator)
        self.assertEqual(len(calls), 8)
        self.assertEqual({item[0] for item in calls}, set(CONTRASTS))
        self.assertTrue(all(item[2] == self.frames and item[3] == LIMITS for item in calls))
        return result

    def test_signal_only_is_a_conditional_hypothesis_with_no_duplicate_paths(self):
        result = self.evaluate(dict.fromkeys(CONTRASTS, -.001))
        self.assertEqual(result["stage"], "signal")
        self.assertEqual(result["version"], VERSION)
        self.assertEqual(result["unit"], "log_pp")
        self.assertEqual(result["contrasts"], dict.fromkeys(CONTRASTS, -6.))
        self.assertEqual(result["effects"], dict.fromkeys(EFFECTS, 0.))
        self.assertLess(result["ranges"]["D00"]["high"], 0)
        self.assertIn("모형", result["hypothesis"])
        serialized = json.dumps(result, allow_nan=False)
        self.assertNotIn("equity_curve", serialized)
        self.assertNotIn("daily_log_returns", serialized)

    def test_execution_and_capital_effects_use_both_starting_states(self):
        execution = self.evaluate({"D00": .001, "D01": .001, "D10": -.001, "D11": -.001})
        self.assertEqual(execution["stage"], "execution")
        self.assertEqual(execution["effects"]["execution_cash"], -12.)
        self.assertEqual(execution["effects"]["execution_inherited"], -12.)
        self.assertEqual(execution["effects"]["gamma"], 0.)
        capital = self.evaluate({"D00": .001, "D01": -.001, "D10": .001, "D11": -.001})
        self.assertEqual(capital["stage"], "capital")
        self.assertEqual(capital["effects"]["capital_daily"], -12.)
        self.assertEqual(capital["effects"]["capital_observed"], -12.)

    def test_state_dependent_opposite_execution_effects_are_interaction(self):
        result = self.evaluate({"D00": -.001, "D01": -.001, "D10": -.002, "D11": .001})
        self.assertEqual(result["stage"], "interaction")
        self.assertEqual(result["effects"]["gamma"], 18.)
        self.assertLess(result["ranges"]["execution_cash"]["high"], 0)
        self.assertGreater(result["ranges"]["execution_inherited"]["low"], 0)

    def test_paired_block_range_crossing_zero_does_not_assign_a_direction(self):
        noise = [-.01] * 20 + [.015] * 20 + [-.006] * 20
        result = self.evaluate(dict.fromkeys(CONTRASTS, noise))
        self.assertEqual(result["stage"], "uncertain")
        self.assertLess(result["contrasts"]["D00"], 0)
        self.assertLess(result["ranges"]["D00"]["low"], 0)
        self.assertGreater(result["ranges"]["D00"]["high"], 0)
        self.assertEqual(result, self.evaluate(dict.fromkeys(CONTRASTS, noise)))

    def test_cost_or_frozen_fill_assumption_reversals_are_uncertain(self):
        values = {"D00": .001, "D01": .001, "D10": -.001, "D11": -.001}
        opposite = dict.fromkeys(CONTRASTS, .002)
        for settings in ({"stress": opposite}, {"scenario": opposite}):
            result = self.evaluate(values, **settings)
            self.assertEqual(result["stage"], "uncertain")
            self.assertEqual(result["reason"], "assumption_direction_flip")

    def test_missing_empirical_support_retains_development_numbers_only(self):
        result = self.evaluate(dict.fromkeys(CONTRASTS, .001), supported=False)
        self.assertEqual(result["stage"], "uncertain")
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["reason"], "execution_support_unconfirmed")
        self.assertEqual(result["contrasts"]["D11"], 6.)

    def test_success_keep_and_invalid_receive_the_same_four_cell_decomposition(self):
        values = dict.fromkeys(CONTRASTS, .001)
        results = [self.evaluate(values, decision=decision) for decision in ("promote", "keep", "invalid")]
        self.assertEqual(results[0]["stage"], "supported")
        self.assertEqual(results[1]["stage"], "supported")
        self.assertEqual(results[2]["stage"], "uncertain")
        for result in results[1:]:
            self.assertEqual(result["contrasts"], results[0]["contrasts"])
            self.assertEqual(result["ranges"], results[0]["ranges"])

    def test_missing_fixed_period_or_inherited_state_is_not_fabricated(self):
        for frames, book in ((self.frames[:-1], self.trial["initial_state"]), (self.frames, None)):
            trial = {**self.trial, "initial_state": book}
            result = attribute_trial(trial, frames, evaluator=lambda *args, **kwargs: self.fail("incomplete replay"))
            self.assertEqual(result["stage"], "insufficient")
            self.assertTrue(all(value is None for value in result["contrasts"].values()))

    def test_actual_engine_profile_is_decomposed_without_promoting_unsupported_assumptions(self):
        from backend.learning_account import execution_profile
        from tests.test_learning_policy import frames
        observed = frames(61, price=10000)
        for frame in observed:
            frame["execution_quotes"] = {"005930": {"price": "10000", "eligible": True,
                "observed_at": frame["as_of"] + "T10:00:00+09:00", "volume": 1000}}
        profile = execution_profile([])
        trial = {**self.trial, "execution_model": profile,
            "initial_state": {"as_of": observed[0]["as_of"], "cash": "1000000", "positions": []},
            "evaluation_spec": make_evaluation_spec(1, bootstrap_repetitions=199,
                scenario_ids=tuple(item["id"] for item in profile["scenarios"]))}
        result = attribute_trial(trial, observed)
        self.assertEqual(result["stage"], "uncertain")
        self.assertEqual(result["reason"], "execution_support_unconfirmed")
        self.assertTrue(all(value is not None for value in result["contrasts"].values()))

    def test_new_trial_tests_the_frozen_component_separately_from_final_advantage(self):
        self.trial.update(id="new", hypothesis_test={"metric": "capital_observed", "direction": "positive",
                                                   "source_trial_id": "previous"})
        result = self.evaluate({"D00": -.003, "D01": -.001, "D10": -.003, "D11": -.001})
        before = deepcopy((self.trial, result))
        assessed = assess_hypothesis(self.trial, result)
        self.assertEqual(assessed["metric"], "capital_observed")
        self.assertEqual(assessed["status"], "supported")
        self.assertEqual(assessed["final_advantage"]["status"], "contradicted")
        self.assertEqual((self.trial, result), before)

    def test_opposite_new_period_contradicts_the_frozen_prediction(self):
        self.trial["hypothesis_test"] = {"metric": "execution_inherited", "direction": "positive"}
        result = self.evaluate({"D00": .001, "D01": .001, "D10": -.001, "D11": -.001})
        assessed = assess_hypothesis(self.trial, result)
        self.assertEqual(assessed["status"], "contradicted")
        self.assertLess(assessed["range"]["high"], 0)

    def test_zero_boundaries_and_crossings_do_not_claim_a_correct_prediction(self):
        self.trial["hypothesis_test"] = {"metric": "D00", "direction": "positive"}
        result = self.evaluate(dict.fromkeys(CONTRASTS, .001))
        for bounds in ({"low": 0, "high": 1}, {"low": -1, "high": 0}, {"low": -1, "high": 1}):
            result["ranges"]["D00"] = bounds
            assessed = assess_hypothesis(self.trial, result)
            self.assertEqual(assessed["status"], "unresolved")
            self.assertEqual(assessed["range"], bounds)

    def test_missing_or_retrofitted_hypothesis_is_not_inferred_from_outcome(self):
        result = self.evaluate(dict.fromkeys(CONTRASTS, .001))
        self.trial["id"] = "new"
        for test in (None, {}, {"metric": "D00", "direction": "negative"},
                     {"metric": "unexpected", "direction": "positive"},
                     {"metric": "D00", "direction": "positive", "source_trial_id": "new"}):
            self.trial["hypothesis_test"] = test
            assessed = assess_hypothesis(self.trial, result)
            self.assertEqual(assessed["status"], "unresolved")
            self.assertIsNone(assessed["range"])

    def test_invalid_or_unsupported_replay_cannot_verify_a_prediction(self):
        self.trial["hypothesis_test"] = {"metric": "D11", "direction": "positive"}
        result = self.evaluate(dict.fromkeys(CONTRASTS, .001))
        for changed in ({"stage": "insufficient"}, {"reason": "invalid_trial"},
                        {"reason": "paired_evaluation_invalid"}, {"reason": "execution_support_unconfirmed"},
                        {"ranges": []}):
            assessed = assess_hypothesis(self.trial, {**result, **changed})
            self.assertEqual(assessed["status"], "unresolved")
            self.assertEqual(assessed["final_advantage"]["status"], "unresolved")

    def test_cash_only_holding_keeps_quotes_after_signal_disappears_until_all_scenarios_exit(self):
        from backend.learning_account import execution_profile
        from backend.learning_policy import portfolio_metrics, signals
        from tests.test_learning_policy import frames, policy, row
        observed = frames(61, price=10000)
        candidate = policy(max_positions=1)
        candidate["strategies"][0]["holding_sessions"] = 3
        incumbent = deepcopy(candidate)
        incumbent["strategies"][0]["holding_sessions"] = 4
        for index, frame in enumerate(observed):
            frame["rows"].append(row("000660", price=10000, learning_eligible=False))
            quote = {"price": "10000", "eligible": True, "volume": 1000,
                     "observed_at": frame["as_of"] + "T10:00:00+09:00"}
            frame["execution_quotes"] = {"000660": quote}
            if index == 1:
                frame["execution_quotes"]["005930"] = deepcopy(quote)
        profile = execution_profile([])
        book = {"as_of": observed[0]["as_of"], "cash": "899900", "positions": [{
            "symbol": "000660", "strategy_id": "momentum", "quantity": 10,
            "cost": "100100", "entry_price": "10000", "last_close": "10000", "held_sessions": 0,
            "exit_policy": {"holding_sessions": 20, "stop_loss_pct": 0, "take_profit_pct": 0}}]}
        trial = {**self.trial, "incumbent": incumbent, "policy": candidate,
                 "initial_state": book, "execution_model": profile,
                 "evaluation_spec": make_evaluation_spec(1, bootstrap_repetitions=199,
                     scenario_ids=tuple(item["id"] for item in profile["scenarios"]))}
        inherited = portfolio_metrics(candidate, observed[:3], LIMITS, initial_state=book, execution_model=profile)
        self.assertTrue(inherited["valid"], inherited["error"])
        self.assertEqual([item["symbol"] for item in inherited["final_state"]["positions"]], ["000660"])
        self.assertEqual(signals(candidate, observed[2]), [])
        self.assertEqual(cash_quote_symbols(trial, observed[:3]), ["005930"])
        missing = portfolio_metrics(candidate, observed[:8], LIMITS, execution_model=profile)
        self.assertFalse(missing["valid"])
        self.assertEqual(missing["error"], "execution_quote_missing")
        for end in range(2, len(observed)):
            for symbol in cash_quote_symbols(trial, observed[:end]):
                observed[end]["execution_quotes"][symbol] = deepcopy(observed[end]["execution_quotes"]["000660"])
        self.assertEqual(cash_quote_symbols(trial, observed), [])
        attribution = attribute_trial(trial, observed)
        self.assertTrue(all(value is not None for value in attribution["contrasts"].values()), attribution)
        self.assertNotEqual(attribution["reason"], "paired_evaluation_invalid")

    def test_cash_quote_symbols_include_stress_only_and_valid_partial_results_without_inventing_data(self):
        calls = []
        def evaluate(policy, frames, limits, **kwargs):
            calls.append(kwargs)
            return {"valid": False, "scenario_results": {
                "good": {"valid": True, "final_state": None,
                         "stress_final_state": {"positions": [{"symbol": "005930"}]}},
                "bad": {"valid": False, "final_state": {"positions": [{"symbol": "000660"}]}}}}
        self.assertEqual(cash_quote_symbols(self.trial, self.frames, evaluator=evaluate), ["005930"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(item == {"initial_state": None, "execution_model": self.trial["execution_model"]} for item in calls))
        self.assertEqual(cash_quote_symbols(self.trial, self.frames,
            evaluator=lambda *args, **kwargs: {"valid": False, "final_state": None}), [])

    def test_cash_quote_symbols_preserve_supported_alphanumeric_holdings(self):
        from backend.learning_account import execution_profile
        from tests.test_learning_policy import frames, policy
        observed = frames(3, price=10000)
        for frame in observed:
            frame["rows"][0]["symbol"] = "0123A0"
            frame["execution_quotes"] = {"0123A0": {"price": "10000", "eligible": True,
                "volume": 1000, "observed_at": frame["as_of"] + "T10:00:00+09:00"}}
        trial = {**self.trial, "incumbent": policy(), "policy": policy(), "execution_model": execution_profile([])}
        self.assertEqual(cash_quote_symbols(trial, observed), ["0123A0"])


if __name__ == "__main__":
    unittest.main()
