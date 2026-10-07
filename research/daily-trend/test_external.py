"""외부 자료의 시점·결측·출처와 날짜 평균 경계검사. 네트워크·실제 성과를 사용하지 않는다."""
import hashlib
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import external_context as external
import context_diagnostics as diagnostics


def observation(day, value):
    return {"observation_date": day, "value": value}


def trade(day, symbol, net, **changes):
    return {"rule_id": "time5", "slippage": .001, "lookback": 20,
            "paired_observed": True, "period": "development", "signal_date": day,
            "symbol": symbol, "net_return": net, **changes}


def daily_rows():
    return [trade("2026-10-01", "A", 0), trade("2026-10-01", "B", .2),
            trade("2026-10-02", "A", -.1)]


def fake_response(request, timeout):
    series = parse_qs(urlparse(request.full_url).query)["id"][0]
    response = io.BytesIO(f"observation_date,{series}\n2026-10-01,17\n".encode())
    response.status = 200
    response.url = request.full_url
    response.headers = {"Content-Type": "application/csv"}
    return response


class ExternalContextTests(unittest.TestCase):
    def test_same_day_and_future_observations_never_enter_korean_signal(self):
        observations = {"VIXCLS": [observation("2026-09-30", 16),
                                    observation("2026-10-01", 17),
                                    observation("2026-10-02", 99),
                                    observation("2026-10-05", 100)]}
        result = external.align_context(["2026-10-02"], observations)[0]
        self.assertEqual((result["observation_date"], result["value"]), ("2026-10-01", 17))
        self.assertFalse(result["point_in_time"])
        self.assertTrue(result["diagnostics_only"])

    def test_h10_uses_fourteen_calendar_day_lag_not_one_day(self):
        observations = {"DEXKOUS": [observation("2026-09-18", 1300),
                                     observation("2026-09-19", 1350),
                                     observation("2026-09-25", 1400)]}
        result = next(row for row in external.align_context(["2026-10-02"], observations)
                      if row["series_id"] == "DEXKOUS")
        self.assertEqual((result["observation_date"], result["value"], result["age_days"]),
                         ("2026-09-18", 1300, 14))

    def test_stale_no_prior_and_unavailable_keep_null_values(self):
        observations = {"VIXCLS": [observation("2026-10-01", 17)]}
        for signal, expected in (("2026-10-01", "no_prior_observation"), ("2026-11-20", "stale")):
            with self.subTest(signal=signal):
                result = external.align_context([signal], observations)[0]
                self.assertEqual(result["status"], expected)
                self.assertIsNone(result["value"])
        missing = external.align_context(["2026-10-02"], observations)[1]
        self.assertEqual(missing["status"], "unavailable")
        self.assertIsNone(missing["value"])

    def test_blank_and_dot_observations_are_missing_not_zero(self):
        raw = b"observation_date,VIXCLS\n2026-09-29,.\n2026-09-30,\n2026-10-01,17\n"
        self.assertEqual(external.parse_csv(raw, "VIXCLS"), [observation("2026-10-01", 17)])

    def test_nan_duplicate_and_invalid_header_are_rejected(self):
        invalid = [b"observation_date,VIXCLS\n2026-10-01,NaN\n",
                   b"observation_date,VIXCLS\n2026-10-01,1\n2026-10-01,2\n",
                   b"<html>error</html>"]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                external.parse_csv(raw, "VIXCLS")

    def test_offline_and_incomplete_http_record_failures_without_fallback(self):
        for error in (OSError("offline"), IncompleteRead(b"partial")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as folder:
                with patch.object(external, "urlopen", side_effect=error) as network:
                    manifest = external.fetch_snapshot(folder, "2026-01-01", "2026-10-02")
                self.assertEqual(network.call_count, len(external.SERIES))
                self.assertTrue(Path(manifest["manifest_path"]).exists())
                for source in manifest["series"].values():
                    self.assertEqual(source["status"], "unavailable")
                    self.assertIsNone(source["raw_file"])
                    self.assertIn(type(error).__name__, source["error"])
                    self.assertIn("retrieved_at_utc", source)
                loaded, _ = external.load_snapshot(manifest["manifest_path"])
                self.assertTrue(all(not rows for rows in loaded.values()))

    def test_snapshot_raw_hash_is_verified_and_tampering_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(external, "urlopen", side_effect=fake_response):
                manifest = external.fetch_snapshot(folder, "2026-01-01", "2026-10-02")
            loaded, _ = external.load_snapshot(manifest["manifest_path"])
            self.assertEqual(loaded["VIXCLS"], [observation("2026-10-01", 17)])
            raw_path = Path(manifest["manifest_path"]).parent / "VIXCLS.csv"
            self.assertEqual(hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                             manifest["series"]["VIXCLS"]["sha256"])
            raw_path.write_bytes(b"observation_date,VIXCLS\n2026-10-01,999\n")
            loaded, checked = external.load_snapshot(manifest["manifest_path"])
            self.assertEqual(loaded["VIXCLS"], [])
            self.assertEqual(checked["series"]["VIXCLS"]["status"], "unavailable")
            self.assertIn("SHA256", checked["series"]["VIXCLS"]["load_error"])
            self.assertEqual(loaded["DGS10"], [observation("2026-10-01", 17)])


class ContextDiagnosticsTests(unittest.TestCase):
    def test_each_signal_day_has_equal_weight_despite_different_stock_counts(self):
        rows = daily_rows() + [trade("2026-10-02", "Z", 99, slippage=.002),
                               trade("2026-10-02", "Q", 99, paired_observed=False)]
        days, counts = diagnostics.aggregate_signals(rows)
        self.assertEqual((counts["included_trades"], counts["signal_days"]), (3, 2))
        self.assertEqual([day["net_return"] for day in days], [.1, -.1])
        summary = diagnostics.group_summary("all", days, len(days))
        self.assertEqual(summary["mean_net_return"], 0)
        self.assertNotEqual(summary["mean_net_return"], sum(row["net_return"] for row in daily_rows()) / 3)

    def test_market_difference_requires_matching_day_mean_and_stock_count(self):
        days, _ = diagnostics.aggregate_signals(daily_rows())
        market_rows = [{"rule": "time5", "slip": .001, "market": "ALL", "period": "development",
                        "date": "2026-10-01", "net": .1, "edge": .03, "observed_count": 2},
                       {"rule": "time5", "slip": .001, "market": "ALL", "period": "development",
                        "date": "2026-10-02", "net": -.1, "edge": .9, "observed_count": 2}]
        diagnostics.attach_market_difference(days, market_rows)
        self.assertEqual(days[0]["market_difference"], .03)
        self.assertIsNone(days[1]["market_difference"])
        self.assertEqual(days[1]["market_difference_status"], "daily_cohort_mismatch")
        market_rows[0].update(net=.2, observed_count=2)
        new_days, _ = diagnostics.aggregate_signals(daily_rows())
        diagnostics.attach_market_difference(new_days, market_rows)
        self.assertIsNone(new_days[0]["market_difference"])

    def test_threshold_boundary_and_failure_groups_preserve_all_signal_days(self):
        days, _ = diagnostics.aggregate_signals(daily_rows())
        observations = {"VIXCLS": [observation("2026-09-30", 20), observation("2026-10-01", 30)],
                        "DGS10": [observation("2026-09-30", 4)]}
        summaries = diagnostics.summarize_context(days, observations)
        by_series = {row["series_id"]: row for row in summaries if row["period"] == "development"}
        vix = by_series["VIXCLS"]
        self.assertEqual([group["signal_days"] for group in vix["groups"][:2]], [1, 1])
        self.assertEqual([group["mean_net_return"] for group in vix["groups"][:2]], [.1, -.1])
        self.assertEqual(vix["missing_rate"], 0)
        self.assertEqual(by_series["DGS10"]["status_counts"]["insufficient_change_history"], 2)
        fx = by_series["DEXKOUS"]
        self.assertEqual((fx["missing_days"], fx["missing_rate"]), (2, 1))
        failure = next(group for group in fx["groups"] if group["group"] == "unavailable")
        self.assertEqual((failure["signal_days"], failure["trades"], failure["mean_net_return"]), (2, 3, 0))
        empty = next(row for row in summaries if row["period"] == "validation")
        self.assertIsNone(empty["missing_rate"])
        json.dumps(summaries, allow_nan=False)

    def test_duplicate_or_nonfinite_completed_trades_are_rejected(self):
        for extra in (daily_rows()[0], trade("2026-10-02", "X", float("nan"))):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                diagnostics.aggregate_signals(daily_rows() + [extra])

    def test_unfinished_study_files_are_not_reported_as_completed(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(FileNotFoundError):
            diagnostics.build_report(folder, "not_read.json")


if __name__ == "__main__":
    unittest.main()
