"""Daily activation integration using temporary records and an offline broker."""
from copy import deepcopy
from datetime import date, datetime
import json
import sqlite3
import unittest
from unittest.mock import Mock, patch

from backend.daily_policy import market_input_hash
from backend.experiment_store import encode
from backend.kis import KST
from backend.learning_policy import build_frame, policy_id
from tests import test_experiments as fixtures
from tests import test_learning_integration as learning_fixtures


rules_policy = learning_fixtures.rules_policy


class PreparedDaily:
    """Execution-side contract fake; transaction invariants belong to service tests."""
    def __init__(self, store, fallback):
        self.store = store
        self.fallback = fallback
        self.enabled = True
        self.proposal = None
        self.current = None
        self.activations = []
        self.notes = []

    def pending(self):
        return deepcopy(self.proposal)

    def active(self):
        return deepcopy(self.current)

    def policy(self):
        return deepcopy(self.current["policy"]) if self.current else self.fallback()

    def note(self, status, error=None, *, fingerprint=None):
        self.notes.append((status, error))
        if status == "blocked":
            self.proposal = None

    def prepare(self, pending, run):
        self.proposal = {**deepcopy(pending), "prepared_run": deepcopy(run)}
        return True

    def activate(self, pending, *, effective_day):
        run = pending["prepared_run"]
        self.store.add_run(run)
        self.current = {"policy": deepcopy(pending["policy"]), "effective_day": effective_day,
                        "run_id": run["id"]}
        self.activations.append(effective_day)
        self.proposal = None
        return True


class DailyExecutionTests(unittest.TestCase):
    make_service = learning_fixtures.LearningIntegrationTests.make_service
    completed_input = staticmethod(learning_fixtures.LearningIntegrationTests.completed_input)
    baseline_analysis = learning_fixtures.LearningIntegrationTests.baseline_analysis
    set_champion = learning_fixtures.LearningIntegrationTests.set_champion
    enable = fixtures.ExperimentsTests.enable
    fill = fixtures.ExperimentsTests.fill

    def setUp(self):
        learning_fixtures.LearningIntegrationTests.setUp(self)
        self.real_daily = self.service.learning.daily
        self.daily = PreparedDaily(self.service.store, self.service.learning.champion)
        self.service.learning.daily = self.daily
        self.original = rules_policy()
        self.candidate = rules_policy(name="다음 장 후보", cash_reserve=.5)
        self.current = datetime(2026, 10, 2, 17, tzinfo=KST)
        self.baseline = self.service._analyze()
        self.propose()

    def propose(self):
        self.daily.proposal = {"policy": deepcopy(self.candidate), "signal_day": self.input_data["as_of"],
            "candidate_id": policy_id(self.candidate), "parent_id": policy_id(self.original),
            "revision": 0, "decision_id": "daily-decision",
            "engine_version": self.real_daily.engine_version,
            "fingerprint": self.service._policy()["fingerprint"],
            "limits": {key: self.service._policy()[key] for key in ("budget", "order_cap", "daily_buy_limit")},
            "market_input_hash": market_input_hash(self.input_data)}

    def prepare(self):
        self.service._analyze()
        return self.daily.pending()["prepared_run"]

    def use_real_daily(self):
        pending = self.daily.pending()
        self.daily = self.real_daily
        self.daily.enabled = True
        self.service.learning.daily = self.daily
        self.service.store.save_setting("daily_policy:" + pending["fingerprint"],
                                        {"pending": pending, "revision": 0, "status": "prepared"})
        with self.service.store.connect() as db:
            db.execute("INSERT INTO daily_policy_decisions VALUES (?,?,?,?)",
                       (pending["decision_id"], pending["fingerprint"], pending["signal_day"],
                        encode({"id": pending["decision_id"], "decision": "apply"})))

    def test_real_staged_run_and_active_policy_survive_restart_together(self):
        self.use_real_daily()
        before = self.service.store.run_revision()
        prepared = self.prepare()
        self.assertEqual(self.service.store.run_revision(), before)
        self.enable()
        self.current = fixtures.NOW
        self.service._cycle()
        self.assertEqual(self.daily.active()["run_id"], prepared["id"])
        self.assertEqual(self.service.store.orders()[0]["run_id"], prepared["id"])
        self.service = self.make_service()
        self.service.learning.daily.enabled = True
        self.assertEqual(policy_id(self.service._adopted_policy()), policy_id(self.candidate))
        self.service._cycle()
        self.assertEqual(len(self.broker.submissions), 1)

    def test_real_activation_storage_failure_leaves_no_half_published_run(self):
        self.use_real_daily()
        self.prepare()
        before = self.service.store.run_revision()
        with self.service.store.connect() as db:
            db.execute("CREATE TRIGGER fail_daily BEFORE UPDATE ON settings "
                       "WHEN NEW.key LIKE 'daily_policy:%' BEGIN SELECT RAISE(ABORT,'disk failure'); END")
        self.enable()
        self.current = fixtures.NOW
        with self.assertRaises(sqlite3.IntegrityError):
            self.service._cycle()
        self.assertEqual(self.service.store.run_revision(), before)
        self.assertIsNone(self.daily.active())
        self.assertIn("prepared_run", self.daily.pending())
        self.assertEqual(policy_id(self.service._adopted_policy()), policy_id(self.original))
        self.assertEqual(self.broker.submissions, [])

    def test_changed_daily_evaluator_cannot_activate_prepared_policy(self):
        self.use_real_daily()
        self.prepare()
        self.daily.engine_version = "changed-daily-evaluation"
        self.current = fixtures.NOW
        self.assertFalse(self.service._activate_daily(self.current.date(), self.broker.days))
        self.assertIsNone(self.daily.active())
        self.assertEqual(policy_id(self.service._adopted_policy()), policy_id(self.original))

    def test_real_activation_rechecks_user_pause_at_transaction_boundary(self):
        self.use_real_daily()
        self.prepare()
        self.service.store.save_settings({"enabled": False, "user_paused": True})
        self.current = fixtures.NOW
        self.assertFalse(self.service._activate_daily(self.current.date(), self.broker.days))
        self.assertIsNone(self.daily.active())
        self.assertEqual(self.broker.submissions, [])

    def test_daily_llm_metadata_freezes_current_account_and_daily_engine(self):
        self.service.learning.daily = self.real_daily
        self.real_daily.enabled = True
        legacy = {**self.service.learning.state(), "fingerprint": "legacy-account", "engine_version": "legacy-engine"}
        self.service.store.save_setting("learning", legacy)
        account = self.service._policy()
        def propose(*args, on_llm_proposal, **kwargs):
            self.service.store.save_setting("policy", {**account, "fingerprint": "changed-during-provider"})
            on_llm_proposal(self.candidate)
            return {"policy": self.candidate}
        with patch("backend.learning_research.propose_candidate", side_effect=propose):
            self.service._propose_learning(self.original, [build_frame(self.input_data)],
                                           self.daily.pending()["limits"], [])
        with self.service.store.connect() as db:
            payload = json.loads(db.execute("SELECT payload FROM learning_proposals").fetchone()[0])
        self.assertEqual(payload["fingerprint"], account["fingerprint"])
        self.assertEqual(payload["engine_version"], self.real_daily.engine_version)
        self.assertEqual(self.service.learning.state(), legacy)

    def test_prepared_signals_are_private_and_repeat_does_not_reroll(self):
        before = self.service.store.run_revision()
        run = self.prepare()
        self.assertEqual(self.service.store.run_revision(), before)
        self.assertEqual(policy_id(self.service._adopted_policy()), policy_id(self.original))
        self.assertTrue(all(row["policy_id"] == policy_id(self.candidate)
                            for row in run["signals"] if row.get("policy_id")))
        self.assertEqual(self.prepare(), run)
        self.assertEqual(self.broker.submissions, [])

    def test_broker_calendar_defers_holiday_then_applies_frozen_signals(self):
        prepared = self.prepare()
        controls = self.service._policy()
        self.enable()
        self.current = datetime(2026, 10, 5, 10, tzinfo=KST)
        self.service._cycle()
        self.assertEqual(self.daily.activations, [])
        self.assertEqual(self.broker.submissions, [])
        self.current = fixtures.NOW
        self.service._cycle()
        self.assertEqual(self.daily.activations, ["2026-10-06"])
        order = self.service.store.orders()[0]
        self.assertEqual(order["run_id"], prepared["id"])
        self.assertEqual(order["policy_id"], policy_id(self.candidate))
        self.assertEqual(self.service._policy(), controls)
        self.service._cycle()
        self.assertEqual(len(self.broker.submissions), 1)

    def test_after_open_cannot_prepare_today_or_activate_a_late_run(self):
        self.current = fixtures.NOW.replace(hour=9, minute=1)
        self.service._analyze()
        self.assertNotIn("prepared_run", self.daily.pending())
        self.assertFalse(self.service._activate_daily(date(2026, 10, 6), self.broker.days))
        self.current = datetime(2026, 10, 2, 17, tzinfo=KST)
        self.prepare()
        self.daily.proposal["prepared_run"]["created_at"] = "2026-10-06T09:00:00+09:00"
        self.current = fixtures.NOW
        self.assertFalse(self.service._activate_daily(date(2026, 10, 6), self.broker.days))
        self.assertEqual(self.daily.activations, [])

    def test_changed_market_input_cannot_prepare_candidate(self):
        self.input_data["histories"][fixtures.SYMBOLS[0]][date(2026, 10, 2)]["turnover"] += 1
        self.service._analyze()
        self.assertNotIn("prepared_run", self.daily.pending())
        self.assertEqual(policy_id(self.service._adopted_policy()), policy_id(self.original))

    def test_first_cycle_rechecks_market_input_before_asynchronous_analysis(self):
        self.use_real_daily()
        before = self.service.store.run_revision()
        self.prepare()
        self.input_data["histories"][fixtures.SYMBOLS[0]][date(2026, 10, 2)]["turnover"] += 1
        self.current = fixtures.NOW
        self.enable()
        self.service._cycle()
        self.assertIsNone(self.daily.active())
        self.assertIsNone(self.daily.pending())
        self.assertEqual(self.daily.state()["status"], "blocked")
        self.assertEqual(self.service.store.run_revision(), before)
        self.assertTrue(all(order.get("policy_id") == policy_id(self.original)
                            for order in self.service.store.orders()))

    def test_activation_needs_the_original_signal_day_and_readable_market_input(self):
        self.prepare()
        self.current = fixtures.NOW
        with patch.object(self.service, "input_reader", side_effect=OSError("unavailable")):
            self.assertFalse(self.service._activate_daily(self.current.date(), self.broker.days))
        self.assertEqual(self.daily.notes[-1][0], "error")
        self.assertIsNotNone(self.daily.pending())
        changed_day = {**self.input_data, "as_of": "2026-10-01"}
        with patch.object(self.service, "input_reader", return_value=changed_day):
            self.assertFalse(self.service._activate_daily(self.current.date(), self.broker.days))
        self.assertEqual(self.daily.notes[-1][0], "blocked")
        self.assertIsNone(self.daily.pending())
        self.assertEqual(self.daily.activations, [])

    def test_changed_engine_or_missed_next_session_keeps_active_policy(self):
        self.prepare()
        version = self.service.learning.version
        self.service.learning.version = "different-engine"
        self.assertFalse(self.service._activate_daily(date(2026, 10, 6), self.broker.days))
        self.service.learning.version = version
        self.broker.days.append("2026-10-07")
        self.assertFalse(self.service._activate_daily(date(2026, 10, 7), self.broker.days))
        self.assertEqual(self.daily.activations, [])

    def test_pause_allows_preparation_without_enabling_orders_or_activation(self):
        self.service.store.save_settings({"enabled": False, "user_paused": True})
        self.prepare()
        self.current = fixtures.NOW
        self.service._cycle()
        self.assertEqual(self.daily.activations, [])
        self.assertFalse(self.service.store.setting("enabled"))
        self.assertTrue(self.service.store.setting("user_paused"))
        self.assertEqual(self.broker.submissions, [])

    def test_loss_protection_does_not_activate_a_new_entry_policy(self):
        self.prepare()
        self.enable()
        self.service.learning_account.observe = Mock(return_value={"risk": {"active": True}})
        self.current = fixtures.NOW
        self.service._cycle()
        self.assertEqual(self.daily.activations, [])
        self.assertEqual(self.broker.submissions, [])

    def test_stale_candidate_display_does_not_prevent_preopen_preparation(self):
        self.current = fixtures.NOW.replace(hour=8)
        self.service.candidate_service = Mock(snapshot=Mock(return_value={"status": "complete", "stale": True}))
        self.service._automatic()
        self.assertIn("prepared_run", self.daily.pending())
        self.assertEqual(self.daily.activations, [])
        self.assertEqual(self.broker.submissions, [])

    def test_afterclose_next_automatic_loop_prepares_completed_proposal(self):
        self.service.candidate_service = Mock(snapshot=Mock(return_value={"status": "complete", "stale": False}))
        self.service._automatic()
        self.assertIn("prepared_run", self.daily.pending())
        self.assertEqual(self.daily.activations, [])
        self.assertEqual(self.broker.submissions, [])

    def test_activation_day_uses_committed_run_even_if_newer_matching_run_exists(self):
        prepared = self.prepare()
        self.current = fixtures.NOW
        self.assertTrue(self.service._activate_daily(self.current.date(), self.broker.days))
        other = deepcopy(prepared)
        other.update(id="newer-run", created_at="2026-10-06T08:59:00+09:00", signals=[])
        self.service.store.add_run(other)
        self.enable()
        self.service._cycle()
        self.assertEqual(self.service.store.orders()[0]["run_id"], prepared["id"])

    def test_existing_position_keeps_entry_exit_policy_after_daily_activation(self):
        self.daily.proposal = None
        self.enable()
        self.current = fixtures.NOW
        self.service._cycle()
        quantity = self.broker.submissions[0][2]
        self.fill(quantity, status="filled")
        self.broker.account["holdings"][fixtures.SYMBOLS[0]] = {
            "quantity": quantity, "sellable_quantity": quantity, "price": "10000"}
        for bars in self.input_data["histories"].values():
            bars[date(2026, 10, 6)] = deepcopy(bars[date(2026, 10, 2)])
        self.input_data["calendars"]["KOSPI"].append(date(2026, 10, 6))
        self.input_data.update(as_of="2026-10-06", observed_at="2026-10-06T17:00:00+09:00")
        for row in self.input_data["rows"]:
            row["as_of"] = "2026-10-06"
        self.candidate["strategies"][0]["holding_sessions"] = 20
        self.current = fixtures.NOW.replace(hour=17)
        self.propose()
        self.prepare()
        self.broker.days += ["2026-10-07", "2026-10-08"]
        self.current = fixtures.NOW.replace(day=7)
        self.service._cycle()
        position = self.service._portfolio(self.service._owned_orders())[0][0]
        self.assertEqual(position["policy_id"], policy_id(self.original))
        self.assertEqual(position["exit_policy"]["holding_sessions"], 2)
        self.current = fixtures.NOW.replace(day=8)
        self.service._cycle()
        self.assertEqual(self.broker.submissions[-1], (fixtures.SYMBOLS[0], "sell", quantity, "10000"))
        self.assertEqual(self.service.store.orders()[-1]["policy_id"], policy_id(self.original))
        self.fill(1, index=1, status="partial")
        self.service._cycle()
        position = self.service._portfolio(self.service._owned_orders())[0][0]
        self.assertEqual(position["quantity"], quantity - 1)
        self.assertEqual(position["policy_id"], policy_id(self.original))
        self.assertEqual(len(self.broker.submissions), 2)
        feed = self.service.store.mirror_snapshot()
        self.assertEqual([(row["side"], row["quantity"]) for row in feed["events"]],
                         [("buy", quantity), ("sell", 1)])


if __name__ == "__main__":
    unittest.main()
