from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import unittest

from backend.experiment_shadow import update_shadow


class Store:
    def __init__(self):
        self.saved = {}
        self.writes = 0

    def setting(self, key, default=None):
        return deepcopy(self.saved.get(key, default))

    def save_setting(self, key, value):
        self.saved[key] = deepcopy(value)
        self.writes += 1


def fixture(created_at="2026-10-02T08:00:00+00:00"):
    # Exchange calendar deliberately omits weekend and the Oct 9 holiday.
    days = [date.fromisoformat(value) for value in
            ("2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-12", "2026-10-13")]
    bars = {day: {"open": str(98 + index), "high": str(100 + index), "low": str(97 + index),
                  "close": str(99 + index), "volume": "100", "turnover": "10000"}
            for index, day in enumerate(days)}
    run = {"id": "run1", "version_id": "v1", "as_of": "2026-10-02", "created_at": created_at,
           "signals": [{"strategy_id": "trend-breakout-v1", "symbol": "005930", "board": "KOSPI",
                        "as_of": "2026-10-02", "action": "buy", "status": "ready"}],
           "analysis": {"input": {"calendars": {"KOSPI": [day.isoformat() for day in days[:2]]},
                                      "histories": {"005930": {days[1].isoformat(): bars[days[1]]}}}}}
    return run, {"005930": bars}, {"KOSPI": days}


class ShadowTests(unittest.TestCase):
    def test_first_ready_hold_cannot_be_replaced_by_later_buy(self):
        run, bars, calendar = fixture()
        run["signals"][0]["action"] = "hold"
        store = Store()
        self.assertEqual(update_shadow(store, [run], bars, calendar, "2026-10-13")[0]["shadow_closed"], 0)
        later = deepcopy(run)
        later.update(id="run2", created_at="2026-10-02T09:00:00+00:00")
        later["signals"][0]["action"] = "buy"
        result = update_shadow(store, [later, run], bars, calendar, "2026-10-13")[0]
        self.assertEqual(result["shadow_signals"], 0)
        self.assertIsNone(result["shadow_net_pct"])

    def test_future_bars_never_used_and_no_next_day_yet_is_pending(self):
        run, bars, calendar = fixture()
        store = Store()
        item = update_shadow(store, [run], bars, calendar, "2026-10-02")[0]
        self.assertEqual(item["shadow_pending"], 1)
        self.assertEqual(item["shadow_open"], 0)
        self.assertEqual(item["shadow_closed"], 0)
        self.assertIsNone(item["shadow_net_pct"])

    def test_entry_next_actual_session_five_sessions_then_next_open(self):
        run, bars, calendar = fixture()
        store = Store()
        item = update_shadow(store, [run], bars, calendar, "2026-10-12")[0]
        self.assertEqual(item["shadow_open"], 1)
        record = next(iter(store.saved["shadow"]["records"].values()))
        self.assertEqual(record["entry_date"], "2026-10-05")
        self.assertIsNone(record["exit_date"])
        item = update_shadow(store, [run], bars, calendar, "2026-10-13")[0]
        self.assertEqual(item["shadow_closed"], 1)
        record = next(iter(store.saved["shadow"]["records"].values()))
        self.assertEqual(record["exit_date"], "2026-10-13")
        expected = (Decimal(105) * Decimal("0.999") * (1 - Decimal("0.000140527") - Decimal("0.002")) /
                    (Decimal(100) * Decimal("1.001") * (1 + Decimal("0.000140527"))) - 1) * 100
        stress = (Decimal(105) * Decimal("0.998") * (1 - Decimal("0.000140527") - Decimal("0.002")) /
                  (Decimal(100) * Decimal("1.002") * (1 + Decimal("0.000140527"))) - 1) * 100
        self.assertEqual(Decimal(item["shadow_net_pct"]), expected)
        self.assertEqual(Decimal(item["shadow_stress_pct"]), stress)

    def test_late_signal_excluded_at_exact_actual_open(self):
        run, bars, calendar = fixture("2026-10-05T00:00:00+00:00")
        store = Store()
        item = update_shadow(store, [run], bars, calendar, "2026-10-13")[0]
        self.assertEqual(item["shadow_excluded"], 1)
        self.assertIsNone(item["shadow_net_pct"])
        record = next(iter(store.saved["shadow"]["records"].values()))
        self.assertEqual(record["reason"], "late_signal")
        run["created_at"] = "2026-10-04T23:59:59+00:00"
        self.assertEqual(update_shadow(Store(), [run], bars, calendar, "2026-10-13")[0]["shadow_closed"], 1)

    def test_analysis_completion_time_not_input_timestamp_controls_eligibility(self):
        run, bars, calendar = fixture("2026-10-05T00:01:00+00:00")
        run["analysis"]["observed_at"] = "2026-10-02T08:00:00+00:00"
        self.assertEqual(update_shadow(Store(), [run], bars, calendar, "2026-10-13")[0]["shadow_excluded"], 1)

    def test_missing_bar_not_zero_and_never_backfilled_into_return(self):
        run, bars, calendar = fixture()
        store = Store()
        original = bars["005930"].pop(date(2026, 10, 7))
        item = update_shadow(store, [run], bars, calendar, "2026-10-13")[0]
        self.assertEqual(item["shadow_unknown"], 1)
        self.assertIsNone(item["shadow_net_pct"])
        bars["005930"][date(2026, 10, 7)] = original
        self.assertEqual(update_shadow(store, [run], bars, calendar, "2026-10-13")[0]["shadow_unknown"], 1)

    def test_zero_volume_flat_or_invalid_price_is_unknown(self):
        for kind in ("volume", "flat", "nan"):
            run, bars, calendar = fixture()
            bar = bars["005930"][date(2026, 10, 5)]
            if kind == "volume":
                bar["volume"] = "0"
            elif kind == "flat":
                bar.update(open="100", high="100", low="100", close="100")
            else:
                bar["open"] = "NaN"
            with self.subTest(kind=kind):
                self.assertEqual(update_shadow(Store(), [run], bars, calendar, "2026-10-13")[0]["shadow_unknown"], 1)

    def test_price_revision_retains_original_and_first_closed_outcome(self):
        run, bars, calendar = fixture()
        store = Store()
        update_shadow(store, [run], bars, calendar, "2026-10-13")
        before = deepcopy(next(iter(store.saved["shadow"]["records"].values())))
        bars["005930"][date(2026, 10, 5)]["open"] = "100.1"
        item = update_shadow(store, [run], bars, calendar, "2026-10-13")[0]
        after = next(iter(store.saved["shadow"]["records"].values()))
        self.assertEqual(item["shadow_unknown"], 1)
        self.assertIsNone(item["shadow_net_pct"])
        self.assertEqual(after["basis"], before["basis"])
        self.assertEqual(after["first_closed"], before["first_closed"])
        self.assertEqual(after["reason"], "price_revision")

    def test_equivalent_numeric_representation_and_rerun_are_idempotent(self):
        run, bars, calendar = fixture()
        store = Store()
        first = update_shadow(store, [run], bars, calendar, "2026-10-13")
        writes = store.writes
        for bar in bars["005930"].values():
            for key in bar:
                bar[key] += ".0"
        self.assertEqual(update_shadow(store, [run], bars, calendar, "2026-10-13"), first)
        self.assertEqual(store.writes, writes)
        other = {**run, "id": "run2", "version_id": "v2", "created_at": "2026-10-05T09:00:00+00:00"}
        self.assertEqual(update_shadow(store, [other, run], bars, calendar, "2026-10-13")[0]["shadow_signals"], 1)

    def test_calendar_gap_and_revisions_do_not_compress_holding_time(self):
        run, bars, calendar = fixture()
        store = Store()
        update_shadow(store, [run], bars, calendar, "2026-10-05")
        calendar["KOSPI"].remove(date(2026, 10, 5))
        self.assertEqual(update_shadow(store, [run], bars, calendar, "2026-10-13")[0]["shadow_unknown"], 1)
        run, bars, calendar = fixture()
        calendar = {"KOSPI": [date(2027, 1, 4)]}
        self.assertEqual(update_shadow(Store(), [run], bars, calendar, "2027-01-04")[0]["shadow_unknown"], 1)

    def test_completed_originals_survive_history_rolloff(self):
        run, bars, calendar = fixture()
        store = Store()
        first = update_shadow(store, [run], bars, calendar, "2026-10-13")
        later = {"005930": {date(2027, 1, 4): {"open": "100", "high": "110", "low": "90", "close": "105", "volume": "100", "turnover": "10000"}}}
        self.assertEqual(update_shadow(store, [], later, {"KOSPI": [date(2027, 1, 4)]}, "2027-01-04"), first)

    def test_independent_strategies_and_only_ready_buy_decisions(self):
        run, bars, calendar = fixture()
        run["signals"] += [{**run["signals"][0], "strategy_id": "relative-strength-v1"},
                           {**run["signals"][0], "strategy_id": "llm-evidence-v1", "status": "error"},
                           {**run["signals"][0], "strategy_id": "pullback-recovery-v1", "action": "hold"}]
        result = {item["strategy_id"]: item for item in update_shadow(Store(), [run], bars, calendar, "2026-10-13")}
        self.assertEqual(result["trend-breakout-v1"]["shadow_closed"], 1)
        self.assertEqual(result["relative-strength-v1"]["shadow_closed"], 1)
        self.assertEqual(result["llm-evidence-v1"]["shadow_signals"], 0)
        self.assertEqual(result["pullback-recovery-v1"]["shadow_signals"], 0)

    def test_invalid_timestamp_cannot_become_trade(self):
        for timestamp in (None, "2026-10-02T08:00:00", "not-time"):
            run, bars, calendar = fixture(timestamp)
            self.assertEqual(update_shadow(Store(), [run], bars, calendar, "2026-10-13")[0]["shadow_unknown"], 1)


if __name__ == "__main__":
    unittest.main()
