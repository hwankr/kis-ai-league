"""Offline service scenarios: durable intent, cumulative fills and bounded exposure."""
from contextlib import closing, contextmanager, nullcontext
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import io
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from backend.experiment_store import ExperimentStore, encode
from backend.experiments import ExperimentService, FEE, ALL_STRATEGIES
from backend.kis import KST, KisError, KisRequestError
from backend.paper_broker import BrokerUnknown, BrokerRejected
from backend.request_gate import file_lock


STRATEGY = "trend-breakout-v1"
SYMBOLS = ("005930", "000660", "035420")
NOW = datetime(2026, 10, 6, 10, 0, tzinfo=KST)


def decision(symbol="005930"):
    return {"strategy_id": STRATEGY, "symbol": symbol, "name": "테스트 " + symbol,
            "board": "KOSPI", "action": "buy", "status": "ready", "as_of": "2026-10-02"}


def hold_lock(path, ready, release):
    with file_lock(Path(path)):
        ready.set()
        release.wait(10)


class FakeBroker:
    def __init__(self, clock):
        self.clock = clock
        self.submissions, self.cancellations = [], []
        self.remote = []
        self.account = {"cash": "1000000", "total_value": "1000000", "holdings": {}}
        self.buying = {"cash": "1000000", "quantity": 100}
        self.price, self.eligible = "10000", True
        self.submit_error, self.cancel_error = None, None
        self.session_override = None
        self.days = ["2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-06"]

    def snapshot(self):
        return deepcopy(self.account)

    def quote(self, symbol):
        return {"symbol": symbol, "price": self.price, "eligible": self.eligible,
                "reason": None, "as_of": self.clock().isoformat()}

    def market_session(self, symbol):
        return deepcopy(self.session_override) if self.session_override else {
            "session_date": self.clock().date().isoformat(),
            "last_trade_at": (self.clock() - timedelta(seconds=30)).isoformat(), "volume": 100, "price": self.price}

    def session_days(self, start, end):
        return [day for day in self.days if start <= day <= end]

    def buyability(self, symbol, price):
        return dict(self.buying)

    def orders(self, start, end):
        return [deepcopy(row) for row in self.remote if start <= row["order_date"] <= end]

    def submit(self, symbol, side, quantity, price, *, send_guard=None):
        with send_guard() if send_guard is not None else nullcontext():
            return self._submit(symbol, side, quantity, price)

    def _submit(self, symbol, side, quantity, price):
        self.submissions.append((symbol, side, quantity, price))
        if self.submit_error:
            raise self.submit_error
        order_id = str(len(self.submissions))
        self.remote.append({"order_date": self.clock().date().isoformat(), "order_id": order_id, "branch_id": "12345",
            "symbol": symbol, "side": side, "quantity": quantity, "limit_price": price,
            "filled_quantity": 0, "remaining_quantity": quantity, "filled_amount": "0", "average_price": "0",
            "status": "open", "order_time": self.clock().strftime("%H:%M:%S")})
        return {"order_id": order_id, "branch_id": "12345", "order_time": self.clock().strftime("%H:%M:%S")}

    def cancel(self, order_id, branch_id, symbol, quantity):
        self.cancellations.append((order_id, branch_id, symbol, quantity))
        if self.cancel_error:
            raise self.cancel_error
        return {"order_id": "900", "branch_id": branch_id, "order_time": self.clock().strftime("%H:%M:%S")}


class ExperimentsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.toml"
        self.config.write_text("[accounts.paper]\napp_key='test-key'\napp_secret='test-secret'\naccount='12345678'\n", encoding="utf-8")
        self.current = NOW
        self.broker = FakeBroker(lambda: self.current)
        self.master = {"status": "ok", "stale": False, "rows": {symbol: {
            "board": "KOSPI", "halted": False, "liquidation": False, "managed": False, "low_liquidity": False,
            "investment_caution": False, "warning_code": "00", "preferred_code": "0"} for symbol in SYMBOLS}}
        from backend.experiment_analysis import analysis_version
        self.analyzer = Mock(return_value={"decisions": [decision()], "input_hash": "input",
            "version_id": analysis_version(self.config), "llm_status": "ready"})
        candidate = {"symbol": "005930", "name": "테스트", "board": "KOSPI", "status": "ok",
                     "as_of": "2026-10-02", "selection": {"status": "selected"}}
        self.input_data = {"as_of": "2026-10-02", "observed_at": "2026-10-02T17:00:00+09:00", "rows": [candidate],
                           "histories": {"005930": {}}, "calendars": {"KOSPI": []}, "benchmarks": {"KOSPI": {}}}
        self.llm = Mock()
        self.service = self.make_service()
        self.policy = {"account_id": "paper", "budget": "100000", "order_cap": "30000", "daily_buy_limit": "50000",
                       "execution_strategy": STRATEGY}
        self.service._configure(self.policy)
        universe_patch = patch("backend.experiments.load_universe", return_value={"status": "verified",
                              "rows": [{"symbol": symbol, "board": "KOSPI"} for symbol in SYMBOLS]})
        universe_patch.start()
        self.addCleanup(universe_patch.stop)

    def make_service(self):
        service = ExperimentService(self.config, directory=self.root / "state", now=lambda: self.current,
            broker_factory=lambda profile: self.broker, analyzer=self.analyzer, input_reader=lambda: deepcopy(self.input_data),
            llm_factory=lambda path: self.llm, master_reader=lambda: deepcopy(self.master))
        self.addCleanup(service.close)
        return service

    def enable(self):
        self.service.store.save_setting("enabled", True)

    def add_run(self, *, symbols=SYMBOLS, as_of="2026-10-02", created="2026-10-02T17:00:00+09:00", run_id="run1"):
        from backend.experiment_analysis import analysis_version
        self.service.store.add_run({"id": run_id, "as_of": as_of, "created_at": created,
            "signals": [decision(symbol) for symbol in symbols], "input_hash": "input",
            "version_id": analysis_version(self.config), "status": "ready"})

    def submit(self, quantity=5, *, symbol="005930", side="buy", key="one"):
        self.enable()
        self.service._submit(self.service._policy(), self.broker, decision(symbol), side, quantity, Decimal("10000"), key, run_id="run1")
        with self.service.store.connect() as db:
            row = db.execute("SELECT payload FROM orders WHERE intent_key=?", (key,)).fetchone()
        return json.loads(row[0])

    def fill(self, quantity, *, index=0, status="partial", amount=None):
        row = self.broker.remote[index]
        row.update(filled_quantity=quantity, remaining_quantity=row["quantity"] - quantity,
                   filled_amount=str(quantity * 10000) if amount is None else amount, average_price="10000", status=status)
        self.service._reconcile(self.service._policy(), self.broker)

    def test_execution_is_paused_without_policy_or_automatic_start(self):
        self.assertFalse(self.service.store.setting("enabled", False))
        self.service.tick()
        self.assertEqual(self.broker.submissions, [])
        other = ExperimentService(self.config, directory=self.root / "empty", now=lambda: self.current,
                                  broker_factory=lambda _: self.broker)
        with self.assertRaises(KisError):
            other.command({"action": "start"})
        self.assertFalse(other.store.setting("enabled", False))

    def test_competition_expiry_preserves_user_pause_and_cannot_be_started(self):
        self.current = datetime(2026, 11, 21, tzinfo=KST)
        for user_paused in (False, True):
            with self.subTest(user_paused=user_paused):
                self.service.store.save_settings({"enabled": True, "user_paused": user_paused,
                                                  "pause_reason": "사용자 중지" if user_paused else None})
                with patch.object(self.service, "_profile") as profile, patch.object(self.service, "_launch") as launch:
                    with self.assertRaisesRegex(KisError, "대회가 종료"):
                        self.service.command({"action": "start"})
                profile.assert_not_called()
                launch.assert_not_called()
                self.assertFalse(self.service.store.setting("enabled"))
                self.assertIs(self.service.store.setting("user_paused"), user_paused)
                if user_paused:
                    self.assertEqual(self.service.store.setting("pause_reason"), "사용자 중지")

    def test_start_rechecks_expiry_after_credentials(self):
        self.current = datetime(2026, 11, 20, 23, 59, 59, tzinfo=KST)
        self.service.store.save_settings({"enabled": False, "user_paused": True})
        def after_profile(policy):
            self.current += timedelta(seconds=1)
        with patch.object(self.service, "_profile", side_effect=after_profile), patch.object(self.service, "_launch") as launch:
            with self.assertRaisesRegex(KisError, "대회가 종료"):
                self.service.command({"action": "start"})
        self.assertTrue(self.service.store.setting("user_paused"))
        self.assertFalse(self.service.store.setting("enabled"))
        launch.assert_not_called()

    def test_competition_expiry_applies_on_restart_and_busy_tick(self):
        self.enable()
        self.service.store.save_setting("user_paused", False)
        self.current = datetime(2026, 11, 21, tzinfo=KST)
        restarted = self.make_service()
        self.assertFalse(restarted.store.setting("enabled"))
        self.assertFalse(restarted.store.setting("user_paused"))
        self.enable()
        self.service.store.save_setting("autonomy_status", {"next_retry_at": "2026-12-01T00:00:00+09:00"})
        with patch.object(self.service, "worker", Mock(is_alive=Mock(return_value=True))), patch.object(self.service, "_launch") as launch:
            self.service.tick()
        self.assertFalse(self.service.store.setting("enabled"))
        launch.assert_not_called()

    def test_first_autonomous_boot_does_not_enable_orders_after_competition(self):
        self.config.write_text(self.config.read_text(encoding="utf-8") + "\n[experiments]\nautonomous=true\n", encoding="utf-8")
        self.current = datetime(2026, 11, 21, tzinfo=KST)
        restarted = self.make_service()
        self.assertFalse(restarted.store.setting("enabled"))
        self.assertFalse(restarted.store.setting("user_paused", False))
        self.assertTrue(restarted.store.setting("autonomous_initialized"))
        self.assertEqual(self.broker.submissions, [])

    def test_competition_expiry_keeps_reconciliation_and_analysis_available(self):
        self.current = datetime(2026, 11, 20, 10, tzinfo=KST)
        self.submit(quantity=2)
        self.broker.remote[0].update(filled_quantity=2, remaining_quantity=0, filled_amount="20000",
                                     average_price="10000", status="filled")
        self.current = datetime(2026, 11, 21, tzinfo=KST)
        with patch.object(self.service, "_analyze") as analyze:
            self.service._automatic()
        analyze.assert_called_once_with(automatic_retry=True)
        self.assertFalse(self.service.store.setting("enabled"))
        self.assertEqual(self.service.store.orders()[0]["filled_quantity"], 2)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.broker.cancellations, [])

    def test_expired_feed_is_disabled_before_scheduler_without_mutating_controls(self):
        self.enable()
        self.current = datetime(2026, 11, 21, tzinfo=KST)
        self.assertFalse(self.service.mirror_snapshot()["orders_enabled"])
        self.assertTrue(self.service.store.setting("enabled"))

    def test_expiry_during_durable_intent_blocks_buy_and_sell_without_unknown_order(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                self.current = datetime(2026, 11, 20, 23, 59, 59, tzinfo=KST)
                reserve = self.service.store.reserve_order
                def cross_boundary(*args):
                    saved = reserve(*args)
                    self.current += timedelta(seconds=1)
                    return saved
                with patch.object(self.service, "_market_open", return_value=True), \
                        patch.object(self.service.store, "reserve_order", side_effect=cross_boundary):
                    order = self.submit(side=side, key=side)
                self.assertEqual(order["status"], "cancelled")
                self.assertTrue(order["submission_skipped"])
                self.assertNotIn("submission_attempted_at", order)
                self.assertIn("대회가 종료", order["error"])
                self.assertFalse(self.service.store.setting("enabled"))
        self.assertEqual(self.broker.submissions, [])

    def test_expired_submit_blocks_before_reserving_even_if_market_is_reported_open(self):
        self.current = datetime(2026, 11, 21, tzinfo=KST)
        self.enable()
        with patch.object(self.service, "_market_open", return_value=True):
            self.service._submit(self.service._policy(), self.broker, decision(), "buy", 1, Decimal("10000"), "expired", run_id=None)
        self.assertEqual(self.service.store.orders(), [])
        self.assertEqual(self.broker.submissions, [])
        self.assertFalse(self.service.store.setting("enabled"))

    def test_broker_deadline_rejection_is_unsent_and_disables_engine(self):
        self.current = datetime(2026, 11, 20, 23, 59, 59, tzinfo=KST)
        def delayed_broker(*args, **kwargs):
            self.current += timedelta(seconds=1)
            raise BrokerRejected("KIS 대회가 종료되어 새 주문을 중지했습니다.", request_sent=False)
        with patch.object(self.service, "_market_open", return_value=True), \
                patch.object(self.broker, "submit", side_effect=delayed_broker):
            order = self.submit()
        self.assertEqual(order["status"], "rejected")
        self.assertTrue(order["submission_skipped"])
        self.assertFalse(order["request_sent"])
        self.assertFalse(self.service.store.setting("enabled"))

    def test_policy_requires_exact_keys_ordered_integer_limits_and_existing_strategy(self):
        cases = [{**self.policy, "budget": "NaN"}, {**self.policy, "budget": True},
                 {**self.policy, "budget": "1.5"}, {**self.policy, "budget": "1000000001"},
                 {**self.policy, "order_cap": "60000"}, {**self.policy, "daily_buy_limit": "110000"},
                 {**self.policy, "execution_strategy": "not-registered"}, {**self.policy, "account_id": "missing"},
                 {**self.policy, "extra": 1}, {k: v for k, v in self.policy.items() if k != "budget"}]
        original = self.service._policy()
        for value in cases:
            with self.subTest(value=value), self.assertRaises(KisError):
                self.service._configure(value)
            self.assertEqual(self.service._policy(), original)

    def test_durable_intent_exists_before_broker_call_and_same_intent_never_reissued(self):
        original = self.broker.submit
        def inspect(*args, **kwargs):
            records = ExperimentStore(self.root / "state" / "experiments.sqlite3").orders()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "submitting")
            return original(*args, **kwargs)
        self.broker.submit = inspect
        self.submit()
        self.submit()
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(len(self.service.store.orders()), 1)

    def test_verified_partial_fills_feed_survives_restart_during_user_pause(self):
        self.submit(quantity=5)
        self.service.command({"action": "pause"})
        self.fill(2)
        first = self.service.mirror_snapshot()
        self.assertEqual([row["quantity"] for row in first["events"]], [2])
        self.assertEqual(first["owned_quantities"], {"005930": 2})
        self.assertFalse(first["submission_enabled"])
        self.fill(2)
        self.assertEqual(self.service.mirror_snapshot(after=first["next_cursor"])["events"], [])
        self.fill(5, status="filled")
        restarted = self.make_service()
        following = restarted.mirror_snapshot(after=first["next_cursor"])
        self.assertEqual([row["quantity"] for row in following["events"]], [3])
        self.assertEqual(following["owned_quantities"], {"005930": 5})
        from backend.mirror_plan import plan_weight_changes
        proposal = plan_weight_changes(
            first["events"] + following["events"], source_fingerprint=following["source_fingerprint"],
            owned_quantities=following["owned_quantities"], source_prices={"005930": {"price": "10000", "fresh": True}},
            source_equity="1000000", destination_positions={"005930": {"weight": "0", "sellable_weight": "0"}},
            pending_orders=[], destination_verified=True)
        self.assertEqual((proposal[0]["status"], proposal[0]["side"], proposal[0]["weight"]),
                         ("proposal", "buy", "5.00"))
        self.assertFalse(restarted.store.setting("enabled"))
        self.assertTrue(restarted.store.setting("user_paused"))
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.broker.cancellations, [])

    def test_unverified_broker_fill_never_enters_mirror_feed(self):
        self.submit(quantity=5)
        self.fill(2)
        original = self.service.mirror_snapshot()
        self.broker.remote[0]["symbol"] = "000660"
        with self.assertRaises(KisError):
            self.fill(4)
        after = self.service.mirror_snapshot()
        self.assertEqual(after["events"], original["events"])
        self.assertEqual(after["owned_quantities"], original["owned_quantities"])

    def test_unknown_submission_stays_quarantined_across_reconcile_and_restart(self):
        self.broker.submit_error = BrokerUnknown("response lost")
        order = self.submit()
        self.assertEqual(order["status"], "unknown")
        self.assertTrue(self.service.store.setting("enabled", False))
        self.assertTrue(self.service._blocked())
        self.service._reconcile(self.service._policy(), self.broker)
        self.service = self.make_service()
        self.assertEqual(self.service.store.orders()[0]["status"], "unknown")
        with self.assertRaises(KisError):
            self.service.command({"action": "start"})
        self.service._work("cycle", {})
        self.assertEqual(len(self.broker.submissions), 1)

    def test_crash_between_intent_and_ack_becomes_unknown_and_never_resubmits(self):
        order = self.submit()
        order.update(status="submitting", order_id=None, branch_id=None)
        self.service.store.save_order(order)
        self.service = self.make_service()
        self.assertEqual(self.service.store.orders()[0]["status"], "unknown")
        self.assertTrue(self.service.store.setting("enabled", False))
        self.assertTrue(self.service._blocked())
        self.submit()
        self.assertEqual(len(self.broker.submissions), 1)

    def test_restart_preserves_enabled_and_reconciles_before_any_submission(self):
        self.enable()
        restarted = self.make_service()
        self.assertTrue(restarted.store.setting("enabled", False))
        self.assertTrue(restarted.store.setting("autonomy_status")["recovering"])
        self.assertEqual(self.broker.submissions, [])

    def test_start_busy_does_not_enable_automation(self):
        self.service.worker = Mock()
        self.service.worker.is_alive.return_value = True
        with self.assertRaises(KisError):
            self.service.command({"action": "start"})
        self.assertFalse(self.service.store.setting("enabled", False))

    def test_second_server_recovery_cannot_mutate_active_submission(self):
        order = self.submit()
        order.update(status="submitting", order_id=None, branch_id=None)
        self.service.store.save_order(order)
        with file_lock(self.root / "state" / "runner.lock"):
            with self.assertRaises(KisError):
                self.make_service()
            self.assertEqual(self.service.store.orders()[0]["status"], "submitting")
            self.assertTrue(self.service.store.setting("enabled", False))

    def test_positions_use_cumulative_fills_not_ordered_or_sum_of_updates(self):
        self.submit()
        self.assertEqual(self.service._portfolio(self.service._owned_orders())[0], [])
        self.fill(2)
        self.fill(3)
        positions, _ = self.service._portfolio(self.service._owned_orders())
        self.assertEqual(positions[0]["quantity"], 3)
        self.assertEqual(Decimal(positions[0]["cost"]), Decimal("30000") * (1 + FEE))

    def test_decreasing_cumulative_fill_amount_is_rejected(self):
        self.submit()
        self.fill(2)
        with self.assertRaises(KisError):
            self.fill(2, amount="19999")
        self.assertEqual(self.service.store.orders()[0]["filled_amount"], "20000")

    def test_mismatched_or_decreasing_remote_quantity_does_not_modify_ledger(self):
        self.submit()
        self.fill(2)
        original = self.service.store.orders()[0]
        for change in ({"symbol": "000660"}, {"quantity": 6}, {"filled_quantity": 1}, {"limit_price": "10001"}):
            row = {**self.broker.remote[0], **change}
            with self.subTest(change=change), self.assertRaises(KisError):
                self.service._apply_remote(deepcopy(original), row)
            self.assertEqual(self.service.store.orders()[0], original)

    def test_cancel_unknown_does_not_get_reset_by_open_order_or_retried(self):
        order = self.submit()
        self.broker.cancel_error = BrokerUnknown("response lost")
        with self.assertRaises(KisError):
            self.service._cancel(order["id"], self.broker)
        self.assertEqual(self.service.store.orders()[0]["status"], "cancel_pending")
        self.service._reconcile(self.service._policy(), self.broker)
        self.assertEqual(self.service.store.orders()[0]["status"], "cancel_pending")
        with self.assertRaises(KisError):
            self.service._cancel(order["id"], self.broker)
        self.assertEqual(len(self.broker.cancellations), 1)
        self.broker.remote[0].update(status="cancelled", remaining_quantity=0)
        self.service._reconcile(self.service._policy(), self.broker)
        self.assertEqual(self.service.store.orders()[0]["status"], "cancelled")

    def test_daily_buy_limit_counts_reserved_cash_across_symbols(self):
        self.add_run()
        self.enable()
        self.service._cycle()
        self.assertEqual([(x[0], x[2]) for x in self.broker.submissions], [("005930", 2), ("000660", 2)])
        self.assertLessEqual(sum(Decimal(x[3]) * x[2] * (1 + FEE) for x in self.broker.submissions), Decimal("50000"))
        self.service._cycle()
        self.assertEqual(len(self.broker.submissions), 2)

    def test_experiment_budget_and_account_cash_each_limit_reservations(self):
        self.service._configure({**self.policy, "budget": "30000", "daily_buy_limit": "30000"})
        self.add_run()
        self.enable()
        self.service._cycle()
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.broker.submissions[0][2], 2)

    def test_account_cash_and_buyability_bound_quantity_independently(self):
        self.add_run()
        self.broker.account["cash"] = "25000"
        self.broker.buying["quantity"] = 1
        self.enable()
        self.service._cycle()
        self.assertEqual([x[2] for x in self.broker.submissions], [1, 1])

    def test_manual_positions_are_not_added_to_or_sold(self):
        self.broker.account["holdings"]["005930"] = {"quantity": 100, "sellable_quantity": 100}
        self.add_run(symbols=("005930",))
        self.enable()
        self.service._cycle()
        self.assertEqual(self.broker.submissions, [])
        self.assertEqual(self.service._portfolio(self.service._owned_orders())[0], [])

    def test_stale_current_or_future_session_analyses_are_never_bought(self):
        for as_of, created in (("2026-10-01", "2026-10-02T17:00:00+09:00"),
                              ("2026-10-06", "2026-10-02T17:00:00+09:00"),
                              ("2026-10-02", "2026-10-06T09:00:00+09:00"),
                              ("2026-10-02", "2026-10-07T08:00:00+09:00")):
            self.add_run(as_of=as_of, created=created, run_id=as_of + created)
        self.enable()
        self.service._cycle()
        self.assertEqual(self.broker.submissions, [])

    def test_holiday_stale_future_or_empty_volume_market_data_prevents_buy(self):
        self.add_run(symbols=("005930",))
        normal = self.broker.market_session("005930")
        for changes in ({"session_date": "2026-10-02"}, {"last_trade_at": (NOW - timedelta(seconds=181)).isoformat()},
                        {"last_trade_at": (NOW + timedelta(seconds=1)).isoformat()}, {"volume": 0}):
            self.broker.session_override = {**normal, **changes}
            self.enable()
            self.service._work("cycle", {})
            self.assertTrue(self.service.store.setting("enabled", False))
        self.assertEqual(self.broker.submissions, [])

    def test_excluded_unknown_master_and_quote_block_order(self):
        self.add_run(symbols=("005930",))
        self.master["rows"]["005930"]["managed"] = True
        self.enable()
        self.service._cycle()
        self.assertEqual(self.broker.submissions, [])
        self.master["rows"]["005930"]["managed"] = None
        self.service._cycle()
        self.assertEqual(self.broker.submissions, [])
        self.master["rows"]["005930"]["managed"] = False
        self.broker.eligible = False
        self.service._cycle()
        self.assertFalse(self.service._blocked())
        self.assertEqual(self.broker.submissions, [])

    def test_changed_account_credentials_fail_before_any_broker_order(self):
        self.add_run(symbols=("005930",))
        self.config.write_text(self.config.read_text().replace("12345678", "87654321"), encoding="utf-8")
        self.enable()
        self.service._work("cycle", {})
        self.assertTrue(self.service.store.setting("enabled", False))
        self.assertTrue(self.service._blocked())
        self.assertIn("계좌 연결", self.service.error)
        self.assertEqual(self.broker.submissions, [])

    def test_five_actual_sessions_exit_only_system_owned_quantity(self):
        order = self.submit(quantity=2)
        order.update(order_date="2026-09-28", created_at="2026-09-28T10:00:00+09:00", status="filled",
                     filled_quantity=2, filled_amount="20000", average_price="10000", remaining_quantity=0)
        self.service.store.save_order(order)
        self.broker.account["holdings"]["005930"] = {"quantity": 12, "sellable_quantity": 12}
        self.service._cycle()
        self.assertEqual(self.broker.submissions[-1], ("005930", "sell", 2, "10000"))
        self.assertEqual(len(self.broker.submissions), 2)

    def test_four_sessions_and_manual_sale_shortage_do_not_exit(self):
        order = self.submit(quantity=2)
        order.update(order_date="2026-09-29", created_at="2026-09-29T10:00:00+09:00", status="filled",
                     filled_quantity=2, filled_amount="20000", average_price="10000", remaining_quantity=0)
        self.service.store.save_order(order)
        self.broker.account["holdings"]["005930"] = {"quantity": 2, "sellable_quantity": 2}
        self.service._cycle()
        self.assertEqual(len(self.broker.submissions), 1)
        order["order_date"] = "2026-09-28"
        self.service.store.save_order(order)
        self.broker.account["holdings"]["005930"] = {"quantity": 1, "sellable_quantity": 1}
        with self.assertRaises(KisError):
            self.service._cycle()
        self.assertEqual(len(self.broker.submissions), 1)

    def test_analysis_same_input_is_immutable_and_does_not_reroll_llm(self):
        first = self.service._analyze()
        self.input_data["observed_at"] = "2026-10-02T18:00:00+09:00"
        self.input_data["rows"][0]["selection"]["status_observed_at"] = "2026-10-02T18:00:00+09:00"
        second = self.service._analyze()
        self.assertEqual(first["id"], second["id"])
        self.analyzer.assert_called_once()
        self.assertIs(self.analyzer.call_args.kwargs["llm"], self.llm)

    def test_refresh_timestamps_do_not_reroll_same_daily_analysis(self):
        first = self.service._analyze()
        self.input_data["observed_at"] = "2026-10-02T18:00:00+09:00"
        self.input_data["rows"][0]["selection"]["status_observed_at"] = "2026-10-02T18:00:00+09:00"
        self.input_data["rows"][0]["observed_at"] = "2026-10-02T18:00:00+09:00"
        second = self.service._analyze()
        self.assertEqual(first["id"], second["id"])
        self.analyzer.assert_called_once()

    def test_unchanged_inputs_skip_full_run_reads_and_shadow_work_across_restart(self):
        from backend.experiment_shadow import update_shadow
        self.service.command({"action": "pause"})
        with patch("backend.experiment_shadow.update_shadow", wraps=update_shadow) as update:
            first = self.service._analyze()
            self.input_data["observed_at"] = "2026-10-02T18:00:00+09:00"
            with patch.object(self.service.store, "runs", wraps=self.service.store.runs) as reads:
                self.assertEqual(self.service._analyze()["id"], first["id"])
                reads.assert_not_called()
            resumed = self.make_service()
            with patch.object(resumed.store, "runs", wraps=resumed.store.runs) as reads:
                self.assertEqual(resumed._analyze()["id"], first["id"])
                self.assertTrue(all(call.kwargs.get("details") is False for call in reads.call_args_list))
            update.assert_called_once()
        self.analyzer.assert_called_once()
        self.assertFalse(resumed.store.setting("enabled"))
        self.assertTrue(resumed.store.setting("user_paused"))
        self.assertEqual(self.broker.submissions, [])

    def test_shadow_refreshes_for_revised_prices_new_signals_and_calendar(self):
        from backend.experiment_shadow import update_shadow
        from tests.test_experiment_shadow import fixture
        run, histories, calendars = fixture()
        self.service.store.add_run(run)
        data = {"as_of": "2026-10-13", "histories": histories, "calendars": calendars}
        with patch("backend.experiment_shadow.update_shadow", wraps=update_shadow) as update:
            self.service._refresh_shadow(data)
            before = deepcopy(next(iter(self.service.store.setting("shadow")["records"].values())))
            self.service._refresh_shadow(data)
            update.assert_called_once()
            histories["005930"][date(2026, 10, 5)]["open"] = "100.1"
            self.service._refresh_shadow(data)
            revised = next(iter(self.service.store.setting("shadow")["records"].values()))
            self.assertEqual(revised["reason"], "price_revision")
            self.assertEqual(revised["basis"], before["basis"])
            self.assertEqual(revised["first_closed"], before["first_closed"])
            self.service.store.add_run({**run, "id": "run2", "created_at": "2026-10-02T09:00:00+00:00"})
            self.service._refresh_shadow(data)
            calendars["KOSPI"].remove(date(2026, 10, 5))
            self.service._refresh_shadow(data)
            self.assertEqual(update.call_count, 4)

    def test_shadow_detects_cache_revision_outside_current_candidates(self):
        from backend.candidate_history import HistoryCache
        from backend.experiment_shadow import update_shadow
        from tests.test_experiment_shadow import fixture
        run, histories, calendars = fixture()
        self.service.store.add_run(run)
        cache = HistoryCache(self.root / ".local" / "candidates" / "history")
        bars = {day: {key: Decimal(value) for key, value in bar.items()}
                for day, bar in histories["005930"].items()}
        cache._write(cache._path("stock", "005930"), "stock", "005930", bars)
        data = {"as_of": "2026-10-13", "histories": {}, "calendars": calendars}
        with patch("backend.experiment_shadow.update_shadow", wraps=update_shadow) as update:
            self.service._refresh_shadow(data)
            self.service._refresh_shadow(data)
            update.assert_called_once()
            bars[date(2026, 10, 5)]["open"] = Decimal("100.1")
            cache._write(cache._path("stock", "005930"), "stock", "005930", bars)
            self.service._refresh_shadow(data)
            self.assertEqual(update.call_count, 2)
        record = next(iter(self.service.store.setting("shadow")["records"].values()))
        self.assertEqual(record["reason"], "price_revision")

    def test_failed_shadow_update_is_retried_with_identical_input(self):
        with patch("backend.experiment_shadow.update_shadow", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.service._analyze()
        self.assertIsNone(self.service.store.setting("shadow_refresh_key"))
        self.service._analyze()
        self.analyzer.assert_called_once()
        self.assertIsNotNone(self.service.store.setting("shadow_refresh_key"))

    def test_dashboard_and_order_cycle_read_only_run_metadata(self):
        self.add_run(symbols=("005930",))
        self.enable()
        with patch.object(self.service.store, "runs", wraps=self.service.store.runs) as reads:
            self.assertNotIn("analysis", self.service.snapshot()["runs"][0])
            self.service._cycle()
            self.assertTrue(reads.called)
            self.assertTrue(all(call.kwargs.get("details") is False for call in reads.call_args_list))
        self.assertEqual(len(self.broker.submissions), 1)

    def test_changed_benchmark_calendar_or_evidence_produces_new_frozen_run(self):
        first = self.service._analyze()
        self.input_data["benchmarks"]["KOSPI"]["2026-10-02"] = "7000"
        second = self.service._analyze()
        self.input_data["calendars"]["KOSPI"] = ["2026-10-02"]
        third = self.service._analyze()
        self.input_data["evidence"] = [{"id": "public1", "symbol": "005930", "title": "new evidence",
            "text": "public evidence", "url": "https://example.org/source", "published_at": "2026-10-02T14:00:00+09:00",
            "observed_at": "2026-10-02T16:00:00+09:00"}]
        fourth = self.service._analyze()
        self.assertEqual(len({item["id"] for item in (first, second, third, fourth)}), 4)
        self.assertEqual(self.analyzer.call_count, 4)

    def test_changed_analysis_version_cannot_submit_old_frozen_signals(self):
        self.add_run(symbols=("005930",))
        self.enable()
        with patch("backend.experiment_analysis.analysis_version", return_value="changed-model-or-prompt"):
            self.service._work("cycle", {})
        self.assertEqual(self.broker.submissions, [])
        self.assertTrue(self.service.store.setting("enabled", False))
        self.assertTrue(any(item["code"] == "analysis_version" for item in self.service.store.setting("issues")))

    def test_failed_llm_manual_retry_preserves_original_and_success_never_rerolls(self):
        self.analyzer.return_value["llm_status"] = {"status": "error", "error": "provider unavailable"}
        first = self.service._analyze()
        self.assertEqual(self.service._analyze()["id"], first["id"])
        self.assertEqual(self.analyzer.call_count, 1)
        self.current += timedelta(seconds=1)
        self.analyzer.return_value["llm_status"] = {"status": "ready", "error": None}
        second = self.service._analyze(retry_llm=True)
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(self.service._analyze(retry_llm=True)["id"], second["id"])
        self.assertEqual(self.analyzer.call_count, 2)
        saved = {item["id"]: item for item in self.service.store.runs()}
        self.assertEqual(saved[first["id"]]["llm_status"]["status"], "error")
        self.assertEqual(saved[second["id"]]["llm_status"]["status"], "ready")

    def test_partial_fill_then_confirmed_cancel_retains_only_filled_position(self):
        order = self.submit()
        self.fill(2)
        self.broker.remote[0].update(status="cancelled", remaining_quantity=0)
        self.service._reconcile(self.service._policy(), self.broker)
        saved = self.service.store.orders()[0]
        self.assertEqual((saved["status"], saved["filled_quantity"], saved["remaining_quantity"]), ("cancelled", 2, 0))
        positions, metrics = self.service._portfolio(self.service._owned_orders())
        self.assertEqual(positions[0]["quantity"], 2)
        self.assertEqual(Decimal(positions[0]["cost"]), Decimal("20000") * (1 + FEE))
        self.assertEqual(metrics[0]["closed_trades"], 0)
        with self.assertRaises(KisError):
            self.service._cancel(order["id"], self.broker)
        self.assertEqual(self.broker.cancellations, [])

    def test_pause_during_submission_allows_recording_ack_but_prevents_next_symbol(self):
        self.add_run()
        self.enable()
        original = self.broker.submit
        def pause_during(*args, **kwargs):
            result = original(*args, **kwargs)
            self.service._pause("사용자 중지")
            return result
        self.broker.submit = pause_during
        self.service._cycle()
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.service.store.orders()[0]["status"], "submitted")
        self.assertFalse(self.service.store.setting("enabled", True))

    @staticmethod
    def transport_ack():
        response = io.StringIO(json.dumps({"rt_cd": "0", "output": {
            "ODNO": "123", "KRX_FWDG_ORD_ORGNO": "12345", "ORD_TMD": "100000"}}))
        response.headers = {}
        return response

    def test_transport_waits_recheck_pause_shutdown_market_and_deadline_for_both_sides(self):
        from backend.kis import PaperClient
        from backend.paper_broker import PaperBroker
        for stage in ("token", "request_guard", "request_spacing"):
            for reason in ("pause", "shutdown", "market", "deadline"):
                for side in ("buy", "sell"):
                    with self.subTest(stage=stage, reason=reason, side=side):
                        self.current = NOW
                        self.service.stopping = False
                        self.enable()
                        policy = self.service._policy()
                        profile = self.service._profile(policy)
                        previous_ids = {item["id"] for item in self.service.store.orders()}

                        def invalidate():
                            if reason == "pause":
                                self.service._pause("사용자 중지")
                            elif reason == "shutdown":
                                self.service.close()
                            elif reason == "market":
                                self.current = self.current.replace(hour=15, minute=15)
                            else:
                                self.current = datetime(2026, 11, 21, tzinfo=KST)

                        @contextmanager
                        def wait_at(name):
                            if name == stage:
                                invalidate()
                            yield

                        def token():
                            if stage == "token":
                                invalidate()
                            return "test-token"

                        client = PaperClient(profile.settings, self.root / "test-token.json",
                                             request_guard=lambda: wait_at("request_guard"))
                        broker = PaperBroker(profile, client=client, now=lambda: self.current)
                        with patch.object(client, "token", side_effect=token), \
                                patch("backend.kis.request_spacing", side_effect=lambda _: wait_at("request_spacing")), \
                                patch("backend.kis.urlopen") as opened:
                            self.service._submit(policy, broker, decision(), side, 1, Decimal("10000"),
                                                 f"{stage}:{reason}:{side}", run_id=None)
                        opened.assert_not_called()
                        created = [item for item in self.service.store.orders() if item["id"] not in previous_ids]
                        self.assertEqual(len(created), 1)
                        order = created[0]
                        self.assertEqual(order["status"], "cancelled")
                        self.assertTrue(order["submission_skipped"])
                        self.assertFalse(order["request_sent"])
                        self.assertNotIn("submission_attempted_at", order)
                        self.assertFalse(self.service.store.setting("issues", []))
        self.service.stopping = False

    def test_pause_from_another_instance_cannot_commit_between_final_check_and_http_entry(self):
        from backend.kis import PaperClient
        from backend.paper_broker import PaperBroker
        other = self.make_service()
        self.enable()
        policy = self.service._policy()
        profile = self.service._profile(policy)
        client = PaperClient(profile.settings, self.root / "test-token.json")
        broker = PaperBroker(profile, client=client, now=lambda: self.current)
        checked, allow_http, entered_http, finish_http = (threading.Event() for _ in range(4))
        pause_attempted, paused = threading.Event(), threading.Event()
        errors, checks = [], []
        original_check = broker._require_competition_open

        def final_check():
            original_check()
            checks.append(True)
            if len(checks) == 2:
                checked.set()
                if not allow_http.wait(5):
                    raise AssertionError("HTTP entry not released")

        @contextmanager
        def observed_lock(path, **kwargs):
            if threading.current_thread().name == "test-pause" and path.name == "submission.lock":
                pause_attempted.set()
            with file_lock(path, **kwargs) as handle:
                yield handle

        def http(*args, **kwargs):
            self.assertTrue(self.service.store.setting("enabled"))
            entered_http.set()
            if not finish_http.wait(5):
                raise AssertionError("HTTP response not released")
            return self.transport_ack()

        def submit():
            try:
                self.service._submit(policy, broker, decision(), "buy", 1, Decimal("10000"),
                                     "in-flight", run_id=None)
            except BaseException as error:
                errors.append(error)

        def pause():
            try:
                other._pause("사용자 중지")
                paused.set()
            except BaseException as error:
                errors.append(error)

        submitting = threading.Thread(target=submit)
        pausing = threading.Thread(target=pause, name="test-pause")
        with patch.object(client, "token", return_value="test-token"), \
                patch("backend.kis.request_spacing", side_effect=lambda _: nullcontext()), \
                patch("backend.experiments.file_lock", side_effect=observed_lock), \
                patch.object(broker, "_require_competition_open", side_effect=final_check), \
                patch("backend.kis.urlopen", side_effect=http) as opened:
            try:
                submitting.start()
                self.assertTrue(checked.wait(5))
                pausing.start()
                self.assertTrue(pause_attempted.wait(5))
                self.assertFalse(paused.wait(0.05))
                allow_http.set()
                self.assertTrue(entered_http.wait(5))
                self.assertFalse(paused.wait(0.05))
            finally:
                allow_http.set()
                finish_http.set()
                submitting.join(5)
                if pausing.ident is not None:
                    pausing.join(5)
            self.assertFalse(submitting.is_alive())
            self.assertFalse(pausing.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(paused.is_set())
            self.assertFalse(self.service.store.setting("enabled"))
            self.assertEqual(self.service.store.orders()[0]["status"], "submitted")
            self.service._submit(policy, broker, decision(), "buy", 1, Decimal("10000"),
                                 "after-pause", run_id=None)
            self.assertEqual(len(self.service.store.orders()), 1)
            opened.assert_called_once()

    def test_transport_unknown_is_not_retried_after_user_pause(self):
        from backend.kis import PaperClient
        from backend.paper_broker import PaperBroker
        self.enable()
        policy = self.service._policy()
        profile = self.service._profile(policy)
        client = PaperClient(profile.settings, self.root / "test-token.json")
        broker = PaperBroker(profile, client=client, now=lambda: self.current)
        with patch.object(client, "token", return_value="test-token"), \
                patch("backend.kis.request_spacing", side_effect=lambda _: nullcontext()), \
                patch("backend.kis.urlopen", side_effect=TimeoutError()) as opened:
            self.service._submit(policy, broker, decision(), "buy", 1, Decimal("10000"), "unknown", run_id=None)
            self.service._pause("사용자 중지")
            self.service._submit(policy, broker, decision(), "buy", 1, Decimal("10000"), "unknown", run_id=None)
            self.assertEqual(self.service.store.orders()[0]["status"], "unknown")
            opened.assert_called_once()

    def test_deadline_guard_releases_submission_lock_before_waiting_for_service_lock(self):
        from backend.competition_deadline import ended
        from backend.kis import PaperClient
        from backend.paper_broker import PaperBroker
        self.enable()
        policy = self.service._policy()
        profile = self.service._profile(policy)
        client = PaperClient(profile.settings, self.root / "test-token.json")
        broker = PaperBroker(profile, client=client, now=lambda: self.current)
        deadline_observed = threading.Event()
        errors = []

        def token():
            self.current = datetime(2026, 11, 21, tzinfo=KST)
            return "test-token"

        def observe_deadline(*args):
            expired = ended(*args)
            if expired:
                deadline_observed.set()
            return expired

        @contextmanager
        def bounded_lock(path, **kwargs):
            kwargs.setdefault("timeout", 1)
            with file_lock(path, **kwargs) as handle:
                yield handle

        def submit():
            try:
                self.service._submit(policy, broker, decision(), "buy", 1, Decimal("10000"),
                                     "deadline-pause", run_id=None)
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=submit)
        with patch.object(client, "token", side_effect=token), \
                patch("backend.kis.request_spacing", side_effect=lambda _: nullcontext()), \
                patch("backend.experiments.file_lock", side_effect=bounded_lock), \
                patch("backend.experiments.ended", side_effect=observe_deadline), \
                patch("backend.kis.urlopen") as opened:
            try:
                with self.service.lock:
                    worker.start()
                    self.assertTrue(deadline_observed.wait(5))
                    self.service._pause("사용자 중지")
            finally:
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            opened.assert_not_called()
        self.assertEqual(self.service.store.setting("pause_reason"), "사용자 중지")
        order = self.service.store.orders()[0]
        self.assertEqual(order["status"], "cancelled")
        self.assertFalse(order["request_sent"])

    def test_submission_lock_entry_failure_is_unsent_without_retry(self):
        from backend.kis import PaperClient
        from backend.paper_broker import PaperBroker
        self.enable()
        policy = self.service._policy()
        profile = self.service._profile(policy)
        client = PaperClient(profile.settings, self.root / "test-token.json")
        broker = PaperBroker(profile, client=client, now=lambda: self.current)
        for error in (TimeoutError("busy"), OSError("unavailable")):
            with self.subTest(error=type(error).__name__):
                previous_ids = {item["id"] for item in self.service.store.orders()}
                with patch.object(client, "token", return_value="test-token"), \
                        patch("backend.kis.request_spacing", side_effect=lambda _: nullcontext()), \
                        patch("backend.experiments.file_lock", side_effect=error), \
                        patch("backend.kis.urlopen") as opened:
                    for _ in range(2):
                        self.service._submit(policy, broker, decision(), "buy", 1, Decimal("10000"),
                                             "lock-" + type(error).__name__, run_id=None)
                    opened.assert_not_called()
                created = [item for item in self.service.store.orders() if item["id"] not in previous_ids]
                self.assertEqual(len(created), 1)
                self.assertEqual(created[0]["status"], "rejected")
                self.assertFalse(created[0]["request_sent"])
                self.assertTrue(created[0]["submission_skipped"])

    def test_pause_then_resume_does_not_revive_an_order_waiting_for_authentication(self):
        from backend.kis import PaperClient
        from backend.paper_broker import PaperBroker
        policy = self.service._policy()
        profile = self.service._profile(policy)
        client = PaperClient(profile.settings, self.root / "test-token.json")
        broker = PaperBroker(profile, client=client, now=lambda: self.current)
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                self.enable()
                before = self.service.store.setting("pause_epoch", 0)
                previous_ids = {item["id"] for item in self.service.store.orders()}
                tokens = []

                def token():
                    if not tokens:
                        self.service._pause("사용자 중지")
                        # Simulate a later authorized resume while old work still
                        # holds its captured epoch. New work uses the new epoch.
                        self.service.store.save_settings({"enabled": True, "user_paused": False, "pause_reason": None})
                    tokens.append(True)
                    return "test-token"

                with patch.object(client, "token", side_effect=token), \
                        patch("backend.kis.request_spacing", side_effect=lambda _: nullcontext()), \
                        patch("backend.kis.urlopen", side_effect=lambda *a, **kw: self.transport_ack()) as opened:
                    self.service._submit(policy, broker, decision(), side, 1, Decimal("10000"),
                                         side + "-before-pause", run_id=None)
                    opened.assert_not_called()
                    cancelled = [item for item in self.service.store.orders() if item["id"] not in previous_ids]
                    self.assertEqual(len(cancelled), 1)
                    self.assertEqual(cancelled[0]["status"], "cancelled")
                    self.assertEqual(cancelled[0]["pause_epoch"], before)
                    self.assertFalse(cancelled[0]["request_sent"])
                    self.assertEqual(self.service.store.setting("pause_epoch"), before + 1)
                    self.assertTrue(self.service.store.setting("enabled"))
                    self.assertFalse(self.service.store.setting("issues", []))
                    self.service._submit(policy, broker, decision(), side, 1, Decimal("10000"),
                                         side + "-after-resume", run_id=None)
                    opened.assert_called_once()
                submitted = [item for item in self.service.store.orders()
                             if item["id"] not in previous_ids and item["status"] == "submitted"]
                self.assertEqual(len(submitted), 1)
                self.assertEqual(submitted[0]["pause_epoch"], before + 1)

    def test_waiting_pause_cannot_be_overtaken_by_more_submissions(self):
        from backend.kis import PaperClient
        from backend.paper_broker import PaperBroker
        self.enable()
        policy = self.service._policy()
        profile = self.service._profile(policy)
        client = PaperClient(profile.settings, self.root / "test-token.json")
        broker = PaperBroker(profile, client=client, now=lambda: self.current)
        waiting, release = threading.Event(), threading.Event()
        errors = []

        @contextmanager
        def defer_pause(path, **kwargs):
            if threading.current_thread().name == "waiting-pause" and path.name == "submission.lock":
                waiting.set()
                if not release.wait(5):
                    raise AssertionError("Pause was not released")
            with file_lock(path, **kwargs) as handle:
                yield handle

        def pause():
            try:
                self.service._pause("사용자 중지")
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=pause, name="waiting-pause")
        with patch.object(client, "token", return_value="test-token"), \
                patch("backend.experiments.file_lock", side_effect=defer_pause), \
                patch("backend.kis.request_spacing", side_effect=lambda _: nullcontext()), \
                patch("backend.kis.urlopen") as opened:
            try:
                worker.start()
                self.assertTrue(waiting.wait(5))
                self.assertTrue(self.service.store.setting("enabled"))
                for index in range(2):
                    self.service._submit(policy, broker, decision(), "buy", 1, Decimal("10000"),
                                         f"overtaking-{index}", run_id=None)
                opened.assert_not_called()
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([item["status"] for item in self.service.store.orders()], ["cancelled", "cancelled"])
        self.assertFalse(self.service.store.setting("issues", []))
        self.assertEqual(self.service.store.setting("pause_epoch"), 1)
        self.assertEqual(self.service._pause_requests, 0)

    def test_broker_deadline_after_service_guard_is_a_normal_unsent_stop(self):
        from backend.kis import PaperClient
        from backend.paper_broker import PaperBroker
        self.enable()
        policy = self.service._policy()
        profile = self.service._profile(policy)
        client = PaperClient(profile.settings, self.root / "test-token.json")
        broker = PaperBroker(profile, client=client, now=lambda: self.current)
        checks = []
        original = broker._require_competition_open

        def cross_deadline():
            if checks:
                self.current = datetime(2026, 11, 21, tzinfo=KST)
            checks.append(True)
            original()

        with patch.object(client, "token", return_value="test-token"), \
                patch.object(broker, "_require_competition_open", side_effect=cross_deadline), \
                patch("backend.kis.request_spacing", side_effect=lambda _: nullcontext()), \
                patch("backend.kis.urlopen") as opened:
            self.service._submit(policy, broker, decision(), "sell", 1, Decimal("10000"), "late-deadline", run_id=None)
            opened.assert_not_called()
        order = self.service.store.orders()[0]
        self.assertEqual(order["status"], "cancelled")
        self.assertFalse(order["request_sent"])
        self.assertFalse(self.service.store.setting("issues", []))

    def test_close_reports_submission_lock_failure_with_bounded_wait(self):
        for error in (TimeoutError("busy"), OSError("unavailable")):
            with self.subTest(error=type(error).__name__), \
                    patch("backend.experiments.file_lock", side_effect=error) as locked, \
                    patch.object(self.service.learning, "close") as close_learning:
                self.assertFalse(self.service.close())
                locked.assert_called_once_with(self.service.directory / "submission.lock", timeout=20)
                close_learning.assert_not_called()

    def test_accepted_ack_database_failure_preserves_intent_for_unknown_recovery(self):
        self.enable()
        with patch.object(self.service.store, "save_order", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.submit()
        saved = self.service.store.orders()[0]
        self.assertEqual(saved["status"], "submitting")
        self.assertIsNone(saved["order_id"])
        self.assertEqual(len(self.broker.submissions), 1)
        self.service = self.make_service()
        self.assertEqual(self.service.store.orders()[0]["status"], "unknown")
        self.assertTrue(self.service.store.setting("enabled", False))
        self.assertTrue(self.service._blocked())
        self.service._work("cycle", {})
        self.assertEqual(len(self.broker.submissions), 1)

    def test_configure_while_worker_is_blocked_leaves_policy_unchanged(self):
        self.add_run()
        self.enable()
        entered, release = threading.Event(), threading.Event()
        original = self.broker.snapshot
        def slow_snapshot():
            entered.set()
            release.wait(5)
            return original()
        self.broker.snapshot = slow_snapshot
        self.service._launch("cycle")
        try:
            self.assertTrue(entered.wait(5))
            previous = self.service._policy()
            with self.assertRaises((BlockingIOError, KisError)):
                self.service.command({"action": "configure", "policy": {**self.policy, "budget": "200000"}})
            self.assertEqual(self.service._policy(), previous)
            self.service._pause("테스트 중지")
        finally:
            release.set()
            self.service.worker.join(5)
        self.assertFalse(self.service.worker.is_alive())

    def test_manual_sale_of_owned_shares_blocks_new_buys_before_exit_due(self):
        order = self.submit(quantity=2)
        order.update(order_date="2026-10-02", created_at="2026-10-02T10:00:00+09:00", status="filled",
                     filled_quantity=2, filled_amount="20000", average_price="10000", remaining_quantity=0)
        self.service.store.save_order(order)
        self.broker.account["holdings"]["005930"] = {"quantity": 1, "sellable_quantity": 1}
        self.add_run(symbols=("000660",))
        self.service._work("cycle", {})
        self.assertTrue(self.service.store.setting("enabled", False))
        self.assertTrue(self.service._blocked())
        self.assertEqual(len(self.broker.submissions), 1)

    def test_configure_is_blocked_while_runner_lock_is_owned_by_another_process(self):
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        process = context.Process(target=hold_lock, args=(str(self.root / "state" / "runner.lock"), ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(5))
            previous = self.service._policy()
            with self.assertRaises((BlockingIOError, KisError)):
                self.service.command({"action": "configure", "policy": {**self.policy, "budget": "200000"}})
            self.assertEqual(self.service._policy(), previous)
            self.service._work("cycle", {})
            self.assertEqual(self.broker.submissions, [])
            self.assertIn("다른 서버", self.service.error)
        finally:
            release.set()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)

    def autonomous_service(self, *, empty=True):
        self.config.write_text(self.config.read_text(encoding="utf-8") + "\n[experiments]\nautonomous=true\n", encoding="utf-8")
        if empty:
            self.service.store.save_setting("policy", {})
        return self.make_service()

    def test_autonomous_bootstraps_actual_equity_once_and_preserves_manual_policy(self):
        original = self.service._policy()
        automatic = self.autonomous_service(empty=False)
        self.assertEqual(automatic._initialize_policy(), original)
        automatic.store.save_setting("policy", {})
        self.broker.account["total_value"] = "1E+8"
        policy = automatic._initialize_policy()
        self.assertEqual(policy["execution_strategy"], ALL_STRATEGIES)
        self.assertEqual(policy["budget"], "100000000")
        self.assertEqual(policy["daily_buy_limit"], "16666666")
        self.assertEqual(policy["order_cap"], "4166666")
        self.broker.account["total_value"] = "200000000"
        self.assertEqual(automatic._initialize_policy(), policy)
        self.assertTrue(automatic.store.setting("enabled"))

    def test_user_pause_is_preserved_across_close_and_autonomous_restart(self):
        automatic = self.autonomous_service()
        automatic._initialize_policy()
        automatic.command({"action": "pause"})
        self.assertTrue(automatic.close())
        resumed = self.make_service()
        self.assertFalse(resumed.store.setting("enabled"))
        self.assertTrue(resumed.store.setting("user_paused"))
        self.assertFalse(resumed.snapshot()["automation"]["enabled"])

    def test_normal_close_retains_intent_and_prevents_inflight_new_submissions(self):
        self.enable()
        self.assertTrue(self.service.close())
        self.assertTrue(self.service.store.setting("enabled"))
        self.service._submit(self.service._policy(), self.broker, decision(), "buy", 2, Decimal("10000"), "after-close", run_id="r")
        self.assertEqual(self.broker.submissions, [])
        self.assertTrue(self.make_service().store.setting("enabled"))

    def test_transient_failure_backoff_survives_restart_and_recovers_automatically(self):
        self.add_run(symbols=("005930",))
        self.enable()
        original = self.broker.snapshot
        self.broker.snapshot = Mock(side_effect=KisError("일시 조회 실패"))
        self.service._work("cycle", {})
        first = self.service.store.setting("autonomy_status")
        self.assertTrue(self.service.store.setting("enabled"))
        self.assertEqual(first["consecutive_failures"], 1)
        self.assertFalse(self.service._retry_due())
        restarted = self.make_service()
        self.assertFalse(restarted._retry_due())
        self.assertEqual(restarted.snapshot()["status"], "error")
        self.assertEqual(restarted.snapshot()["error"], "일시 조회 실패")
        self.assertEqual(restarted.store.setting("autonomy_status")["last_error_operation"], "cycle")
        with patch.object(restarted, "_launch") as launch:
            restarted.tick()
            launch.assert_not_called()
        self.current += timedelta(seconds=31)
        self.broker.snapshot = original
        restarted._work("cycle", {})
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(restarted.store.setting("autonomy_status")["consecutive_failures"], 0)
        self.assertIsNone(restarted.store.setting("autonomy_status")["next_retry_at"])
        self.assertTrue(all(item["state"] == "resolved" for item in restarted.store.setting("issues")))

    def test_backoff_is_capped_without_pausing_user_intent(self):
        self.enable()
        for _ in range(12):
            self.service._failure(KisError("일시 연결 오류"), "cycle")
        health = self.service.store.setting("autonomy_status")
        self.assertEqual((datetime.fromisoformat(health["next_retry_at"]) - self.current).total_seconds(), 900)
        self.assertTrue(self.service.store.setting("enabled"))
        self.assertFalse(self.service._blocked())

    def test_temporary_failure_only_asks_after_thirty_minutes_and_resolves(self):
        self.service._failure(KisError("연결 실패"), "cycle")
        issue = self.service.store.setting("issues")[0]
        self.assertIsNone(issue["question"])
        self.current += timedelta(minutes=30)
        self.service._failure(KisError("연결 실패"), "cycle")
        issue = self.service.store.setting("issues")[0]
        self.assertTrue(issue["question"])
        self.assertFalse(issue["blocking"])
        health = self.service.store.setting("autonomy_status")
        self.service._work("analyze", {})
        after_analysis = self.service.store.setting("autonomy_status")
        for key in ("next_retry_at", "consecutive_failures", "last_error", "last_error_operation"):
            self.assertEqual(after_analysis[key], health[key])
        self.assertIsNone(after_analysis.get("last_cycle_at"))
        self.assertEqual(self.service.snapshot()["status"], "error")
        self.service._work("cycle", {})
        self.assertEqual(next(item for item in self.service.store.setting("issues")
                              if item["id"] == "temporary_failure")["state"], "resolved")

    def test_automatic_records_cycle_failure_once_despite_successful_analysis(self):
        self.enable()
        with patch.object(self.service, "_cycle", side_effect=KisError("체결 조회 실패")), \
                patch.object(self.service, "_analyze") as analyze:
            self.service._work("automatic", {})
        analyze.assert_called_once_with(automatic_retry=True)
        health = self.service.store.setting("autonomy_status")
        self.assertEqual(health["last_error_operation"], "cycle")
        self.assertEqual(health["consecutive_failures"], 1)
        self.assertFalse(self.service._retry_due())
        self.assertIsNone(health.get("last_cycle_at"))
        self.assertEqual(self.service.snapshot()["error"], "체결 조회 실패")

    def test_successful_automatic_without_cycle_preserves_trading_failure(self):
        self.service._failure(KisError("거래 조회 실패"), "cycle")
        before = self.service.store.setting("autonomy_status")
        with patch.object(self.service, "_cycle") as cycle, patch.object(self.service, "_analyze"):
            self.service._work("automatic", {})
        cycle.assert_not_called()
        health = self.service.store.setting("autonomy_status")
        self.assertEqual(health["next_retry_at"], before["next_retry_at"])
        self.assertEqual(health["last_error_operation"], "cycle")
        self.assertIsNone(health.get("last_cycle_at"))

    def test_independent_operation_failures_require_each_operation_to_recover(self):
        self.enable()
        with patch.object(self.service, "_cycle", side_effect=KisError("체결 조회 실패")), \
                patch.object(self.service, "_analyze", side_effect=KisError("분석 실패")):
            self.service._work("automatic", {})
        health = self.service.store.setting("autonomy_status")
        self.assertEqual(set(health["operation_failures"]), {"cycle", "analyze"})
        with patch.object(self.service, "_analyze"):
            self.service._work("analyze", {})
        health = self.service.store.setting("autonomy_status")
        self.assertEqual(health["last_error_operation"], "cycle")
        self.assertEqual(health["last_error"], "체결 조회 실패")
        issue = next(item for item in self.service.store.setting("issues") if item["id"] == "temporary_failure")
        self.assertEqual(issue["message"], "체결 조회 실패")
        self.assertEqual(issue["state"], "open")
        with patch.object(self.service, "_cycle"):
            self.service._work("cycle", {})
        health = self.service.store.setting("autonomy_status")
        self.assertEqual(health["operation_failures"], {})
        self.assertIsNone(health["last_error"])
        self.assertIsNone(health["next_retry_at"])

    def test_analysis_failure_does_not_erase_completed_cycle_timestamp(self):
        self.enable()
        with patch.object(self.service, "_cycle"), \
                patch.object(self.service, "_analyze", side_effect=KisError("분석 실패")):
            self.service._work("automatic", {})
        completed = self.service.store.setting("autonomy_status")["last_cycle_at"]
        self.assertEqual(completed, self.service._timestamp())
        self.current += timedelta(minutes=1)
        with patch.object(self.service, "_analyze"):
            self.service._work("analyze", {})
        health = self.service.store.setting("autonomy_status")
        self.assertEqual(health["last_cycle_at"], completed)
        self.assertNotEqual(health["last_success_at"], completed)
        self.assertIsNone(health["last_error"])

    def test_safe_request_diagnostics_are_durable_and_cleared_only_on_recovery(self):
        error = KisRequestError("KIS 요청 실패", "/uapi/domestic-stock/v1/trading/inquire-balance",
                                http_status=500, message_code="EGW00201")
        self.service._failure(error, "cycle")
        restarted = self.make_service()
        health = restarted.snapshot()["autonomy"]
        self.assertEqual(health["last_error_request"], {
            "request_kind": "balance", "http_status": 500, "message_code": "EGW00201",
            "occurred_at": error.occurred_at, "attempts": 1})
        with patch.object(restarted, "_analyze"):
            restarted._work("analyze", {})
        self.assertEqual(restarted.snapshot()["autonomy"]["last_error_request"], health["last_error_request"])
        with patch.object(restarted, "_cycle"):
            restarted._work("cycle", {})
        self.assertIsNone(restarted.snapshot()["autonomy"]["last_error_request"])

    def test_legacy_failure_is_not_inferred_recovered_from_analysis(self):
        self.service.store.save_setting("autonomy_status", {
            "last_error": "과거 요청 실패", "last_error_at": self.service._timestamp(),
            "consecutive_failures": 2, "next_retry_at": (self.current + timedelta(minutes=1)).isoformat()})
        restarted = self.make_service()
        with patch.object(restarted, "_analyze"):
            restarted._work("analyze", {})
        self.assertEqual(restarted.snapshot()["error"], "과거 요청 실패")
        restarted.store.save_setting("enabled", True)
        with patch.object(restarted, "_cycle"), patch.object(restarted, "_analyze"):
            restarted._work("automatic", {})
        self.assertIsNone(restarted.snapshot()["error"])

    def test_cancel_failure_clears_after_reconciliation_with_unknown_order_still_protected(self):
        order = self.submit()
        self.broker.cancel_error = BrokerUnknown("응답 없음")
        self.service._work("cancel", {"order_id": order["id"]})
        self.service.store.save_setting("enabled", False)
        self.service._work("automatic", {})
        self.assertIsNone(self.service.snapshot()["error"])
        self.assertEqual(self.service.store.orders()[0]["status"], "cancel_pending")
        self.assertTrue(self.service._blocked())
        self.assertEqual(len(self.broker.cancellations), 1)
        self.broker.remote[0].update(status="cancelled", remaining_quantity=0)
        self.service._work("automatic", {})
        self.assertEqual(self.service.store.orders()[0]["status"], "cancelled")
        self.assertFalse(self.service._blocked())
        self.assertEqual(len(self.broker.cancellations), 1)

    def test_paused_cancel_race_does_not_leave_permanent_error_without_active_orders(self):
        order = self.submit()
        self.service.store.save_setting("enabled", False)
        self.broker.remote[0].update(status="cancelled", remaining_quantity=0)
        self.service._work("cancel", {"order_id": order["id"]})
        self.assertEqual(self.service.snapshot()["status"], "error")
        self.assertEqual(self.service.store.orders()[0]["status"], "cancelled")
        self.service._work("automatic", {})
        self.assertIsNone(self.service.snapshot()["error"])
        self.assertEqual(self.broker.cancellations, [])
        self.assertFalse(self.service.store.setting("enabled"))

    def test_expired_intraday_quote_failure_is_retired_without_quote_or_cycle_success(self):
        self.service._failure(KisError("관측 조회 실패"), "learning_quotes")
        self.current = self.current.replace(hour=18)
        with patch.object(self.service, "_observe_learning_quotes") as quotes:
            self.service._work("automatic", {})
        quotes.assert_not_called()
        health = self.service.store.setting("autonomy_status")
        self.assertIsNone(health["last_error"])
        self.assertIsNone(health.get("last_cycle_at"))
        self.assertEqual(health["last_success_operation"], "analyze")

    def test_unexpected_outer_automatic_failure_is_durable(self):
        with patch.object(self.service, "_automatic", side_effect=ValueError("private")):
            self.service._work("automatic", {})
        self.assertEqual(self.service.store.setting("autonomy_status")["last_error_operation"], "automatic")
        self.assertNotIn("private", self.make_service().snapshot()["error"])

    def test_missing_preopen_analysis_is_visible_and_resolves_when_available(self):
        self.enable()
        self.service._cycle()
        issue = next(item for item in self.service.store.setting("issues") if item["id"] == "analysis_missing")
        self.assertEqual(issue["state"], "open")
        self.assertFalse(issue["blocking"])
        self.add_run(symbols=("005930",))
        self.service._cycle()
        issue = next(item for item in self.service.store.setting("issues") if item["id"] == "analysis_missing")
        self.assertEqual(issue["state"], "resolved")
        self.assertEqual(len(self.broker.submissions), 1)

    def test_llm_auto_retry_is_backed_off_and_success_never_rerolls(self):
        self.analyzer.return_value["llm_status"] = {"status": "error", "error": "timeout", "error_code": "codex_timeout"}
        first = self.service._analyze(automatic_retry=True)
        self.assertEqual(self.service._analyze(automatic_retry=True)["id"], first["id"])
        self.assertEqual(self.analyzer.call_count, 1)
        self.current += timedelta(seconds=61)
        self.analyzer.return_value["llm_status"] = {"status": "ready", "error": None}
        self.service._analyze(automatic_retry=True)
        self.service._analyze(automatic_retry=True)
        self.assertEqual(self.analyzer.call_count, 2)
        self.assertEqual(len(self.service.store.runs()), 2)
        self.assertEqual(self.service.store.setting("analysis_attempt")["state"], "complete")
        self.assertEqual(self.service.store.setting("llm_retry"), {})

    def test_llm_auth_and_quota_have_distinct_retry_and_questions(self):
        for code, delay, question in (("codex_authentication", 21600, True), ("codex_usage_limit", 1800, False)):
            with self.subTest(code=code):
                self.service.store.save_setting("llm_retry", {})
                self.service._llm_retry_state({"collection_hash": "x", "llm_status": {
                    "status": "error", "error": "provider error", "error_code": code}})
                retry = self.service.store.setting("llm_retry")
                self.assertEqual((datetime.fromisoformat(retry["next_retry_at"]) - self.current).total_seconds(), delay)
                issue = next(item for item in self.service.store.setting("issues") if item["id"] == "llm_failure")
                self.assertEqual(bool(issue["question"]), question)
                self.assertFalse(issue["blocking"])

    def test_automatic_analyzes_before_16_even_with_broker_failure_or_user_pause(self):
        self.current = NOW.replace(hour=7)
        self.service._automatic()
        self.analyzer.assert_called_once()
        self.enable()
        with patch.object(self.service, "_cycle", side_effect=KisError("조회 실패")), patch.object(self.service, "_analyze") as analyze:
            with self.assertRaises(KisError):
                self.service._automatic()
            analyze.assert_called_once_with(automatic_retry=True)

    def test_interrupted_analysis_retries_without_creating_an_executable_partial_run(self):
        self.analyzer.side_effect = RuntimeError("process interrupted")
        with self.assertRaises(RuntimeError):
            self.service._analyze()
        self.assertEqual(self.service.store.setting("analysis_attempt")["state"], "running")
        self.assertEqual(self.service.store.runs(), [])
        self.analyzer.side_effect = None
        resumed = self.make_service()
        resumed._automatic()
        self.assertEqual(len(resumed.store.runs()), 1)
        self.assertEqual(resumed.store.setting("analysis_attempt")["state"], "complete")
        self.assertEqual(self.broker.submissions, [])

    def test_four_strategy_allocation_rotates_and_never_buys_same_symbol_twice(self):
        automatic = self.autonomous_service()
        automatic._initialize_policy()
        specs = automatic._specs()
        signals = [{**decision(symbol), "strategy_id": spec["id"]} for spec in specs for symbol in SYMBOLS]
        from backend.experiment_analysis import analysis_version
        run = {"id": "shared", "as_of": "2026-10-02", "created_at": "2026-10-02T17:00:00+09:00",
               "signals": signals, "version_id": analysis_version(self.config)}
        automatic.store.add_run(run)
        ordered, caps = automatic._entry_plan(automatic._policy(), run, self.current.date())
        next_ordered, _ = automatic._entry_plan(automatic._policy(), run, (self.current + timedelta(days=1)).date())
        self.assertNotEqual(ordered[0]["strategy_id"], next_ordered[0]["strategy_id"])
        self.assertEqual(len(set(caps.values())), 1)
        automatic._cycle()
        orders = automatic.store.orders()
        self.assertEqual(len({item["symbol"] for item in orders}), len(orders))
        self.assertGreater(len(orders), 0)
        self.assertLessEqual(sum(Decimal(item["limit_price"]) * item["quantity"] * (1 + FEE) for item in orders),
                             Decimal(automatic._policy()["daily_buy_limit"]))
        for spec in specs:
            allocated = sum(Decimal(item["limit_price"]) * item["quantity"] * (1 + FEE)
                            for item in orders if item["strategy_id"] == spec["id"])
            self.assertLessEqual(allocated, Decimal(automatic._policy()["daily_buy_limit"]) / 4)
        for remote in self.broker.remote:
            remote.update(status="cancelled", remaining_quantity=0)
        automatic._cycle()
        self.assertEqual(len(self.broker.submissions), len(orders))

    def test_four_strategy_custom_order_cap_is_respected(self):
        self.service._configure({**self.policy, "execution_strategy": ALL_STRATEGIES, "order_cap": "5000"})
        run = {"signals": [decision()]}
        _, caps = self.service._entry_plan(self.service._policy(), run, self.current.date())
        self.assertEqual(caps[STRATEGY], Decimal("5000"))
        self.add_run(symbols=("005930",))
        self.enable()
        self.service._cycle()
        self.assertEqual(self.broker.submissions, [])

    def test_symbol_restriction_skips_only_that_symbol(self):
        self.add_run()
        self.enable()
        original = self.broker.quote
        self.broker.quote = lambda symbol: {**original(symbol), "eligible": symbol != "005930"}
        self.service._cycle()
        self.assertEqual([item[0] for item in self.broker.submissions], ["000660", "035420"])
        self.assertFalse(self.service._blocked())

    def test_quote_transport_failure_uses_global_retry_instead_of_symbol_skip(self):
        self.add_run(symbols=("005930",))
        self.enable()
        self.broker.quote = Mock(side_effect=KisError("모의 시세 연결 시간 초과"))
        self.service._work("cycle", {})
        self.assertEqual(self.broker.submissions, [])
        self.assertFalse(self.service._retry_due())
        self.assertTrue(self.service.store.setting("enabled"))

    def test_balance_mismatch_is_quarantined_then_automatically_resolved(self):
        order = self.submit(quantity=2)
        self.fill(2, status="filled")
        self.service._work("cycle", {})
        self.assertTrue(self.service._blocked())
        self.assertTrue(self.service.store.setting("enabled"))
        self.broker.account["holdings"][order["symbol"]] = {"quantity": 2, "sellable_quantity": 2}
        self.service._work("cycle", {})
        self.assertFalse(self.service._blocked())
        self.assertEqual(len(self.broker.submissions), 1)

    def test_multi_session_analysis_partial_fill_restart_exit_and_realized_pnl(self):
        self.current = datetime(2026, 10, 2, 17, tzinfo=KST)
        self.service._analyze()
        self.enable()
        self.current = NOW
        self.service._work("cycle", {})
        self.assertEqual(len(self.broker.submissions), 1)
        buy = self.service.store.orders()[0]
        self.assertEqual(buy["quantity"], 2)
        self.fill(1)
        self.broker.account["holdings"]["005930"] = {"quantity": 1, "sellable_quantity": 1}
        self.service.close()
        self.service = self.make_service()
        self.assertTrue(self.service.store.setting("enabled"))
        self.fill(2, status="filled")
        self.broker.account["holdings"]["005930"] = {"quantity": 2, "sellable_quantity": 2}
        self.service._work("cycle", {})
        self.assertEqual(len(self.broker.submissions), 1)
        self.broker.days += ["2026-10-07", "2026-10-08", "2026-10-12", "2026-10-13", "2026-10-14"]
        self.current = datetime(2026, 10, 14, 10, tzinfo=KST)
        self.broker.price = "12000"
        self.service._work("cycle", {})
        self.assertEqual(self.broker.submissions[-1], ("005930", "sell", 2, "12000"))
        self.broker.remote[-1].update(status="filled", filled_quantity=2, remaining_quantity=0,
                                      filled_amount="24000", average_price="12000")
        self.broker.account["holdings"] = {}
        self.service._work("cycle", {})
        positions, metrics = self.service._portfolio(self.service._owned_orders())
        self.assertEqual(positions, [])
        self.assertEqual(metrics[0]["closed_trades"], 1)
        expected = Decimal("24000") * (1 - FEE - Decimal("0.002")) - Decimal("20000") * (1 + FEE)
        self.assertEqual(Decimal(metrics[0]["realized_pnl"]), expected)
        self.assertEqual(len(self.broker.submissions), 2)

    def test_stale_order_is_cancelled_after_entry_cutoff_without_new_entry(self):
        self.current = NOW.replace(hour=15, minute=14)
        self.submit(quantity=2)
        self.fill(1)
        self.broker.account["holdings"]["005930"] = {"quantity": 1, "sellable_quantity": 1}
        self.current += timedelta(minutes=10)
        self.service._cycle()
        self.assertEqual(len(self.broker.cancellations), 1)
        self.assertEqual(self.broker.cancellations[0][-1], 1)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.service.store.orders()[0]["status"], "cancel_pending")

    def test_prior_day_remaining_is_not_sent_to_today_cancel_and_other_exit_continues(self):
        held = self.submit(quantity=2)
        held.update(order_date="2026-09-28", created_at="2026-09-28T10:00:00+09:00", status="filled",
                    filled_quantity=2, filled_amount="20000", average_price="10000", remaining_quantity=0)
        self.service.store.save_order(held)
        old = self.submit(quantity=2, symbol="000660", key="old-open")
        old.update(order_date="2026-10-02", created_at="2026-10-02T15:14:00+09:00")
        self.service.store.save_order(old)
        self.broker.remote[-1]["order_date"] = "2026-10-02"
        self.broker.account["holdings"]["005930"] = {"quantity": 2, "sellable_quantity": 2}
        self.service._cycle()
        self.assertEqual(self.broker.cancellations, [])
        self.assertEqual(self.broker.submissions[-1], ("005930", "sell", 2, "10000"))
        self.assertTrue(self.service._blocked())
        issue = next(item for item in self.service.store.setting("issues") if item["id"] == "order:" + old["id"])
        self.assertEqual(issue["code"], "order_expiry_unverified")
        self.broker.remote[1].update(status="cancelled", remaining_quantity=0)
        self.service._reconcile(self.service._policy(), self.broker)
        issue = next(item for item in self.service.store.setting("issues") if item["id"] == "order:" + old["id"])
        self.assertEqual(issue["state"], "resolved")

    def test_single_rejection_keeps_safe_code_without_question_or_retry(self):
        self.broker.submit_error = BrokerRejected("모의 주문이 KIS에서 거절됐습니다.", code="APBK00001")
        order = self.submit()
        self.assertEqual(order["broker_error_code"], "APBK00001")
        issue = self.service.store.setting("issues")[0]
        self.assertIn("APBK00001", issue["message"])
        self.assertIsNone(issue["question"])
        self.assertFalse(issue["blocking"])
        self.submit()
        self.assertEqual(len(self.broker.submissions), 1)

    def test_all_symbol_rejections_raise_question_and_next_day_success_resolves(self):
        self.broker.submit_error = BrokerRejected("모의 주문이 KIS에서 거절됐습니다.", code="APBK00001")
        for symbol in SYMBOLS:
            self.submit(symbol=symbol, key=symbol)
        issue = next(item for item in self.service.store.setting("issues") if item["id"] == "submission_rejections")
        self.assertTrue(issue["question"])
        self.assertFalse(issue["blocking"])
        self.current += timedelta(days=1)
        self.broker.submit_error = None
        self.submit(key="following-day")
        self.assertTrue(all(item["state"] == "resolved" for item in self.service.store.setting("issues")))
        self.assertEqual(sum(order["status"] == "rejected" for order in self.service.store.orders()), 3)

    def test_authentication_rejection_question_resolves_after_confirmed_acceptance(self):
        self.broker.submit_error = BrokerRejected("모의 주문 전 인증에 실패했습니다.", request_sent=False)
        self.submit()
        self.assertTrue(self.service.store.orders()[0]['submission_skipped'])
        from backend.learning_account import execution_profile
        self.assertEqual(execution_profile(self.service.store.orders())['basis']['buy_orders'], 0)
        self.assertIn("인증", self.service.store.setting("issues")[0]["question"])
        self.current += timedelta(seconds=1)
        self.broker.submit_error = None
        self.submit(symbol="000660", key="authenticated")
        self.assertEqual(self.service.store.setting("issues")[0]["state"], "resolved")

    def test_exit_rejection_question_resolves_only_after_position_is_closed(self):
        self.submit(quantity=2)
        self.fill(2, status="filled")
        self.broker.submit_error = BrokerRejected("모의 주문이 KIS에서 거절됐습니다.", code="APBK00001")
        self.current += timedelta(seconds=1)
        rejected = self.submit(quantity=2, side="sell", key="rejected-exit")
        issue_id = "rejected:" + rejected["id"]
        issue = next(item for item in self.service.store.setting("issues") if item["id"] == issue_id)
        self.assertTrue(issue["question"])
        self.current += timedelta(days=1)
        self.broker.submit_error = None
        self.submit(quantity=2, side="sell", key="new-exit")
        issue = next(item for item in self.service.store.setting("issues") if item["id"] == issue_id)
        self.assertEqual(issue["state"], "open")
        self.fill(2, index=1, status="filled")
        issue = next(item for item in self.service.store.setting("issues") if item["id"] == issue_id)
        self.assertEqual(issue["state"], "resolved")


class ExperimentStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "experiments.sqlite3"

    def test_legacy_index_backfill_preserves_frozen_payload_and_pause(self):
        frozen = {"observed_at": "2026-10-02T08:00:00+00:00", "as_of": "2026-10-02", "rows": []}
        run = {"id": "legacy", "as_of": "2026-10-02", "created_at": "2026-10-02T09:00:00+00:00",
               "version_id": "v1", "signals": [decision()], "analysis": {"input": frozen, "decisions": [decision()]}}
        original = json.dumps(run, indent=2)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE runs (id TEXT PRIMARY KEY,day TEXT,created_at TEXT,payload TEXT)")
            db.execute("INSERT INTO runs VALUES (?,?,?,?)", (run["id"], run["as_of"], run["created_at"], original))
            db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.executemany("INSERT INTO settings VALUES (?,?)", [("enabled", "false"), ("user_paused", "true")])
        store = ExperimentStore(self.path)
        key = hashlib.sha256(encode({"as_of": "2026-10-02", "rows": [], "analysis_version": "v1"}).encode()).hexdigest()
        self.assertEqual(store.find_run(key)["id"], "legacy")
        self.assertNotIn("analysis", store.find_run(key))
        self.assertEqual(store.runs(), [run])
        self.assertFalse(store.setting("enabled"))
        self.assertTrue(store.setting("user_paused"))
        # A second startup uses the existing index without rewriting source records.
        ExperimentStore(self.path)
        with store.connect() as db:
            self.assertEqual(db.execute("SELECT payload FROM runs").fetchone()[0], original)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM run_index").fetchone()[0], 1)

    def test_new_runs_store_decisions_once_and_hydrate_existing_shape(self):
        store = ExperimentStore(self.path)
        run = {"id": "new", "as_of": "2026-10-02", "created_at": "2026-10-02T09:00:00+00:00",
               "collection_hash": "same-input", "signals": [decision()], "analysis": {"decisions": [decision()]}}
        original = deepcopy(run)
        store.add_run(run)
        self.assertEqual(run, original)
        self.assertEqual(store.runs(), [original])
        self.assertEqual(store.find_run("same-input"), store.runs(details=False)[0])
        with store.connect() as db:
            stored = json.loads(db.execute("SELECT payload FROM runs").fetchone()[0])
        self.assertNotIn("decisions", stored["analysis"])
        self.assertEqual(stored["signals"], original["signals"])

    def test_index_lookup_is_not_limited_to_recent_200_runs(self):
        store = ExperimentStore(self.path)
        for index in range(202):
            store.add_run({"id": f"run-{index:03}", "as_of": "2026-10-02",
                           "created_at": "2026-10-02T09:00:00+00:00", "signals": [],
                           "collection_hash": f"input-{index}"})
        self.assertEqual(store.find_run("input-0")["id"], "run-000")
        self.assertIsNone(store.find_run("missing"))


if __name__ == "__main__":
    unittest.main()
