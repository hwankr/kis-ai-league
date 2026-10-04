from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.candidate_history import HistoryCache, MAX_BYTES, MAX_PAGES
from backend.kis import KisError


def series(count=80, kind="stock"):
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(count)]
    if kind == "index":
        return {day: Decimal(100 + i) for i, day in enumerate(days)}
    return {day: {"open": Decimal(100 + i), "high": Decimal(102 + i), "low": Decimal(97 + i),
                   "close": Decimal(101 + i), "volume": Decimal(10), "turnover": Decimal(10000 + i)}
            for i, day in enumerate(days)}


def page_fetcher(rows, width):
    def fetch(cursor):
        days = [day for day in sorted(rows) if day <= cursor][-width:]
        return {day: deepcopy(rows[day]) for day in days}
    return Mock(side_effect=fetch)


class CandidateHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.cache = HistoryCache(self.directory)

    def test_same_asof_and_next_session_reuse_overlapping_cache_without_api(self):
        for kind, symbol in (("stock", "0126Z0"), ("index", "0001")):
            rows = series(81, kind)
            days = sorted(rows)
            self.cache.put(kind, symbol, {day: rows[day] for day in days[:-1]})
            forbidden = Mock(side_effect=AssertionError("unexpected API request"))
            required = days[20:80]
            first = self.cache.extend(kind, symbol, {day: rows[day] for day in days[60:80]}, required, forbidden)
            self.assertEqual(first, {day: rows[day] for day in required})
            restarted = HistoryCache(self.directory)
            required = days[21:81]
            second = restarted.extend(kind, symbol, {day: rows[day] for day in days[61:81]}, required, forbidden)
            self.assertEqual(second, {day: rows[day] for day in required})
            forbidden.assert_not_called()
        long_index = series(670, "index")
        dates = sorted(long_index)
        self.cache.put("index", "1001", long_index)
        forbidden = Mock(side_effect=AssertionError("unexpected seeded index request"))
        seeded = self.cache.extend("index", "1001", {day: long_index[day] for day in dates[-50:]}, 253, forbidden)
        self.assertEqual(seeded, {day: long_index[day] for day in dates[-253:]})
        forbidden.assert_not_called()

    def test_changed_overlap_discards_all_old_adjusted_values_and_backfills_from_api(self):
        original = series(12)
        self.cache.put("stock", "005930", original)
        adjusted = deepcopy(original)
        for row in adjusted.values():
            for field in ("open", "high", "low", "close"):
                row[field] *= 2
        days = sorted(adjusted)
        fetch = page_fetcher(adjusted, 4)
        result = self.cache.extend("stock", "005930", {day: adjusted[day] for day in days[-3:]}, days, fetch)
        self.assertEqual(result, adjusted)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [days[9], days[6], days[3]])

    def test_boundary_conflict_or_api_error_preserves_previous_file(self):
        original = series(12)
        self.cache.put("stock", "005930", original)
        path = self.directory / "stock-005930.json"
        saved = path.read_bytes()
        changed = deepcopy(original)
        days = sorted(changed)
        for day in days[-3:]:
            changed[day]["volume"] += 1
        # 기존 캐시가 폐기된 뒤 원본 페이지의 경계 volume과 새 최신값이 충돌한다.
        with self.assertRaisesRegex(KisError, "경계"):
            self.cache.extend("stock", "005930", {day: changed[day] for day in days[-3:]}, days,
                              page_fetcher(original, 4))
        self.assertEqual(path.read_bytes(), saved)
        failure = KisError("일봉 조회 실패")
        with self.assertRaises(KisError) as caught:
            self.cache.extend("stock", "005930", {day: changed[day] for day in days[-3:]}, days,
                              Mock(side_effect=failure))
        self.assertIs(caught.exception, failure)
        self.assertEqual(path.read_bytes(), saved)
        with patch("backend.candidate_history.Path.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.cache.extend("stock", "005930", {day: changed[day] for day in days[-3:]}, days,
                                  page_fetcher(changed, 4))
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual({entry.name for entry in self.directory.iterdir()}, {"stock-005930.json", "stock-005930.lock"})

    def test_ipo_empty_or_repeated_boundary_returns_short_history_without_compression(self):
        all_rows = series(20)
        days = sorted(all_rows)
        short = {day: all_rows[day] for day in days[-3:]}
        for response in ({}, {days[-3]: all_rows[days[-3]]}):
            fetch = Mock(return_value=response)
            with self.subTest(response=response):
                result = self.cache.extend("stock", "005930", short, days, fetch)
                self.assertEqual(result, short)
                fetch.assert_called_once_with(days[-3])
        missing = dict(all_rows)
        del missing[days[8]]
        forbidden = Mock(side_effect=AssertionError("cannot fill a middle gap by fetching older data"))
        self.assertEqual(self.cache.extend("stock", "000660", missing, days, forbidden), missing)
        forbidden.assert_not_called()

    def test_tampered_cache_is_ignored_and_rebuilt_using_validated_api_pages(self):
        rows = series(12)
        days = sorted(rows)
        self.cache.put("stock", "005930", rows)
        path = self.directory / "stock-005930.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        invalid = []
        for field, value in (("version", 99), ("version", True), ("kind", "index"), ("symbol", "000660")):
            changed = deepcopy(original)
            changed[field] = value
            invalid.append(changed)
        for field, value in (("close", "NaN"), ("high", "1"), ("volume", "-1"), ("turnover", "1e3")):
            changed = deepcopy(original)
            changed["rows"][days[0].isoformat()][field] = value
            invalid.append(changed)
        for payload in invalid:
            path.write_text(json.dumps(payload), encoding="utf-8")
            fetch = page_fetcher(rows, 4)
            with self.subTest(payload=payload):
                result = self.cache.extend("stock", "005930", {day: rows[day] for day in days[-3:]}, days, fetch)
                self.assertEqual(result, rows)
                self.assertGreater(fetch.call_count, 0)
        path.write_bytes(b" " * (MAX_BYTES + 1))
        fetch = page_fetcher(rows, 4)
        self.assertEqual(self.cache.extend("stock", "005930", {day: rows[day] for day in days[-3:]}, days, fetch), rows)
        self.assertGreater(fetch.call_count, 0)
        path.write_text(json.dumps(original).replace('"version": 1', '"version": 1, "version": 1'), encoding="utf-8")
        fetch = page_fetcher(rows, 4)
        self.assertEqual(self.cache.extend("stock", "005930", {day: rows[day] for day in days[-3:]}, days, fetch), rows)
        self.assertGreater(fetch.call_count, 0)

    def test_index_count_mode_builds_actual_calendar_and_stock_uses_that_calendar(self):
        index = series(330, "index")
        # 시장 휴일은 인위적으로 채우지 않는다.
        for day in sorted(index)[::9]:
            del index[day]
        dates = sorted(index)
        fetch = page_fetcher(index, 50)
        result = self.cache.extend("index", "0001", {day: index[day] for day in dates[-50:]}, 253, fetch)
        self.assertEqual(result, {day: index[day] for day in dates[-253:]})
        self.assertEqual(len(result), 253)
        self.assertEqual(fetch.call_count, 5)
        self.assertTrue(all(len([day for day in index if day <= call.args[0]][-50:]) <= 50
                            for call in fetch.call_args_list))
        stock = series(330)
        required = sorted(result)
        stock_fetch = page_fetcher(stock, 100)
        selected = self.cache.extend("stock", "005930", {day: stock[day] for day in required[-80:]}, required, stock_fetch)
        self.assertEqual(set(selected), set(required))
        self.assertEqual(selected, {day: stock[day] for day in required})
        forbidden = Mock(side_effect=AssertionError("unexpected API request"))
        self.assertEqual(HistoryCache(self.directory).extend("index", "0001",
                         {day: index[day] for day in dates[-50:]}, 253, forbidden), result)
        forbidden.assert_not_called()

    def test_future_and_outside_calendar_values_are_not_returned_or_saved(self):
        rows = series(100)
        days = sorted(rows)
        self.cache.put("stock", "005930", rows)
        latest = {day: rows[day] for day in days[50:90]}
        required = days[20:80]
        forbidden = Mock(side_effect=AssertionError("unexpected API request"))
        result = self.cache.extend("stock", "005930", latest, required, forbidden)
        self.assertEqual(result, {day: rows[day] for day in required})
        saved = json.loads((self.directory / "stock-005930.json").read_text(encoding="utf-8"))
        self.assertEqual(set(saved["rows"]), {day.isoformat() for day in required})
        index = series(100, "index")
        self.cache.put("index", "0001", index)
        historical = self.cache.extend("index", "0001", {day: index[day] for day in days[50:80]}, 60, forbidden)
        self.assertEqual(historical, {day: index[day] for day in days[20:80]})
        forbidden.assert_not_called()

    def test_nonprogress_missing_boundary_page_limit_and_invalid_seed_validation(self):
        rows = series(80, "index")
        days = sorted(rows)
        with self.assertRaisesRegex(KisError, "경계"):
            self.cache.extend("index", "0001", {days[-1]: rows[days[-1]]}, 61,
                              Mock(return_value={days[-2]: rows[days[-2]]}))
        fetch = page_fetcher(rows, 2)
        with self.assertRaisesRegex(KisError, "한도"):
            self.cache.extend("index", "0001", {days[-1]: rows[days[-1]]}, 61, fetch)
        self.assertEqual(fetch.call_count, MAX_PAGES)
        self.assertFalse((self.directory / "index-0001.json").exists())
        bad = series(10)
        bad[min(bad)]["low"] = Decimal(100000)
        with self.assertRaises(KisError):
            self.cache.put("stock", "005930", bad)
        with self.assertRaises(KisError):
            self.cache.put("index", "0001", series(1001, "index"))
        with self.assertRaises(KisError):
            self.cache.extend("stock", "../x", {}, [], Mock())
        with self.assertRaises(KisError):
            self.cache.extend("stock", "005930", series(2), 61, Mock())


if __name__ == "__main__":
    unittest.main()
