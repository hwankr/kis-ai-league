from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.supervisor import _write
from backend.timefolio_account import AccountCollector, account_snapshot, parse_account_snapshot
from tests.test_timefolio_dom import grid, snapshot


NOW = datetime(2026, 10, 12, 1, tzinfo=timezone.utc)
BINDING = {"source_account": "competition", "source_fingerprint": "private-source",
           "account_key": "private-account", "contest": "RFM 13회 대회"}


def observation():
    value = snapshot()
    value.update(account_key=BINDING["account_key"], contest=BINDING["contest"],
                 nav=["1,000.08", "0.08 (0.01%)", "327위 / 2071명"],
                 guideline={"current_text": "금주 2.3%", "current_color": "red",
                            "confirmed_text": "금주 제외 (확정) 누적 0번 위반"})
    value["tables"][0] = grid("positions", ["prodId", "prodNm", "close", "chPct", "pos", "wei", "avgPrc"],
                              [("pos1", ["005930", "삼성전자", "100", "-1.23", "3", "1.234", "90.1234"])])
    return value


def parsed():
    return parse_account_snapshot(observation(), BINDING["account_key"], BINDING["contest"])


class TimefolioAccountTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / ".local" / "experiments" / "experiments.sqlite3"
        self.db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); "
                             "CREATE TABLE mirror_runtime(id INTEGER PRIMARY KEY,payload TEXT); "
                             "CREATE TABLE mirror_jobs(id TEXT PRIMARY KEY,payload TEXT);")
            db.execute("INSERT INTO mirror_jobs VALUES (?,?)", ("job1", json.dumps({"receipt": {
                "order_id": "ord-123", "symbol": "005930", "side": "buy", "verified": True}})))
        self.binding()
        self.state()
        self.browser = Mock()
        self.browser.account_snapshot.return_value = parsed()
        self.elapsed = 0
        self.collector = AccountCollector(self.root, self.browser, now=lambda: NOW + timedelta(seconds=self.elapsed),
                                          monotonic=lambda: self.elapsed)

    def binding(self, **changes):
        binding = {**BINDING, **changes}
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('policy',?)", (json.dumps({
                "account_id": binding["source_account"], "fingerprint": binding["source_fingerprint"]}),))
            db.execute("INSERT OR REPLACE INTO mirror_runtime VALUES (1,?)", (json.dumps(binding),))

    def state(self, status="idle", now=NOW):
        _write(self.root / ".local" / "timefolio-mirror-state.json", {"status": status, "updated_at": now.isoformat()})

    def read(self, now=NOW):
        return account_snapshot(self.root, now=now)

    def test_full_observation_keeps_exact_csv_values_and_nav_is_separate(self):
        result = parsed()
        holding = result["holdings"][0]
        self.assertEqual(holding["quantity"], 3)
        self.assertEqual(holding["market_value"], "300")
        self.assertEqual(holding["purchase_amount"], "270.3702")
        self.assertEqual(holding["pnl"], "29.6298")
        self.assertEqual(Decimal(holding["return_pct"]), Decimal("29.6298") / Decimal("270.3702") * 100)
        self.assertEqual((holding["weight_pct"], holding["change_pct"]), ("1.234", "-1.23"))
        self.assertEqual(result["summary"]["nav"], "1000.08")
        self.assertEqual(result["summary"]["holdings_value"], "300")
        self.assertEqual((result["summary"]["rank"], result["summary"]["participants"]), (327, 2071))
        self.assertEqual(result["orders"][0]["average_price"], "70000")
        self.assertEqual(result["orders"][0]["filled_quantity"], 2)
        self.assertNotIn("cash", result["summary"])

    def test_missing_average_is_unknown_not_zero_profit(self):
        value = observation()
        value["tables"][0]["csv_text"] = value["tables"][0]["csv_text"].replace("90.1234", "")
        result = parse_account_snapshot(value, BINDING["account_key"], BINDING["contest"])
        for key in ("avg_price", "purchase_amount", "pnl", "return_pct"):
            self.assertIsNone(result["holdings"][0][key])
        self.assertIsNone(result["summary"]["holdings_pnl"])
        self.assertIsNone(result["summary"]["holdings_return_pct"])
        self.assertEqual(result["summary"]["holdings_value"], "300")

    def test_complete_empty_holdings_are_zero_and_unranked_is_null(self):
        value = observation()
        value["tables"][0] = grid("positions", ["prodId", "pos", "wei"], [])
        value["nav"] = ["1000", "0 (0%)"]
        result = parse_account_snapshot(value, BINDING["account_key"], BINDING["contest"])
        self.assertEqual(result["holdings"], [])
        self.assertEqual(result["summary"]["holdings_value"], "0")
        self.assertEqual(result["summary"]["holdings_pnl"], "0")
        self.assertIsNone(result["summary"]["holdings_return_pct"])
        self.assertIsNone(result["summary"]["rank"])

    def test_receipt_only_or_incomplete_csv_never_becomes_empty_balance(self):
        for change in ("receipt", "incomplete", "wrong_identity", "nav", "quantity"):
            with self.subTest(change=change):
                value = observation()
                if change == "receipt":
                    value["tables"] = [t for t in value["tables"] if t["kind"] not in {"positions", "targets"}]
                elif change == "incomplete":
                    value["tables"][0]["complete"] = False
                elif change == "wrong_identity":
                    value["account_key"] = "other"
                elif change == "nav":
                    value["nav"] = []
                else:
                    value["tables"][0]["csv_text"] = value["tables"][0]["csv_text"].replace(",3,", ",3.5,")
                with self.assertRaises(ValueError):
                    parse_account_snapshot(value, BINDING["account_key"], BINDING["contest"])

    def test_collector_first_tick_then_sixty_seconds_and_public_data_only(self):
        self.collector.tick()
        self.elapsed = 59
        self.collector.tick()
        self.browser.account_snapshot.assert_called_once()
        self.elapsed = 60
        self.collector.tick()
        self.assertEqual(self.browser.account_snapshot.call_count, 2)
        result = self.read(now=NOW + timedelta(seconds=60))
        self.assertEqual((result["status"], result["stale"]), ("ok", False))
        self.assertTrue(result["orders"][0]["system_owned"])
        self.assertEqual(result["refresh_interval_seconds"], 60)
        self.assertEqual(result["account"]["name"], "타임폴리오")
        text = json.dumps(result)
        for value in ("private-source", "private-account", "account_key", "source_fingerprint", "csv_text"):
            self.assertNotIn(value, text)

    def test_external_orders_and_receipt_identity_mismatch_are_not_owned(self):
        self.collector.tick()
        for field, value in (("order_id", "other"), ("symbol", "000660"), ("side", "sell"), ("verified", False)):
            with self.subTest(field=field), closing(sqlite3.connect(self.db)) as db, db:
                receipt = {"order_id": "ord-123", "symbol": "005930", "side": "buy", "verified": True, field: value}
                db.execute("UPDATE mirror_jobs SET payload=?", (json.dumps({"receipt": receipt}),))
            self.assertFalse(self.read()["orders"][0]["system_owned"])

    def test_collection_failure_retains_last_good_and_sanitizes_auth_and_other_errors(self):
        self.collector.tick()
        previous = self.read()
        for message, expected in (("timefolio_login_required", "로그인"),
                                  ("password=private-account https://private.example", "갱신")):
            with self.subTest(message=message):
                self.elapsed += 60
                self.browser.account_snapshot.side_effect = RuntimeError(message)
                self.collector.tick()
                result = self.read(now=NOW + timedelta(seconds=self.elapsed))
                self.assertEqual((result["status"], result["stale"]), ("error", True))
                self.assertEqual(result["holdings"], previous["holdings"])
                self.assertEqual(result["updated_at"], previous["updated_at"])
                self.assertIn(expected, result["error"])
                self.assertNotIn("password", (self.root / ".local" / "timefolio-account.json").read_text(encoding="utf-8"))

    def test_staleness_boundary_and_stopped_collector(self):
        self.collector.tick()
        self.assertFalse(self.read(now=NOW + timedelta(seconds=179))["stale"])
        self.assertTrue(self.read(now=NOW + timedelta(seconds=180))["stale"])
        self.state("stopped")
        self.assertTrue(self.read()["stale"])
        self.assertEqual(len(self.read()["holdings"]), 1)

    def test_changed_source_or_destination_hides_previous_account(self):
        self.collector.tick()
        for key in BINDING:
            with self.subTest(key=key):
                self.binding(**{key: "changed"})
                result = self.read()
                self.assertEqual(result["status"], "waiting")
                self.assertIsNone(result["summary"])
                self.assertEqual(result["holdings"], [])
                self.assertEqual(result["orders"], [])
                self.assertIsNone(result["turnover"]["current_pct"])
                self.assertIsNone(result["turnover"]["missed_weeks"])
                self.binding()

    def test_binding_change_during_collection_is_not_published(self):
        def changed():
            self.binding(account_key="changed")
            return parsed()
        self.browser.account_snapshot.side_effect = changed
        self.collector.tick()
        self.assertEqual(self.read()["holdings"], [])
        self.assertEqual(self.read()["status"], "waiting")

    def test_write_failure_does_not_escape_or_change_ledger(self):
        before = self.db.read_bytes()
        with patch("backend.timefolio_account._write", side_effect=OSError("private-file-path")):
            self.collector.tick()
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.read()["status"], "waiting")

    def test_inflight_or_unknown_orders_defer_extra_account_reads(self):
        for status in ("submitting", "unknown", "working"):
            with self.subTest(status=status):
                with closing(sqlite3.connect(self.db)) as db, db:
                    db.execute("UPDATE mirror_jobs SET payload=?", (json.dumps({"status": status}),))
                self.collector.tick()
                self.elapsed += 60
        self.browser.account_snapshot.assert_not_called()
        self.assertFalse((self.root / ".local" / "timefolio-account.json").exists())

    def test_get_does_not_create_files_and_malformed_values_are_safe(self):
        missing = self.root / "absent"
        self.assertEqual(account_snapshot(missing, now=NOW)["status"], "waiting")
        self.assertFalse(missing.exists())
        self.collector.tick()
        path = self.root / ".local" / "timefolio-account.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["snapshot"]["summary"]["nav"] = {"password": "private"}
        _write(path, saved)
        result = self.read()
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["summary"])
        self.assertNotIn("private", json.dumps(result))
        path.write_text("not-json private", encoding="utf-8")
        self.assertEqual(self.read()["status"], "error")

    def test_get_drops_unapproved_fields_from_saved_snapshot(self):
        self.collector.tick()
        path = self.root / ".local" / "timefolio-account.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["snapshot"].update(password="private-password", csv_text="private-csv")
        saved["snapshot"]["holdings"][0]["secret"] = "private-secret"
        saved["snapshot"]["orders"][0]["token"] = "private-token"
        _write(path, saved)
        self.assertNotIn("private", json.dumps(self.read()))

    def publish_guideline(self, guideline, *, session_date="2026-10-12", now=NOW):
        value = observation()
        value.update(guideline=guideline, session_date=session_date)
        self.browser.account_snapshot.return_value = parse_account_snapshot(value, BINDING["account_key"], BINDING["contest"])
        self.collector.next_at = 0
        self.collector.tick()
        return self.read(now=now)["turnover"]

    def test_turnover_uses_official_display_and_confirmed_count(self):
        self.collector.tick()
        self.assertEqual(self.read()["turnover"], {
            "week_start": "2026-10-12", "week_end": "2026-10-16", "current_pct": "2.3",
            "target_pct": 5, "status": "below", "missed_weeks": 0, "max_missed_weeks": 3,
            "source": "official_guideline"})
        saved = json.loads((self.root / ".local" / "timefolio-account.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["snapshot"]["turnover"]["official_status"], "below")
        self.assertNotIn("confirmed_text", json.dumps(saved))

    def test_rounded_five_requires_official_color_and_preserves_display_precision(self):
        for color, expected in (("red", "below"), ("black", "met"),
                                ("rgb(255, 0, 0)", "below"), ("rgb(0, 0, 0)", "met"),
                                ("", "unknown"), ("green", "unknown"), ([], "unknown")):
            with self.subTest(color=color):
                result = self.publish_guideline({"current_text": "금주 5.0%", "current_color": color})
                self.assertEqual(result["current_pct"], "5.0")
                self.assertEqual(result["status"], expected)
                self.assertIsNone(result["missed_weeks"])

    def test_conflicting_official_color_does_not_claim_compliance(self):
        for display, color in (("4.9", "black"), ("5.1", "red")):
            with self.subTest(display=display):
                result = self.publish_guideline({"current_text": f"금주 {display}%", "current_color": color})
                self.assertEqual(result["current_pct"], display)
                self.assertEqual(result["status"], "unknown")

    def test_projected_violations_are_not_confirmed_missed_weeks(self):
        for text in ("금주 포함 (예상) 누적 4번 위반", "누적 4번 위반", "금주 제외 (확정) 누적 -1번 위반"):
            with self.subTest(text=text):
                result = self.publish_guideline({"current_text": "금주 2.3%", "current_color": "red", "confirmed_text": text})
                self.assertIsNone(result["missed_weeks"])
        result = self.publish_guideline({"confirmed_text": "금주 제외 (확정) 누적 4번 위반"})
        self.assertEqual(result["missed_weeks"], 4)
        self.assertIsNone(result["current_pct"])
        self.assertEqual(result["status"], "unknown")

    def test_missing_or_invalid_guideline_does_not_invent_turnover_or_drop_balance(self):
        for value in (None, {}, "private", {"current_text": "금주 NaN%", "current_color": "black"},
                      {"current_text": "금주 -1.0%", "current_color": "red"},
                      {"current_text": "금주 5.00%", "current_color": "black"}):
            with self.subTest(value=value):
                result = self.publish_guideline(value)
                self.assertIsNone(result["current_pct"])
                self.assertIsNone(result["missed_weeks"])
                self.assertIsNone(result["source"])
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(self.read()["summary"]["nav"], "1000.08")

    def test_old_week_and_future_session_do_not_become_current_week_progress(self):
        for session_date in ("2026-10-09", "2026-10-13"):
            with self.subTest(session_date=session_date):
                result = self.publish_guideline(observation()["guideline"], session_date=session_date)
                self.assertEqual(result["week_start"], "2026-10-12")
                self.assertIsNone(result["current_pct"])
                self.assertIsNone(result["missed_weeks"])
                self.assertIsNone(result["source"])
                self.assertEqual(result["status"], "unknown")

    def test_week_rollover_uses_korean_date(self):
        self.collector.tick()
        sunday = datetime(2026, 10, 18, 14, 59, tzinfo=timezone.utc)
        self.assertEqual(self.read(now=sunday)["turnover"]["current_pct"], "2.3")
        monday = sunday + timedelta(minutes=1)
        result = self.read(now=monday)["turnover"]
        self.assertEqual(result["week_start"], "2026-10-19")
        self.assertIsNone(result["current_pct"])
        self.assertEqual(result["status"], "unknown")

    def test_october_fifth_week_is_exempt_and_regular_week_resumes(self):
        result = self.publish_guideline(observation()["guideline"], session_date="2026-10-08",
                                        now=datetime(2026, 10, 8, 1, tzinfo=timezone.utc))
        self.assertEqual((result["week_start"], result["week_end"], result["status"]),
                         ("2026-10-05", "2026-10-09", "exempt"))
        self.assertEqual(self.publish_guideline(observation()["guideline"])["status"], "below")

    def test_legacy_saved_snapshot_without_turnover_is_backward_compatible(self):
        self.browser.account_snapshot.return_value.pop("turnover")
        self.collector.tick()
        result = self.read()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["turnover"]["status"], "unknown")
        self.assertIsNone(result["turnover"]["current_pct"])
        self.assertIsNone(result["turnover"]["missed_weeks"])

    def test_malformed_saved_turnover_is_unknown_without_exposing_raw_fields(self):
        self.collector.tick()
        path = self.root / ".local" / "timefolio-account.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["snapshot"]["turnover"].update(current_pct="NaN", missed_weeks="private", secret="private")
        _write(path, saved)
        result = self.read()
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["turnover"]["current_pct"])
        self.assertEqual(result["turnover"]["status"], "unknown")
        self.assertNotIn("private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
