"""Archived development frames use only local, bounded, dated evidence."""
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.learning_history import development_frames


def sessions(count=90):
    values, day = [], date(2026, 1, 5)
    while len(values) < count:
        if day.weekday() < 5:
            values.append(day.isoformat())
        day += timedelta(days=1)
    return values


def bar(index, *, index_price=False):
    close = (1000 if index_price else 100) + index
    return {"open": str(close), "high": str(close + 2), "low": str(close - 2),
            "close": str(close), "volume": str(1000 + index), "turnover": "1000000"}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.days = sessions()

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def collection(self, name="2026-04-10", *, count=70, collected=None):
        directory = self.root / ".local" / "research" / ("candidate-screen-" + name)
        universe = {"status": "verified", "rows": [{"symbol": "005930", "name": "과거 이름", "board": "KOSPI"}]}
        self.write(directory / "universe-snapshot.json", universe)
        manifest = {"version": 1, "universe_sha256": hashlib.sha256((directory / "universe-snapshot.json").read_bytes()).hexdigest(),
                    "requested_start": self.days[0], "requested_end": self.days[count - 1], "stock_count": 1,
                    "environment": "paper", "period": "D", "adjusted": True, "FID_ORG_ADJ_PRC": "0"}
        collected = collected or self.days[count - 1] + "T16:30:00+09:00"
        self.write(directory / "manifest.json", manifest)
        self.write(directory / "progress.json", {"version": 1, "status": "complete", "updated_at": collected})
        for kind, symbol, board in (("stock", "005930", "KOSPI"), ("index", "0001", "KOSPI"), ("index", "1001", "KOSDAQ")):
            payload = {"version": 1, "kind": kind, "symbol": symbol, "board": board, "status": "complete",
                       "error": None, "conflicts": [], "collected_at": collected,
                       **{key: manifest[key] for key in ("universe_sha256", "requested_start", "requested_end")},
                       "rows": [{"date": day, **bar(index, index_price=kind == "index")} for index, day in enumerate(self.days[:count])]}
            self.write(directory / ("series" if kind == "stock" else "indices") / (symbol + ".json"), payload)
        return directory

    def current(self, count=80, *, live=True):
        data = {"as_of": self.days[count - 1], "observed_at": self.days[count - 1] + "T17:00:00+09:00",
                "rows": [{"symbol": "005930", "name": "현재 이름", "board": "KOSPI", "learning_eligible": True}]}
        if live:
            data.update(histories={"005930": {day: bar(index) for index, day in enumerate(self.days[:count])}},
                        benchmarks={board: {day: bar(index, index_price=True)["close"] for index, day in enumerate(self.days[:count])}
                                    for board in ("KOSPI", "KOSDAQ")},
                        calendars={board: self.days[:count] for board in ("KOSPI", "KOSDAQ")})
        return data

    def test_development_role_warmup_limits_and_current_public_metadata(self):
        self.collection()
        data = self.current(live=False)
        data["rows"].append({"symbol": "000660", "name": "연구에 없는 종목", "board": "KOSPI"})
        frames = development_frames(self.root, data, limit=5)
        self.assertEqual(len(frames), 5)
        self.assertEqual(frames[-1]["as_of"], self.days[69])
        for frame in frames:
            self.assertEqual(frame["role"], "development_only")
            self.assertIn("current_universe", frame["provenance"]["universe_warning"])
            self.assertLessEqual(len(frame["session_days"]), 63)
            self.assertEqual(frame["baseline_signals"], [])
            self.assertEqual([(row["symbol"], row["name"]) for row in frame["rows"]], [("005930", "현재 이름")])
            self.assertIsNotNone(frame["rows"][0]["features"]["close_sma60_pct"])
            self.assertIsNotNone(frame["rows"][0]["features"]["excess_20d_pp"])

    def test_current_supplied_prices_extend_archive_without_missing_sessions(self):
        self.collection()
        frames = development_frames(self.root, self.current(), limit=20)
        self.assertEqual([frame["as_of"] for frame in frames], self.days[60:80])
        self.assertEqual(frames[-1]["rows"][0]["close"], 179)
        self.assertEqual(frames[-1]["provenance"]["current_series_replacements"], 0)

    def test_future_prices_and_calendars_cannot_change_development_result(self):
        self.collection()
        data = self.current()
        before = development_frames(self.root, data, limit=5)
        future = self.days[85]
        data["histories"]["005930"][future] = bar(10000)
        for board in ("KOSPI", "KOSDAQ"):
            data["benchmarks"][board][future] = "10000000"
            data["calendars"][board].append(future)
        self.assertEqual(development_frames(self.root, data, limit=5), before)

    def test_archive_itself_is_cut_off_at_current_asof(self):
        self.collection(count=80, collected=self.days[60] + "T16:30:00+09:00")
        frames = development_frames(self.root, self.current(70, live=False))
        self.assertEqual(len(frames), 11)
        self.assertEqual(frames[-1]["as_of"], self.days[69])

    def test_changed_adjustment_uses_entire_current_series_not_old_prefix(self):
        self.collection()
        data = self.current()
        data["histories"]["005930"] = {day: {**bar(index), "open": str(200 + index), "close": str(200 + index),
                                                         "high": str(202 + index), "low": str(198 + index)}
                                              for index, day in enumerate(self.days[:80]) if index >= 15}
        frames = development_frames(self.root, data, limit=30)
        self.assertEqual(frames[-1]["provenance"]["current_series_replacements"], 1)
        first = frames[0]["rows"][0]
        self.assertFalse(first["learning_eligible"])
        self.assertIsNone(first["features"]["close_sma60_pct"])
        self.assertTrue(frames[-1]["rows"][0]["learning_eligible"])
        self.assertEqual(frames[-1]["rows"][0]["close"], 279)

    def test_index_vintage_conflict_replaces_both_calendars(self):
        self.collection()
        data = self.current()
        for board in ("KOSPI", "KOSDAQ"):
            data["benchmarks"][board] = {day: str(2000 + i) for i, day in enumerate(self.days[:80]) if i >= 15}
            data["calendars"][board] = self.days[15:80]
        frames = development_frames(self.root, data)
        self.assertEqual(len(frames), 6)  # 65 sessions minus 59 warmup observations.
        self.assertEqual(frames[-1]["session_days"][0], self.days[17])

    def test_newest_compatible_collection_and_invalid_newest_fallback(self):
        older = self.collection("older", count=70)
        newer = self.collection("newer", count=75)
        data = self.current(live=False)
        frames = development_frames(self.root, data, limit=2)
        self.assertTrue(frames[0]["provenance"]["source"].endswith(newer.name))
        stock = newer / "series" / "005930.json"
        value = json.loads(stock.read_text(encoding="utf-8"))
        value["universe_sha256"] = "wrong"
        self.write(stock, value)
        frames = development_frames(self.root, data, limit=2)
        self.assertTrue(frames[0]["provenance"]["source"].endswith(older.name))

    def test_bad_status_version_future_collection_and_conflicting_duplicates_fall_back(self):
        directory = self.collection()
        stock = directory / "series" / "005930.json"
        original = json.loads(stock.read_text(encoding="utf-8"))
        bad = [{"status": "error"}, {"version": True}, {"collected_at": "2099-01-01T00:00:00+00:00"},
               {"rows": original["rows"] + [{**original["rows"][0], "close": "999"}]}]
        for changes in bad:
            with self.subTest(changes=changes.keys()):
                self.write(stock, {**original, **changes})
                self.assertEqual(development_frames(self.root, self.current()), [])

    def test_missing_or_short_research_is_optional_and_does_not_start_services(self):
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")), \
                patch("subprocess.run", side_effect=AssertionError("process forbidden")):
            self.assertEqual(development_frames(self.root, self.current()), [])
            self.collection(count=59)
            self.assertEqual(development_frames(self.root, self.current(live=False)), [])
            self.collection(count=70)
            data = self.current()
            before = deepcopy(data)
            self.assertTrue(development_frames(self.root, data, limit=2))
            self.assertEqual(data, before)


if __name__ == "__main__":
    unittest.main()
