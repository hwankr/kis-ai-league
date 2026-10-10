"""Offline evidence -> research -> prospective comparison -> durable adoption."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from backend.experiment_store import ExperimentStore
from backend.kis import KST
from backend.learning import LearningService
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
        for index in range(39):
            self.advance(index)
        self.assertEqual(self.learner.champion()["kind"], "legacy")
        self.advance(39)
        self.assertEqual(policy_id(self.learner.champion()), policy_id(candidate()))
        evaluation = self.learner.snapshot()["last_evaluation"]
        self.assertEqual(evaluation["decision"], "promote")
        self.assertGreaterEqual(evaluation["closed_trades"], 20)
        self.assertGreater(evaluation["challenger_return_pct"], 1)
        self.assertEqual(self.learner.snapshot()["challenger"]["sessions"], 0)
        restored = LearningService(self.store, enabled=True, now=lambda: self.current, proposer=self.learner.proposer)
        self.addCleanup(restored.close)
        self.assertEqual(restored.champion(), self.learner.champion())
        self.assertFalse(self.store.setting("enabled"))
        self.assertTrue(self.store.setting("user_paused"))
        self.assertEqual(self.store.orders(), [])
        self.assertEqual(sum(item["decision"] == "promote" for item in restored.history()), 1)

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
        self.assertEqual(self.learner.snapshot()["challenger"]["sessions"], 2)

    def test_changed_same_day_evidence_is_not_replaced(self):
        data = self.advance(0)
        original = self.learner.frames()[0]
        data["histories"]["005930"][data["as_of"]]["volume"] += 1
        self.learner.process(data, [], LIMITS)
        self.assertEqual(self.learner.frames()[0], original)
        self.assertIsNone(self.learner.snapshot()["challenger"])
        self.assertEqual(self.learner.history()[0]["decision"], "invalid")
        self.assertEqual(self.learner.champion()["kind"], "legacy")

    def test_changed_historical_price_invalidates_cohort_not_previous_record(self):
        first = self.advance(0)
        self.advance(1)
        data = raw_input(2)
        self.current = datetime.fromisoformat(data["observed_at"])
        data["histories"]["005930"][first["as_of"]]["close"] -= 1
        self.learner.process(data, [], LIMITS)
        self.assertEqual(self.learner.snapshot()["last_evaluation"]["decision"], "invalid")
        self.assertEqual(self.learner.champion()["kind"], "legacy")

    def test_late_observation_and_session_gap_cannot_support_promotion(self):
        self.advance(0)
        data = raw_input(1)
        self.current = datetime.fromisoformat(raw_input(2)["as_of"] + "T10:00:00+09:00")
        self.learner.process(data, [], LIMITS)
        self.advance(2)
        self.assertEqual(self.learner.snapshot()["last_evaluation"]["decision"], "invalid")
        self.advance(4)
        # The interrupted cohort restarts, rather than assigning skipped returns zero.
        self.assertEqual(self.learner.champion()["kind"], "legacy")

    def test_judge_rejects_negative_stress_large_drawdown_and_inconsistent_halves(self):
        base = {"valid": True, "sessions": 40, "closed_trades": 30, "net_return_pct": 1.,
                "stress_return_pct": 0., "max_drawdown_pct": 1., "stress_drawdown_pct": 2., "half_returns": [.5, .5]}
        winner = {**base, "net_return_pct": 5., "stress_return_pct": 3., "half_returns": [2., 3.]}
        self.assertEqual(self.learner.judge(base, winner)[0], "promote")
        for changes in ({"stress_return_pct": -.1}, {"max_drawdown_pct": 90.},
                        {"max_drawdown_pct": -90.}, {"half_returns": [-1., 6.]}, {"valid": False}):
            self.assertNotEqual(self.learner.judge(base, {**winner, **changes})[0], "promote")

    def test_deterioration_uses_new_period_and_restores_previous_policy(self):
        for index in range(20):
            self.advance(index)
        active = candidate()
        state = self.learner.state()
        state.update(champion={"policy": active, "adopted_at": self.current.isoformat()},
                     guard={"policy": BASELINE_POLICY, "limits": LIMITS, "start_day": raw_input(0)["as_of"]})
        self.store.save_setting("learning", state)
        def evaluate(policy, frames, limits):
            good = policy["kind"] == "legacy"
            return {"valid": True, "sessions": len(frames), "closed_trades": 20,
                    "net_return_pct": 0 if good else -10, "stress_return_pct": -1 if good else -12,
                    "max_drawdown_pct": 1 if good else 12, "half_returns": [0, 0] if good else [-5, -5]}
        self.learner.evaluator = evaluate
        self.advance(20)
        self.assertEqual(self.learner.champion()["kind"], "legacy")
        self.assertIn("복귀", self.learner.snapshot()["last_change"]["reason"])
        self.assertFalse(self.store.setting("enabled"))
        rollback = next(item for item in self.learner.history() if item["decision"] == "rollback")
        self.assertEqual(rollback["metrics"]["challenger"]["net_return_pct"], -10)
        self.assertEqual(rollback["id"], self.learner.snapshot()["last_change"]["trial_id"])

    def test_changed_limits_restart_comparison_without_mutating_account_settings(self):
        self.advance(0)
        data = raw_input(1)
        self.current = datetime.fromisoformat(data["observed_at"])
        limits = {**LIMITS, "order_cap": "100000"}
        self.learner.process(data, [], limits)
        self.assertEqual(self.learner.state()["trial"]["limits"], limits)
        self.assertIn("운용 한도", next(item["reason"] for item in self.learner.history() if item["decision"] == "invalid"))
        self.assertIsNone(self.store.setting("policy"))

    def test_engine_change_restarts_trial_and_keeps_original_evidence(self):
        self.advance(0)
        original = self.learner.frames()
        identity = self.learner.state()["trial"]["id"]
        self.learner.version = "new-evaluation-version"
        self.advance(1)
        self.assertNotEqual(self.learner.state()["trial"]["id"], identity)
        retired = next(item for item in self.learner.history() if item["id"] == identity)
        self.assertEqual(retired["reason"], "평가 코드 버전 변경")
        self.assertEqual(self.learner.frames()[0], original[0])
        self.assertEqual(self.learner.state()["trial"]["engine_version"], self.learner.version)

    def test_execution_feedback_excludes_unresolved_and_other_accounts(self):
        self.store.save_setting("policy", {"fingerprint": "owned"})
        for index, (status, filled, fingerprint) in enumerate([
                ("filled", 10, "owned"), ("cancelled", 2, "owned"), ("rejected", 0, "owned"),
                ("unknown", 0, "owned"), ("submitted", 0, "owned"), ("rejected", 0, "other")]):
            self.store.reserve_order({"id": str(index), "created_at": self.current.isoformat(), "status": status,
                                      "filled_quantity": filled, "quantity": 10, "fingerprint": fingerprint}, str(index))
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
        self.assertEqual(self.learner.snapshot()["last_evaluation"]["sessions"], 2)
        self.assertEqual(self.learner.snapshot()["last_evaluation"]["decision"], "waiting")


if __name__ == "__main__":
    unittest.main()
