from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.chart import ChartService
from backend.chart_cache import DiskChartCache, MAX_BYTES
from backend.kis import KST, KisError
from test_chart import row, response


class PagingClient:
    def __init__(self, day="20261002", until="100000"):
        self.day = day
        self.rows = {}
        self.minute_calls = []
        self.daily_calls = 0
        self.failure = None
        self.fill(until)

    def fill(self, until):
        clock = datetime.strptime("090000", "%H%M%S")
        end = datetime.strptime(until, "%H%M%S")
        while clock <= end:
            hour = clock.strftime("%H%M%S")
            self.rows.setdefault(hour, row(self.day, hour))
            clock += timedelta(minutes=1)

    def chart_minutes(self, symbol, hour):
        self.minute_calls.append(hour)
        if self.failure:
            raise self.failure
        return response(*[self.rows[key] for key in sorted(self.rows, reverse=True) if key <= hour][:30])

    def chart_daily(self, symbol, start, end):
        self.daily_calls += 1
        if self.failure:
            raise self.failure
        return response(row(self.day))


class ChartCacheTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.config = self.folder / "config.toml"
        self.config.write_text("app_key='private-key'\napp_secret='private-secret'\naccount='12345678'\n", encoding="utf-8")
        self.wall = [datetime(2026, 10, 2, 10, tzinfo=KST)]
        self.ticks = [0]
        self.names = Mock()
        self.names.lookup.return_value = {"005930": "삼성전자"}
        self.client = PagingClient()
        self.factory = Mock(side_effect=lambda profile: self.client)
        self.service = self.new_service()

    def new_service(self):
        return ChartService(self.config, self.factory, lambda: self.ticks[0], lambda: self.wall[0], self.names)

    def advance(self, seconds):
        self.wall[0] += timedelta(seconds=seconds)
        self.ticks[0] += seconds

    def paths(self):
        return list((self.folder / ".local" / "chart-cache").glob("*.json"))

    def raw(self):
        return next(iter(self.service.cache.values())).snapshot["bars"]

    def test_warm_interval_switch_and_process_restart_do_not_query(self):
        first = self.service.snapshot("005930", "5m")
        self.assertEqual(first["status"], "ok")
        self.assertEqual(len(self.client.minute_calls), 3)
        self.service.snapshot("005930", "15m")
        self.new_service().snapshot("005930", "5m")
        self.assertEqual(len(self.client.minute_calls), 3)
        text = self.paths()[0].read_text(encoding="utf-8")
        for secret in ("private-key", "private-secret", "12345678"):
            self.assertNotIn(secret, text)
        self.assertEqual(json.loads(text)["version"], 1)

    def test_incremental_replaces_recent_minutes_and_zero_volume_removes_old_bar(self):
        self.service.snapshot("005930", "5m")
        self.advance(60)
        self.client.fill("100100")
        self.client.rows["095900"].update(stck_prpr="107", stck_hgpr="120", cntg_vol="2")
        self.client.rows["100000"].update(cntg_vol="0")
        self.client.minute_calls.clear()
        result = self.service.snapshot("005930", "5m")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(self.client.minute_calls), 1)
        rows = {bar["time"][11:19]: bar for bar in self.raw()}
        self.assertEqual(rows["09:59:00"]["close"], "107")
        self.assertEqual(rows["09:59:00"]["volume"], "2")
        self.assertNotIn("10:00:00", rows)
        self.assertIn("10:01:00", rows)
        self.assertIn("09:00:00", rows)
        self.assertEqual(sum(int(bar["volume"]) for bar in rows.values()), 602)

    def test_page_before_overlap_does_not_stop_early(self):
        self.wall[0] = self.wall[0].replace(hour=9, minute=30)
        self.client = PagingClient(until="093000")
        self.service.snapshot("005930", "5m")
        self.advance(31 * 60)
        self.client.fill("100100")
        self.client.minute_calls.clear()
        result = self.service.snapshot("005930", "5m")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(self.client.minute_calls), 2)
        self.assertEqual(len(self.raw()), 62)

    def test_missing_overlap_minute_requires_complete_refresh(self):
        self.service.snapshot("005930", "5m")
        self.advance(60)
        self.client.fill("100100")
        del self.client.rows["095900"]
        self.client.minute_calls.clear()
        result = self.service.snapshot("005930", "5m")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(self.client.minute_calls), 3)
        self.assertNotIn("09:59:00", [bar["time"][11:19] for bar in self.raw()])

    def test_force_uses_incremental_page_and_failure_keeps_memory_and_disk(self):
        first = self.service.snapshot("005930", "5m")
        original = self.paths()[0].read_bytes()
        self.client.minute_calls.clear()
        forced = self.service.snapshot("005930", "15m", force=True)
        self.assertEqual(forced["status"], "ok")
        self.assertEqual(len(self.client.minute_calls), 1)
        self.client.failure = KisError("조회 실패")
        failed = self.service.snapshot("005930", "5m", force=True)
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["bars"], first["bars"])
        self.assertEqual(self.paths()[0].read_bytes(), original)
        count = len(self.client.minute_calls)
        self.service.snapshot("005930", "5m", force=True)
        self.assertEqual(len(self.client.minute_calls), count)

    def test_incremental_midway_failure_preserves_normal_cache(self):
        self.service.snapshot("005930", "5m")
        old = self.paths()[0].read_bytes()
        self.advance(3600)
        self.client.fill("110000")
        original = self.client.chart_minutes
        count = [0]
        def failing(symbol, hour):
            count[0] += 1
            if count[0] == 2:
                raise KisError("중간 조회 실패")
            return original(symbol, hour)
        self.client.chart_minutes = failing
        result = self.service.snapshot("005930", "5m")
        self.assertTrue(result["stale"])
        self.assertEqual(result["as_of"], "2026-10-02")
        self.assertEqual(self.paths()[0].read_bytes(), old)

    def test_new_session_replaces_old_day_without_early_overlap(self):
        self.service.snapshot("005930", "5m")
        self.wall[0] = datetime(2026, 10, 5, 10, tzinfo=KST)
        self.ticks[0] = 3 * 86400
        self.client = PagingClient("20261005", "100000")
        result = self.service.snapshot("005930", "5m")
        self.assertEqual(result["as_of"], "2026-10-05")
        self.assertEqual(len(self.client.minute_calls), 3)
        self.assertTrue(all(bar["time"].startswith("2026-10-05") for bar in self.raw()))

    def test_older_session_or_earlier_latest_time_is_not_accepted(self):
        first = self.service.snapshot("005930", "5m")
        self.client = PagingClient("20261001", "153000")
        result = self.service.snapshot("005930", "5m", force=True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["bars"], first["bars"])
        self.advance(10)
        self.client = PagingClient("20261002", "093000")
        result = self.service.snapshot("005930", "5m", force=True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["bars"], first["bars"])

    def test_confirmed_after_close_reuses_through_weekend_restart_and_midnight(self):
        self.wall[0] = datetime(2026, 10, 2, 16, tzinfo=KST)
        self.client = PagingClient(until="153000")
        self.service.snapshot("005930", "5m")
        self.service.snapshot("005930", "day")
        count = len(self.client.minute_calls)
        daily = self.client.daily_calls
        self.wall[0] = datetime(2026, 10, 3, 10, tzinfo=KST)
        self.ticks[0] = 18 * 3600
        self.service.snapshot("005930", "15m")
        self.new_service().snapshot("005930", "5m")
        self.new_service().snapshot("005930", "day")
        self.assertEqual(len(self.client.minute_calls), count)
        self.assertEqual(self.client.daily_calls, daily)
        self.wall[0] = datetime(2026, 10, 5, 9, tzinfo=KST)
        self.ticks[0] = 3 * 86400
        self.client = PagingClient("20261005", "090000")
        result = self.service.snapshot("005930", "5m")
        self.assertEqual(result["as_of"], "2026-10-05")
        self.assertEqual(len(self.client.minute_calls), 1)

    def test_intraday_partial_cache_is_not_frozen_when_weekend_arrives(self):
        self.service.snapshot("005930", "5m")
        self.wall[0] = datetime(2026, 10, 3, 10, tzinfo=KST)
        self.ticks[0] = 86400
        self.client.fill("153000")
        self.client.minute_calls.clear()
        result = self.new_service().snapshot("005930", "5m")
        self.assertEqual(result["bars"][-1]["time"], "2026-10-02T15:30:00+09:00")
        self.assertGreater(len(self.client.minute_calls), 1)

    def test_1530_snapshot_still_refreshes_until_post_close_grace_passes(self):
        self.wall[0] = datetime(2026, 10, 2, 15, 30, tzinfo=KST)
        self.client = PagingClient(until="153000")
        self.service.snapshot("005930", "5m")
        self.client.minute_calls.clear()
        self.advance(60)
        self.service.snapshot("005930", "5m")
        self.assertEqual(len(self.client.minute_calls), 1)
        self.wall[0] = datetime(2026, 10, 2, 16, tzinfo=KST)
        self.ticks[0] += 1800
        self.service.snapshot("005930", "5m")
        self.advance(3600)
        self.service.snapshot("005930", "5m")
        self.assertEqual(len(self.client.minute_calls), 2)

    def test_weekday_previous_session_is_not_assumed_to_mean_holiday(self):
        self.client = PagingClient("20261001", "153000")
        self.service.snapshot("005930", "5m")
        self.client.minute_calls.clear()
        self.advance(30)
        self.service.snapshot("005930", "5m")
        self.assertGreater(len(self.client.minute_calls), 0)

    def test_corrupt_or_untrusted_cache_is_ignored(self):
        self.service.snapshot("005930", "5m")
        path = self.paths()[0]
        original = json.loads(path.read_text(encoding="utf-8"))
        corruptions = [None, {**original, "version": 99}, {**original, "symbol": "000660"},
                       {**original, "kind": "day"}, {**original, "identity": "0" * 64},
                       {**original, "observed_at": "2099-01-01T00:00:00+00:00"},
                       {**original, "minute_until": "2026-10-02T10:00:00"},
                       {**original, "bars": list(reversed(original["bars"]))},
                       {**original, "bars": [original["bars"][0]] * 2},
                       {**original, "bars": [{**original["bars"][0], "high": "1"}]}]
        for corrupt in corruptions:
            with self.subTest(corrupt=corrupt):
                path.write_text(json.dumps(corrupt), encoding="utf-8")
                self.client.minute_calls.clear()
                result = self.new_service().snapshot("005930", "5m")
                self.assertEqual(result["status"], "ok")
                self.assertEqual(len(self.client.minute_calls), 3)
        path.write_bytes(b"x" * (MAX_BYTES + 1))
        self.client.minute_calls.clear()
        self.new_service().snapshot("005930", "5m")
        self.assertEqual(len(self.client.minute_calls), 3)

    def test_cache_write_failure_keeps_successful_response(self):
        with patch.object(self.service.disk_cache, "write", side_effect=OSError("private-secret")):
            result = self.service.snapshot("005930", "5m")
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("private-secret", json.dumps(result))

    def test_disk_files_are_bounded_and_newer_observation_is_not_replaced(self):
        store = DiskChartCache(self.folder / "bounded")
        data = {"observed_at": "2026-10-02T07:00:00+00:00", "bars": []}
        for index in range(33):
            store.write(f"{index:064x}", data)
        self.assertEqual(len(list(store.directory.glob("*.json"))), 32)
        identity = f"{32:064x}"
        store.write(identity, {**data, "observed_at": "2026-10-02T06:00:00+00:00"})
        self.assertEqual(store.read(identity)["observed_at"], data["observed_at"])


if __name__ == "__main__":
    unittest.main()
