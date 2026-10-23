from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
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

    @contextmanager
    def failed_supervision(self, supervisor, child):
        waiting, release = threading.Event(), threading.Event()
        failures = []
        original_poll = child.poll
        def poll():
            if supervisor.state["status"] == "stop_failed" and child.returncode is None:
                waiting.set()
                release.wait(5)
            return original_poll()
        def run():
            try:
                supervisor.run()
            except BaseException as error:
                failures.append(error)
        worker = threading.Thread(target=run, daemon=True)
        with patch.object(child, "poll", side_effect=poll):
            worker.start()
            try:
                self.assertTrue(waiting.wait(3), "failed supervision was not reached")
                self.assertTrue(worker.is_alive())
                yield failures
            finally:
                child.returncode = 9
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())

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

    def test_persistent_state_and_stop_file_failures_cannot_skip_owned_child_cleanup(self):
        for fail_state, fail_stop in ((True, False), (False, True), (True, True)):
            with self.subTest(state=fail_state, stop=fail_stop):
                child = FakeChild(13000)
                child.wait_timeouts = 2
                supervisor = Supervisor(self.root / f"failure-{fail_state}-{fail_stop}",
                    port_busy=lambda: False, interval=0)
                self.addCleanup(supervisor._close_logs)
                def spawn(*args, **kwargs):
                    supervisor.stop_event.set()
                    return child
                supervisor.popen = Mock(side_effect=spawn)
                primary = OSError(errno.EACCES, "private-state-path")
                stop_error = OSError(errno.ENOSPC, "private-stop-path")
                state_failed = False
                def write(path, value):
                    nonlocal state_failed
                    if path.name == "state.json" and fail_state:
                        state_failed = state_failed or supervisor.child is not None
                        if state_failed:
                            raise primary
                    if path.name == "child-stop.json" and fail_stop:
                        raise stop_error
                    return _write(path, value)
                with patch("backend.supervisor._write", side_effect=write):
                    if fail_state:
                        with self.assertRaises(OSError) as raised:
                            supervisor.run()
                        self.assertIs(raised.exception, primary)
                        self.assertEqual(supervisor.state["failure"], {"type": "PermissionError", "errno": errno.EACCES})
                    else:
                        self.assertEqual(supervisor.run(), 0)
                self.assertIsNone(supervisor.child)
                self.assertEqual(child.wait_calls, [30, 5, 5])
                self.assertEqual((child.terminate_count, child.kill_count), (1, 1))
                self.assertEqual(supervisor.state["status"], "stopped")
                stages = [item["stage"] for item in supervisor.state.get("cleanup_errors", [])]
                if fail_state:
                    self.assertEqual(stages.count("state_publish"), 2)
                if fail_stop:
                    self.assertIn("child_stop_file", stages)
                self.assertNotIn("private", json.dumps(supervisor.state))
                with file_lock(supervisor.directory / "supervisor.lock", blocking=False):
                    pass

    def test_failed_stop_file_still_allows_graceful_exit_without_forced_termination(self):
        self.supervisor.step()
        child = self.supervisor.child
        with patch("backend.supervisor._write", side_effect=OSError(errno.EACCES, "private")):
            self.supervisor._stop_child()
        self.assertIsNone(self.supervisor.child)
        self.assertEqual(child.wait_calls, [30])
        self.assertEqual((child.terminate_count, child.kill_count), (0, 0))
        self.assertEqual(self.supervisor.state["cleanup_errors"], [
            {"stage": "child_stop_file", "type": "PermissionError", "errno": errno.EACCES}])

    def test_initial_state_publish_failure_still_cleans_up_an_already_owned_child(self):
        self.supervisor.step()
        child = self.supervisor.child
        failure = OSError(errno.ENOSPC, "private-state-path")
        def write(path, value):
            if path.name == "state.json":
                raise failure
            return _write(path, value)
        with patch("backend.supervisor._write", side_effect=write), self.assertRaises(OSError) as raised:
            self.supervisor.run()
        self.assertIs(raised.exception, failure)
        self.assertEqual(child.wait_calls, [30])
        self.assertIsNone(self.supervisor.child)
        self.assertEqual(len(self.calls), 1)

    def test_already_exited_child_needs_no_stop_file_or_signals(self):
        self.supervisor.step()
        child = self.supervisor.child
        child.returncode = 9
        with patch("backend.supervisor._write", side_effect=OSError("private")) as write:
            self.supervisor._stop_child()
        write.assert_not_called()
        self.assertIsNone(self.supervisor.child)
        self.assertEqual(child.wait_calls, [])
        self.assertEqual((child.terminate_count, child.kill_count), (0, 0))
        self.assertEqual(self.supervisor.state["last_exit_code"], 9)

    def test_unconfirmed_exit_retains_handle_and_never_reports_stopped_or_restarts(self):
        self.supervisor.step()
        child = self.supervisor.child
        child.wait_timeouts = 3
        logs = list(self.supervisor.logs)
        self.supervisor.stop_event.set()
        with self.failed_supervision(self.supervisor, child) as failures:
            self.assertIs(self.supervisor.child, child)
            self.assertTrue(all(not stream.closed for stream in logs))
            self.assertEqual(child.wait_calls, [30, 5, 5])
            self.assertEqual((child.terminate_count, child.kill_count), (1, 1))
            self.assertEqual(self.state()["status"], "stop_failed")
            self.assertEqual(self.state()["child_pid"], child.pid)
            self.assertIsNone(self.state()["next_retry"])
            self.supervisor.stop_event.clear()
            with self.assertRaises(SupervisorError):
                self.supervisor.step()
            with self.assertRaises(SupervisorError):
                self.supervisor._launch()
            second = Supervisor(self.root, popen=Mock(), port_busy=lambda: False)
            with self.assertRaisesRegex(SupervisorError, "이미 실행"):
                second.run()
            second.popen.assert_not_called()
            self.assertEqual(self.state()["status"], "stop_failed")
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(failures, [])
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], SupervisorError)
        self.assertIsNone(self.supervisor.child)
        self.assertTrue(all(stream.closed for stream in logs))
        self.assertEqual(self.state()["status"], "stopped")
        self.assertEqual(self.state()["last_exit_code"], 9)
        self.assertEqual(child.wait_calls, [30, 5, 5])
        with file_lock(self.supervisor.directory / "supervisor.lock", blocking=False):
            pass

    def test_original_failure_and_unconfirmed_cleanup_failure_remain_distinct(self):
        self.supervisor.step()
        child = self.supervisor.child
        child.wait_timeouts = 3
        primary = RuntimeError("private-original-failure")
        self.supervisor.probe = Mock(side_effect=primary)
        with self.failed_supervision(self.supervisor, child) as failures:
            self.assertEqual(self.state()["failure"], {"type": "RuntimeError"})
            self.assertEqual(self.state()["status"], "stop_failed")
            self.assertIn("child_cleanup", [item["stage"] for item in self.state()["cleanup_errors"]])
            self.assertIs(self.supervisor.child, child)
        self.assertEqual(failures, [primary])
        self.assertEqual(self.state()["status"], "stopped")
        self.assertNotIn("private", json.dumps(self.state()))

    def test_failed_supervision_keeps_owner_lock_despite_persistent_file_failures(self):
        for fail_state, fail_stop in ((True, False), (False, True), (True, True)):
            with self.subTest(state=fail_state, stop=fail_stop):
                child = FakeChild(13000)
                child.wait_timeouts = 3
                primary = OSError(errno.ENOSPC, "private-state") if fail_state else RuntimeError("private-original")
                supervisor = Supervisor(self.root / f"retained-{fail_state}-{fail_stop}",
                    probe=Mock(side_effect=primary), port_busy=lambda: False, interval=0)
                supervisor.child = child
                def write(path, value):
                    if path.name == "state.json" and fail_state:
                        raise primary
                    if path.name == "child-stop.json" and fail_stop:
                        raise OSError(errno.EACCES, "private-stop")
                    return _write(path, value)
                with patch("backend.supervisor._write", side_effect=write):
                    with self.failed_supervision(supervisor, child) as failures:
                        self.assertEqual(supervisor.state["status"], "stop_failed")
                        self.assertIs(supervisor.child, child)
                        second = Supervisor(supervisor.root, popen=Mock(), port_busy=lambda: False)
                        with self.assertRaisesRegex(SupervisorError, "이미 실행"):
                            second.run()
                        second.popen.assert_not_called()
                        self.assertEqual(failures, [])
                    self.assertEqual(failures, [primary])
                self.assertIsNone(supervisor.child)
                self.assertEqual(supervisor.state["status"], "stopped")
                self.assertEqual(supervisor.state["last_exit_code"], 9)
                self.assertEqual(child.wait_calls, [30, 5, 5])
                self.assertEqual((child.terminate_count, child.kill_count), (1, 1))
                self.assertNotIn("private", json.dumps(supervisor.state))
                with file_lock(supervisor.directory / "supervisor.lock", blocking=False):
                    pass

    def test_failed_supervision_polling_waits_despite_stop_event_and_write_errors(self):
        self.supervisor.step()
        child = self.supervisor.child
        self.supervisor.stop_event.set()
        delay = Mock()
        with (patch("backend.supervisor.threading.Event", return_value=delay),
              patch("backend.supervisor._write", side_effect=OSError(errno.ENOSPC, "private")),
              patch.object(child, "poll", side_effect=[OSError(errno.EIO, "private"), None, 9])):
            self.supervisor._wait_for_child_exit()
        self.assertEqual([call.args for call in delay.wait.call_args_list], [(1,), (1,)])
        self.assertIsNone(self.supervisor.child)
        self.assertEqual(self.supervisor.state["last_exit_code"], 9)
        self.assertEqual((child.terminate_count, child.kill_count), (0, 0))
        self.assertEqual([item["stage"] for item in self.supervisor.state["cleanup_errors"]],
                         ["child_poll", "state_publish", "state_publish"])

    def test_signal_failures_still_wait_and_retain_unconfirmed_child(self):
        self.supervisor.step()
        child = self.supervisor.child
        child.wait_timeouts = 3
        with (patch.object(child, "terminate", side_effect=OSError(errno.EACCES, "private")) as terminate,
              patch.object(child, "kill", side_effect=OSError(errno.EACCES, "private")) as kill,
              self.assertRaises(SupervisorError)):
            self.supervisor._stop_child()
        terminate.assert_called_once_with()
        kill.assert_called_once_with()
        self.assertEqual(child.wait_calls, [30, 5, 5])
        self.assertIs(self.supervisor.child, child)
        self.assertEqual(self.supervisor.state["status"], "stop_failed")
        self.assertEqual([item["stage"] for item in self.supervisor.state["cleanup_errors"]],
                         ["child_terminate", "child_kill"])

    def test_terminate_error_racing_with_exit_does_not_trigger_kill(self):
        self.supervisor.step()
        child = self.supervisor.child
        child.wait_timeouts = 1
        def terminate():
            child.returncode = 7
            raise OSError(errno.ESRCH, "private")
        with patch.object(child, "terminate", side_effect=terminate):
            self.supervisor._stop_child()
        self.assertIsNone(self.supervisor.child)
        self.assertEqual(child.wait_calls, [30, 5])
        self.assertEqual(child.kill_count, 0)
        self.assertEqual(self.supervisor.state["last_exit_code"], 7)

    def test_wait_error_does_not_skip_retained_handle_termination(self):
        self.supervisor.step()
        child = self.supervisor.child
        with patch.object(child, "wait", side_effect=[OSError(errno.EIO, "private"), 0]) as wait:
            self.supervisor._stop_child()
        self.assertIsNone(self.supervisor.child)
        self.assertEqual([call.kwargs["timeout"] for call in wait.call_args_list], [30, 5])
        self.assertEqual((child.terminate_count, child.kill_count), (1, 0))
        self.assertEqual(self.supervisor.state["cleanup_errors"], [
            {"stage": "child_wait", "type": "OSError", "errno": errno.EIO}])

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

    @unittest.skipUnless(os.name == 'nt', 'Windows replacement errors')
    def test_atomic_replace_retries_transient_windows_errors_with_bounded_waits(self):
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                target = self.root / 'retry-state.json'
                _write(target, {'value': 'old'})
                error = OSError(errno.EACCES, 'private-file-path')
                error.winerror = code
                original = Path.replace
                calls = []
                def replace(source, destination):
                    calls.append(source)
                    if len(calls) < 3:
                        raise error
                    return original(source, destination)
                with patch.object(Path, 'replace', replace), patch('backend.supervisor.time.sleep') as sleep:
                    _write(target, {'value': 'new'})
                self.assertEqual(_read(target), {'value': 'new'})
                self.assertEqual(len(calls), 3)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [.05, .1])

    @unittest.skipUnless(os.name == 'nt', 'Real Windows deny-delete handle')
    def test_real_windows_reader_denies_delete_then_release_allows_atomic_replace(self):
        target = self.root / 'held-state.json'
        _write(target, {'value': 'old'})
        original = Path.replace
        errors = []
        with target.open('rb') as held:
            def replace(source, destination):
                try:
                    return original(source, destination)
                except OSError as error:
                    errors.append(getattr(error, 'winerror', None))
                    self.assertEqual(_read(target), {'value': 'old'})
                    raise
            with patch.object(Path, 'replace', replace), \
                    patch('backend.supervisor.time.sleep', side_effect=lambda _: held.close()) as sleep:
                _write(target, {'value': 'new'})
        self.assertTrue(errors)
        self.assertTrue(set(errors) <= {5, 32, 33})
        sleep.assert_called_once_with(.05)
        self.assertEqual(_read(target), {'value': 'new'})
        self.assertEqual({path.name for path in self.root.iterdir()}, {'held-state.json'})

    @unittest.skipUnless(os.name == 'nt', 'Windows replacement errors')
    def test_persistent_windows_denial_exhausts_then_preserves_original(self):
        target = self.root / 'persistent-state.json'
        _write(target, {'value': 'old'})
        error = OSError(errno.EACCES, 'private-file-path')
        error.winerror = 5
        with patch.object(Path, 'replace', side_effect=error) as replace, \
                patch('backend.supervisor.time.sleep') as sleep:
            with self.assertRaises(OSError) as raised:
                _write(target, {'value': 'new'})
        self.assertIs(raised.exception, error)
        self.assertEqual(replace.call_count, 5)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [.05, .1, .2, .4])
        self.assertLess(sum(call.args[0] for call in sleep.call_args_list), 1)
        self.assertEqual(_read(target), {'value': 'old'})
        self.assertEqual({path.name for path in self.root.iterdir()}, {'persistent-state.json'})

    def test_nontransient_replace_and_other_write_stages_never_retry(self):
        target = self.root / 'failure-state.json'
        _write(target, {'value': 'old'})
        for winerror in (112, 1117, None):
            with self.subTest(winerror=winerror):
                error = OSError(errno.ENOSPC, 'private-file-path')
                if winerror is not None:
                    error.winerror = winerror
                with patch.object(Path, 'replace', side_effect=error) as replace, \
                        patch('backend.supervisor.time.sleep') as sleep:
                    with self.assertRaises(OSError):
                        _write(target, {'value': 'new'})
                self.assertEqual(replace.call_count, 1)
                sleep.assert_not_called()
        error = OSError(errno.EACCES, 'private-file-path')
        error.winerror = 5
        with patch('backend.supervisor.os.fsync', side_effect=error), \
                patch.object(Path, 'replace') as replace, patch('backend.supervisor.time.sleep') as sleep:
            with self.assertRaises(OSError):
                _write(target, {'value': 'new'})
        replace.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(_read(target), {'value': 'old'})

    def test_main_oserror_reports_only_numeric_codes(self):
        error = OSError(errno.EACCES, 'private-secret-command', 'private-filename')
        error.winerror = 5
        with patch('backend.supervisor.Supervisor.run', side_effect=error), \
                patch('backend.supervisor.signal.signal'), patch('sys.stderr', new_callable=io.StringIO) as output:
            self.assertEqual(main(['run']), 1)
        self.assertIn('errno=13', output.getvalue())
        self.assertIn('winerror=5', output.getvalue())
        self.assertNotIn('private', output.getvalue())

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
