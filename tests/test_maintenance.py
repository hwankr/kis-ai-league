"""Offline deployment and final transport-boundary permission regressions."""
from contextlib import closing, contextmanager
from decimal import Decimal
import io
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.maintenance import MaintenanceError, MaintenanceGate, main
from backend.request_gate import file_lock
from tests import test_experiments as fixtures


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.gate = MaintenanceGate(self.directory)

    def test_uninitialized_status_does_not_create_files(self):
        self.assertEqual(self.gate.snapshot()['revision'], 0)
        self.assertFalse(self.gate.snapshot()['closed'])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_restart_preserves_gate_and_does_not_modify_financial_store(self):
        financial = self.directory / 'experiments.sqlite3'
        financial.write_bytes(b'original financial records and user intent')
        original = financial.read_bytes()
        closed = self.gate.close('deployment')
        self.assertEqual(closed['revision'], 1)
        self.assertTrue(MaintenanceGate(self.directory).snapshot()['closed'])
        self.assertEqual(financial.read_bytes(), original)
        with closing(sqlite3.connect(self.gate.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM audit').fetchone()[0], 1)

    def test_release_requires_exact_revision_and_reclose_invalidates_old_plan(self):
        self.gate.close('deployment')
        with self.assertRaises(MaintenanceError):
            self.gate.open(expected_revision=0, reason='verified')
        opened = self.gate.open(expected_revision=1, reason='verified')
        self.assertFalse(opened['closed'])
        self.assertEqual(opened['revision'], 2)
        self.assertEqual(self.gate.close('next deployment')['revision'], 3)

    def test_corrupt_or_removed_initialized_state_fails_closed(self):
        self.gate.close('deployment')
        self.gate.path.unlink()
        with self.assertRaises(MaintenanceError):
            self.gate.snapshot()
        self.gate.path.write_text('corrupt', encoding='utf-8')
        with self.assertRaises(MaintenanceError):
            MaintenanceGate(self.directory).snapshot()

    def test_close_waits_for_inflight_submission_lock(self):
        attempted, completed = threading.Event(), threading.Event()
        errors = []
        def close():
            attempted.set()
            try:
                self.gate.close('deployment')
            except Exception as error:
                errors.append(error)
            completed.set()
        with file_lock(self.directory / 'submission.lock'):
            worker = threading.Thread(target=close)
            worker.start()
            self.assertTrue(attempted.wait(2))
            self.assertFalse(completed.wait(0.05))
        worker.join(2)
        self.assertFalse(errors)
        self.assertTrue(completed.is_set())
        self.assertTrue(self.gate.snapshot()['closed'])

    def test_cli_status_and_compare_and_swap_release(self):
        args = ['--directory', str(self.directory)]
        with patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(args + ['close', '--reason', 'deploy']), 0)
            self.assertEqual(main(args + ['open', '--reason', 'verified', '--expected-revision', '0']), 1)
            self.assertEqual(main(args + ['open', '--reason', 'verified', '--expected-revision', '1']), 0)


class SubmissionMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExperimentsTests('test_execution_is_paused_without_policy_or_automatic_start')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        self.fixture.enable()
        self.policy = self.service._policy()

    def submit(self, *, side='buy', run_id='approved', decision=None, key='one'):
        self.service._submit(self.policy, self.fixture.broker, decision or fixtures.decision(), side, 1,
                             Decimal('10000'), key, run_id=run_id)

    def active(self, *, revision=1, run_id='approved', effective_day=None):
        state = {'revision': revision, 'active': {'revision': revision, 'candidate_id': 'candidate-' + str(revision),
                    'run_id': run_id, 'effective_day': effective_day or self.fixture.current.date().isoformat()}}
        self.service.store.save_setting('daily_policy:' + self.policy['fingerprint'], state)

    def after_reservation(self, mutation):
        original = self.fixture.broker.submit
        def submit(*args, **kwargs):
            mutation()
            return original(*args, **kwargs)
        self.fixture.broker.submit = submit

    def assert_skipped(self):
        self.assertEqual(self.fixture.broker.submissions, [])
        order, = self.service.store.orders()
        self.assertEqual(order['status'], 'cancelled')
        self.assertTrue(order['submission_skipped'])
        self.assertFalse(order['request_sent'])
        self.assertNotIn('submission_attempted_at', order)

    def test_closed_gate_and_new_process_cannot_submit_or_change_user_intent(self):
        original = {key: self.service.store.setting(key) for key in ('enabled', 'user_paused', 'policy')}
        MaintenanceGate(self.service.directory).close('deployment')
        replacement = self.fixture.make_service()
        replacement._submit(self.policy, self.fixture.broker, fixtures.decision(), 'buy', 1,
                            Decimal('10000'), 'replacement', run_id='approved')
        self.assertEqual(self.fixture.broker.submissions, [])
        self.assertEqual(self.service.store.orders(), [])
        self.assertEqual({key: self.service.store.setting(key) for key in original}, original)

    def test_close_and_reopen_during_wait_blocks_stale_worker(self):
        def transition():
            gate = MaintenanceGate(self.service.directory)
            state = gate.close('deployment')
            gate.open(expected_revision=state['revision'], reason='replacement verified')
        self.after_reservation(transition)
        self.submit()
        self.assert_skipped()
        self.assertTrue(self.service.store.setting('enabled'))

    def test_policy_revision_or_run_change_during_wait_blocks_old_buy(self):
        self.active()
        self.after_reservation(lambda: self.active(revision=2, run_id='replacement'))
        self.submit()
        self.assert_skipped()

    def test_revision_change_with_same_run_also_blocks_old_buy(self):
        self.active()
        self.after_reservation(lambda: self.active(revision=2))
        self.submit()
        self.assert_skipped()

    def test_existing_lot_exit_does_not_depend_on_new_entry_run(self):
        self.active()
        self.after_reservation(lambda: self.active(revision=2, run_id='replacement'))
        self.submit(side='sell', run_id=None)
        self.assertEqual(len(self.fixture.broker.submissions), 1)

    def test_incorrect_active_run_is_rejected_before_intent(self):
        self.active(run_id='different')
        self.submit()
        self.assertEqual(self.service.store.orders(), [])
        self.assertEqual(self.fixture.broker.submissions, [])

    def test_kept_policy_allows_next_session_regular_run(self):
        self.active(run_id='previous-activation-run', effective_day='2026-10-02')
        self.submit(run_id='next-session-preopen-run')
        self.assertEqual(len(self.fixture.broker.submissions), 1)

    def test_kept_policy_revision_is_still_checked_during_wait(self):
        self.active(run_id='previous-activation-run', effective_day='2026-10-02')
        self.after_reservation(lambda: self.active(revision=2, run_id='previous-activation-run', effective_day='2026-10-02'))
        self.submit(run_id='next-session-preopen-run')
        self.assert_skipped()

    def test_protection_activated_during_wait_blocks_buy(self):
        key = 'learning_account:' + self.policy['fingerprint']
        self.after_reservation(lambda: self.service.store.save_setting(key, {'risk': {'active': True}}))
        self.submit()
        self.assert_skipped()

    def test_limits_changed_during_wait_block_buy(self):
        self.after_reservation(lambda: self.service.store.save_setting('policy', {**self.policy, 'order_cap': '1'}))
        self.submit()
        self.assert_skipped()

    def test_own_reserved_intent_does_not_double_count_daily_limit(self):
        self.service.store.save_setting('policy', {**self.policy, 'daily_buy_limit': '10002', 'order_cap': '10002'})
        self.policy = self.service._policy()
        self.submit()
        self.assertEqual(len(self.fixture.broker.submissions), 1)

    def test_current_limit_blocks_oversized_order_at_send_boundary(self):
        self.service.store.save_setting('policy', {**self.policy, 'order_cap': '9999'})
        self.policy = self.service._policy()
        self.submit()
        self.assert_skipped()

    def test_new_fill_consumes_daily_capacity_while_request_waits(self):
        def other_fill():
            prior = self.service.store.orders()[0]
            other = dict(prior, id='other-fill', symbol='000660', quantity=4, filled_quantity=4,
                         filled_amount='40000', average_price='10000', remaining_quantity=0,
                         status='filled', order_id='other', branch_id='12345')
            self.service.store.reserve_order(other, 'other-fill')
        self.after_reservation(other_fill)
        self.submit()
        self.assertEqual(self.fixture.broker.submissions, [])
        original = next(row for row in self.service.store.orders() if row['id'] != 'other-fill')
        self.assertEqual(original['status'], 'cancelled')
        self.assertFalse(original['request_sent'])

    def test_time_boundary_is_rechecked_after_local_final_validation_wait(self):
        original = self.service._profile
        calls = []
        def profile(policy):
            result = original(policy)
            calls.append(True)
            if len(calls) == 2:
                self.fixture.current = self.fixture.current.replace(hour=15, minute=15)
            return result
        with patch.object(self.service, '_profile', side_effect=profile):
            self.submit(side='sell', run_id=None)
        self.assert_skipped()

    def test_maintenance_is_rechecked_after_real_transport_wait_stages(self):
        from backend.kis import PaperClient
        from backend.paper_broker import PaperBroker
        for stage in ('token', 'request_guard', 'request_spacing'):
            with self.subTest(stage=stage):
                gate = MaintenanceGate(self.service.directory)
                if gate.snapshot()['closed']:
                    gate.open(expected_revision=gate.snapshot()['revision'], reason='next test')
                profile = self.service._profile(self.policy)
                @contextmanager
                def waiting(name):
                    if name == stage:
                        gate.close('deployment during ' + name)
                    yield
                def token():
                    if stage == 'token':
                        gate.close('deployment during token')
                    return 'test-token'
                client = PaperClient(profile.settings, self.fixture.root / 'test-token.json',
                                     request_guard=lambda: waiting('request_guard'))
                broker = PaperBroker(profile, client=client, now=lambda: self.fixture.current)
                with patch.object(client, 'token', side_effect=token), \
                        patch('backend.kis.request_spacing', side_effect=lambda _: waiting('request_spacing')), \
                        patch('backend.kis.urlopen') as opened:
                    self.service._submit(self.policy, broker, fixtures.decision(), 'buy', 1, Decimal('10000'),
                                         stage, run_id='approved')
                opened.assert_not_called()
                order = next(row for row in self.service.store.orders() if row['symbol'] == '005930'
                             and row['status'] == 'cancelled')
                self.assertTrue(order['submission_skipped'])


if __name__ == '__main__':
    unittest.main()
