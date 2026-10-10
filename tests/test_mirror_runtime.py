"""Offline durable runner scenarios using in-memory source/browser adapters."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from backend.experiment_store import ExperimentStore
from backend.mirror_runtime import MirrorRuntime, MirrorBlocked, MirrorUnknown, MirrorRejected


NOW = datetime(2026, 10, 12, 1, tzinfo=timezone.utc)
SYMBOL = "005930"


class FakeSource:
    def __init__(self):
        self.data = {"source_account": "competition", "source_fingerprint": "kis-fingerprint",
                     "events": [], "next_cursor": 0, "has_more": False, "owned_quantities": {},
                     "source_issues": [], "prices": {}, "equity": "10000", "as_of": NOW.isoformat(),
                     "market_open": True, "orders_enabled": True}
        self.duplicate = False
        self.on_read = None
        self.reads = 0
        self.valuations = []

    def fill(self, quantity, side="buy", symbol=SYMBOL):
        self.data["next_cursor"] += 1
        self.data["events"].append({"id": self.data["next_cursor"], "source_fingerprint": self.data["source_fingerprint"],
                                    "symbol": symbol, "side": side, "quantity": quantity})
        self.data["owned_quantities"][symbol] = self.data["owned_quantities"].get(symbol, 0) + quantity * (1 if side == "buy" else -1)
        self.data["prices"][symbol] = {"price": "100", "fresh": True}

    def read(self, cursor):
        self.reads += 1
        if self.on_read:
            self.on_read(self.reads)
        result = deepcopy(self.data)
        if not self.duplicate:
            result["events"] = [event for event in result["events"] if event["id"] > cursor]
        return result

    def value(self, source, symbols):
        self.valuations.append(list(symbols))
        return deepcopy(source)


class FakeDestination:
    def __init__(self):
        self.account_key, self.contest = "timefolio-account", "2026-contest"
        self.session_date = "2026-10-12"
        self.positions, self.orders, self.job_orders = {}, {}, {}
        self.calls, self.submissions = [], []
        self.lookup_unknown = False
        self.submit_error = None
        self.prepare_hook = None
        self.prepare_mismatch = False
        self.prepare_error = None
        self.cash_headroom = "90"

    def snapshot(self, symbols):
        self.calls.append("snapshot")
        positions = {symbol: {"weight": str(weight), "sellable_weight": str(weight)} for symbol, weight in self.positions.items()}
        for symbol in symbols:
            positions.setdefault(symbol, {"weight": "0", "sellable_weight": "0"})
        pending = [{"symbol": row["symbol"], "side": row["side"],
                    "weight": str(Decimal(row["weight"]) - Decimal(row["filled_weight"]))}
                   for row in self.orders.values() if row["status"] == "working"]
        return {"verified": True, "account_key": self.account_key, "contest": self.contest,
                "session_date": self.session_date, "positions": positions, "pending_orders": pending,
                "orders": deepcopy(list(self.orders.values())), "constraints": {"verified": True,
                    "cash_buy_headroom": self.cash_headroom, "smallcap_buy_headroom": "20",
                    "sectors": {"IT": "113.2"}, "stocks": {symbol: {"eligible": True, "sector": "IT",
                        "smallcap": False, "buy_headroom": "30"} for symbol in symbols}}}

    def prepare(self, proposal):
        self.calls.append("prepare")
        if self.prepare_error:
            raise self.prepare_error
        if self.prepare_hook:
            self.prepare_hook()
        return {**proposal, "verified": True, **({"side": "sell"} if self.prepare_mismatch else {})}

    def submit(self, proposal):
        self.calls.append("submit")
        self.submissions.append(deepcopy(proposal))
        if isinstance(self.submit_error, MirrorRejected):
            raise self.submit_error
        identity = str(len(self.orders) + 1)
        receipt = {"verified": True, "order_id": identity, "symbol": proposal["symbol"],
                   "side": proposal["side"], "weight": proposal["weight"], "filled_weight": "0", "status": "working"}
        self.orders[identity] = receipt
        self.job_orders[proposal["job_id"]] = identity
        if self.submit_error:
            raise self.submit_error
        return deepcopy(receipt)

    def receipt_snapshot(self):
        result = self.snapshot([])
        self.calls[-1] = "receipt_snapshot"
        return result

    def lookup(self, job, snapshot=None):
        self.calls.append("lookup")
        if self.lookup_unknown:
            return {"verified": False, "status": "unknown"}
        identity = self.job_orders.get(job["id"])
        if snapshot is None:
            raise AssertionError("lookup must reuse the cycle observation")
        return next((deepcopy(row) for row in snapshot["orders"] if row["order_id"] == identity), None)

    def fill(self, identity, filled_weight):
        order = self.orders[identity]
        delta = Decimal(filled_weight) - Decimal(order["filled_weight"])
        symbol = order["symbol"]
        self.positions[symbol] = self.positions.get(symbol, Decimal(0)) + delta * (1 if order["side"] == "buy" else -1)
        order["filled_weight"] = str(filled_weight)
        order["status"] = "filled" if Decimal(filled_weight) == Decimal(order["weight"]) else "working"


class MirrorRuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = ExperimentStore(Path(directory.name) / "experiments.sqlite3")
        self.source, self.destination = FakeSource(), FakeDestination()
        self.runtime = self.make_runtime()

    def make_runtime(self):
        return MirrorRuntime(self.store, self.source, self.destination, now=lambda: NOW)

    def initialize(self):
        self.assertEqual(self.runtime.tick()["status"], "initialized")

    def test_buy_partial_completion_then_sell(self):
        self.initialize()
        self.source.fill(3)
        self.assertEqual(self.runtime.tick()["status"], "working")
        self.assertEqual(self.destination.submissions[0]["weight"], "3.00")
        self.destination.fill("1", "1")
        self.runtime.tick()
        self.assertEqual(self.runtime.snapshot()["jobs"][0]["receipt"]["filled_weight"], "1")
        self.source.fill(2)
        self.runtime.tick()
        self.assertEqual(len(self.destination.submissions), 1)
        self.assertEqual(self.runtime.snapshot()["jobs"][-1]["reason"], "destination_symbol_order_working")
        self.destination.fill("1", "3")
        self.runtime.tick()
        self.assertEqual(self.destination.submissions[1]["weight"], "2.00")
        self.destination.fill("2", "2")
        self.runtime.tick()
        self.source.fill(5, "sell")
        self.runtime.tick()
        self.assertEqual((self.destination.submissions[2]["side"], self.destination.submissions[2]["weight"]), ("sell", "5.00"))
        self.destination.fill("3", "5")
        self.assertEqual(self.runtime.tick()["status"], "idle")
        self.assertEqual(self.destination.positions[SYMBOL], 0)

    def test_first_run_baselines_old_events_without_initial_position_orders(self):
        self.source.fill(4)
        self.assertEqual(self.runtime.tick()["reason"], "initial_source_must_be_empty")
        self.source.fill(4, "sell")
        self.initialize()
        self.assertEqual(self.runtime.snapshot()["binding"]["cursor"], 2)
        self.assertEqual(self.destination.submissions, [])
        self.source.fill(2)
        self.runtime.tick()
        self.assertEqual(self.destination.submissions[0]["weight"], "2.00")

    def test_duplicate_reads_and_restart_do_not_repeat_order(self):
        self.initialize()
        self.source.fill(2)
        self.runtime.tick()
        self.source.duplicate = True
        for _ in range(3):
            self.runtime = self.make_runtime()
            self.runtime.tick()
        self.assertEqual(len(self.destination.submissions), 1)
        self.assertEqual(len(self.runtime.snapshot()["jobs"]), 1)

    def test_unknown_response_never_reclicks_and_can_resolve_by_receipt(self):
        self.initialize()
        self.source.fill(2)
        self.destination.submit_error = MirrorUnknown("response lost")
        self.destination.lookup_unknown = True
        self.assertEqual(self.runtime.tick()["reason"], "unknown_destination_submission")
        self.runtime = self.make_runtime()
        self.runtime.tick()
        self.runtime.tick()
        self.assertEqual(len(self.destination.submissions), 1)
        self.destination.lookup_unknown = False
        self.destination.submit_error = None
        self.runtime.tick()
        self.assertEqual(self.runtime.snapshot()["jobs"][0]["status"], "working")
        self.assertEqual(len(self.destination.submissions), 1)

    def test_crash_after_click_recovers_as_unknown_without_repeat(self):
        self.initialize()
        self.source.fill(2)
        self.destination.submit_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.runtime.tick()
        self.assertEqual(self.runtime.snapshot()["jobs"][0]["status"], "submitting")
        self.runtime = self.make_runtime()
        self.assertEqual(self.runtime.snapshot()["jobs"][0]["status"], "unknown")
        self.destination.lookup_unknown = True
        self.runtime.tick()
        self.assertEqual(len(self.destination.submissions), 1)

    def test_paused_source_queues_events_and_reconciles_existing_orders(self):
        self.initialize()
        self.source.fill(2)
        self.runtime.tick()
        self.destination.fill("1", "2")
        self.source.fill(2)
        self.source.data.update(orders_enabled=False, equity=None, prices={})
        self.assertEqual(self.runtime.tick()["status"], "paused")
        jobs = self.runtime.snapshot()["jobs"]
        self.assertEqual([job["status"] for job in jobs], ["filled", "pending"])
        self.assertEqual(len(self.destination.submissions), 1)

    def test_source_pause_or_new_fill_during_prepare_prevents_outdated_click(self):
        self.initialize()
        self.source.fill(2)
        self.destination.prepare_hook = lambda: self.source.data.update(orders_enabled=False)
        self.assertEqual(self.runtime.tick()["reason"], "source_execution_prerequisite_changed")
        self.assertEqual(self.destination.submissions, [])
        self.source.data["orders_enabled"] = True
        self.destination.prepare_hook = lambda: self.source.fill(1)
        self.assertEqual(self.runtime.tick()["status"], "source_changed")
        self.assertEqual(self.destination.submissions, [])
        self.destination.prepare_hook = None
        self.runtime.tick()
        self.assertEqual(self.destination.submissions[0]["weight"], "3.00")

    def test_opposite_pending_reservation_blocks_new_sell(self):
        self.initialize()
        self.source.fill(3)
        self.runtime.tick()
        self.destination.fill("1", "1")
        self.source.fill(3, "sell")
        self.runtime.tick()
        self.assertEqual(self.runtime.snapshot()["jobs"][-1]["reason"], "destination_symbol_order_working")
        self.assertEqual(len(self.destination.submissions), 1)

    def test_destination_limits_are_left_to_actual_submission(self):
        self.initialize()
        self.source.fill(3)
        self.destination.cash_headroom = "1"
        self.runtime.tick()
        self.assertEqual(self.destination.submissions[0]["weight"], "3.00")

    def test_initial_manual_position_or_new_unowned_order_blocks(self):
        self.destination.positions[SYMBOL] = Decimal(1)
        self.assertEqual(self.runtime.tick()["reason"], "initial_destination_must_be_empty")
        self.destination.positions[SYMBOL] = Decimal(0)
        self.initialize()
        self.source.fill(2)
        self.destination.orders["manual"] = {"order_id": "manual", "status": "filled", "symbol": SYMBOL}
        self.assertEqual(self.runtime.tick()["status"], "blocked")
        self.assertEqual(self.runtime.snapshot()["jobs"][0]["reason"], "unowned_destination_order_detected")
        self.assertEqual(self.destination.submissions, [])

    def test_initial_unresolved_reservation_is_not_an_empty_account(self):
        snapshot = self.destination.snapshot
        self.destination.snapshot = lambda symbols: {**snapshot(symbols), "unresolved_symbols": [SYMBOL]}
        self.assertEqual(self.runtime.tick()["reason"], "initial_destination_must_be_empty")
        self.assertIsNone(self.runtime.snapshot()["binding"])

    def test_binding_changes_stale_inputs_and_form_mismatch_block(self):
        self.initialize()
        self.source.fill(2)
        self.destination.contest = "other"
        self.assertEqual(self.runtime.tick()["reason"], "destination_binding_changed")
        self.destination.contest = "2026-contest"
        self.source.data["source_account"] = "other"
        self.assertEqual(self.runtime.tick()["reason"], "source_binding_changed")
        self.source.data["source_account"] = "competition"
        self.source.data["as_of"] = (NOW - timedelta(minutes=4)).isoformat()
        self.assertEqual(self.runtime.tick()["reason"], "source_snapshot_stale_or_invalid")
        self.source.data["as_of"] = NOW.isoformat()
        self.destination.prepare_mismatch = True
        self.assertEqual(self.runtime.tick()["reason"], "prepared_form_mismatch")
        self.assertEqual(self.destination.submissions, [])

    def test_closed_market_allows_binding_but_not_submission(self):
        self.source.data.update(market_open=False, equity=None)
        self.destination.session_date = "2026-10-09"
        self.initialize()
        self.source.fill(2)
        self.assertEqual(self.runtime.tick()["status"], "waiting_market")
        self.assertEqual(self.destination.submissions, [])

    def test_cursor_and_pending_work_are_one_transaction(self):
        self.initialize()
        self.source.fill(2)
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_job BEFORE INSERT ON mirror_jobs BEGIN SELECT RAISE(ABORT,'fail'); END")
        self.runtime.tick()
        self.assertEqual(self.runtime.snapshot()["binding"]["cursor"], 0)
        self.assertEqual(self.runtime.snapshot()["jobs"], [])
        self.assertEqual(self.destination.submissions, [])

    def test_rejected_response_is_not_retried_without_new_fill(self):
        self.initialize()
        self.source.fill(2)
        self.destination.submit_error = MirrorRejected("explicit rejection")
        self.runtime.tick()
        self.runtime.tick()
        self.assertEqual(self.runtime.snapshot()["jobs"][0]["status"], "rejected")
        self.assertEqual(len(self.destination.submissions), 1)

    def test_receipt_rejection_keeps_actual_reason(self):
        self.initialize()
        self.source.fill(2)
        self.runtime.tick()
        self.destination.orders["1"].update(status="rejected", reason="destination_rejected: 주문 가능 비중 초과")
        self.runtime.tick()
        job = self.runtime.snapshot()["jobs"][0]
        self.assertEqual(job["status"], "rejected")
        self.assertEqual(job["reason"], "destination_rejected: 주문 가능 비중 초과")

    def test_completed_receipt_does_not_invent_filled_weight(self):
        self.initialize()
        self.source.fill(2)
        self.runtime.tick()
        self.destination.orders["1"].update(status="completed", filled_quantity=13, filled_weight=None)
        self.runtime.tick()
        receipt = self.runtime.snapshot()["jobs"][0]["receipt"]
        self.assertEqual((receipt["status"], receipt["filled_quantity"], receipt["filled_weight"]), ("completed", 13, None))

    def test_existing_order_cannot_be_used_as_receipt_for_later_job(self):
        self.initialize()
        self.source.fill(2)
        self.runtime.tick()
        self.destination.fill("1", "2")
        self.runtime.tick()
        self.source.fill(2)
        self.destination.submit = lambda proposal: deepcopy(self.destination.orders["1"])
        self.assertEqual(self.runtime.tick()["reason"], "unknown_destination_submission")
        self.assertEqual(self.runtime.snapshot()["jobs"][-1]["status"], "unknown")

    def test_full_fill_claim_requires_full_weight_evidence(self):
        self.initialize()
        self.source.fill(2)
        self.runtime.tick()
        self.destination.orders["1"].update(status="filled", filled_weight="0")
        self.runtime.tick()
        job = self.runtime.snapshot()["jobs"][0]
        self.assertEqual(job["status"], "working")
        self.assertEqual(job["receipt"]["filled_weight"], "0")
        self.assertEqual(job["reason"], "destination_lookup_unresolved")

    def test_valuation_is_fixed_for_the_batch_even_if_prices_move_during_prepare(self):
        self.initialize()
        self.source.fill(3)
        self.destination.prepare_hook = lambda: self.source.data.update(equity="9999.9")
        self.runtime.tick()
        self.assertEqual(self.destination.submissions[0]["weight"], "3.00")
        self.destination.fill("1", "3")
        self.source.fill(2)
        self.destination.prepare_hook = lambda: self.source.data.update(equity="9000")
        self.runtime.tick()
        self.assertEqual(self.destination.submissions[-1]["weight"], "2.00")
        self.assertEqual(len(self.destination.submissions), 2)

    def test_unresolved_order_only_serializes_its_own_symbol(self):
        self.initialize()
        self.source.fill(2)
        self.runtime.tick()
        snapshot = self.destination.snapshot
        self.destination.snapshot = lambda symbols: {**snapshot(symbols), "pending_orders": []}
        self.source.fill(2, symbol="000660")
        self.destination.lookup_unknown = True
        self.runtime.tick()
        self.assertEqual(len(self.destination.submissions), 2)
        self.assertEqual(self.runtime.snapshot()["jobs"][0]["status"], "working")

    def test_adapter_stable_validation_code_is_preserved(self):
        def fail(cursor):
            raise ValueError("source_policy_changed")
        self.source.read = fail
        self.assertEqual(self.runtime.tick()["reason"], "source_policy_changed")

    def test_idle_and_paused_queue_need_no_browser_or_valuation(self):
        self.initialize()
        self.destination.calls.clear()
        self.runtime.tick()
        self.source.fill(2)
        self.source.data["orders_enabled"] = False
        self.assertEqual(self.runtime.tick()["status"], "paused")
        self.assertEqual(self.destination.calls, [])
        self.assertEqual(self.source.valuations, [])
        self.assertEqual(self.runtime.snapshot()["binding"]["cursor"], 1)

    def test_netted_round_trip_needs_no_browser_or_valuation(self):
        self.initialize()
        self.destination.calls.clear()
        self.source.fill(2)
        self.source.fill(2, side="sell")
        self.assertEqual(self.runtime.tick()["status"], "idle")
        self.assertEqual(self.destination.calls, [])
        self.assertEqual(self.source.valuations, [])
        self.assertEqual(self.runtime.snapshot()["jobs"][0]["status"], "unchanged")

    def test_batch_uses_one_valuation_and_one_portfolio_observation(self):
        self.initialize()
        self.source.fill(2)
        self.source.fill(3, symbol="000660")
        self.destination.calls.clear()
        self.source.reads = 0
        self.runtime.tick()
        self.assertEqual(self.source.valuations, [["000660", SYMBOL]])
        self.assertEqual(self.source.reads, 3)  # feed plus a local stop/fill check per submit
        self.assertEqual(self.destination.calls.count("snapshot"), 1)
        self.assertEqual(len(self.destination.submissions), 2)
        self.destination.calls.clear()
        self.runtime.tick()
        self.assertEqual(self.destination.calls, ["receipt_snapshot", "lookup", "lookup"])
        self.assertEqual(len(self.source.valuations), 1)

    def test_unchanged_receipt_does_not_write_or_update_timestamp(self):
        self.initialize()
        self.source.fill(2)
        self.runtime.tick()
        before = self.runtime.snapshot()["jobs"][0]
        with self.store.connect() as db:
            db.executescript("CREATE TABLE writes(n INTEGER); INSERT INTO writes VALUES(0); "
                             "CREATE TRIGGER count_write AFTER UPDATE ON mirror_jobs BEGIN UPDATE writes SET n=n+1; END;")
        self.runtime.now = lambda: NOW + timedelta(seconds=30)
        self.runtime.tick()
        self.assertEqual(self.runtime.snapshot()["jobs"][0], before)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT n FROM writes").fetchone()[0], 0)

    def test_unknown_submission_does_not_block_another_symbol_or_repeat_itself(self):
        self.initialize()
        self.source.fill(2)
        self.destination.submit_error = MirrorUnknown("lost response")
        self.runtime.tick()
        self.destination.submit_error = None
        self.destination.lookup_unknown = True
        self.source.fill(3, symbol="000660")
        self.source.fill(1)
        self.runtime.tick()
        self.assertEqual([p["symbol"] for p in self.destination.submissions], [SYMBOL, "000660"])
        blocked = [job for job in self.runtime.snapshot()["jobs"] if job["status"] == "blocked"]
        self.assertEqual(blocked[0]["reason"], "destination_symbol_order_unknown")

    def test_other_source_order_issue_or_new_fill_does_not_cancel_prepared_order(self):
        self.initialize()
        self.source.fill(2)
        self.source.data["source_issues"] = [{"id": "other", "reason": "source_order_unconfirmed"}]
        self.destination.prepare_hook = lambda: self.source.fill(3, symbol="000660")
        self.runtime.tick()
        self.assertEqual(len(self.destination.submissions), 1)
        self.assertEqual(self.destination.submissions[0]["symbol"], SYMBOL)
        self.assertEqual(self.runtime.snapshot()["binding"]["cursor"], 2)

    def test_explicit_form_rejection_is_terminal_without_click_or_retry(self):
        self.initialize()
        self.source.fill(2)
        self.destination.prepare_error = MirrorRejected("destination_order_rejected")
        self.runtime.tick()
        self.runtime.tick()
        self.assertEqual(self.runtime.snapshot()["jobs"][0]["status"], "rejected")
        self.assertEqual(self.destination.calls.count("prepare"), 1)
        self.assertEqual(self.destination.submissions, [])

    def test_preclick_session_end_is_blocked_but_bad_receipt_is_unknown(self):
        self.initialize()
        self.source.fill(2)
        submit = self.destination.submit
        def expired(proposal):
            raise MirrorBlocked("destination_outside_order_session")
        self.destination.submit = expired
        self.runtime.tick()
        self.assertEqual(self.runtime.snapshot()["jobs"][0]["status"], "blocked")
        self.assertEqual(self.destination.submissions, [])
        self.destination.submit = lambda proposal: {**submit(proposal), "weight": None}
        self.runtime.tick()
        self.assertEqual(self.runtime.snapshot()["jobs"][0]["status"], "unknown")
        self.runtime.tick()
        self.assertEqual(len(self.destination.submissions), 1)

    def test_prior_ids_only_include_matching_receipt_candidates(self):
        self.destination.orders = {
            "same": {"order_id": "same", "symbol": SYMBOL, "side": "buy", "weight": "2", "status": "completed"},
            "other": {"order_id": "other", "symbol": "000660", "side": "buy", "weight": "2", "status": "completed"},
            "different": {"order_id": "different", "symbol": SYMBOL, "side": "buy", "weight": "3", "status": "completed"}}
        self.initialize()
        self.source.fill(2)
        self.runtime.tick()
        self.assertEqual(self.destination.submissions[0]["prior_order_ids"], ["same"])


if __name__ == "__main__":
    unittest.main()
