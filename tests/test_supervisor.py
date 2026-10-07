from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.request_gate import file_lock
from backend.supervisor import (HEALTH_GRACE, MAX_BACKOFF, STARTUP_GRACE, STABLE_RESET,
    Supervisor, SupervisorError, _read, _write, health_probe, install_startup, main,
    request_stop, startup_file)


class FakeChild:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None
        self.terminate_count = self.kill_count = 0
        self.wait_timeouts = 0
        self.wait_calls = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.wait_timeouts:
            self.wait_timeouts -= 1
            raise subprocess.TimeoutExpired("owned-test-child", timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminate_count += 1

    def kill(self):
        self.kill_count += 1


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.seconds = 0
        self.busy = False
        self.children, self.calls = [], []
        self.base = datetime(2026, 10, 5, 10, tzinfo=timezone.utc)
        self.supervisor = Supervisor(self.root, now=lambda: self.base + timedelta(seconds=self.seconds),
            monotonic=lambda: self.seconds, popen=self.spawn, probe=self.health,
            port_busy=lambda: self.busy, interval=0.001)
        self.addCleanup(self.supervisor._close_logs)

    def spawn(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        child = FakeChild(12000 + len(self.children))
        self.children.append(child)
        return child

    def health(self):
        return {"status": "ok", "pid": self.supervisor.child.pid, "instance": self.supervisor.instance,
                "updated_at": (self.base + timedelta(seconds=self.seconds)).isoformat(), "autonomy": {}}

    def advance(self, seconds):
        self.seconds += seconds

    def state(self):
        return _read(self.supervisor.directory / "state.json")

    def test_launches_only_dashboard_hidden_with_runtime_python_and_owned_stop_path(self):
        self.assertTrue(self.supervisor.step())
        args, options = self.calls[0]
        self.assertEqual(args[0][-4:], ["-m", "backend.dashboard", "--port", "8765"])
        self.assertEqual(options["cwd"], self.root.resolve())
        self.assertEqual(options["env"]["KIS_SUPERVISOR_INSTANCE"], self.supervisor.instance)
        self.assertEqual(Path(options["env"]["KIS_SUPERVISOR_STOP_FILE"]), self.supervisor.directory / "child-stop.json")
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        if os.name == "nt":
            self.assertTrue(options["creationflags"] & subprocess.CREATE_NO_WINDOW)
        self.assertEqual(self.state()["child_pid"], self.children[0].pid)
        self.assertEqual(self.state()["status"], "starting")

    def test_venv_wrapper_is_bypassed_for_child_start_and_startup_script(self):
        base = self.root / "base-runtime" / "python.exe"
        base.parent.mkdir()
        base.write_bytes(b"base-interpreter-fixture")
        wrapper = self.root / ".venv" / "Scripts" / "python.exe"
        with (patch("backend.supervisor.sys.executable", str(wrapper)),
              patch("backend.supervisor.sys._base_executable", str(base))):
            self.supervisor.step()
            self.assertEqual(self.calls[0][0][0][0], str(base.resolve()))
            installed = install_startup(self.root, startup_directory=self.root / "Startup")
            content = Path(installed["path"]).read_bytes().decode("utf-16")
            self.assertIn(str(base.resolve()), content)
            self.assertNotIn(str(wrapper), content)
            process = Mock(pid=12345)
            with (patch("backend.supervisor.ROOT", self.root),
                  patch("backend.supervisor._port_busy", return_value=False),
                  patch("backend.supervisor.subprocess.Popen", return_value=process) as spawn,
                  patch("sys.stdout", new_callable=io.StringIO)):
                self.assertEqual(main(["start"]), 0)
                self.assertEqual(spawn.call_args.args[0][0], str(base.resolve()))

    def test_existing_server_conflict_never_spawns_or_stops_a_process(self):
        self.busy = True
        self.assertFalse(self.supervisor.step())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.state()["status"], "conflict")
        self.assertIsNone(self.state()["child_pid"])

    def test_crashes_restart_with_bounded_exponential_backoff(self):
        self.supervisor.step()
        for attempt in range(1, 10):
            child = self.supervisor.child
            child.returncode = 7
            self.supervisor.step()
            delay = self.supervisor.next_retry - self.seconds
            self.assertEqual(delay, min(MAX_BACKOFF, 2 ** (attempt - 1)))
            self.assertEqual(self.state()["attempt"], attempt)
            self.assertEqual(self.state()["last_exit_code"], 7)
            self.assertEqual(self.state()["status"], "backoff")
            old_count = len(self.children)
            self.supervisor.step()
            self.assertEqual(len(self.children), old_count)
            self.advance(delay)
            self.supervisor.step()
            self.assertEqual(len(self.children), old_count + 1)

    def test_startup_readiness_failure_restarts_only_after_grace(self):
        self.supervisor.probe = lambda: None
        self.supervisor.step()
        owned = self.supervisor.child
        self.advance(STARTUP_GRACE - 1)
        self.supervisor.step()
        self.assertIs(self.supervisor.child, owned)
        self.advance(1)
        self.supervisor.step()
        self.assertIsNone(self.supervisor.child)
        self.assertEqual(owned.wait_calls, [30])
        self.assertEqual(self.state()["status"], "backoff")

    def test_running_health_stall_has_separate_grace_and_reason(self):
        self.supervisor.step()
        self.supervisor.step()
        self.assertEqual(self.state()["status"], "running")
        self.supervisor.probe = lambda: None
        self.advance(HEALTH_GRACE - 1)
        self.supervisor.step()
        self.assertIsNotNone(self.supervisor.child)
        self.advance(1)
        self.supervisor.step()
        self.assertIsNone(self.supervisor.child)
        self.assertIn("상태 응답", self.state()["error"])

    def test_healthy_stable_period_resets_failure_counter(self):
        self.supervisor.step()
        self.supervisor.child.returncode = 1
        self.supervisor.step()
        self.advance(1)
        self.supervisor.step()
        self.supervisor.step()
        self.assertEqual(self.supervisor.attempt, 1)
        self.advance(STABLE_RESET)
        self.supervisor.step()
        self.assertEqual(self.state()["attempt"], 0)

    def test_stale_future_foreign_pid_or_instance_health_is_not_ready(self):
        self.supervisor.step()
        normal = self.health()
        variants = [{**normal, "updated_at": (self.base - timedelta(seconds=61)).isoformat()},
                    {**normal, "updated_at": (self.base + timedelta(seconds=6)).isoformat()},
                    {**normal, "updated_at": "2026-10-05T10:00:00"},
                    {**normal, "pid": 99999}, {**normal, "instance": "unrelated"},
                    {**normal, "status": "error"}, {}, None]
        for value in variants:
            with self.subTest(value=value):
                self.assertFalse(self.supervisor._healthy(value))
        self.assertTrue(self.supervisor._healthy(normal))

    def test_matching_stop_request_drains_owned_child_and_reports_stopped(self):
        self.supervisor.step()
        owned = self.supervisor.child
        request_stop(self.supervisor.directory)
        self.assertFalse(self.supervisor.step())
        self.assertEqual(owned.wait_calls, [30])
        self.assertEqual(owned.terminate_count, 0)
        self.assertEqual(_read(self.supervisor.directory / "child-stop.json"),
                         {"instance": self.supervisor.instance, "child_pid": owned.pid})
        self.assertEqual(self.state()["status"], "stopped")
        self.assertIsNone(self.state()["child_pid"])

    def test_force_stop_fallback_only_uses_retained_popen_handle(self):
        self.supervisor.step()
        owned = self.supervisor.child
        owned.wait_timeouts = 2
        self.supervisor.stop_event.set()
        self.assertFalse(self.supervisor.step())
        self.assertEqual(owned.wait_calls, [30, 5, 5])
        self.assertEqual((owned.terminate_count, owned.kill_count), (1, 1))

    def test_old_instance_stop_or_stale_saved_pid_is_never_used_to_stop_child(self):
        _write(self.supervisor.directory / "state.json", {"child_pid": 55555, "supervisor_pid": 44444,
                                                         "status": "running", "instance": "old"})
        _write(self.supervisor.directory / "stop.json", {"instance": "old", "child_pid": 55555})
        self.supervisor.step()
        owned = self.supervisor.child
        self.supervisor.step()
        self.assertEqual(self.state()["status"], "running")
        self.assertEqual((owned.terminate_count, owned.kill_count), (0, 0))

    def test_lifetime_lock_prevents_duplicate_without_overwriting_owner_state(self):
        original = {"status": "running", "instance": "a" * 32, "supervisor_pid": 42}
        _write(self.supervisor.directory / "state.json", original)
        with file_lock(self.supervisor.directory / "supervisor.lock"):
            with self.assertRaises(SupervisorError):
                self.supervisor.run()
        self.assertEqual(self.state(), original)
        self.assertEqual(self.calls, [])

    def test_run_stop_releases_lifetime_lock(self):
        self.supervisor.stop_event.set()
        self.assertEqual(self.supervisor.run(), 0)
        with file_lock(self.supervisor.directory / "supervisor.lock", blocking=False):
            self.assertEqual(self.state()["status"], "stopped")

    def test_spawn_error_is_sanitized_and_retried_without_secret_details(self):
        self.supervisor.popen = Mock(side_effect=OSError("private-secret-command"))
        self.supervisor.step()
        self.assertEqual(self.state()["status"], "backoff")
        self.assertNotIn("private", json.dumps(self.state()))
        self.assertEqual(self.supervisor.logs, [])

    def test_foreign_listener_after_owned_crash_halts_restarts_without_killing_it(self):
        self.supervisor.step()
        self.supervisor.child.returncode = 2
        self.supervisor.step()
        self.busy = True
        self.advance(1)
        self.assertFalse(self.supervisor.step())
        self.assertEqual(len(self.children), 1)
        self.assertEqual(self.state()["status"], "conflict")

    def test_state_updates_are_atomic_bounded_and_preserve_heartbeat(self):
        self.supervisor.step()
        self.supervisor.step()
        first = self.state()["last_heartbeat"]
        self.advance(5)
        self.supervisor.step()
        self.assertNotEqual(first, self.state()["last_heartbeat"])
        self.assertEqual({path.name for path in self.supervisor.directory.iterdir()},
                         {"state.json", "dashboard.stdout.log", "dashboard.stderr.log"})
        bad = self.supervisor.directory / "bad.json"
        bad.write_text("{}" * 40000)
        self.assertEqual(_read(bad), {})

    def test_health_probe_reads_only_local_health_with_required_header(self):
        response = Mock()
        response.status = 200
        response.read.return_value = b'{"status":"ok"}'
        connection = Mock()
        connection.getresponse.return_value = response
        with patch("backend.supervisor.HTTPConnection", return_value=connection) as connect:
            self.assertEqual(health_probe(), {"status": "ok"})
        connect.assert_called_once_with("127.0.0.1", 8765, timeout=3)
        connection.request.assert_called_once_with("GET", "/api/health", headers={"X-KIS-Dashboard": "1"})
        connection.close.assert_called_once()

    def test_health_redirect_is_not_followed(self):
        connection = Mock()
        connection.getresponse.return_value.status = 302
        with patch("backend.supervisor.HTTPConnection", return_value=connection):
            self.assertIsNone(health_probe())
        connection.request.assert_called_once()
        connection.close.assert_called_once()

    def test_startup_install_is_hidden_and_removes_only_exact_owned_file(self):
        directory = self.root / "Startup"
        unrelated = directory / "unrelated.vbs"
        directory.mkdir()
        unrelated.write_text("keep")
        first = install_startup(self.root, startup_directory=directory)
        second = install_startup(self.root, startup_directory=directory)
        self.assertEqual(first, second)
        path = Path(first["path"])
        content = path.read_bytes().decode("utf-16")
        self.assertIn("backend.supervisor", content)
        self.assertIn(", 0, False", content)
        self.assertIn(str(self.root), content)
        self.assertEqual(install_startup(self.root, startup_directory=directory, remove=True)["status"], "removed")
        self.assertFalse(path.exists())
        self.assertEqual(unrelated.read_text(), "keep")

    def test_startup_collision_modified_content_never_overwritten_or_deleted(self):
        directory = self.root / "Startup"
        directory.mkdir()
        path = startup_file(self.root, startup_directory=directory)
        path.write_text("unrelated", encoding="utf-16")
        for remove in (False, True):
            with self.subTest(remove=remove), self.assertRaises(SupervisorError):
                install_startup(self.root, startup_directory=directory, remove=remove)
            self.assertEqual(path.read_text(encoding="utf-16"), "unrelated")

    def test_stop_without_valid_active_record_does_not_create_stop_file(self):
        for value in ({}, {"instance": "a" * 32, "status": "stopped"}, {"instance": "short", "status": "running"}):
            _write(self.supervisor.directory / "state.json", value)
            with self.assertRaises(SupervisorError):
                request_stop(self.supervisor.directory)
        self.assertFalse((self.supervisor.directory / "stop.json").exists())

    def test_status_cli_does_not_spawn_process(self):
        with (patch("backend.supervisor.ROOT", self.root), patch("backend.supervisor.subprocess.Popen") as spawn,
              patch("sys.stdout", new_callable=io.StringIO) as output):
            self.assertEqual(main(["status"]), 0)
            self.assertEqual(json.loads(output.getvalue()), {"status": "not_started"})
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
