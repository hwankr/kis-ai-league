from contextlib import redirect_stdout
from datetime import datetime
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.timefolio_mirror import _wait_for_stop, local_status, main, process_lock
from backend.timefolio_browser import match_receipt
from backend.mirror_runtime import MirrorBlocked, MirrorUnknown
from backend.supervisor import _write


class TimefolioCliTests(unittest.TestCase):
    def test_default_status_does_not_create_db_or_start_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.local.toml"
            with patch("backend.timefolio_browser.TimefolioBrowser", side_effect=AssertionError("browser constructed")):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--config", str(config)]), 0)
                    self.assertEqual(main(["--config", str(config), "--headless",
                                           "--browser-executable", str(Path(directory) / "missing.exe")]), 0)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_lock_rejects_second_process_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mirror.lock"
            with process_lock(path):
                with self.assertRaises(ValueError):
                    with process_lock(path):
                        pass
            with process_lock(path):
                pass

    def test_login_does_not_construct_source_or_runtime_and_closes_under_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.local.toml"
            lock = Path(directory) / ".local" / "timefolio-mirror.lock"
            browser = Mock()
            def close():
                with self.assertRaises(ValueError):
                    with process_lock(lock):
                        pass
            browser.close.side_effect = close
            with patch("backend.timefolio_browser.TimefolioBrowser", return_value=browser) as browser_class, \
                    patch("backend.mirror_source.TimefolioMirrorSource") as source, \
                    patch("backend.mirror_runtime.MirrorRuntime") as runtime, \
                    patch("builtins.input", return_value="") as login_input, redirect_stdout(io.StringIO()):
                self.assertEqual(main(["login", "--config", str(config)]), 0)
                browser_class.assert_called_once_with(config.resolve().parent / ".local" / "timefolio-browser",
                                                      executable_path=None, headless=False)
                login_input.assert_called_once()
                source.assert_not_called()
                runtime.assert_not_called()
            browser.open.assert_called_once()
            browser.identity.assert_called_once()
            browser.close.assert_called_once()
            with process_lock(lock):
                pass

    def test_headless_login_uses_saved_identity_without_input_or_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.local.toml"
            executable = Path(directory) / "browser" / "chrome.exe"
            browser = Mock()
            output = io.StringIO()
            with patch("backend.timefolio_browser.TimefolioBrowser", return_value=browser) as browser_class, \
                    patch("backend.mirror_source.TimefolioMirrorSource") as source, \
                    patch("backend.mirror_runtime.MirrorRuntime") as runtime, \
                    patch("backend.experiment_store.ExperimentStore") as store, \
                    patch("builtins.input", side_effect=AssertionError("headless input")), redirect_stdout(output):
                self.assertEqual(main(["login", "--config", str(config), "--headless",
                                       "--browser-executable", str(executable)]), 0)
                browser_class.assert_called_once_with(config.resolve().parent / ".local" / "timefolio-browser",
                                                      executable_path=executable, headless=True)
                source.assert_not_called()
                runtime.assert_not_called()
                store.assert_not_called()
            browser.open.assert_called_once()
            browser.identity.assert_called_once()
            browser.close.assert_called_once()
            self.assertNotIn("별도 Chrome", output.getvalue())

    def test_headless_login_failure_closes_without_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = Mock()
            browser.identity.side_effect = MirrorBlocked("timefolio_login_required")
            with patch("backend.timefolio_browser.TimefolioBrowser", return_value=browser), \
                    patch("backend.mirror_runtime.MirrorRuntime") as runtime, \
                    patch("backend.experiment_store.ExperimentStore") as store, \
                    patch("builtins.input", side_effect=AssertionError("headless input")), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["login", "--headless", "--config", str(Path(directory) / "config.toml")]), 1)
                runtime.assert_not_called()
                store.assert_not_called()
            browser.identity.assert_called_once()
            browser.close.assert_called_once()

    def test_run_without_existing_dashboard_db_does_not_open_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = Mock()
            with patch("backend.timefolio_browser.TimefolioBrowser", return_value=browser), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run", "--config", str(Path(directory) / "config.toml")]), 1)
            browser.open.assert_not_called()
            state = json.loads((Path(directory) / ".local" / "timefolio-mirror-state.json").read_text())
            self.assertEqual(state["status"], "error")

    def test_running_lock_prevents_second_browser_open(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / ".local" / "timefolio-mirror.lock"
            state = lock.parent / "timefolio-mirror-state.json"
            state.parent.mkdir()
            existing = b'{"instance":"existing","status":"paused"}'
            state.write_bytes(existing)
            browser = Mock()
            with process_lock(lock), patch("backend.timefolio_browser.TimefolioBrowser", return_value=browser), \
                    redirect_stdout(io.StringIO()):
                for command in ("login", "run"):
                    self.assertEqual(main([command, "--config", str(Path(directory) / "config.toml")]), 1)
            browser.open.assert_not_called()
            browser.close.assert_not_called()
            self.assertEqual(state.read_bytes(), existing)

    def test_status_and_login_preserve_existing_runtime_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / ".local" / "timefolio-mirror-state.json"
            state.parent.mkdir()
            existing = b'{"instance":"existing","status":"paused"}'
            state.write_bytes(existing)
            with patch("backend.timefolio_browser.TimefolioBrowser", return_value=Mock()), \
                    patch("builtins.input", return_value=""), redirect_stdout(io.StringIO()):
                for options in (["status"], ["login"], ["login", "--headless"]):
                    self.assertEqual(main([*options, "--config", str(Path(directory) / "config.toml")]), 0)
                    self.assertEqual(state.read_bytes(), existing)

    def test_run_publishes_each_tick_and_stops_only_after_browser_close(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            local = Path(directory) / ".local"
            db = local / "experiments" / "experiments.sqlite3"
            db.parent.mkdir(parents=True)
            db.touch()
            state_path = local / "timefolio-mirror-state.json"
            browser, runtime = Mock(), Mock()
            runtime.snapshot.return_value = {"jobs": []}
            runtime.tick.side_effect = [{"status": "initialized", "cursor": 0},
                {"status": "paused", "cursor": 0}, {"status": "paused", "cursor": 0}]
            browser.open.side_effect = lambda: self.assertEqual(json.loads(state_path.read_text())["status"], "starting")
            def close():
                self.assertEqual(json.loads(state_path.read_text())["status"], "paused")
                with self.assertRaises(ValueError), process_lock(local / "timefolio-mirror.lock"):
                    pass
            browser.close.side_effect = close
            published = []
            def write(path, value):
                published.append(dict(value))
                _write(path, value)
            with patch("backend.timefolio_browser.TimefolioBrowser", return_value=browser), \
                    patch("backend.mirror_source.TimefolioMirrorSource"), \
                    patch("backend.mirror_runtime.MirrorRuntime", return_value=runtime), \
                    patch("backend.experiment_store.ExperimentStore"), \
                    patch.dict(os.environ, {"KIS_MIRROR_INSTANCE": "a" * 32}), \
                    patch("backend.timefolio_mirror._write", side_effect=write), \
                    patch("backend.timefolio_mirror._wait_for_stop", side_effect=[False, False, KeyboardInterrupt]), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run", "--headless", "--config", str(config)]), 0)
            self.assertEqual([value["status"] for value in published],
                             ["starting", "initialized", "paused", "paused", "stopped"])
            for value in published:
                self.assertEqual(value["instance"], "a" * 32)
                self.assertEqual(value["pid"], os.getpid())
                self.assertEqual(value["control_version"], 1)
                self.assertEqual(value["started_at"], published[0]["started_at"])
                self.assertLessEqual(set(value), {"instance", "pid", "started_at", "updated_at", "status", "cursor", "reason", "control_version"})
                self.assertIsNotNone(datetime.fromisoformat(value["updated_at"]).utcoffset())
            self.assertEqual(json.loads(state_path.read_text())["status"], "stopped")
            self.assertEqual(json.loads(state_path.read_text())["cursor"], 0)

    def test_run_failure_closes_browser_and_publishes_sanitized_error(self):
        for failure_at in ("open", "tick", "close"):
            with self.subTest(failure_at=failure_at), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "config.toml"
                local = Path(directory) / ".local"
                db = local / "experiments" / "experiments.sqlite3"
                db.parent.mkdir(parents=True)
                db.touch()
                browser, runtime = Mock(), Mock()
                private_error = RuntimeError("password=private-value user@example.com")
                runtime.tick.side_effect = private_error if failure_at == "tick" else KeyboardInterrupt
                if failure_at != "tick":
                    getattr(browser, failure_at).side_effect = private_error
                with patch("backend.timefolio_browser.TimefolioBrowser", return_value=browser), \
                        patch("backend.mirror_source.TimefolioMirrorSource"), \
                        patch("backend.mirror_runtime.MirrorRuntime", return_value=runtime), \
                        patch("backend.experiment_store.ExperimentStore"), \
                        patch.dict(os.environ, {"KIS_MIRROR_INSTANCE": "invalid-private-instance"}), \
                        redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["run", "--headless", "--config", str(config)]), 1)
                saved = (local / "timefolio-mirror-state.json").read_text()
                state = json.loads(saved)
                self.assertEqual(state["status"], "error")
                self.assertEqual(state["reason"], "browser_close_failed" if failure_at == "close" else "mirror_run_failed")
                self.assertRegex(state["instance"], r"^[0-9a-f]{32}$")
                self.assertNotIn("private", saved)
                self.assertNotIn("example.com", saved)
                browser.close.assert_called_once()

    def test_run_login_failure_persists_reason_for_recovery_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            local = Path(directory) / ".local"
            db = local / "experiments" / "experiments.sqlite3"
            db.parent.mkdir(parents=True)
            db.touch()
            browser = Mock()
            browser.identity.side_effect = MirrorBlocked("timefolio_login_required")
            with patch("backend.timefolio_browser.TimefolioBrowser", return_value=browser), \
                    patch("backend.mirror_runtime.MirrorRuntime") as runtime, redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run", "--headless", "--config", str(config)]), 1)
            runtime.assert_not_called()
            browser.close.assert_called_once()
            self.assertEqual(json.loads((local / "timefolio-mirror-state.json").read_text())["reason"],
                             "timefolio_login_required")

    def test_disconnected_browser_exits_after_blocked_tick_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            local = Path(directory) / ".local"
            db = local / "experiments" / "experiments.sqlite3"
            db.parent.mkdir(parents=True)
            db.touch()
            browser, runtime = Mock(), Mock()
            browser.is_alive.return_value = False
            runtime.tick.return_value = {"status": "blocked", "reason": "adapter_or_storage_error"}
            with patch("backend.timefolio_browser.TimefolioBrowser", return_value=browser), \
                    patch("backend.mirror_source.TimefolioMirrorSource"), \
                    patch("backend.mirror_runtime.MirrorRuntime", return_value=runtime), \
                    patch("backend.experiment_store.ExperimentStore"), \
                    patch("backend.timefolio_mirror._wait_for_stop") as wait, redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run", "--headless", "--config", str(config)]), 1)
            wait.assert_not_called()
            runtime.tick.assert_called_once()
            browser.close.assert_called_once()
            with process_lock(local / "timefolio-mirror.lock"):
                self.assertEqual(json.loads((local / "timefolio-mirror-state.json").read_text())["status"], "error")

    def test_runtime_heartbeat_excludes_private_tick_fields_and_reason_text(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            db = Path(directory) / ".local" / "experiments" / "experiments.sqlite3"
            db.parent.mkdir(parents=True)
            db.touch()
            runtime = Mock()
            runtime.snapshot.return_value = {"jobs": []}
            runtime.tick.return_value = {"status": "blocked", "reason": "password=private-value",
                                         "account_key": "private-hash", "csv": "private-csv", "cursor": 3}
            with patch("backend.timefolio_browser.TimefolioBrowser", return_value=Mock()), \
                    patch("backend.mirror_source.TimefolioMirrorSource"), \
                    patch("backend.mirror_runtime.MirrorRuntime", return_value=runtime), \
                    patch("backend.experiment_store.ExperimentStore"), \
                    patch("backend.timefolio_mirror._write") as write, \
                    patch("backend.timefolio_mirror.time.sleep", side_effect=KeyboardInterrupt), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run", "--headless", "--config", str(config)]), 0)
            tick = write.call_args_list[1].args[1]
            self.assertEqual(tick["reason"], "mirror_error")
            self.assertEqual(tick["cursor"], 3)
            self.assertNotIn("account_key", tick)
            self.assertNotIn("csv", tick)

    def test_stop_request_is_checked_during_interval_without_another_tick(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            local = Path(directory) / ".local"
            db = local / "experiments" / "experiments.sqlite3"
            db.parent.mkdir(parents=True)
            db.touch()
            state_path = local / "timefolio-mirror-state.json"
            browser, runtime = Mock(), Mock()
            runtime.snapshot.return_value = {"jobs": []}
            runtime.tick.return_value = {"status": "paused", "cursor": 0}
            def request_stop(seconds):
                self.assertEqual(seconds, 1)
                state = json.loads(state_path.read_text())
                _write(local / "timefolio-mirror-stop.json", {"action": "stop",
                    **{key: state[key] for key in ("instance", "pid", "started_at")}})
            def close():
                self.assertEqual(json.loads(state_path.read_text())["status"], "stopping")
                with self.assertRaises(ValueError), process_lock(local / "timefolio-mirror.lock"):
                    pass
            browser.close.side_effect = close
            with patch("backend.timefolio_browser.TimefolioBrowser", return_value=browser), \
                    patch("backend.mirror_source.TimefolioMirrorSource"), \
                    patch("backend.mirror_runtime.MirrorRuntime", return_value=runtime), \
                    patch("backend.experiment_store.ExperimentStore"), \
                    patch("backend.timefolio_mirror.time.sleep", side_effect=request_stop) as sleep, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run", "--headless", "--config", str(config)]), 0)
            runtime.tick.assert_called_once()
            browser.close.assert_called_once()
            sleep.assert_called_once_with(1)
            self.assertEqual(json.loads(state_path.read_text())["status"], "stopped")

    def test_stop_request_requires_current_instance_pid_start_and_action(self):
        for change in ({"instance": "b" * 32}, {"pid": -1}, {"started_at": "old"}, {"action": "kill"}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "config.toml"
                local = Path(directory) / ".local"
                db = local / "experiments" / "experiments.sqlite3"
                db.parent.mkdir(parents=True)
                db.touch()
                browser, runtime = Mock(), Mock()
                runtime.snapshot.return_value = {"jobs": []}
                runtime.tick.return_value = {"status": "paused"}
                def identity():
                    state = json.loads((local / "timefolio-mirror-state.json").read_text())
                    request = {"action": "stop", **{key: state[key] for key in ("instance", "pid", "started_at")}}
                    _write(local / "timefolio-mirror-stop.json", {**request, **change})
                browser.identity.side_effect = identity
                with patch("backend.timefolio_browser.TimefolioBrowser", return_value=browser), \
                        patch("backend.mirror_source.TimefolioMirrorSource"), \
                        patch("backend.mirror_runtime.MirrorRuntime", return_value=runtime), \
                        patch("backend.experiment_store.ExperimentStore"), \
                        patch("backend.timefolio_mirror.time.sleep", side_effect=KeyboardInterrupt), \
                        redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["run", "--headless", "--config", str(config)]), 0)
                runtime.tick.assert_called_once()
                browser.close.assert_called_once()

    def test_stop_wait_checks_before_sleep_and_at_end(self):
        with patch("backend.timefolio_mirror.time.sleep") as sleep:
            self.assertTrue(_wait_for_stop(30, Mock(return_value=True)))
            sleep.assert_not_called()
            self.assertTrue(_wait_for_stop(1, Mock(side_effect=[False, True])))
            sleep.assert_called_once_with(1)

    def test_fake_run_reads_new_receipts_within_same_tick(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            db = Path(directory) / ".local" / "experiments" / "experiments.sqlite3"
            db.parent.mkdir(parents=True)
            db.touch()
            browser, runtime = Mock(), Mock()
            jobs = []
            runtime.snapshot.side_effect = lambda: {"jobs": list(jobs)}
            def tick():
                self.assertEqual(browser.jobs_reader(), [])
                jobs.append({"symbol": "005930", "status": "completed", "receipt": {"order_id": "new"}})
                self.assertEqual(browser.jobs_reader(), jobs)
                return {"status": "idle", "cursor": 1}
            runtime.tick.side_effect = tick
            with patch("backend.timefolio_browser.TimefolioBrowser", return_value=browser), \
                    patch("backend.mirror_source.TimefolioMirrorSource") as source, \
                    patch("backend.mirror_runtime.MirrorRuntime", return_value=runtime), \
                    patch("builtins.input", return_value=""), \
                    patch("backend.timefolio_mirror.time.sleep", side_effect=KeyboardInterrupt), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run", "--config", str(config)]), 0)
                source.return_value.read.assert_not_called()
            runtime.tick.assert_called_once()
            browser.close.assert_called_once()

    def test_corrupt_status_is_reported_without_browser_or_file_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            db = Path(directory) / ".local" / "experiments" / "experiments.sqlite3"
            db.parent.mkdir(parents=True)
            db.write_bytes(b"not a sqlite file")
            with patch("backend.timefolio_browser.TimefolioBrowser.open") as opened, redirect_stdout(io.StringIO()):
                self.assertEqual(main(["status", "--config", str(config)]), 1)
                opened.assert_not_called()
            self.assertEqual(db.read_bytes(), b"not a sqlite file")

    def test_only_exact_new_receipt_can_be_associated(self):
        proposal = {"symbol": "005930", "side": "buy", "weight": "1.25", "prior_order_ids": ["old"]}
        receipt = {"verified": True, "order_id": "new", "symbol": "005930", "side": "buy", "weight": 1.25}
        self.assertEqual(match_receipt(proposal, [{**receipt, "order_id": "old"}, receipt]), receipt)
        for orders in ([{**receipt, "order_id": "old"}], [receipt, {**receipt, "order_id": "other"}],
                       [{**receipt, "weight": 1.26}], [{**receipt, "verified": False}]):
            with self.assertRaises(MirrorUnknown):
                match_receipt(proposal, orders)

    def test_existing_receipt_lookup_requires_same_identity(self):
        proposal = {"symbol": "005930", "side": "buy", "weight": "1.25", "prior_order_ids": []}
        receipt = {"verified": True, "order_id": "known", "symbol": "005930", "side": "buy", "weight": "1.25"}
        self.assertEqual(match_receipt(proposal, [receipt], order_id="known"), receipt)
        with self.assertRaises(MirrorUnknown):
            match_receipt(proposal, [receipt], order_id="different")


if __name__ == "__main__":
    unittest.main()
