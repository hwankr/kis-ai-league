"""Offline lifecycle checks, not evidence of policy evaluation or profitability.

The existing fixture supplies market, analysis, broker and background-work
doubles; the evaluator/proposer inject apply. Daily process, preparation,
persistence, activation and order handling use their real implementations.
All state is in the existing temporary fixture.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
import unittest
from unittest.mock import Mock

from backend.experiment_analysis import analysis_version, build_snapshot
from backend.kis import KST
from backend.learning_policy import BASELINE_POLICY, build_frame, policy_id, signals
from backend.research_rules import MINIMUM_SESSIONS, evaluate_pullback
from tests import test_experiments as fixtures
from tests import test_learning_integration as learning_fixtures


class DailyActivationRecoveryTests(unittest.TestCase):
    completed_input = staticmethod(learning_fixtures.LearningIntegrationTests.completed_input)
    set_champion = learning_fixtures.LearningIntegrationTests.set_champion
    fill = fixtures.ExperimentsTests.fill

    def setUp(self):
        # Deliberately inject a verdict, not an investment-performance evaluator.
        self.evaluator = Mock(return_value={"decision": "apply", "metrics": None,
            "reason": "Lifecycle fixture only; no performance claim", "changes": []})
        learning_fixtures.LearningIntegrationTests.setUp(self)
        self.current = datetime(2026, 10, 2, 17, tzinfo=KST)
        self.broker.days += ["2026-10-07", "2026-10-08"]

    def make_service(self):
        service = learning_fixtures.LearningIntegrationTests.make_service(self)
        service.learning.daily.enabled = True
        service.learning.daily.evaluator = self.evaluator
        service._refresh_shadow = Mock()
        return service

    def baseline_analysis(self, **data):
        data.pop("llm")
        day = str(data["as_of"])
        return {"decisions": [{**fixtures.decision(symbol), "as_of": day, "score": None,
                    "reason": "Fixture baseline", "evidence_ids": [f"market:{symbol}:{day}"]}
                    for symbol in fixtures.SYMBOLS],
                "input_hash": "fixture-baseline-" + day,
                "version_id": analysis_version(self.config),
                "llm_status": {"status": "ready"}, "input": build_snapshot(**data)}

    def restart(self):
        self.service.close()
        self.service = self.make_service()

    def controls(self):
        return {key: self.service.store.setting(key) for key in
                ("policy", "enabled", "user_paused", "mirror_intent")}

    def allow_fixture_orders(self):
        self.service.store.save_settings({"enabled": True, "user_paused": False})

    def candidate(self, holding_sessions):
        value = deepcopy(self.service.learning.daily.policy())
        value["name"] = f"Lifecycle fixture {holding_sessions}"
        value["strategies"][0]["holding_sessions"] = holding_sessions
        return value

    def advance_completed_input(self, day):
        previous = date.fromisoformat(self.input_data["as_of"])
        session = date.fromisoformat(day)
        self.current = datetime.combine(session, datetime.min.time(), KST).replace(hour=17)
        for bars in self.input_data["histories"].values():
            bars[session] = deepcopy(bars[previous])
        self.input_data["calendars"]["KOSPI"].append(session)
        self.input_data["benchmarks"]["KOSPI"][session] = 1000
        self.input_data.update(as_of=day, observed_at=self.current.isoformat())
        for row in self.input_data["rows"]:
            row["as_of"] = day

    def stage(self, candidate, *, prepare=True):
        # _analyze builds the same owned-book context that the normal worker gets.
        baseline = self.service._analyze()
        context = deepcopy(self.service.learning.enqueue.call_args.kwargs["context"])
        proposer = Mock(return_value={"policy": candidate, "change_family": "exit",
            "rationale": "Lifecycle fixture", "method": "test"})
        baseline_signals = [row for row in baseline["signals"] if not row.get("policy_id")]
        self.service.learning.daily.process(deepcopy(self.input_data),
            [build_frame(self.input_data, baseline_signals)],
            self.service._policy(), [], context=context, proposer=proposer)
        self.assertEqual(self.service.learning.daily.history()[0]["decision"], "apply")
        if prepare:
            self.assertTrue(self.service._prepare_daily(deepcopy(self.input_data), baseline))
        pending = self.service.learning.daily.pending()
        if prepare:
            self.assertIsNotNone(pending["prepared_run"])
            self.assertNotIn(pending["prepared_run"]["id"],
                             [run["id"] for run in self.service.store.runs()])
        return pending, baseline, context, proposer

    @staticmethod
    def learned_signals(run):
        return [row for row in run["signals"] if row.get("policy_id")]

    def committed_run(self, active):
        return next(run for run in self.service.store.runs() if run["id"] == active["run_id"])

    def assert_material_change_blocks(self, change):
        pending, _, _, _ = self.stage(self.candidate(4))
        original = deepcopy(self.service.learning.daily.history()[0])
        change()
        self.restart()
        self.allow_fixture_orders()
        self.current = datetime(2026, 10, 6, 9, 5, tzinfo=KST)
        self.service._cycle()
        self.assertIsNone(self.service.learning.daily.active())
        self.assertEqual(self.service.learning.daily.snapshot()["status"], "blocked")
        self.assertEqual(self.service.learning.daily.pending(), pending)
        self.assertEqual(self.service.learning.daily.history(), [original])
        self.assertNotIn(pending["prepared_run"]["id"],
                         [run["id"] for run in self.service.store.runs()])
        self.assertTrue(all(order.get("policy_id") != pending["candidate_id"]
                            for order in self.service.store.orders()))

    def activate_at(self, day):
        self.current = datetime.fromisoformat(day + "T09:05:00+09:00")
        self.service._cycle()
        active = self.service.learning.daily.active()
        self.assertIsNotNone(active)
        self.assertEqual(active["effective_day"], day)
        self.assertIsNone(self.service.learning.daily.pending())
        return active

    def fill_entry(self):
        symbol, side, quantity, _ = self.broker.submissions[0]
        self.assertEqual(side, "buy")
        self.fill(quantity, status="filled")
        self.broker.account["holdings"][symbol] = {
            "quantity": quantity, "sellable_quantity": quantity, "price": "10000"}
        return symbol, quantity

    def test_process_prepare_restart_activate_preserves_frozen_run_and_deduplicates(self):
        controls = self.controls()
        pending, baseline, context, proposer = self.stage(self.candidate(4))
        original_record = deepcopy(self.service.learning.daily.history()[0])
        self.assertEqual(self.controls(), controls)
        self.assertEqual(self.broker.submissions, [])
        self.current += timedelta(minutes=10)
        baseline_signals = [row for row in baseline["signals"] if not row.get("policy_id")]
        self.service.learning.daily.process(deepcopy(self.input_data),
            [build_frame(self.input_data, baseline_signals)],
            self.service._policy(), [], context=context, proposer=proposer)
        self.assertFalse(self.service._prepare_daily(deepcopy(self.input_data), baseline))
        changed = deepcopy(pending["prepared_run"])
        changed["signals"] = []
        self.assertTrue(self.service.learning.daily.prepare(pending, changed))
        changed["id"] = "replacement-is-forbidden"
        self.assertFalse(self.service.learning.daily.prepare(pending, changed))
        self.restart()
        self.assertEqual(self.service.learning.daily.pending(), pending)
        self.assertEqual(self.service.learning.daily.history(), [original_record])
        proposer.assert_called_once()
        self.evaluator.assert_called_once()
        self.allow_fixture_orders()
        controls = self.controls()
        self.current = datetime(2026, 10, 6, 9, 4, tzinfo=KST)
        self.service._cycle()
        self.assertIsNone(self.service.learning.daily.active())
        self.assertEqual(self.broker.submissions, [])
        active = self.activate_at("2026-10-06")
        self.assertEqual(active["revision"], 1)
        self.assertEqual(active["run_id"], pending["prepared_run"]["id"])
        committed = next(run for run in self.service.store.runs() if run["id"] == active["run_id"])
        self.assertEqual(committed, pending["prepared_run"])
        updated_record = self.service.learning.daily.history()[0]
        self.assertEqual({key: value for key, value in updated_record.items() if key != "activation"},
                         original_record)
        self.assertEqual(updated_record["activation"]["run_id"], active["run_id"])
        orders = self.service.store.orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["run_id"], active["run_id"])
        self.assertEqual(orders[0]["policy_id"], pending["candidate_id"])
        self.service._cycle()
        self.restart()
        self.service._cycle()
        self.assertEqual(self.service.learning.daily.active(), active)
        reconciled = self.service.store.orders()
        self.assertEqual([{key: value for key, value in order.items() if key != "reconciled_at"}
                          for order in reconciled], orders)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(sum(run["id"] == active["run_id"] for run in self.service.store.runs()), 1)
        self.assertEqual(self.controls(), controls)
        self.llm.assert_not_called()

    def test_two_completed_days_activate_sequentially_without_rewriting_prior_evidence(self):
        first, _, _, _ = self.stage(self.candidate(4))
        self.allow_fixture_orders()
        self.restart()
        first_active = self.activate_at("2026-10-06")
        self.fill_entry()
        first_record = deepcopy(self.service.learning.daily.history()[0])
        first_run = next(run for run in self.service.store.runs() if run["id"] == first_active["run_id"])
        orders = self.service.store.orders()
        self.advance_completed_input("2026-10-06")
        second, _, _, _ = self.stage(self.candidate(6))
        self.assertNotEqual(first["decision_id"], second["decision_id"])
        self.assertEqual(second["parent_id"], first["candidate_id"])
        self.assertEqual(second["revision"], 1)
        self.assertEqual(self.service.learning.daily.active(), first_active)
        self.assertEqual(self.service.store.orders(), orders)
        self.restart()
        self.assertEqual(self.service.learning.daily.pending(), second)
        second_active = self.activate_at("2026-10-07")
        self.assertEqual(second_active["revision"], 2)
        self.assertEqual(second_active["run_id"], second["prepared_run"]["id"])
        self.service._cycle()
        self.restart()
        self.service._cycle()
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.service.store.orders(), orders)
        self.assertEqual(self.evaluator.call_count, 2)
        history = self.service.learning.daily.history()
        self.assertEqual(len(history), 2)
        self.assertEqual(next(row for row in history if row["id"] == first["decision_id"]), first_record)
        self.assertEqual(next(run for run in self.service.store.runs() if run["id"] == first_active["run_id"]),
                         first_run)
        self.assertEqual([row["activation"]["revision"] for row in history], [2, 1])

    def test_new_daily_policy_keeps_existing_position_exit_and_does_not_repeat_sell(self):
        original = self.service.learning.daily.policy()
        self.service._analyze()
        self.allow_fixture_orders()
        self.current = fixtures.NOW
        self.service._cycle()
        symbol, quantity = self.fill_entry()
        original_order = deepcopy(self.service.store.orders()[0])
        self.advance_completed_input("2026-10-06")
        pending, _, _, _ = self.stage(self.candidate(20))
        self.restart()
        active = self.activate_at("2026-10-07")
        self.assertEqual(active["candidate_id"], pending["candidate_id"])
        positions, _ = self.service._portfolio(self.service._owned_orders())
        self.assertEqual(positions[0]["policy_id"], policy_id(original))
        self.assertEqual(positions[0]["exit_policy"], original_order["exit_policy"])
        self.assertEqual(positions[0]["exit_policy"]["holding_sessions"], 2)
        self.assertEqual(len(self.broker.submissions), 1)
        self.current = fixtures.NOW.replace(day=8)
        self.service._cycle()
        self.assertEqual(self.broker.submissions[-1], (symbol, "sell", quantity, "10000"))
        sell = self.service.store.orders()[-1]
        self.assertEqual(sell["policy_id"], original_order["policy_id"])
        self.assertEqual(sell["exit_policy"], original_order["exit_policy"])
        self.assertEqual(len(self.broker.submissions), 2)
        self.service._cycle()
        self.restart()
        self.service._cycle()
        self.assertEqual(len(self.broker.submissions), 2)
        self.fill(quantity, index=1, status="filled")
        self.broker.account["holdings"].pop(symbol)
        self.service._cycle()
        self.assertEqual(self.service._portfolio(self.service._owned_orders())[0], [])
        self.assertEqual(len(self.broker.submissions), 2)
        self.assertEqual(self.service.learning.daily.active(), active)

    def test_false_to_true_before_preparation_cannot_add_a_frozen_out_signal(self):
        excluded = fixtures.SYMBOLS[0]  # Highest turnover would otherwise rank first.
        self.input_data["rows"][0]["learning_eligible"] = False
        frozen = build_frame(self.input_data)
        candidate = self.candidate(4)
        expected = signals(candidate, frozen)
        pending, baseline, _, _ = self.stage(candidate, prepare=False)
        contract = pending["signal_inputs"]
        self.assertEqual(contract["version"], "daily-inputs-v1")
        self.assertEqual(contract["frame"], {key: value for key, value in frozen.items()
                                            if key != "observed_at"})
        self.assertEqual(contract["hash"], pending["market_input_hash"])
        self.assertEqual(self.service.learning.daily.history()[0]["signal_inputs"], contract)
        self.input_data["rows"][0]["learning_eligible"] = True
        self.assertIn(excluded, [row["symbol"] for row in signals(candidate, build_frame(self.input_data))])
        self.current = datetime(2026, 10, 6, 8, 59, 59, tzinfo=KST)
        self.assertTrue(self.service._prepare_daily(deepcopy(self.input_data), baseline))
        prepared = self.service.learning.daily.pending()
        self.assertEqual(self.learned_signals(prepared["prepared_run"]), expected)
        self.assertNotIn(excluded, [row["symbol"] for row in expected])
        self.restart()
        self.assertEqual(self.service.learning.daily.pending(), prepared)
        self.allow_fixture_orders()
        active = self.activate_at("2026-10-06")
        self.assertEqual(self.learned_signals(self.committed_run(active)), expected)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertNotIn(excluded, [order[0] for order in self.broker.submissions])
        self.service._cycle()
        self.assertEqual(len(self.broker.submissions), 1)
        self.evaluator.assert_called_once()

    def test_current_eligibility_flag_change_preserves_activation_and_frozen_signals(self):
        pending, _, _, _ = self.stage(self.candidate(4))
        self.input_data["rows"][0]["learning_eligible"] = False
        self.restart()
        self.allow_fixture_orders()
        active = self.activate_at("2026-10-06")
        self.assertEqual(self.committed_run(active)["signals"], pending["prepared_run"]["signals"])
        # A research flag alone does not redefine live master/quote permission.

    def test_current_master_halt_blocks_entry_without_blocking_policy_activation(self):
        pending, _, _, _ = self.stage(self.candidate(4))
        self.input_data["rows"][0]["learning_eligible"] = False
        self.master["rows"][fixtures.SYMBOLS[0]]["halted"] = True
        self.restart()
        self.allow_fixture_orders()
        active = self.activate_at("2026-10-06")
        self.assertEqual(self.committed_run(active)["signals"], pending["prepared_run"]["signals"])
        self.assertEqual([order[0] for order in self.broker.submissions], [fixtures.SYMBOLS[1]])
        self.service._cycle()
        self.assertEqual(len(self.broker.submissions), 1)

    def test_current_unknown_master_blocks_entry_without_blocking_policy_activation(self):
        pending, _, _, _ = self.stage(self.candidate(4))
        self.input_data["rows"][0]["learning_eligible"] = False
        self.master["rows"].pop(fixtures.SYMBOLS[0])
        self.restart()
        self.allow_fixture_orders()
        active = self.activate_at("2026-10-06")
        self.assertEqual(self.committed_run(active)["signals"], pending["prepared_run"]["signals"])
        self.assertEqual([order[0] for order in self.broker.submissions], [fixtures.SYMBOLS[1]])

    def test_current_quote_ineligible_blocks_entries_without_blocking_policy_activation(self):
        pending, _, _, _ = self.stage(self.candidate(4))
        for row in self.input_data["rows"]:
            row["learning_eligible"] = False
        self.broker.eligible = False
        self.restart()
        self.allow_fixture_orders()
        active = self.activate_at("2026-10-06")
        self.assertEqual(self.committed_run(active)["signals"], pending["prepared_run"]["signals"])
        self.assertEqual(self.broker.submissions, [])

    def test_new_symbol_before_preparation_is_ignored_by_the_frozen_contract(self):
        candidate = self.candidate(4)
        expected = signals(candidate, build_frame(self.input_data))
        pending, baseline, _, _ = self.stage(candidate, prepare=False)
        new_symbol = "051910"
        self.input_data["rows"].append({**deepcopy(self.input_data["rows"][0]), "symbol": new_symbol})
        self.input_data["histories"][new_symbol] = deepcopy(self.input_data["histories"][fixtures.SYMBOLS[0]])
        for bar in self.input_data["histories"][new_symbol].values():
            bar["turnover"] = 1000000
        self.assertEqual(signals(candidate, build_frame(self.input_data))[0]["symbol"], new_symbol)
        self.assertTrue(self.service._prepare_daily(deepcopy(self.input_data), baseline))
        self.assertEqual(self.learned_signals(self.service.learning.daily.pending()["prepared_run"]), expected)
        self.restart()
        self.allow_fixture_orders()
        active = self.activate_at("2026-10-06")
        self.assertEqual(self.learned_signals(self.committed_run(active)), expected)
        self.assertEqual(active["candidate_id"], pending["candidate_id"])
        self.assertEqual([order[0] for order in self.broker.submissions], [fixtures.SYMBOLS[0]])

    def test_legacy_baseline_changes_before_preparation_cannot_replace_frozen_signals(self):
        pending, baseline, _, _ = self.stage(deepcopy(BASELINE_POLICY), prepare=False)
        original = deepcopy([row for row in baseline["signals"] if not row.get("policy_id")])
        record = deepcopy(self.service.learning.daily.history()[0])
        self.assertEqual(pending["signal_inputs"]["baseline_signals"], original)
        self.assertEqual(record["signal_inputs"], pending["signal_inputs"])
        baseline["signals"][0].update(action="hold", reason="Later baseline replacement")
        baseline["signals"].append({**fixtures.decision("051910"), "reason": "Later new symbol"})
        self.current = datetime(2026, 10, 6, 8, 59, 59, tzinfo=KST)
        self.assertTrue(self.service._prepare_daily(deepcopy(self.input_data), baseline))
        prepared = self.service.learning.daily.pending()
        self.assertEqual(prepared["prepared_run"]["signals"], original)
        self.assertEqual(self.service.learning.daily.history(), [record])
        self.restart()
        self.assertEqual(self.service.learning.daily.pending(), prepared)
        self.allow_fixture_orders()
        active = self.activate_at("2026-10-06")
        self.assertEqual(self.committed_run(active)["signals"], original)
        self.assertTrue(self.broker.submissions)
        self.assertIn((fixtures.SYMBOLS[0], "buy"), [order[:2] for order in self.broker.submissions])
        self.assertNotIn("051910", [order[0] for order in self.broker.submissions])
        original_submissions = deepcopy(self.broker.submissions)
        self.service._cycle()
        self.restart()
        self.service._cycle()
        self.assertEqual(self.broker.submissions, original_submissions)

    def test_prepared_analysis_preserves_original_selection_observation_and_exact_input_hash(self):
        pending, baseline, _, _ = self.stage(self.candidate(4), prepare=False)
        original_input = deepcopy(baseline["analysis"]["input"])
        original_record = deepcopy(self.service.learning.daily.history()[0])
        new_symbol = "051910"
        self.input_data["rows"].append({**deepcopy(self.input_data["rows"][0]), "symbol": new_symbol})
        self.input_data["histories"][new_symbol] = deepcopy(self.input_data["histories"][fixtures.SYMBOLS[0]])
        self.input_data["rows"][0].update(status="error", selection={"status": "excluded"}, as_of="2026-10-01")
        self.input_data["rows"][1]["selection"] = {"status": "excluded"}
        self.current = datetime(2026, 10, 6, 8, 59, 59, tzinfo=KST)
        self.input_data["observed_at"] = self.current.isoformat()
        later_input = build_snapshot(**self.input_data)
        self.assertNotEqual(later_input["rows"], original_input["rows"])
        self.assertIn(new_symbol, [row["symbol"] for row in later_input["rows"]])
        baseline["input_hash"] = "later-baseline-hash-must-not-be-reused"
        self.assertTrue(self.service._prepare_daily(deepcopy(self.input_data), baseline))
        prepared = self.service.learning.daily.pending()
        run = prepared["prepared_run"]
        self.assertEqual(run["analysis"]["input"], original_input)
        expected_hash = hashlib.sha256(json.dumps(original_input, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(run["input_hash"], expected_hash)
        self.assertEqual(run["analysis"]["input_hash"], expected_hash)
        self.assertEqual(run["analysis"]["input"]["observed_at"], original_input["observed_at"])
        self.assertEqual(self.service.learning.daily.history(), [original_record])
        self.restart()
        self.assertEqual(self.service.learning.daily.pending(), prepared)
        self.allow_fixture_orders()
        active = self.activate_at("2026-10-06")
        self.assertEqual(active["candidate_id"], pending["candidate_id"])
        self.assertEqual(self.committed_run(active), run)
        self.assertNotIn(new_symbol, [order[0] for order in self.broker.submissions])

    def test_frozen_price_correction_blocks_activation(self):
        self.assert_material_change_blocks(lambda: self.input_data["histories"][fixtures.SYMBOLS[0]][
            date(2026, 10, 2)].update(close=10001))

    def test_frozen_volume_correction_blocks_activation(self):
        self.assert_material_change_blocks(lambda: self.input_data["histories"][fixtures.SYMBOLS[0]][
            date(2026, 10, 2)].update(volume=1001))

    def test_legacy_63rd_bar_correction_blocks_even_when_learning_features_are_unchanged(self):
        symbol = fixtures.SYMBOLS[0]
        days = self.input_data["calendars"]["KOSPI"]
        self.assertEqual(len(days), MINIMUM_SESSIONS)
        for index, day in enumerate(days):
            close = 9000 if index < 40 else 10000
            if index >= 59:
                close = [11000, 10900, 10800, 11050][index - 59]
            self.input_data["histories"][symbol][day].update(
                open=close, close=close, high=close + 10, low=close - 10)
        before = deepcopy(self.input_data)
        self.assertTrue(evaluate_pullback(before["histories"][symbol], days)["signal"])

        def correct_oldest_bar():
            self.input_data["histories"][symbol][days[0]].update(
                open=100000, close=100000, high=100010, low=99990)
            self.assertEqual(build_frame(self.input_data), build_frame(before))
            self.assertFalse(evaluate_pullback(self.input_data["histories"][symbol], days)["signal"])

        self.assert_material_change_blocks(correct_oldest_bar)

    def test_missing_frozen_symbol_blocks_activation(self):
        self.assert_material_change_blocks(lambda: self.input_data["rows"].pop())

    def test_changed_frozen_calendar_blocks_activation(self):
        self.assert_material_change_blocks(lambda: self.input_data["calendars"]["KOSPI"].pop(0))

    def test_preparation_at_nine_is_rejected_without_publishing_or_rewriting_decision(self):
        pending, baseline, _, _ = self.stage(self.candidate(4), prepare=False)
        original = deepcopy(self.service.learning.daily.history()[0])
        self.current = datetime(2026, 10, 6, 9, 0, tzinfo=KST)
        self.assertFalse(self.service._prepare_daily(deepcopy(self.input_data), baseline))
        self.restart()
        self.allow_fixture_orders()
        self.current = datetime(2026, 10, 6, 9, 5, tzinfo=KST)
        self.service._cycle()
        self.assertIsNone(self.service.learning.daily.active())
        self.assertNotIn("prepared_run", self.service.learning.daily.pending())
        self.assertEqual(self.service.learning.daily.history(), [original])
        self.assertTrue(all(order.get("policy_id") != pending["candidate_id"]
                            for order in self.service.store.orders()))

    def test_non_session_waits_then_only_first_following_session_activates(self):
        pending, _, _, _ = self.stage(self.candidate(4))
        self.allow_fixture_orders()
        self.current = datetime(2026, 10, 5, 9, 5, tzinfo=KST)
        self.service._cycle()  # Weekday omitted from the broker's session calendar.
        self.assertIsNone(self.service.learning.daily.active())
        self.assertEqual(self.service.learning.daily.pending(), pending)
        self.assertEqual(self.broker.submissions, [])
        self.restart()
        active = self.activate_at("2026-10-06")
        self.assertEqual(active["run_id"], pending["prepared_run"]["id"])

    def test_missing_the_first_following_session_cannot_activate_on_a_later_day(self):
        pending, _, _, _ = self.stage(self.candidate(4))
        self.restart()
        self.allow_fixture_orders()
        self.current = datetime(2026, 10, 7, 9, 5, tzinfo=KST)
        self.service._cycle()
        self.assertIsNone(self.service.learning.daily.active())
        self.assertEqual(self.service.learning.daily.pending(), pending)
        self.assertEqual(self.broker.submissions, [])
        self.assertNotIn(pending["prepared_run"]["id"],
                         [run["id"] for run in self.service.store.runs()])


if __name__ == "__main__":
    unittest.main()
