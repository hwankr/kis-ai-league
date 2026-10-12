from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.experiment_store import ExperimentStore, encode
from backend.learning_benchmarks import benchmark_signature, refresh_market_comparison


TODAY = "2026-10-12"
LATER = "2026-10-13"
AT = "2026-10-12T10:00:00+00:00"


class LearningBenchmarksTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = ExperimentStore(Path(folder.name) / "experiments.sqlite3")
        with self.store.connect() as db:
            db.execute("CREATE TABLE learning_frames (day TEXT PRIMARY KEY,digest TEXT,payload TEXT)")
        self.frame(TODAY)
        self.state = {"scorecard_start": TODAY, "scorecard_day": TODAY, "scorecard_market": None,
                      "scorecard_capital": "1000000", "scorecard_book": {"as_of": TODAY, "positions": []},
                      "trial": {"start_day": TODAY, "elapsed_intervals": 0},
                      "scorecard": {"start_day": TODAY, "status": "ready", "error": None,
                                    "baseline_return_pct": 0., "ai_return_pct": -1., "market_return_pct": None}}
        self.store.save_setting("learning", self.state)
        self.store.save_setting("policy", {"budget": "1000000"})

    def frame(self, day, benchmark=None):
        frame = {"as_of": day, "benchmark_prices": {} if benchmark is None else {"KOSPI": benchmark},
                 "rows": [], "captured_at": AT}
        raw = encode(frame)
        with self.store.connect() as db:
            db.execute("INSERT INTO learning_frames VALUES (?,?,?)",
                       (day, hashlib.sha256(raw.encode()).hexdigest(), raw))

    def data(self, prices=None, as_of=TODAY):
        return {"as_of": as_of, "observed_at": AT, "benchmarks": {"KOSPI": prices or {}}}

    def supplements(self):
        with self.store.connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='learning_frame_benchmarks'").fetchone()
            return db.execute("SELECT * FROM learning_frame_benchmarks ORDER BY day,board").fetchall() if exists else []

    def protected(self):
        with self.store.connect() as db:
            return {table: db.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
                    for table in ("learning_frames", "settings", "runs", "orders")}

    def test_missing_then_same_day_recovery_preserves_frozen_rows_and_state_anchors(self):
        before = self.protected()
        self.assertTrue(refresh_market_comparison(self.store, self.state, self.data(), AT))
        error = self.state["scorecard_market_error"]
        self.assertTrue(error)
        self.assertEqual(self.state["scorecard"]["error"], error)
        self.assertIsNone(self.state["scorecard"]["market_return_pct"])
        self.assertEqual(self.supplements(), [])
        self.assertFalse(refresh_market_comparison(self.store, self.state, self.data(), AT))
        data = self.data({date(2026, 10, 12): Decimal("6384.24")})
        self.assertTrue(refresh_market_comparison(self.store, self.state, data, AT))
        self.assertEqual(self.state["scorecard_market"], "6384.24")
        self.assertEqual(self.state["scorecard"]["market_return_pct"], 0.)
        self.assertIsNone(self.state["scorecard_market_error"])
        self.assertIsNone(self.state["scorecard"]["error"])
        self.assertEqual(self.state["scorecard_start"], TODAY)
        self.assertEqual(self.state["scorecard_capital"], "1000000")
        self.assertEqual(self.state["trial"], {"start_day": TODAY, "elapsed_intervals": 0})
        self.assertEqual(before, self.protected())

    def test_supplement_contains_immutable_provenance_and_retries_are_idempotent(self):
        data = self.data({TODAY: "6384.24"})
        refresh_market_comparison(self.store, self.state, data, AT)
        rows = self.supplements()
        day, board, digest, raw = rows[0]
        payload = json.loads(raw)
        self.assertEqual((day, board), (TODAY, "KOSPI"))
        with self.store.connect() as db:
            self.assertEqual(digest, db.execute("SELECT digest FROM learning_frames WHERE day=?", (TODAY,)).fetchone()[0])
        self.assertEqual(payload, {"price": "6384.24", "source_input_hash": benchmark_signature(data, TODAY),
                                  "source_as_of": TODAY, "observed_at": AT, "recovered_at": AT})
        self.assertFalse(refresh_market_comparison(self.store, self.state, data, "2026-10-12T11:00:00+00:00"))
        self.assertEqual(rows, self.supplements())

    def test_later_day_keeps_initial_anchor_even_when_old_cache_entry_is_absent(self):
        refresh_market_comparison(self.store, self.state, self.data({TODAY: "1000"}), AT)
        self.frame(LATER)
        self.state["scorecard_day"] = LATER
        refresh_market_comparison(self.store, self.state, self.data({LATER: "1100"}, LATER), AT)
        self.assertEqual(Decimal(self.state["scorecard_market"]), Decimal(1000))
        self.assertEqual(self.state["scorecard_start"], TODAY)
        self.assertEqual(self.state["scorecard"]["market_return_pct"], 10.)
        self.assertEqual(len(self.supplements()), 2)

    def test_late_first_recovery_uses_original_start_day_not_current_day(self):
        self.frame(LATER)
        self.state["scorecard_day"] = LATER
        refresh_market_comparison(self.store, self.state, self.data({LATER: "1100"}, LATER), AT)
        self.assertIsNone(self.state["scorecard_market"])
        self.assertIsNone(self.state["scorecard"]["market_return_pct"])
        refresh_market_comparison(self.store, self.state, self.data({TODAY: "1000", LATER: "1100"}, LATER), AT)
        self.assertEqual(Decimal(self.state["scorecard_market"]), Decimal(1000))
        self.assertEqual(self.state["scorecard"]["market_return_pct"], 10.)

    def test_baseline_failure_is_preserved_during_market_failure_and_recovery(self):
        self.state["scorecard"].update(status="invalid", error="기존 정책 평가 자료 부족")
        refresh_market_comparison(self.store, self.state, self.data(), AT)
        self.assertEqual(self.state["scorecard"]["error"], "기존 정책 평가 자료 부족")
        refresh_market_comparison(self.store, self.state, self.data({TODAY: 1000}), AT)
        self.assertEqual(self.state["scorecard"]["error"], "기존 정책 평가 자료 부족")
        self.assertEqual(self.state["scorecard"]["status"], "invalid")
        self.assertIsNone(self.state["scorecard_market_error"])
        self.assertEqual(self.state["scorecard"]["market_return_pct"], 0.)

    def test_existing_frozen_frame_price_is_authoritative_without_supplement(self):
        self.frame(LATER, 1100.)
        self.state.update(scorecard_start=LATER, scorecard_day=LATER)
        refresh_market_comparison(self.store, self.state, self.data({}, LATER), AT)
        self.assertEqual(Decimal(self.state["scorecard_market"]), Decimal(1100))
        self.assertEqual(self.state["scorecard"]["market_return_pct"], 0.)
        self.assertEqual(self.supplements(), [])

    def test_supplement_and_input_conflict_preserves_original_price(self):
        refresh_market_comparison(self.store, self.state, self.data({TODAY: 1000}), AT)
        rows = self.supplements()
        anchor = self.state["scorecard_market"]
        refresh_market_comparison(self.store, self.state, self.data({TODAY: 1001}), AT)
        self.assertIn("충돌", self.state["scorecard_market_error"])
        self.assertIsNone(self.state["scorecard"]["market_return_pct"])
        self.assertEqual(self.state["scorecard_market"], anchor)
        self.assertEqual(rows, self.supplements())

    def test_existing_anchor_conflict_cannot_create_a_new_supplement(self):
        self.state["scorecard_market"] = "999"
        refresh_market_comparison(self.store, self.state, self.data({TODAY: 1000}), AT)
        self.assertEqual(self.state["scorecard_market"], "999")
        self.assertIn("충돌", self.state["scorecard_market_error"])
        self.assertEqual(self.supplements(), [])

    def test_supplement_frame_digest_conflict_does_not_overwrite_provenance(self):
        refresh_market_comparison(self.store, self.state, self.data({TODAY: 1000}), AT)
        with self.store.connect() as db:
            db.execute("UPDATE learning_frame_benchmarks SET frame_digest='different'")
        rows = self.supplements()
        refresh_market_comparison(self.store, self.state, self.data({TODAY: 1000}), AT)
        self.assertIn("일치하지", self.state["scorecard_market_error"])
        self.assertEqual(rows, self.supplements())

    def test_invalid_prices_and_conflicting_date_aliases_are_visible(self):
        for value in (0, -1, float("nan"), float("inf"), True, "invalid"):
            with self.subTest(value=value):
                refresh_market_comparison(self.store, self.state, self.data({TODAY: value}), AT)
                self.assertTrue(self.state["scorecard_market_error"])
                self.assertIsNone(self.state["scorecard"]["market_return_pct"])
                self.assertEqual(self.supplements(), [])
        refresh_market_comparison(self.store, self.state,
                                  self.data({TODAY: 1000, date(2026, 10, 12): 1001}), AT)
        self.assertIn("충돌", self.state["scorecard_market_error"])
        self.assertEqual(self.supplements(), [])

    def test_future_and_missing_frame_are_not_replaced_with_available_current_prices(self):
        self.state["scorecard_day"] = LATER
        refresh_market_comparison(self.store, self.state, self.data({TODAY: 1000, LATER: 1100}), AT)
        self.assertIsNone(self.state["scorecard"]["market_return_pct"])
        self.assertEqual(self.supplements(), [])
        refresh_market_comparison(self.store, self.state, self.data({TODAY: 1000, LATER: 1100}, LATER), AT)
        self.assertIn("학습 관측이 없습니다", self.state["scorecard_market_error"])
        self.assertEqual(self.supplements(), [])

    def test_signature_normalizes_dates_and_detects_missing_conflicting_or_new_start_prices(self):
        a = self.data({TODAY: "1000"})
        b = self.data({date(2026, 10, 12): Decimal("1000.0")})
        self.assertEqual(benchmark_signature(a, TODAY), benchmark_signature(b, date(2026, 10, 12)))
        signatures = {benchmark_signature(data, "2026-10-08") for data in (
            self.data(), a, self.data({TODAY: "1001"}),
            self.data({TODAY: "1000", date(2026, 10, 12): "1001"}),
            self.data({TODAY: "1000", date(2026, 10, 12): "1002"}),
            self.data({TODAY: "1000", "2026-10-08": "900"}))}
        self.assertEqual(len(signatures), 6)
        self.assertNotEqual(benchmark_signature(self.data({TODAY: float("nan")})),
                            benchmark_signature(self.data({TODAY: float("inf")})))

    def test_no_scorecard_is_a_noop(self):
        state = {"trial": {"elapsed_intervals": 0}}
        before = deepcopy(state)
        self.assertFalse(refresh_market_comparison(self.store, state, self.data({TODAY: 1000}), AT))
        self.assertEqual(state, before)
        self.assertEqual(self.supplements(), [])


if __name__ == "__main__":
    unittest.main()
