from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.timefolio_mirror import local_status, main, process_lock
from backend.timefolio_browser import match_receipt
from backend.mirror_runtime import MirrorBlocked, MirrorUnknown


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

    def test_running_lock_prevents_second_browser_open(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / ".local" / "timefolio-mirror.lock"
            browser = Mock()
            with process_lock(lock), patch("backend.timefolio_browser.TimefolioBrowser", return_value=browser), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(["login", "--config", str(Path(directory) / "config.toml")]), 1)
            browser.open.assert_not_called()
            browser.close.assert_not_called()

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
