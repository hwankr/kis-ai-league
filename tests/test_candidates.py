from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from backend.candidates import (CandidateService, InsufficientHistory, calculate_metrics,
                                completed_cutoff, index_series, stock_series)
from backend.kis import KST, KisError
from backend.request_gate import file_lock


NOW = datetime(2026, 10, 4, 17, tzinfo=KST)
END = date(2026, 10, 2)
DAYS = sorted(END - timedelta(days=i) for i in range(29) if (END - timedelta(days=i)).weekday() < 5)
START = DAYS[0] - timedelta(days=10)


def index_rows(days=DAYS):
    return [{"stck_bsop_date": day.strftime("%Y%m%d"), "bstp_nmix_prpr": str(100 + i * 2)}
            for i, day in enumerate(days)]


def stock_response(symbol="005930", days=DAYS):
    return {"output1": {"stck_shrn_iscd": symbol}, "output2": [
        {"stck_bsop_date": day.strftime("%Y%m%d"), "stck_oprc": str(100 + i * 5),
         "stck_hgpr": str(110 + i * 5), "stck_lwpr": str(90 + i * 5),
         "stck_clpr": str(100 + i * 5), "acml_vol": "10", "acml_tr_pbmn": str((i + 1) * 100)}
        for i, day in enumerate(days)]}


def universe():
    return {"status": "verified", "as_of": "2026-10-01", "checked_at": NOW.isoformat(),
            "source_url": "https://example.test/competition-universe", "count": 2, "error": None,
            "rows": [{"symbol": "005930", "name": "종목 A", "board": "KOSPI"},
                     {"symbol": "035720", "name": "종목 B", "board": "KOSDAQ"}]}


class FakeClient:
    def __init__(self):
        self.index_calls = []
        self.stock_calls = []
        self.index_data = {"0001": index_rows(), "1001": index_rows()}
        self.stock_data = {symbol: stock_response(symbol) for symbol in ("005930", "035720", "000660")}
        self.entered = threading.Event()
        self.release = None

    def index_daily(self, start, end, symbol="0001"):
        self.index_calls.append((start, end, symbol))
        self.entered.set()
        if self.release is not None and not self.release.wait(4):
            raise RuntimeError("test worker release timeout")
        data = self.index_data[symbol]
        if isinstance(data, Exception):
            raise data
        return deepcopy(data)

    def chart_daily(self, symbol, start, end):
        self.stock_calls.append((symbol, start, end))
        data = self.stock_data[symbol]
        if isinstance(data, Exception):
            raise data
        return deepcopy(data)


class PagingClient:
    def __init__(self):
        self.days = sorted(END - timedelta(days=i) for i in range(370)
                           if (END - timedelta(days=i)).weekday() < 5)[-253:]
        self.index_data = {code: index_rows(self.days) for code in ("0001", "1001")}
        self.stock_data = {symbol: stock_response(symbol, self.days) for symbol in ("005930", "035720")}
        self.index_calls, self.stock_calls = [], []
        self.index_backfill_failure = self.stock_backfill_failure = None
        self.on_stock = None

    @staticmethod
    def page(rows, start, end, limit):
        return deepcopy([row for row in rows
                         if start <= datetime.strptime(row["stck_bsop_date"], "%Y%m%d").date() <= end][-limit:])

    def index_daily(self, start, end, symbol="0001"):
        start, end = date.fromisoformat(start), date.fromisoformat(end)
        self.index_calls.append((start, end, symbol))
        if end < END and symbol == self.index_backfill_failure:
            raise KisError("선별용 지수 이력 실패")
        return self.page(self.index_data[symbol], start, end, 50)

    def chart_daily(self, symbol, start, end):
        self.stock_calls.append((symbol, start, end))
        if self.on_stock:
            self.on_stock()
        if end < END and symbol == self.stock_backfill_failure:
            raise KisError("선별용 종목 이력 실패")
        return {"output1": {"stck_shrn_iscd": symbol},
                "output2": self.page(self.stock_data[symbol]["output2"], start, end, 100)}


class RecordingSelector:
    def __init__(self, now):
        self.now = now
        self.active, self.policy_id, self.history_sessions = True, "test-policy-v1", 253
        self.calls = []
        self.on_select = None

    def enabled(self):
        return self.active

    def select(self, rows, features, client):
        self.calls.append(deepcopy(features))
        rank = 0
        for row in rows:
            error = features[row["symbol"]].get("error")
            if not error:
                rank += 1
            row["selection"] = {"status": "unverified" if error else "selected",
                                "reasons": [error] if error else [],
                                "rank": None if error else rank, "score": None if error else "1.5"}
        result = {"policy_id": self.policy_id, "checked_at": self.now().isoformat(), "status": "ready",
                  "master_observed_at": self.now().isoformat(), "label": "test", "score_label": "test",
                  "score_unit": "%p", "criteria": ["test"], "error": None,
                  "counts": {state: sum(row["selection"]["status"] == state for row in rows)
                             for state in ("selected", "reserve", "excluded", "unverified")}}
        if self.on_select:
            self.on_select()
        return result


class CandidateMetricTests(unittest.TestCase):
    def setUp(self):
        self.stock = stock_series(stock_response(), "005930", START, END)
        self.index = index_series(index_rows(), START, END)

    def test_returns_use_exact_five_and_twenty_session_baselines_and_pp_difference(self):
        result = calculate_metrics(self.stock, self.index, DAYS)
        self.assertEqual(result["close"], "200")
        self.assertEqual(result["return_5d_pct"], "14.2857")
        self.assertEqual(result["return_20d_pct"], "100.0000")
        self.assertEqual(result["excess_5d_pp"], "6.5934")
        self.assertEqual(result["excess_20d_pp"], "60.0000")

    def test_average_includes_latest_day_but_ratio_baseline_excludes_it(self):
        result = calculate_metrics(self.stock, self.index, DAYS)
        self.assertEqual(result["avg_turnover_20d"], "1150.00")
        self.assertEqual(result["turnover_ratio"], "2.0000")

    def test_missing_middle_session_is_not_compressed_even_with_older_extra_row(self):
        stock = deepcopy(self.stock)
        stock[START] = stock.pop(DAYS[9])
        self.assertEqual(len(stock), 21)
        with self.assertRaises(InsufficientHistory):
            calculate_metrics(stock, self.index, DAYS)

    def test_invalid_or_unaligned_session_windows_are_rejected(self):
        for days in (DAYS[:-1], DAYS + [END + timedelta(days=1)], DAYS[::-1], [DAYS[0]] + DAYS[:-1]):
            with self.subTest(days=days), self.assertRaises(KisError):
                calculate_metrics(self.stock, self.index, days)
        del self.index[DAYS[3]]
        with self.assertRaises(KisError):
            calculate_metrics(self.stock, self.index, DAYS)

    def test_zero_latest_volume_excludes_and_zero_previous_turnover_has_no_ratio(self):
        self.stock[END]["volume"] = Decimal(0)
        with self.assertRaises(InsufficientHistory):
            calculate_metrics(self.stock, self.index, DAYS)
        self.stock[END]["volume"] = Decimal(10)
        for day in DAYS[:-1]:
            self.stock[day]["turnover"] = Decimal(0)
        result = calculate_metrics(self.stock, self.index, DAYS)
        self.assertIsNone(result["turnover_ratio"])
        self.assertEqual(result["avg_turnover_20d"], "105.00")

    def test_stock_duplicate_wrong_symbol_and_invalid_dates_fail(self):
        original = stock_response()
        duplicate = deepcopy(original)
        duplicate["output2"].append(deepcopy(duplicate["output2"][0]))
        wrong_symbol = stock_response("000660")
        invalid_date = deepcopy(original)
        invalid_date["output2"][0]["stck_bsop_date"] = "20260230"
        outside_range = stock_response(days=[START - timedelta(days=1)])
        for response in (duplicate, wrong_symbol, invalid_date, outside_range, {"output2": [None]}):
            with self.subTest(response=response), self.assertRaises(KisError):
                stock_series(response, "005930", START, END)

    def test_stock_missing_or_invalid_numeric_values_fail_closed(self):
        for key, value in (("stck_clpr", "NaN"), ("stck_clpr", "Infinity"), ("stck_clpr", "0"),
                           ("stck_hgpr", "1"), ("acml_vol", "1.5"), ("acml_vol", "-1"),
                           ("acml_tr_pbmn", "-1"), ("acml_tr_pbmn", "1e3"),
                           ("acml_tr_pbmn", None), ("acml_tr_pbmn", "private-secret")):
            response = stock_response()
            response["output2"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(KisError) as caught:
                stock_series(response, "005930", START, END)
            self.assertNotIn("private-secret", str(caught.exception))

    def test_index_needs_twenty_one_valid_unique_days(self):
        duplicate = index_rows() + [index_rows()[0]]
        for rows in ([], index_rows()[:-1], duplicate, [None], index_rows() * 5):
            with self.subTest(rows=rows), self.assertRaises(KisError):
                index_series(rows, START, END)

    def test_backfill_parser_allows_short_pages_and_retains_stock_ohlc(self):
        self.assertEqual(index_series([], START, END, minimum=0), {})
        self.assertEqual(index_series(index_rows()[:1], START, END, minimum=0),
                         {DAYS[0]: Decimal(100)})
        self.assertEqual(self.stock[DAYS[0]], {"open": Decimal(100), "high": Decimal(110),
                         "low": Decimal(90), "close": Decimal(100), "volume": Decimal(10),
                         "turnover": Decimal(100)})
        for value in ("0", "-1", "NaN", "Infinity", "1e3", None, 123):
            rows = index_rows()
            rows[0]["bstp_nmix_prpr"] = value
            with self.subTest(value=value), self.assertRaises(KisError):
                index_series(rows, START, END)

    def test_completed_cutoff_uses_kst_1600_boundary(self):
        self.assertEqual(completed_cutoff(datetime(2026, 10, 2, 15, 59, 59, tzinfo=KST)), date(2026, 10, 1))
        self.assertEqual(completed_cutoff(datetime(2026, 10, 2, 16, tzinfo=KST)), END)
        self.assertEqual(completed_cutoff(datetime(2026, 10, 2, 6, 59, tzinfo=timezone.utc)), date(2026, 10, 1))
        self.assertEqual(completed_cutoff(datetime(2026, 10, 2, 7, tzinfo=timezone.utc)), END)


class CandidateServiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "config.toml"
        self.config.write_text("default_account='paper'\n[accounts.paper]\napp_key='test-key'\n"
                               "app_secret='test-secret'\n[market_data]\naccount='paper'\n", encoding="utf-8")
        self.current_universe = universe()
        self.now = NOW
        self.client = FakeClient()
        self.factory = Mock(return_value=self.client)
        self.selector = Mock()
        self.selector.enabled.return_value = False
        self.service = self.make_service()

    def make_service(self):
        return CandidateService(self.config, directory=self.root / "candidates",
                                universe_loader=lambda: deepcopy(self.current_universe),
                                client_factory=self.factory, now=lambda: self.now, selector=self.selector)

    def finish(self, service=None):
        service = service or self.service
        service.start()
        if service.worker is not None:
            service.worker.join(5)
            self.assertFalse(service.worker.is_alive(), "Candidate worker did not finish")
        return service.snapshot()

    def test_snapshot_and_unverified_start_do_not_construct_broker_client(self):
        self.assertEqual(self.service.snapshot()["status"], "idle")
        self.current_universe.update(status="unverified", error="대상 목록 확인 필요")
        result = self.finish()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "대상 목록 확인 필요")
        self.assertEqual(result["rows"], [])
        self.factory.assert_not_called()

    def test_complete_collects_all_rows_and_market_specific_indices_on_same_date(self):
        result = self.finish()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["as_of"], END.isoformat())
        self.assertEqual(result["progress"], {"completed": 2, "total": 2})
        self.assertFalse(result["stale"])
        self.assertEqual({row["symbol"] for row in result["rows"]}, {"005930", "035720"})
        self.assertTrue(all(row["status"] == "ok" for row in result["rows"]))
        self.assertEqual({call[2] for call in self.client.index_calls}, {"0001", "1001"})
        self.assertTrue(all(call[1] == "2026-10-04" for call in self.client.index_calls))
        self.assertTrue(all(call[2] == END for call in self.client.stock_calls))
        self.assertNotIn("test-secret", json.dumps(result))
        result["rows"][0]["close"] = "broken"
        self.assertEqual(self.service.snapshot()["rows"][0]["close"], "200")

    def test_repeat_and_restart_reuse_daily_cache_without_more_api_requests(self):
        first = self.finish()
        counts = (len(self.client.index_calls), len(self.client.stock_calls))
        self.assertEqual(self.finish()["rows"], first["rows"])
        restored = self.make_service()
        self.assertEqual(restored.snapshot()["rows"], first["rows"])
        self.assertEqual(self.finish(restored)["status"], "complete")
        self.assertEqual((len(self.client.index_calls), len(self.client.stock_calls)), counts)

    def test_snapshot_and_restart_become_stale_when_completed_cutoff_advances(self):
        self.now = datetime(2026, 10, 2, 16, 10, tzinfo=KST)
        first = self.finish()
        self.assertEqual(first["requested_through"], "2026-10-02")
        self.assertFalse(first["stale"])
        calls = (len(self.client.index_calls), len(self.client.stock_calls))
        self.now = datetime(2026, 10, 3, 15, 59, 59, tzinfo=KST)
        self.assertFalse(self.service.snapshot()["stale"])
        self.assertFalse(self.make_service().snapshot()["stale"])
        self.now = datetime(2026, 10, 3, 16, tzinfo=KST)
        for service in (self.service, self.make_service()):
            with self.subTest(restored=service is not self.service):
                snapshot = service.snapshot()
                self.assertEqual(snapshot["status"], "complete")
                self.assertTrue(snapshot["stale"])
                self.assertEqual(snapshot["rows"], first["rows"])
                self.assertEqual(snapshot["as_of"], "2026-10-02")
        self.assertEqual((len(self.client.index_calls), len(self.client.stock_calls)), calls)

    def test_delayed_index_session_recovers_after_60_seconds_but_reuses_cache_before_then(self):
        self.now = datetime(2026, 10, 5, 16, tzinfo=KST)
        initial_time = self.now
        first = self.finish()
        self.assertEqual(first["requested_through"], "2026-10-05")
        self.assertEqual(first["as_of"], "2026-10-02")
        newer_days = DAYS + [date(2026, 10, 5)]
        self.client.index_data = {code: index_rows(newer_days) for code in ("0001", "1001")}
        self.client.stock_data = {symbol: stock_response(symbol, newer_days)
                                  for symbol in ("005930", "035720")}
        self.now = initial_time + timedelta(seconds=59)
        cached = self.finish()
        self.assertEqual(cached["as_of"], "2026-10-02")
        self.assertEqual((len(self.client.index_calls), len(self.client.stock_calls)), (2, 2))
        self.now = initial_time + timedelta(seconds=60)
        refreshed = self.finish()
        self.assertEqual(refreshed["status"], "complete")
        self.assertEqual(refreshed["as_of"], "2026-10-05")
        self.assertEqual(refreshed["requested_through"], "2026-10-05")
        self.assertFalse(refreshed["stale"])
        self.assertTrue(all(row["status"] == "ok" and row["close"] == "205"
                            and row["as_of"] == "2026-10-05" for row in refreshed["rows"]))
        self.assertEqual((len(self.client.index_calls), len(self.client.stock_calls)), (4, 4))
        self.assertTrue(all(call[2] == date(2026, 10, 5) for call in self.client.stock_calls[-2:]))
        self.assertEqual(self.make_service().snapshot()["as_of"], "2026-10-05")

    def test_expired_index_cache_does_not_expire_stock_cache_for_unchanged_session(self):
        first = self.finish()
        self.now += timedelta(seconds=60)
        result = self.finish()
        self.assertEqual(result["rows"], first["rows"])
        self.assertEqual((len(self.client.index_calls), len(self.client.stock_calls)), (4, 2))

    def test_all_350_candidates_are_processed_independently_of_20_symbol_watchlist(self):
        self.current_universe["rows"] = [
            {"symbol": str(100000 + i), "name": f"대상 {i}", "board": "KOSPI" if i % 2 else "KOSDAQ"}
            for i in range(350)]
        self.current_universe["count"] = 350
        symbols = [row["symbol"] for row in self.current_universe["rows"]]
        with self.config.open("a", encoding="utf-8") as stream:
            stream.write("symbols=" + json.dumps(symbols[:20]) + "\n")
        self.client.stock_data = {symbol: stock_response(symbol) for symbol in symbols}
        result = self.finish()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["progress"], {"completed": 350, "total": 350})
        self.assertEqual(len(result["rows"]), 350)
        self.assertEqual({row["symbol"] for row in result["rows"]}, set(symbols))
        self.assertTrue(all(row["status"] == "ok" for row in result["rows"]))
        self.assertEqual([call[0] for call in self.client.stock_calls], symbols)
        self.assertEqual(len(self.client.index_calls), 2)

    def test_concurrent_start_reuses_the_live_worker(self):
        self.client.release = threading.Event()
        self.addCleanup(self.client.release.set)
        first = self.service.start()
        self.assertEqual(first["status"], "running")
        self.assertTrue(self.client.entered.wait(2))
        worker = self.service.worker
        try:
            second = self.service.start()
            self.assertEqual(second["status"], "running")
            self.assertIs(self.service.worker, worker)
            self.assertEqual(len(self.client.index_calls), 1)
        finally:
            self.client.release.set()
            worker.join(5)
        self.assertEqual(self.service.snapshot()["status"], "complete")

    def test_stock_failure_and_missing_history_do_not_stop_other_candidates(self):
        self.current_universe["rows"].append({"symbol": "000660", "name": "종목 C", "board": "KOSPI"})
        self.current_universe["count"] = 3
        self.client.stock_data["005930"] = KisError("일봉 조회 실패")
        self.client.stock_data["035720"]["output2"].pop(8)
        result = self.finish()
        self.assertEqual(result["status"], "complete")
        self.assertEqual([row["status"] for row in result["rows"]], ["error", "excluded", "ok"])
        self.assertEqual(result["error"], "1종목 조회 실패")
        self.assertEqual(result["progress"], {"completed": 3, "total": 3})
        self.assertTrue(all(row["close"] is None for row in result["rows"][:2]))

    def test_fatal_index_failure_preserves_previous_complete_rows_and_disk_result(self):
        first = self.finish()
        saved = (self.service.directory / "latest.json").read_bytes()
        self.now += timedelta(days=1)
        self.client.index_data["1001"] = KisError("지수 조회 실패")
        result = self.finish()
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["stale"])
        self.assertEqual(result["rows"], first["rows"])
        self.assertEqual((self.service.directory / "latest.json").read_bytes(), saved)

    def test_index_latest_date_or_middle_session_mismatch_blocks_all_stock_requests(self):
        for replacement in (DAYS[:-1] + [END + timedelta(days=1)],
                            [DAYS[0] - timedelta(days=1)] + DAYS[1:]):
            with self.subTest(replacement=replacement):
                self.client.index_data["1001"] = index_rows(replacement)
                cache = self.service.directory / "daily" / "index-1001.json"
                cache.unlink(missing_ok=True)
                result = self.finish()
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["rows"], [])
        self.assertEqual(self.client.stock_calls, [])

    def test_invalid_raw_stock_cache_is_refetched_and_validated(self):
        self.finish()
        path = self.service.directory / "daily" / "stock-005930.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["data"]["output2"][0]["stck_clpr"] = "NaN"
        path.write_text(json.dumps(saved), encoding="utf-8")
        self.client.stock_calls.clear()
        result = self.finish()
        self.assertEqual(result["status"], "complete")
        self.assertEqual([call[0] for call in self.client.stock_calls], ["005930"])
        self.assertEqual(result["rows"][0]["close"], "200")

    def test_invalid_raw_index_cache_is_refetched_before_reusing_stock_data(self):
        self.finish()
        path = self.service.directory / "daily" / "index-0001.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["data"][0]["bstp_nmix_prpr"] = "0"
        path.write_text(json.dumps(saved), encoding="utf-8")
        self.client.index_calls.clear()
        self.client.stock_calls.clear()
        self.assertEqual(self.finish()["status"], "complete")
        self.assertEqual([call[2] for call in self.client.index_calls], ["0001"])
        self.assertEqual(self.client.stock_calls, [])

    def test_corrupt_or_nonfinite_saved_snapshot_is_not_restored(self):
        self.finish()
        path = self.service.directory / "latest.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["state"]["rows"][0]["close"] = "NaN"
        path.write_text(json.dumps(saved), encoding="utf-8")
        self.assertEqual(self.make_service().snapshot()["rows"], [])
        path.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.make_service().snapshot()["rows"], [])

    def test_saved_snapshot_rejects_invalid_metrics_dates_and_market_metadata(self):
        self.finish()
        path = self.service.directory / "latest.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for changes in ({"close": "-1"}, {"close": "0"}, {"close": None}, {"return_5d_pct": None},
                        {"avg_turnover_20d": "-1"}, {"turnover_ratio": "-1"},
                        {"as_of": "2026-09-30"}, {"board": "KOSDAQ"}, {"name": "wrong name"},
                        {"status": "excluded"}):
            saved = deepcopy(original)
            saved["state"]["rows"][0].update(changes)
            path.write_text(json.dumps(saved), encoding="utf-8")
            with self.subTest(changes=changes):
                self.assertEqual(self.make_service().snapshot()["rows"], [])

    def test_saved_snapshot_requires_valid_requested_through_not_earlier_than_as_of(self):
        self.finish()
        path = self.service.directory / "latest.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for value in (None, "invalid", "2026-02-30", "2026-10-01"):
            saved = deepcopy(original)
            saved["state"]["requested_through"] = value
            path.write_text(json.dumps(saved), encoding="utf-8")
            with self.subTest(value=value):
                self.assertEqual(self.make_service().snapshot()["rows"], [])
        del original["state"]["requested_through"]
        path.write_text(json.dumps(original), encoding="utf-8")
        self.assertEqual(self.make_service().snapshot()["rows"], [])

    def test_changed_universe_marks_existing_rows_stale_and_prevents_restore(self):
        first = self.finish()
        self.current_universe["rows"] = self.current_universe["rows"][:1]
        self.current_universe["count"] = 1
        snapshot = self.service.snapshot()
        self.assertTrue(snapshot["stale"])
        self.assertEqual(snapshot["rows"], first["rows"])
        self.assertIn("변경", snapshot["error"])
        self.assertEqual(self.make_service().snapshot()["rows"], [])

    def test_universe_changed_during_collection_is_not_published(self):
        self.client.release = threading.Event()
        self.service.start()
        self.assertTrue(self.client.entered.wait(2))
        try:
            self.current_universe["as_of"] = "2026-10-02"
        finally:
            self.client.release.set()
            self.service.worker.join(5)
        result = self.service.snapshot()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["rows"], [])
        self.assertFalse((self.service.directory / "latest.json").exists())

    def test_universe_verification_revoked_during_run_is_not_published(self):
        self.client.release = threading.Event()
        self.service.start()
        self.assertTrue(self.client.entered.wait(2))
        try:
            self.current_universe.update(status="unverified", error="출처 확인 실패")
        finally:
            self.client.release.set()
            self.service.worker.join(5)
        result = self.service.snapshot()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["rows"], [])
        self.assertFalse((self.service.directory / "latest.json").exists())

    def test_unverified_universe_marks_previous_snapshot_stale_and_prevents_restore(self):
        self.finish()
        self.current_universe.update(status="unverified", error="출처 확인 실패")
        self.assertTrue(self.service.snapshot()["stale"])
        self.assertEqual(self.make_service().snapshot()["rows"], [])

    def test_shared_run_lock_rejects_duplicate_server_work_without_broker_call(self):
        with file_lock(self.service.directory / "run.lock"):
            result = self.finish()
        self.assertEqual(result["status"], "error")
        self.assertIn("다른 서버", result["error"])
        self.factory.assert_not_called()


class CandidateSelectionIntegrationTests(unittest.TestCase):
    make_service = CandidateServiceTests.make_service
    finish = CandidateServiceTests.finish

    def setUp(self):
        CandidateServiceTests.setUp(self)
        self.client = PagingClient()
        self.factory.return_value = self.client
        self.selector = RecordingSelector(lambda: self.now)
        self.service = self.make_service()

    def test_backfill_253_sessions_feeds_selector_and_reuses_cache_on_repeat_and_restart(self):
        result = self.finish()
        self.assertEqual(result["status"], "complete", result["error"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["screening"]["policy_id"], self.selector.policy_id)
        self.assertTrue(all(row["status"] == "ok" and row["selection"]["status"] == "selected"
                            for row in result["rows"]))
        feature = self.selector.calls[0]["005930"]
        self.assertIsNotNone(feature["excess_12to7m_pp"])
        self.assertAlmostEqual(float(feature["excess_12to7m_pp"]), 378)
        self.assertEqual(feature["atr14"], "20.0000")
        self.assertEqual(result["rows"][0]["close"], "1360")
        self.assertEqual(result["rows"][0]["return_20d_pct"], "7.9365")
        self.assertEqual(len(self.client.index_calls), 12)  # each: latest 50 + five boundary pages
        self.assertEqual(len(self.client.stock_calls), 6)  # each: latest 100 + two boundary pages
        history_start = END - timedelta(days=600)
        self.assertTrue(all(call[0] == history_start for call in self.client.index_calls if call[1] < END))
        self.assertTrue(all(call[1] == history_start for call in self.client.stock_calls if call[2] < END))
        saved = json.loads((self.service.directory / "history" / "stock-005930.json").read_text())
        self.assertEqual(len(saved["rows"]), 253)
        self.assertEqual(set(saved["rows"][END.isoformat()]), {"open", "high", "low", "close", "volume", "turnover"})
        self.assertEqual(self.finish()["rows"], result["rows"])
        calls = len(self.selector.calls)
        self.factory.reset_mock()
        restored = self.make_service()
        self.assertEqual(restored.snapshot()["screening"], result["screening"])
        self.assertEqual(restored.snapshot()["rows"], result["rows"])
        self.assertEqual(len(self.selector.calls), calls)
        self.factory.assert_not_called()
        self.assertEqual(self.finish(restored)["status"], "complete")
        self.assertEqual((len(self.client.index_calls), len(self.client.stock_calls)), (12, 6))

    def test_real_selector_uses_extended_features_and_latest_raw_price_without_network_on_get(self):
        from backend.candidate_selection import CandidateSelector

        policy = self.root / "selection.json"
        policy.write_text(json.dumps({"version": 1, "variant": "rs12to7m_top20", "maximum_shortlist": 20,
                                     "average_turnover_20d_krw": 10_000_000_000, "minimum_raw_price_krw": 1000,
                                     "decision": "test", "evidence_path": "test-only"}), encoding="utf-8")
        master = {row["symbol"]: {"board": row["board"], "halted": False, "liquidation": False,
                                 "managed": False, "low_liquidity": False, "investment_caution": False,
                                 "warning_code": "00", "preferred_code": "0"}
                  for row in self.current_universe["rows"]}
        eligibility = Mock()
        eligibility.master_snapshot.return_value = {"status": "ok", "stale": False, "rows": master,
                                                    "observed_at": self.now.isoformat(), "error": None}
        for data in self.client.stock_data.values():
            for row in data["output2"]:
                row["acml_tr_pbmn"] = "20000000000"
        self.client.stock_status = Mock(side_effect=lambda symbol: {
            "symbol": symbol, "status": "ok", "unknown_fields": [],
            "current_price": "1360" if symbol == "005930" else "999",
            "temp_halted": False, "managed": False, "liquidation": False,
            "investment_caution": False, "short_overheated": False, "warning_code": "00"})
        self.selector = CandidateSelector(policy_path=policy, eligibility=eligibility, now=lambda: self.now)
        self.service = self.make_service()
        self.service.snapshot()
        eligibility.master_snapshot.assert_not_called()
        self.factory.assert_not_called()
        result = self.finish()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["screening"]["counts"], {"selected": 1, "reserve": 0, "excluded": 1, "unverified": 0})
        self.assertEqual(result["rows"][0]["selection"]["rank"], 1)
        self.assertEqual(result["rows"][0]["selection"]["score"], "378.00")
        self.assertEqual(result["rows"][1]["close"], "1360")
        self.assertIn("현재가 1,000원 미만", result["rows"][1]["selection"]["reasons"])
        self.factory.reset_mock()
        self.assertFalse(self.make_service().snapshot()["stale"])
        self.service.snapshot()
        self.factory.assert_not_called()
        eligibility.master_snapshot.assert_called_once_with()
        self.assertEqual(self.client.stock_status.call_count, 2)

    def test_disabled_policy_keeps_original_contract_without_history_or_selection(self):
        self.selector.active = False
        result = self.finish()
        self.assertEqual(result["status"], "complete")
        self.assertNotIn("screening", result)
        self.assertTrue(all("selection" not in row for row in result["rows"]))
        self.assertEqual(self.selector.calls, [])
        self.assertEqual((len(self.client.index_calls), len(self.client.stock_calls)), (2, 2))
        self.assertFalse((self.service.directory / "history").exists())

    def test_stock_backfill_failure_preserves_original_metrics_and_other_stock_features(self):
        self.client.stock_backfill_failure = "005930"
        result = self.finish()
        self.assertEqual(result["status"], "complete")
        self.assertTrue(all(row["status"] == "ok" for row in result["rows"]))
        self.assertEqual(result["rows"][0]["return_20d_pct"], "7.9365")
        self.assertEqual(result["rows"][0]["selection"]["status"], "unverified")
        self.assertEqual(self.selector.calls[0]["005930"], {"error": "선별용 종목 이력 실패"})
        self.assertIsNotNone(self.selector.calls[0]["035720"]["excess_12to7m_pp"])

    def test_index_backfill_failure_only_blocks_affected_market_selection(self):
        self.client.index_backfill_failure = "0001"
        result = self.finish()
        self.assertEqual(result["status"], "complete")
        self.assertTrue(all(row["status"] == "ok" for row in result["rows"]))
        self.assertEqual(self.selector.calls[0]["005930"], {"error": "선별용 지수 이력 실패"})
        self.assertIsNotNone(self.selector.calls[0]["035720"]["excess_12to7m_pp"])
        self.assertEqual(sum(call[0] == "005930" for call in self.client.stock_calls), 1)

    def test_ipo_history_does_not_replace_missing_dates_with_older_observations(self):
        self.client.stock_data["005930"]["output2"] = self.client.stock_data["005930"]["output2"][-40:]
        result = self.finish()
        self.assertEqual(result["rows"][0]["status"], "ok")
        self.assertEqual(result["rows"][0]["selection"]["status"], "unverified")
        self.assertIn("error", self.selector.calls[0]["005930"])
        self.assertEqual(result["rows"][1]["selection"]["status"], "selected")

    def test_policy_change_or_missing_saved_screening_is_stale_without_network(self):
        first = self.finish()
        self.factory.reset_mock()
        self.selector.policy_id = "test-policy-v2"
        for service in (self.service, self.make_service()):
            result = service.snapshot()
            self.assertTrue(result["stale"])
            self.assertEqual(result["rows"], first["rows"])
            self.assertIn("전체 조회", result["error"])
        self.selector.policy_id = "test-policy-v1"
        path = self.service.directory / "latest.json"
        payload = json.loads(path.read_text())
        del payload["state"]["screening"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertTrue(self.make_service().snapshot()["stale"])
        self.factory.assert_not_called()

    def test_corrupt_screening_is_discarded_without_losing_prices_or_disabling_retry(self):
        first = self.finish()
        path = self.service.directory / "latest.json"
        original = json.loads(path.read_text())
        expected_rows = [{key: value for key, value in row.items() if key != "selection"} for row in first["rows"]]
        mutations = [
            ("screening-type", lambda state: state.update(screening=[])),
            ("status", lambda state: state["screening"].update(status="unknown")),
            ("label", lambda state: state["screening"].update(score_label=None)),
            ("criteria", lambda state: state["screening"].update(criteria=[None])),
            ("missing-time", lambda state: state["screening"].pop("checked_at")),
            ("naive-time", lambda state: state["screening"].update(checked_at="2026-10-04T17:00:00")),
            ("master-time", lambda state: state["screening"].update(master_observed_at="2026-10-04T17:00:00")),
            ("bad-time", lambda state: state["screening"].update(master_observed_at="2026-02-30T00:00:00Z")),
            ("counts", lambda state: state["screening"]["counts"].update(selected=3)),
            ("boolean-count", lambda state: state["screening"]["counts"].update(reserve=False)),
            ("missing-selection", lambda state: state["rows"][0].pop("selection")),
            ("duplicate-rank", lambda state: state["rows"][0]["selection"].update(rank=2)),
            ("rank-gap", lambda state: state["rows"][0]["selection"].update(rank=3)),
            ("boolean-rank", lambda state: state["rows"][0]["selection"].update(rank=True)),
            ("missing-rank", lambda state: state["rows"][0]["selection"].update(rank=None)),
            ("missing-score", lambda state: state["rows"][0]["selection"].update(score=None)),
            ("nonfinite-score", lambda state: state["rows"][0]["selection"].update(score="NaN")),
            ("overflow-score", lambda state: state["rows"][0]["selection"].update(score="1e999")),
            ("bad-reasons", lambda state: state["rows"][0]["selection"].update(reasons=[None])),
        ]
        self.factory.reset_mock()
        for name, mutate in mutations:
            payload = deepcopy(original)
            mutate(payload["state"])
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.subTest(name=name):
                restored = self.make_service()
                result = restored.snapshot()
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["universe"]["status"], "verified")
                self.assertEqual(result["rows"], expected_rows)
                self.assertNotIn("screening", result)
                self.assertTrue(result["stale"])
                self.assertIn("전체 조회", result["error"])
        self.factory.assert_not_called()
        retried = self.finish(restored)
        self.assertEqual(retried["status"], "complete")
        self.assertFalse(retried["stale"])
        self.assertEqual(retried["screening"], first["screening"])
        self.assertEqual(retried["rows"], first["rows"])
        self.assertEqual((len(self.client.index_calls), len(self.client.stock_calls)), (12, 6))

    def test_selection_expires_at_six_hours_in_snapshot_and_after_restart(self):
        first = self.finish()
        self.factory.reset_mock()
        self.now += timedelta(hours=6, seconds=-1)
        self.assertFalse(self.service.snapshot()["stale"])
        self.now += timedelta(seconds=1)
        for service in (self.service, self.make_service()):
            result = service.snapshot()
            self.assertTrue(result["stale"])
            self.assertEqual(result["rows"], first["rows"])
            self.assertIn("전체 조회", result["error"])
        self.factory.assert_not_called()

    def test_invalid_policy_is_reported_before_constructing_broker(self):
        self.selector.enabled = Mock(side_effect=KisError("선별 정책 형식 오류"))
        result = self.finish()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "선별 정책 형식 오류")
        self.factory.assert_not_called()

    def test_fresh_summary_does_not_extend_the_age_of_master_or_quote_observations(self):
        self.finish()
        path = self.service.directory / "latest.json"
        original = json.loads(path.read_text())
        initial_now = self.now
        for source in ("master", "quote"):
            payload = deepcopy(original)
            old = (initial_now - timedelta(hours=6, seconds=-60)).isoformat()
            if source == "master":
                payload["state"]["screening"]["master_observed_at"] = old
            else:
                payload["state"]["rows"][0]["selection"]["status_observed_at"] = old
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.now = initial_now
            restored = self.make_service()
            with self.subTest(source=source):
                self.assertFalse(restored.snapshot()["stale"])
                self.now += timedelta(seconds=60)
                self.assertTrue(restored.snapshot()["stale"])
                self.assertIn("전체 조회", restored.snapshot()["error"])

    def test_policy_changes_during_collection_or_selection_preserve_previous_disk_result(self):
        first = self.finish()
        path = self.service.directory / "latest.json"
        saved = path.read_bytes()
        self.selector.on_select = lambda: setattr(self.selector, "policy_id", "changed-on-select")
        result = self.finish()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["rows"], first["rows"])
        self.assertEqual(path.read_bytes(), saved)
        self.selector.policy_id = "test-policy-v1"
        self.selector.on_select = None
        self.client.on_stock = lambda: setattr(self.selector, "policy_id", "changed-on-fetch")
        (self.service.directory / "daily" / "stock-005930.json").unlink()
        selection_calls = len(self.selector.calls)
        result = self.finish()
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(self.selector.calls), selection_calls)
        self.assertEqual(result["rows"], first["rows"])
        self.assertEqual(path.read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
