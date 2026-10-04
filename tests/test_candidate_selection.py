from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from backend.candidate_selection import CandidateSelector, SCORES
from backend.kis import KisError

NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


def quote(symbol, **overrides):
    return {"symbol": symbol, "status": "ok", "current_price": "1000", "unknown_fields": [],
            "temp_halted": False, "managed": False, "liquidation": False,
            "investment_caution": False, "short_overheated": False, "warning_code": "00", **overrides}


def feature(score="10", **overrides):
    return {"avg_turnover_20d": "10000000000", "zero_volume_days_20d": 0,
            "excess_20d_pp": score, "excess_60d_pp": score,
            "excess_6m_skip1m_pp": score, "excess_12to7m_pp": score,
            "risk_adjusted_rs60": score, "trend": True,
            "market_up": True, "turnover_ratio": "1.5", "extension_atr": "2", **overrides}


class Eligibility:
    def __init__(self, symbols):
        self.payload = {"status": "ok", "stale": False, "observed_at": NOW.isoformat(), "error": None,
                        "rows": {symbol: {"board": "KOSPI", "halted": False, "liquidation": False,
                            "managed": False, "low_liquidity": False, "investment_caution": False,
                            "warning_code": "00", "preferred_code": "0"} for symbol in symbols}}
        self.calls = 0

    def master_snapshot(self):
        self.calls += 1
        return deepcopy(self.payload)


class Client:
    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls = []

    def stock_status(self, symbol):
        self.calls.append(symbol)
        value = self.overrides.get(symbol, {})
        if isinstance(value, Exception):
            raise value
        return quote(symbol, **value)


class CandidateSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "policy.json"
        self.symbols = [f"{i:06d}" for i in range(30)]
        self.eligibility = Eligibility(self.symbols)
        self.selector = CandidateSelector(self.path, eligibility=self.eligibility, now=lambda: NOW)
        self.rows = [{"symbol": symbol, "board": "KOSPI", "status": "ok", "error": None}
                     for symbol in self.symbols]
        self.features = {symbol: feature(str(100 - i)) for i, symbol in enumerate(self.symbols)}
        self.policy()

    def policy(self, variant="rs60_top20", **overrides):
        payload = {"version": 1, "variant": variant, "maximum_shortlist": 20,
                   "average_turnover_20d_krw": 10_000_000_000, "minimum_raw_price_krw": 1000,
                   "decision": "test decision", "evidence_path": "test evidence", **overrides}
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_config_reads_do_not_fetch_and_change_identity(self):
        before = self.selector.policy_id
        self.assertTrue(self.selector.enabled())
        self.assertEqual(self.selector.history_sessions, 61)
        self.policy("rs12to7m_top20")
        self.assertEqual(self.selector.history_sessions, 253)
        self.assertNotEqual(before, self.selector.policy_id)
        self.assertEqual(self.eligibility.calls, 0)
        self.path.unlink()
        self.assertFalse(self.selector.enabled())
        self.policy("undefined")
        with self.assertRaises(KisError):
            self.selector.enabled()

    def test_all_filters_apply_before_limit_and_raw_price_not_adjusted_price(self):
        self.features[self.symbols[0]]["avg_turnover_20d"] = "9999999999.999"
        self.features[self.symbols[1]]["zero_volume_days_20d"] = 1
        self.eligibility.payload["rows"][self.symbols[2]]["warning_code"] = "01"
        # Historical adjusted comparison price must not satisfy the raw-current-price gate.
        self.rows[3]["close"] = "5000"
        self.rows[4]["close"] = "500"
        client = Client({self.symbols[3]: {"current_price": "999"}})
        result = self.selector.select(self.rows, self.features, client)
        self.assertEqual(result["counts"], {"selected": 20, "reserve": 6, "excluded": 4, "unverified": 0})
        self.assertEqual(self.rows[4]["selection"]["rank"], 1)
        self.assertEqual(self.rows[4]["selection"]["observed_status"]["current_price"], "1000")
        self.assertEqual(self.rows[4]["selection"]["status_observed_at"], NOW.isoformat())
        self.assertEqual(self.rows[23]["selection"]["rank"], 20)
        self.assertEqual(client.calls, self.symbols[3:24])
        self.assertIn("현재가·장중 상태 미확인", self.rows[24]["selection"]["reasons"][0])

    def test_undefined_flags_and_quote_failures_never_pass(self):
        client = Client({self.symbols[0]: {"temp_halted": None},
                         self.symbols[1]: {"warning_code": "99"},
                         self.symbols[2]: KisError("secret"),
                         self.symbols[3]: {"short_overheated": True}})
        result = self.selector.select(self.rows, self.features, client)
        self.assertEqual(result["counts"], {"selected": 20, "reserve": 6, "excluded": 1, "unverified": 3})
        self.assertNotIn("secret", json.dumps(self.rows))

    def test_stale_master_cannot_confirm_any_candidate_or_issue_quotes(self):
        self.eligibility.payload.update(status="error", stale=True, error="마스터 갱신 실패")
        client = Client()
        result = self.selector.select(self.rows, self.features, client)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["counts"]["unverified"], 30)
        self.assertEqual(client.calls, [])

    def test_all_quote_failures_report_error_instead_of_a_no_signal_result(self):
        result = self.selector.select(self.rows, self.features, Client({symbol: KisError("fail") for symbol in self.symbols}))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["counts"]["selected"], 0)
        self.assertEqual(result["error"], "30종목 선별 확인 필요")

    def test_unknown_master_code_and_missing_history_stay_unverified(self):
        self.eligibility.payload["rows"][self.symbols[0]]["preferred_code"] = "9"
        self.features[self.symbols[1]] = {"error": "연속 일봉 부족"}
        result = self.selector.select(self.rows, self.features, Client())
        self.assertEqual(result["counts"]["unverified"], 2)

    def test_no_signal_does_not_substitute_ineligible_rows(self):
        self.features = {symbol: feature("0") for symbol in self.symbols}
        client = Client()
        result = self.selector.select(self.rows, self.features, client)
        self.assertEqual(result["counts"]["selected"], 0)
        self.assertEqual(result["counts"]["excluded"], 30)
        self.assertEqual(client.calls, [])

    def test_ranking_keeps_precision_and_ties_use_symbol(self):
        self.features = {symbol: feature("1.000000001") for symbol in self.symbols}
        self.features[self.symbols[-1]] = feature("1.000000002")
        self.rows.reverse()
        self.selector.select(self.rows, self.features, Client())
        selected = sorted((row for row in self.rows if row["selection"]["status"] == "selected"),
                          key=lambda row: row["selection"]["rank"])
        self.assertEqual([row["symbol"] for row in selected], [self.symbols[-1], *self.symbols[:19]])

    def test_predicate_boundaries_use_unrounded_values(self):
        cases = [("trend_volume", "turnover_ratio", "1.4999999999999"),
                 ("trend_extension", "extension_atr", "2.000000000001"),
                 ("trend_rs20_60", "excess_20d_pp", "0"),
                 ("trend_market", "market_up", False),
                 ("trend_rs60", "trend", False)]
        for variant, field, value in cases:
            self.policy(variant)
            rows = deepcopy(self.rows)
            features = deepcopy(self.features)
            features[self.symbols[0]][field] = value
            with self.subTest(variant=variant):
                self.selector.select(rows, features, Client())
                self.assertEqual(rows[0]["selection"]["status"], "excluded")
                self.assertEqual(rows[1]["selection"]["rank"], 1)

    def test_all_registered_variants_accept_known_valid_inputs(self):
        for variant in SCORES:
            self.policy(variant)
            with self.subTest(variant=variant):
                result = self.selector.select(deepcopy(self.rows), self.features, Client())
                self.assertEqual(result["counts"]["selected"], 20)

    def test_policy_change_during_quotes_cannot_publish_mixed_result(self):
        client = Client()
        original = client.stock_status
        def changed(symbol):
            self.policy("trend_rs60")
            return original(symbol)
        client.stock_status = changed
        with self.assertRaises(KisError):
            self.selector.select(self.rows, self.features, client)


if __name__ == "__main__":
    unittest.main()
