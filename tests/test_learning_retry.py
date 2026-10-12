"""Same-session research handoffs through the real worker, with no external calls."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from backend.experiment_store import ExperimentStore
from backend.experiments import ALL_STRATEGIES
from backend.learning import LearningService
from backend.request_gate import file_lock
from tests import test_experiments as service_fixtures
from tests.test_learning import LIMITS, candidate, raw_input


def wait_for_worker(test, learner):
    worker = learner.worker
    if worker is not None:
        worker.join(10)
        test.assertFalse(worker.is_alive(), "research worker did not finish")


class LearningRetryTests(unittest.TestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ExperimentStore(Path(temporary.name) / "experiments.sqlite3")
        self.store.save_settings({"enabled": False, "user_paused": True, "pause_reason": "사용자 중지"})
        self.data = raw_input(0)
        self.current = datetime.fromisoformat(self.data["observed_at"])
        self.proposer = Mock(return_value={"policy": candidate(), "method": "fixture",
                                          "rationale": "offline", "development": None})
        self.learner = self.make_learner()

    def make_learner(self):
        learner = LearningService(self.store, enabled=True, now=lambda: self.current, proposer=self.proposer)
        self.addCleanup(learner.close)
        return learner

    def context(self, **changes):
        book = {"as_of": self.data["as_of"], "cash": "1000000", "equity": "1000000",
                "positions": [], "reserved_cash": "0", "pending_orders": 0, "unresolved_orders": 0}
        book.update(changes)
        return {"initial_state": book, "execution_model": {"mode": "daily_open"}}

    def enqueue(self, context, *, wait=True):
        self.learner.enqueue(deepcopy(self.data), [], dict(LIMITS), source_id="same-completed-analysis",
                             context=deepcopy(context))
        if wait:
            wait_for_worker(self, self.learner)

    def assert_waiting(self):
        self.assertEqual(self.learner.state()["status"], "waiting")
        self.assertIsNone(self.learner.state().get("trial"))
        self.proposer.assert_not_called()

    def assert_one_trial_and_paused(self):
        self.assertEqual(self.proposer.call_count, 1)
        self.assertIsNotNone(self.learner.state().get("trial"))
        self.assertEqual(self.learner.state()["trial_sequence"], 1)
        self.assertEqual(len(self.learner.history()), 1)
        self.assertEqual(len(self.learner.frames()), 1)
        self.assertFalse(self.store.setting("enabled"))
        self.assertTrue(self.store.setting("user_paused"))
        self.assertEqual(self.store.setting("pause_reason"), "사용자 중지")
        self.assertEqual(self.store.orders(), [])

    def test_missing_book_can_become_ready_with_the_same_analysis(self):
        self.enqueue({"execution_model": {"mode": "daily_open"}})
        self.assert_waiting()
        self.enqueue(self.context())
        self.assert_one_trial_and_paused()

    def test_pending_orders_can_settle_on_the_same_day(self):
        self.enqueue(self.context(pending_orders=1, reserved_cash="100000"))
        self.assert_waiting()
        self.enqueue(self.context())
        self.assert_one_trial_and_paused()

    def test_unresolved_orders_can_settle_on_the_same_day(self):
        self.enqueue(self.context(unresolved_orders=1))
        self.assert_waiting()
        self.enqueue(self.context())
        self.assert_one_trial_and_paused()

    def test_restart_rechecks_a_waiting_same_day_without_unpausing(self):
        self.enqueue(self.context(pending_orders=1, unresolved_orders=1))
        self.assert_waiting()
        self.assertTrue(self.learner.close())
        self.learner = self.make_learner()
        self.enqueue(self.context())
        self.assert_one_trial_and_paused()

    def test_repeated_polling_and_ready_book_changes_do_not_reroll_a_trial(self):
        self.enqueue(self.context(pending_orders=1))
        for _ in range(3):
            self.enqueue(self.context(pending_orders=1))
        self.assert_waiting()
        self.enqueue(self.context())
        original = deepcopy(self.learner.state()["trial"])
        for _ in range(3):
            self.enqueue(self.context())
        self.enqueue(self.context(cash="999000", equity="999000"))
        self.assertEqual(self.learner.state()["trial"], original)
        self.assert_one_trial_and_paused()

    def test_polling_while_a_proposal_is_running_does_not_duplicate_it(self):
        entered, release = threading.Event(), threading.Event()
        proposal = deepcopy(self.proposer.return_value)

        def propose(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release the proposer")
            return deepcopy(proposal)

        self.proposer.side_effect = propose
        self.enqueue(self.context(), wait=False)
        try:
            self.assertTrue(entered.wait(5), "proposer was not reached")
            for _ in range(3):
                self.enqueue(self.context(), wait=False)
            self.enqueue(self.context(cash="999000", equity="999000"), wait=False)
        finally:
            release.set()
            wait_for_worker(self, self.learner)
        self.assert_one_trial_and_paused()

    def test_ready_handoff_is_not_lost_while_the_waiting_attempt_runs(self):
        entered, release = threading.Event(), threading.Event()
        process = self.learner.process

        def process_after_release(*args, **kwargs):
            if not kwargs["context"].get("initial_state"):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test did not release the waiting attempt")
            return process(*args, **kwargs)

        with patch.object(self.learner, "process", side_effect=process_after_release):
            self.enqueue({"execution_model": {"mode": "daily_open"}}, wait=False)
            try:
                self.assertTrue(entered.wait(5), "waiting attempt was not reached")
                self.enqueue(self.context(), wait=False)
            finally:
                release.set()
                wait_for_worker(self, self.learner)
        self.assert_one_trial_and_paused()

    def test_latest_unresolved_state_cancels_a_queued_ready_handoff(self):
        entered, release = threading.Event(), threading.Event()
        process = self.learner.process

        def process_after_release(*args, **kwargs):
            if kwargs["context"]["initial_state"].get("pending_orders"):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test did not release the waiting attempt")
            return process(*args, **kwargs)

        with patch.object(self.learner, "process", side_effect=process_after_release):
            self.enqueue(self.context(pending_orders=1), wait=False)
            try:
                self.assertTrue(entered.wait(5), "waiting attempt was not reached")
                self.enqueue(self.context(), wait=False)
                self.enqueue(self.context(unresolved_orders=1), wait=False)
            finally:
                release.set()
                wait_for_worker(self, self.learner)
        self.assert_waiting()
        self.assertEqual(self.learner.history(), [])
        self.enqueue(self.context())
        self.assert_one_trial_and_paused()

    def test_same_analysis_retries_after_research_lock_contention(self):
        with file_lock(self.store.path.parent / "learning.lock"):
            self.enqueue(self.context())
        self.proposer.assert_not_called()
        self.enqueue(self.context())
        self.assert_one_trial_and_paused()

    def test_queued_ready_handoff_recovers_after_the_previous_attempt_loses_the_lock(self):
        entered, release = threading.Event(), threading.Event()

        @contextmanager
        def busy_lock(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release the lock attempt")
            raise BlockingIOError("another research worker owns the lock")
            yield

        with patch("backend.learning.file_lock", busy_lock):
            self.enqueue({"execution_model": {"mode": "daily_open"}}, wait=False)
            try:
                self.assertTrue(entered.wait(5), "lock attempt was not reached")
                self.enqueue(self.context(), wait=False)
            finally:
                release.set()
                wait_for_worker(self, self.learner)
        self.proposer.assert_not_called()
        self.enqueue(self.context())
        self.assert_one_trial_and_paused()

    def test_same_day_account_change_invalidates_the_existing_trial(self):
        self.store.save_setting("policy", {"fingerprint": "paper-account-a"})
        self.enqueue(self.context())
        original = deepcopy(self.learner.state()["trial"])
        self.store.save_setting("policy", {"fingerprint": "paper-account-b"})
        self.enqueue(self.context())
        trial = self.learner.state()["trial"]
        self.assertEqual(trial["id"], original["id"])
        self.assertEqual(trial["fingerprint"], "paper-account-a")
        self.assertEqual(trial["decision"], "invalid")
        self.assertEqual(trial["invalid_reason"], "운용 계좌 또는 한도 변경")
        self.assert_one_trial_and_paused()


class LearningCloseHandoffTests(unittest.TestCase):
    make_service = service_fixtures.ExperimentsTests.make_service

    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        service_fixtures.ExperimentsTests.setUp(self)
        self.policy.update(execution_strategy=ALL_STRATEGIES)
        self.service._configure(self.policy)
        self.service.learning.enabled = True
        self.service.learning.llm_factory = None
        self.service.learning.development_reader = None
        self.service.learning.proposer = Mock(return_value={"policy": candidate(), "method": "fixture",
                                                           "rationale": "offline", "development": None})
        self.service.store.save_settings({"enabled": False, "user_paused": True})

    def test_close_handoff_reaches_real_worker_after_preclose_wait(self):
        data = raw_input(0)
        self.current = datetime.fromisoformat(data["observed_at"]).replace(hour=15, minute=59)
        run = {"collection_hash": "same-completed-analysis", "signals": []}
        self.service._learn(data, run)
        wait_for_worker(self, self.service.learning)
        self.assertIsNone(self.service.learning.state().get("trial"))
        self.service.learning.proposer.assert_not_called()
        self.current = self.current.replace(hour=16, minute=0)
        self.service._learn(data, run)
        wait_for_worker(self, self.service.learning)
        self.assertEqual(self.service.learning.proposer.call_count, 1)
        self.assertIsNotNone(self.service.learning.state().get("trial"))
        self.assertFalse(self.service.store.setting("enabled"))
        self.assertTrue(self.service.store.setting("user_paused"))
        self.assertEqual(self.broker.submissions, [])
        self.assertEqual(self.broker.cancellations, [])
        self.llm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
