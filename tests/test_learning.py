"""Offline evidence -> research -> prospective comparison -> durable adoption."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from backend.experiment_store import ExperimentStore
from backend.kis import KST
from backend.learning import LearningService
from backend.learning_evaluation import daily_metrics, make_evaluation_spec
from backend.learning_policy import BASELINE_POLICY, policy_id
from tests.test_learning_policy import days, policy


LIMITS = {"budget": "1000000", "order_cap": "250000", "daily_buy_limit": "500000"}


def raw_input(index):
    calendar = days(60 + index)
    prices = [round(10000 * 1.01 ** i, 2) for i in range(len(calendar))]
    bars = {day: {"open": price, "high": price + 10, "low": price - 10, "close": price,
                  "volume": 1000, "turnover": price * 1000} for day, price in zip(calendar, prices)}
    return {"as_of": calendar[-1], "observed_at": calendar[-1] + "T16:30:00+09:00",
            "rows": [{"symbol": "005930", "name": "테스트", "board": "KOSPI", "status": "ok",
                      "selection": {"status": "selected"}, "as_of": calendar[-1]}],
            "histories": {"005930": bars}, "calendars": {"KOSPI": calendar},
            "benchmarks": {"KOSPI": {day: 100 for day in calendar}}}


def candidate(hold=1):
    result = policy(entry_slippage_bps=200)
    result["strategies"][0]["holding_sessions"] = hold
    return result


class LearningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ExperimentStore(Path(temporary.name) / "experiments.sqlite3")
        self.current = datetime.fromisoformat(raw_input(0)["observed_at"])
        self.proposals = []
        def propose(champion, frames, limits, history, llm=None):
            value = candidate(1 if champion["kind"] == "legacy" else 2)
            self.proposals.append(deepcopy(value))
            return {"policy": value, "rationale": "테스트 후보", "method": "test", "development": None}
        self.learner = LearningService(self.store, enabled=True, now=lambda: self.current, proposer=propose)
        self.addCleanup(self.learner.close)
        self.store.save_settings({"enabled": False, "user_paused": True})

    def advance(self, index):
        data = raw_input(index)
        self.current = datetime.fromisoformat(data["observed_at"])
        self.learner.process(data, [], LIMITS)
        return data

    def test_prospective_evidence_promotes_and_survives_restart_without_unpausing(self):
        for index in range(60):
            self.advance(index)
        self.assertEqual(self.learner.champion()["kind"], "legacy")
        before = self.learner.state()["trial"]
        self.assertEqual(before["elapsed_intervals"], 59)
        self.assertEqual(before["decision"], "waiting")
        self.assertEqual(before["evaluation_spec"], make_evaluation_spec(1))
        self.advance(60)
        self.assertEqual(policy_id(self.learner.champion()), policy_id(candidate()))
        evaluation = self.learner.snapshot()["last_evaluation"]
        self.assertEqual(evaluation["decision"], "promote")
        self.assertEqual(evaluation["sessions"], 60)
        self.assertEqual(evaluation["required_sessions"], 60)
        self.assertEqual(evaluation["phase"], "paper_provisional")
        self.assertGreater(evaluation["challenger_return_pct"], 1)
        self.assertEqual(self.learner.snapshot()["challenger"]["sessions"], 0)
        restored = LearningService(self.store, enabled=True, now=lambda: self.current, proposer=self.learner.proposer)
        self.addCleanup(restored.close)
        self.assertEqual(restored.champion(), self.learner.champion())
        self.assertFalse(self.store.setting("enabled"))
        self.assertTrue(self.store.setting("user_paused"))
        self.assertEqual(self.store.orders(), [])
        self.assertEqual(sum(item["decision"] == "promote" for item in restored.history()), 1)
        promoted = next(item for item in restored.history() if item["decision"] == "promote")
        self.assertEqual(promoted["metrics"]["champion"]["intervals"], 60)
        self.assertEqual(promoted["metrics"]["challenger"]["intervals"], 60)
        self.assertEqual(len(promoted["metrics"]["challenger"]["daily_log_returns"]), 60)
        self.assertEqual(restored.state()["trial_sequence"], 2)

    def test_same_observation_restart_and_timestamp_refresh_do_not_research_again(self):
        data = self.advance(0)
        self.current += timedelta(hours=1)
        data["observed_at"] = self.current.isoformat()
        self.learner.process(data, [], LIMITS)
        restored = LearningService(self.store, enabled=True, now=lambda: self.current, proposer=self.learner.proposer)
        self.addCleanup(restored.close)
        restored.process(data, [], LIMITS)
        self.assertEqual(len(self.proposals), 1)
        self.assertEqual(len(restored.frames()), 1)

    def test_candidate_created_after_history_cannot_backdate_evaluation(self):
        data = raw_input(0)
        self.current = datetime.fromisoformat(raw_input(3)["observed_at"])
        self.learner.process(data, [], LIMITS)
        self.assertEqual(self.learner.state()["trial"]["start_day"], self.current.date().isoformat())
        self.advance(3)
        identity = self.learner.state()["trial"]["id"]
        self.assertEqual(self.learner.snapshot()["last_evaluation"]["decision"], "waiting")
        self.advance(4)
        self.assertEqual(self.learner.state()["trial"]["id"], identity)
        self.assertEqual(self.learner.snapshot()["challenger"]["sessions"], 1)

    def test_changed_same_day_evidence_is_not_replaced(self):
        data = self.advance(0)
        original = self.learner.frames()[0]
        data["histories"]["005930"][data["as_of"]]["volume"] += 1
        self.learner.process(data, [], LIMITS)
        self.assertEqual(self.learner.frames()[0], original)
        self.assertEqual(self.learner.state()["trial"]["decision"], "invalid")
        self.assertIsNotNone(self.learner.snapshot()["challenger"])
        self.assertEqual(self.learner.history()[0]["decision"], "invalid")
        self.assertEqual(self.learner.champion()["kind"], "legacy")

    def test_changed_historical_price_invalidates_cohort_not_previous_record(self):
        first = self.advance(0)
        original = self.learner.frames()[0]
        identity = self.learner.state()["trial"]["id"]
        self.advance(1)
        data = raw_input(2)
        self.current = datetime.fromisoformat(data["observed_at"])
        data["histories"]["005930"][first["as_of"]]["close"] -= 1
        self.learner.process(data, [], LIMITS)
        self.assertEqual(self.learner.snapshot()["last_evaluation"]["decision"], "invalid")
        self.assertEqual(self.learner.champion()["kind"], "legacy")
        self.assertEqual(self.learner.state()["trial"]["id"], identity)
        self.assertEqual(self.learner.frames()[0], original)
        self.advance(3)  # A later unmodified input must not erase the invalid window.
        self.assertEqual(self.learner.state()["trial"]["decision"], "invalid")
        self.assertEqual(len(self.proposals), 1)

    def test_late_observation_and_session_gap_cannot_support_promotion(self):
        self.advance(0)
        identity = self.learner.state()["trial"]["id"]
        data = raw_input(1)
        self.current = datetime.fromisoformat(raw_input(2)["as_of"] + "T10:00:00+09:00")
        self.learner.process(data, [], LIMITS)
        self.advance(2)
        self.assertEqual(self.learner.snapshot()["last_evaluation"]["decision"], "invalid")
        self.advance(4)
        # Missing observations consume the original window, never a fresh lucky start.
        self.assertEqual(self.learner.champion()["kind"], "legacy")
        self.assertEqual(self.learner.state()["trial"]["id"], identity)
        self.assertEqual(self.learner.state()["trial"]["elapsed_intervals"], 4)
        self.assertEqual(len(self.proposals), 1)

    def test_judge_uses_paired_returns_not_trade_count_or_old_headline_metrics(self):
        base = daily_metrics([0.] * 60)
        winner = {**daily_metrics([.002] * 60, [.0015] * 60), "closed_trades": 0}
        self.assertEqual(self.learner.judge(base, winner)[0], "promote")
        inflated = {**daily_metrics([0.] * 60), "closed_trades": 999, "net_return_pct": 900,
                    "stress_return_pct": 800, "half_returns": [100, 100]}
        self.assertEqual(self.learner.judge(base, inflated)[0], "keep")
        self.assertEqual(self.learner.judge(daily_metrics([0.] * 59), daily_metrics([.002] * 59))[0], "waiting")
        negative_stress = daily_metrics([.002] * 60, [-.001] * 60)
        self.assertEqual(self.learner.judge(base, negative_stress)[0], "keep")

    def test_rolling_guard_cannot_restore_a_policy_outside_a_new_fixed_comparison(self):
        self.advance(0)
        active = candidate(2)
        state = self.learner.state()
        state.update(champion={"policy": active, "adopted_at": self.current.isoformat()},
                     guard={"policy": BASELINE_POLICY, "limits": LIMITS, "start_day": raw_input(0)["as_of"]})
        self.store.save_setting("learning", state)
        self.advance(1)
        self.assertEqual(self.learner.champion(), active)
        self.assertNotIn("guard", self.learner.state())
        self.assertIsNone(self.learner.snapshot()["last_change"])
        self.assertFalse(self.store.setting("enabled"))
        self.assertFalse(any(item["decision"] == "rollback" for item in self.learner.history()))

    def test_changed_limits_invalidate_original_window_without_mutating_account_settings(self):
        self.advance(0)
        original = deepcopy(self.learner.state()["trial"])
        data = raw_input(1)
        self.current = datetime.fromisoformat(data["observed_at"])
        limits = {**LIMITS, "order_cap": "100000"}
        self.learner.process(data, [], limits)
        trial = self.learner.state()["trial"]
        self.assertEqual(trial["id"], original["id"])
        self.assertEqual(trial["limits"], LIMITS)
        self.assertEqual(trial["evaluation_spec"], original["evaluation_spec"])
        self.assertEqual(trial["decision"], "invalid")
        self.assertIn("한도 변경", trial["reason"])
        self.assertIsNone(self.store.setting("policy"))

    def test_engine_change_consumes_original_window_and_keeps_original_evidence(self):
        self.advance(0)
        original = self.learner.frames()
        identity = self.learner.state()["trial"]["id"]
        self.learner.version = "new-evaluation-version"
        self.advance(1)
        self.assertEqual(self.learner.state()["trial"]["id"], identity)
        retired = next(item for item in self.learner.history() if item["id"] == identity)
        self.assertEqual(retired["reason"], "평가 코드 버전 변경")
        self.assertEqual(self.learner.frames()[0], original[0])
        self.assertNotEqual(self.learner.state()["trial"]["engine_version"], self.learner.version)

    def test_execution_feedback_excludes_unresolved_and_other_accounts(self):
        self.store.save_setting("policy", {"fingerprint": "owned"})
        for index, (status, filled, fingerprint) in enumerate([
                ("filled", 10, "owned"), ("cancelled", 2, "owned"), ("rejected", 0, "owned"),
                ("unknown", 0, "owned"), ("submitted", 0, "owned"), ("rejected", 0, "other")]):
            self.store.reserve_order({"id": str(index), "created_at": self.current.isoformat(), "status": status,
                                      "filled_quantity": filled, "quantity": 10, "fingerprint": fingerprint}, str(index))
        self.store.reserve_order({'id': 'skipped', 'created_at': self.current.isoformat(),
                                  'status': 'cancelled', 'filled_quantity': 0, 'quantity': 10,
                                  'fingerprint': 'owned', 'submission_skipped': True}, 'skipped')
        feedback = self.learner._research_history()[0]["execution"]
        self.assertEqual(feedback["terminal_orders"], 3)
        self.assertEqual(feedback["unresolved_orders"], 2)
        self.assertAlmostEqual(feedback["rejection_rate"], 1 / 3)
        self.assertAlmostEqual(feedback["fill_ratio"], .6)
        self.store.save_setting("policy", {"fingerprint": "empty"})
        self.assertIsNone(self.learner._research_history()[0]["execution"]["fill_ratio"])

    def test_historical_development_never_counts_toward_future_promotion(self):
        from backend.learning_policy import build_frame
        old = [build_frame(raw_input(index)) for index in range(45)]
        seen = []
        original = self.learner.proposer
        def propose(champion, frames, limits, history, llm=None):
            seen.append(len(frames))
            return original(champion, frames, limits, history, llm=llm)
        self.learner.proposer = propose
        self.learner.development_reader = lambda _: old
        self.advance(45)
        self.assertEqual(seen, [46])
        self.assertEqual(len(self.learner.frames()), 1)
        self.assertEqual(self.learner.champion()["kind"], "legacy")
        self.advance(46)
        self.assertEqual(self.learner.snapshot()["last_evaluation"]["sessions"], 1)
        self.assertEqual(self.learner.snapshot()["last_evaluation"]["decision"], "waiting")

    def test_invalid_attempt_keeps_deadline_and_sequence_survives_restart(self):
        self.advance(0)
        original = deepcopy(self.learner.state()["trial"])
        self.advance(2)  # Missing day 1 makes the original comparison unusable.
        self.assertEqual(self.learner.state()["trial"]["decision"], "invalid")
        restored = LearningService(self.store, enabled=True, now=lambda: self.current, proposer=self.learner.proposer)
        self.addCleanup(restored.close)
        self.learner = restored
        for index in range(3, 60):
            self.advance(index)
        self.assertEqual(restored.state()["trial"]["id"], original["id"])
        self.assertEqual(restored.state()["trial_sequence"], 1)
        self.assertEqual(len(self.proposals), 1)
        self.advance(60)
        retired = next(item for item in restored.history() if item["id"] == original["id"])
        self.assertEqual(retired["decision"], "invalid")
        self.assertEqual(retired["elapsed_intervals"], 60)
        self.assertEqual(retired["evaluation_spec"], original["evaluation_spec"])
        self.assertEqual(restored.state()["trial_sequence"], 2)
        self.assertEqual(restored.state()["trial"]["evaluation_spec"], make_evaluation_spec(2))
        self.assertFalse(self.store.setting("enabled"))
        self.assertTrue(self.store.setting("user_paused"))

    def test_model_initial_book_and_spec_are_frozen_before_new_observations(self):
        data = raw_input(0)
        book = {"as_of": data["as_of"], "cash": "900000", "positions": [], "reserved_cash": "0",
                "pending_orders": 0, "unresolved_orders": 0, "equity": "900000"}
        model = {"mode": "daily_open", "buy_fill_ratio": .5, "sell_fill_ratio": .8}
        self.learner.process(data, [], LIMITS, context={"initial_state": book, "execution_model": model})
        before = deepcopy(self.learner.state()["trial"])
        model["buy_fill_ratio"] = 0
        book["cash"] = "100"
        data = raw_input(1)
        self.current = datetime.fromisoformat(data["observed_at"])
        book["as_of"] = data["as_of"]
        self.learner.process(data, [], LIMITS, context={"initial_state": book, "execution_model": model})
        after = self.learner.state()["trial"]
        for key in ("id", "limits", "initial_state", "execution_model", "evaluation_spec"):
            self.assertEqual(after[key], before[key])
        self.assertTrue(after["metrics"]["challenger"]["valid"])
        self.assertEqual(after["metrics"]["challenger"]["equity_curve"][0]["equity"], 900000)

    def test_production_trial_waits_for_pending_orders_to_settle(self):
        data = raw_input(0)
        book = {"as_of": data["as_of"], "cash": "900000", "positions": [], "reserved_cash": "100000",
                "pending_orders": 1, "unresolved_orders": 0, "equity": "900000"}
        self.learner.process(data, [], LIMITS, context={"initial_state": book})
        self.assertIsNone(self.learner.state().get("trial"))
        self.assertEqual(len(self.proposals), 0)
        data = raw_input(1)
        self.current = datetime.fromisoformat(data["observed_at"])
        book.update(as_of=data["as_of"], pending_orders=0, reserved_cash="0")
        self.learner.process(data, [], LIMITS, context={"initial_state": book})
        self.assertEqual(len(self.proposals), 1)
        self.assertEqual(self.learner.state()["trial"]["initial_state"]["cash"], "900000")
        self.assertFalse(self.store.setting("enabled"))


if __name__ == "__main__":
    unittest.main()
