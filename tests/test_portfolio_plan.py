"""Allocation conservation and offline live/replay boundary checks."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import unittest
from unittest.mock import patch

from backend.portfolio_plan import FEE, plan_entries
from backend.experiments import ALL_STRATEGIES
from backend.learning_account import owned_book
from backend.learning_policy import portfolio_metrics
from tests import test_experiments as fixtures
from tests.test_learning_integration import rules_policy
from tests import test_learning_integration as learning_fixtures
from tests.test_learning_policy import row


def candidate(symbol, strategy="a", price="100", quantity_cap=100, **values):
    return {"symbol": symbol, "strategy_id": strategy, "price": price,
            "quantity_cap": quantity_cap, **values}


class PortfolioPlanTests(unittest.TestCase):
    def plan(self, candidates, **values):
        args = {"mode": "equal_eligible", "available_cash": "1000", "daily_remaining": "1000",
                "portfolio_remaining": "1000", "order_cap": "1000", "strategy_remaining": {"a": "1000"},
                "strategy_weights": {"a": "1"}, "fee_rate": 0}
        args.update(values)
        return plan_entries(candidates, **args)

    def test_duplicates_do_not_duplicate_orders_or_discard_investable_cash(self):
        candidates = [candidate(symbol, strategy) for strategy in ("a", "b", "c", "d")
                      for symbol in ("000001", "000002")]
        arguments = {"strategy_remaining": dict.fromkeys("abcd", "1000"),
                     "strategy_weights": dict.fromkeys("abcd", ".25")}
        fresh = self.plan(candidates, **arguments)
        old = self.plan(candidates, mode="legacy", **arguments)
        self.assertEqual([(row["symbol"], row["quantity"]) for row in fresh], [("000001", 5), ("000002", 5)])
        self.assertEqual(sum(row["quantity"] for row in old), 2)
        self.assertEqual(len({row["symbol"] for row in fresh}), len(fresh))

    def test_ineligible_and_zero_weight_rows_cannot_dilute_valid_candidates(self):
        result = self.plan([candidate("bad", eligible=False), candidate("good"), candidate("zero", "b")],
                           strategy_weights={"a": 1, "b": 0}, strategy_remaining={"a": 1000, "b": 1000})
        self.assertEqual([(row["symbol"], row["quantity"]) for row in result], [("good", 10)])

    def test_rounding_and_quantity_cap_residual_go_to_other_symbol(self):
        result = self.plan([candidate("first", quantity_cap=1), candidate("second")])
        self.assertEqual([row["quantity"] for row in result], [1, 9])
        result = self.plan([candidate("first"), candidate("second")], available_cash="700")
        self.assertEqual([row["quantity"] for row in result], [4, 3])

    def test_shared_strategy_caps_and_empty_strategy_weights_are_hard_and_soft_respectively(self):
        result = self.plan([candidate("first", "a"), candidate("second", "b")],
                           strategy_weights={"a": ".8", "b": ".1", "empty": ".1"},
                           strategy_remaining={"a": "100", "b": "1000", "empty": "1000"})
        self.assertEqual([row["quantity"] for row in result], [1, 9])
        result = self.plan([candidate("first"), candidate("second")], strategy_remaining={"a": "350"})
        self.assertEqual(sum(row["quantity"] for row in result), 3)

    def test_rank_allocation_ignores_raw_score_scale_and_respects_position_limit(self):
        source = [candidate("first", rank=1, score=-100), candidate("second", rank=2, score=999999)]
        expected = self.plan(source, mode="score_rank", available_cash="900")
        source[0]["score"], source[1]["score"] = 1e20, -1e20
        self.assertEqual(expected, self.plan(source, mode="score_rank", available_cash="900"))
        self.assertEqual([row["quantity"] for row in expected], [6, 3])
        result = self.plan(list(reversed(source)), mode="score_rank", max_new_positions=1)
        self.assertEqual([(row["symbol"], row["quantity"]) for row in result], [("first", 10)])

    def test_commission_cash_order_daily_and_position_caps_all_bind(self):
        for field in ("available_cash", "daily_remaining", "portfolio_remaining", "order_cap"):
            with self.subTest(field=field):
                result = self.plan([candidate("only")], **{field: "700", "fee_rate": FEE})
                self.assertEqual(result[0]["quantity"], 6)
                self.assertEqual(Decimal(result[0]["estimated_cost"]), Decimal(600) * (1 + FEE))
        result = self.plan([candidate("first", cash_cap="150"), candidate("second")])
        self.assertEqual([row["quantity"] for row in result], [1, 9])

    def test_unaffordable_candidate_cannot_consume_position_slot(self):
        result = self.plan([candidate("expensive", price="1001"), candidate("affordable")], max_new_positions=1)
        self.assertEqual(result[0]["symbol"], "affordable")
        self.assertEqual(self.plan([candidate("none")], max_new_positions=0), [])

    def test_no_inputs_are_mutated_and_invalid_quotes_fail(self):
        source = [candidate("first"), candidate("second")]
        old = deepcopy(source)
        self.plan(source)
        self.assertEqual(source, old)
        for values in ({"price": 0}, {"rank": 0}, {"quantity_cap": -1}, {"price": "NaN"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.plan([candidate("bad", **values)])
        with self.assertRaisesRegex(ValueError, "inconsistent_allocation_quote"):
            self.plan([candidate("one"), candidate("one", price="101")])


class AllocationExecutionTests(unittest.TestCase):
    make_service = fixtures.ExperimentsTests.make_service
    enable = fixtures.ExperimentsTests.enable

    def setUp(self):
        fixtures.ExperimentsTests.setUp(self)
        self.policy.update(budget="1000000", order_cap="300000", daily_buy_limit="500000",
                           execution_strategy=ALL_STRATEGIES)
        self.service._configure(self.policy)
        self.execution = rules_policy()
        self.execution.update(allocation_mode="equal_eligible", cash_reserve=0, max_positions=3)
        self.execution["strategies"][0].update(weight=1, top_n=3)
        self.signals = [{**fixtures.decision(symbol), "strategy_id": "learned-volume", "reference_close": "10000",
                         "policy_id": "frozen-policy", "rank": rank,
                         "exit_policy": {"holding_sessions": 5, "stop_loss_pct": 0, "take_profit_pct": 0}}
                        for rank, symbol in enumerate(fixtures.SYMBOLS, 1)]
        self.enable()
        self.network = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def allocate(self):
        self.service._allocated_entries(self.service._policy(), self.execution, {"id": "allocation-run"},
            self.signals, self.broker, self.current.date(),
            {symbol: {"board": "KOSPI"} for symbol in fixtures.SYMBOLS}, self.master)

    def test_live_plan_matches_pure_quantities_and_reserves_prevent_duplicate_spending(self):
        self.allocate()
        expected = plan_entries([candidate(symbol, "learned-volume", "10000", rank=rank)
                                for rank, symbol in enumerate(fixtures.SYMBOLS, 1)],
            mode="equal_eligible", available_cash="1000000", daily_remaining="500000",
            portfolio_remaining="1000000", order_cap="300000",
            strategy_remaining={"learned-volume": "1000000"}, strategy_weights={"learned-volume": 1})
        self.assertEqual([row[2] for row in self.broker.submissions], [row["quantity"] for row in expected])
        self.assertEqual(sum(row[2] for row in self.broker.submissions), 49)
        self.allocate()
        self.assertEqual(len(self.broker.submissions), 3)
        limits = self.service._allocation_limits(self.service._policy(), self.execution,
            self.service._owned_orders(), self.broker.snapshot(), self.current.date())
        reserved = sum(Decimal(row["estimated_cost"]) for row in expected)
        self.assertEqual(limits["daily_remaining"], Decimal(500000) - reserved)
        self.assertEqual(limits["available_cash"], Decimal(1000000) - reserved)

    def test_external_holdings_and_restricted_names_are_removed_before_division(self):
        self.broker.account["holdings"][fixtures.SYMBOLS[0]] = {"quantity": 1}
        self.allocate()
        self.assertEqual([row[0] for row in self.broker.submissions], list(fixtures.SYMBOLS[1:]))
        self.assertEqual(sum(row[2] for row in self.broker.submissions), 49)

    def test_broker_quantity_and_buying_power_caps_redistribute_to_other_names(self):
        original = self.broker.buyability
        self.broker.buyability = lambda symbol, price: ({"quantity": 1, "cash": "10002"}
            if symbol == fixtures.SYMBOLS[0] else original(symbol, price))
        self.allocate()
        self.assertEqual(self.broker.submissions[0][2], 1)
        self.assertEqual(sum(row[2] for row in self.broker.submissions), 49)

    def test_refresh_price_or_cash_cannot_enlarge_or_overspend_the_plan(self):
        original = self.broker.quote
        calls = {}
        def quote(symbol):
            calls[symbol] = calls.get(symbol, 0) + 1
            result = original(symbol)
            if calls[symbol] > 1:
                result["price"] = "10001"
                self.broker.account["cash"] = "150000"
            return result
        self.broker.quote = quote
        self.allocate()
        spent = sum(Decimal(price) * quantity * (1 + FEE)
                    for _, _, quantity, price in self.broker.submissions)
        self.assertLessEqual(spent, Decimal(150000))

    def test_refreshed_restriction_reallocates_only_unattempted_symbols(self):
        original, calls = self.broker.quote, {}
        def quote(symbol):
            calls[symbol] = calls.get(symbol, 0) + 1
            result = original(symbol)
            if symbol == fixtures.SYMBOLS[0] and calls[symbol] > 1:
                result["eligible"] = False
            return result
        self.broker.quote = quote
        self.allocate()
        self.assertEqual([item[0] for item in self.broker.submissions], list(fixtures.SYMBOLS[1:]))
        self.assertEqual(sum(item[2] for item in self.broker.submissions), 49)

    def test_refreshed_buying_quantity_cap_redistributes_residual(self):
        original, calls = self.broker.buyability, {}
        def buying(symbol, price):
            calls[symbol] = calls.get(symbol, 0) + 1
            if symbol == fixtures.SYMBOLS[0] and calls[symbol] > 1:
                return {"quantity": 1, "cash": "10002"}
            return original(symbol, price)
        self.broker.buyability = buying
        self.allocate()
        self.assertEqual(self.broker.submissions[0][2], 1)
        self.assertEqual(sum(item[2] for item in self.broker.submissions), 49)

    def replay_frames(self):
        result = []
        for day in ("2026-10-02", "2026-10-06"):
            rows = []
            for rank, symbol in enumerate(fixtures.SYMBOLS, 1):
                item = row(symbol=symbol, price=10000)
                item["features"]["turnover"] = 4 - rank
                rows.append(item)
            result.append({"as_of": day, "observed_at": day + "T16:05:00+09:00", "rows": rows,
                           "execution_quotes": {symbol: {"price": 10000, "eligible": True,
                               "observed_at": day + "T09:05:00+09:00"} for symbol in fixtures.SYMBOLS}})
        return result

    def current_book(self, day="2026-10-06"):
        orders = self.service._owned_orders()
        positions, metrics = self.service._portfolio(orders)
        return owned_book(self.service._policy(), positions, metrics, orders,
                          dict.fromkeys(fixtures.SYMBOLS, 10000), as_of=day, session_days=self.broker.days)

    def compare_replay_book(self, ratio):
        initial = self.current_book("2026-10-02")
        self.allocate()
        planned_cost = sum(Decimal(price) * quantity * (1 + FEE)
                           for _, _, quantity, price in self.broker.submissions)
        self.assertEqual(Decimal(self.current_book()["reserved_cash"]), planned_cost)
        for remote in self.broker.remote:
            filled = int(Decimal(remote["quantity"]) * Decimal(str(ratio)))
            remote.update(filled_quantity=filled, remaining_quantity=remote["quantity"] - filled,
                          filled_amount=str(filled * 10000), average_price="10000", status="partial")
        self.service._reconcile(self.service._policy(), self.broker)
        partial = self.current_book()
        filled_cost = sum(Decimal(item["cost"]) for item in partial["positions"])
        self.assertEqual(Decimal(partial["reserved_cash"]), planned_cost - filled_cost)
        self.assertEqual(sum(Decimal(item["cost"]) for item in partial["reserved_entries"]), planned_cost - filled_cost)
        for remote in self.broker.remote:
            remote["status"] = "filled" if remote["filled_quantity"] == remote["quantity"] else "cancelled"
            remote["remaining_quantity"] = 0
        self.service._reconcile(self.service._policy(), self.broker)
        actual = self.current_book()
        simulated = portfolio_metrics(self.execution, self.replay_frames(), self.policy, initial_state=initial,
            execution_model={"mode": "observed_quotes", "buy_fill_ratio": ratio, "stress_fee_rate": str(FEE)})
        self.assertTrue(simulated["valid"], simulated["error"])
        replayed = simulated["final_state"]
        self.assertEqual({item["symbol"]: item["quantity"] for item in actual["positions"]},
                         {item["symbol"]: item["quantity"] for item in replayed["positions"]})
        self.assertEqual(Decimal(actual["cash"]), Decimal(replayed["cash"]))
        self.assertEqual(Decimal(actual["reserved_cash"]), Decimal(replayed["reserved_cash"]))
        # Cancellation does not authorize a second order in the same symbol/day.
        submitted = len(self.broker.submissions)
        self.allocate()
        self.assertEqual(len(self.broker.submissions), submitted)

    def test_live_full_fills_match_replay_from_identical_initial_book_signals_and_quotes(self):
        self.compare_replay_book(1)

    def test_partial_fills_reserve_then_cancel_without_redistribution_and_match_replay(self):
        self.compare_replay_book(.5)

    def test_unchanged_overlapping_strategy_quotes_keep_original_batch_proportions(self):
        first = deepcopy(self.execution["strategies"][0])
        first.update(id="first", weight=.5, conditions=[{"feature": "return_5d_pct", "op": "gt", "value": 0}])
        second = deepcopy(first)
        second.update(id="second", conditions=[{"feature": "close_sma20_pct", "op": "gt", "value": 0}])
        self.execution["strategies"] = [first, second]
        original = deepcopy(self.signals)
        self.signals = [{**original[0], "strategy_id": "first", "rank": 1},
                        {**original[1], "strategy_id": "second", "rank": 1},
                        {**original[1], "strategy_id": "first", "rank": 2},
                        {**original[2], "strategy_id": "second", "rank": 2}]
        initial = self.current_book("2026-10-02")
        self.allocate()
        self.assertEqual([item[2] for item in self.broker.submissions], [12, 25, 12])
        sample = self.replay_frames()
        for frame in sample:
            frame["rows"][0]["features"]["close_sma20_pct"] = -1
            frame["rows"][2]["features"]["return_5d_pct"] = -1
        simulated = portfolio_metrics(self.execution, sample, self.policy, initial_state=initial,
            execution_model={"mode": "observed_quotes", "stress_fee_rate": str(FEE)})
        self.assertTrue(simulated["valid"], simulated["error"])
        self.assertEqual({item[0]: item[2] for item in self.broker.submissions},
                         {item["symbol"]: item["quantity"] for item in simulated["final_state"]["positions"]})

    def test_initial_partial_order_counts_one_slot_and_same_strategy_budget_in_both_paths(self):
        self.execution["max_positions"] = 2
        current = self.current
        self.current = self.current.replace(day=2)
        self.service._submit(self.service._policy(), self.broker, self.signals[0], "buy", 20,
                             Decimal(10000), "initial-pending", run_id="prior-run")
        remote = self.broker.remote[0]
        remote.update(filled_quantity=5, remaining_quantity=15, filled_amount="50000",
                      average_price="10000", status="partial")
        self.service._reconcile(self.service._policy(), self.broker)
        self.broker.account["holdings"][fixtures.SYMBOLS[0]] = {"quantity": 5}
        initial = self.current_book("2026-10-02")
        self.current = current
        self.allocate()
        self.assertEqual(len(self.broker.submissions), 2)  # Held + reserved same name uses one slot.
        self.assertNotEqual(self.broker.submissions[1][0], fixtures.SYMBOLS[0])
        for remote in self.broker.remote[1:]:
            remote.update(filled_quantity=remote["quantity"], remaining_quantity=0,
                          filled_amount=str(remote["quantity"] * 10000), average_price="10000", status="filled")
        self.service._reconcile(self.service._policy(), self.broker)
        actual = self.current_book()
        simulated = portfolio_metrics(self.execution, self.replay_frames(), self.policy, initial_state=initial,
            execution_model={"mode": "observed_quotes", "stress_fee_rate": str(FEE)})
        self.assertTrue(simulated["valid"], simulated["error"])
        replayed = simulated["final_state"]
        self.assertEqual({item["symbol"]: item["quantity"] for item in actual["positions"]},
                         {item["symbol"]: item["quantity"] for item in replayed["positions"]})
        self.assertEqual(Decimal(actual["cash"]), Decimal(replayed["cash"]))
        self.assertEqual(actual["reserved_entries"], replayed["reserved_entries"])
        self.assertEqual(Decimal(actual["reserved_cash"]), Decimal(replayed["reserved_cash"]))

    def test_retired_position_and_negative_current_strategy_capacity_do_not_overallocate(self):
        sample = self.replay_frames()
        self.execution["cash_reserve"] = .8
        self.execution["strategies"][0]["weight"] = .2
        state = {"cash": "300000", "reserved_cash": "0", "reserved_entries": [], "positions": [
            {"symbol": fixtures.SYMBOLS[0], "strategy_id": "learned-volume", "quantity": 70,
             "cost": "700000", "entry_price": "10000", "last_close": "10000", "held_sessions": 0,
             "exit_policy": {"holding_sessions": 20, "stop_loss_pct": 0, "take_profit_pct": 0}}]}
        for owner in ("learned-volume", "retired-strategy"):
            state["positions"][0]["strategy_id"] = owner
            result = portfolio_metrics(self.execution, sample, self.policy, initial_state=state,
                execution_model={"mode": "observed_quotes", "stress_fee_rate": str(FEE)})
            self.assertTrue(result["valid"], result["error"])
            self.assertEqual(len(result["final_state"]["positions"]), 1)
            self.assertEqual(result["final_state"]["cash"], "300000")

    def test_missing_current_candidate_price_is_invalid_without_reusing_prior_bar(self):
        sample = self.replay_frames()
        sample[1]["rows"] = sample[1]["rows"][1:]
        result = portfolio_metrics(self.execution, sample, self.policy,
            execution_model={"mode": "observed_quotes", "stress_fee_rate": str(FEE)})
        self.assertFalse(result["valid"])
        self.assertEqual(result["error"], "required_price_missing")

    def test_reserved_metadata_requires_exact_cost_and_legacy_missing_metadata_remains_supported(self):
        sample = self.replay_frames()
        state = {"cash": "1000000", "reserved_cash": "200000", "positions": []}
        legacy = deepcopy(self.execution)
        legacy.pop("allocation_mode")
        self.assertTrue(portfolio_metrics(legacy, sample, self.policy, initial_state=state)["valid"])
        result = portfolio_metrics(self.execution, sample, self.policy, initial_state=state)
        self.assertEqual(result["error"], "reserved_entries_required")
        state["reserved_entries"] = [{"symbol": fixtures.SYMBOLS[0], "strategy_id": "learned-volume", "cost": "199999"}]
        result = portfolio_metrics(self.execution, sample, self.policy, initial_state=state)
        self.assertEqual(result["error"], "reserved_entries_cost_mismatch")
        state["reserved_entries"][0]["cost"] = "200000"
        state["cash"] = "2000000"  # Realized profit does not enlarge the fixed investment cap.
        result = portfolio_metrics(self.execution, sample, self.policy, initial_state=state)
        self.assertTrue(result["valid"], result["error"])
        self.assertLessEqual(sum(Decimal(item["cost"]) for item in result["final_state"]["positions"])
                             + Decimal(result["final_state"]["reserved_cash"]), Decimal(1000000))

    def test_cash_reserve_and_existing_strategy_budget_remain_hard_caps(self):
        self.execution["cash_reserve"] = .6
        self.execution["strategies"][0]["weight"] = .4
        self.allocate()
        self.assertEqual(sum(row[2] for row in self.broker.submissions), 39)
        self.assertLessEqual(sum(Decimal(price) * quantity * (1 + FEE)
                               for _, _, quantity, price in self.broker.submissions), Decimal(400000))

    def test_live_extension_uses_entry_policy_prior_close_and_explicit_maximum(self):
        self.execution["strategies"][0].update(holding_sessions=2, stop_loss_pct=5, take_profit_pct=0,
            exit_mode="signal_extension", max_holding_sessions=6)
        exit_policy = {"holding_sessions": 2, "stop_loss_pct": 5, "take_profit_pct": 0,
            "exit_mode": "signal_extension", "max_holding_sessions": 6,
            "continuation_policy": deepcopy(self.execution), "continuation_strategy": "learned-volume"}
        position = {"symbol": fixtures.SYMBOLS[0], "entry_date": "2026-09-29", "quantity": 2,
            "average_price": str(Decimal(10000) * (1 + FEE)), "cost": str(Decimal(20000) * (1 + FEE)),
            "exit_policy": exit_policy}
        frame = {"as_of": "2026-10-02", "rows": [row(symbol=fixtures.SYMBOLS[0], price=11000)]}
        with patch.object(self.service.learning, "frames", return_value=[frame]), \
                patch.object(self.service.learning, "champion", side_effect=AssertionError("current policy is irrelevant")):
            self.assertFalse(self.service._exit_due(position, self.broker.days, self.current.date()))
            # Current price is not used; positive proceeds at the prior close
            # and the frozen entry signal determine continuation.
            self.broker.price = "8000"
            self.assertFalse(self.service._exit_due(position, self.broker.days, self.current.date()))
            frame["rows"][0]["learning_eligible"] = False
            self.assertTrue(self.service._exit_due(position, self.broker.days, self.current.date()))
            frame["rows"][0]["learning_eligible"] = True
            frame["rows"][0]["close"] = 10000
            self.assertTrue(self.service._exit_due(position, self.broker.days, self.current.date()))
            frame["rows"][0]["close"] = 11000
            position["entry_date"] = "2026-09-25"
            self.assertTrue(self.service._exit_due(position, self.broker.days, self.current.date()))

    def test_missing_extension_frame_cannot_extend_and_stop_precedes_minimum_hold(self):
        strategy = self.execution["strategies"][0]
        strategy.update(holding_sessions=5, stop_loss_pct=5, take_profit_pct=0,
                        exit_mode="signal_extension", max_holding_sessions=10)
        position = {"symbol": fixtures.SYMBOLS[0], "entry_date": "2026-10-02", "quantity": 2,
            "average_price": str(Decimal(10000) * (1 + FEE)), "cost": str(Decimal(20000) * (1 + FEE)),
            "exit_policy": {key: strategy[key] for key in
                ("holding_sessions", "stop_loss_pct", "take_profit_pct", "exit_mode", "max_holding_sessions")}}
        position["exit_policy"].update(continuation_policy=deepcopy(self.execution), continuation_strategy=strategy["id"])
        with patch.object(self.service.learning, "frames", return_value=[]), \
                patch("backend.experiments.HistoryCache") as cache:
            cache.return_value._read.return_value = {date(2026, 10, 2): {"close": 9400}}
            self.assertTrue(self.service._exit_due(position, self.broker.days, self.current.date()))
            cache.return_value._read.return_value = {date(2026, 10, 2): {"close": 11000}}
            self.assertFalse(self.service._exit_due(position, self.broker.days, self.current.date()))
            position["entry_date"] = "2026-09-28"
            self.assertTrue(self.service._exit_due(position, self.broker.days, self.current.date()))
            position["exit_pending"] = True
            self.assertTrue(self.service._exit_due(position, self.broker.days, self.current.date()))


class AllocationCycleTests(unittest.TestCase):
    make_service = learning_fixtures.LearningIntegrationTests.make_service
    completed_input = staticmethod(learning_fixtures.LearningIntegrationTests.completed_input)
    baseline_analysis = learning_fixtures.LearningIntegrationTests.baseline_analysis
    set_champion = learning_fixtures.LearningIntegrationTests.set_champion
    analyze_before_open = learning_fixtures.LearningIntegrationTests.analyze_before_open
    enable = fixtures.ExperimentsTests.enable

    def setUp(self):
        learning_fixtures.LearningIntegrationTests.setUp(self)

    def test_only_explicit_allocation_option_routes_cycle_to_new_planner(self):
        value = rules_policy(cash_reserve=0, max_positions=3)
        value["strategies"][0].update(weight=1, top_n=3)
        value["allocation_mode"] = "equal_eligible"
        self.set_champion(value)
        self.analyze_before_open()
        self.enable()
        with patch.object(self.service, "_allocated_entries", wraps=self.service._allocated_entries) as allocated:
            self.service._cycle()
        allocated.assert_called_once()
        self.assertEqual(sum(item[2] for item in self.broker.submissions), 49)
        # A legacy-format policy keeps the old path and its historical sizing.
        self.service.store.save_setting("enabled", False)
        value.pop("allocation_mode")
        self.set_champion(value)
        self.analyze_before_open()
        self.enable()
        with patch.object(self.service, "_allocated_entries", side_effect=AssertionError("opt-in required")):
            self.service._cycle()


if __name__ == "__main__":
    unittest.main()
