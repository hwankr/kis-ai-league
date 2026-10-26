"""Offline exact-source recovery, with real validators and a fake broker only."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from backend import entry_recovery
from backend.daily_inputs import freeze_inputs, freeze_entry_selection
from backend.experiment_analysis import analysis_version, build_snapshot, make_llm
from backend.experiment_store import encode
from backend.experiments import OperationalBlock
from backend.kis import KST
from backend.learning_policy import build_frame, policy_id, signals
from backend.paper_broker import BrokerStopped
from tests import test_learning_integration as fixtures


class RecoveryTests(unittest.TestCase):
    make_service = fixtures.LearningIntegrationTests.make_service
    completed_input = staticmethod(fixtures.LearningIntegrationTests.completed_input)
    baseline_analysis = fixtures.LearningIntegrationTests.baseline_analysis
    set_champion = fixtures.LearningIntegrationTests.set_champion

    def setUp(self):
        fixtures.LearningIntegrationTests.setUp(self)
        self.config.write_text(self.config.read_text(encoding='utf-8') +
            '\n[experiments.llm]\nprovider="codex"\nmodel="gpt-6-astra"\nreasoning_effort="ultra"\n', encoding='utf-8')
        self.service.learning.daily.enabled = True
        self.execution = fixtures.rules_policy(held_before_top_n=True, no_same_day_reentry=True)
        self.active = {'revision': 3, 'policy': self.execution, 'candidate_id': policy_id(self.execution),
                       'effective_day': '2026-10-01', 'run_id': 'prior-activation'}
        self.daily_key = 'daily_policy:' + self.service._policy()['fingerprint']
        self.service.store.save_setting(self.daily_key, {'revision': 3, 'active': self.active, 'pending': None})
        normalized = build_snapshot(**self.input_data)
        baseline = self.baseline_analysis(**self.input_data, llm=None)['decisions']
        frame = build_frame(self.input_data, baseline)
        frame.update(captured_at='2026-10-02T17:05:00+09:00', signal_inputs=freeze_inputs(self.input_data))
        selection = freeze_entry_selection(self.execution, {'positions': [], 'reserved_entries': []})
        decisions = baseline + signals(self.execution, build_frame(self.input_data), entry_context=selection['context'])
        base_key = entry_recovery.digest(encode({**{k: v for k, v in normalized.items() if k != 'observed_at'},
                                               'analysis_version': 'original-analysis'}))
        key = entry_recovery.digest(encode([base_key, policy_id(self.execution), 'original-learning',
            {k: v for k, v in build_frame(self.input_data).items() if k != 'observed_at'}]))
        self.run = {'id': 'original-run', 'as_of': '2026-10-02', 'created_at': '2026-10-02T17:00:00+09:00',
                    'version_id': 'original-analysis', 'learning_engine_version': 'original-learning',
                    'collection_hash': key, 'input_hash': entry_recovery.digest(encode(normalized)),
                    'learning_policy': self.execution, 'signals': decisions, 'status': 'ready',
                    'entry_selection': selection}
        self.full = {**self.run, 'analysis': {'version_id': self.run['version_id'], 'input': normalized,
                     'input_hash': self.run['input_hash'], 'llm_config': make_llm(self.config).public_config()}}
        self.service.store.add_run(self.full)
        with self.service.store.connect() as db:
            db.execute('UPDATE run_index SET input_key=? WHERE id=?', (base_key, self.run['id']))
            db.execute('INSERT INTO learning_frames VALUES (?,?,?)', (frame['as_of'],
                entry_recovery.digest(encode({k: v for k, v in frame.items() if k not in {'observed_at', 'captured_at'}})), encode(frame)))
            db.execute('INSERT INTO daily_policy_contexts VALUES (?,?,?)',
                       (self.service._policy()['fingerprint'], frame['as_of'], encode({'engine_version': 'original-daily',
                        'initial_state': {'as_of': frame['as_of'], 'positions': [], 'reserved_entries': []},
                        'limits': {k: str(self.service._policy()[k]) for k in ('budget', 'order_cap', 'daily_buy_limit')}})))
        self.manifest = {'version': 1, 'execution_day': '2026-10-06', 'signal_day': frame['as_of'],
            'run_id': self.run['id'], 'active_sha256': entry_recovery.digest(encode(self.active)),
            'policy_sha256': entry_recovery.digest(encode(self.service._policy())),
            'config_sha256': entry_recovery.digest(self.config.read_bytes()),
            'deployment_sha256': entry_recovery.LOADED_DEPLOYMENT,
            'current_analysis': analysis_version(self.config), 'current_learning': self.service.learning.version,
            'current_daily': self.service.learning.daily.engine_version,
            'original_analysis': self.run['version_id'], 'original_learning': self.run['learning_engine_version'],
            'original_daily': 'original-daily', 'llm': make_llm(self.config).public_config()}
        self.path = self.service.directory / 'entry-recovery.json'
        self.seal_records()

    def seal_records(self):
        with self.service.store.connect() as db:
            self.manifest['records'] = {name: entry_recovery.digest(db.execute(sql).fetchone()[0]) for name, sql in (
                ('run', "SELECT payload FROM runs WHERE id='original-run'"),
                ('summary', "SELECT summary FROM run_index WHERE id='original-run'"),
                ('frame', "SELECT payload FROM learning_frames WHERE day='2026-10-02'"),
                ('book', "SELECT payload FROM daily_policy_contexts WHERE day='2026-10-02'"))}
        self.path.write_text(encode(self.manifest), encoding='utf-8')

    def test_original_reused_with_full_validation_no_llm_research_or_rewrite(self):
        self.service._failure(OperationalBlock('original failed selection', 'entry_selection'), 'analyze')
        before = self.service.store.runs(details=True)
        with patch.object(self.service, '_learn') as learn, patch.object(self.service, '_prepare_daily') as prepare:
            self.assertEqual(self.service._analyze(), self.run)
        learn.assert_not_called()
        prepare.assert_not_called()
        self.analyzer.assert_not_called()
        self.assertEqual(self.service.store.runs(details=True), before)
        self.assertEqual(self.service._operation_failures(), {})
        self.assertFalse(any(x['state'] == 'open' for x in self.service.store.setting('issues', [])))
        from backend.learning_compatibility import live_entry_version_matches, version_matches
        self.assertFalse(live_entry_version_matches(self.run, self.service.learning.version))
        self.assertFalse(version_matches('original-daily', self.service.learning.daily.engine_version))

    def test_each_manifest_pin_fails_closed_and_other_day_grants_nothing(self):
        for field in ('run_id', 'signal_day', 'active_sha256', 'policy_sha256', 'config_sha256',
                      'deployment_sha256', 'current_analysis', 'current_learning', 'current_daily',
                      'original_analysis', 'original_learning', 'original_daily', 'llm'):
            with self.subTest(field=field):
                changed = deepcopy(self.manifest)
                changed[field] = 'changed'
                self.path.write_text(encode(changed), encoding='utf-8')
                with self.assertRaises(OperationalBlock):
                    self.service._analyze()
        self.path.write_text(encode(self.manifest), encoding='utf-8')
        self.current += timedelta(days=1)
        self.assertIsNone(entry_recovery.resolve(self.service))

    def test_raw_original_record_tampering_is_rejected(self):
        for name in ('run', 'summary', 'frame', 'book'):
            changed = deepcopy(self.manifest)
            changed['records'][name] = 'bad-hash'
            self.path.write_text(encode(changed), encoding='utf-8')
            with self.assertRaises(OperationalBlock):
                self.service._analyze()

    def test_current_input_change_or_actual_session_mismatch_blocks_recovery(self):
        symbol = next(iter(self.input_data['histories']))
        day = next(iter(self.input_data['histories'][symbol]))
        self.input_data['histories'][symbol][day]['close'] += 1
        with self.assertRaises(OperationalBlock):
            self.service._analyze()
        self.input_data['histories'][symbol][day]['close'] -= 1
        self.broker.days.append('2026-10-05')
        with self.assertRaises(OperationalBlock):
            self.service._analyze()
        self.assertEqual(self.broker.submissions, [])

    def test_sealed_but_inconsistent_baseline_still_rejected(self):
        self.full['signals'][0]['reason'] = 'changed explanation'
        with self.service.store.connect() as db:
            db.execute('UPDATE runs SET payload=? WHERE id=?', (encode(self.full), self.run['id']))
            db.execute('UPDATE run_index SET summary=? WHERE id=?',
                       (encode({k: v for k, v in self.full.items() if k != 'analysis'}), self.run['id']))
        self.seal_records()
        with self.assertRaises(OperationalBlock):
            self.service._analyze()

    def test_final_send_rechecks_revocation_and_expiry_without_touching_exits(self):
        token = entry_recovery.resolve(self.service)['token']
        decision = {**self.run['signals'][0], 'entry_recovery_token': token}
        policy = self.service._policy()
        args = dict(run_id=self.run['id'], own_order_id=None,
                    gate_revision=self.service._maintenance_gate().snapshot()['revision'],
                    active_pin=self.service._active_submission_pin(policy))
        self.path.unlink()
        with self.assertRaises(BrokerStopped):
            self.service._final_submission_check(policy, decision, 'buy', 1, Decimal(10000), **args)
        self.service._final_submission_check(policy, decision, 'sell', 1, Decimal(10000), **args)
        changed = deepcopy(self.manifest)
        changed['policy_sha256'] = 'tampered'
        self.path.write_text(encode(changed), encoding='utf-8')
        with self.assertRaises(BrokerStopped):
            self.service._final_submission_check(policy, decision, 'buy', 1, Decimal(10000), **args)
        self.path.write_text(encode(self.manifest), encoding='utf-8')
        self.current += timedelta(days=1)
        with self.assertRaises(BrokerStopped):
            self.service._final_submission_check(policy, decision, 'buy', 1, Decimal(10000), **args)

    def test_real_cycle_uses_original_and_restart_does_not_duplicate(self):
        self.service.store.save_setting('enabled', True)
        self.service._cycle()
        self.assertTrue(self.broker.submissions)
        self.assertEqual({x['run_id'] for x in self.service.store.orders()}, {self.run['id']})
        sent = deepcopy(self.broker.submissions)
        self.service.close()
        self.service = self.make_service()
        self.service.learning.daily.enabled = True
        self.service._cycle()
        self.assertEqual(self.broker.submissions, sent)

    def test_new_error_during_validation_is_not_cleared(self):
        self.service._failure(OperationalBlock('old generation', 'entry_selection'), 'analyze')
        validate = self.service._validate_frozen_entry_run
        def changed_generation(*args, **kwargs):
            result = validate(*args, **kwargs)
            self.service._failure(OperationalBlock('new generation', 'entry_selection'), 'analyze')
            return result
        with patch.object(self.service, '_validate_frozen_entry_run', side_effect=changed_generation):
            self.assertEqual(self.service._analyze(), self.run)
        self.assertEqual(self.service._operation_failures()['analyze']['last_error'], 'new generation')
        self.assertTrue(self.service._blocked())

    def test_rejected_run_owner_requires_exact_sealed_generation_and_original(self):
        rejected = deepcopy(self.full)
        rejected['id'] = 'rejected-run'
        rejected['signals'][0]['reason'] = 'newer rerolled explanation'
        self.service.store.add_run(rejected)
        error = OperationalBlock('wrong frozen run', 'entry_selection')
        error.run_id = rejected['id']
        self.service._failure(error, 'analyze')
        self.assertEqual(self.service._analyze(), self.run)
        self.assertTrue(self.service._blocked())
        failure = self.service._entry_validation_token('analyze')['entry_failure']
        self.manifest['failure_owners'] = {'analyze': entry_recovery.digest(encode(failure))}
        with self.service.store.connect() as db:
            self.manifest['rejected_runs'] = {rejected['id']: {
                'run': entry_recovery.digest(db.execute('SELECT payload FROM runs WHERE id=?', (rejected['id'],)).fetchone()[0]),
                'summary': entry_recovery.digest(db.execute('SELECT summary FROM run_index WHERE id=?', (rejected['id'],)).fetchone()[0])}}
        self.seal_records()
        self.assertEqual(self.service._analyze(), self.run)
        self.assertFalse(self.service._blocked())
        self.service._failure(error, 'analyze')
        self.assertEqual(self.service._analyze(), self.run)
        self.assertTrue(self.service._blocked())  # Same failed run, different generation.
        with self.service.store.connect() as db:
            db.execute('UPDATE runs SET payload=? WHERE id=?', (encode({**rejected, 'extra': 'changed'}), rejected['id']))
        with self.assertRaises(OperationalBlock):
            self.service._analyze()


if __name__ == '__main__':
    unittest.main()
