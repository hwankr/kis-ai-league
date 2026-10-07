from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from backend.autonomy import AutonomousMonitor
from backend.experiment_store import ExperimentStore
from backend.history import series_for_profile
from backend.kis import KST, KisError, load_profiles


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        config = self.root / "config.toml"
        config.write_text("[accounts.paper]\napp_key='test-key'\napp_secret='test-secret'\naccount='12345678'\n", encoding="utf-8")
        self.store = ExperimentStore(self.root / "state.sqlite3")
        self.fingerprint = series_for_profile(load_profiles(config).select("paper")).fingerprint
        self.store.save_setting("policy", {"account_id": "paper", "fingerprint": self.fingerprint})
        self.store.save_setting("enabled", True)
        self.experiments = Mock(config_path=config, store=self.store)
        self.accounts, self.candidates = Mock(), Mock()
        self.candidates.snapshot.return_value = {"status": "complete", "stale": False}
        self.current = datetime(2026, 10, 6, 10, tzinfo=KST)
        self.monitor = AutonomousMonitor(self.experiments, self.accounts, self.candidates, now=lambda: self.current)
        self.addCleanup(self.monitor.close)

    def account(self, total="1000", cash="1000"):
        self.accounts.snapshot.return_value = {"status": "ok", "stale": False,
            "updated_at": self.current.isoformat(), "summary": {"total_value": total, "cash": cash}}
        self.store.save_setting("account_retry_at", None)
        self.monitor._account()

    def test_background_without_browser_and_duplicate_bucket(self):
        self.accounts.snapshot.return_value = {"status": "ok", "stale": False,
            "updated_at": self.current.isoformat(), "summary": {"total_value": "1000", "cash": "900"}}
        self.monitor.tick()
        self.monitor.worker.join(5)
        self.assertFalse(self.monitor.worker.is_alive())
        self.accounts.snapshot.assert_called_once_with("paper")
        self.account()
        result = self.monitor.snapshot()
        self.assertEqual(result["performance"]["observations"], 1)
        self.assertIsNotNone(result["last_heartbeat_at"])
        self.assertIsNotNone(result["last_account_at"])

    def test_returns_drawdown_and_restart_preserve_full_baseline(self):
        self.account()
        self.current += timedelta(minutes=5)
        self.account("1100")
        self.current += timedelta(minutes=5)
        self.account("990")
        monitor = AutonomousMonitor(self.experiments, self.accounts, self.candidates, now=lambda: self.current)
        result = monitor.performance(self.fingerprint)
        self.assertEqual(result["baseline"], "1000")
        self.assertEqual(Decimal(result["return_pct"]), Decimal(-1))
        self.assertEqual(Decimal(result["max_drawdown_pct"]), Decimal(-10))
        self.assertEqual(result["observations"], 3)
        self.assertEqual(monitor.performance("different-account")["observations"], 0)

    def test_failed_or_stale_account_never_becomes_zero_or_observation(self):
        self.account()
        self.current += timedelta(minutes=5)
        self.store.save_setting("account_retry_at", None)
        self.accounts.snapshot.return_value = {"status": "error", "error": "네트워크 연결 실패"}
        self.monitor._account()
        self.assertEqual(self.monitor.performance(self.fingerprint)["observations"], 1)
        self.assertEqual(self.monitor.snapshot()["status"], "degraded")
        self.current += timedelta(minutes=31)
        self.monitor._account()
        self.assertEqual(self.monitor.snapshot()["status"], "attention")
        self.account("1001")
        self.assertEqual(self.monitor.snapshot()["status"], "healthy")

    def test_changed_identity_cannot_mix_account_series(self):
        self.store.save_setting("policy", {"account_id": "paper", "fingerprint": "old"})
        with self.assertRaises(KisError):
            self.monitor._account()
        self.accounts.snapshot.assert_not_called()

    def test_account_timestamp_after_network_latency_is_valid(self):
        def response(_):
            self.current += timedelta(seconds=3)
            return {"status": "ok", "stale": False, "updated_at": self.current.isoformat(),
                    "summary": {"total_value": "1000", "cash": "900"}}
        self.accounts.snapshot.side_effect = response
        self.monitor._account()
        self.assertEqual(self.monitor.performance(self.fingerprint)["observations"], 1)

    def test_collection_errors_do_not_block_account_and_retry_bounded(self):
        self.candidates.snapshot.side_effect = OSError("private")
        self.accounts.snapshot.return_value = {"status": "ok", "stale": False,
            "updated_at": self.current.isoformat(), "summary": {"total_value": "1000", "cash": "900"}}
        self.monitor._work()
        self.assertEqual(self.monitor.performance(self.fingerprint)["observations"], 1)
        self.assertNotIn("private", str(self.monitor.issues()))
        self.candidates.snapshot.side_effect = None
        self.candidates.snapshot.return_value = {"status": "complete", "stale": True}
        self.candidates.start.return_value = {"status": "running"}
        self.monitor._collect()
        self.monitor._collect()
        self.candidates.start.assert_called_once()

    def test_daily_report_is_scoped_and_idempotent(self):
        self.current = self.current.replace(hour=18)
        self.account()
        self.account("1010")
        reports = self.monitor.snapshot()["daily_reports"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["date"], "2026-10-06")
        self.assertEqual(reports[0]["orders"], 0)
        self.assertEqual(reports[0]["filled_orders"], 0)

    def test_async_collection_failure_surfaces_before_next_retry(self):
        self.candidates.snapshot.return_value = {"status": "error", "error": "일봉 조회 실패"}
        self.store.save_setting("collection_retry_at", (self.current + timedelta(hours=1)).isoformat())
        self.monitor._collect()
        self.assertEqual(self.monitor.snapshot()["status"], "degraded")
        self.current += timedelta(minutes=31)
        self.monitor._collect()
        self.assertEqual(self.monitor.snapshot()["status"], "attention")
        self.candidates.start.assert_not_called()
        self.candidates.snapshot.return_value = {"status": "complete", "stale": False}
        self.monitor._collect()
        self.assertEqual(self.monitor.snapshot()["status"], "healthy")

    def test_worker_deadline_detects_blocked_task_despite_heartbeat(self):
        worker = Mock()
        worker.is_alive.return_value = True
        self.monitor.worker = worker
        self.monitor.worker_started_at = 0
        self.monitor.clock = lambda: 181
        self.monitor.tick()
        self.assertTrue(self.monitor.stalled())
        self.assertEqual(self.monitor.snapshot()["status"], "degraded")
        self.assertIsNotNone(self.monitor.snapshot()["last_heartbeat_at"])

    def test_frozen_candidate_progress_triggers_health_recovery(self):
        self.candidates.snapshot.return_value = {"status": "running", "progress": {"completed": 3, "total": 351}}
        self.monitor.clock = lambda: 0
        self.monitor._collect()
        self.monitor.clock = lambda: 601
        self.monitor._collect()
        self.assertTrue(self.monitor.stalled())
        self.candidates.snapshot.return_value = {"status": "running", "progress": {"completed": 4, "total": 351}}
        self.monitor._collect()
        self.assertFalse(self.monitor.stalled())

    def test_answer_never_overrides_unknown_order_gate(self):
        self.monitor._issue("unknown", "주문 확인 필요", question="확인했나요?", blocking=True)
        issue = self.monitor.issues()[0]
        self.experiments.command.side_effect = KisError("미확정 주문")
        with self.assertRaises(KisError):
            self.monitor.answer({"action": "answer", "id": issue["id"], "answer": "retry"})
        self.assertEqual(self.monitor.issues()[0]["state"], "open")
        def user_pause(command):
            self.assertEqual(command, {"action": "pause"})
            self.store.save_setting("enabled", False)
            self.store.save_setting("user_paused", True)
        self.experiments.command.side_effect = user_pause
        self.monitor.answer({"action": "answer", "id": issue["id"], "answer": "keep_paused"})
        self.experiments.command.assert_called_with({"action": "pause"})
        self.assertEqual(self.monitor.issues()[0]["state"], "resolved")
        with self.assertRaises(KisError):
            self.monitor.answer({"action": "answer", "id": issue["id"], "answer": "retry"})

    def test_resume_exposes_previously_acknowledged_unresolved_engine_and_monitor_issues(self):
        self.monitor._issue("monitor_problem", "계좌 자료 문제", question="확인해 주세요.", blocking=True)
        monitor_issue = self.monitor.issues()[0]
        engine_issue = {**monitor_issue, "id": "engine_problem", "code": "balance_mismatch", "message": "원장 불일치"}
        self.store.save_setting("issues", [engine_issue])
        def user_pause(command):
            self.assertEqual(command, {"action": "pause"})
            self.store.save_setting("enabled", False)
            self.store.save_setting("user_paused", True)
        self.experiments.command.side_effect = user_pause
        for issue in (monitor_issue, engine_issue):
            self.monitor.answer({"action": "answer", "id": issue["id"], "answer": "keep_paused"})
        paused = self.monitor.snapshot()
        self.assertEqual(paused["status"], "paused")
        self.assertTrue(all(item["state"] == "resolved" for item in paused["issues"]))
        self.assertEqual(self.store.setting("issues")[0]["state"], "open")
        self.assertEqual(self.store.setting("monitor_issues")[0]["state"], "open")
        # This is the same durable intent change performed by the engine's start command.
        self.store.save_setting("enabled", True)
        self.store.save_setting("user_paused", False)
        self.monitor._issue("monitor_problem", "계좌 자료 문제 지속", question="확인해 주세요.", blocking=True)
        resumed = self.monitor.snapshot()
        self.assertEqual(resumed["status"], "attention")
        self.assertIsNotNone(resumed["error"])
        self.assertTrue(all(item["state"] == "open" for item in resumed["issues"]))
        self.assertEqual(next(item for item in resumed["issues"] if item["id"] == monitor_issue["id"])["first_seen"],
                         monitor_issue["first_seen"])

    def test_pause_answer_does_not_hide_issues_when_enabled_or_user_pause_not_active(self):
        self.monitor._issue("problem", "문제 지속", question="확인해 주세요.", blocking=True)
        issue = self.monitor.issues()[0]
        self.store.save_setting("issue_answers", {issue["id"]: {"answer": "keep_paused", "first_seen": issue["first_seen"]}})
        for enabled, user_paused in ((True, True), (True, False), (False, False)):
            self.store.save_setting("enabled", enabled)
            self.store.save_setting("user_paused", user_paused)
            with self.subTest(enabled=enabled, user_paused=user_paused):
                self.assertEqual(self.monitor.issues()[0]["state"], "open")


if __name__ == "__main__":
    unittest.main()
