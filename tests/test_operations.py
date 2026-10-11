from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from backend.launcher import lock_busy, set_mirror_desired
from backend.operations import OperationsService
from backend.request_gate import file_lock
from backend.supervisor import _write


class OperationsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.local = self.root / ".local"
        self.service = OperationsService(self.root)
        self.seconds = 0
        self.now = datetime.now(timezone.utc)
        self.service._now = lambda: self.now + timedelta(seconds=self.seconds)
        self.service._monotonic = lambda: self.seconds
        self.service._launcher = Mock()
        self.service._launcher.ensure_mirror.return_value = {"action": "started", "status": "paused"}
        self.mirror_locked = False
        mocked_lock = patch("backend.operations.lock_busy", side_effect=lambda path:
            self.mirror_locked if path.name == "timefolio-mirror.lock" else lock_busy(path))
        mocked_lock.start()
        self.addCleanup(mocked_lock.stop)
        self.controls = {"enabled": False, "user_paused": True}
        mocked_controls = patch("backend.operations.order_controls", side_effect=lambda root: dict(self.controls))
        self.read_controls = mocked_controls.start()
        self.addCleanup(mocked_controls.stop)

    def stamp(self, offset=0):
        return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()

    def save(self, state):
        _write(self.local / "timefolio-mirror-state.json", state)

    def state(self, **changes):
        self.local.mkdir(exist_ok=True)
        (self.local / "timefolio-mirror.lock").write_bytes(b"0")
        self.mirror_locked = True
        value = {"instance": "a" * 32, "pid": 456, "started_at": self.stamp(-60),
                 "updated_at": self.stamp(-2), "status": "paused", "control_version": 1}
        value.update(changes)
        self.save(value)
        return value

    def verified(self, **changes):
        value = self.state(**changes)
        self.assertEqual(self.service.snapshot()["mirror"]["status"], "unverified")
        value["updated_at"] = self.stamp(-1)
        self.save(value)
        return value

    def finish(self):
        self.service._worker.join(2)
        self.assertFalse(self.service._worker.is_alive())
        return self.service.snapshot()

    def test_empty_snapshot_is_read_only_and_does_not_start_services(self):
        before = list(self.root.iterdir())
        value = self.service.snapshot()
        self.assertEqual(value, {"mirror": {"status": "stopped", "updated_at": None,
            "reason": None, "can_start": True, "can_stop": False}, "busy": False, "error": None})
        self.assertEqual(list(self.root.iterdir()), before)
        self.service._launcher.ensure_mirror.assert_not_called()
        self.read_controls.assert_not_called()

    def test_lock_and_fresh_file_need_an_observed_heartbeat_change(self):
        value = self.state()
        for _ in range(2):
            mirror = self.service.snapshot()["mirror"]
            self.assertEqual(mirror["status"], "unverified")
            self.assertFalse(mirror["can_start"] or mirror["can_stop"])
        value["updated_at"] = self.stamp(-1)
        self.save(value)
        self.assertTrue(self.service.snapshot()["mirror"]["can_stop"])
        self.mirror_locked = False
        self.assertEqual(self.service.snapshot()["mirror"]["status"], "stopped")
        self.mirror_locked = True  # A subsequent login may hold the same profile lock.
        self.assertEqual(self.service.snapshot()["mirror"]["status"], "unverified")

    def test_legacy_stale_or_changed_identity_cannot_be_stopped(self):
        for changes in ({"control_version": 0}, {"control_version": True}, {"pid": True},
                        {"instance": "legacy"}, {"updated_at": self.stamp(-1000)}):
            with self.subTest(changes=changes):
                self.service._observed = None
                value = self.state(**changes)
                self.service.snapshot()
                if "updated_at" not in changes:
                    value["updated_at"] = self.stamp(-1)
                    self.save(value)
                self.assertFalse(self.service.snapshot()["mirror"]["can_stop"])
                with self.assertRaises(ValueError):
                    self.service.command({"action": "mirror_stop"})
        value = self.verified()
        self.assertTrue(self.service.snapshot()["mirror"]["can_stop"])
        value["instance"] = "b" * 32
        self.save(value)
        self.assertFalse(self.service.snapshot()["mirror"]["can_stop"])
        self.assertFalse((self.local / "timefolio-mirror-stop.json").exists())

    def test_snapshot_bounds_fields_and_never_returns_raw_reason(self):
        for reason in ("password=private user@example.com", {"private": "secret"}):
            self.service._observed = None
            self.verified(status="blocked", reason=reason, password="secret", account_hash="secret")
            value = self.service.snapshot()
            self.assertEqual(value["mirror"]["reason"], "연동 상태를 확인하세요.")
            self.assertEqual(set(value["mirror"]), {"status", "updated_at", "reason", "can_start", "can_stop"})
            self.assertNotIn("secret", json.dumps(value))
            self.assertNotIn("private", json.dumps(value))
        self.state(status=[], reason=[], updated_at="x" * 1000)
        self.assertEqual(self.service.snapshot()["mirror"]["status"], "unverified")
        self.assertIsNone(self.service.snapshot()["mirror"]["updated_at"])

    def test_command_accepts_only_the_two_exact_actions(self):
        for body in (None, [], {}, {"action": []}, {"action": "run"},
                     {"action": "mirror_start", "pid": 123}, {"action": "mirror_stop", "path": "private"}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.service.command(body)
        self.assertIsNone(self.service._worker)

    def test_start_is_nonblocking_serialized_and_preserves_any_order_controls(self):
        entered, release = threading.Event(), threading.Event()
        def start():
            with self.assertRaises(BlockingIOError), file_lock(self.local / "launcher.lock", blocking=False):
                pass
            entered.set()
            self.assertTrue(release.wait(2))
            return {"action": "started", "status": "idle"}
        self.service._launcher.ensure_mirror.side_effect = start
        self.controls = {"enabled": True, "user_paused": False}
        try:
            self.assertTrue(self.service.command({"action": "mirror_start"})["busy"])
            self.assertTrue(entered.wait(1))
            with self.assertRaises(ValueError):
                self.service.command({"action": "mirror_start"})
        finally:
            release.set()
        value = self.finish()
        self.assertIsNone(value["error"])
        self.assertEqual(self.read_controls.call_count, 2)
        self.service._launcher.ensure_mirror.assert_called_once()
        self.service._launcher.ensure_dashboard.assert_not_called()
        self.assertEqual(self.controls, {"enabled": True, "user_paused": False})

    def test_global_launcher_lock_rejects_another_start(self):
        with file_lock(self.local / "launcher.lock", blocking=False):
            value = self.service.snapshot()
            self.assertTrue(value["busy"])
            self.assertFalse(value["mirror"]["can_start"])
            with self.assertRaises(ValueError):
                self.service.command({"action": "mirror_start"})
        self.service._launcher.ensure_mirror.assert_not_called()

    def test_start_detects_changed_controls_and_sanitizes_worker_failure(self):
        self.service._launcher.ensure_mirror.side_effect = lambda: self.controls.update(enabled=True)
        self.service.command({"action": "mirror_start"})
        self.assertIn("주문 설정이 변경", self.finish()["error"])
        self.service._launcher.ensure_mirror.side_effect = RuntimeError("password=private")
        self.service.command({"action": "mirror_start"})
        value = self.finish()
        self.assertFalse(value["busy"])
        self.assertNotIn("private", value["error"])
        self.assertFalse(lock_busy(self.local / "launcher.lock"))

    def test_stop_targets_only_verified_instance_and_waits_for_closed_state(self):
        value = self.verified()
        self.assertTrue(self.service.snapshot()["mirror"]["can_stop"])
        self.read_controls.side_effect = OSError("unreadable database")
        def finish_stop(seconds):
            request = json.loads((self.local / "timefolio-mirror-stop.json").read_text())
            self.assertEqual({key: request[key] for key in ("instance", "pid", "started_at")},
                             {key: value[key] for key in ("instance", "pid", "started_at")})
            mirror = self.service.snapshot()["mirror"]
            self.assertEqual(mirror["status"], "stopping")
            self.assertFalse(mirror["can_start"] or mirror["can_stop"])
            with self.assertRaises(ValueError):
                self.service.command({"action": "mirror_stop"})
            self.save({**value, "status": "stopped", "updated_at": self.stamp()})
            self.mirror_locked = False
        with patch("backend.operations.time.sleep", side_effect=finish_stop):
            self.service.command({"action": "mirror_stop"})
            result = self.finish()
        self.assertIsNone(result["error"])
        self.assertEqual(result["mirror"]["status"], "stopped")
        self.assertTrue(result["mirror"]["can_start"])
        self.read_controls.assert_not_called()
        self.service._launcher.ensure_mirror.assert_not_called()

    def test_stop_timeout_leaves_request_and_never_restarts_or_kills(self):
        self.verified()
        self.service._timeout = 0
        self.service.command({"action": "mirror_stop"})
        result = self.finish()
        self.assertIn("종료를 아직 확인", result["error"])
        self.assertEqual(result["mirror"]["status"], "stopping")
        self.assertFalse(result["mirror"]["can_start"] or result["mirror"]["can_stop"])
        self.assertTrue((self.local / "timefolio-mirror-stop.json").exists())
        self.assertTrue(self.mirror_locked)
        self.service._launcher.ensure_mirror.assert_not_called()

    def test_stop_rechecks_identity_before_writing_request(self):
        value = self.verified()
        self.service.snapshot()
        target = (value["instance"], value["pid"], value["started_at"])
        self.save({**value, "pid": value["pid"] + 1, "updated_at": self.stamp()})
        with self.assertRaises(ValueError):
            self.service._stop(target)
        self.assertFalse((self.local / "timefolio-mirror-stop.json").exists())

    def test_missing_preference_does_not_enable_recovery_or_create_files(self):
        self.service.tick()
        self.service._launcher.ensure_mirror.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_runner_is_recovered_without_changing_order_permissions(self):
        set_mirror_desired(self.root, True)
        self.service.tick()
        value = self.finish()
        self.assertIsNone(value["error"])
        self.service._launcher.ensure_mirror.assert_called_once()
        self.service._launcher.ensure_dashboard.assert_not_called()
        self.assertEqual(self.controls, {"enabled": False, "user_paused": True})
        control = self.service._control()
        self.assertTrue(control["desired_running"])
        self.assertEqual(control["failures"], 1)
        self.assertEqual(datetime.fromisoformat(control["retry_at"]), self.now + timedelta(seconds=15))

    def test_retry_backoff_survives_dashboard_recreation_and_is_capped(self):
        set_mirror_desired(self.root, True)
        self.service._launcher.ensure_mirror.side_effect = RuntimeError("private")
        for delay in (15, 30, 60, 120, 240, 300, 300):
            self.service.tick()
            self.finish()
            control = self.service._control()
            expected = self.now + timedelta(seconds=self.seconds + delay)
            self.assertEqual(datetime.fromisoformat(control["retry_at"]), expected)
            calls = self.service._launcher.ensure_mirror.call_count
            self.seconds += delay - 1
            self.service.tick()
            self.assertEqual(self.service._launcher.ensure_mirror.call_count, calls)
            # Recreating the service must retain its saved retry deadline.
            previous = self.service
            self.service = OperationsService(self.root, now=previous._now, monotonic=previous._monotonic)
            self.service._launcher = previous._launcher
            self.seconds += 1
        self.assertEqual(self.service._control()["failures"], 7)

    def test_held_lock_with_stale_heartbeat_never_starts_a_duplicate(self):
        set_mirror_desired(self.root, True)
        self.state(updated_at=self.stamp(-1000))
        for seconds in (0, 360, 3600):
            self.seconds = seconds
            self.service.tick()
        self.service._launcher.ensure_mirror.assert_not_called()
        self.assertTrue(self.mirror_locked)
        self.assertFalse((self.local / "timefolio-mirror-stop.json").exists())

    def test_authentication_and_ambiguous_submission_wait_for_explicit_start(self):
        set_mirror_desired(self.root, True)
        for status, reason in (("error", "timefolio_login_required"),
                               ("blocked", "unknown_destination_submission"),
                               ("blocked", "prepared_form_mismatch"),
                               ("error", "browser_close_failed")):
            with self.subTest(status=status, reason=reason):
                self.state(status=status, reason=reason)
                self.mirror_locked = False
                self.seconds += 300
                self.service.tick()
                self.service._launcher.ensure_mirror.assert_not_called()
        # Explicit web restart is allowed after the user resolves the issue.
        self.service.command({"action": "mirror_start"})
        self.assertIsNone(self.finish()["error"])
        self.service._launcher.ensure_mirror.assert_called_once()

    def test_browser_disconnect_error_is_recoverable(self):
        set_mirror_desired(self.root, True)
        self.state(status="error", reason="timefolio_browser_disconnected")
        self.mirror_locked = False
        self.service.tick()
        self.assertIsNone(self.finish()["error"])
        self.service._launcher.ensure_mirror.assert_called_once()

    def test_transient_block_before_process_exit_is_recoverable(self):
        for reason in ("source_dashboard_unavailable", "adapter_or_storage_error"):
            with self.subTest(reason=reason):
                set_mirror_desired(self.root, True)
                self.state(status="blocked", reason=reason)
                self.mirror_locked = False
                self.seconds += 300
                self.service.tick()
                self.assertIsNone(self.finish()["error"])
        self.assertEqual(self.service._launcher.ensure_mirror.call_count, 2)

    def test_stop_cancels_recovery_while_runner_is_absent_or_login_failed(self):
        for state in ({}, {"status": "error", "reason": "timefolio_login_required"}):
            with self.subTest(state=state):
                set_mirror_desired(self.root, True)
                self.service._retry(self.service._control(), advance=True)
                self.save(state)
                self.assertTrue(self.service.snapshot()["mirror"]["can_stop"])
                self.service.command({"action": "mirror_stop"})
                self.assertIsNone(self.finish()["error"])
                self.assertFalse(self.service._control()["desired_running"])
                self.assertFalse((self.local / "timefolio-mirror-stop.json").exists())
                self.seconds += 600
                self.service.tick()
        self.service._launcher.ensure_mirror.assert_not_called()

    def test_absent_runner_stop_does_not_target_a_new_unverified_process(self):
        set_mirror_desired(self.root, True)
        self.state()
        self.service._execute("mirror_stop", None)
        self.assertFalse(self.service._control()["desired_running"])
        self.assertTrue(self.mirror_locked)
        self.assertIsNotNone(self.service.snapshot()["error"])
        self.assertFalse((self.local / "timefolio-mirror-stop.json").exists())

    def test_manual_stop_disables_recovery_even_after_timeout_and_dashboard_restart(self):
        set_mirror_desired(self.root, True)
        value = self.verified()
        self.service._timeout = 0
        self.service.command({"action": "mirror_stop"})
        self.finish()
        self.assertFalse(self.service._control()["desired_running"])
        self.save({**value, "status": "stopped", "updated_at": self.stamp()})
        self.mirror_locked = False
        restarted = OperationsService(self.root)
        restarted._launcher = Mock()
        restarted.tick()
        restarted._launcher.ensure_mirror.assert_not_called()

    def test_stable_verified_runner_resets_failures_but_initial_heartbeat_does_not(self):
        set_mirror_desired(self.root, True)
        self.service._retry(self.service._control(), advance=True)
        value = self.verified()
        self.service.tick()
        self.assertEqual(self.service._control()["failures"], 1)
        self.seconds = 299
        self.service.tick()
        self.assertEqual(self.service._control()["failures"], 1)
        self.seconds = 304
        value["updated_at"] = self.stamp()
        self.save(value)
        self.service.tick()
        self.assertEqual(self.service._control()["failures"], 0)

    def test_global_launcher_lock_and_close_prevent_recovery(self):
        set_mirror_desired(self.root, True)
        with file_lock(self.local / "launcher.lock", blocking=False):
            self.service.tick()
        self.service._launcher.ensure_mirror.assert_not_called()
        self.assertTrue(self.service.close())
        self.seconds = 300
        self.service.tick()
        self.service._launcher.ensure_mirror.assert_not_called()
        self.assertTrue(self.service._control()["desired_running"])
        with self.assertRaises(ValueError):
            self.service.command({"action": "mirror_start"})

    def test_queue_rechecks_persisted_stop_before_recovery(self):
        set_mirror_desired(self.root, False)
        self.service._execute("mirror_start", None, automatic=True)
        self.service._launcher.ensure_mirror.assert_not_called()
        self.assertFalse(self.service._control()["desired_running"])


if __name__ == "__main__":
    unittest.main()
