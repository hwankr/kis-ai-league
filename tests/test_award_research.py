"""Current holdings inform research without becoming a historical start book."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from backend.learning_policy import BASELINE_POLICY, policy_id
from backend.learning_research import MAX_CANDIDATES, propose_candidate, public_competition_context, _public_daily_evaluation
from backend.policy_capacity import capacity_packet, signal_hold_packet, validate_capacity_member
from tests.test_learning_policy import LIMITS, frames
from tests.test_learning_research import rule
from tests.test_turnover_research import competition


def account_context():
    return {"signal_day": "2026-10-13", "initial_state": {
        "positions": [{"symbol": str(100000 + index), "held_sessions": 2} for index in range(8)],
        "cash": "9000", "equity": "10000", "pending_orders": 0, "unresolved_orders": 0,
        "account_protection": {"active": False}},
        "competition_context": competition()}


class AwardResearchTests(unittest.TestCase):
    def test_all_eligibility_counts_survive_safe_context_roundtrip(self):
        source = competition(metrics={"trading_days": {"value": 2}, "symbols": {"value": 8},
                                      "api_orders": {"value": 8}})
        clean = public_competition_context(source)
        self.assertEqual(public_competition_context(clean), clean)
        self.assertEqual(clean["metrics"]["trading_days"], {"value": 2, "target": 5})
        for bad in (True, -1, .5, "nan"):
            source["metrics"]["api_orders"]["value"] = bad
            self.assertIsNone(public_competition_context(source)["metrics"]["api_orders"]["value"])

    def test_bootstrap_compares_current_slot_capacity_with_same_conditions_and_exits(self):
        context = account_context()
        original = deepcopy(context)
        proposal = propose_candidate(BASELINE_POLICY, frames(1), LIMITS, [], award_context=context)
        pool = proposal["candidate_pool"]
        self.assertLessEqual(len(pool), MAX_CANDIDATES)
        repaired = [item["policy"] for item in pool if any("A" in p["roles"] for p in item.get("capacity_packets", []))]
        self.assertTrue(repaired)
        self.assertTrue(all(candidate["max_positions"] > 8 for candidate in repaired))
        first = repaired[0]
        metadata = next(item["capacity_packets"][0] for item in pool if item["policy"] == first)
        control = next(item["policy"] for item in pool if policy_id(item["policy"]) == metadata["base_id"])
        for before, after in zip(control["strategies"], first["strategies"]):
            for key in ("conditions", "rank_by", "holding_sessions", "stop_loss_pct", "take_profit_pct", "weight"):
                self.assertEqual(before[key], after[key])
        self.assertEqual(first["cash_reserve"], control["cash_reserve"])
        self.assertEqual(context, original)

    def test_rules_common_control_preserves_signals_and_existing_or_future_exit_rules(self):
        parent = rule()
        proposal = propose_candidate(parent, frames(1), LIMITS, [], award_context=account_context())
        repairs = [item for item in proposal["candidate_pool"]
                   if any("C" in p["roles"] and p["base_id"] == policy_id(parent)
                          for p in item.get("capacity_packets", []))]
        self.assertTrue(repairs)
        for item in repairs:
            self.assertEqual(item["change_family"], "capacity")
            self.assertTrue(item['policy']['held_before_top_n'])
            self.assertTrue(item['policy']['no_same_day_reentry'])
            for before, after in zip(parent["strategies"], item["policy"]["strategies"]):
                for key in ("conditions", "holding_sessions", "stop_loss_pct", "take_profit_pct", "weight"):
                    self.assertEqual(before[key], after[key])

    def test_provider_gets_aggregate_book_not_account_identifiers_or_positions(self):
        provider = Mock()
        provider.generate.return_value = rule()
        context = account_context()
        context["initial_state"]["secret"] = "DO_NOT_SEND"
        propose_candidate(BASELINE_POLICY, frames(1), LIMITS, [], provider, award_context=context)
        sent = provider.generate.call_args.args[1]
        self.assertEqual(sent["transition_context"]["held_symbols"], 8)
        self.assertNotIn("DO_NOT_SEND", json.dumps(sent))
        self.assertNotIn("positions", sent["transition_context"])

    def test_stale_competition_does_not_trigger_capacity_expansion(self):
        context = account_context()
        context["competition_context"]["stale"] = True
        proposal = propose_candidate(BASELINE_POLICY, frames(1), LIMITS, [], award_context=context)
        self.assertFalse(any(item.get("capacity_packets")
                             for item in proposal["candidate_pool"]))

    def test_legacy_pool_has_complete_four_family_packets_and_no_score_prefix(self):
        proposal = propose_candidate(BASELINE_POLICY, frames(1), LIMITS, [], award_context=account_context())
        pool = proposal["candidate_pool"]
        identities = {policy_id(item["policy"]) for item in pool}
        self.assertEqual(len(pool), 16)
        self.assertEqual(len(identities), 16)
        self.assertEqual({item["policy"]["strategies"][0]["id"] for item in pool},
                         {"research-trend", "research-relative", "research-recovery", "research-liquid"})
        for packet in proposal["search_plan"]["packets"]:
            self.assertTrue(set(packet["member_ids"].values()) <= identities)

    def test_rules_compound_has_exact_controls_even_when_controls_were_seen(self):
        parent = rule()
        packet = signal_hold_packet(parent, frames(1))
        history = [{"policy": packet["members"]["C"], "candidate_id": packet["member_ids"]["C"]}]
        proposal = propose_candidate(parent, frames(1), LIMITS, history, award_context=account_context())
        pool = proposal["candidate_pool"]
        compounds = [item for item in pool if item["change_family"] == "capacity"]
        self.assertTrue(compounds)
        for item in compounds:
            self.assertTrue(validate_capacity_member(parent, item, pool, frames=frames(1), limits=LIMITS))
        self.assertTrue(set(packet["member_ids"].values()) <= {policy_id(parent), *[policy_id(p["policy"]) for p in pool]})
        self.assertEqual({item["change_family"] for item in pool}, {'capacity'})
        self.assertLessEqual(len(pool), 16)
        self.assertEqual(len(proposal["search_plan"]["packets"]), 1)
        self.assertEqual(proposal["search_plan"]["allocation_levels"], [8])
        self.assertEqual({p["actual_parent_id"] for p in proposal["search_plan"]["packets"]},
                         {policy_id(parent)})

    def test_rules_llm_observation_does_not_replace_registered_signal_hold_packet(self):
        parent = rule()
        provider = Mock()
        signal = deepcopy(parent)
        signal["strategies"][0]["conditions"] = [{"feature": "volume_ratio", "op": "gt", "value": .75}]
        provider.generate.return_value = signal
        proposal = propose_candidate(parent, frames(1), LIMITS, [], provider, award_context=account_context())
        self.assertFalse(proposal["search_plan"]["llm_packet_used"])
        self.assertEqual(proposal["search_plan"]['llm_observation_candidate_ids'], [policy_id(signal)])
        pool = proposal["candidate_pool"]
        for item in pool:
            if item["change_family"] == "capacity":
                self.assertTrue(validate_capacity_member(parent, item, pool, frames=frames(1), limits=LIMITS))
        nonsignal = deepcopy(parent)
        nonsignal["strategies"][0]["stop_loss_pct"] = 3
        provider.generate.return_value = nonsignal
        fallback = propose_candidate(parent, frames(1), LIMITS, [], provider, award_context=account_context())
        self.assertFalse(fallback["search_plan"]["llm_packet_used"])
        self.assertEqual({policy_id(p['policy']) for p in pool},
                         {policy_id(p['policy']) for p in fallback['candidate_pool']})
        self.assertTrue(all(p["policy"]["strategies"][0]["stop_loss_pct"] == 4
                            for p in fallback["candidate_pool"]))

    def test_llm_signal_with_hidden_optional_exit_field_uses_valid_fixed_fallback(self):
        parent = rule()
        candidate = deepcopy(parent)
        candidate["strategies"][0]["conditions"] = [{"feature": "volume_ratio", "op": "gt", "value": .75}]
        candidate["strategies"][0]["exit_mode"] = "fixed"
        provider = Mock()
        provider.generate.return_value = candidate
        proposal = propose_candidate(parent, frames(1), LIMITS, [], provider, award_context=account_context())
        self.assertFalse(proposal["search_plan"]["llm_packet_used"])
        self.assertEqual(len(proposal["search_plan"]["packets"]), 1)
        pool = proposal["candidate_pool"]
        for item in pool:
            self.assertTrue(validate_capacity_member(parent, item, pool, frames=frames(1), limits=LIMITS))
            self.assertNotIn("exit_mode", item["policy"]["strategies"][0])

    def test_llm_packet_rotation_and_failure_fallback_are_fixed_by_input_day(self):
        provider = Mock()
        provider.generate.return_value = rule()
        context = account_context()
        a = propose_candidate(BASELINE_POLICY, frames(1), LIMITS, [], provider, award_context=context)
        b = propose_candidate(BASELINE_POLICY, frames(1), LIMITS, [], provider, award_context=context)
        self.assertEqual(a, b)
        self.assertEqual(len(a["candidate_pool"]), 16)
        self.assertTrue(a["search_plan"]["llm_packet_used"])
        provider.generate.side_effect = RuntimeError("provider unavailable")
        fallback = propose_candidate(BASELINE_POLICY, frames(1), LIMITS, [], provider, award_context=context)
        self.assertFalse(fallback["search_plan"]["llm_packet_used"])
        self.assertEqual(len(fallback["search_plan"]["packets"]), 4)

    def test_same_scalar_deficit_preserves_different_failed_requirements_and_identity(self):
        def evaluation(key):
            return {"decision": "keep", "metrics": {"award_selection": {"outcomes": [{
                "candidate_id": "a" * 64, "secret": "DO_NOT_SEND", "qualification": {
                    "status": "unknown", "worst_shortfall": None, "scenario_count": 24,
                    "evaluated_scenarios": 8, "qualifying_scenarios": 0,
                    "scenarios": [{"id": "recent_full_normal", "shortfall": .4,
                        "values": {key: 3}, "checks": {key: False}, "joint_met": False}]}}]}}}
        amount = _public_daily_evaluation(evaluation("gross_amount"))
        symbols = _public_daily_evaluation(evaluation("symbols"))
        self.assertNotEqual(amount, symbols)
        outcome = amount["metrics"]["award_selection"]["outcomes"][0]
        self.assertEqual(outcome["candidate_id"], "a" * 64)
        self.assertEqual(outcome["coverage"]["evaluated_scenarios"], 8)
        self.assertFalse(outcome["scenarios"][0]["checks"]["gross_amount"])
        self.assertIsNone(outcome["scenarios"][0]["checks"]["symbols"])
        self.assertNotIn("DO_NOT_SEND", json.dumps(amount))


if __name__ == "__main__":
    unittest.main()
