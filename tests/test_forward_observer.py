"""Synthetic forward-clock, immutable-record and hypothetical-price regressions."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
from http.client import HTTPConnection
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from backend.forward_observer import ForwardObserver, _read, _append, _hash, _observer_code_hash
from backend.kis import KST


DAYS = [date(2026, 6, 1) + timedelta(days=i) for i in range(180)
        if (date(2026, 6, 1) + timedelta(days=i)).weekday() < 5]


class Clock:
    def __init__(self, day=62, hour=9):
        self.set(day, hour)

    def set(self, day, hour=17):
        self.value = datetime.combine(DAYS[day], time(hour), KST)

    def __call__(self):
        return self.value


def evaluate(bars, calendar):
    eligible = len(calendar) >= 63 and all(day in bars for day in calendar[-63:])
    return {"eligible": eligible, "signal": eligible, "trend": eligible,
            "reason": "signal" if eligible else "insufficient_history"}


def payload(clock, last=62, window=63):
    days = DAYS[max(0, last - window + 1):last + 1]
    symbols = [("000001", "KOSPI"), ("000002", "KOSDAQ")]
    histories = {symbol: {day: {"open": Decimal(100 + i), "high": Decimal(102 + i),
                               "low": Decimal(98 + i), "close": Decimal(100 + i),
                               "volume": Decimal(1000), "turnover": Decimal(20_000_000_000)}
                         for i, day in enumerate(DAYS[:last + 1]) if day in days}
                 for symbol, _ in symbols}
    rows = [{"symbol": symbol, "name": symbol, "board": board, "status": "ok", "error": None,
             "selection": {"status": "selected", "rank": i + 1, "score": "200",
                           "status_observed_at": clock().isoformat()}}
            for i, (symbol, board) in enumerate(symbols)]
    universe = {"status": "verified", "as_of": "2026-09-21", "source_url": "https://example.test",
                "rows": [{key: row[key] for key in ("symbol", "name", "board")} for row in rows]}
    return {"universe": universe,
            "result": {"status": "complete", "as_of": DAYS[last].isoformat(),
                       "requested_through": clock().date().isoformat(), "updated_at": clock().isoformat(),
                       "screening": {"status": "ready", "checked_at": clock().isoformat()}, "rows": rows},
            "histories": histories, "calendars": {board: days for _, board in symbols},
            "indices": {board: {day: Decimal(100 + i) for i, day in enumerate(days)} for _, board in symbols},
            "errors": {}, "sources": {"stock-" + symbol: {"observed_at": clock().isoformat(), "raw_sha256": "test"}
                                       for symbol, _ in symbols}}


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.clock = Clock()
        self.observer = self.make()

    def make(self, **kwargs):
        return ForwardObserver(self.directory, now=self.clock, evaluator=evaluate,
                               dependencies=kwargs.pop("dependencies", {"test": "v1"}), **kwargs)

    def observe(self, last=62, **changes):
        self.clock.set(last)
        values = payload(self.clock, last)
        for key, value in changes.items():
            values[key] = value
        self.observer.observe(**values)
        return self.observer.snapshot()

    def records(self):
        return [_read(path) for path in (self.observer.path / "records").glob("*.json")]

    def test_freeze_precedes_prospective_and_restart_preserves_it(self):
        frozen = self.observer.version["frozen_at"]
        result = self.observe()
        self.assertEqual(result["counts"]["prospective"], 2)
        self.assertEqual(result["counts"]["open"], 2)
        self.assertTrue(all(row["outcome"]["status"] == "pending_entry" for row in result["observations"]))
        self.clock.set(64)
        restarted = self.make()
        self.assertEqual(restarted.version["frozen_at"], frozen)
        self.assertEqual(restarted.snapshot()["counts"]["prospective"], 2)
        self.assertEqual(result["review_date"], "2026-11-20")
        self.assertFalse(result["order_enabled"])
        self.assertIsNone(result["allocation"])

    def test_bootstrap_and_late_never_enter_performance_counts(self):
        self.clock.set(62, 17)
        self.observer = self.make(dependencies={"test": "after-close"})
        self.observer.observe(**payload(self.clock))
        self.assertEqual(self.observer.snapshot()["counts"]["bootstrap"], 2)
        self.clock.set(64, 10)
        self.observer.observe(**payload(self.clock, 63))
        self.assertEqual(self.observer.snapshot()["counts"]["timing_unverified"], 2)
        self.clock.set(64)
        values = payload(self.clock, 64)
        for row in values["result"]["rows"]:
            row["selection"]["status"] = "reserve"
        self.observer.observe(**values)
        result = self.observer.snapshot()
        self.assertEqual(result["counts"]["late"], 2)
        self.assertEqual(result["counts"]["prospective"], 0)
        self.assertTrue(all(row["outcome"]["status"] == "excluded" for row in result["observations"]))

    def test_same_input_deduplicates_despite_refresh_timestamps(self):
        self.observe()
        before = {str(path.relative_to(self.directory)): path.read_bytes()
                  for path in self.directory.rglob("*.json")}
        self.clock.value += timedelta(minutes=10)
        self.observer.observe(**payload(self.clock))
        after = {str(path.relative_to(self.directory)): path.read_bytes()
                 for path in self.directory.rglob("*.json")}
        self.assertEqual(before, after)

    def test_holiday_requests_share_compressed_market_input(self):
        self.observe()
        first = self.observer.snapshot()
        self.clock.value += timedelta(days=1)
        self.observer.observe(**payload(self.clock, 62))
        inputs = list((self.observer.path / "inputs").glob("*.json"))
        data = list((self.observer.path / "data").glob("*.json.gz"))
        self.assertEqual(len(inputs), 2)
        self.assertEqual(len(data), 1)
        metadata = [_read(path) for path in inputs]
        self.assertEqual(len({item["market_data_sha256"] for item in metadata}), 1)
        self.assertTrue(all("histories" not in item["data"] for item in metadata))
        market = _read(data[0])
        self.assertEqual(len(market["histories"]), 2)
        self.assertLess(data[0].stat().st_size, len(str(market).encode()) // 4)
        self.assertEqual(self.observer.snapshot()["observations"], first["observations"])

    def test_legacy_full_inputs_remain_readable_and_unchanged(self):
        self.observe()
        # Recreate the pre-upgrade layout inside this isolated test directory.
        for path in (self.observer.path / "inputs").glob("*.json"):
            saved = _read(path)
            market = _read(self.observer.path / "data" / f"{saved.pop('market_data_sha256')}.json.gz")
            saved["data"].update(market)
            path.unlink()
            _append(path, saved)
        for path in (self.observer.path / "data").glob("*.json.gz"):
            path.unlink()
        original = {path: path.read_bytes() for path in self.observer.path.rglob("*.json")}
        frozen = self.observer.version["frozen_at"]
        self.observer = self.make()
        self.assertEqual(self.observer.version["frozen_at"], frozen)
        self.assertEqual(self.observer.snapshot()["counts"]["prospective"], 2)
        result = self.observe(68)
        self.assertEqual(result["counts"]["closed"], 2)
        self.assertEqual(original, {path: path.read_bytes() for path in original})

    def test_unchanged_snapshot_and_tick_reuse_verified_records(self):
        self.observe()
        candidate = Mock()
        with patch("backend.forward_observer._read", wraps=_read) as reader:
            self.assertEqual(self.observer.snapshot()["status"], "ready")
            reader.assert_not_called()
            self.observer.tick(candidate)
            self.assertEqual([call.args[0].name for call in reader.call_args_list], ["freeze.json"])
            candidate.start.assert_not_called()
        # A caller cannot mutate the cached records through the public response.
        value = self.observer.snapshot()
        value["observations"][0]["outcome"]["status"] = "changed by caller"
        self.assertEqual(self.observer.snapshot()["observations"][0]["outcome"]["status"], "pending_entry")

    def test_compressed_input_corruption_and_loss_invalidate_warm_cache(self):
        self.observe()
        path = next((self.observer.path / "data").glob("*.json.gz"))
        original = path.read_bytes()
        path.write_bytes(b"broken gzip")
        self.assertEqual(self.observer.snapshot()["status"], "error")
        with self.assertRaises(OSError):
            self.observer.observe(**payload(self.clock))
        self.assertEqual(path.read_bytes(), b"broken gzip")
        path.write_bytes(original)
        self.assertEqual(self.observer.snapshot()["status"], "ready")
        path.unlink()
        self.assertEqual(self.observer.snapshot()["status"], "error")

    def test_receipts_reference_each_committed_file_only_once(self):
        self.observe()
        self.observe(64)
        receipts = [_read(path) for path in (self.observer.path / "receipts").glob("*.json")]
        references = [name for receipt in receipts for name in receipt["files"]]
        self.assertEqual(len(references), len(set(references)))
        files = {str(path.relative_to(self.observer.path)) for directory in ("records", "days", "timing", "outcomes")
                 for path in (self.observer.path / directory).glob("*.json")}
        self.assertEqual(set(references), files)
        # Losing an old outcome remains detectable through its original receipt.
        first = min((self.observer.path / "outcomes").glob("*.json"), key=lambda path: _read(path)["sequence"])
        first.unlink()
        self.assertEqual(self.observer.snapshot()["status"], "error")

    def test_storage_change_preserves_version_but_rule_change_does_not(self):
        import backend.forward_observer as observer_module
        source = Path(observer_module.__file__).read_bytes()
        legacy = "17b750f3f7c099e75e0fc139c2c371543a349d0118e5bb386c318f0c6f0eeeee"
        self.assertEqual(_observer_code_hash(source), legacy)
        self.assertEqual(_observer_code_hash(source + b"\n# Storage-only comment\n"), legacy)
        changed = source.replace(b"holding_days=offset", b"holding_days=offset + 1")
        self.assertEqual(_observer_code_hash(changed), hashlib.sha256(changed).hexdigest())
        self.assertNotEqual(_observer_code_hash(changed), legacy)
        changed_quality = source.replace(b"ready = (not missing", b"ready = (bool(missing)")
        self.assertEqual(_observer_code_hash(changed_quality), hashlib.sha256(changed_quality).hexdigest())
        changed_serialization = source.replace(b'return format(value, "f")', b'return format(value, ".1f")')
        self.assertEqual(_observer_code_hash(changed_serialization), hashlib.sha256(changed_serialization).hexdigest())

    def test_runtime_storage_source_edit_requires_restart(self):
        import backend.forward_observer as observer_module
        original_read = Path.read_bytes
        source = Path(observer_module.__file__).resolve()
        candidate = Mock()
        with patch.object(Path, "read_bytes", autospec=True,
                          side_effect=lambda path: original_read(path) + (b"\n# storage edit\n" if path.resolve() == source else b"")):
            self.observer.tick(candidate)
        candidate.start.assert_not_called()
        self.assertIn("서버를 다시 시작", self.observer.snapshot()["error"])

    def test_native_dependency_keys_reuse_a_legacy_freeze(self):
        universe = payload(self.clock)["universe"]
        probe = self.make(dependencies=None, universe_loader=lambda: universe)
        dependencies = probe._fingerprint()
        observer_key = str(Path("backend") / "forward_observer.py")
        self.assertEqual(dependencies[observer_key],
                         "17b750f3f7c099e75e0fc139c2c371543a349d0118e5bb386c318f0c6f0eeeee")
        # Pre-upgrade freezes use native relative paths (backslashes on Windows).
        legacy_dependencies = {str(Path(key)): value for key, value in dependencies.items()}
        self.assertEqual(dependencies, legacy_dependencies)
        legacy_id = _hash({"rule_id": probe.rule_id, "dependencies": legacy_dependencies})
        directory = self.directory / "legacy-copy"
        freeze_path = directory / legacy_id / "freeze.json"
        frozen_at = datetime.combine(DAYS[61], time(9), KST).isoformat()
        _append(freeze_path, {**probe.version, "version_id": legacy_id,
                             "dependencies": legacy_dependencies, "frozen_at": frozen_at})
        original = freeze_path.read_bytes()
        restored = ForwardObserver(directory, now=self.clock, universe_loader=lambda: universe)
        self.assertEqual(restored.path, freeze_path.parent)
        self.assertEqual(restored.version["frozen_at"], frozen_at)
        self.assertEqual(freeze_path.read_bytes(), original)
        self.assertEqual([path.name for path in directory.iterdir() if path.is_dir()], [legacy_id])

    def test_first_ready_day_seals_membership_and_signal_inputs(self):
        self.observe()
        old_records = self.records()
        changed = payload(self.clock)
        changed["result"]["rows"][0]["selection"]["status"] = "reserve"
        new_row = deepcopy(changed["result"]["rows"][0])
        new_row.update(symbol="000003", name="new")
        new_row["selection"]["status"] = "selected"
        changed["result"]["rows"].append(new_row)
        changed["histories"]["000003"] = deepcopy(changed["histories"]["000001"])
        self.observer.observe(**changed)
        self.assertEqual(sorted(self.records(), key=lambda x: x["symbol"]), sorted(old_records, key=lambda x: x["symbol"]))
        self.assertEqual(len(list((self.observer.path / "inputs").glob("*.json"))), 2)

    def test_crash_between_day_seal_and_records_recovers_with_actual_late_time(self):
        original = _append
        def crash(path, values):
            if Path(path).parent.name == "records":
                raise OSError("synthetic interruption")
            original(path, values)
        self.clock.set(62)
        with patch("backend.forward_observer._append", side_effect=crash):
            with self.assertRaises(OSError):
                self.observer.observe(**payload(self.clock))
        self.assertEqual(len(list((self.observer.path / "days").glob("*.json"))), 1)
        self.clock.set(63, 10)
        self.observer = self.make()
        self.observer.observe(**payload(self.clock, 62))
        pending = self.observer.snapshot()
        self.assertEqual(pending["counts"]["timing_unverified"], 2)
        generated = {row["record_id"]: row["generated_at"] for row in pending["observations"]}
        self.clock.set(63)
        values = payload(self.clock, 63)
        for row in values["result"]["rows"]:
            row["selection"]["status"] = "reserve"
        self.observer.observe(**values)
        result = self.observer.snapshot()
        self.assertEqual(result["counts"]["late"], 2)
        self.assertEqual(result["counts"]["prospective"], 0)
        self.assertTrue(all(row["generated_at"] == generated[row["record_id"]] for row in result["observations"]))

    def test_weekend_generation_waits_for_actual_successor_then_becomes_prospective(self):
        self.assertEqual(DAYS[64].weekday(), 4)
        self.clock.value = datetime.combine(DAYS[64] + timedelta(days=2), time(20), KST)
        self.observer.observe(**payload(self.clock, 64))
        initial = self.observer.snapshot()
        self.assertEqual(initial["counts"]["timing_unverified"], 2)
        sealed = {row["record_id"]: row["generated_at"] for row in initial["observations"]}
        self.clock.set(65)
        values = payload(self.clock, 65)
        for row in values["result"]["rows"]:
            row["selection"]["status"] = "reserve"
        self.observer.observe(**values)
        result = self.observer.snapshot()
        self.assertEqual(result["counts"]["prospective"], 2)
        self.assertEqual(result["counts"]["timing_unverified"], 0)
        self.assertTrue(all(row["generated_at"] == sealed[row["record_id"]] for row in result["observations"]))
        self.assertTrue(all(row["original_classification"] == "timing_unverified" for row in result["observations"]))

    def test_known_short_history_does_not_hide_actual_collection_failures(self):
        self.clock.set(62)
        values = payload(self.clock)
        values["result"]["rows"][1]["selection"]["status"] = "unverified"
        values["known_ineligible"] = {"000002": {"eligible": False, "reason": "insufficient_contiguous_history",
                                                   "available_sessions": 27, "required_sessions": 61}}
        self.observer.observe(**values)
        self.assertEqual(self.observer.snapshot()["status"], "ready")
        self.assertEqual(self.observer.snapshot()["counts"]["signals"], 1)
        self.clock.set(63)
        failed = payload(self.clock, 63)
        failed["known_ineligible"] = values["known_ineligible"]
        failed["errors"] = {"000002": "network unavailable"}
        self.observer.observe(**failed)
        self.assertEqual(self.observer.snapshot()["status"], "partial")

    def test_partial_is_saved_and_does_not_seal_or_create_signals(self):
        self.clock.set(62)
        values = payload(self.clock)
        values["result"]["rows"][1]["selection"]["status"] = "unverified"
        self.observer.observe(**values)
        result = self.observer.snapshot()
        self.assertEqual(result["status"], "partial")
        self.assertIn("1종목", result["error"])
        self.assertEqual(result["counts"]["signals"], 0)
        self.assertFalse((self.observer.path / "days").exists())
        self.observer.observe(**payload(self.clock))
        self.assertEqual(self.observer.snapshot()["counts"]["prospective"], 2)

    def test_63_day_shortage_keeps_ineligible_judgment(self):
        self.clock.set(62)
        values = payload(self.clock)
        del values["histories"]["000001"][DAYS[0]]
        self.observer.observe(**values)
        row = next(row for row in self.records() if row["symbol"] == "000001")
        self.assertFalse(row["eligible"])
        self.assertFalse(row["signal"])
        self.assertEqual(row["reason"], "insufficient_history")

    def test_next_open_and_sixth_open_result_has_exact_costs_and_query_time(self):
        self.observe()
        result = self.observe(68)
        first = next(row for row in result["observations"] if row["signal_date"] == DAYS[62].isoformat())
        outcome = first["outcome"]
        self.assertEqual(outcome["status"], "closed")
        self.assertEqual(outcome["entry_date"], DAYS[63].isoformat())
        self.assertEqual(outcome["exit_date"], DAYS[68].isoformat())
        self.assertEqual(outcome["holding_days"], 5)
        expected = Decimal(168) * Decimal('.999') * (1 - Decimal('.000140527') - Decimal('.002'))
        expected /= Decimal(163) * Decimal('1.001') * (1 + Decimal('.000140527'))
        self.assertAlmostEqual(outcome["returns"][0]["net_return"], float(expected - 1))
        self.assertEqual(datetime.fromisoformat(outcome["entry_observed_at"]), self.clock())

    def test_missing_future_bar_is_unknown_and_can_recover(self):
        self.observe()
        self.clock.set(64)
        values = payload(self.clock, 64)
        del values["histories"]["000001"][DAYS[63]]
        self.observer.observe(**values)
        first = next(row for row in self.observer.snapshot()["observations"]
                     if row["signal_date"] == DAYS[62].isoformat() and row["symbol"] == "000001")
        self.assertEqual(first["outcome"]["status"], "unknown")
        self.assertIsNone(first["outcome"]["entry_observed_at"])
        self.observer.observe(**payload(self.clock, 64))
        first = next(row for row in self.observer.snapshot()["observations"]
                     if row["signal_date"] == DAYS[62].isoformat() and row["symbol"] == "000001")
        self.assertEqual(first["outcome"]["status"], "open")

    def test_unverifiable_entry_is_not_a_claim_of_actual_cancel(self):
        self.observe()
        self.clock.set(63)
        values = payload(self.clock, 63)
        values["histories"]["000001"][DAYS[63]]["volume"] = Decimal(0)
        self.observer.observe(**values)
        first = next(row for row in self.observer.snapshot()["observations"]
                     if row["signal_date"] == DAYS[62].isoformat() and row["symbol"] == "000001")
        self.assertEqual(first["outcome"]["status"], "fill_unverifiable")
        self.assertIsNone(first["outcome"]["returns"][0]["net_return"])

    def test_unverifiable_exit_delays_hypothetical_sale(self):
        self.observe()
        self.clock.set(69)
        values = payload(self.clock, 69)
        values["histories"]["000001"][DAYS[68]]["volume"] = Decimal(0)
        self.observer.observe(**values)
        first = next(row for row in self.observer.snapshot()["observations"]
                     if row["signal_date"] == DAYS[62].isoformat() and row["symbol"] == "000001")
        self.assertEqual(first["outcome"]["status"], "closed")
        self.assertEqual(first["outcome"]["exit_date"], DAYS[69].isoformat())
        self.assertEqual(first["outcome"]["reason"], "time5_delayed")
        self.assertEqual(first["outcome"]["exit_delay_dates"], [DAYS[68].isoformat()])

    def test_price_revision_in_prior_outcome_is_permanently_unknown(self):
        self.observe()
        self.observe(64)
        self.clock.set(65)
        revised = payload(self.clock, 65)
        revised["histories"]["000001"][DAYS[63]]["high"] += 1
        self.observer.observe(**revised)
        result = self.observe(68)
        first = next(row for row in result["observations"]
                     if row["signal_date"] == DAYS[62].isoformat() and row["symbol"] == "000001")
        self.assertEqual(first["outcome"]["reason"], "price_revision")
        self.assertIsNone(first["outcome"]["returns"][0]["net_return"])

    def test_equivalent_decimal_scale_is_not_price_revision(self):
        self.observe()
        self.clock.set(63)
        values = payload(self.clock, 63)
        for bars in values["histories"].values():
            for bar in bars.values():
                for name in ("open", "high", "low", "close"):
                    bar[name] = bar[name].quantize(Decimal('.00'))
        self.observer.observe(**values)
        first = next(row for row in self.observer.snapshot()["observations"] if row["signal_date"] == DAYS[62].isoformat())
        self.assertEqual(first["outcome"]["status"], "open")

    def test_long_offline_calendar_is_not_compressed(self):
        self.observe()
        result = self.observe(126)
        first = next(row for row in result["observations"] if row["signal_date"] == DAYS[62].isoformat())
        self.assertEqual(first["outcome"]["reason"], "calendar_gap")
        self.assertIsNone(first["outcome"]["returns"][0]["net_return"])

    def test_missing_or_corrupted_input_fails_closed_and_is_not_overwritten(self):
        self.observe()
        path = next((self.observer.path / "inputs").glob("*.json"))
        original = path.read_bytes()
        path.write_bytes(b"broken")
        self.assertEqual(self.observer.snapshot()["status"], "error")
        with self.assertRaises(ValueError):
            self.observer.observe(**payload(self.clock))
        self.assertEqual(path.read_bytes(), b"broken")
        path.write_bytes(original)
        self.assertEqual(self.observer.snapshot()["status"], "ready")
        path.unlink()
        self.assertEqual(self.observer.snapshot()["status"], "error")

    def test_dependency_change_creates_new_version_preserving_old_bytes(self):
        self.observe()
        original = {str(path.relative_to(self.observer.path)): path.read_bytes()
                    for path in self.observer.path.rglob("*.json")}
        previous = self.observer.path
        self.clock.set(63)
        changed = self.make(dependencies={"test": "v2"})
        self.assertNotEqual(previous, changed.path)
        self.assertEqual(original, {str(path.relative_to(previous)): path.read_bytes() for path in previous.rglob("*.json")})

    def test_scheduler_bootstrap_retry_restart_and_successful_holiday_dedup(self):
        candidate = Mock()
        candidate.snapshot.return_value = {"status": "idle"}
        candidate.start.return_value = {"status": "error", "error": "test unavailable"}
        self.observer.tick(candidate)
        self.assertEqual(candidate.start.call_count, 1)
        self.clock.value += timedelta(minutes=10)
        self.observer.tick(candidate)
        self.assertEqual(candidate.start.call_count, 1)
        self.clock.value += timedelta(minutes=21)
        self.observer.tick(candidate)
        self.assertEqual(candidate.start.call_count, 2)

        self.clock.set(62, 17)
        values = payload(self.clock)
        self.observer.observe(**values)
        self.observer = self.make()
        self.observer.tick(candidate)
        self.assertEqual(candidate.start.call_count, 2)
        self.clock.value += timedelta(days=1)
        values = payload(self.clock, 62)
        self.observer.observe(**values)  # Market closed; same as_of, new requested-through day.
        self.observer.tick(candidate)
        self.assertEqual(candidate.start.call_count, 2)

    def test_runtime_dependency_change_requires_restart_before_new_collection(self):
        self.observe()
        original_version = self.observer.version["version_id"]
        self.observer.dependencies["test"] = "edited while running"
        candidate = Mock()
        self.clock.set(63)
        self.observer.tick(candidate)
        candidate.start.assert_not_called()
        result = self.observer.snapshot()
        self.assertEqual(result["status"], "error")
        self.assertIn("서버를 다시 시작", result["error"])
        self.assertEqual(result["version_id"], original_version)

    def test_committed_no_signal_record_loss_is_not_recreated(self):
        self.observer.evaluate = lambda bars, calendar: {"eligible": True, "signal": False,
                                                        "trend": True, "reason": "no_pullback"}
        self.observe()
        path = next((self.observer.path / "records").glob("*.json"))
        path.unlink()
        self.assertEqual(self.observer.snapshot()["status"], "error")
        with self.assertRaises(ValueError):
            self.observer.observe(**payload(self.clock))
        self.assertFalse(path.exists())

    def test_committed_outcome_loss_cannot_silently_revert_to_an_older_state(self):
        self.observe()
        self.observe(64)
        events = list((self.observer.path / "outcomes").glob("*.json"))
        newest = max(events, key=lambda path: _read(path)["sequence"])
        newest.unlink()
        self.assertEqual(self.observer.snapshot()["status"], "error")
        with self.assertRaises(FileNotFoundError):
            self.observer.observe(**payload(self.clock, 64))
        self.assertFalse(newest.exists())

    def test_collection_failure_stops_running_status_and_survives_retry_backoff(self):
        self.observe()
        self.observer.collecting = True
        self.observer.next_attempt = self.clock() + timedelta(minutes=30)
        self.observer.fail("일봉 API 조회 실패")
        self.clock.value += timedelta(minutes=2)
        self.observer.tick(Mock())
        result = self.observer.snapshot()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "일봉 API 조회 실패")
        self.assertFalse(self.observer.collecting)

    def test_comparison_weights_markets_by_signal_count_then_dates_equally(self):
        records, outcomes = [], {}
        for market, count, signal_return, control_return in (("KOSPI", 9, .1, .02), ("KOSDAQ", 1, -.1, -.02)):
            for i in range(count):
                key = f"{market}-{i}"
                records.append({"record_id": key, "classification": "prospective", "signal_date": "2026-10-05",
                                "board": market, "signal": True, "control": False})
                outcomes[key] = {"status": "closed", "returns": [{"net_return": signal_return}]}
            key = f"{market}-control"
            records.append({"record_id": key, "classification": "prospective", "signal_date": "2026-10-05",
                            "board": market, "signal": False, "control": True})
            outcomes[key] = {"status": "closed", "returns": [{"net_return": control_return}]}
        comparison = ForwardObserver._comparison(records, outcomes)
        self.assertAlmostEqual(comparison["signal_mean"], .08)
        self.assertAlmostEqual(comparison["control_mean"], .016)
        self.assertAlmostEqual(comparison["edge"], .064)
        self.assertEqual(comparison["signal_count"], 10)
        outcomes["KOSDAQ-0"]["status"] = "unknown"
        self.assertEqual(ForwardObserver._comparison(records, outcomes)["unresolved_groups"], 1)
        outcomes["KOSDAQ-0"]["status"] = "closed"
        records[-1]["classification"] = "late"
        mixed = ForwardObserver._comparison(records, outcomes)
        self.assertEqual(mixed["unresolved_groups"], 1)
        self.assertEqual(mixed["control_count"], 1)

    def test_research_http_uses_local_guard_and_injected_candidate_never_auto_starts(self):
        from backend.dashboard import DashboardServer
        candidate = Mock()
        candidate.snapshot.return_value = {"status": "idle"}
        server = DashboardServer(0, service=Mock(), candidate_service=candidate, research_service=self.observer)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_port)
            for path, headers, expected in (("/api/research", {}, 403),
                                             ("/api/research?extra=1", {"X-KIS-Dashboard": "1"}, 400),
                                             ("/api/research", {"X-KIS-Dashboard": "1", "Origin": "https://bad.test"}, 403),
                                             ("/api/research", {"X-KIS-Dashboard": "1"}, 200)):
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                response.read()
            connection.close()
            candidate.start.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)


class CandidateObservationIntegrationTests(unittest.TestCase):
    def fixture(self):
        from tests.test_candidates import CandidateSelectionIntegrationTests
        fixture = CandidateSelectionIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.selector.history_sessions = 61
        return fixture

    def test_observer_gets_63_sessions_without_changing_existing_selection_features(self):
        fixture = self.fixture()
        before = fixture.finish()
        features = deepcopy(fixture.selector.calls[-1])
        observer = Mock(minimum_sessions=63)
        fixture.service.observer = observer
        after = fixture.finish()
        self.assertEqual(before["rows"], after["rows"])
        self.assertEqual(features, fixture.selector.calls[-1])
        values = observer.observe.call_args.kwargs
        self.assertTrue(all(len(days) == 63 for days in values["calendars"].values()))
        self.assertTrue(all(len(bars) == 63 for bars in values["histories"].values()))
        self.assertTrue(all(len(source["raw_sha256"]) == 64 for source in values["sources"].values()))

    def test_successful_27_session_history_is_known_ineligible_but_missing_middle_is_unknown(self):
        fixture = self.fixture()
        fixture.client.stock_data["005930"]["output2"] = fixture.client.stock_data["005930"]["output2"][-27:]
        observer = Mock(minimum_sessions=63)
        fixture.service.observer = observer
        fixture.finish()
        values = observer.observe.call_args.kwargs
        self.assertEqual(values["known_ineligible"]["005930"]["available_sessions"], 27)
        self.assertNotIn("005930", values["errors"])
        self.assertEqual(len(values["histories"]["005930"]), 27)
        other = self.fixture()
        rows = other.client.stock_data["005930"]["output2"]
        del rows[-40]  # Outside the 21-session metrics, inside the 61-session eligibility window.
        other_observer = Mock(minimum_sessions=63)
        other.service.observer = other_observer
        other.finish()
        values = other_observer.observe.call_args.kwargs
        self.assertNotIn("005930", values["known_ineligible"])
        self.assertIn("005930", values["errors"])

    def test_metric_exclusions_from_short_history_or_zero_volume_are_known_ineligible(self):
        for reason in ("short", "zero_volume"):
            with self.subTest(reason=reason):
                fixture = self.fixture()
                rows = fixture.client.stock_data["005930"]["output2"]
                if reason == "short":
                    fixture.client.stock_data["005930"]["output2"] = rows[-10:]
                else:
                    rows[-1]["acml_vol"] = "0"
                observer = Mock(minimum_sessions=63)
                fixture.service.observer = observer
                result = fixture.finish()
                self.assertEqual(result["rows"][0]["status"], "excluded")
                values = observer.observe.call_args.kwargs
                self.assertIn("005930", values["known_ineligible"])
                self.assertNotIn("005930", values["errors"])

    def test_early_worker_failure_immediately_notifies_observer(self):
        fixture = self.fixture()
        from backend.kis import KisError
        fixture.factory.side_effect = KisError("test API unavailable")
        observer = Mock(minimum_sessions=63)
        fixture.service.observer = observer
        result = fixture.finish()
        self.assertEqual(result["status"], "error")
        observer.fail.assert_called_once_with("test API unavailable")
        observer.observe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
