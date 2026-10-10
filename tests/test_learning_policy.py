"""Pure policy and portfolio tests; no app, broker, browser, or network starts."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import json
import math
import unittest

from backend.learning_policy import (BASELINE_POLICY, FEATURES, FEE, TAX, build_frame,
                                     policy_id, portfolio_metrics, signals, validate_policy)


def policy(**changes):
    return {"kind": "rules", "name": "시험 정책", "cash_reserve": 0., "max_positions": 20,
            "entry_slippage_bps": 100, "cancel_after_minutes": 10,
            "strategies": [{"id": "momentum", "name": "모멘텀", "weight": 1., "conditions": [],
                            "rank_by": "return_5d_pct", "descending": True, "top_n": 1,
                            "holding_sessions": 5, "stop_loss_pct": 0., "take_profit_pct": 0.}], **changes}


def days(count):
    result, current = [], date(2026, 7, 1)
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def row(symbol="005930", price=100, **changes):
    return {"symbol": symbol, "name": symbol, "board": "KOSPI", "open": price,
            "high": price + 1, "low": price - 1, "close": price, "volume": 1000,
            "features": {key: 1. for key in FEATURES}, "learning_eligible": True, **changes}


def frames(count=7, price=100):
    calendar = days(count)
    return [{"as_of": day, "observed_at": day + "T16:05:00+09:00", "session_days": calendar[:i + 1],
             "rows": [row(price=price, learning_eligible=i == 0)], "baseline_signals": []}
            for i, day in enumerate(calendar)]


LIMITS = {"budget": "10000", "order_cap": "10000", "daily_buy_limit": "10000"}


class PolicyTests(unittest.TestCase):
    def test_validation_is_canonical_and_rejects_unbounded_or_unsafe_fields(self):
        value = policy()
        original = deepcopy(value)
        canonical = validate_policy(value)
        self.assertEqual(canonical, json.loads(json.dumps(canonical)))
        self.assertEqual(policy_id(value), policy_id(canonical))
        self.assertEqual(value, original)
        for field, invalid in (("cash_reserve", .91), ("max_positions", True),
                               ("entry_slippage_bps", float("nan")), ("cancel_after_minutes", 4)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_policy({**value, field: invalid})
        with self.assertRaisesRegex(ValueError, "fields"):
            validate_policy({**value, "code": "print('no')"})
        value["strategies"][0]["conditions"] = [{"feature": "__import__", "op": "gt", "value": 0}]
        with self.assertRaisesRegex(ValueError, "condition"):
            validate_policy(value)

    def test_weight_sum_and_legacy_policy_are_fixed(self):
        value = policy(cash_reserve=.2)
        with self.assertRaisesRegex(ValueError, "weights_exceed"):
            validate_policy(value)
        value["strategies"][0]["weight"] = .8
        self.assertEqual(validate_policy(value)["strategies"][0]["weight"], .8)
        self.assertEqual(validate_policy(BASELINE_POLICY), BASELINE_POLICY)
        with self.assertRaisesRegex(ValueError, "legacy_policy"):
            validate_policy({**BASELINE_POLICY, "max_positions": 2})

    def test_learned_strategy_cannot_reuse_legacy_identity(self):
        for identity in ("trend-breakout-v1", "pullback-recovery-v1", "relative-strength-v1", "llm-evidence-v1"):
            value = policy()
            value["strategies"][0]["id"] = identity
            with self.subTest(identity=identity), self.assertRaisesRegex(ValueError, "reserved_strategy_id"):
                validate_policy(value)

    def test_conditions_ranking_eligibility_and_two_strategy_composition(self):
        value = policy()
        first = value["strategies"][0]
        first.update(weight=.5, conditions=[{"feature": "return_5d_pct", "op": "gt", "value": 0}])
        second = {**deepcopy(first), "id": "reversal", "name": "역추세", "descending": False,
                  "conditions": [{"feature": "return_5d_pct", "op": "lt", "value": 0}]}
        value["strategies"].append(second)
        frame = frames(1)[0]
        frame["rows"] = [row("005930"), row("000660"), row("000020"), row("000030", learning_eligible=False)]
        for item, score in zip(frame["rows"], (3, -2, 0, 100)):
            item["features"]["return_5d_pct"] = score
        result = signals(value, frame)
        self.assertEqual([(item["strategy_id"], item["symbol"]) for item in result],
                         [("momentum", "005930"), ("reversal", "000660")])
        self.assertTrue(all(item["policy_id"] == policy_id(value) for item in result))
        self.assertEqual(result[0]["reference_close"], 100)

    def test_unknown_features_do_not_turn_into_zero_or_a_signal(self):
        frame = frames(1)[0]
        frame["rows"][0]["features"]["return_5d_pct"] = None
        self.assertEqual(signals(policy(), frame), [])


class FrameTests(unittest.TestCase):
    def data(self):
        calendar = days(60)
        bars = {day: {**row(), "turnover": 100000} for day in calendar}
        bars[calendar[-1]].update(close=120, high=150, open=100, low=99, volume=2000)
        return {"as_of": calendar[-1], "observed_at": calendar[-1] + "T16:05:00+09:00",
                "rows": [{"symbol": "005930", "name": "종목", "board": "KOSPI"}],
                "histories": {"005930": bars}, "calendars": {"KOSPI": calendar},
                "benchmarks": {"KOSPI": {day: 1000 for day in calendar}}}

    def test_features_use_completed_history_and_exclude_current_high_from_breakout(self):
        data = self.data()
        result = build_frame(data)
        feature = result["rows"][0]["features"]
        self.assertEqual(set(feature), set(FEATURES))
        self.assertAlmostEqual(feature["return_5d_pct"], 20)
        self.assertAlmostEqual(feature["return_20d_pct"], 20)
        self.assertAlmostEqual(feature["excess_20d_pp"], 20)
        self.assertAlmostEqual(feature["breakout_20d_pct"], (120 / 101 - 1) * 100)
        self.assertEqual(feature["volume_ratio"], 2)
        self.assertEqual(feature["turnover"], 100000)
        self.assertEqual(result["benchmark_prices"], {"KOSPI": 1000})
        self.assertAlmostEqual(feature["close_sma20_pct"], (120 / 101 - 1) * 100)
        self.assertAlmostEqual(feature["close_sma60_pct"], (120 / (6020 / 60) - 1) * 100)

    def test_future_bars_do_not_change_frame_and_before_close_is_rejected(self):
        data = self.data()
        before = build_frame(data)
        data["histories"]["005930"]["2027-01-04"] = {**row(price=10000), "turnover": 10**12}
        data["calendars"]["KOSPI"].append("2027-01-04")
        self.assertEqual(build_frame(data), before)
        data["observed_at"] = data["as_of"] + "T15:59:00+09:00"
        with self.assertRaisesRegex(ValueError, "incomplete_session"):
            build_frame(data)

    def test_missing_universe_prices_are_explicit_and_ineligible_prices_are_kept(self):
        data = self.data()
        data["rows"][0]["learning_eligible"] = False
        data["rows"].append({"symbol": "000660", "name": "없는 종목", "board": "KOSPI"})
        result = build_frame(data)
        self.assertEqual(result["missing_symbols"], ["000660"])
        self.assertEqual(result["rows"][0]["close"], 120)
        self.assertEqual(signals(policy(), result), [])


class PortfolioTests(unittest.TestCase):
    def evaluate(self, value=None, observations=None, limits=None):
        return portfolio_metrics(value or policy(), observations or frames(), limits or LIMITS)

    def test_flat_market_loses_only_costs_and_stress_is_more_expensive(self):
        result = self.evaluate()
        self.assertTrue(result["valid"], result)
        unit = Decimal(100) * Decimal("1.001") * (1 + FEE)
        quantity = int(Decimal(10000) / unit)
        cash = Decimal(10000) - quantity * unit
        cash += quantity * Decimal(100) * Decimal(".999") * (1 - FEE - TAX)
        self.assertAlmostEqual(result["net_return_pct"], float((cash / 10000 - 1) * 100), places=10)
        self.assertLess(result["stress_return_pct"], result["net_return_pct"])
        self.assertEqual(result["closed_trades"], 1)
        self.assertGreater(result["max_drawdown_pct"], 0)
        self.assertGreater(result["stress_drawdown_pct"], result["max_drawdown_pct"])
        self.assertEqual(result["sessions"], 7)

    def test_signal_enters_next_open_never_its_own_bar(self):
        observations = frames(2)
        observations[0]["rows"][0].update(open=1, low=1, high=101, close=100)
        result = self.evaluate(observations=observations)
        self.assertTrue(result["valid"])
        self.assertLess(result["net_return_pct"], 0)
        self.assertGreater(result["net_return_pct"], -1)
        self.assertEqual(result["closed_trades"], 0)  # Final mark is not a fake sale.

    def test_entry_gap_limit_skips_without_inventing_a_limit_fill(self):
        observations = frames(2)
        observations[1]["rows"][0].update(open=103, low=102, high=104, close=103)
        result = self.evaluate(observations=observations)
        self.assertEqual((result["valid"], result["net_return_pct"], result["closed_trades"]), (True, 0., 0))

    def test_cash_reserve_and_strategy_budget_limit_whole_share_exposure(self):
        value = policy(cash_reserve=.6)
        value["strategies"][0]["weight"] = .4
        observations = frames(3)
        observations[2]["rows"][0].update(open=120, close=120, high=121, low=119)
        result = self.evaluate(value, observations)
        unit = Decimal(100) * Decimal("1.001") * (1 + FEE)
        quantity = int(Decimal(4000) / unit)
        final = Decimal(10000) - quantity * unit + quantity * Decimal(120) * Decimal(".999") * (1 - FEE - TAX)
        self.assertAlmostEqual(result["net_return_pct"], float((final / 10000 - 1) * 100), places=10)
        self.assertEqual(quantity, 39)
        limits = {"budget": 10000, "daily_buy_limit": 2000, "order_cap": 1000}
        smaller = self.evaluate(value, observations, limits)
        self.assertLess(smaller["net_return_pct"], result["net_return_pct"])

    def test_overlapping_strategies_cannot_buy_same_symbol_twice(self):
        value = policy()
        value["strategies"][0]["weight"] = .5
        value["strategies"].append({**deepcopy(value["strategies"][0]), "id": "second", "name": "두 번째"})
        single = deepcopy(value)
        single["strategies"].pop()
        self.assertEqual(self.evaluate(value)["net_return_pct"], self.evaluate(single)["net_return_pct"])
        self.assertEqual(self.evaluate(value)["closed_trades"], 1)

    def test_stop_uses_prior_close_and_next_open_not_today_low(self):
        value = policy()
        value["strategies"][0]["stop_loss_pct"] = 5
        observations = frames(4)
        observations[1]["rows"][0]["low"] = 50  # Intraday low is not an exit signal.
        self.assertEqual(self.evaluate(value, observations)["closed_trades"], 0)
        observations[2]["rows"][0].update(close=90, low=89)
        observations[3]["rows"][0].update(open=95, close=95, low=94, high=96)
        result = self.evaluate(value, observations)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["closed_trades"], 1)
        self.assertGreater(result["net_return_pct"], -6)
        self.assertGreater(result["max_drawdown_pct"], 10)

    def test_take_profit_and_one_session_holding_exit_at_next_open(self):
        value = policy()
        value["strategies"][0]["take_profit_pct"] = 5
        observations = frames(4)
        observations[2]["rows"][0].update(close=110, high=111)
        self.assertEqual(self.evaluate(value, observations)["closed_trades"], 1)
        value["strategies"][0].update(take_profit_pct=0, holding_sessions=1)
        self.assertEqual(self.evaluate(value, frames(3))["closed_trades"], 1)

    def test_required_missing_prices_invalidate_but_known_halts_do_not_fill(self):
        for index in (1, 2):
            observations = frames(3)
            observations[index]["rows"] = []
            result = self.evaluate(observations=observations)
            self.assertEqual((result["valid"], result["error"]), (False, "required_price_missing"))
        for changes in ({"volume": 0}, {"open": 100, "high": 100, "low": 100, "close": 100}):
            observations = frames(3)
            observations[1]["rows"][0].update(changes)
            result = self.evaluate(observations=observations)
            self.assertTrue(result["valid"], result)
            self.assertEqual(result["net_return_pct"], 0)
            self.assertEqual(result["final_state"]["positions"], [])
            self.assertEqual(result["diagnostics"]["normal"]["untradeable_bars"], 1)

    def test_missing_session_or_duplicate_day_is_invalid(self):
        observations = frames(4)
        observations.pop(1)
        self.assertEqual(self.evaluate(observations=observations)["error"], "missing_session")
        observations = frames(3)
        observations[1]["as_of"] = observations[0]["as_of"]
        self.assertEqual(self.evaluate(observations=observations)["error"], "unordered_frames")

    def test_legacy_uses_only_supplied_ready_buys_and_quarter_budget(self):
        observations = frames()
        observations[0]["baseline_signals"] = [{"strategy_id": "trend-breakout-v1", "symbol": "005930", "action": "buy", "status": "ready"}]
        result = self.evaluate(BASELINE_POLICY, observations)
        comparison = policy()
        comparison["strategies"][0]["weight"] = .25
        self.assertEqual(result["net_return_pct"], self.evaluate(comparison, observations)["net_return_pct"])
        observations[0]["baseline_signals"][0]["status"] = "pending"
        self.assertEqual(self.evaluate(BASELINE_POLICY, observations)["net_return_pct"], 0)

    def test_legacy_does_not_apply_new_policy_gap_filter(self):
        observations = frames(2)
        observations[0]["baseline_signals"] = [{"strategy_id": "trend-breakout-v1", "symbol": "005930", "action": "buy", "status": "ready"}]
        observations[1]["rows"][0].update(open=120, close=120, high=121, low=119)
        result = self.evaluate(BASELINE_POLICY, observations)
        self.assertTrue(result["valid"], result)
        self.assertLess(result["net_return_pct"], 0)  # It bought the gap and paid costs.
        self.assertEqual(self.evaluate(policy(), observations)["net_return_pct"], 0)

    def test_legacy_can_accumulate_more_than_twenty_positions_across_days(self):
        observations = frames(8)
        symbols = [f"{number:06d}" for number in range(1, 22)]
        for frame in observations:
            frame["rows"] = [row(symbol) for symbol in symbols]
        def decision(symbol):
            return {"strategy_id": "trend-breakout-v1", "symbol": symbol, "action": "buy", "status": "ready"}
        observations[0]["baseline_signals"] = [decision(symbol) for symbol in symbols[:20]]
        observations[1]["baseline_signals"] = [decision(symbols[-1])]
        result = self.evaluate(BASELINE_POLICY, observations, {"budget": 1000000, "daily_buy_limit": 100000, "order_cap": 1000})
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["closed_trades"], 21)

    def test_reproducible_pure_results_and_half_returns_compound(self):
        value, observations = policy(), frames()
        original = deepcopy((value, observations, LIMITS))
        result = self.evaluate(value, observations)
        self.assertEqual(result, self.evaluate(json.loads(json.dumps(value)), json.loads(json.dumps(observations))))
        first, second = result["half_returns"]
        self.assertAlmostEqual(((1 + first / 100) * (1 + second / 100) - 1) * 100, result["net_return_pct"])
        self.assertEqual((value, observations, LIMITS), original)

    def test_dated_equity_and_log_returns_have_one_value_per_interval(self):
        observations = frames()
        result = self.evaluate(observations=observations)
        self.assertEqual(result["intervals"], 6)
        self.assertEqual([point["as_of"] for point in result["equity_curve"]], days(7))
        for prefix, return_key in (("", "net_return_pct"), ("stress_", "stress_return_pct")):
            values = result[prefix + "daily_log_returns"]
            self.assertEqual(len(values), 6)
            self.assertAlmostEqual(math.expm1(sum(values)) * 100, result[return_key], places=10)
            for point in result[prefix + "equity_curve"]:
                self.assertAlmostEqual(point["equity"], point["cash"] + point["position_value"])

    def test_json_state_restart_matches_uninterrupted_normal_and_stress(self):
        observations = frames()
        original = portfolio_metrics(policy(), observations, LIMITS)
        first = portfolio_metrics(policy(), observations[:3], LIMITS)
        state = json.loads(json.dumps({"normal": first["final_state"], "stress": first["stress_final_state"]}))
        resumed = portfolio_metrics(policy(), observations[2:], LIMITS, initial_state=state)
        self.assertTrue(resumed["valid"], resumed)
        for prefix in ("", "stress_"):
            self.assertEqual(resumed[prefix + "final_state"], original[prefix + "final_state"])
            self.assertEqual(first[prefix + "daily_log_returns"] + resumed[prefix + "daily_log_returns"],
                             original[prefix + "daily_log_returns"])

    def test_inherited_exit_policy_survives_replacement_and_realizes_loss(self):
        observations = frames(2, price=80)
        for frame in observations:
            frame["rows"][0]["learning_eligible"] = False
        observations[1]["rows"] = [row(price=70, learning_eligible=False)]
        state = {"as_of": observations[0]["as_of"], "cash": "9000", "positions": [
            {"symbol": "005930", "strategy_id": "retired-policy", "quantity": 10, "cost": "1000",
             "entry_price": "100", "last_close": "80", "held_sessions": 1,
             "exit_policy": {"holding_sessions": 20, "stop_loss_pct": 5, "take_profit_pct": 0}}]}
        result = portfolio_metrics(policy(), observations, LIMITS, initial_state=state)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["closed_trades"], 1)
        self.assertEqual(result["final_state"]["positions"], [])
        self.assertLess(Decimal(result["final_state"]["cash"]), Decimal(9700))
        self.assertLess(result["net_return_pct"], -1)
        self.assertEqual(state["positions"][0]["quantity"], 10)

    def test_reserved_cash_is_owned_cash_but_cannot_be_spent(self):
        state = {"cash": "10000", "reserved_cash": "2000", "positions": []}
        result = portfolio_metrics(policy(), frames(2), LIMITS, initial_state=state)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["equity_curve"][0]["equity"], 10000)
        self.assertEqual(result["final_state"]["positions"][0]["quantity"], 79)
        self.assertGreaterEqual(Decimal(result["final_state"]["cash"]), Decimal(2000))
        self.assertEqual(result["final_state"]["reserved_cash"], "2000")

    def test_inherited_old_strategy_and_profit_cash_share_total_investable_cap(self):
        value = policy(cash_reserve=.2)
        value["strategies"][0]["weight"] = .8
        observations = frames(2)
        for frame in observations:
            frame["rows"].append(row("000660", learning_eligible=False))
        # Original budget 10,000: an old strategy still owns cost 4,000, while
        # realized gains have restored cash to 10,000. They do not enlarge caps.
        state = {"cash": "10000", "positions": [{"symbol": "000660", "strategy_id": "retired-policy",
                 "quantity": 40, "cost": "4000", "entry_price": "100", "last_close": "100",
                 "held_sessions": 0, "exit_policy": {"holding_sessions": 20, "stop_loss_pct": 0,
                                                       "take_profit_pct": 0}}]}
        result = portfolio_metrics(value, observations, LIMITS, initial_state=state)
        self.assertTrue(result["valid"], result)
        for key in ("final_state", "stress_final_state"):
            positions = {item["symbol"]: item for item in result[key]["positions"]}
            self.assertEqual(positions["005930"]["quantity"], 39)
            self.assertEqual(positions["000660"]["quantity"], 40)
            self.assertLessEqual(sum(Decimal(item["cost"]) for item in positions.values()), Decimal(8000))
        self.assertEqual(state["positions"][0]["cost"], "4000")

    def test_holding_age_counts_after_entry_close_and_continues_on_restart(self):
        value = policy()
        value["strategies"][0]["holding_sessions"] = 1
        first = portfolio_metrics(value, frames(2), LIMITS)
        self.assertEqual(first["final_state"]["positions"][0]["held_sessions"], 0)
        rest = portfolio_metrics(value, frames(3)[1:], LIMITS,
                                 initial_state={"normal": first["final_state"], "stress": first["stress_final_state"]})
        self.assertTrue(rest["valid"], rest)
        self.assertEqual(rest["closed_trades"], 1)

    def test_halted_loss_exit_stays_in_equity_and_retries_after_rebound(self):
        value = policy()
        value["strategies"][0]["stop_loss_pct"] = 5
        observations = frames(4)
        observations[1]["rows"][0].update(close=90, low=89)
        observations[2]["rows"] = [row(price=70, volume=0, learning_eligible=False)]
        observations[3]["rows"] = [row(price=102, learning_eligible=False)]
        first = portfolio_metrics(value, observations[:3], LIMITS)
        self.assertTrue(first["valid"], first)
        self.assertLess(first["net_return_pct"], -29)
        self.assertTrue(first["final_state"]["positions"][0]["exit_pending"])
        result = portfolio_metrics(value, observations, LIMITS)
        self.assertEqual(result["closed_trades"], 1)
        self.assertGreater(result["max_drawdown_pct"], 29)
        self.assertEqual(result["diagnostics"]["normal"]["delayed_exit_sessions"], 1)

    def test_partial_buys_and_sells_keep_remaining_cost_quantity_and_fees(self):
        value = policy()
        value["strategies"][0]["holding_sessions"] = 1
        result = portfolio_metrics(value, frames(4), LIMITS,
                                   execution_model={"buy_fill_ratio": .5, "sell_fill_ratio": .5})
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["diagnostics"]["normal"]["buy_filled_quantity"], 49)
        self.assertEqual(result["diagnostics"]["normal"]["sell_filled_quantity"], 37)
        self.assertEqual(result["final_state"]["positions"][0]["quantity"], 12)
        self.assertEqual(result["closed_trades"], 0)
        self.assertEqual(result["diagnostics"]["normal"]["partial_orders"], 3)
        unit = Decimal(100) * Decimal("1.001") * (1 + FEE)
        self.assertAlmostEqual(float(result["final_state"]["positions"][0]["cost"]), float(12 * unit))
        self.assertGreater(result["diagnostics"]["normal"]["fees_paid"], 0)
        self.assertGreater(result["diagnostics"]["normal"]["taxes_paid"], 0)

    def test_zero_sell_ratio_retains_loss_instead_of_removing_failed_trade(self):
        value = policy()
        value["strategies"][0]["holding_sessions"] = 1
        observations = frames(4)
        observations[2]["rows"] = [row(price=80, learning_eligible=False)]
        observations[3]["rows"] = [row(price=60, learning_eligible=False)]
        result = portfolio_metrics(value, observations, LIMITS, execution_model={"sell_fill_ratio": 0})
        self.assertTrue(result["valid"], result)
        self.assertLess(result["net_return_pct"], -39)
        self.assertEqual(result["final_state"]["positions"][0]["quantity"], 99)
        self.assertEqual(result["closed_trades"], 0)
        self.assertEqual(result["diagnostics"]["normal"]["delayed_exit_sessions"], 2)

    def test_fractional_sell_model_can_close_last_share_after_restart(self):
        observations = frames(3)
        for frame in observations:
            frame["rows"][0]["learning_eligible"] = False
        state = {"cash": 9000, "positions": [{"symbol": "005930", "strategy_id": "old", "quantity": 1,
                 "cost": "100.0140527", "entry_price": 100, "last_close": 100, "held_sessions": 1,
                 "exit_policy": {"holding_sessions": 1, "stop_loss_pct": 0, "take_profit_pct": 0}}]}
        model = {"sell_fill_ratio": .5}
        first = portfolio_metrics(policy(), observations[:2], LIMITS, initial_state=state, execution_model=model)
        self.assertTrue(first["valid"], first)
        self.assertEqual(first["final_state"]["positions"][0]["quantity"], 1)
        self.assertEqual(first["final_state"]["positions"][0]["exit_fill_remainder"], "0.5")
        resumed = portfolio_metrics(policy(), observations[1:], LIMITS,
                                    initial_state=json.loads(json.dumps({"normal": first["final_state"], "stress": first["stress_final_state"]})),
                                    execution_model=model)
        full = portfolio_metrics(policy(), observations, LIMITS, initial_state=state, execution_model=model)
        self.assertTrue(resumed["valid"], resumed)
        self.assertEqual(resumed["closed_trades"], 1)
        self.assertEqual(resumed["final_state"], full["final_state"])

    def observed(self, count=3):
        observations = frames(count)
        for frame in observations:
            frame["execution_quotes"] = {"005930": {"price": 100, "eligible": True,
                                           "observed_at": frame["as_of"] + "T09:10:00+09:00"}}
        return observations

    def test_observed_execution_uses_decision_price_not_daily_open(self):
        observations = self.observed(2)
        observations[1]["rows"][0].update(open=50, low=49)
        result = portfolio_metrics(policy(), observations, LIMITS, execution_model={"mode": "observed_quotes"})
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["final_state"]["positions"][0]["quantity"], 99)
        self.assertEqual(Decimal(result["final_state"]["positions"][0]["entry_price"]), 100)
        self.assertEqual(result["diagnostics"]["cost_stress"]["slippage_bps"], 0)
        self.assertAlmostEqual(result["diagnostics"]["cost_stress"]["fee_rate"], float(FEE) + .001)
        self.assertLess(result["stress_return_pct"], result["net_return_pct"])

    def test_missing_quote_invalid_but_known_ineligible_quote_does_not_fill(self):
        result = portfolio_metrics(policy(), frames(2), LIMITS, execution_model={"mode": "observed_quotes"})
        self.assertEqual(result["error"], "execution_quote_missing")
        observations = self.observed(2)
        observations[1]["execution_quotes"]["005930"].update(eligible=False)
        observations[1]["execution_quotes"]["005930"].pop("price")
        result = portfolio_metrics(policy(), observations, LIMITS, execution_model={"mode": "observed_quotes"})
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["net_return_pct"], 0)
        self.assertEqual(result["diagnostics"]["normal"]["ineligible_quotes"], 1)

    def test_manual_holding_blocks_new_buys_but_keeps_owned_exits_available(self):
        observations = self.observed(2)
        observations[1]['execution_quotes']['005930']['buy_eligible'] = False
        model = {'mode': 'observed_quotes'}
        result = portfolio_metrics(policy(), observations, LIMITS, execution_model=model)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['final_state']['positions'], [])
        state = {'cash': 9000, 'positions': [{'symbol': '005930', 'strategy_id': 'old', 'quantity': 10,
                 'cost': 1000, 'entry_price': 100, 'last_close': 100, 'held_sessions': 1,
                 'exit_policy': {'holding_sessions': 1, 'stop_loss_pct': 0, 'take_profit_pct': 0}}]}
        result = portfolio_metrics(policy(), observations, LIMITS, initial_state=state, execution_model=model)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['closed_trades'], 1)
        self.assertEqual(result['final_state']['positions'], [])

    def test_quote_time_outside_its_session_is_invalid(self):
        observations = self.observed(2)
        observations[1]["execution_quotes"]["005930"]["observed_at"] = "2027-01-04T09:10:00+09:00"
        result = portfolio_metrics(policy(), observations, LIMITS, execution_model={"mode": "observed_quotes"})
        self.assertEqual(result["error"], "execution_quote_outside_session")

    def test_slippage_cannot_fill_beyond_buy_limit_or_sell_limit(self):
        observations = frames(2)
        observations[1]["rows"] = [row(price=101, learning_eligible=False)]
        result = self.evaluate(observations=observations)
        self.assertEqual(result["net_return_pct"], 0)  # 101.101 is worse than the 101 limit.
        self.assertEqual(result["diagnostics"]["normal"]["limit_blocked"], 1)
        value = policy()
        value["strategies"][0]["holding_sessions"] = 1
        observations = self.observed(3)
        observations[2]["execution_quotes"]["005930"]["sell_limit_price"] = 101
        result = portfolio_metrics(value, observations, LIMITS, execution_model={"mode": "observed_quotes"})
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["closed_trades"], 0)
        self.assertEqual(result["diagnostics"]["normal"]["limit_blocked"], 1)
        self.assertTrue(result["final_state"]["positions"][0]["exit_pending"])
        result = portfolio_metrics(policy(), self.observed(2), LIMITS,
                                   execution_model={"mode": "observed_quotes", "slippage_bps": 10, "stress_slippage_bps": 20})
        self.assertEqual(result["diagnostics"]["normal"]["limit_blocked"], 1)
        self.assertEqual(result["final_state"]["positions"], [])

    def test_cost_and_fill_stress_are_separate_and_do_not_mutate_frozen_model(self):
        model = {"buy_fill_ratio": 1, "sell_fill_ratio": .8, "stress_fill_ratio": .25}
        original = deepcopy(model)
        result = portfolio_metrics(policy(), frames(), LIMITS, execution_model=model)
        self.assertTrue(result["valid"], result)
        normal, cost, fills = (result["diagnostics"][key] for key in ("normal", "cost_stress", "execution_stress"))
        self.assertEqual(cost["buy_fill_ratio"], normal["buy_fill_ratio"])
        self.assertEqual(cost["sell_fill_ratio"], normal["sell_fill_ratio"])
        self.assertGreater(cost["slippage_bps"], normal["slippage_bps"])
        self.assertEqual(fills["slippage_bps"], normal["slippage_bps"])
        self.assertEqual(fills["buy_fill_ratio"], .25)
        self.assertIsNotNone(result["execution_stress_return_pct"])
        self.assertEqual(model, original)

    def test_invalid_initial_state_and_execution_model_fail_without_side_effects(self):
        for state in ({"cash": 100, "reserved_cash": 101}, {"cash": -1}, {"cash": 100, "as_of": "2000-01-01"}):
            result = portfolio_metrics(policy(), frames(2), LIMITS, initial_state=state)
            self.assertFalse(result["valid"])
        for model in ({"fill_ratio": float("nan")}, {"sell_fill_ratio": -1}, {"mode": "market"},
                      {"slippage_bps": 30, "stress_slippage_bps": 20}):
            result = portfolio_metrics(policy(), frames(2), LIMITS, execution_model=model)
            self.assertFalse(result["valid"])


if __name__ == "__main__":
    unittest.main()
