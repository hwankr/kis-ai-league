"""Offline mirror calculations; no browser, account, file or network mutations."""
from copy import deepcopy
import json
import unittest

from backend.mirror_plan import MirrorInputError, detect_fill_changes, plan_weight_changes


FP = "source-fingerprint"


def order(filled=0, *, identity="1", side="buy", symbol="005930", **changes):
    return {"fingerprint": FP, "id": "local-" + identity, "order_date": "2026-10-12",
            "branch_id": "12", "order_id": identity, "symbol": symbol, "side": side,
            "quantity": 10, "filled_quantity": filled, "filled_amount": str(filled * 100),
            "status": "partial" if filled else "submitted", **changes}


def change(side="buy", quantity=2, symbol="005930"):
    return {"source_fingerprint": FP, "symbol": symbol, "side": side, "quantity": quantity}


class FillChangesTests(unittest.TestCase):
    def detect(self, rows, **kwargs):
        return detect_fill_changes(rows, source_fingerprint=FP, **kwargs)

    def test_first_use_requires_explicit_baseline_or_replay(self):
        with self.assertRaisesRegex(MirrorInputError, "explicit_baseline"):
            self.detect([order(2)])
        baseline = self.detect([order(2)], initialize="baseline")
        self.assertEqual(baseline["changes"], [])
        self.assertEqual(baseline["owned_quantities"], {"005930": 2})
        self.assertEqual(self.detect([order(2)], initialize="replay")["changes"][0]["quantity"], 2)

    def test_partial_fill_duplicate_and_serialized_checkpoint(self):
        checkpoint = self.detect([order()], initialize="baseline")["checkpoint"]
        quantities, boundaries = [], []
        for filled in (2, 2, 5):
            result = self.detect([order(filled)], checkpoint=json.loads(json.dumps(checkpoint)))
            quantities.extend(row["quantity"] for row in result["changes"])
            boundaries.extend((row["from_quantity"], row["to_quantity"]) for row in result["changes"])
            checkpoint = result["checkpoint"]
        self.assertEqual(quantities, [2, 3])
        self.assertEqual(boundaries, [(0, 2), (2, 5)])

    def test_cancelled_partial_fill_and_rejected_unfilled(self):
        prior = self.detect([order(2)], initialize="baseline")
        result = self.detect([order(2, status="cancelled"), order(identity="2", status="rejected")], checkpoint=prior["checkpoint"])
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["owned_quantities"], {"005930": 2})

    def test_sell_changes_and_other_accounts_filtered(self):
        rows = [order(5), order(2, identity="2", side="sell"), order(8, fingerprint="other")]
        result = self.detect(rows, initialize="replay")
        self.assertEqual([item["side"] for item in result["changes"]], ["buy", "sell"])
        self.assertEqual(result["owned_quantities"], {"005930": 3})

    def test_decrease_missing_identity_symbol_or_amount_conflicts(self):
        checkpoint = self.detect([order(2)], initialize="baseline")["checkpoint"]
        for rows in ([order(1)], [], [order(2, order_id="3")], [order(2, symbol="000660")],
                     [order(2, filled_amount="201")], [order(3, filled_amount="200")],
                     [order(2, order_id=None)], [order(2, quantity=1)]):
            with self.subTest(rows=rows), self.assertRaises(MirrorInputError):
                self.detect(rows, checkpoint=checkpoint)

    def test_fingerprint_change_or_malformed_checkpoint_rejected(self):
        checkpoint = self.detect([order(2)], initialize="baseline")["checkpoint"]
        with self.assertRaisesRegex(MirrorInputError, "source_account_changed"):
            detect_fill_changes([], source_fingerprint="other", checkpoint=checkpoint)
        checkpoint["orders"][0]["fingerprint"] = "other"
        with self.assertRaisesRegex(MirrorInputError, "invalid_checkpoint_orders"):
            self.detect([order(2)], checkpoint=checkpoint)
        checkpoint = self.detect([order(2)], initialize="baseline")["checkpoint"]
        checkpoint["version"] = True
        with self.assertRaisesRegex(MirrorInputError, "invalid_checkpoint"):
            self.detect([order(2)], checkpoint=checkpoint)

    def test_unacknowledged_intent_waits_but_filled_identity_is_required(self):
        self.assertEqual(self.detect([order(order_id=None, branch_id=None)], initialize="replay")["changes"], [])
        with self.assertRaisesRegex(MirrorInputError, "order_id"):
            self.detect([order(1, order_id=None, branch_id=None)], initialize="replay")

    def test_broker_identity_works_without_local_uuid_and_inputs_unchanged(self):
        row = order(2)
        del row["id"]
        before = deepcopy(row)
        result = self.detect([row], initialize="replay")
        self.assertEqual(json.loads(result["changes"][0]["source_key"]), [FP, "2026-10-12", "12", "1"])
        self.assertEqual(row, before)

    def test_duplicate_broker_or_local_identity_rejected(self):
        for rows in ([order(2), order(2)], [order(2), order(2, identity="2", id="local-1")]):
            with self.subTest(rows=rows), self.assertRaisesRegex(MirrorInputError, "duplicate_order_identity"):
                self.detect(rows, initialize="replay")


class WeightProposalTests(unittest.TestCase):
    def setUp(self):
        self.args = {"source_fingerprint": FP, "owned_quantities": {"005930": 5},
                     "source_prices": {"005930": {"price": "100", "fresh": True}},
                     "source_equity": "10000", "destination_positions": {"005930": {"weight": "2", "sellable_weight": "2"}},
                     "pending_orders": [], "destination_verified": True}

    def plan(self, changes=None):
        return plan_weight_changes([change()] if changes is None else changes, **self.args)

    def test_current_price_weight_and_no_changes_no_price_rebalancing(self):
        proposal = self.plan()[0]
        self.assertEqual((proposal["status"], proposal["side"], proposal["weight"]), ("proposal", "buy", "3.00"))
        self.args["source_prices"]["005930"]["price"] = "300"
        self.assertEqual(self.plan([]), [])

    def test_round_toward_zero_and_sub_step_finishes_unchanged(self):
        self.args["source_prices"]["005930"]["price"] = "100.199"
        self.assertEqual(self.plan()[0]["weight"], "3.00")
        for weight in ("5", "5.015"):
            for side in ("buy", "sell"):
                with self.subTest(weight=weight, side=side):
                    self.args["destination_positions"]["005930"]["weight"] = weight
                    result = self.plan([change(side)])[0]
                    self.assertEqual((result["status"], result["reason"]), ("unchanged", "below_weight_step"))

    def test_pending_buy_reserve_is_not_bought_again(self):
        self.args["pending_orders"] = [{"symbol": "005930", "side": "buy", "weight": "1.25"}]
        self.assertEqual(self.plan()[0]["weight"], "1.75")
        self.args["pending_orders"][0]["weight"] = "3"
        self.assertEqual(self.plan()[0]["status"], "unchanged")

    def test_pending_reverse_and_direction_conflict(self):
        self.args["pending_orders"] = [{"symbol": "005930", "side": "sell", "weight": "1"}]
        self.assertEqual(self.plan()[0]["reason"], "pending_reverse_side_conflict")
        self.args["pending_orders"] = []
        self.args["destination_positions"]["005930"]["weight"] = "8"
        self.assertEqual(self.plan()[0]["reason"], "direction_conflict")
        self.args["destination_positions"]["005930"]["weight"] = "2"
        self.assertEqual(self.plan([change("sell")])[0]["reason"], "direction_conflict")

    def test_sell_checks_actual_sellable_less_pending(self):
        self.args["owned_quantities"]["005930"] = 1
        self.args["source_prices"]["005930"]["price"] = "454.1"
        self.args["destination_positions"]["005930"] = {"weight": "8", "sellable_weight": "3.459"}
        self.args["pending_orders"] = [{"symbol": "005930", "side": "sell", "weight": "1"}]
        result = self.plan([change("sell")])[0]
        self.assertEqual((result["side"], result["weight"], result["delta_weight"]), ("sell", "2.45", "-2.45"))
        self.args["owned_quantities"]["005930"] = 0
        self.args["source_prices"] = {}
        result = self.plan([change("sell")])[0]
        self.assertEqual((result["reason"], result["wanted_weight"], result["available_weight"]),
                         ("sellable_weight_exceeded", "7.00", "2.459"))

    def test_missing_stale_nonfinite_and_unverified_inputs_block(self):
        for key, value in (("source_prices", {}), ("source_equity", "NaN"),
                           ("destination_positions", {}), ("pending_orders", None),
                           ("destination_verified", False),
                           ("owned_quantities", {})):
            with self.subTest(key=key):
                original = self.args[key]
                self.args[key] = value
                self.assertEqual(self.plan()[0]["status"], "blocked")
                self.args[key] = original
        self.args["source_prices"]["005930"]["fresh"] = False
        self.assertEqual(self.plan()[0]["reason"], "source_price_missing_or_stale")

    def test_each_touched_symbol_is_converted_without_repeating_site_limits(self):
        self.args["owned_quantities"]["000660"] = 5
        self.args["source_prices"]["000660"] = {"price": "100", "fresh": True}
        self.args["destination_positions"]["000660"] = {"weight": "2", "sellable_weight": "2"}
        before = deepcopy(self.args)
        result = self.plan([change(), change(symbol="000660")])
        self.assertEqual([(row["symbol"], row["status"], row["weight"]) for row in result],
                         [("000660", "proposal", "3.00"), ("005930", "proposal", "3.00")])
        self.assertEqual(self.args, before)
        self.assertEqual([row["symbol"] for row in self.plan()], ["005930"])

    def test_other_symbol_pending_sell_does_not_change_buy_weight(self):
        self.args["pending_orders"] = [{"symbol": "000660", "side": "sell", "weight": "20"}]
        self.assertEqual(self.plan()[0]["weight"], "3.00")

    def test_full_source_exit_needs_no_price_but_cannot_oversell(self):
        self.args["owned_quantities"]["005930"] = 0
        self.args["source_prices"] = {}
        result = self.plan([change("sell")])[0]
        self.assertEqual((result["side"], result["weight"]), ("sell", "2.00"))
        self.args["pending_orders"] = [{"symbol": "005930", "side": "sell", "weight": "2.01"}]
        self.assertEqual(self.plan([change("sell")])[0]["reason"], "pending_sell_exceeds_sellable")

    def test_invalid_quantities_and_reservations_are_not_proposals(self):
        for quantity in (True, -1, "5"):
            self.args["owned_quantities"]["005930"] = quantity
            self.assertEqual(self.plan()[0]["status"], "blocked")
        self.args["owned_quantities"]["005930"] = 5
        for weight in (True, "NaN", "Infinity", "-1", "101"):
            self.args["pending_orders"] = [{"symbol": "005930", "side": "buy", "weight": weight}]
            self.assertEqual(self.plan()[0]["status"], "blocked")

    def test_net_zero_catchup_does_not_replay_historical_roundtrip(self):
        self.assertEqual(self.plan([change(), change("sell")])[0]["reason"], "net_fills_zero")

    def test_change_account_mismatch_blocks(self):
        wrong = {**change(), "source_fingerprint": "other"}
        self.assertEqual(self.plan([wrong])[0]["reason"], "source_account_changed")


if __name__ == "__main__":
    unittest.main()
