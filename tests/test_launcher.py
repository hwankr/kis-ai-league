from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from backend.launcher import LaunchError, Launcher, main, order_controls
from backend.request_gate import file_lock


class FakeChild:
    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.local = self.root / ".local"
        self.database = self.local / "experiments" / "experiments.sqlite3"
        self.python = self.root / ".venv" / "Scripts" / "python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b"never-execute-this-fixture")
        self.seconds = 0
        self.locks = set()
        self.health = None
        self.children = []
        self.calls = []
        self.on_spawn = self.on_sleep = None
        self.launcher = Launcher(
            self.root, popen=self.spawn, probe=lambda: self.health,
            busy=lambda path: Path(path).name in self.locks,
            port_busy=lambda: False, monotonic=lambda: self.seconds,
            sleep=self.sleep, timeout=3)

    def spawn(self, command, **options):
        self.calls.append((command, options))
        child = FakeChild()
        self.children.append(child)
        if self.on_spawn:
            self.on_spawn(command, options, child)
        return child

    def sleep(self, seconds):
        self.seconds += seconds
        if self.on_sleep:
            self.on_sleep()

    def settings(self, values=None):
        if values is None:
            values = {"enabled": False, "user_paused": True, "autonomous_initialized": True}
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("DELETE FROM settings")
            db.executemany("INSERT INTO settings VALUES (?, ?)",
                           [(key, json.dumps(value)) for key, value in values.items()])

    def save(self, relative, value):
        path = self.local / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def timestamp(self, offset=0):
        return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()

    def healthy_dashboard(self):
        stamp = self.timestamp()
        state = {"instance": "owned-supervisor", "child_pid": 12345, "last_heartbeat": stamp}
        self.save("supervisor/state.json", state)
        self.health = {"instance": state["instance"], "pid": state["child_pid"],
                       "status": "ok", "updated_at": stamp}
        self.locks.add("supervisor.lock")
        return state

    def mirror_state(self, *, instance="owned-mirror", status="paused", offset=0, pid=23456):
        state = {"instance": instance, "pid": pid, "status": status,
                 "updated_at": self.timestamp(offset)}
        self.save("timefolio-mirror-state.json", state)
        return state

    def test_controls_are_read_only_and_preserve_both_boolean_values(self):
        real_connect = sqlite3.connect
        for enabled in (False, True):
            for paused in (False, True):
                with self.subTest(enabled=enabled, paused=paused):
                    self.settings({"enabled": enabled, "user_paused": paused,
                                   "autonomous_initialized": True})
                    before = self.database.read_bytes()
                    with patch("backend.launcher.sqlite3.connect", wraps=real_connect) as connect:
                        result = order_controls(self.root)
                    self.assertEqual(result, {"enabled": enabled, "user_paused": paused})
                    self.assertEqual(self.database.read_bytes(), before)
                    self.assertTrue(connect.call_args.args[0].endswith("?mode=ro"))
                    self.assertIs(connect.call_args.kwargs["uri"], True)

    def test_missing_database_is_not_created_and_nothing_starts(self):
        with self.assertRaises(LaunchError):
            self.launcher.run()
        self.assertFalse(self.database.exists())
        self.assertEqual(self.calls, [])

    def test_missing_malformed_or_auto_initializable_controls_block_before_spawn(self):
        invalid = [{}, {"enabled": False}, {"user_paused": True},
                   {"enabled": 0, "user_paused": True},
                   {"enabled": False, "user_paused": "true"},
                   {"enabled": False, "user_paused": None},
                   {"enabled": False, "user_paused": False},
                   {"enabled": False, "user_paused": False, "autonomous_initialized": False},
                   {"enabled": True, "user_paused": False, "autonomous_initialized": 1}]
        for values in invalid:
            with self.subTest(values=values):
                self.settings(values)
                before = self.database.read_bytes()
                with self.assertRaises(LaunchError):
                    self.launcher.run()
                self.assertEqual(self.calls, [])
                self.assertEqual(self.database.read_bytes(), before)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE settings SET value = 'not-json' WHERE key = 'enabled'")
        with self.assertRaises(LaunchError):
            self.launcher.run()
        self.assertEqual(self.calls, [])

    def test_persistent_user_pause_is_safe_without_first_run_marker(self):
        self.settings({"enabled": False, "user_paused": True})
        self.assertEqual(order_controls(self.root), {"enabled": False, "user_paused": True})

    def test_success_preserves_controls_and_records_only_after_both_services_ready(self):
        self.settings()
        before = self.database.read_bytes()
        with (patch.object(self.launcher, "ensure_dashboard", return_value="reused"),
              patch.object(self.launcher, "ensure_mirror", return_value={"action": "started", "status": "paused"})):
            result = self.launcher.run()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["orders"], {"enabled": False, "user_paused": True})
        self.assertEqual(self.database.read_bytes(), before)
        saved = json.loads((self.local / "launcher-status.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["orders"], result["orders"])
        self.assertEqual(saved["status"], "ready")
        control = json.loads((self.local / "timefolio-mirror-control.json").read_text(encoding="utf-8"))
        self.assertTrue(control["desired_running"])
        self.assertEqual(control["failures"], 0)

    def test_controls_changed_during_dashboard_start_prevent_mirror_start(self):
        self.settings()
        def changed():
            self.settings({"enabled": True, "user_paused": False, "autonomous_initialized": True})
            return "started"
        with (patch.object(self.launcher, "ensure_dashboard", side_effect=changed),
              patch.object(self.launcher, "ensure_mirror") as mirror):
            with self.assertRaises(LaunchError):
                self.launcher.run()
        mirror.assert_not_called()
        self.assertFalse((self.local / "launcher-status.json").exists())

    def test_controls_changed_during_mirror_start_are_never_reported_ready(self):
        self.settings()
        def changed():
            self.settings({"enabled": True, "user_paused": False, "autonomous_initialized": True})
            return {"action": "started", "status": "idle"}
        with (patch.object(self.launcher, "ensure_dashboard", return_value="reused"),
              patch.object(self.launcher, "ensure_mirror", side_effect=changed)):
            with self.assertRaises(LaunchError):
                self.launcher.run()
        self.assertFalse((self.local / "launcher-status.json").exists())

    def test_second_launcher_cannot_spawn_or_read_controls_during_first_launch(self):
        with (file_lock(self.local / "launcher.lock", blocking=False),
              patch("backend.launcher.order_controls") as controls):
            self.assertEqual(self.launcher.run(), {"status": "already_starting"})
        controls.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_matching_live_dashboard_is_reused_without_spawn(self):
        self.healthy_dashboard()
        self.assertEqual(self.launcher.ensure_dashboard(), "reused")
        self.assertEqual(self.calls, [])

    def test_dashboard_requires_matching_fresh_state_health_and_held_lock(self):
        cases = ["missing_instance", "wrong_instance", "wrong_pid", "stale_state", "stale_health",
                 "future_health", "naive_health", "unhealthy", "no_lock"]
        for case in cases:
            with self.subTest(case=case):
                self.seconds = 0
                state = self.healthy_dashboard()
                if case == "missing_instance":
                    state.pop("instance")
                elif case == "wrong_instance":
                    self.health["instance"] = "foreign"
                elif case == "wrong_pid":
                    self.health["pid"] += 1
                elif case == "stale_state":
                    state["last_heartbeat"] = self.timestamp(-61)
                elif case == "stale_health":
                    self.health["updated_at"] = self.timestamp(-61)
                elif case == "future_health":
                    self.health["updated_at"] = self.timestamp(10)
                elif case == "naive_health":
                    self.health["updated_at"] = "2026-10-10T10:00:00"
                elif case == "unhealthy":
                    self.health["status"] = "error"
                else:
                    self.locks.clear()
                    self.launcher.port_busy = lambda: True
                self.save("supervisor/state.json", state)
                with self.assertRaises(LaunchError):
                    self.launcher.ensure_dashboard()
                self.assertEqual(self.calls, [])

    def test_port_conflict_without_supervisor_lock_never_spawns(self):
        self.launcher.port_busy = lambda: True
        with self.assertRaisesRegex(LaunchError, "8765"):
            self.launcher.ensure_dashboard()
        self.assertEqual(self.calls, [])

    def test_missing_dashboard_starts_hidden_and_waits_for_owned_health(self):
        self.on_sleep = self.healthy_dashboard
        self.assertEqual(self.launcher.ensure_dashboard(), "started")
        self.assertGreater(self.seconds, 0)
        self.assertEqual(len(self.calls), 1)
        command, options = self.calls[0]
        self.assertEqual(command[0], str(self.python))
        self.assertEqual(command[-2:], ["backend.supervisor", "run"])
        self.assert_hidden_child(options)

    def assert_hidden_child(self, options):
        self.assertEqual(options["cwd"], self.root.resolve())
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertTrue(options["close_fds"])
        if os.name == "nt":
            self.assertTrue(options["creationflags"] & subprocess.CREATE_NO_WINDOW)
        else:
            self.assertTrue(options["start_new_session"])

    def test_dashboard_exit_and_readiness_timeout_are_failures_without_respawn(self):
        for exit_code in (7, None):
            with self.subTest(exit_code=exit_code):
                self.seconds = 0
                self.calls.clear()
                self.on_spawn = lambda command, options, child: setattr(child, "returncode", exit_code)
                with self.assertRaises(LaunchError):
                    self.launcher.ensure_dashboard()
                self.assertEqual(len(self.calls), 1)

    def test_legacy_mirror_lock_never_spawns_and_requires_user_check(self):
        self.locks.add("timefolio-mirror.lock")
        self.assertEqual(self.launcher.ensure_mirror(), {"action": "reused", "status": "existing_unverified"})
        self.assertEqual(self.calls, [])
        self.settings()
        with patch.object(self.launcher, "ensure_dashboard", return_value="reused"):
            self.assertEqual(self.launcher.run()["status"], "needs_check")
        self.assertEqual(self.calls, [])

    def test_locked_mirror_without_new_heartbeat_is_unverified(self):
        self.locks.add("timefolio-mirror.lock")
        self.mirror_state()
        self.assertEqual(self.launcher.ensure_mirror(), {"action": "reused", "status": "existing_unverified"})
        self.assertEqual(self.seconds, self.launcher.timeout)
        self.assertEqual(self.calls, [])

    def test_locked_mirror_is_reused_only_after_a_new_matching_tick(self):
        self.locks.add("timefolio-mirror.lock")
        previous = self.mirror_state()
        def progressed():
            self.save("timefolio-mirror-state.json", {
                **previous, "updated_at": (datetime.fromisoformat(previous["updated_at"])
                                            + timedelta(seconds=1)).isoformat()})
        self.on_sleep = progressed
        self.assertEqual(self.launcher.ensure_mirror(), {"action": "reused", "status": "paused"})
        self.assertEqual(self.seconds, 1)
        self.assertEqual(self.calls, [])

    def test_locked_mirror_does_not_accept_older_foreign_or_unlocked_tick(self):
        cases = ("older", "different_instance", "different_pid", "lock_released", "stale", "initialized")
        for case in cases:
            with self.subTest(case=case):
                self.seconds = 0
                self.locks.add("timefolio-mirror.lock")
                previous = self.mirror_state()
                def progressed():
                    current = {**previous, "updated_at": (datetime.fromisoformat(previous["updated_at"])
                                                          + timedelta(seconds=1)).isoformat()}
                    if case == "older":
                        current["updated_at"] = (datetime.fromisoformat(previous["updated_at"])
                                                 - timedelta(seconds=1)).isoformat()
                    elif case == "different_instance":
                        current["instance"] = "another-runner"
                    elif case == "different_pid":
                        current["pid"] += 1
                    elif case == "lock_released":
                        self.locks.clear()
                    elif case == "stale":
                        current["updated_at"] = self.timestamp(-61)
                    else:
                        current["status"] = "initialized"
                    self.save("timefolio-mirror-state.json", current)
                self.on_sleep = progressed
                self.assertEqual(self.launcher.ensure_mirror(),
                                 {"action": "reused", "status": "existing_unverified"})
                self.assertEqual(self.calls, [])

    def test_mirror_waits_for_current_instance_tick_and_lock_before_ready(self):
        self.mirror_state(instance="previous-process", status="paused")
        instance = {}
        def spawned(command, options, child):
            instance["value"] = options["env"]["KIS_MIRROR_INSTANCE"]
        def progressed():
            if self.seconds == 1:
                self.mirror_state(instance=instance["value"], status="initialized")
            else:
                self.mirror_state(instance=instance["value"], status="paused")
            if self.seconds == 3:
                self.locks.add("timefolio-mirror.lock")
        self.on_spawn, self.on_sleep = spawned, progressed
        result = self.launcher.ensure_mirror()
        self.assertEqual(result, {"action": "started", "status": "paused"})
        self.assertEqual(self.seconds, 3)
        self.assertEqual(len(self.calls), 1)
        command, options = self.calls[0]
        self.assertEqual(command[0], str(self.python))
        self.assertIn("--headless", command)
        self.assertIn("backend.timefolio_mirror", command)
        self.assert_hidden_child(options)

    def test_configured_dedicated_chrome_is_passed_to_headless_mirror(self):
        chrome = self.local / "timefolio-chrome" / "chrome-win64" / "chrome.exe"
        chrome.parent.mkdir(parents=True)
        chrome.write_bytes(b"never-execute-browser-fixture")
        def spawned(command, options, child):
            self.mirror_state(instance=options["env"]["KIS_MIRROR_INSTANCE"])
            self.locks.add("timefolio-mirror.lock")
        self.on_spawn = spawned
        self.assertEqual(self.launcher.ensure_mirror()["status"], "paused")
        command, _ = self.calls[0]
        self.assertEqual(command[command.index("--browser-executable") + 1], str(chrome))

    def test_mirror_terminal_tick_blocks_success_and_never_respawns(self):
        for status in ("error", "stopped", "blocked"):
            with self.subTest(status=status):
                self.calls.clear()
                self.locks.clear()
                def spawned(command, options, child):
                    self.mirror_state(instance=options["env"]["KIS_MIRROR_INSTANCE"], status=status)
                    self.locks.add("timefolio-mirror.lock")
                self.on_spawn = spawned
                with self.assertRaises(LaunchError):
                    self.launcher.ensure_mirror()
                self.assertEqual(len(self.calls), 1)

    def test_mirror_exit_or_initialized_timeout_never_counts_as_ready(self):
        for exit_code in (7, None):
            with self.subTest(exit_code=exit_code):
                self.seconds = 0
                self.calls.clear()
                self.locks.clear()
                def spawned(command, options, child):
                    child.returncode = exit_code
                    self.mirror_state(instance=options["env"]["KIS_MIRROR_INSTANCE"], status="initialized")
                    self.locks.add("timefolio-mirror.lock")
                self.on_spawn = spawned
                with self.assertRaises(LaunchError):
                    self.launcher.ensure_mirror()
                self.assertEqual(len(self.calls), 1)

    def test_missing_venv_never_falls_back_to_another_python(self):
        self.python.unlink()
        with self.assertRaises(LaunchError):
            self.launcher.ensure_dashboard()
        self.assertEqual(self.calls, [])


class LauncherDesktopTests(unittest.TestCase):
    def test_desktop_entry_opens_ready_web_instead_of_duplicate_warning_dialog(self):
        with (patch("backend.launcher.Launcher") as factory,
              patch("backend.launcher.webbrowser.open", return_value=True) as browser,
              patch("backend.launcher.notify_error") as notify,
              patch("backend.launcher.sys.stdout", None)):
            factory.return_value.run.return_value = {"status": "needs_check", "message": "연동 확인 필요"}
            factory.return_value._dashboard_ready.return_value = True
            self.assertEqual(main(["--open-dashboard"]), 1)
            browser.assert_called_once_with("http://127.0.0.1:8765/#/experiments", new=0)
            notify.assert_not_called()

    def test_no_ui_overrides_web_open_and_error_dialogs(self):
        with (patch("backend.launcher.Launcher") as factory,
              patch("backend.launcher.webbrowser.open") as browser,
              patch("backend.launcher.notify_error") as notify,
              patch("backend.launcher.sys.stdout", None)):
            factory.return_value.run.side_effect = LaunchError("시작 확인 필요")
            self.assertEqual(main(["--no-ui", "--open-dashboard"]), 1)
            browser.assert_not_called()
            notify.assert_not_called()

    def test_start_failure_is_not_hidden_when_existing_web_opens(self):
        with (patch("backend.launcher.Launcher") as factory,
              patch("backend.launcher.webbrowser.open", return_value=True) as browser,
              patch("backend.launcher.notify_error") as notify,
              patch("backend.launcher.sys.stdout", None)):
            factory.return_value.run.side_effect = LaunchError("연동 실행 실패")
            factory.return_value._dashboard_ready.return_value = True
            self.assertEqual(main(["--open-dashboard"]), 1)
            browser.assert_called_once()
            notify.assert_called_once_with("연동 실행 실패")

    def test_unavailable_dashboard_does_not_open_a_broken_web_page(self):
        with (patch("backend.launcher.Launcher") as factory,
              patch("backend.launcher.webbrowser.open") as browser,
              patch("backend.launcher.notify_error") as notify,
              patch("backend.launcher.sys.stdout", None)):
            factory.return_value.run.side_effect = LaunchError("서버 시작 실패")
            factory.return_value._dashboard_ready.return_value = False
            self.assertEqual(main(["--open-dashboard"]), 1)
            browser.assert_not_called()
            notify.assert_called_once_with("서버 시작 실패")


if __name__ == "__main__":
    unittest.main()
