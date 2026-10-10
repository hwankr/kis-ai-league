"""Offline learned-policy execution regressions using only an in-memory fake broker."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import unittest
from unittest.mock import Mock, patch

from backend.experiment_analysis import analysis_version, build_snapshot
from backend.experiments import ALL_STRATEGIES, FEE
from backend.kis import KST, KisError
from backend.learning_policy import BASELINE_POLICY, policy_id, validate_policy
from tests import test_experiments as fixtures


def rules_policy(**changes):
    return validate_policy({"kind": "rules", "name": "검증 후보", "cash_reserve": .6,
        "max_positions": 1, "entry_slippage_bps": 100, "cancel_after_minutes": 15,
        "strategies": [{"id": "learned-volume", "name": "거래대금 순위", "weight": .4,
            "conditions": [], "rank_by": "turnover", "descending": True, "top_n": 2,
            "holding_sessions": 2, "stop_loss_pct": 5., "take_profit_pct": 8.}], **changes})


class LearningIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Reuse account/store setup without inheriting unrelated test methods.
        fixtures.ExperimentsTests.setUp(self)
        self.policy.update(budget="1000000", order_cap="300000", daily_buy_limit="500000",
                           execution_strategy=ALL_STRATEGIES)
        self.service._configure(self.policy)
        self.input_data = self.completed_input()
        self.analyzer.side_effect = self.baseline_analysis
        self.service._refresh_shadow = Mock()
        self.set_champion(rules_policy())
        self.network = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def make_service(self):
        service = fixtures.ExperimentsTests.make_service(self)
        service.learning.enabled = True
        service.learning.enqueue = Mock()
        return service

    enable = fixtures.ExperimentsTests.enable
    fill = fixtures.ExperimentsTests.fill

    @staticmethod
    def completed_input():
        day = date(2026, 10, 2)
        calendar = []
        while len(calendar) < 63:
            if day.weekday() < 5:
                calendar.append(day)
            day -= timedelta(days=1)
        calendar.reverse()
        histories = {}
        for symbol, turnover in zip(fixtures.SYMBOLS, (300000, 200000, 100000)):
            histories[symbol] = {day: {"open": 10000, "high": 10100, "low": 9900,
                "close": 10000, "volume": 1000, "turnover": turnover} for day in calendar}
        return {"as_of": "2026-10-02", "observed_at": "2026-10-02T17:00:00+09:00",
            "rows": [{"symbol": symbol, "name": "시험 " + symbol, "board": "KOSPI", "status": "ok",
                      "as_of": "2026-10-02", "selection": {"status": "selected"}, "learning_eligible": True}
                     for symbol in fixtures.SYMBOLS],
            "histories": histories, "calendars": {"KOSPI": calendar},
            "benchmarks": {"KOSPI": {day: 1000 for day in calendar}}}

    def baseline_analysis(self, **data):
        data.pop("llm")  # Never invoke the injected LLM object.
        normalized = build_snapshot(**data)
        return {"decisions": [{**fixtures.decision(symbol), "score": None,
                    "reason": "관측 조건 충족", "evidence_ids": [f"market:{symbol}:2026-10-02"]}
                    for symbol in fixtures.SYMBOLS],
                "input_hash": "frozen-baseline", "version_id": analysis_version(self.config),
                "llm_status": {"status": "ready"}, "input": normalized}

    def set_champion(self, policy):
        self.service.store.save_setting("learning", {"status": "evaluating",
            "champion": {"policy": deepcopy(policy), "adopted_at": "2026-10-01T07:00:00Z"},
            "last_evaluation": None, "last_change": None, "error": None})

    def analyze_before_open(self):
        self.current = datetime(2026, 10, 2, 17, tzinfo=KST)
        run = self.service._analyze()
        self.current = fixtures.NOW
        return run

    def test_adopted_policy_sizes_entries_and_counts_pending_positions_without_duplicate_orders(self):
        policy = rules_policy()
        run = self.analyze_before_open()
        learned = [item for item in run["signals"] if item.get("policy_id")]
        self.assertEqual([item["symbol"] for item in learned], list(fixtures.SYMBOLS[:2]))
        self.enable()
        self.service._cycle()
        expected = int(Decimal("500000") * Decimal(".4") / 2 / (Decimal("10000") * (1 + FEE)))
        self.assertEqual(self.broker.submissions, [(fixtures.SYMBOLS[0], "buy", expected, "10000")])
        self.service._cycle()
        self.assertEqual(len(self.broker.submissions), 1)
        order = self.service.store.orders()[0]
        self.assertEqual(order["policy_id"], policy_id(policy))
        self.assertEqual(order["cancel_after_minutes"], 15)
        self.assertEqual(order["exit_policy"]["holding_sessions"], 2)
        self.llm.assert_not_called()
        self.assertIsNone(self.service.learning.worker)

    def test_rank_interleaving_matches_execution_when_position_limit_binds(self):
        first = {**rules_policy()["strategies"][0], "id": "high", "weight": .5}
        second = {**first, "id": "low", "name": "낮은 거래대금", "descending": False}
        self.set_champion(rules_policy(cash_reserve=0, max_positions=2, strategies=[first, second]))
        run = self.analyze_before_open()
        learned = [item for item in run["signals"] if item.get("policy_id")]
        self.assertEqual([(item["strategy_id"], item["symbol"]) for item in learned],
                         [("high", "005930"), ("low", "035420"), ("high", "000660"), ("low", "000660")])
        self.enable()
        self.service._cycle()
        self.assertEqual([item[0] for item in self.broker.submissions], ["005930", "035420"])

    def test_adopted_entry_gap_blocks_order_without_discarding_signals(self):
        run = self.analyze_before_open()
        self.assertTrue(any(item.get("policy_id") for item in run["signals"]))
        self.broker.price = "10200"
        self.enable()
        self.service._cycle()
        self.assertEqual(self.broker.submissions, [])
        self.assertEqual(self.service.store.orders(), [])

    def test_external_account_cash_does_not_replenish_strategy_losses(self):
        self.analyze_before_open()
        self.broker.account["cash"] = "9000000"
        original = self.service._portfolio
        def realized(orders):
            positions, metrics = original(orders)
            return positions, [*metrics, {"strategy_id": "prior", "realized_pnl": "-380000"}]
        with patch.object(self.service, "_portfolio", side_effect=realized):
            self.enable()
            self.service._cycle()
        # 1m budget - 380k loss - 600k cash floor leaves 20k, not external cash.
        self.assertEqual(self.broker.submissions[0][2], 1)

    def test_engine_update_requires_new_preopen_analysis(self):
        self.analyze_before_open()
        self.service.learning.version = "new-evaluation-version"
        self.enable()
        self.service._cycle()
        self.assertEqual(self.broker.submissions, [])

    def test_existing_position_keeps_original_exit_policy_after_champion_changes(self):
        original = rules_policy()
        self.analyze_before_open()
        self.enable()
        self.service._cycle()
        quantity = self.broker.submissions[0][2]
        self.fill(quantity, status="filled")
        self.broker.account["holdings"]["005930"] = {"quantity": quantity, "sellable_quantity": quantity}
        changed = deepcopy(original)
        changed["name"] = "새 장기 전략"
        changed["strategies"][0].update(holding_sessions=20, stop_loss_pct=0, take_profit_pct=0)
        self.set_champion(changed)
        positions, _ = self.service._portfolio(self.service._owned_orders())
        self.assertEqual(positions[0]["policy_id"], policy_id(original))
        self.assertEqual(positions[0]["exit_policy"], {"holding_sessions": 2, "stop_loss_pct": 5., "take_profit_pct": 8.})
        self.broker.days += ["2026-10-07", "2026-10-08"]
        self.current = fixtures.NOW.replace(day=7)
        self.service._cycle()
        self.assertEqual(len(self.broker.submissions), 1)
        self.current = fixtures.NOW.replace(day=8)
        self.service._cycle()
        self.assertEqual(self.broker.submissions[-1], ("005930", "sell", quantity, "10000"))
        self.assertEqual(self.service.store.orders()[-1]["policy_id"], policy_id(original))

    def test_user_pause_survives_policy_change_and_service_restart(self):
        self.analyze_before_open()
        self.enable()
        self.service.command({"action": "pause"})
        self.set_champion(rules_policy(name="교체된 후보"))
        self.service = self.make_service()
        self.service._cycle()
        self.assertFalse(self.service.store.setting("enabled"))
        self.assertTrue(self.service.store.setting("user_paused"))
        self.assertEqual(self.broker.submissions, [])
        self.assertEqual(self.service.store.orders(), [])

    def test_legacy_rollback_cannot_execute_old_policy_or_reroll_cached_baseline(self):
        learned = self.analyze_before_open()
        self.assertEqual(self.analyzer.call_count, 1)
        self.set_champion(BASELINE_POLICY)
        self.enable()
        self.service._cycle()
        self.assertEqual(self.broker.submissions, [])
        self.current = datetime(2026, 10, 2, 17, 1, tzinfo=KST)
        legacy = self.service._analyze()
        self.assertNotEqual(legacy["id"], learned["id"])
        self.assertEqual(legacy["learning_policy"]["kind"], "legacy")
        self.assertTrue(all(not item.get("policy_id") for item in legacy["signals"]))
        self.assertEqual(self.analyzer.call_count, 1)
        self.assertEqual(len(self.service.store.runs()), 2)
        self.current = fixtures.NOW
        self.service._cycle()
        self.assertGreater(len(self.broker.submissions), 0)
        self.assertTrue(all(order["strategy_id"] == fixtures.STRATEGY for order in self.service.store.orders()))

    def test_learned_strategy_cannot_be_configured_without_its_policy_context(self):
        with self.assertRaises(KisError):
            self.service._configure({**self.policy, "execution_strategy": "learned-volume"})
        self.assertEqual(self.service._policy()["execution_strategy"], ALL_STRATEGIES)

    def test_snapshot_exposes_optional_learning_contract_and_serializable_strategy_facts(self):
        self.analyze_before_open()
        snapshot = self.service.snapshot()
        json.dumps(snapshot, allow_nan=False)
        learning = snapshot["learning"]
        self.assertEqual(set(learning), {"enabled", "status", "champion", "challenger", "last_evaluation", "last_change", "error"})
        self.assertTrue(learning["enabled"])
        self.assertEqual(learning["champion"]["id"], policy_id(rules_policy()))
        self.assertEqual(learning["champion"]["name"], "검증 후보")
        self.assertIsNone(learning["challenger"])
        spec = next(item for item in snapshot["strategies"] if item["id"] == "learned-volume")
        self.assertIs(spec["selectable"], False)
        self.assertEqual(spec["version"], learning["champion"]["id"])
        for signal in snapshot["runs"][0]["signals"]:
            self.assertTrue({"strategy_id", "symbol", "name", "action", "score", "reason", "evidence_ids"} <= signal.keys())


if __name__ == "__main__":
    unittest.main()
