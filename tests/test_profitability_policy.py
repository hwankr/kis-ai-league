"""Regression cases for bounded profitability options and inherited account risk."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from backend.experiment_store import ExperimentStore
from backend.learning import LearningService
from backend.learning_history import enrich_selection
from backend.learning_policy import (BASELINE_POLICY, build_frame, portfolio_metrics,
                                     position_exit_due, signals, validate_policy)
from tests.test_learning_policy import LIMITS, days, frames, policy, row
from tests.test_learning import raw_input


class CandidateScopeTests(unittest.TestCase):
    def test_scope_uses_previous_exchange_sessions_not_last_positive_bars(self):
        calendar = days(62)
        data = {"as_of": calendar[-1], "observed_at": calendar[-1]+"T16:05:00+09:00",
                "rows": [{"symbol": "005930", "board": "KOSPI"}],
                "histories": {"005930": {day: {**row(), "turnover": 10**10} for day in calendar}},
                "calendars": {"KOSPI": calendar}}
        value = policy(candidate_scope="liquid_all")
        self.assertEqual(len(signals(value, build_frame(data))), 1)
        without_calendar = {**data, "calendars": {}}
        self.assertEqual(signals(value, build_frame(without_calendar)), [])
        data["histories"]["005930"][calendar[-10]]["volume"] = 0
        self.assertEqual(signals(value, build_frame(data)), [])
        data["histories"]["005930"][calendar[-10]]["volume"] = 1000
        del data["histories"]["005930"][calendar[-40]]
        self.assertEqual(signals(value, build_frame(data)), [])

    def test_top20_and_all_change_scope_before_strategy_ranking(self):
        frame = frames(1)[0]
        frame["rows"] = [row(f"{index:06}", selection={"complete_61": True,
            "positive_volume_20": True, "turnover_20d_mean": 10**10+index}) for index in range(21)]
        for index, item in enumerate(frame["rows"]):
            item["features"]["return_5d_pct"] = -index
        self.assertEqual(signals(policy(candidate_scope="liquid_all"), frame)[0]["symbol"], "000000")
        self.assertEqual(signals(policy(candidate_scope="liquid_top20"), frame)[0]["symbol"], "000001")
        self.assertEqual(signals(policy(), frame)[0]["symbol"], "000000")

    def test_missing_scope_evidence_fails_instead_of_using_current_universe(self):
        with self.assertRaisesRegex(ValueError, "candidate_scope_metadata_missing"):
            signals(policy(candidate_scope="liquid_all"), frames(1)[0])
        self.assertEqual(len(signals(policy(), frames(1)[0])), 1)

    def test_enrichment_is_nonmutating_and_requires_identical_prices_and_features(self):
        frozen = frames(1)[0]
        original = deepcopy(frozen)
        derived = deepcopy(frozen)
        derived["rows"][0]["selection"] = {"complete_61": True, "positive_volume_20": True,
                                           "turnover_20d_mean": 10**10}
        enriched = enrich_selection(frozen, derived)
        self.assertEqual(frozen, original)
        self.assertIn("selection", enriched["rows"][0])
        for field in ("close", "features", "board"):
            changed = deepcopy(derived)
            changed["rows"][0][field] = {"return_5d_pct": 2} if field == "features" else 2
            self.assertEqual(enrich_selection(frozen, changed), frozen)

    def test_future_history_cannot_change_liquidity_selection(self):
        calendar = days(61)
        data = {"as_of": calendar[-1], "observed_at": calendar[-1]+"T16:05:00+09:00",
                "rows": [{"symbol": "005930", "board": "KOSPI"}],
                "histories": {"005930": {day: {**row(), "turnover": 10**10} for day in calendar}},
                "calendars": {"KOSPI": calendar}}
        before = build_frame(data)
        data["calendars"]["KOSPI"].append("2027-01-04")
        data["histories"]["005930"]["2027-01-04"] = {**row(price=1000), "turnover": 10**14}
        self.assertEqual(build_frame(data), before)


class ExitExtensionTests(unittest.TestCase):
    def rule(self):
        value = policy()
        value["strategies"][0].update(exit_mode="signal_extension", max_holding_sessions=8)
        return signals(value, frames(1)[0])[0]["exit_policy"]

    def due(self, rule=None, **changes):
        args = {"symbol": "005930", "held_sessions": 5, "entry_price": 100, "last_close": 110,
                "quantity": 10, "cost": 1001, "frame": frames(1)[0]}
        args.update(changes)
        return position_exit_due(rule or self.rule(), **args)

    def test_only_profitable_still_ranked_winner_extends_to_bounded_age(self):
        self.assertFalse(self.due())
        self.assertTrue(self.due(held_sessions=8))
        self.assertTrue(self.due(last_close=100))
        self.assertTrue(self.due(frame=None))
        removed = frames(1)[0]
        removed["rows"][0]["learning_eligible"] = False
        self.assertTrue(self.due(frame=removed))

    def test_stop_take_and_frozen_policy_override_future_policy_changes(self):
        rule = self.rule()
        rule["take_profit_pct"] = 5
        self.assertTrue(self.due(rule))
        rule["take_profit_pct"] = 0
        rule["stop_loss_pct"] = 5
        self.assertTrue(self.due(rule, last_close=90, held_sessions=1))
        frame = frames(1)[0]
        frame["rows"].append(row("000660"))
        frame["rows"][1]["features"]["return_5d_pct"] = 100
        self.assertTrue(self.due(rule, frame=frame))

    def test_halted_maximum_exit_stays_pending_and_json_restart_matches(self):
        observations = frames(5, price=110)
        for frame in observations:
            frame["rows"][0]["learning_eligible"] = False
        observations[1]["rows"][0]["volume"] = 0
        initial = {"as_of": observations[0]["as_of"], "cash": "9000", "positions": [{
            "symbol": "005930", "strategy_id": "momentum", "quantity": 10, "cost": "1000",
            "entry_price": "100", "last_close": "110", "held_sessions": 7,
            "exit_policy": self.rule()}]}
        whole = portfolio_metrics(policy(), observations, LIMITS, initial_state=initial)
        first = portfolio_metrics(policy(), observations[:2], LIMITS, initial_state=initial)
        self.assertTrue(first["valid"], first)
        position = first["final_state"]["positions"][0]
        self.assertTrue(position["exit_pending"])
        self.assertEqual(position["quantity"], 10)
        rest = portfolio_metrics(policy(), observations[1:], LIMITS,
                                 initial_state=json.loads(json.dumps(first["final_state"])))
        self.assertEqual(whole["final_state"], rest["final_state"])
        self.assertEqual(whole["closed_trades"], 1)

    def test_optional_fields_preserve_legacy_schema_and_reject_unbounded_exit(self):
        self.assertEqual(validate_policy(BASELINE_POLICY), BASELINE_POLICY)
        with self.assertRaises(ValueError):
            validate_policy({**BASELINE_POLICY, "allocation_mode": "equal_eligible"})
        value = policy()
        value["strategies"][0].update(exit_mode="signal_extension", max_holding_sessions=21)
        with self.assertRaisesRegex(ValueError, "max_holding_sessions"):
            validate_policy(value)


class InheritedProtectionTests(unittest.TestCase):
    def initial(self, *, active=False):
        return {"as_of": frames(1)[0]["as_of"], "cash": "9000", "positions": [{
            "symbol": "005930", "strategy_id": "old-strategy", "quantity": 10, "cost": "1000",
            "entry_price": "100", "last_close": "100", "held_sessions": 0,
            "exit_policy": {"holding_sessions": 20, "stop_loss_pct": 0, "take_profit_pct": 0}}],
            "account_protection": {"units": "10000", "peak": "1.175", "active": active, "limit_pct": 15}}

    def test_prior_peak_triggers_before_buy_and_halted_exit_is_not_cash(self):
        observations = frames(3)
        for frame in observations:
            frame["rows"][0]["learning_eligible"] = True
        observations[1]["rows"][0].update(open=90, high=91, low=89, close=90, volume=0)
        result = portfolio_metrics(policy(), observations[:2], LIMITS, initial_state=self.initial())
        self.assertTrue(result["valid"], result)
        self.assertTrue(result["final_state"]["account_protection"]["active"])
        self.assertEqual(result["final_state"]["cash"], "9000")
        self.assertTrue(result["final_state"]["positions"][0]["exit_pending"])
        self.assertEqual(result["diagnostics"]["normal"]["buy_filled_quantity"], 0)

    def test_active_protection_survives_new_policy_and_restart_after_rebound(self):
        observations = frames(4, price=110)
        for frame in observations:
            frame["rows"][0]["learning_eligible"] = True
        initial = self.initial(active=True)
        result = portfolio_metrics(policy(allocation_mode="equal_eligible"), observations, LIMITS, initial_state=initial)
        self.assertTrue(result["valid"], result)
        self.assertTrue(result["final_state"]["account_protection"]["active"])
        self.assertEqual(result["final_state"]["positions"], [])
        part = portfolio_metrics(policy(allocation_mode="equal_eligible"), observations[:2], LIMITS, initial_state=initial)
        restart = portfolio_metrics(policy(allocation_mode="equal_eligible"), observations[1:], LIMITS,
                                    initial_state=json.loads(json.dumps(part["final_state"])))
        self.assertEqual(result["final_state"], restart["final_state"])

    def test_invalid_protection_is_rejected(self):
        initial = self.initial()
        initial["account_protection"]["units"] = "0"
        result = portfolio_metrics(policy(), frames(), LIMITS, initial_state=initial)
        self.assertFalse(result["valid"])
        self.assertIn("invalid_account_protection", result["error"])

    def quoted_frames(self, *, holding_time='09:04:00', buy_time='09:05:00', holding_price=100):
        observations = frames(2)
        for frame in observations:
            frame['rows'][0]['learning_eligible'] = False
            frame['rows'].append(row('000660'))
            frame['execution_quotes'] = {
                '005930': {'eligible': True, 'price': holding_price,
                           'observed_at': frame['as_of'] + 'T' + holding_time + '+09:00'},
                '000660': {'eligible': True, 'price': 100,
                           'observed_at': frame['as_of'] + 'T' + buy_time + '+09:00'}}
        return observations

    def test_later_holding_quote_cannot_block_or_fund_an_earlier_entry(self):
        for price in (90, 100):
            result = portfolio_metrics(policy(), self.quoted_frames(holding_time='09:10:00', holding_price=price),
                LIMITS, initial_state=self.initial(), execution_model={'mode': 'observed_quotes'})
            self.assertFalse(result['valid'])
            self.assertEqual(result['error'], 'protection_quote_after_decision')

    def test_observed_protection_requires_valid_held_quotes_without_close_fallback(self):
        for kind in ('missing', 'future', 'zero'):
            observations = self.quoted_frames()
            quote = observations[1]['execution_quotes']['005930']
            if kind == 'missing':
                del observations[1]['execution_quotes']['005930']
            elif kind == 'future':
                quote['observed_at'] = observations[1]['as_of'] + 'T16:06:00+09:00'
            else:
                quote['price'] = 0
            result = portfolio_metrics(policy(), observations, LIMITS, initial_state=self.initial(),
                                       execution_model={'mode': 'observed_quotes'})
            self.assertFalse(result['valid'], kind)
            self.assertIn(result['error'], {'protection_quote_missing', 'execution_quote_outside_session', 'invalid_execution_quote'})

    def test_separate_early_protection_quote_preserves_later_execution_prices(self):
        observations = self.quoted_frames(holding_time='09:10:00', holding_price=107)
        observations[1]['protection_quotes'] = {'005930': {
            'price': 100, 'observed_at': observations[1]['as_of'] + 'T09:04:00+09:00'}}
        initial = self.initial()
        initial['account_protection']['peak'] = '1'
        initial['positions'][0]['exit_policy']['holding_sessions'] = 1
        before = deepcopy((observations, initial))
        result = portfolio_metrics(policy(), observations, LIMITS, initial_state=initial,
                                   execution_model={'mode': 'observed_quotes'})
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['trading_activity']['gross_sell_amount'], 1070)
        self.assertGreater(result['trading_activity']['gross_buy_amount'], 0)
        self.assertEqual(result['final_state']['positions'][0]['symbol'], '000660')
        self.assertEqual(result['final_state']['positions'][0]['entry_price'], '100')
        self.assertEqual((observations, initial), before)

    def test_explicit_protection_map_never_falls_back_and_keeps_time_price_checks(self):
        cases = {'empty': ({}, 'protection_quote_missing'),
                 'null': (None, 'protection_quote_missing'),
                 'wrong_shape': ([], 'protection_quote_missing'),
                 'late': ('09:06:00', 'protection_quote_after_decision'),
                 'future': ('16:06:00', 'execution_quote_outside_session'),
                 'zero': (0, 'invalid_execution_quote')}
        for kind, (value, error) in cases.items():
            with self.subTest(kind=kind):
                observations = self.quoted_frames()
                if kind in {'late', 'future', 'zero'}:
                    value = {'005930': {'price': 0 if kind == 'zero' else 100,
                        'observed_at': observations[1]['as_of'] + 'T' +
                            ('09:04:00' if kind == 'zero' else value) + '+09:00'}}
                observations[1]['protection_quotes'] = value
                before = deepcopy(observations)
                result = portfolio_metrics(policy(), observations, LIMITS, initial_state=self.initial(),
                                           execution_model={'mode': 'observed_quotes'})
                self.assertFalse(result['valid'], kind)
                self.assertEqual(result['error'], error)
                self.assertEqual(observations, before)

    def test_protection_quotes_never_grant_trade_permission_or_supply_execution_quotes(self):
        for missing in (False, True):
            with self.subTest(missing_execution_quote=missing):
                observations = self.quoted_frames()
                observations[1]['protection_quotes'] = deepcopy(observations[1]['execution_quotes'])
                if missing:
                    del observations[1]['execution_quotes']['000660']
                else:
                    observations[1]['execution_quotes']['000660']['eligible'] = False
                initial = self.initial()
                initial['account_protection']['peak'] = '1'
                result = portfolio_metrics(policy(), observations, LIMITS, initial_state=initial,
                                           execution_model={'mode': 'observed_quotes'})
                if missing:
                    self.assertFalse(result['valid'])
                    self.assertEqual(result['error'], 'execution_quote_missing')
                else:
                    self.assertTrue(result['valid'], result)
                    self.assertEqual(result['trading_activity']['gross_buy_amount'], 0)

    def test_absent_protection_map_retains_legacy_replay_without_mutating_frames(self):
        observations = self.quoted_frames()
        before = deepcopy(observations)
        legacy = portfolio_metrics(policy(), observations, LIMITS, initial_state=self.initial(),
                                   execution_model={'mode': 'observed_quotes'})
        explicit = deepcopy(observations)
        explicit[1]['protection_quotes'] = deepcopy(explicit[1]['execution_quotes'])
        separate = portfolio_metrics(policy(), explicit, LIMITS, initial_state=self.initial(),
                                     execution_model={'mode': 'observed_quotes'})
        self.assertTrue(legacy['valid'], legacy)
        self.assertEqual(legacy, separate)
        self.assertEqual(observations, before)
        self.assertTrue(all('protection_quotes' not in frame for frame in observations))

    def test_later_close_does_not_change_earlier_buy_but_can_activate_close_protection(self):
        initial = self.initial()
        initial['account_protection']['peak'] = '1'
        normal = self.quoted_frames()
        fallen = deepcopy(normal)
        for item in fallen[1]['rows']:
            item.update(close=50, low=49)
        outputs = [portfolio_metrics(policy(), observed, LIMITS, initial_state=initial,
                    execution_model={'mode': 'observed_quotes'}) for observed in (normal, fallen)]
        self.assertTrue(all(item['valid'] for item in outputs), outputs)
        self.assertEqual(outputs[0]['equity_curve'][1]['buy_cost'], outputs[1]['equity_curve'][1]['buy_cost'])
        self.assertGreater(outputs[0]['equity_curve'][1]['buy_cost'], 0)
        self.assertFalse(outputs[0]['final_state']['account_protection']['active'])
        self.assertTrue(outputs[1]['final_state']['account_protection']['active'])


class ProtectionQuoteFrameTests(unittest.TestCase):
    def test_both_learning_modes_freeze_optional_quotes_without_retrofitting_old_frames(self):
        for daily in (False, True):
            for supplied in (False, True):
                with self.subTest(daily=daily, supplied=supplied), tempfile.TemporaryDirectory() as directory:
                    store = ExperimentStore(Path(directory) / 'experiments.sqlite3')
                    data = raw_input(0)
                    current = datetime.fromisoformat(data['observed_at'])
                    learner = LearningService(store, enabled=True, daily_enabled=daily, now=lambda: current)
                    self.addCleanup(learner.close)
                    context = {'execution_quotes': {}}
                    quotes = {'005930': {'price': 100, 'observed_at': data['as_of'] + 'T09:04:00+09:00'}}
                    if supplied:
                        context['protection_quotes'] = deepcopy(quotes)
                    learner.process(data, [], LIMITS, context=context)
                    frozen = learner.frames()[0]
                    with store.connect() as db:
                        original = db.execute('SELECT digest,payload FROM learning_frames').fetchone()
                    self.assertEqual('protection_quotes' in frozen, supplied)
                    if supplied:
                        self.assertEqual(frozen['protection_quotes'], quotes)
                    learner.process(data, [], LIMITS, context=context)
                    context['protection_quotes'] = {'005930': {**quotes['005930'], 'price': 1}}
                    learner.process(data, [], LIMITS, context=context)
                    self.assertEqual(learner.frames()[0], frozen)
                    with store.connect() as db:
                        self.assertEqual(db.execute('SELECT digest,payload FROM learning_frames').fetchone(), original)


if __name__ == "__main__":
    unittest.main()
