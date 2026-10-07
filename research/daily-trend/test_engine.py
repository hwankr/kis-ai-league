"""Independent synthetic boundary tests; never read historical research outcomes."""

import importlib.util
from pathlib import Path
import sys
import unittest

import numpy as np
import pandas as pd


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("daily_trend_engine", HERE / "engine.py")
engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = engine
spec.loader.exec_module(engine)

SIGNAL = 25
ENTRY = SIGNAL + 1
ZERO_COSTS = {"buy_fee": 0.0, "sell_fee": 0.0, "sell_tax": 0.0}


def fixture(days=48):
    """A completed breakout followed by constant, liquid OHLC bars."""
    frame = pd.DataFrame(
        {"open": 90.0, "high": 92.0, "low": 88.0, "close": 90.0,
         "volume": 1_000_000.0, "turnover": 20_000_000_000.0},
        index=pd.bdate_range("2024-01-02", periods=days),
    )
    frame.loc[frame.index[SIGNAL:], ["open", "high", "low", "close"]] = [100, 102, 98, 100]
    return frame


def bar(frame, position, **values):
    for name, value in values.items():
        frame.iloc[position, frame.columns.get_loc(name)] = value


def rule(exit="time", max_hold=5, **values):
    return {"id": f"synthetic_{exit}_{max_hold}", "exit": exit,
            "max_hold": max_hold, **values}


def simulate(frame, exit="time", max_hold=5, slippage=0.0, costs=None, **values):
    return engine.simulate_trade(frame, SIGNAL, rule(exit, max_hold, **values),
                                 slippage, ZERO_COSTS if costs is None else costs)


class DailyTrendEngineTests(unittest.TestCase):
    def assert_date(self, value, expected):
        self.assertEqual(pd.Timestamp(value), pd.Timestamp(expected))

    def assert_unpriced(self, result, status):
        self.assertEqual(result["status"], status)
        self.assertIsNone(result.get("net_return"))
        self.assertIsNone(result.get("gross_return"))

    def test_entry_is_next_open_and_never_signal_close(self):
        frame = fixture()
        bar(frame, ENTRY, open=110, high=112, low=98, close=100)
        result = simulate(frame)
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["entry_pos"], ENTRY)
        self.assert_date(result["entry_date"], frame.index[ENTRY])
        self.assertEqual(result["entry_raw"], 110)
        self.assertAlmostEqual(result["opening_gap"], .1)
        self.assertNotEqual(result["entry_raw"], frame.iloc[SIGNAL]["close"])

    def test_time_exit_counts_entry_day_and_uses_following_open(self):
        for max_hold in (5, 10):
            with self.subTest(max_hold=max_hold):
                frame = fixture()
                signal_pos = ENTRY + max_hold - 1
                exit_pos = ENTRY + max_hold
                bar(frame, signal_pos, close=104, high=105)
                bar(frame, exit_pos, open=107, high=120, low=90, close=115)
                result = simulate(frame, max_hold=max_hold)
                self.assertEqual(result["exit_reason"], "time")
                self.assert_date(result["exit_signal_date"], frame.index[signal_pos])
                self.assert_date(result["exit_date"], frame.index[exit_pos])
                self.assertEqual(result["exit_pos"], exit_pos)
                self.assertEqual(result["exit_raw"], 107)
                self.assertEqual(result["holding_days"], max_hold)

    def test_signal_without_next_session_is_pending_not_zero_return(self):
        frame = fixture().iloc[:SIGNAL + 1].copy()
        self.assert_unpriced(simulate(frame), "pending_entry")

    def test_draft_stop_ignores_intraday_low_touch(self):
        frame = fixture()
        bar(frame, ENTRY, low=90, close=100)
        result = simulate(frame, exit="draft")
        self.assertEqual(result["exit_reason"], "time")
        self.assertEqual(result["exit_pos"], ENTRY + 5)

    def test_draft_stop_at_exact_threshold_sells_next_open(self):
        frame = fixture()
        bar(frame, ENTRY, close=97, low=96)
        bar(frame, ENTRY + 1, open=95, low=94)
        result = simulate(frame, exit="draft")
        self.assertEqual(result["exit_reason"], "draft_stop")
        self.assert_date(result["exit_signal_date"], frame.index[ENTRY])
        self.assertEqual(result["exit_pos"], ENTRY + 1)
        self.assertEqual(result["exit_raw"], 95)

    def test_stop_does_not_cap_gap_loss_at_three_percent(self):
        frame = fixture()
        bar(frame, ENTRY, close=96, low=95)
        bar(frame, ENTRY + 1, open=90, low=89)
        result = simulate(frame, exit="draft")
        self.assertEqual(result["exit_reason"], "draft_stop")
        self.assertAlmostEqual(result["net_return"], -.1)
        self.assertLess(result["net_return"], -.03)

    def test_draft_sma10_uses_current_close_and_next_open(self):
        frame = fixture()
        frame.loc[frame.index[:SIGNAL], ["open", "high", "low", "close"]] = [109, 110, 108, 109]
        bar(frame, SIGNAL, open=110, high=111, low=109, close=110)
        bar(frame, ENTRY, open=110, high=111, low=107, close=108)
        bar(frame, ENTRY + 1, open=107, high=109, low=99, close=100)
        result = simulate(frame, exit="draft")
        self.assertEqual(result["exit_reason"], "draft_sma10")
        self.assertEqual(result["exit_pos"], ENTRY + 1)
        self.assertEqual(result["exit_raw"], 107)

    def test_draft_sma10_equality_triggers_exit(self):
        frame = fixture()
        frame.loc[:, ["open", "high", "low", "close"]] = [100, 102, 98, 100]
        result = simulate(frame, exit="draft")
        self.assertEqual(result["exit_reason"], "draft_sma10")
        self.assertEqual(result["exit_pos"], ENTRY + 1)

    def test_failed_breakout_uses_close_not_intraday_low(self):
        frame = fixture()
        bar(frame, ENTRY, low=85, close=100)
        bar(frame, ENTRY + 1, close=94, low=93)
        bar(frame, ENTRY + 2, open=93, low=92)
        result = simulate(frame, exit="failed_breakout", breakout_level=95)
        self.assertEqual(result["exit_reason"], "failed_breakout")
        self.assert_date(result["exit_signal_date"], frame.index[ENTRY + 1])
        self.assertEqual(result["exit_pos"], ENTRY + 2)

    def test_atr_trail_ignores_high_spikes_and_keeps_signal_atr(self):
        frame = fixture()
        bar(frame, ENTRY, high=150, low=50, close=100)
        bar(frame, ENTRY + 1, close=97, low=96)
        result = simulate(frame, exit="atr_trail", atr=2)
        self.assertEqual(result["exit_reason"], "time")
        self.assertEqual(result["exit_pos"], ENTRY + 5)

    def test_atr_trail_updates_from_prior_highest_close(self):
        frame = fixture()
        bar(frame, ENTRY, close=105, high=106, low=98)
        bar(frame, ENTRY + 1, open=105, high=106, low=99, close=100)
        bar(frame, ENTRY + 2, open=99, low=98)
        result = simulate(frame, exit="atr_trail", atr=2)
        self.assertEqual(result["exit_reason"], "atr_trail")
        self.assert_date(result["exit_signal_date"], frame.index[ENTRY + 1])
        self.assertEqual(result["exit_pos"], ENTRY + 2)

    def test_atr_trail_initial_stop_uses_slipped_entry(self):
        frame = fixture()
        bar(frame, ENTRY, close=96.5, low=95)
        result = simulate(frame, exit="atr_trail", atr=2, slippage=.01)
        self.assertEqual(result["entry_price"], 101)
        self.assertEqual(result["exit_reason"], "atr_trail")
        self.assertEqual(result["exit_pos"], ENTRY + 1)

    def test_zero_volume_entry_is_cancelled_without_retry(self):
        frame = fixture()
        bar(frame, ENTRY, volume=0)
        self.assert_unpriced(simulate(frame), "cancelled")

    def test_flat_entry_is_cancelled_without_retry(self):
        frame = fixture()
        bar(frame, ENTRY, open=100, high=100, low=100, close=100)
        self.assert_unpriced(simulate(frame), "cancelled")

    def test_untradeable_exit_waits_for_first_tradeable_open(self):
        frame = fixture()
        due = ENTRY + 5
        bar(frame, due, volume=0)
        bar(frame, due + 1, open=95, high=95, low=95, close=95)
        bar(frame, due + 2, open=93, low=92)
        result = simulate(frame)
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["exit_reason"], "time")
        self.assert_date(result["exit_signal_date"], frame.index[due - 1])
        self.assertEqual(result["exit_pos"], due + 2)
        self.assertEqual(result["exit_raw"], 93)
        self.assertEqual(result["holding_days"], 7)

    def test_missing_entry_price_is_unknown(self):
        frame = fixture()
        bar(frame, ENTRY, open=np.nan)
        self.assert_unpriced(simulate(frame), "unknown")

    def test_missing_held_bar_is_unknown_even_if_data_returns(self):
        frame = fixture()
        frame.iloc[ENTRY + 2] = np.nan
        self.assert_unpriced(simulate(frame), "unknown")

    def test_missing_exit_bar_is_unknown_not_a_free_deferral(self):
        frame = fixture()
        bar(frame, ENTRY + 5, open=np.nan)
        self.assert_unpriced(simulate(frame), "unknown")

    def test_unclosed_trade_is_open_with_no_fabricated_return(self):
        frame = fixture().iloc[:ENTRY + 5].copy()
        result = simulate(frame)
        self.assert_unpriced(result, "open")
        self.assertIsNone(result.get("exit_date"))

    def test_costs_match_independent_cash_flow_calculation(self):
        frame = fixture()
        bar(frame, ENTRY + 5, open=110, high=112, low=98)
        costs = {"buy_fee": .000140527, "sell_fee": .000140527, "sell_tax": .002}
        result = simulate(frame, slippage=.001, costs=costs)
        cash_paid = (100 * 1.001) * (1 + .000140527)
        cash_received = (110 * .999) * (1 - .000140527 - .002)
        self.assertAlmostEqual(result["entry_price"], 100.1)
        self.assertAlmostEqual(result["exit_price"], 109.89)
        self.assertAlmostEqual(result["net_return"], cash_received / cash_paid - 1, places=13)
        self.assertAlmostEqual(result["gross_return"], 109.89 / 100.1 - 1, places=13)

    def test_excursions_ignore_signal_bar_and_after_exit_open(self):
        frame = fixture()
        bar(frame, SIGNAL, high=999, low=1)
        bar(frame, ENTRY, high=120, low=90)
        bar(frame, ENTRY + 5, open=105, high=999, low=1, close=900)
        result = simulate(frame)
        self.assertAlmostEqual(result["mae"], -.1)
        self.assertAlmostEqual(result["mfe"], .2)
        self.assertAlmostEqual(result["close_mae"], 0)

    def test_closed_result_is_identical_with_or_without_future_rows(self):
        frame = fixture()
        full = simulate(frame)
        prefix = simulate(frame.iloc[:ENTRY + 6].copy())
        self.assertEqual(full, prefix)
        changed = frame.copy(deep=True)
        changed.iloc[ENTRY + 6:] = np.nan
        self.assertEqual(full, simulate(changed))

    def test_simulation_does_not_mutate_inputs(self):
        frame = fixture()
        before = frame.copy(deep=True)
        configuration = rule("atr_trail", atr=2)
        costs = ZERO_COSTS.copy()
        engine.simulate_trade(frame, SIGNAL, configuration, .001, costs)
        pd.testing.assert_frame_equal(frame, before)
        self.assertEqual(configuration, rule("atr_trail", atr=2))
        self.assertEqual(costs, ZERO_COSTS)


if __name__ == "__main__":
    unittest.main()
