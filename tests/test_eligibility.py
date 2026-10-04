import base64
from copy import deepcopy
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from backend.eligibility import CACHE_AGE, EligibilityService, SOURCE_URLS, _parse_master, assess_master
from backend.kis import KST, KisError, PaperClient, Settings


def master(board, entries=None):
    if entries is None:
        entries = [("005930" if board == "kospi" else "035900", {})]
    width = 288 if board == "kospi" else 282
    offsets = {"low_liquidity": (77, 1), "halted": (121, 1), "liquidation": (122, 1),
               "managed": (123, 1), "warning_code": (124, 2), "preferred_code": (219, 1)} if board == "kospi" else {
               "low_liquidity": (77, 1), "halted": (116, 1), "liquidation": (117, 1),
               "managed": (118, 1), "warning_code": (119, 2), "preferred_code": (214, 1),
               "investment_caution": (91, 1)}
    lines = []
    for symbol, changes in entries:
        raw = bytearray(b"0" * width)
        raw[:9] = symbol.encode("ascii").ljust(9)
        raw[9:21] = b"KR7005930003"
        raw[21:61] = "종목 테스트".encode("cp949").ljust(40)
        values = {key: "0" if key == "preferred_code" else "00" if key == "warning_code" else "N"
                  for key in offsets}
        values.update(changes)
        for key, (offset, size) in offsets.items():
            raw[offset:offset + size] = values[key].encode("ascii").ljust(size)
        lines.append(bytes(raw))
    return archive(board, b"\r\n".join(lines) + b"\r\n")


def archive(board, contents, extra=False):
    result = io.BytesIO()
    with ZipFile(result, "w") as zipped:
        zipped.writestr(f"{board}_code.mst", contents)
        if extra:
            zipped.writestr("unexpected.txt", "unexpected")
    return result.getvalue()


def normal_row(board="KOSPI"):
    return {"board": board, "halted": False, "liquidation": False, "managed": False,
            "low_liquidity": False, "warning_code": "00", "preferred_code": "0",
            "investment_caution": False if board == "KOSDAQ" else None}


class EligibilityParserTests(unittest.TestCase):
    def test_official_byte_layouts_keep_alphanumeric_symbols_and_board_specific_flags(self):
        kospi = _parse_master(master("kospi", [("0126Z0", {"managed": "Y"}),
                                               ("005935", {"preferred_code": "1"})]), "kospi")
        self.assertEqual(kospi["0126Z0"], {**normal_row(), "managed": True})
        self.assertEqual(kospi["005935"]["preferred_code"], "1")
        kosdaq = _parse_master(master("kosdaq", [("035900", {"investment_caution": "Y", "warning_code": "02"})]), "kosdaq")
        self.assertEqual(kosdaq["035900"], {**normal_row("KOSDAQ"), "investment_caution": True, "warning_code": "02"})

    def test_unknown_or_missing_flags_remain_unknown_instead_of_becoming_false(self):
        rows = _parse_master(master("kospi", [("005930", {"halted": "?", "managed": " ",
                                                            "warning_code": "99", "preferred_code": "9"})]), "kospi")
        row = rows["005930"]
        self.assertIsNone(row["halted"])
        self.assertIsNone(row["managed"])
        self.assertEqual(row["warning_code"], "99")
        self.assertEqual(row["preferred_code"], "9")
        result = assess_master(row, "KOSPI")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(set(result["reasons"]), {"unknown_halted", "unknown_managed",
                                                   "unknown_warning_code", "unknown_preferred_code"})

    def test_wrong_archive_wrong_row_width_empty_and_duplicate_symbols_are_rejected(self):
        invalid = [b"not zip", master("kosdaq"), archive("kospi", b"short\n"),
                   archive("kospi", b""), master("kospi", [("005930", {}), ("005930", {})]),
                   archive("kospi", b"short\n", extra=True), master("kospi", [("0126z0", {})])]
        for payload in invalid:
            with self.subTest(payload_length=len(payload)), self.assertRaises(Exception):
                _parse_master(payload, "kospi")

    def test_all_master_rows_are_retained_including_non_six_digit_public_instruments(self):
        entries = [(str(100000 + i), {}) for i in range(400)] + [("Q530001", {})]
        parsed = _parse_master(master("kospi", entries), "kospi")
        self.assertEqual(set(parsed), {symbol for symbol, _ in entries})

    def test_assessment_excludes_confirmed_risks_and_does_not_equate_unknown_preference_with_common(self):
        cases = [("halted", True, "halted"), ("liquidation", True, "liquidation"),
                 ("managed", True, "managed"), ("low_liquidity", True, "low_liquidity"),
                 ("preferred_code", "1", "preferred_share"), ("preferred_code", "2", "preferred_share")]
        cases += [("warning_code", code, "market_warning") for code in ("01", "02", "03")]
        for field, value, reason in cases:
            with self.subTest(field=field, value=value):
                self.assertEqual(assess_master({**normal_row(), field: value}, "KOSPI"),
                                 {"status": "excluded", "reasons": [reason]})
        self.assertEqual(assess_master(normal_row(), "KOSPI"), {"status": "pass", "reasons": []})
        self.assertEqual(assess_master({**normal_row("KOSDAQ"), "investment_caution": True}, "KOSDAQ"),
                         {"status": "excluded", "reasons": ["investment_caution"]})
        self.assertEqual(assess_master({**normal_row(), "preferred_code": "9"}, "KOSPI")["status"], "unknown")

    def test_missing_values_board_mismatch_and_nonboolean_false_values_do_not_pass(self):
        for row, board in ((None, "KOSPI"), ({}, "KOSPI"), (normal_row(), "KOSDAQ"),
                           ({**normal_row(), "halted": 0}, "KOSPI"),
                           ({**normal_row(), "warning_code": None}, "KOSPI"),
                           ({**normal_row("KOSDAQ"), "investment_caution": None}, "KOSDAQ")):
            with self.subTest(row=row, board=board):
                self.assertEqual(assess_master(row, board)["status"], "unknown")

    def test_known_exclusion_is_preserved_alongside_unknown_fields(self):
        result = assess_master({**normal_row(), "halted": True, "warning_code": None}, "KOSPI")
        self.assertEqual(result, {"status": "excluded", "reasons": ["halted", "unknown_warning_code"]})


class EligibilityServiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name) / "eligibility"
        self.time = datetime(2026, 10, 4, 17, tzinfo=KST)
        self.payloads = {board: master(board) for board in ("kospi", "kosdaq")}
        self.fetcher = Mock(side_effect=lambda url: self.payloads["kospi" if "kospi_" in url else "kosdaq"])
        self.service = self.make_service()

    def make_service(self):
        return EligibilityService(self.directory, now=lambda: self.time, fetcher=self.fetcher)

    def test_construction_does_not_download_and_explicit_snapshot_fetches_both_boards(self):
        self.fetcher.assert_not_called()
        result = self.service.master_snapshot()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source_urls"], SOURCE_URLS)
        self.assertFalse(result["stale"])
        self.assertEqual(set(result["rows"]), {"005930", "035900"})
        self.assertEqual(self.fetcher.call_count, 2)
        self.assertEqual({path.name for path in self.directory.iterdir()}, {"masters.json", "master.lock"})

    def test_six_hour_cache_survives_restart_and_refreshes_at_boundary(self):
        first = self.service.master_snapshot()
        self.time += CACHE_AGE - timedelta(seconds=1)
        self.assertEqual(self.make_service().master_snapshot(), first)
        self.assertEqual(self.fetcher.call_count, 2)
        self.time += timedelta(seconds=1)
        refreshed = self.service.master_snapshot()
        self.assertEqual(refreshed["status"], "ok")
        self.assertNotEqual(refreshed["observed_at"], first["observed_at"])
        self.assertEqual(self.fetcher.call_count, 4)

    def test_snapshot_mutation_cannot_change_saved_or_memory_result(self):
        first = self.service.master_snapshot()
        first["rows"]["005930"]["halted"] = True
        first["source_urls"].clear()
        current = self.service.master_snapshot()
        self.assertFalse(current["rows"]["005930"]["halted"])
        self.assertEqual(current["source_urls"], SOURCE_URLS)

    def test_failed_second_board_does_not_publish_partial_update_and_keeps_old_snapshot(self):
        first = self.service.master_snapshot()
        saved = (self.directory / "masters.json").read_bytes()
        self.time += CACHE_AGE
        self.payloads["kospi"] = master("kospi", [("005930", {"halted": "Y"})])
        self.payloads["kosdaq"] = b"private-invalid-archive"
        failed = self.service.master_snapshot()
        self.assertEqual(failed["status"], "error")
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["rows"], first["rows"])
        self.assertEqual(failed["observed_at"], first["observed_at"])
        self.assertNotIn("private", json.dumps(failed))
        self.assertEqual((self.directory / "masters.json").read_bytes(), saved)

    def test_initial_network_failure_or_unknown_archive_does_not_report_success(self):
        for failure in (OSError("private-secret"), b"bad archive"):
            self.fetcher.side_effect = failure if isinstance(failure, Exception) else None
            self.fetcher.return_value = failure
            with self.subTest(failure=type(failure).__name__):
                result = self.service.master_snapshot()
                self.assertEqual(result["status"], "error")
                self.assertFalse(result["stale"])
                self.assertEqual(result["rows"], {})
                self.assertIsNone(result["observed_at"])
                self.assertNotIn("private", json.dumps(result))
                self.assertFalse((self.directory / "masters.json").exists())

    def test_cross_board_duplicate_symbol_rejects_whole_snapshot(self):
        self.payloads["kosdaq"] = master("kosdaq", [("005930", {})])
        result = self.service.master_snapshot()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["rows"], {})

    def test_saved_raw_structure_is_revalidated_before_using_cache(self):
        self.service.master_snapshot()
        path = self.directory / "masters.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["masters"]["kosdaq"] = base64.b64encode(archive("kosdaq", b"bad row\n")).decode("ascii")
        path.write_text(json.dumps(saved), encoding="utf-8")
        self.fetcher.reset_mock()
        result = self.make_service().master_snapshot()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(set(result["rows"]), {"005930", "035900"})
        self.assertEqual(self.fetcher.call_count, 2)

    def test_future_or_naive_cached_timestamps_are_not_used(self):
        self.service.master_snapshot()
        path = self.directory / "masters.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for stamp in ((self.time + timedelta(days=1)).isoformat(), self.time.replace(tzinfo=None).isoformat()):
            saved = deepcopy(original)
            saved["observed_at"] = stamp
            path.write_text(json.dumps(saved), encoding="utf-8")
            self.fetcher.reset_mock()
            with self.subTest(stamp=stamp):
                self.assertEqual(self.make_service().master_snapshot()["status"], "ok")
                self.assertEqual(self.fetcher.call_count, 2)

    def test_atomic_save_failure_keeps_previous_file_and_marks_old_result_stale(self):
        first = self.service.master_snapshot()
        saved = (self.directory / "masters.json").read_bytes()
        self.time += CACHE_AGE
        with patch("backend.eligibility.Path.replace", side_effect=OSError("private-disk-error")):
            result = self.service.master_snapshot()
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["stale"])
        self.assertEqual(result["rows"], first["rows"])
        self.assertEqual((self.directory / "masters.json").read_bytes(), saved)
        self.assertEqual({path.name for path in self.directory.iterdir()}, {"masters.json", "master.lock"})


class StockStatusTests(unittest.TestCase):
    def setUp(self):
        self.client = PaperClient(Settings("test-key", "test-secret"))
        self.output = {"stck_shrn_iscd": "0126Z0", "stck_prpr": "373500", "temp_stop_yn": "N",
                       "mang_issu_cls_code": "N", "sltr_yn": "N", "invt_caful_yn": "N",
                       "short_over_yn": "N", "mrkt_warn_cls_code": "00", "iscd_stat_cls_code": "55"}

    def read(self, output):
        with patch.object(self.client, "_get", return_value=({"output": output}, {})):
            return self.client.stock_status("0126Z0")

    def test_current_original_price_and_flags_are_validated_without_interpreting_status_55(self):
        with patch.object(self.client, "_get", return_value=({"output": self.output}, {})) as get:
            result = self.client.stock_status("0126Z0")
        self.assertEqual(result, {"symbol": "0126Z0", "current_price": "373500", "temp_halted": False,
                                  "managed": False, "liquidation": False, "investment_caution": False,
                                  "short_overheated": False, "warning_code": "00",
                                  "status": "ok", "unknown_fields": []})
        get.assert_called_once_with("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                                    {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "0126Z0"})
        self.assertEqual(self.read({**self.output, "iscd_stat_cls_code": "private-secret"}), result)

    def test_unknown_missing_or_invalid_price_and_flags_do_not_default_to_normal(self):
        fields = (("stck_prpr", "current_price"), ("temp_stop_yn", "temp_halted"),
                  ("mang_issu_cls_code", "managed"), ("sltr_yn", "liquidation"),
                  ("invt_caful_yn", "investment_caution"), ("short_over_yn", "short_overheated"),
                  ("mrkt_warn_cls_code", "warning_code"))
        for original, normalized in fields:
            for value in (None, "", "private-secret", [], True, 0):
                with self.subTest(field=original, value=value):
                    result = self.read({**self.output, original: value})
                    self.assertEqual(result["status"], "unknown")
                    self.assertIsNone(result[normalized])
                    self.assertIn(normalized, result["unknown_fields"])
                    self.assertNotIn("private", json.dumps(result))
            missing = {key: value for key, value in self.output.items() if key != original}
            self.assertEqual(self.read(missing)["status"], "unknown")
        for value in ("0", "-1", "NaN", "Infinity", "1e3", "1000.5"):
            with self.subTest(price=value):
                self.assertIsNone(self.read({**self.output, "stck_prpr": value})["current_price"])

    def test_known_risk_flags_and_warning_codes_are_preserved(self):
        result = self.read({**self.output, "temp_stop_yn": "Y", "mang_issu_cls_code": "Y",
                            "sltr_yn": "Y", "invt_caful_yn": "Y", "short_over_yn": "Y",
                            "mrkt_warn_cls_code": "03"})
        self.assertEqual(result["status"], "ok")
        self.assertTrue(all(result[key] for key in ("temp_halted", "managed", "liquidation",
                                                    "investment_caution", "short_overheated")))
        self.assertEqual(result["warning_code"], "03")
        self.assertEqual(self.read({**self.output, "mrkt_warn_cls_code": "99"})["status"], "unknown")

    def test_missing_identity_is_unknown_but_mismatch_and_malformed_output_fail(self):
        result = self.read({key: value for key, value in self.output.items() if key != "stck_shrn_iscd"})
        self.assertEqual(result["status"], "unknown")
        self.assertIn("symbol", result["unknown_fields"])
        for output in (None, {}, [], {**self.output, "stck_shrn_iscd": "005930"}):
            with self.subTest(output=output), self.assertRaises(KisError):
                self.read(output)

    def test_invalid_input_never_reaches_auth_or_network(self):
        for symbol in (None, 126, "0126z0", "0126_Z", "0126Z00", "０１２６Ｚ０"):
            with self.subTest(symbol=symbol), patch.object(self.client, "_get") as get:
                with self.assertRaises(KisError):
                    self.client.stock_status(symbol)
                get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
