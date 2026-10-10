from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.experiment_analysis import AnalysisError, CodexLLM, CompatibleLLM
from backend.learning_policy import BASELINE_POLICY, policy_id, validate_policy
from backend.learning_research import MAX_CANDIDATES, POLICY_SCHEMA, propose_candidate


LIMITS = {"budget": "1000000", "order_cap": "100000", "daily_buy_limit": "300000"}


def metrics(net=10, stress=8, drawdown=2):
    return {"net_return_pct": net, "stress_return_pct": stress, "max_drawdown_pct": drawdown,
            "closed_trades": 20, "sessions": 40, "half_returns": [net / 2, net / 2],
            "valid": True, "error": None}


def rule():
    return {"kind": "rules", "name": "candidate", "cash_reserve": .1, "max_positions": 5,
            "entry_slippage_bps": 100, "cancel_after_minutes": 10, "strategies": [{
                "id": "generated-rule", "name": "rule", "weight": .9,
                "conditions": [{"feature": "return_5d_pct", "op": "gt", "value": 0}],
                "rank_by": "return_5d_pct", "descending": True, "top_n": 3,
                "holding_sessions": 3, "stop_loss_pct": 4, "take_profit_pct": 10}]}


def frames(count=42):
    days, day = [], date(2026, 1, 1)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    result = []
    for index, day in enumerate(days):
        price = 1000 * 1.004 ** index
        result.append({"as_of": day, "observed_at": day + "T08:00:00+00:00",
            "session_days": days[:index + 1], "baseline_signals": [], "rows": [{
                "symbol": "005930", "name": "public stock", "board": "KOSPI",
                "open": price, "high": price * 1.02, "low": price * .98, "close": price,
                "volume": 100000, "features": {"return_5d_pct": index / 4 + .5,
                    "return_20d_pct": index / 3 + 2, "excess_20d_pp": index / 2 + 1,
                    "close_sma20_pct": index / 5 + .1, "close_sma60_pct": index / 8 + .5,
                    "breakout_20d_pct": index / 10, "volume_ratio": 1 + index / 20,
                    "turnover": 100000000 + index * 1000000}}]})
    return result


class ResearchTests(unittest.TestCase):
    def test_bootstrap_is_novel_without_fabricated_returns_or_evaluation(self):
        with patch("backend.learning_research.portfolio_metrics", side_effect=AssertionError("no evidence")):
            result = propose_candidate(BASELINE_POLICY, [], LIMITS, [])
        self.assertEqual(result["method"], "grammar_bootstrap")
        self.assertEqual(validate_policy(result["policy"]), result["policy"])
        self.assertNotEqual(policy_id(BASELINE_POLICY), policy_id(result["policy"]))
        self.assertFalse(result["development"]["valid"])
        self.assertIsNone(result["development"]["net_return_pct"])
        self.assertEqual(result["development"]["candidates_tested"], 0)

    def test_same_inputs_are_deterministic_and_frozen(self):
        args = [deepcopy(BASELINE_POLICY), frames(), deepcopy(LIMITS), []]
        before = deepcopy(args)
        first = propose_candidate(*args)
        second = propose_candidate(*args)
        self.assertEqual(first, second)
        self.assertEqual(args, before)
        self.assertEqual(first["development"]["role"], "development_only")
        self.assertGreater(first["development"]["closed_trades"], 0)
        self.assertLessEqual(first["development"]["candidates_tested"], MAX_CANDIDATES)

    def test_prior_trial_outcomes_change_conditions_exits_and_sizing(self):
        good = [{"policy": deepcopy(BASELINE_POLICY), "decision": "promote", "metrics": {"challenger": metrics()}}]
        bad = deepcopy(good)
        bad[0].update(decision="keep", metrics={"challenger": metrics(-3, -6, 8)})
        with patch("backend.learning_research.portfolio_metrics", return_value={"valid": False}):
            a = propose_candidate(BASELINE_POLICY, frames(5), LIMITS, good)["policy"]
            b = propose_candidate(BASELINE_POLICY, frames(5), LIMITS, bad)["policy"]
        self.assertNotEqual(a["cash_reserve"], b["cash_reserve"])
        self.assertNotEqual(a["strategies"][0]["holding_sessions"], b["strategies"][0]["holding_sessions"])
        self.assertNotEqual(a["strategies"][0]["stop_loss_pct"], b["strategies"][0]["stop_loss_pct"])
        self.assertNotEqual(a["strategies"][0]["conditions"], b["strategies"][0]["conditions"])
        self.assertEqual(a["cancel_after_minutes"], BASELINE_POLICY["cancel_after_minutes"])

    def test_retired_behavior_cannot_be_retried_under_another_name(self):
        first = propose_candidate(BASELINE_POLICY, [], LIMITS, [])["policy"]
        prior = deepcopy(first)
        prior["name"] = "different label"
        prior["strategies"][0].update(id="different-id", name="different label")
        result = propose_candidate(BASELINE_POLICY, [], LIMITS, [{"policy": prior, "decision": "retired"}])
        self.assertNotEqual(result["policy"]["strategies"][0]["conditions"], first["strategies"][0]["conditions"])
        id_only = propose_candidate(BASELINE_POLICY, [], LIMITS, [{"candidate_id": policy_id(first)}])
        self.assertNotEqual(policy_id(id_only["policy"]), policy_id(first))

    def test_evaluations_are_bounded_and_penalize_costs_and_drawdown(self):
        calls = []
        def evaluate(policy, observations, limits):
            calls.append(deepcopy(policy))
            return metrics(10, 8, 2) if policy["cash_reserve"] >= .15 else metrics(30, -10, 20)
        with patch("backend.learning_research.portfolio_metrics", side_effect=evaluate):
            result = propose_candidate(BASELINE_POLICY, frames(3), LIMITS, [])
        self.assertEqual(len(calls), MAX_CANDIDATES)
        self.assertGreaterEqual(result["policy"]["cash_reserve"], .15)
        self.assertEqual(result["development"]["net_return_pct"], 10)
        self.assertGreater(len({item["strategies"][0]["rank_by"] for item in calls}), 1)
        self.assertGreater(len({item["strategies"][0]["holding_sessions"] for item in calls}), 1)
        self.assertGreater(len({item["entry_slippage_bps"] for item in calls}), 1)

    def test_llm_receives_only_public_summaries_and_candidate_is_validated(self):
        returned = rule()
        returned["cancel_after_minutes"] = 60
        calls = []
        class FakeLLM:
            def generate(self, prompt, payload, schema):
                calls.append((prompt, deepcopy(payload), schema))
                return returned
        history = [{"policy": rule(), "decision": "keep", "metrics": metrics(-2, -4, 6),
                    "account_id": "private-account", "api_key": "private-key",
                    "execution": {"terminal_orders": 5, "unresolved_orders": 2,
                                  "fill_ratio": .4, "rejection_rate": .2, "account": "private-account"}}]
        # Make the LLM policy novel relative to this old trial.
        history[0]["policy"]["strategies"][0]["holding_sessions"] = 7
        with patch("backend.learning_research.portfolio_metrics", side_effect=lambda policy, *args:
                   metrics(20, 18) if policy["strategies"][0]["id"] == "generated-rule" else metrics(1, .5)):
            result = propose_candidate(BASELINE_POLICY, frames(3), {**LIMITS, "budget": "987654321"}, history, llm=FakeLLM())
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["method"], "llm_development")
        self.assertEqual(result["policy"]["cancel_after_minutes"], 10)
        self.assertEqual(returned["cancel_after_minutes"], 60)
        serialized = json.dumps(calls)
        for private in ("private-account", "private-key", "987654321", "005930", "public stock"):
            self.assertNotIn(private, serialized)
        self.assertEqual(calls[0][1]["history"][0]["execution"]["fill_ratio"], .4)
        self.assertEqual(calls[0][1]["history"][0]["execution"]["terminal_orders"], 5)
        self.assertEqual(calls[0][1]["history"][0]["execution"]["unresolved_orders"], 2)
        self.assertTrue(calls[0][1]["execution_hint"])

    def test_confirmed_execution_hint_changes_only_entry_and_liquidity(self):
        normal = propose_candidate(BASELINE_POLICY, [], LIMITS, [])
        for execution in ({"terminal_orders": 5, "rejection_rate": .2, "fill_ratio": None},
                          {"terminal_orders": 5, "rejection_rate": 0, "fill_ratio": .49, "unresolved_orders": 20}):
            hint = propose_candidate(BASELINE_POLICY, [], LIMITS,
                                     [{"method": "execution_feedback", "policy": BASELINE_POLICY, "execution": execution}])
            self.assertTrue(hint["development"]["execution_hint"])
            self.assertIsNone(hint["development"]["net_return_pct"])
            self.assertLess(hint["policy"]["entry_slippage_bps"], normal["policy"]["entry_slippage_bps"])
            a, b = normal["policy"]["strategies"][0], hint["policy"]["strategies"][0]
            self.assertTrue(any(item["feature"] == "turnover" for item in b["conditions"]))
            self.assertLessEqual(b["top_n"], 2)
            for key in ("holding_sessions", "stop_loss_pct", "take_profit_pct", "weight"):
                self.assertEqual(a[key], b[key])
            for key in ("cash_reserve", "max_positions", "cancel_after_minutes"):
                self.assertEqual(normal["policy"][key], hint["policy"][key])

    def test_unconfirmed_missing_or_invalid_execution_is_not_a_reward(self):
        expected = propose_candidate(BASELINE_POLICY, [], LIMITS, [])["policy"]
        cases = [{"terminal_orders": value, "rejection_rate": .9, "fill_ratio": 0, "unresolved_orders": 20}
                 for value in (None, 0, 4, True)]
        cases += [{"terminal_orders": 5, "rejection_rate": value, "fill_ratio": value}
                  for value in (None, -1, 2, float("nan"))]
        cases += [{"terminal_orders": 5, "rejection_rate": .19, "fill_ratio": .5}]
        for execution in cases:
            result = propose_candidate(BASELINE_POLICY, [], LIMITS, [{"execution": execution}])
            self.assertFalse(result["development"]["execution_hint"])
            self.assertEqual(result["policy"], expected)

    def test_execution_diagnostics_do_not_change_development_score(self):
        history = [{"execution": {"terminal_orders": 10, "rejection_rate": .5, "fill_ratio": None}}]
        with patch("backend.learning_research.portfolio_metrics", return_value=metrics()):
            normal = propose_candidate(BASELINE_POLICY, frames(3), LIMITS, [])
            diagnosed = propose_candidate(BASELINE_POLICY, frames(3), LIMITS, history)
        self.assertEqual(normal["development"]["score"], diagnosed["development"]["score"])
        latest = {"execution": {"terminal_orders": 5, "rejection_rate": 0, "fill_ratio": 1}}
        self.assertFalse(propose_candidate(BASELINE_POLICY, [], LIMITS, [latest, *history])["development"]["execution_hint"])

    def test_invalid_llm_and_transport_failure_use_deterministic_fallback(self):
        expected = propose_candidate(BASELINE_POLICY, [], LIMITS, [])["policy"]
        cases = [{"code": "import os"}, {**rule(), "extra": "not allowed"}, "not json", None]
        invalid = rule()
        invalid["strategies"][0]["conditions"][0]["value"] = float("nan")
        cases.append(invalid)
        for value in cases:
            class FakeLLM:
                def generate(self, *args):
                    if value is None:
                        raise RuntimeError("private-token")
                    return value
            result = propose_candidate(BASELINE_POLICY, [], LIMITS, [], llm=FakeLLM())
            self.assertEqual(result["policy"], expected)
            self.assertNotIn("private-token", json.dumps(result))

    def test_missing_or_incomplete_metrics_never_imply_learned_performance(self):
        for outcome in ({"valid": False, "error": "missing_session"},
                        {**metrics(), "closed_trades": 0}, {**metrics(), "stress_return_pct": float("nan")}):
            with patch("backend.learning_research.portfolio_metrics", return_value=outcome):
                result = propose_candidate(BASELINE_POLICY, frames(3), LIMITS, [])
            self.assertFalse(result["development"]["valid"])
            self.assertIsNone(result["development"]["net_return_pct"])


class StructuredProviderTests(unittest.TestCase):
    def test_compatible_generic_json_uses_configured_provider_without_tools(self):
        calls = []
        def transport(request, timeout, limit):
            payload = json.loads(request.data)
            calls.append(payload)
            self.assertEqual(payload["messages"][0]["content"], "public research")
            self.assertEqual(payload["response_format"]["json_schema"]["schema"], POLICY_SCHEMA)
            self.assertNotIn("tools", payload)
            return json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(rule())}}]}).encode()
        provider = CompatibleLLM({"provider": "local", "base_url": "http://127.0.0.1:8000/v1", "model": "chosen"},
                                 transport=transport, environ={})
        self.assertEqual(provider.generate("public research", {"public": True}, POLICY_SCHEMA), rule())
        self.assertEqual(len(calls), 1)

    def test_codex_generic_json_preserves_isolation_and_custom_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            def runner(args, **kwargs):
                calls.append(args)
                self.assertIn('--sandbox', args)
                self.assertIn('web_search="disabled"', args)
                self.assertIn('developer_instructions="public research"', args)
                self.assertNotIn("KIS_APP_SECRET", kwargs["env"])
                self.assertEqual(json.loads(Path(args[args.index("--output-schema") + 1]).read_text(encoding="utf-8")), POLICY_SCHEMA)
                Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(rule()), encoding="utf-8")
                kwargs["stdout"].write(b'{"type":"turn.completed"}\n')
                return SimpleNamespace(returncode=0)
            provider = CodexLLM(runner=runner, executable="fake-codex.exe",
                                environ={"CODEX_HOME": directory, "KIS_APP_SECRET": "private"})
            self.assertEqual(provider.generate("public research", {"public": True}, POLICY_SCHEMA), rule())
            self.assertEqual(len(calls), 1)

    def test_generic_provider_rejects_tools_and_non_object_json(self):
        for message in ({"content": "[]"}, {"content": "{}", "tool_calls": [{"name": "order"}]},
                        {"content": '{"value":1,"value":2}'}, {"content": '{"value":1e999}'}):
            wire = json.dumps({"choices": [{"finish_reason": "stop", "message": message}]}).encode()
            provider = CompatibleLLM({"provider": "local", "base_url": "http://localhost:8000", "model": "test"},
                                     transport=lambda *args: wire, environ={})
            with self.assertRaises(AnalysisError):
                provider.generate("public", {}, POLICY_SCHEMA)


if __name__ == "__main__":
    unittest.main()
