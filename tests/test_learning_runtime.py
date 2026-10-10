"""Offline service-to-learning regressions; brokers, research and clocks are local."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

from backend.experiments import ALL_STRATEGIES
from backend.paper_broker import BrokerUnknown
from tests import test_experiments as service_fixtures
from tests.test_learning import candidate, raw_input


class LearningRuntimeTests(unittest.TestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        service_fixtures.ExperimentsTests.setUp(self)
        self.policy.update(execution_strategy=ALL_STRATEGIES, budget="1000000",
                           order_cap="250000", daily_buy_limit="500000")
        self.service._configure(self.policy)
        self.service.learning.enabled = True
        self.service.learning.llm_factory = None
        self.service.learning.development_reader = None
        self.service.learning.proposer = Mock(return_value={"policy": candidate(), "method": "fixture",
                                                           "rationale": "offline", "development": None})
        self.service.learning.enqueue = Mock()
        self.broker.days = raw_input(4)["calendars"]["KOSPI"]
        self.data = raw_input(0)
        self.current = datetime.fromisoformat(self.data["observed_at"])
        self.service.store.save_settings({"enabled": False, "user_paused": True})

    make_service = service_fixtures.ExperimentsTests.make_service
    submit = service_fixtures.ExperimentsTests.submit
    fill = service_fixtures.ExperimentsTests.fill
    enable = service_fixtures.ExperimentsTests.enable

    def learn(self, index, *, process=False):
        self.data = raw_input(index)
        self.current = datetime.fromisoformat(self.data["observed_at"])
        run = {"collection_hash": "fixture-" + self.data["as_of"],
               "signals": [{**service_fixtures.decision(), "as_of": self.data["as_of"]}]}
        self.service._learn(self.data, run)
        args, kwargs = self.service.learning.enqueue.call_args
        if process:
            self.service.learning.process(*args, context=kwargs["context"])
        return deepcopy(kwargs["context"])

    def test_first_decision_quote_is_frozen_once_per_symbol_day_and_reaches_eod_frame(self):
        self.current = self.current.replace(hour=10, minute=0)
        first_at = self.current.isoformat(timespec="microseconds")
        original = self.service.store.save_setting
        with patch.object(self.service.store, "save_setting", wraps=original) as writes:
            self.assertEqual(self.service._symbol_quote(self.broker, "005930"), Decimal("10000"))
            self.service._symbol_quote(self.broker, "005930")
            self.current += timedelta(minutes=1)
            self.broker.price = "10100"
            self.assertEqual(self.service._symbol_quote(self.broker, "005930"), Decimal("10100"))
        self.assertEqual(sum(call.args[0] == "learning_quotes" for call in writes.call_args_list), 1)
        expected = {"price": "10000", "eligible": True, "buy_eligible": True, "observed_at": first_at,
                    "volume": 100, "last_trade_at": (datetime.fromisoformat(first_at).astimezone(
                        self.current.tzinfo) - timedelta(seconds=30)).isoformat()}
        self.assertEqual(self.service.store.setting("learning_quotes")["quotes"], {"005930": expected})
        context = self.learn(0, process=True)
        self.assertEqual(context["execution_quotes"], {"005930": expected})
        self.assertEqual(self.service.learning.frames()[0]["execution_quotes"], {"005930": expected})
        self.current = datetime.fromisoformat(raw_input(1)["observed_at"]).replace(hour=10, minute=0)
        self.service._symbol_quote(self.broker, "005930")
        self.assertEqual(self.service.store.setting("learning_quotes")["quotes"]["005930"]["price"], "10100")
        self.assertEqual(self.broker.submissions, [])

    def test_automatic_observes_quotes_during_user_pause_without_orders_or_repeated_polling(self):
        self.learn(0, process=True)
        self.current = datetime.fromisoformat(raw_input(1)["observed_at"]).replace(hour=10, minute=0)
        with patch.object(self.service, "_analyze", return_value={}) as analysis, \
                patch.object(self.broker, "quote", wraps=self.broker.quote) as quote, \
                patch.object(self.service, "_cycle", side_effect=AssertionError("paused cycle")):
            self.service._automatic()
            self.service._automatic()
        self.assertEqual(analysis.call_count, 2)
        quote.assert_called_once_with("005930")
        self.assertFalse(self.service.store.setting("enabled"))
        self.assertTrue(self.service.store.setting("user_paused"))
        self.assertEqual(self.broker.submissions, [])
        self.assertEqual(self.broker.cancellations, [])
        self.assertEqual(self.service.store.orders(), [])
        self.llm.assert_not_called()

    def test_quote_collection_includes_inherited_and_cost_stress_only_positions_with_null_final_state(self):
        self.learn(0, process=True)
        state = self.service.learning.state()
        state["trial"]["policy"]["strategies"][0]["conditions"] = [
            {"feature": "return_5d_pct", "op": "gt", "value": 10000}]
        state["trial"]["initial_state"] = {"positions": []}
        state["trial"]["attribution_quote_symbols"] = ['035420']
        state["trial"]["metrics"] = {
            "champion": {"final_state": None, "stress_final_state": None},
            "challenger": {"final_state": None, "stress_final_state": None,
                "scenario_results": {"delayed_sell": {"final_state": None,
                    "stress_final_state": {"positions": [{"symbol": "000660"}]}}}}}
        state["scorecard_book"] = {"positions": [{"symbol": "005930"}]}
        self.service.store.save_setting("learning", state)
        self.current = datetime.fromisoformat(raw_input(1)["observed_at"]).replace(hour=10, minute=0)
        with patch.object(self.broker, "quote", wraps=self.broker.quote) as quote:
            self.service._observe_learning_quotes(self.service._policy())
        self.assertEqual({call.args[0] for call in quote.call_args_list}, {"005930", "000660", "035420"})
        self.assertEqual(set(self.service.store.setting("learning_quotes")["quotes"]), {"005930", "000660", "035420"})
        self.assertEqual(self.broker.submissions, [])

    def test_live_owned_book_is_recorded_only_after_1600_for_the_same_session(self):
        self.current = self.current.replace(hour=10, minute=0)
        self.submit(quantity=2)
        self.fill(2, status="filled")
        self.service.store.save_settings({"enabled": False, "user_paused": True})
        run = {"collection_hash": "fixture", "signals": []}
        self.current = self.current.replace(hour=15, minute=59)
        self.service._learn(self.data, run)
        self.assertNotIn("initial_state", self.service.learning.enqueue.call_args.kwargs["context"])
        with self.service.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM learning_daily").fetchone()[0], 0)
        self.current = self.current.replace(hour=16, minute=0)
        self.service._learn(self.data, run)
        book = self.service.learning.enqueue.call_args.kwargs["context"]["initial_state"]
        self.assertEqual(book["as_of"], self.data["as_of"])
        self.assertEqual(book["positions"][0]["quantity"], 2)
        self.current += timedelta(minutes=1)
        self.service._learn(self.data, run)
        self.current = datetime.fromisoformat(raw_input(1)["observed_at"])
        self.service._learn(self.data, run)  # Current holdings cannot be backdated to yesterday.
        self.assertNotIn("initial_state", self.service.learning.enqueue.call_args.kwargs["context"])
        with self.service.store.connect() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT day FROM learning_daily")], [self.data["as_of"]])

    def test_service_execution_model_and_observed_quotes_evaluate_both_policies(self):
        context = self.learn(0, process=True)
        trial = deepcopy(self.service.learning.state()["trial"])
        self.assertEqual(trial["execution_model"], context["execution_model"])
        self.assertEqual(trial["execution_model"]["status"], "insufficient")
        self.assertTrue(all(item["model"]["mode"] == "observed_quotes"
                            for item in trial["execution_model"]["scenarios"]))
        self.assertEqual(trial["initial_state"]["cash"], self.policy["budget"])
        self.current = datetime.fromisoformat(raw_input(1)["observed_at"]).replace(hour=10, minute=0)
        self.service._symbol_quote(self.broker, "005930")
        self.learn(1, process=True)
        observed = self.service.learning.state()["trial"]
        self.assertEqual(observed["id"], trial["id"])
        for name in ("champion", "challenger"):
            result = observed["metrics"][name]
            self.assertTrue(result["valid"], result["error"])
            self.assertTrue(result["final_state"])
            self.assertTrue(result["stress_final_state"])
            self.assertGreater(result["diagnostics"]["normal"]["buy_attempts"], 0)
            self.assertGreater(result["diagnostics"]["normal"]["buy_filled_quantity"], 0)
            self.assertFalse(result["promotion_supported"])
            self.assertEqual(set(result["scenario_results"]), {"partial_fill", "delayed_sell", "adverse_buy"})
        self.assertEqual(observed["elapsed_intervals"], 1)
        self.assertEqual(self.broker.submissions, [])

    def test_prediction_is_saved_before_submit_and_only_prior_terminal_results_update_it(self):
        from backend.learning_account import execution_profile
        self.current = self.current.replace(hour=10, minute=0)
        self.service._symbol_quote(self.broker, "005930")
        original = self.broker.submit
        def inspect(*args):
            order = self.service.store.orders()[-1]
            self.assertEqual(order['status'], 'submitting')
            self.assertEqual(order['decision_price'], '10000')
            self.assertEqual(order['decision_volume'], 100)
            self.assertEqual(datetime.fromisoformat(order['decision_observed_at']), self.current)
            self.assertIsNone(order['execution_prediction']['fill_ratio'])
            return original(*args)
        with patch.object(self.broker, 'submit', side_effect=inspect):
            first = self.submit(quantity=2)
        prediction = deepcopy(first['execution_prediction'])
        self.current += timedelta(seconds=10)
        self.fill(1)
        self.assertIsNone(execution_profile(self.service.store.orders())['estimates']['buy_fill_ratio'])
        self.current += timedelta(seconds=10)
        self.fill(2, status='filled')
        first = self.service.store.orders()[0]
        self.assertEqual(first['execution_prediction'], prediction)
        self.assertLess(first['first_fill_observed_at'], first['last_fill_observed_at'])
        self.assertEqual(first['last_fill_observed_at'], first['terminal_observed_at'])
        second = self.submit(quantity=2, key='second')
        self.assertEqual(second['execution_prediction']['fill_ratio'], 1)
        self.assertEqual(second['execution_prediction']['sample_orders'], 1)
        self.current += timedelta(seconds=10)
        self.fill(1, index=1, status='cancelled')
        profile = execution_profile(self.service.store.orders())
        self.assertEqual(profile['estimates']['buy_fill_ratio'], .75)
        self.assertEqual(profile['prediction_basis']['buy'], 1)
        self.assertEqual(profile['prediction_mae']['buy'], .5)
        self.assertEqual(self.service.store.orders()[1]['execution_prediction'], second['execution_prediction'])

    def test_unresolved_source_order_delays_trial_handoff_without_counting_as_failure(self):
        self.current = self.current.replace(hour=10, minute=0)
        self.broker.submit_error = BrokerUnknown("offline missing acknowledgement")
        order = self.submit(quantity=2)
        self.service.store.save_settings({"enabled": False, "user_paused": True})
        context = self.learn(0, process=True)
        self.assertEqual(context["initial_state"]["pending_orders"], 1)
        self.assertEqual(context["initial_state"]["unresolved_orders"], 1)
        self.assertIsNone(self.service.learning.state().get("trial"))
        self.service.learning.proposer.assert_not_called()
        evidence = self.service.learning._research_history()[0]["execution"]
        self.assertEqual(evidence["terminal_orders"], 0)
        self.assertEqual(evidence["unresolved_orders"], 1)
        self.assertIsNone(evidence["rejection_rate"])
        self.assertIsNone(evidence["fill_ratio"])
        self.assertIsNone(context["execution_model"]["estimates"]["buy_fill_ratio"])
        self.assertEqual(context["execution_model"]["status"], "insufficient")
        # A separately confirmed terminal cancellation unlocks the next session.
        order.update(status="cancelled", remaining_quantity=0)
        self.service.store.save_order(order)
        self.learn(1, process=True)
        self.assertIsNotNone(self.service.learning.state().get("trial"))
        self.service.learning.proposer.assert_called_once()
        self.assertFalse(self.service.store.setting("enabled"))
        self.assertTrue(self.service.store.setting("user_paused"))


if __name__ == "__main__":
    unittest.main()
