"""HTTP 서버·브라우저·실제 KIS 요청 없이 별도 수집 프로세스의 수명주기를 검증한다."""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from backend.collector import request_stop
from backend.market_history import MarketHistory


ROOT = Path(__file__).resolve().parents[1]
CHILD = r"""
from pathlib import Path
import sys
import time
from backend.collector import Collector

config, database, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
class FakeClient:
    def quote(self, symbol):
        if mode == 'blocked':
            database.with_suffix('.request-started').touch()
            deadline = time.monotonic() + 20
            while not database.with_suffix('.release-request').exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError('Fake request was not released')
                time.sleep(0.05)
        return {'environment': 'paper', 'market': 'KRX', 'symbol': symbol,
                'price': '60100', 'change_percent': '-0.25', 'volume': '100',
                'cumulative_turnover': '6010000'}

class InitializingCollector(Collector):
    def collect_cycle(self):
        database.with_suffix('.initializing').touch()
        deadline = time.monotonic() + 20
        while not database.with_suffix('.release-initialization').exists():
            if time.monotonic() >= deadline:
                raise RuntimeError('Fake initialization was not released')
            time.sleep(0.05)
        return super().collect_cycle()

collector_class = InitializingCollector if mode == 'initializing' else Collector
collector = collector_class(config, database, client_factory=lambda profile: FakeClient())
try:
    raise SystemExit(collector.run(once=mode == 'once'))
except BlockingIOError:
    raise SystemExit(17)
"""


class CollectorProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / "market-history.sqlite3"
        self.config = self.root / "config.toml"
        self.config.write_text("app_key = 'offline-test-key'\napp_secret = 'offline-test-secret'\n"
                               "[market_data]\nsymbols = ['005930']\ninterval_seconds = 10\n",
                               encoding="utf-8")
        self.store = MarketHistory(self.database)
        self.processes = []
        self.addCleanup(self.close_processes)

    def close_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def start(self, mode="loop"):
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", CHILD,
                                    str(self.config), str(self.database), mode], cwd=ROOT,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.processes.append(process)
        return process

    def until(self, predicate, *, timeout=5, process=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if process is not None and process.poll() is not None:
                output, error = process.communicate()
                self.fail(f"Collector ended prematurely: {process.returncode}; {output}; {error}")
            time.sleep(0.05)
        self.fail("Timed out waiting for collector state")

    def count(self):
        return self.store.read(["005930"])["total_count"]

    def stop_cli(self):
        result = subprocess.run([sys.executable, "-X", "utf8", "-m", "backend.collector",
                                 "--database", str(self.database), "--stop"], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", timeout=5,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("종료를 요청했습니다", result.stdout)

    def test_without_http_samples_advance_singleton_stop_and_restart(self):
        first = self.start()
        self.until(lambda: self.count() >= 1, process=first)
        initial = self.store.get_collector()
        duplicate = self.start("once")
        output, error = duplicate.communicate(timeout=5)
        self.assertEqual(duplicate.returncode, 17, output + error)
        after_duplicate = self.store.get_collector()
        self.assertEqual(after_duplicate["collector_id"], initial["collector_id"])
        self.assertEqual(after_duplicate["state"], "running")
        self.until(lambda: self.count() >= 2, timeout=13, process=first)
        self.stop_cli()
        first.communicate(timeout=5)
        self.assertEqual(first.returncode, 0)
        stopped_count = self.count()
        self.assertEqual(self.store.get_collector()["state"], "stopped")
        time.sleep(0.2)
        self.assertEqual(self.count(), stopped_count)

        restarted = self.start("once")
        output, error = restarted.communicate(timeout=5)
        self.assertEqual(restarted.returncode, 0, output + error)
        self.assertEqual(self.count(), stopped_count + 1)
        self.assertNotEqual(self.store.get_collector()["collector_id"], initial["collector_id"])
        self.assertEqual(self.store.get_collector()["state"], "stopped")

    def test_killed_process_becomes_stale_and_new_process_can_recover(self):
        first = self.start()
        self.until(lambda: self.count() == 1, process=first)
        last = self.store.get_collector()
        first.kill()
        first.communicate(timeout=5)
        after_timeout = (datetime.fromisoformat(last["heartbeat_at"]) + timedelta(seconds=21)).isoformat()
        self.assertEqual(self.store.get_collector(after_timeout)["state"], "stale")
        self.assertEqual(self.count(), 1)
        recovered = self.start("once")
        output, error = recovered.communicate(timeout=5)
        self.assertEqual(recovered.returncode, 0, output + error)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.store.get_collector()["state"], "stopped")

    def test_heartbeat_remains_fresh_during_blocked_request_and_stop_discards_response(self):
        process = self.start("blocked")
        self.until(lambda: self.database.with_suffix(".request-started").exists(), process=process)
        initial = self.store.get_collector()
        self.assertEqual(initial["state"], "running")
        self.until(lambda: self.store.get_collector()["heartbeat_at"] > initial["heartbeat_at"],
                   timeout=8, process=process)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.store.get_collector()["state"], "running")
        self.stop_cli()
        # monitor가 종료 요청을 읽을 때까지 기다린 뒤 대기 중인 가짜 요청을 완료한다.
        time.sleep(1.1)
        self.database.with_suffix(".release-request").touch()
        output, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, output + error)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.store.get_collector()["state"], "stopped")

    def test_stop_during_initialization_ignores_previous_run_identity(self):
        previous = self.start("once")
        output, error = previous.communicate(timeout=5)
        self.assertEqual(previous.returncode, 0, output + error)
        old_state = self.store.get_collector()
        self.assertEqual(self.count(), 1)

        process = self.start("initializing")
        self.until(lambda: self.database.with_suffix(".initializing").exists(), process=process)
        # 첫 상태 저장 전에는 DB에 이전 실행 ID가 남아 있어도 현재 프로세스를 종료한다.
        self.assertEqual(self.store.get_collector()["collector_id"], old_state["collector_id"])
        self.stop_cli()
        time.sleep(1.1)
        self.database.with_suffix(".release-initialization").touch()
        output, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, output + error)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.store.get_collector()["state"], "stopped")
        self.assertNotEqual(self.store.get_collector()["collector_id"], old_state["collector_id"])

    def test_invalid_utf8_stop_file_keeps_monitor_alive_and_can_be_replaced(self):
        process = self.start()
        self.until(lambda: self.count() == 1, process=process)
        initial = self.store.get_collector()
        self.database.with_suffix(".stop").write_bytes(b"\xff\xfeinvalid-utf8")
        self.until(lambda: self.store.get_collector()["heartbeat_at"] > initial["heartbeat_at"],
                   timeout=8, process=process)
        current = self.store.get_collector()
        self.assertEqual(current["state"], "running")
        self.assertEqual(current["error"], "수집기 종료 요청 파일을 읽지 못했습니다.")
        self.stop_cli()
        output, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, output + error)
        self.assertEqual(self.store.get_collector()["state"], "stopped")

    def test_stop_request_cannot_cross_a_process_restart_or_apply_an_old_run_token(self):
        first = self.start()
        self.until(lambda: self.count() == 1, process=first)
        old_id = self.store.get_collector()["collector_id"]
        request_ready, release_request = threading.Event(), threading.Event()
        original_replace = Path.replace
        outcomes = []

        def paused_replace(source, target):
            if Path(target) == self.database.with_suffix(".stop"):
                request_ready.set()
                if not release_request.wait(5):
                    raise RuntimeError("Stop request was not released")
            return original_replace(source, target)

        def stop():
            try:
                outcomes.append(request_stop(self.database))
            except Exception as error:
                outcomes.append(type(error).__name__)

        with patch.object(Path, "replace", paused_replace):
            stopper = threading.Thread(target=stop)
            stopper.start()
            try:
                self.assertTrue(request_ready.wait(5))
                # 요청이 이전 실행 토큰을 읽은 뒤 그 프로세스를 종료하고 새 실행을 시작한다.
                first.kill()
                first.communicate(timeout=5)
                restarted = self.start()
                time.sleep(0.2)
                self.assertIsNone(restarted.poll())
                self.assertEqual(self.store.get_collector()["collector_id"], old_id)
            finally:
                release_request.set()
                stopper.join(timeout=5)
            self.assertFalse(stopper.is_alive())
            self.assertEqual(outcomes, [True])

        self.until(lambda: self.count() == 2, process=restarted)
        new_id = self.store.get_collector()["collector_id"]
        self.assertNotEqual(new_id, old_id)
        # 지연된 옛 요청이나 벽시계 값은 새 실행에 영향을 주지 않는다.
        self.database.with_suffix(".stop").write_text(
            json.dumps({"collector_id": old_id, "requested_at": time.time() + 3600}), encoding="utf-8")
        time.sleep(1.2)
        self.assertIsNone(restarted.poll())
        self.assertEqual(self.store.get_collector()["state"], "running")
        self.stop_cli()
        output, error = restarted.communicate(timeout=5)
        self.assertEqual(restarted.returncode, 0, output + error)
        self.assertEqual(self.store.get_collector()["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
