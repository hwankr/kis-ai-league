import copy
import csv
import io
import unittest

from backend.timefolio_dom import parse_receipts, parse_snapshot


def grid(kind, columns, records, *, parent_symbol=None, labels=None):
    labels = labels or columns
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(labels)
    rows = []
    for order_id, values in records:
        writer.writerow(values)
        cells = {column: {"id": f"cell{order_id}_{column}", "text": str(value), "badges": []}
                 for column, value in zip(columns, values)}
        rows.append({"cells": cells})
    return {"kind": kind, "headers": [{"id": c, "text": t} for c, t in zip(columns, labels)],
            "csv_text": buffer.getvalue(), "rows": rows, "complete": True, "stable": True,
            "parent_symbol": parent_symbol}


def snapshot():
    return {"url": "https://contest.timefolio.net/", "account_key": "portfolio-13", "contest": "RFM13",
            "session_date": "2026-10-12", "tables": [
                grid("positions", ["prodId", "prodNm", "wei", "pos"], [("p1", ["005930", "삼성전자", "10.1234", "100"])]),
                grid("targets", ["prodId", "prodNm", "w2o", "tgtWei"], [("t1", ["005930", "삼성전자", "1.23", "11.3534"])]),
                grid("orders", ["genT", "prodId", "prodNm", "sgn", "wei", "cumQty", "cnclT", "limitPrc", "hm0"],
                     [("ord-123", ["2026-10-12T09:06:01+09:00", "005930", "삼성전자", "1", "1.234", "2", "", "", "09:06"])]),
                grid("order_details", ["ls", "hm0", "state", "cumQty", "avgPx"],
                     [("ord-123", ["L", "09:06", "Working", "2", "70000"])], parent_symbol="005930"),
            ]}


def parse(value):
    return parse_snapshot(value, ["005930"], "portfolio-13", "RFM13")


class TimefolioDomTests(unittest.TestCase):
    def test_raw_csv_precision_and_real_id_preserved(self):
        data = snapshot()
        # Display formatting loses precision, while UI CSV preserves primitives.
        data["tables"][2]["rows"][0]["cells"]["wei"]["text"] = "1"
        out = parse(data)
        self.assertTrue(out["verified"], out["errors"])
        self.assertEqual(out["orders"][0]["order_id"], "ord-123")
        self.assertEqual(out["orders"][0]["weight"], 1.234)
        self.assertEqual(out["orders"][0]["filled_quantity"], 2)
        self.assertIsNone(out["orders"][0]["filled_weight"])
        self.assertEqual(out["positions"]["005930"]["weight"], 10.1234)
        self.assertEqual(out["pending_orders"], [{"symbol": "005930", "side": "buy", "weight": 1.23}])

    def test_done_is_terminal_but_does_not_claim_full_fill(self):
        data = snapshot()
        data["tables"][3]["csv_text"] = data["tables"][3]["csv_text"].replace("Working", "Done")
        data["tables"][1]["csv_text"] = data["tables"][1]["csv_text"].replace("1.23,11.3534", "0,10.1234")
        out = parse(data)
        self.assertTrue(out["verified"], out["errors"])
        self.assertEqual(out["orders"][0]["status"], "completed")
        self.assertTrue(out["orders"][0]["terminal"])
        self.assertIsNone(out["orders"][0]["filled_weight"])

    def test_rounded_progress_does_not_prove_fill(self):
        data = snapshot()
        data["tables"][3]["rows"][0]["cells"]["cumQty"]["progress"] = [{"value": 100, "max": 100}]
        out = parse(data)
        self.assertEqual(out["orders"][0]["status"], "working")
        self.assertIsNone(out["orders"][0]["filled_weight"])

    def test_row_number_never_used_as_order_id(self):
        data = snapshot()
        for cell in data["tables"][2]["rows"][0]["cells"].values():
            cell["id"] = ""
        data["tables"][2]["rows"][0]["attributes"] = {"data-rowidx": "123"}
        self.assertFalse(parse(data)["verified"])

    def test_mismatched_cell_ids_rejected(self):
        data = snapshot()
        data["tables"][2]["rows"][0]["cells"]["wei"]["id"] = "cellother_wei"
        self.assertFalse(parse(data)["verified"])

    def test_virtualized_or_filtered_or_unstable_grid_rejected(self):
        for field in ("complete", "stable"):
            with self.subTest(field=field):
                data = snapshot()
                data["tables"][0][field] = False
                self.assertFalse(parse(data)["verified"])
        data = snapshot()
        data["tables"][0]["rows"] = []
        self.assertFalse(parse(data)["verified"])

    def test_csv_row_swap_detected_by_symbol(self):
        data = snapshot()
        data["tables"][2]["rows"][0]["cells"]["prodId"]["text"] = "000660"
        self.assertFalse(parse(data)["verified"])

    def test_duplicate_header_labels_still_map_by_column_id(self):
        data = snapshot()
        data["tables"][0] = grid("positions", ["prodId", "prodNm", "wei", "prftRate"],
                                 [("p1", ["005930", "삼성전자", "10.1234", "7.125"])],
                                 labels=["코드", "종목명", "%", "%"])
        self.assertEqual(parse(data)["positions"]["005930"]["weight"], 10.1234)

    def test_missing_state_isolates_symbol(self):
        data = snapshot()
        data["tables"] = [t for t in data["tables"] if t["kind"] != "order_details"]
        out = parse(data)
        self.assertTrue(out["verified"])
        self.assertEqual(out["unresolved_symbols"], ["005930"])
        self.assertEqual(out["orders"][0]["status"], "unknown")

    def test_known_terminal_receipt_requires_exact_metadata(self):
        data = snapshot()
        old = parse(data)["orders"][0]
        old["status"] = "completed"
        old["weight"] = "1.2340"
        data["known_receipts"] = {old["order_id"]: old}
        data["tables"] = [t for t in data["tables"] if t["kind"] != "order_details"]
        data["tables"][1]["csv_text"] = data["tables"][1]["csv_text"].replace("1.23,11.3534", "0,10.1234")
        self.assertTrue(parse(data)["verified"])
        data["known_receipts"]["ord-123"]["filled_quantity"] = 3
        self.assertEqual(parse(data)["orders"][0]["status"], "unknown")

    def test_mixed_side_pending_orders_fail_closed(self):
        data = snapshot()
        data["tables"][2] = grid("orders", ["prodId", "sgn", "wei", "cumQty", "cnclT"],
                                 [("o1", ["005930", "1", "2", "0", ""]), ("o2", ["005930", "-1", "1", "0", ""])])
        data["tables"][3] = grid("order_details", ["ls", "state", "cumQty"],
                                 [("o1", ["L", "Working", "0"]), ("o2", ["L", "Working", "0"])], parent_symbol="005930")
        self.assertTrue(parse(data)["verified"])
        self.assertEqual(parse(data)["unresolved_symbols"], ["005930"])

    def test_pending_sell_is_deducted_once_by_planner(self):
        data = snapshot()
        data["tables"][2]["csv_text"] = data["tables"][2]["csv_text"].replace(",1,1.234,", ",-1,1.234,")
        data["tables"][1]["csv_text"] = data["tables"][1]["csv_text"].replace("1.23,11.3534", "-1.23,8.8934")
        out = parse(data)
        self.assertTrue(out["verified"], out["errors"])
        self.assertAlmostEqual(out["positions"]["005930"]["sellable_weight"], 10.1234)
        from backend.mirror_plan import plan_weight_changes
        planned = plan_weight_changes(
            [{"source_fingerprint": "fp", "symbol": "005930", "side": "sell", "quantity": 1}],
            source_fingerprint="fp", owned_quantities={"005930": 0}, source_prices={}, source_equity=100,
            destination_positions=out["positions"], pending_orders=out["pending_orders"],
            destination_verified=out["verified"])
        self.assertEqual(planned[0]["status"], "proposal", planned)
        self.assertEqual(planned[0]["weight"], "8.89")

    def test_prefixed_product_codes_need_no_stock_probes(self):
        data = snapshot()
        for table in data["tables"]:
            table["csv_text"] = table["csv_text"].replace("005930", "A005930")
            for row in table["rows"]:
                if "prodId" in row["cells"]:
                    row["cells"]["prodId"]["text"] = "A005930"
        data.pop("stock_checks", None)
        data["tables"] = data["tables"][:4]
        out = parse(data)
        self.assertTrue(out["verified"], out["errors"])
        self.assertIn("005930", out["positions"])

    def test_price_change_between_grids_does_not_block_planning(self):
        data = snapshot()
        data["tables"][0]["csv_text"] = data["tables"][0]["csv_text"].replace("10.1234", "10.124")
        self.assertTrue(parse(data)["verified"])

    def test_buy_snapshot_matches_planner_contract(self):
        from backend.mirror_plan import plan_weight_changes
        out = parse(snapshot())
        planned = plan_weight_changes(
            [{"source_fingerprint": "fp", "symbol": "005930", "side": "buy", "quantity": 1}],
            source_fingerprint="fp", owned_quantities={"005930": 15},
            source_prices={"005930": {"price": 1, "fresh": True}}, source_equity=100,
            destination_positions=out["positions"], pending_orders=out["pending_orders"],
            destination_verified=out["verified"])
        self.assertEqual(planned[0]["status"], "proposal", planned)
        self.assertEqual(planned[0]["weight"], "3.64")





    def test_incomplete_csv_rejected_for_positions_or_orders(self):
        for index in (0, 1, 2, 3):
            with self.subTest(index=index):
                data = snapshot()
                table = data["tables"][index]
                table.update(complete=False, csv_complete=True, filtered=False, total_rows=1)
                self.assertFalse(parse(data)["verified"])



    def test_receipts_only_need_no_positions_targets_or_constraints(self):
        data = snapshot()
        data["tables"] = data["tables"][2:4]
        data.pop("stock_checks", None)
        out = parse_receipts(data, "portfolio-13", "RFM13")
        self.assertTrue(out["verified"], out["errors"])
        self.assertEqual(out["positions"], {})
        self.assertEqual(out["pending_orders"], [])
        self.assertNotIn("constraints", out)
        self.assertEqual(out["orders"][0]["order_id"], "ord-123")

    def test_past_unknown_order_does_not_reserve_current_symbol(self):
        data = snapshot()
        data["tables"] = data["tables"][:3]
        data["tables"][2]["csv_text"] = data["tables"][2]["csv_text"].replace("2026-10-12", "2026-10-09")
        data["tables"][1]["csv_text"] = data["tables"][1]["csv_text"].replace("1.23,11.3534", "0,10.1234")
        out = parse(data)
        self.assertTrue(out["verified"], out["errors"])
        self.assertEqual(out["orders"][0]["status"], "unknown")
        self.assertEqual(out["unresolved_symbols"], [])

    def test_later_fill_does_not_invalidate_earlier_state_observation(self):
        data = snapshot()
        data["tables"][2]["csv_text"] = data["tables"][2]["csv_text"].replace(",2,", ",3,")
        out = parse(data)
        self.assertTrue(out["verified"], out["errors"])
        self.assertEqual(out["orders"][0]["filled_quantity"], 3)
        self.assertEqual(out["orders"][0]["status"], "working")

    def test_unaccepted_error_is_verified_rejection_with_sanitized_reason(self):
        data = snapshot()
        data["tables"][3] = grid("unaccepted", ["prodId", "state"], [("ord-123", ["005930", "Generated"])])
        data["tables"][3]["rows"][0]["cells"]["state"]["errors"] = ["종목 한도 초과 user@example.com 1234567890"]
        out = parse(data)
        self.assertEqual(out["orders"][0]["status"], "rejected")
        self.assertIn("종목 한도 초과", out["orders"][0]["reason"])
        self.assertNotIn("user@example.com", out["orders"][0]["reason"])
        self.assertNotIn("1234567890", out["orders"][0]["reason"])

    def test_generated_order_in_both_grids_does_not_count_as_duplicate(self):
        data = snapshot()
        data["tables"][3]["csv_text"] = data["tables"][3]["csv_text"].replace("Working", "Generated")
        error_grid = grid("unaccepted", ["prodId", "state"], [("ord-123", ["005930", "Generated"])])
        error_grid["rows"][0]["cells"]["state"]["errors"] = ["개별 종목 한도 초과"]
        data["tables"].append(error_grid)
        out = parse(data)
        self.assertTrue(out["verified"], out["errors"])
        self.assertEqual(out["orders"][0]["status"], "rejected")

    def test_account_date_and_origin_must_match(self):
        for key, value in [("account_key", "other"), ("contest", "Training"),
                           ("session_date", "2026-02-31"), ("url", "https://contest.timefolio.net.evil/")]:
            with self.subTest(key=key):
                data = snapshot()
                data[key] = value
                self.assertFalse(parse(data)["verified"])

    def test_nonfinite_and_negative_position_rejected(self):
        for value in ("NaN", "Infinity", "-1"):
            data = snapshot()
            data["tables"][0]["csv_text"] = data["tables"][0]["csv_text"].replace("10.1234", value)
            self.assertFalse(parse(data)["verified"])

    def test_input_not_mutated(self):
        data = snapshot()
        before = copy.deepcopy(data)
        parse(data)
        self.assertEqual(data, before)


if __name__ == "__main__":
    unittest.main()
