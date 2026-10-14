"""Offline contract/execution checks; injected verdicts do not test profitability."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
import threading
import sqlite3
from unittest.mock import patch

from backend.daily_inputs import _hash, freeze_entry_selection
from backend.experiments import FEE, OperationalBlock
from backend.kis import KST, KisError, PaperClient
from backend.learning_policy import validate_policy
from backend.paper_broker import PaperBroker
from backend.request_gate import file_lock
from tests import test_daily_activation_recovery as lifecycle
from tests import test_experiments as fixtures
from tests import test_portfolio_plan as allocation


class EntrySelectionLifecycleTests(unittest.TestCase):
    # Reuse only fixtures, never inherit their unrelated test cases.
    setUp = lifecycle.DailyActivationRecoveryTests.setUp
    make_service = lifecycle.DailyActivationRecoveryTests.make_service
    completed_input = staticmethod(lifecycle.DailyActivationRecoveryTests.completed_input)
    set_champion = lifecycle.DailyActivationRecoveryTests.set_champion
    baseline_analysis = lifecycle.DailyActivationRecoveryTests.baseline_analysis
    fill = lifecycle.DailyActivationRecoveryTests.fill
    restart = lifecycle.DailyActivationRecoveryTests.restart
    controls = lifecycle.DailyActivationRecoveryTests.controls
    allow_fixture_orders = lifecycle.DailyActivationRecoveryTests.allow_fixture_orders
    stage = lifecycle.DailyActivationRecoveryTests.stage
    learned_signals = staticmethod(lifecycle.DailyActivationRecoveryTests.learned_signals)
    committed_run = lifecycle.DailyActivationRecoveryTests.committed_run
    activate_at = lifecycle.DailyActivationRecoveryTests.activate_at
    advance_completed_input = lifecycle.DailyActivationRecoveryTests.advance_completed_input

    def entry_policy(self, **changes):
        policy = deepcopy(self.service.learning.daily.policy())
        policy.update(name="Entry lifecycle fixture", held_before_top_n=True,
                      no_same_day_reentry=True, max_positions=3,
                      allocation_mode="equal_eligible", **changes)
        policy["strategies"][0].update(top_n=1, holding_sessions=5)
        return validate_policy(policy)

    def seed_entry(self, symbol, *, filled=True, holding=10):
        previous = self.current
        self.current = datetime(2026, 10, 1, 10, tzinfo=KST)
        self.allow_fixture_orders()
        decision = {**fixtures.decision(symbol), "strategy_id": "learned-volume",
            "policy_id": "old-lot-policy", "reference_close": "10000",
            "exit_policy": {"holding_sessions": holding, "stop_loss_pct": 0, "take_profit_pct": 0}}
        index = len(self.broker.remote)
        self.service._submit(self.service._policy(), self.broker, decision, "buy", 1,
                             Decimal(10000), "seed:" + symbol, run_id="old-run")
        if filled:
            self.fill(1, index=index, status="filled")
            self.broker.account["holdings"][symbol] = {
                "quantity": 1, "sellable_quantity": 1, "price": "10000"}
        self.current = previous
        return next(item for item in self.service.store.orders() if item["symbol"] == symbol)

    def archive_payload(self, pending):
        with self.service.store.connect() as db:
            return db.execute("SELECT payload FROM daily_policy_preparations WHERE decision_id=?",
                              (pending["decision_id"],)).fetchone()[0]

    def test_prepare_uses_original_award_book_after_current_holdings_change(self):
        self.seed_entry(fixtures.SYMBOLS[0])
        candidate = self.entry_policy()
        pending, baseline, _, _ = self.stage(candidate, prepare=False)
        original = deepcopy(pending["award_context"])
        self.seed_entry(fixtures.SYMBOLS[1])
        controls = self.controls()
        self.assertTrue(self.service._prepare_daily(deepcopy(self.input_data), baseline))
        pending = self.service.learning.daily.pending()
        run = pending["prepared_run"]
        self.assertEqual(pending["award_context"], original)
        self.assertEqual(run["entry_selection"], freeze_entry_selection(candidate, original["initial_state"]))
        self.assertEqual(run["entry_selection"]["context"]["held_symbols"], [fixtures.SYMBOLS[0]])
        self.assertEqual([item["symbol"] for item in self.learned_signals(run)], [fixtures.SYMBOLS[1]])
        # A later held name cannot make the frozen top-N rerank to the third name.
        sent = list(self.broker.submissions)
        self.restart()
        active = self.activate_at("2026-10-06")
        self.assertEqual(self.committed_run(active), run)
        self.assertEqual(self.broker.submissions, sent)
        self.assertEqual(self.controls(), controls)

    def test_restart_activation_executes_lower_rank_with_same_limits_and_old_lot_exit(self):
        old = self.seed_entry(fixtures.SYMBOLS[0])
        controls = self.controls()
        pending, _, _, _ = self.stage(self.entry_policy())
        frozen = deepcopy(pending["prepared_run"])
        self.assertEqual([item["symbol"] for item in self.learned_signals(frozen)], [fixtures.SYMBOLS[1]])
        archive = self.archive_payload(pending)
        self.restart()
        active = self.activate_at("2026-10-06")
        self.assertEqual(self.committed_run(active), frozen)
        self.assertEqual(self.archive_payload(pending), archive)
        self.assertEqual(self.broker.submissions[-1][0:2], (fixtures.SYMBOLS[1], "buy"))
        self.assertEqual(len(self.broker.submissions), 2)
        order = next(item for item in self.service.store.orders() if item["run_id"] == active["run_id"])
        self.assertEqual(order["policy_id"], pending["candidate_id"])
        self.assertEqual(order["exit_policy"]["holding_sessions"], 5)
        self.assertLessEqual(Decimal(order["limit_price"]) * order["quantity"] * (1 + FEE), Decimal(self.policy["order_cap"]))
        self.assertEqual(self.controls(), controls)
        stored_old = next(item for item in self.service.store.orders() if item["id"] == old["id"])
        self.assertEqual(stored_old["exit_policy"], old["exit_policy"])
        self.service._cycle()
        self.assertEqual(len(self.broker.submissions), 2)

    def test_prepare_rejects_missing_or_changed_context_hash_and_signal_payload(self):
        self.seed_entry(fixtures.SYMBOLS[0])
        pending, _, _, _ = self.stage(self.entry_policy())
        run = pending["prepared_run"]
        archive = self.archive_payload(pending)
        variants = []
        missing = deepcopy(run)
        missing.pop("entry_selection")
        variants.append(missing)
        changed = deepcopy(run)
        changed["entry_selection"]["context"]["held_symbols"] = []
        variants.append(changed)
        rehashed = deepcopy(changed)
        contract = rehashed["entry_selection"]
        contract["hash"] = _hash({key: value for key, value in contract.items() if key != "hash"})
        variants.append(rehashed)
        changed_signal = deepcopy(run)
        self.learned_signals(changed_signal)[0]["symbol"] = fixtures.SYMBOLS[2]
        variants.append(changed_signal)
        for index, value in enumerate(variants):
            with self.subTest(index=index):
                self.assertFalse(self.service.learning.daily.prepare(pending, value))
                self.assertEqual(self.service.learning.daily.pending(), pending)
                self.assertEqual(self.archive_payload(pending), archive)

    def test_restart_blocks_tampered_pending_and_preserves_first_preparation(self):
        self.seed_entry(fixtures.SYMBOLS[0])
        pending, _, _, _ = self.stage(self.entry_policy())
        daily = self.service.learning.daily
        original_state = daily.state()
        original_record = daily.get_decision(pending["decision_id"])
        archive = self.archive_payload(pending)
        for mutation in ("missing_context", "rehash_context", "signals", "award_book"):
            with self.subTest(mutation=mutation):
                changed = deepcopy(original_state)
                current = changed["pending"]
                run = current["prepared_run"]
                if mutation == "missing_context":
                    run.pop("entry_selection")
                elif mutation == "rehash_context":
                    contract = run["entry_selection"]
                    contract["context"]["held_symbols"] = []
                    contract["hash"] = _hash({key: value for key, value in contract.items() if key != "hash"})
                elif mutation == "signals":
                    self.learned_signals(run)[0]["symbol"] = fixtures.SYMBOLS[2]
                else:
                    current["award_context"]["initial_state"]["positions"] = []
                self.service.store.save_setting(daily._key(pending["fingerprint"]), changed)
                self.restart()
                daily = self.service.learning.daily
                self.current = datetime(2026, 10, 6, 9, 5, tzinfo=KST)
                self.assertFalse(self.service._activate_daily(self.current.date(), self.broker.days))
                self.assertIsNone(daily.active())
                self.assertEqual(daily.snapshot()["status"], "blocked")
                self.assertEqual(daily.state()["activation_check"]["code"], "award_inputs_mismatch")
                self.assertEqual(daily.pending(), current)
                self.assertEqual(daily.get_decision(pending["decision_id"]), original_record)
                self.assertEqual(self.archive_payload(pending), archive)
                self.assertNotIn(run["id"], [item["id"] for item in self.service.store.runs()])

    def test_intraday_analyze_preserves_first_frozen_mask_until_new_completed_frame(self):
        candidate = self.entry_policy()
        self.set_champion(candidate)
        first = self.service._analyze()
        self.assertEqual(self.service._analyze()["id"], first["id"])
        self.current = datetime(2026, 10, 6, 10, tzinfo=KST)
        self.seed_entry(fixtures.SYMBOLS[0])
        second = self.service._analyze()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["signals"], first["signals"])
        self.seed_entry(fixtures.SYMBOLS[1], filled=False)
        third = self.service._analyze()
        self.assertEqual(second["collection_hash"], third["collection_hash"])
        self.assertEqual(third["entry_selection"], first["entry_selection"])
        self.assertEqual(first["entry_selection"]["context"], {"held_symbols": [], "reserved_symbols": []})
        # A new completed market frame may freeze the updated book once.
        self.advance_completed_input("2026-10-06")
        next_close = self.service._analyze()
        self.assertNotEqual(first["id"], next_close["id"])
        self.assertEqual(next_close["entry_selection"]["context"], {
            "held_symbols": [fixtures.SYMBOLS[0]], "reserved_symbols": [fixtures.SYMBOLS[1]]})
        self.assertEqual([item["symbol"] for item in self.learned_signals(next_close)], [fixtures.SYMBOLS[2]])
        self.assertEqual(self.service._analyze()["id"], next_close["id"])

    def test_live_reserved_and_held_exclusion_uses_frozen_lower_rank(self):
        self.seed_entry(fixtures.SYMBOLS[0])
        self.seed_entry(fixtures.SYMBOLS[1], filled=False)
        candidate = self.entry_policy()
        self.set_champion(candidate)
        run = self.service._analyze()
        self.assertEqual([item["symbol"] for item in self.learned_signals(run)], [fixtures.SYMBOLS[2]])
        self.current = datetime(2026, 10, 6, 9, 5, tzinfo=KST)
        decisions, _ = self.service._entry_plan(self.service._policy(), run, self.current.date())
        self.service._allocated_entries(self.service._policy(), candidate, run, decisions,
            self.broker, self.current.date(), {symbol: {"board": "KOSPI"} for symbol in fixtures.SYMBOLS}, self.master)
        self.assertEqual(self.broker.submissions[-1][0:2], (fixtures.SYMBOLS[2], "buy"))
        self.assertEqual(len(self.broker.submissions), 3)

    def test_activation_keeps_existing_lot_due_exit_even_when_candidate_holds_longer(self):
        old = self.seed_entry(fixtures.SYMBOLS[0], holding=1)
        self.stage(self.entry_policy())
        self.restart()
        self.activate_at("2026-10-06")
        self.assertIn((fixtures.SYMBOLS[0], "sell", 1, "10000"), self.broker.submissions)
        stored = next(item for item in self.service.store.orders() if item["id"] == old["id"])
        self.assertEqual(stored["exit_policy"], old["exit_policy"])
        self.assertTrue(stored["exit_pending"])

    def test_reconciliation_failure_stops_activation_and_entries(self):
        pending, _, _, _ = self.stage(self.entry_policy())
        self.allow_fixture_orders()
        self.current = datetime(2026, 10, 6, 9, 5, tzinfo=KST)
        with patch.object(self.service, "_reconcile", side_effect=KisError("offline read failure")):
            with self.assertRaises(KisError):
                self.service._cycle()
        self.assertIsNone(self.service.learning.daily.active())
        self.assertEqual(self.service.learning.daily.pending(), pending)
        self.assertEqual(self.broker.submissions, [])

    def test_activated_preparation_remains_analyze_cache_after_intraday_fill(self):
        self.seed_entry(fixtures.SYMBOLS[0])
        pending, _, _, _ = self.stage(self.entry_policy())
        frozen = deepcopy(pending["prepared_run"])
        self.restart()
        active = self.activate_at("2026-10-06")
        self.fill(self.broker.remote[1]["quantity"], index=1, status="filled")
        self.current += timedelta(minutes=5)
        run = self.service._analyze()
        self.assertEqual(run["id"], active["run_id"])
        self.assertEqual(run, {key: value for key, value in frozen.items() if key != "analysis"})
        self.assertEqual(self.committed_run(active), frozen)
        self.assertEqual([item["symbol"] for item in self.learned_signals(run)], [fixtures.SYMBOLS[1]])

    def test_intraday_missing_original_or_new_frame_cannot_generate_new_entry_mask(self):
        self.set_champion(self.entry_policy())
        self.current = datetime(2026, 10, 6, 9, 5, tzinfo=KST)
        with self.assertRaises(OperationalBlock):
            self.service._analyze()
        self.assertEqual(self.service.store.runs(), [])
        self.analyzer.assert_not_called()
        self.advance_completed_input("2026-10-06")
        self.current = self.current.replace(hour=15)
        with self.assertRaises(OperationalBlock):
            self.service._analyze()
        self.assertEqual(self.service.store.runs(), [])
        self.analyzer.assert_not_called()


class EntrySelectionTransmissionTests(unittest.TestCase):
    setUp = allocation.AllocationExecutionTests.setUp
    make_service = allocation.AllocationExecutionTests.make_service
    enable = allocation.AllocationExecutionTests.enable
    allocate = allocation.AllocationExecutionTests.allocate

    def stored_order(self, symbol, *, side="sell", filled=True, status=None, order_id="prior", store=None):
        store = store or self.service.store
        quantity = 1 if filled else 0
        order = {"id": order_id, "strategy_id": "learned-volume", "symbol": symbol,
            "name": "Offline fixture", "side": side, "quantity": 1, "limit_price": "10000",
            "filled_quantity": quantity, "filled_amount": str(quantity * 10000),
            "average_price": "10000" if filled else None,
            "remaining_quantity": 0 if filled else 1, "status": status or ("filled" if filled else "submitted"),
            "order_id": order_id, "branch_id": "12345", "order_date": self.current.date().isoformat(),
            "created_at": self.service._timestamp(), "fingerprint": self.service._policy()["fingerprint"],
            "run_id": "old-run", "error": None,
            "exit_policy": {"holding_sessions": 10, "stop_loss_pct": 0, "take_profit_pct": 0}}
        if side == "sell" and filled:
            # A sold lot has a prior buy; never inject an impossible negative book.
            buy = dict(order, id="buy:" + order_id, order_id="buy:" + order_id, side="buy",
                       order_date="2026-10-02", created_at="2026-10-02T00:05:00+00:00")
            store.reserve_order(buy, "fixture-buy:" + order_id)
        store.reserve_order(order, "fixture:" + order_id)
        return order

    def test_opt_in_blocks_same_day_sell_before_allocation_but_omitted_flag_is_unchanged(self):
        self.stored_order(fixtures.SYMBOLS[0])
        self.execution["no_same_day_reentry"] = True
        self.allocate()
        self.assertEqual([item[0] for item in self.broker.submissions], list(fixtures.SYMBOLS[1:]))
        # A direct ordinary legacy submit keeps its existing semantics.
        self.service._submit(self.service._policy(), self.broker, self.signals[0], "buy", 1,
                             Decimal(10000), "explicit-legacy-path", run_id="legacy")
        self.assertEqual(self.broker.submissions[-1][0], fixtures.SYMBOLS[0])

    def test_late_fill_or_reservation_is_blocked_after_token_and_before_http(self):
        for stage in ("token", "request_guard", "request_spacing"):
            for mutation in ("sold", "held", "reserved", "unknown"):
                with self.subTest(stage=stage, mutation=mutation):
                    # Each case uses a distinct temporary account/intent state.
                    with self.service.store.connect() as db:
                        db.execute("DELETE FROM orders")
                    policy = self.service._policy()
                    profile = self.service._profile(policy)
                    symbol = fixtures.SYMBOLS[0]
                    changed = []
                    def invalidate():
                        if changed:
                            return
                        changed.append(True)
                        self.stored_order(symbol if mutation != "unknown" else fixtures.SYMBOLS[2],
                            side="sell" if mutation == "sold" else "buy",
                            filled=mutation in ("sold", "held"),
                            status="unknown" if mutation == "unknown" else None)
                    @contextmanager
                    def waiting(name):
                        if name == stage:
                            invalidate()
                        yield
                    def token():
                        if stage == "token":
                            invalidate()
                        return "test-token"
                    client = PaperClient(profile.settings, self.root / "test-entry-token.json",
                                         request_guard=lambda: waiting("request_guard"))
                    broker = PaperBroker(profile, client=client, now=lambda: self.current)
                    decision = dict(self.signals[0], no_same_day_reentry=True)
                    with patch.object(client, "token", side_effect=token), \
                            patch("backend.kis.request_spacing", side_effect=lambda _: waiting("request_spacing")), \
                            patch("backend.kis.urlopen") as opened:
                        self.service._submit(policy, broker, decision, "buy", 1, Decimal(10000),
                                             stage + ":" + mutation, run_id="new-run")
                    opened.assert_not_called()
                    created = next(item for item in self.service.store.orders() if item["run_id"] == "new-run")
                    self.assertEqual(created["status"], "cancelled")
                    self.assertTrue(created["submission_skipped"])
                    self.assertFalse(created["request_sent"])
                    self.assertNotIn("submission_attempted_at", created)

    def test_send_guard_allows_own_reserved_intent_and_no_duplicate_submit(self):
        decision = dict(self.signals[0], no_same_day_reentry=True)
        for _ in range(2):
            self.service._submit(self.service._policy(), self.broker, decision, "buy", 1,
                                 Decimal(10000), "once", run_id="new-run")
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.service.store.orders()[0]["status"], "submitted")

    def test_reentry_day_uses_kst_order_session_not_delayed_observation_timestamp(self):
        # The same UTC date spans two KST sessions; observation is not execution.
        self.current = datetime(2026, 10, 6, 14, 59, tzinfo=timezone.utc)
        symbol = fixtures.SYMBOLS[0]
        self.stored_order(symbol)
        order = next(item for item in self.service.store.orders() if item["id"] == "prior")
        order["reconciled_at"] = "2026-10-07T00:01:00+09:00"
        self.service.store.save_order(order)
        self.assertFalse(self.service._entry_still_available(self.service._policy(), symbol))
        self.current += timedelta(minutes=2)
        self.assertEqual(self.current.astimezone(KST).date().isoformat(), "2026-10-07")
        self.assertTrue(self.service._entry_still_available(self.service._policy(), symbol))

    def test_unknown_sell_and_unreconciled_owned_order_block_before_intent(self):
        for side, status in (("sell", "unknown"), ("sell", "submitted"), ("buy", "submitted")):
            with self.subTest(side=side, status=status):
                with self.service.store.connect() as db:
                    db.execute("DELETE FROM orders")
                self.stored_order(fixtures.SYMBOLS[0], side=side, filled=False, status=status)
                decision = dict(self.signals[0], no_same_day_reentry=True)
                self.service._submit(self.service._policy(), self.broker, decision, "buy", 1,
                                     Decimal(10000), "blocked", run_id="new-run")
                self.assertEqual(self.broker.submissions, [])
                self.assertTrue(all(item["run_id"] != "new-run" for item in self.service.store.orders()))

    def test_shared_submission_lock_wait_rereads_other_instance_reservation(self):
        other = self.make_service()
        entered, errors = threading.Event(), []
        original_lock = file_lock
        @contextmanager
        def observed_lock(path, **options):
            if path.name == "submission.lock":
                entered.set()
            with original_lock(path, timeout=3):
                yield
        def submit():
            try:
                self.service._submit(self.service._policy(), self.broker,
                    dict(self.signals[0], no_same_day_reentry=True), "buy", 1,
                    Decimal(10000), "blocked-after-lock", run_id="new-run")
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=submit)
        with patch("backend.experiments.file_lock", side_effect=observed_lock):
            try:
                with original_lock(self.service.directory / "submission.lock", timeout=3):
                    worker.start()
                    self.assertTrue(entered.wait(3))
                    self.stored_order(fixtures.SYMBOLS[0], side="buy", filled=False,
                                      order_id="other-intent", store=other.store)
            finally:
                if worker.ident is not None:
                    worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.broker.submissions, [])
        created = next(item for item in self.service.store.orders() if item["run_id"] == "new-run")
        self.assertEqual(created["status"], "cancelled")
        self.assertFalse(created["request_sent"])

    def test_entry_read_failure_after_token_is_known_unsent(self):
        policy = self.service._policy()
        profile = self.service._profile(policy)
        client = PaperClient(profile.settings, self.root / "test-entry-token.json")
        broker = PaperBroker(profile, client=client, now=lambda: self.current)
        original = self.service._owned_orders
        token_observed = []
        def token():
            token_observed.append(True)
            return "test-token"
        def read_orders(*args, **kwargs):
            if token_observed:
                raise sqlite3.OperationalError("fixture database read failed")
            return original(*args, **kwargs)
        with patch.object(client, "token", side_effect=token), \
                patch.object(self.service, "_owned_orders", side_effect=read_orders), \
                patch("backend.kis.request_spacing", side_effect=lambda _: nullcontext()), \
                patch("backend.kis.urlopen") as opened:
            self.service._submit(policy, broker, dict(self.signals[0], no_same_day_reentry=True),
                "buy", 1, Decimal(10000), "read-failed", run_id="new-run")
        opened.assert_not_called()
        order = self.service.store.orders()[0]
        self.assertEqual(order["status"], "cancelled")
        self.assertTrue(order["submission_skipped"])
        self.assertFalse(order["request_sent"])
        self.assertNotIn("submission_attempted_at", order)


if __name__ == "__main__":
    unittest.main()
