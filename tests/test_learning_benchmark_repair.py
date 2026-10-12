"""Offline reporting repair must preserve frozen prospective trial evidence."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.experiment_store import ExperimentStore, encode
from backend.learning import LearningService
from tests.test_learning import LIMITS, candidate, raw_input


class BenchmarkRepairIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ExperimentStore(Path(temporary.name) / "experiments.sqlite3")
        self.current = datetime.fromisoformat(raw_input(0)["observed_at"])
        self.proposals = []
        self.fingerprint = "fixture-account"
        self.limits = {**LIMITS, "fingerprint": self.fingerprint}

        def propose(champion, frames, limits, history, llm=None):
            self.proposals.append(deepcopy(frames))
            return {"policy": candidate(), "method": "test", "development": None}

        self.proposer = propose
        self.learner = self.make_learner()
        self.old_version = "previous-reviewed-reporting-engine"
        compatibility = patch("backend.learning_compatibility.REPORTING_ONLY_UPGRADES",
                              frozenset({(self.old_version, self.learner.version)}))
        compatibility.start()
        self.addCleanup(compatibility.stop)
        network = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.store.save_settings({"enabled": False, "user_paused": True,
                                  "policy": {"fingerprint": self.fingerprint}})

    def make_learner(self):
        learner = LearningService(self.store, enabled=True, now=lambda: self.current,
                                  proposer=self.proposer)
        self.addCleanup(learner.close)
        return learner

    @staticmethod
    def date_input(index=0, *, short=False):
        data = raw_input(index)
        data["benchmarks"] = {board: {date.fromisoformat(day): value for day, value in bars.items()}
                              for board, bars in data["benchmarks"].items()}
        if short:
            keep = set(data["calendars"]["KOSPI"][-20:])
            data["calendars"]["KOSPI"] = sorted(keep)
            data["histories"] = {symbol: {day: bar for day, bar in bars.items() if day in keep}
                                 for symbol, bars in data["histories"].items()}
        return data

    def seed_legacy(self, *, short=False):
        data = self.date_input(short=short)
        price = data["histories"]["005930"][data["as_of"]]["close"]
        self.context = {"initial_state": {"as_of": data["as_of"], "cash": str(1000000 - price),
            "equity": "1000000", "budget": "1000000", "reserved_cash": "0",
            "pending_entries": [], "pending_orders": 0, "unresolved_orders": 0,
            "positions": [{"symbol": "005930", "strategy_id": "trend-breakout-v1", "quantity": 1,
                "cost": str(price), "entry_price": str(price), "last_close": str(price),
                "entry_date": data["as_of"], "held_sessions": 0, "exit_pending": False,
                "exit_policy": {"holding_sessions": 5, "stop_loss_pct": 0, "take_profit_pct": 0}}]},
            "execution_quotes": {"005930": {"price": str(price), "eligible": True}}}
        account = {"unit": "1", "equity": "1000000", "cash": str(1000000 - price),
                   "return_pct": 0, "as_of": self.current.isoformat()}
        self.store.save_setting("learning_account:" + self.fingerprint, account)
        self.learner.process(data, [], self.limits, context=self.context)
        state = self.learner.state()
        state["engine_version"] = self.old_version
        state["trial"]["engine_version"] = self.old_version
        state["scorecard_engine"] = self.old_version
        state["scorecard_market"] = None
        state["scorecard"]["market_return_pct"] = None
        state["scorecard"]["error"] = None
        state.pop("scorecard_market_error", None)
        # Reconstruct the pre-fix persisted evidence, only within this temporary DB.
        with self.store.connect() as db:
            day, payload = db.execute("SELECT day,payload FROM learning_frames").fetchone()
            frame = json.loads(payload)
            frame["benchmark_prices"] = {}
            digest = hashlib.sha256(encode({key: value for key, value in frame.items()
                if key not in {"observed_at", "captured_at"}}).encode()).hexdigest()
            db.execute("UPDATE learning_frames SET digest=?,payload=? WHERE day=?",
                       (digest, encode(frame), day))
            db.execute("UPDATE learning_trials SET payload=?", (encode(state["trial"]),))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_frame_benchmarks'").fetchone():
                db.execute("DELETE FROM learning_frame_benchmarks")
            db.execute("INSERT INTO orders VALUES (?,?,?,?)",
                       ("fixture-fill", "fixture-intent", self.current.isoformat(),
                        encode({"id": "fixture-fill", "side": "buy", "status": "filled",
                                "fingerprint": self.fingerprint, "symbol": "005930",
                                "quantity": 1, "filled_quantity": 1, "limit_price": str(price)})))
            db.execute("INSERT OR REPLACE INTO learning_daily VALUES (?,?,?)",
                       (self.fingerprint, day, encode({"as_of": day, "equity": "1000000",
                           "return_pct": 0, "captured_at": self.current.isoformat(),
                           "comparisons": state["scorecard"]})))
        self.store.save_setting("learning", state)
        self.original_state = deepcopy(state)
        self.original_evidence = self.evidence()
        return data

    def evidence(self):
        with self.store.connect() as db:
            return {"frames": db.execute("SELECT day,digest,payload FROM learning_frames ORDER BY day").fetchall(),
                    "trials": db.execute("SELECT id,created_at,payload FROM learning_trials ORDER BY id").fetchall(),
                    "policies": db.execute("SELECT id,payload FROM learning_policies ORDER BY id").fetchall(),
                    "orders": db.execute("SELECT id,payload FROM orders ORDER BY id").fetchall()}

    def assert_original_trial(self, learner=None):
        learner = learner or self.learner
        self.assertEqual(learner.state()["trial"], self.original_state["trial"])
        self.assertEqual(self.evidence(), self.original_evidence)
        self.assertEqual(len(self.proposals), 1)
        self.assertFalse(self.store.setting("enabled"))
        self.assertTrue(self.store.setting("user_paused"))

    @staticmethod
    def drain(learner):
        worker = learner.worker
        if worker:
            worker.join(5)
            if worker.is_alive():
                raise AssertionError("offline learning worker did not finish")

    def test_same_day_repair_and_restart_keep_original_trial_and_evidence(self):
        data = self.seed_legacy()
        self.learner.process(data, [], self.limits, context=self.context)
        self.assert_original_trial()
        state = self.learner.state()
        self.assertEqual(state["engine_version"], self.learner.version)
        self.assertEqual(state["scorecard_engine"], self.old_version)
        self.assertEqual(Decimal(str(state["scorecard_market"])), 100)
        self.assertEqual(state["scorecard"]["market_return_pct"], 0)
        self.assertIsNone(state.get("scorecard_market_error"))
        for key in ("scorecard_start", "scorecard_capital", "scorecard_ai_unit", "scorecard_book"):
            self.assertEqual(state[key], self.original_state[key])
        with self.store.connect() as db:
            supplements = db.execute("SELECT day,board,frame_digest,payload FROM learning_frame_benchmarks").fetchall()
            daily = json.loads(db.execute("SELECT payload FROM learning_daily").fetchone()[0])
        self.assertEqual(len(supplements), 1)
        self.assertEqual(supplements[0][2], self.original_evidence["frames"][0][1])
        self.assertEqual(daily["comparisons"]["market_return_pct"], 0)
        self.assertEqual(daily["equity"], "1000000")
        restored = self.make_learner()
        self.current += timedelta(hours=1)
        data["observed_at"] = self.current.isoformat()
        restored.process(data, [], self.limits, context=self.context)
        self.assert_original_trial(restored)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT day,board,frame_digest,payload FROM learning_frame_benchmarks").fetchall(),
                             supplements)

    def test_same_source_missing_then_available_retries_reporting_without_reproposal(self):
        data = self.seed_legacy(short=True)
        missing = {**data, "benchmarks": {}}
        self.learner.enqueue(missing, [], self.limits, source_id="same-source", context=self.context)
        self.drain(self.learner)
        self.assertIsNotNone(self.learner.state().get("scorecard_market_error"))
        self.assertIsNotNone(self.learner.state()["scorecard"]["error"])
        self.assertIsNone(self.learner.state()["scorecard"]["market_return_pct"])
        self.assert_original_trial()
        self.learner.enqueue(data, [], self.limits, source_id="same-source", context=self.context)
        self.drain(self.learner)
        self.assertEqual(self.learner.state()["scorecard"]["market_return_pct"], 0)
        self.assertIsNone(self.learner.state().get("scorecard_market_error"))
        self.assertIsNone(self.learner.state()["scorecard"]["error"])
        self.assert_original_trial()

    def test_following_day_uses_original_anchor_and_only_one_forward_interval(self):
        data = self.seed_legacy()
        self.learner.process(data, [], self.limits, context=self.context)
        following = self.date_input(1)
        self.current = datetime.fromisoformat(following["observed_at"])
        following["benchmarks"]["KOSPI"][date.fromisoformat(following["as_of"])] = 110
        self.learner.process(following, [], self.limits, context=self.context)
        state = self.learner.state()
        self.assertAlmostEqual(state["scorecard"]["market_return_pct"], 10)
        self.assertEqual(state["scorecard_start"], data["as_of"])
        self.assertEqual(Decimal(str(state["scorecard_market"])), 100)
        self.assertEqual(state["scorecard"]["status"], "ready")
        for key in ("id", "created_at", "start_day", "initial_state", "evaluation_spec", "policy", "engine_version"):
            self.assertEqual(state["trial"][key], self.original_state["trial"][key])
        self.assertEqual(state["trial"]["elapsed_intervals"], 1)
        self.assertEqual(state["trial"]["evaluation_spec"]["horizon"], 60)
        self.assertNotEqual(state["trial"]["decision"], "invalid")
        self.assertEqual(len(self.proposals), 1)
        self.assertEqual(self.evidence()["frames"][0], self.original_evidence["frames"][0])

    def test_unapproved_engine_change_still_invalidates_original_window(self):
        data = self.seed_legacy()
        self.learner.process(data, [], self.limits, context=self.context)
        self.learner.version = "unreviewed-evaluator-change"
        following = self.date_input(1)
        self.current = datetime.fromisoformat(following["observed_at"])
        self.learner.process(following, [], self.limits, context=self.context)
        trial = self.learner.state()["trial"]
        self.assertEqual(trial["decision"], "invalid")
        self.assertEqual(trial["invalid_reason"], "평가 코드 버전 변경")
        for key in ("id", "start_day", "evaluation_spec", "initial_state", "engine_version"):
            self.assertEqual(trial[key], self.original_state["trial"][key])
        self.assertEqual(self.evidence()["frames"][0], self.original_evidence["frames"][0])

    def test_same_day_stock_price_change_is_still_rejected(self):
        data = self.seed_legacy()
        data["histories"]["005930"][data["as_of"]]["close"] += 1
        self.assert_changed_evidence_rejected(data, [], self.context)

    def test_same_day_execution_quote_change_is_still_rejected(self):
        data = self.seed_legacy()
        context = deepcopy(self.context)
        context["execution_quotes"]["005930"]["price"] = "12345"
        self.assert_changed_evidence_rejected(data, [], context)

    def test_same_day_baseline_signal_change_is_still_rejected(self):
        data = self.seed_legacy()
        signals = [{"symbol": "005930", "strategy_id": "trend-breakout-v1", "signal": "buy", "status": "ready"}]
        self.assert_changed_evidence_rejected(data, signals, self.context)

    def assert_changed_evidence_rejected(self, data, signals, context):
        self.learner.process(data, signals, self.limits, context=context)
        state = self.learner.state()
        self.assertEqual(state["trial"]["decision"], "invalid")
        self.assertIn("동일 거래일 관측", state["error"])
        self.assertEqual(self.evidence()["frames"], self.original_evidence["frames"])
        self.assertEqual(state["trial"]["id"], self.original_state["trial"]["id"])
        self.assertEqual(state["trial"]["evaluation_spec"], self.original_state["trial"]["evaluation_spec"])
        self.assertEqual(len(self.proposals), 1)

    def test_market_repair_does_not_clear_unrelated_comparison_error(self):
        data = self.seed_legacy()
        state = self.learner.state()
        state["scorecard"].update(status="invalid", error="최초 정책 비교 자료 또는 평가 버전 변경")
        self.store.save_setting("learning", state)
        self.learner.process(data, [], self.limits, context=self.context)
        repaired = self.learner.state()["scorecard"]
        self.assertEqual(repaired["market_return_pct"], 0)
        self.assertEqual(repaired["status"], "invalid")
        self.assertEqual(repaired["error"], state["scorecard"]["error"])
        self.assert_original_trial()


if __name__ == "__main__":
    unittest.main()
