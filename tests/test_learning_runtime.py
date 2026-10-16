"""Offline service-to-learning regressions; brokers, research and clocks are local."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

from backend.experiments import ALL_STRATEGIES
from backend.learning_policy import portfolio_metrics
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

    def seed_held_position(self):
        self.current = self.current.replace(hour=10, minute=0)
        self.service.store.save_setting("user_paused", False)
        self.submit(quantity=2)
        self.fill(2, status="filled")
        self.broker.account["holdings"] = {
            "005930": {"quantity": 2, "sellable_quantity": 2, "price": "10000"}}

    def test_cycle_snapshot_protection_quotes_reach_two_frames_and_all_execution_scenarios(self):
        self.seed_held_position()
        policy_before = self.service._policy()
        positions_before = self.service._portfolio(self.service._owned_orders())[0]
        contexts = []
        frozen = []
        for index in range(2):
            data = raw_input(index)
            self.current = datetime.fromisoformat(data["observed_at"]).replace(hour=10, minute=0)
            observed_at = (self.current - timedelta(seconds=7)).isoformat()
            price = str(data["histories"]["005930"][data["as_of"]]["close"])
            self.broker.account["as_of"] = observed_at
            self.broker.account["holdings"]["005930"]["price"] = price
            expected = {"005930": {"price": price, "observed_at": observed_at,
                                    "source": "account_snapshot"}}
            cache_before = self.service.store.setting("learning_protection_quotes", {})
            original_exit = self.service._exit_due
            def check_exit(*args):
                self.assertEqual(self.service._cycle_protection_quotes, expected)
                self.assertEqual(self.service.store.setting("learning_protection_quotes", {}), cache_before)
                return original_exit(*args)
            with patch.object(self.service, "_exit_due", side_effect=check_exit) as exit_check, \
                    patch.object(self.broker, "quote", side_effect=AssertionError("snapshot already has prices")):
                self.service._cycle()
            exit_check.assert_called_once()
            cache = self.service.store.setting("learning_protection_quotes")
            self.assertEqual(cache["day"], data["as_of"])
            self.assertEqual(cache["fingerprint"], policy_before["fingerprint"])
            self.assertEqual(cache["quotes"], expected)
            self.assertEqual(self.service.store.setting("learning_quotes", {}), {})
            context = self.learn(index, process=True)
            self.assertEqual(context["protection_quotes"], expected)
            contexts.append(context)
            frozen.append(deepcopy(self.service.learning.frames()[-1]))
            self.assertEqual(frozen[-1]["protection_quotes"], expected)
        frames = self.service.learning.frames()
        self.assertEqual(frames, frozen)  # The next cycle cannot rewrite yesterday's frame.
        rules = candidate(hold=5)
        rules["strategies"][0]["conditions"] = [{"feature": "return_5d_pct", "op": "gt", "value": 10000}]
        arguments = {"initial_state": contexts[0]["initial_state"],
                     "execution_model": contexts[0]["execution_model"]}
        result = portfolio_metrics(rules, frames, policy_before, **arguments)
        self.assertTrue(result["valid"], result["error"])
        self.assertEqual(set(result["scenario_results"]), {"partial_fill", "delayed_sell", "adverse_buy"})
        for scenario in result["scenario_results"].values():
            self.assertTrue(scenario["valid"], scenario["error"])
            for field in ("final_state", "stress_final_state"):
                self.assertEqual(scenario[field]["positions"][0]["quantity"], 2)
                self.assertEqual(Decimal(scenario[field]["account_protection"]["limit_pct"]), 15)
                self.assertFalse(scenario[field]["account_protection"]["active"])
        missing = deepcopy(frames)
        for frame in missing:
            frame["protection_quotes"] = {}
        blocked = portfolio_metrics(rules, missing, policy_before, **arguments)
        self.assertFalse(blocked["valid"])
        self.assertEqual({value["error"] for value in blocked["scenario_results"].values()},
                         {"protection_quote_missing"})
        self.assertEqual(self.service._policy(), policy_before)
        self.assertEqual(self.service._portfolio(self.service._owned_orders())[0], positions_before)
        self.assertEqual(len(self.broker.submissions), 1)  # Only the fixture's confirmed buy.
        self.assertEqual(self.broker.cancellations, [])
        self.assertTrue(self.service.store.setting("enabled"))
        self.assertFalse(self.service.store.setting("user_paused"))
        self.assertEqual(self.service.risk_limit, 15)

    def test_protective_cancel_and_exit_finish_before_protection_cache_write(self):
        self.seed_held_position()
        self.submit(quantity=1, symbol="000660", key="pending-buy")
        policy = self.service._policy()
        self.service.store.save_setting("learning_account:" + policy["fingerprint"], {
            "risk": {"active": True, "peak": "1", "limit_pct": 15}})
        observed_at = (self.current - timedelta(seconds=7)).isoformat()
        self.broker.account["as_of"] = observed_at
        calls = []
        cancel, submit, save = self.service._cancel, self.service._submit, self.service._save_protection_quotes
        def record_cancel(*args, **kwargs):
            calls.append("cancel")
            self.assertEqual(self.service.store.setting("learning_protection_quotes", {}), {})
            return cancel(*args, **kwargs)
        def record_submit(*args, **kwargs):
            calls.append(args[3])
            self.assertEqual(self.service.store.setting("learning_protection_quotes", {}), {})
            result = submit(*args, **kwargs)
            self.current += timedelta(seconds=2)
            return result
        def record_save(observations):
            calls.append("cache")
            return save(observations)
        with patch.object(self.service, "_cancel", side_effect=record_cancel), \
                patch.object(self.service, "_submit", side_effect=record_submit), \
                patch.object(self.service, "_save_protection_quotes", side_effect=record_save), \
                patch.object(self.service, "_activate_daily", side_effect=AssertionError("protected entry")):
            self.service._cycle()
        self.assertEqual(calls, ["cancel", "sell", "cache"])
        self.assertEqual(self.broker.submissions[-1][:3], ("005930", "sell", 2))
        saved = self.service.store.setting("learning_protection_quotes")["quotes"]["005930"]
        self.assertEqual(saved, {"price": "10000", "observed_at": observed_at, "source": "account_snapshot"})
        self.assertEqual(self.service.learning_account.state(policy["fingerprint"])["risk"]["limit_pct"], 15)
        self.assertTrue(self.service.learning_account.state(policy["fingerprint"])["risk"]["active"])
        # A later off-hours cycle must not persist the prior cycle's in-memory observation.
        self.current = self.current.replace(hour=16)
        with patch.object(self.service, "_save_protection_quotes") as save_after_close:
            self.service._cycle()
        save_after_close.assert_not_called()

    def test_protection_cache_failure_preserves_trade_return_and_original_exception(self):
        observations = {"005930": {"price": "10000", "observed_at": self.current.isoformat(),
                                    "source": "account_snapshot"}}
        result = object()
        for original_error in (None, RuntimeError("original trading failure")):
            with self.subTest(failed_trade=original_error is not None):
                def trade():
                    self.service._cycle_protection_quotes = observations
                    if original_error is not None:
                        raise original_error
                    return result
                with patch.object(self.service, "_trade_cycle", side_effect=trade), \
                        patch.object(self.service, "_save_protection_quotes", side_effect=OSError("cache failure")) as save, \
                        patch.object(self.service, "_issue", wraps=self.service._issue) as issue:
                    if original_error is None:
                        self.assertIs(self.service._cycle(), result)
                    else:
                        with self.assertRaises(RuntimeError) as raised:
                            self.service._cycle()
                        self.assertIs(raised.exception, original_error)
                save.assert_called_once_with(observations)
                issue.assert_called_once()
                self.assertEqual(issue.call_args.args[0], "protection_observation")
                self.assertFalse(issue.call_args.kwargs["blocking"])
        self.assertEqual(self.broker.submissions, [])

    def test_protection_cache_and_issue_failures_do_not_hide_original_trading_error(self):
        original_error = RuntimeError("original trading failure")
        def trade():
            self.service._cycle_protection_quotes = {}
            raise original_error
        with patch.object(self.service, "_trade_cycle", side_effect=trade), \
                patch.object(self.service, "_save_protection_quotes", side_effect=OSError("cache failure")) as save, \
                patch.object(self.service, "_issue", side_effect=OSError("issue write failure")) as issue:
            with self.assertRaises(RuntimeError) as raised:
                self.service._cycle()
        self.assertIs(raised.exception, original_error)
        save.assert_called_once_with({})
        issue.assert_called_once()
        self.assertEqual(self.broker.submissions, [])
        self.assertFalse(self.service.store.setting("enabled"))
        self.assertTrue(self.service.store.setting("user_paused"))
        self.assertEqual(self.service.risk_limit, 15)

    def test_cycle_fallback_quote_keeps_its_real_time_instead_of_snapshot_time(self):
        self.seed_held_position()
        snapshot_at = (self.current - timedelta(seconds=60)).isoformat()
        self.broker.account["as_of"] = snapshot_at
        self.broker.account["holdings"]["005930"]["price"] = None
        with patch.object(self.broker, "quote", wraps=self.broker.quote) as quote:
            self.service._cycle()
        quote.assert_called_once_with("005930")
        saved = self.service.store.setting("learning_protection_quotes")["quotes"]["005930"]
        self.assertEqual(saved, {"price": "10000", "observed_at": self.current.isoformat(),
                                 "source": "decision_quote"})
        self.assertNotEqual(saved["observed_at"], snapshot_at)
        self.assertNotIn("eligible", saved)
        self.assertNotIn("buy_eligible", saved)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.broker.cancellations, [])

    def test_six_symbol_protection_cache_is_immutable_and_scoped_to_day_and_fingerprint(self):
        self.current = self.current.replace(hour=10, minute=0)
        policy = self.service._policy()
        risk_key = "learning_account:" + policy["fingerprint"]
        risk = {"risk": {"active": True, "peak": "1.1", "limit_pct": 15}}
        self.service.store.save_setting(risk_key, risk)
        symbols = ("005930", "000660", "035420", "051910", "068270", "011200")
        observations = {symbol: {"price": str(10000 + index), "observed_at": self.current.isoformat(),
            "source": "account_snapshot", "eligible": True, "buy_eligible": True,
            "quantity": 145 if symbol == "011200" else 2} for index, symbol in enumerate(symbols)}
        original = self.service.store.save_setting
        with patch.object(self.service.store, "save_setting", wraps=original) as writes:
            self.service._save_protection_quotes(observations)
            first = self.service.store.setting("learning_protection_quotes")
            self.current += timedelta(seconds=30)
            changed = {symbol: dict(value, price="20000", observed_at=self.current.isoformat())
                       for symbol, value in observations.items()}
            self.service._save_protection_quotes(changed)
        self.assertEqual(sum(call.args[0] == "learning_protection_quotes" for call in writes.call_args_list), 1)
        self.assertEqual(self.service.store.setting("learning_protection_quotes"), first)
        self.assertEqual(set(first["quotes"]), set(symbols))
        self.assertTrue(all(set(value) == {"price", "observed_at", "source"} for value in first["quotes"].values()))
        self.assertEqual(self.service.store.setting(risk_key), risk)
        self.assertEqual(self.service._policy(), policy)
        self.assertEqual(self.service.store.orders(), [])
        self.assertFalse(self.service.store.setting("enabled"))
        self.assertTrue(self.service.store.setting("user_paused"))
        self.current = datetime.fromisoformat(raw_input(1)["observed_at"]).replace(hour=10, minute=0)
        self.service._save_protection_quotes({"011200": dict(observations["011200"], price="21000",
                                                            observed_at=self.current.isoformat())})
        next_day = self.service.store.setting("learning_protection_quotes")
        self.assertEqual(next_day["day"], self.current.date().isoformat())
        self.assertEqual(set(next_day["quotes"]), {"011200"})
        self.assertEqual(next_day["quotes"]["011200"]["price"], "21000")
        self.service.store.save_setting("policy", dict(policy, fingerprint="other-fixture-account"))
        self.service._save_protection_quotes({"005930": dict(observations["005930"],
                                                            observed_at=self.current.isoformat())})
        next_account = self.service.store.setting("learning_protection_quotes")
        self.assertEqual(next_account["fingerprint"], "other-fixture-account")
        self.assertEqual(set(next_account["quotes"]), {"005930"})
        self.current = datetime.fromisoformat(raw_input(2)["observed_at"]).replace(hour=10, minute=0)
        self.service._save_protection_quotes({"005930": observations["005930"]})
        empty_day = self.service.store.setting("learning_protection_quotes")
        self.assertEqual(empty_day["day"], self.current.date().isoformat())
        self.assertEqual(empty_day["quotes"], {})  # A failed fresh observation cannot inherit old prices.
        self.assertEqual(self.service.store.setting(risk_key), risk)

    def test_protection_cache_rejects_invalid_prices_sources_and_observation_times(self):
        self.current = self.current.replace(hour=10, minute=0)
        observation = {"price": "10000", "observed_at": self.current.isoformat(), "source": "account_snapshot"}
        invalid = [dict(observation, price=value) for value in (None, "0", "-1", "NaN", "Infinity")]
        invalid += [dict(observation, source="unverified"),
                    dict(observation, observed_at=None),
                    dict(observation, observed_at=self.current.replace(tzinfo=None).isoformat())]
        invalid += [dict(observation, observed_at=(self.current + delta).isoformat()) for delta in
                    (timedelta(seconds=-181), timedelta(seconds=1), timedelta(days=-1))]
        for index, value in enumerate(invalid):
            with self.subTest(index=index):
                self.service._save_protection_quotes({"005930": value})
                self.assertEqual(self.service.store.setting("learning_protection_quotes")["quotes"], {})
        self.service._save_protection_quotes({"005930": dict(observation,
            observed_at=(self.current - timedelta(seconds=180)).isoformat())})
        self.assertEqual(self.service.store.setting("learning_protection_quotes")["quotes"]["005930"]["price"], "10000")
        # Explicit unavailability is evidence, never a usable or eligible price.
        self.service._save_protection_quotes({"011200": dict(observation, price=None, source="unavailable")})
        self.assertEqual(self.service.store.setting("learning_protection_quotes")["quotes"]["011200"],
                         dict(observation, price=None, source="unavailable"))

    def test_learn_does_not_attach_protection_quotes_from_another_day_or_account(self):
        policy = self.service._policy()
        for day, fingerprint in ((raw_input(1)["as_of"], policy["fingerprint"]),
                                 (self.data["as_of"], "other-fixture-account")):
            with self.subTest(day=day, fingerprint=fingerprint):
                self.service.store.save_setting("learning_protection_quotes", {
                    "day": day, "fingerprint": fingerprint, "quotes": {"011200": {
                        "price": "10000", "observed_at": self.current.isoformat(), "source": "account_snapshot"}}})
                self.assertNotIn("protection_quotes", self.learn(0))

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
        self.service.store.save_setting('user_paused', False)
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
        self.service.store.save_setting('user_paused', False)
        self.service._symbol_quote(self.broker, "005930")
        original = self.broker.submit
        def inspect(*args, **kwargs):
            order = self.service.store.orders()[-1]
            self.assertEqual(order['status'], 'submitting')
            self.assertEqual(order['decision_price'], '10000')
            self.assertEqual(order['decision_volume'], 100)
            self.assertEqual(datetime.fromisoformat(order['decision_observed_at']), self.current)
            self.assertIsNone(order['execution_prediction']['fill_ratio'])
            return original(*args, **kwargs)
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
        self.service.store.save_setting('user_paused', False)
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
