"""Atomic confirmed-fill observations; no destination or live account access."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend.experiment_store import ExperimentStore, encode


FP = "source-fingerprint"
STAMP = "2026-10-12T00:05:00+00:00"


def order(identity="1", filled=0, **changes):
    return {"id": "local-" + identity, "fingerprint": FP, "created_at": STAMP,
            "order_date": "2026-10-12", "branch_id": "12", "order_id": identity,
            "symbol": "005930", "side": "buy", "quantity": 1000,
            "filled_quantity": filled, "filled_amount": str(filled * 100),
            "reconciled_at": STAMP, "status": "partial" if filled else "submitted", "error": None,
            **changes}


class MirrorFeedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "experiments.sqlite3"
        self.store = ExperimentStore(self.path)
        self.store.save_setting("policy", {"account_id": "paper", "fingerprint": FP})

    def reserve(self, value):
        self.assertTrue(self.store.reserve_order(value, value["id"]))
        return value

    def capture(self, value, filled, **changes):
        value = {**value, "filled_quantity": filled, "filled_amount": str(filled * 100), **changes}
        self.store.save_order(value, capture_fill=True)
        return value

    def test_feed_includes_order_controls_without_changing_them(self):
        self.assertFalse(self.store.mirror_snapshot()["orders_enabled"])
        self.store.save_setting("enabled", True)
        self.store.save_setting("user_paused", True)
        view = self.store.mirror_snapshot()
        self.assertTrue(view["orders_enabled"])
        self.assertTrue(view["user_paused"])
        self.assertFalse(view["submission_enabled"])
        self.assertIs(self.store.setting("user_paused"), True)

    def test_partial_fill_repeats_cancellation_and_cursor(self):
        value = self.reserve(order())
        for quantity in (2, 2, 5):
            value = self.capture(value, quantity)
        value = self.capture(value, 5, status="cancelled")
        result = self.store.mirror_snapshot()
        self.assertEqual([row["quantity"] for row in result["events"]], [2, 3])
        self.assertEqual([row["amount"] for row in result["events"]], ["200", "300"])
        self.assertEqual([(row["from_quantity"], row["to_quantity"]) for row in result["events"]], [(0, 2), (2, 5)])
        self.assertEqual(result["owned_quantities"], {"005930": 5})
        self.assertEqual(json.loads(result["events"][0]["source_key"]), [FP, "2026-10-12", "12", "1"])
        self.assertEqual(self.store.mirror_snapshot(after=result["next_cursor"])["events"], [])
        self.assertFalse(result["submission_enabled"])

    def test_sell_fill_observation_and_owned_quantity(self):
        self.reserve(order(filled=5))
        value = self.reserve(order("2", side="sell"))
        self.capture(value, 2)
        result = self.store.mirror_snapshot()
        self.assertEqual((result["events"][0]["side"], result["events"][0]["quantity"]), ("sell", 2))
        self.assertEqual(result["owned_quantities"], {"005930": 3})

    def test_unknown_intent_can_resolve_to_confirmed_identity(self):
        value = self.reserve(order(order_id=None, branch_id=None, status="unknown", reconciled_at=None))
        result = self.store.mirror_snapshot()
        self.assertEqual(result["events"], [])
        self.assertEqual(result["source_issues"][0]["reason"], "source_order_unconfirmed")
        self.capture(value, 2, order_id="1", branch_id="12", status="partial", reconciled_at=STAMP)
        result = self.store.mirror_snapshot()
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["source_issues"], [])

    def test_invalid_reconciliation_time_never_implies_verified_source(self):
        value = self.reserve(order(filled=2, reconciled_at="2026-10-12T10:00:00"))
        self.assertEqual(self.store.mirror_snapshot()["source_issues"][0]["reason"], "source_order_not_reconciled")
        self.store.save_order({**value, "reconciled_at": "invalid"})
        self.assertEqual(self.store.mirror_snapshot()["source_issues"][0]["reason"], "source_order_not_reconciled")

    def test_restarts_retain_events_and_identical_observation_performs_no_writes(self):
        value = self.capture(self.reserve(order()), 2)
        restarted = ExperimentStore(self.path)
        with closing(sqlite3.connect(self.path)) as watcher:
            before = watcher.execute("PRAGMA data_version").fetchone()[0]
            restarted.save_order(deepcopy(value), capture_fill=True)
            self.assertEqual(watcher.execute("PRAGMA data_version").fetchone()[0], before)
        self.assertEqual(len(restarted.mirror_snapshot()["events"]), 1)

    def test_event_insert_failure_rolls_back_order_update(self):
        value = self.reserve(order())
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_fill BEFORE INSERT ON mirror_fills BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test failure"):
            self.capture(value, 2)
        self.assertEqual(self.store.orders(), [value])
        self.assertEqual(self.store.mirror_snapshot()["events"], [])

    def test_conflicts_and_bad_timestamps_roll_back_both_records(self):
        value = self.capture(self.reserve(order()), 2)
        original = deepcopy(self.store.orders())
        mutations = ({"filled_quantity": 1, "filled_amount": "100"},
                     {"filled_amount": "201"}, {"filled_quantity": 3, "filled_amount": "200"},
                     {"order_id": "99"}, {"branch_id": "99"}, {"symbol": "000660"},
                     {"side": "sell"}, {"fingerprint": "other"}, {"quantity": 3},
                     {"filled_quantity": 3, "filled_amount": "300", "reconciled_at": None},
                     {"filled_quantity": 3, "filled_amount": "300", "reconciled_at": "2026-10-12T10:00:00"})
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.store.save_order({**value, **mutation}, capture_fill=True)
            self.assertEqual(self.store.orders(), original)
            self.assertEqual(len(self.store.mirror_snapshot()["events"]), 1)

    def test_legacy_orders_and_default_save_do_not_replay_old_fills(self):
        value = self.reserve(order(filled=5))
        self.store.save_order({**value, "status": "filled"})
        restarted = ExperimentStore(self.path)
        result = restarted.mirror_snapshot()
        self.assertEqual(result["events"], [])
        self.assertEqual(result["owned_quantities"], {"005930": 5})
        self.capture({**value, "status": "filled"}, 6)
        self.assertEqual(self.store.mirror_snapshot()["events"][0]["quantity"], 1)

    def test_pagination_over_200_and_cursor_account_binding(self):
        value = self.reserve(order())
        for quantity in range(1, 204):
            value = self.capture(value, quantity)
        first = self.store.mirror_snapshot(limit=200)
        self.assertEqual(len(first["events"]), 200)
        self.assertTrue(first["has_more"])
        second = self.store.mirror_snapshot(after=first["next_cursor"], limit=200)
        self.assertEqual(len(second["events"]), 3)
        self.assertFalse(second["has_more"])
        self.assertEqual(len({row["id"] for row in first["events"] + second["events"]}), 203)
        self.store.save_setting("policy", {"account_id": "competition", "fingerprint": "other"})
        self.assertEqual(self.store.mirror_snapshot()["events"], [])
        with self.assertRaisesRegex(ValueError, "cursor_account"):
            self.store.mirror_snapshot(after=first["next_cursor"])

    def test_read_snapshot_is_readonly_and_page_size_does_not_limit_positions(self):
        for identity, symbol in (("1", "005930"), ("2", "000660")):
            self.capture(self.reserve(order(identity, symbol=symbol)), 2)
        with closing(sqlite3.connect(self.path)) as watcher:
            before = watcher.execute("PRAGMA data_version").fetchone()[0]
            result = self.store.mirror_snapshot(limit=1)
            self.assertEqual(watcher.execute("PRAGMA data_version").fetchone()[0], before)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["owned_quantities"], {"005930": 2, "000660": 2})

    def test_unconfigured_missing_cursor_and_invalid_arguments(self):
        self.store.save_setting("policy", {})
        result = self.store.mirror_snapshot()
        self.assertEqual((result["status"], result["source_account"], result["events"]), ("unconfigured", None, []))
        for kwargs in ({"after": 1}, {"after": -1}, {"after": True}, {"after": 1.0},
                       {"limit": 0}, {"limit": 201}, {"limit": True}, {"limit": "1"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.mirror_snapshot(**kwargs)

    def test_feed_whitelists_event_fields_and_redacts_source_issue_error(self):
        secret = "app_secret=do-not-export"
        value = self.reserve(order(raw_response={"secret": secret}, app_key=secret))
        value = self.capture(value, 2)
        self.store.save_order({**value, "error": secret})
        result = self.store.mirror_snapshot()
        serialized = encode(result)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("raw_response", serialized)
        self.assertEqual(result["source_issues"][0]["reason"], "source_order_error")
        self.assertEqual(set(result["events"][0]), {"id", "source_key", "source_fingerprint", "source_order_id",
                         "symbol", "side", "quantity", "amount", "from_quantity", "to_quantity", "observed_at"})


if __name__ == "__main__":
    unittest.main()
