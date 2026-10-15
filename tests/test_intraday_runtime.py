"""Read-only preparation, evidence bindings and closed-gate CLI checks."""
from copy import deepcopy
from datetime import datetime
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from backend.experiment_store import ExperimentStore, encode
from backend.intraday_policy import canonical_hash, seal
from backend.intraday_runtime import (APPROVAL_ID, ARTIFACT_VERSION, create_service, economic_book,
    guard_snapshot, main, prepare, snapshot, validate_artifact, validate_receipt)
from backend.kis import KST
from backend.learning_policy import BASELINE_POLICY, policy_id, signals
from backend.policy_award import selection_contract
from backend.policy_capacity import validate_capacity_member
from tests.test_intraday_scenarios import fixture
from tests.test_learning_research import rule
from tests.test_policy_award import outcome
from tests.test_signal_hold_packet import packet_pool


class IntradayRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ExperimentStore(self.root / 'experiments.sqlite3')
        self.config = self.root / 'config.toml'
        self.config.write_text('', encoding='utf-8')
        (self.root / '.local').mkdir()
        (self.root / '.local' / 'timefolio-mirror-control.json').write_text(
            encode({'version': 1, 'desired_running': True, 'failures': 0, 'retry_at': None}), encoding='utf-8')
        self.now = datetime(2026, 10, 15, 12, 0, tzinfo=KST)
        self.frames, self.context = fixture()
        self.closing = self.context['source_award_context']
        self.parent_policy = rule()
        _, self.pool = packet_pool(self.parent_policy, self.frames)
        self.versions = {'daily': 'runtime-daily', 'learning': 'runtime-learning', 'analysis': 'runtime-analysis'}
        runtime = patch('backend.intraday_runtime.runtime_versions', return_value=self.versions)
        runtime.start()
        self.addCleanup(runtime.stop)
        limits = self.closing['limits']
        self.parent = {'policy': self.parent_policy, 'candidate_id': policy_id(self.parent_policy),
            'fingerprint': 'test-fingerprint', 'limits': limits, 'decision_id': 'source-decision',
            'signal_day': '2026-10-14', 'effective_day': '2026-10-15', 'run_id': 'source-run', 'revision': 1,
            'adopted_at': '2026-10-15T00:05:00+00:00'}
        frozen_input = {'as_of': '2026-10-14', 'rows': self.frames[-1]['rows'],
                        'observed_at': '2026-10-14T16:05:00+09:00'}
        original_signals = signals(self.parent_policy, self.frames[-1])
        original = {'id': 'source-run', 'as_of': '2026-10-14', 'created_at': '2026-10-14T16:30:00+09:00',
            'status': 'ready', 'input_hash': canonical_hash(frozen_input), 'version_id': 'old-analysis',
            'learning_policy': self.parent_policy, 'learning_engine_version': 'old-learning',
            'signals': original_signals, 'analysis': {'input': frozen_input,
                'input_hash': canonical_hash(frozen_input), 'version_id': 'old-analysis', 'decisions': original_signals}}
        self.source = {'prepared_run': original, 'signal_inputs': {'frame': self.frames[-1],
                       'baseline_signals': self.frames[-1]['baseline_signals']}}
        self.decision = {'id': 'source-decision', 'comparison_seed': {key: self.closing[key]
            for key in ('initial_state', 'execution_model', 'limits', 'signal_day')}}
        with self.store.connect() as db:
            db.executescript('''
                CREATE TABLE daily_policy_decisions (id TEXT PRIMARY KEY, fingerprint TEXT, day TEXT, payload TEXT);
                CREATE TABLE daily_policy_preparations (decision_id TEXT PRIMARY KEY, fingerprint TEXT, day TEXT, payload TEXT);
                CREATE TABLE learning_policies (id TEXT PRIMARY KEY,payload TEXT);
                CREATE TABLE mirror_runtime (id INTEGER PRIMARY KEY,payload TEXT);
            ''')
            db.execute('INSERT INTO daily_policy_decisions VALUES (?,?,?,?)',
                       ('source-decision', 'test-fingerprint', '2026-10-14', encode(self.decision)))
            db.execute('INSERT INTO daily_policy_preparations VALUES (?,?,?,?)',
                       ('source-decision', 'test-fingerprint', '2026-10-14', encode(self.source)))
            db.execute('INSERT INTO mirror_runtime VALUES (1,?)', (encode({'source_account': 'competition',
                'source_fingerprint': 'test-fingerprint', 'account_key': 'destination-key', 'contest': 'test',
                'baseline_order_ids': [], 'initialized_at': '2026-10-12T09:00:00+09:00', 'cursor': 8}),))
        self.store.add_run(original)
        protection = self.context['current_book']['account_protection']
        self.store.save_settings({'policy': {'fingerprint': 'test-fingerprint', 'account_id': 'competition',
            'execution_strategy': 'all-strategies-v1', **limits}, 'enabled': True, 'user_paused': False,
            'daily_policy:test-fingerprint': {'active': self.parent, 'revision': 1},
            'learning_account:test-fingerprint': {'risk': {**protection}, 'units': protection['units'],
                                                 'budget': limits['budget'], 'peak': protection['peak']}})
        request = {'champion': BASELINE_POLICY, 'frames': self.frames, 'limits': limits, 'award_context': self.closing}
        self.request_path = self.root / 'original-request.json'
        self.request_path.write_text(encode(request), encoding='utf-8')
        records = []
        for proposal in self.pool:
            result = outcome()
            result['metrics'].update(change_family='capacity', capacity_contract=validate_capacity_member(
                self.parent_policy, proposal, self.pool, limits=limits, frames=self.frames))
            records.append([proposal, policy_id(proposal['policy']), result])
        evaluation = {'records': records, 'selected_index': 0, 'audit': {'status': 'apply'}}
        self.artifact = seal({'version': ARTIFACT_VERSION, 'approval_id': APPROVAL_ID,
            'effective_day': '2026-10-15', 'signal_day': '2026-10-14', 'fingerprint': 'test-fingerprint',
            'created_at': '2026-10-15T11:50:00+09:00', 'expires_at': '2026-10-15T15:15:00+09:00',
            'source_decision_id': 'source-decision', 'source_decision_hash': canonical_hash(self.decision),
            'source_preparation_hash': canonical_hash(self.source), 'source_request_path': str(self.request_path),
            'source_request_hash': canonical_hash(request), 'closing_context': self.closing,
            'source_context_hash': self.closing['hash'], 'parent_active': self.parent,
            'parent_policy': self.parent_policy, 'parent_active_hash': canonical_hash(self.parent),
            'pool': self.pool, 'pool_hash': canonical_hash(self.pool), 'evaluation': evaluation,
            'evaluation_hash': canonical_hash(evaluation), 'selected_candidate_id': records[0][1],
            'runtime': self.versions, 'runtime_hash': canonical_hash(self.versions),
            'selection_contract': selection_contract(), 'intraday_context': self.context})
        self.artifact_path = self.root / 'selection.json'
        self.artifact_path.write_text(encode(self.artifact), encoding='utf-8')

    def verifier(self):
        # Native gate/rank replay has its own policy_award regression tests. This
        # fixture isolates the runtime's file/runtime/pool/source bindings.
        return patch('backend.policy_award.verify_award_record_evidence', return_value=(0, {'status': 'apply'}))

    def guard(self):
        with self.store.connect() as db:
            return guard_snapshot(db, self.config)

    def test_economic_projection_ignores_marks_and_age_but_preserves_exits_and_cash(self):
        _, context = fixture(held=True)
        original, current = context['source_award_context']['initial_state'], context['current_book']
        self.assertNotEqual(original, current)
        self.assertEqual(economic_book(original), economic_book(current))
        changed = deepcopy(current)
        changed['positions'][0]['exit_policy']['holding_sessions'] = 1
        self.assertNotEqual(economic_book(original), economic_book(changed))
        changed = deepcopy(current)
        changed['cash'] = str(float(changed['cash']) - 1)
        self.assertNotEqual(economic_book(original), economic_book(changed))

    def test_guard_binds_real_mirror_intent_and_protection_highwater_not_heartbeat(self):
        original = self.guard()
        self.assertEqual(original['economic_book'], economic_book(self.closing['initial_state']))
        with self.store.connect() as db:
            mirror = json.loads(db.execute('SELECT payload FROM mirror_runtime WHERE id=1').fetchone()[0])
            mirror.update(cursor=9, pid=1234, heartbeat='later')
            db.execute('UPDATE mirror_runtime SET payload=?', (encode(mirror),))
        self.assertEqual(original, self.guard())
        with self.store.connect() as db:
            mirror['account_key'] = 'changed-destination'
            db.execute('UPDATE mirror_runtime SET payload=?', (encode(mirror),))
        self.assertNotEqual(original['controls'], self.guard()['controls'])
        account = self.store.setting('learning_account:test-fingerprint')
        account['risk']['peak'] = '1.02'
        self.store.save_setting('learning_account:test-fingerprint', account)
        self.assertNotEqual(original['controls']['protection_revision'], self.guard()['controls']['protection_revision'])
        control = self.root / '.local' / 'timefolio-mirror-control.json'
        control.write_text(encode({'version': 1, 'desired_running': False, 'failures': 2}), encoding='utf-8')
        self.assertNotEqual(original['controls']['mirror_process_intent'], self.guard()['controls']['mirror_process_intent'])

    def test_actual_parent_is_separate_from_immutable_legacy_replay_request(self):
        before = self.request_path.read_bytes()
        with self.verifier() as verify:
            source, selected = validate_artifact(self.artifact, self.config)
            self.assertEqual(source['champion'], BASELINE_POLICY)
            self.assertEqual(selected, self.pool[0]['policy'])
            self.assertEqual(verify.call_args.args[0], self.parent_policy)
        self.assertEqual(before, self.request_path.read_bytes())

    def test_artifact_rejects_changed_runtime_pool_source_and_native_decision(self):
        mutations = [lambda a: a['runtime'].update(daily='different'),
                     lambda a: a['pool'].pop(), lambda a: a.update(source_request_hash='0'*64),
                     lambda a: a.update(selected_candidate_id='bad'),
                     lambda a: a['evaluation'].update(selected_index=16)]
        for mutation in mutations:
            value = deepcopy(self.artifact)
            mutation(value)
            with self.subTest(mutation=mutation), self.verifier(), self.assertRaises(ValueError):
                validate_artifact(seal(value), self.config)
        with patch('backend.policy_award.verify_award_record_evidence', side_effect=ValueError('rank mismatch')):
            with self.assertRaisesRegex(ValueError, 'rank mismatch'):
                validate_artifact(self.artifact, self.config)

    def test_prepare_is_read_only_and_publish_uses_new_timestamps_exact_inputs(self):
        with self.store.connect() as db:
            before = db.execute('SELECT * FROM settings ORDER BY key').fetchall()
            original = db.execute('SELECT * FROM runs').fetchall()
        with self.verifier():
            proposal = prepare(self.artifact_path, db_path=self.store.path, config_path=self.config, now=self.now)
            with self.store.connect() as db:
                self.assertEqual(before, db.execute('SELECT * FROM settings ORDER BY key').fetchall())
                self.assertEqual(original, db.execute('SELECT * FROM runs').fetchall())
            self.assertEqual(proposal['prepared_run']['analysis']['input'], self.source['prepared_run']['analysis']['input'])
            self.assertEqual(proposal['prepared_run']['created_at'], '2026-10-15T03:00:00+00:00')
            service = create_service(self.store, config_path=self.config, now=lambda: self.now)
            self.assertTrue(service.stage(proposal), service.last_error)
            self.assertTrue(service.activate(proposal['id'], broker_days=self.context['session_days']), service.last_error)
            self.assertEqual(service.resolve_run(today='2026-10-15', broker_days=self.context['session_days']), proposal['prepared_run'])
            self.assertEqual(service.resolve_analysis_run(), proposal['prepared_run'])
            self.assertEqual(snapshot(self.store.path)['active']['type'], 'intraday_override')

    def test_receipt_fails_closed_after_source_file_or_artifact_changes(self):
        with self.verifier():
            proposal = prepare(self.artifact_path, db_path=self.store.path, config_path=self.config, now=self.now)
            self.assertTrue(validate_receipt(proposal['evaluation_receipt'], self.config))
            source = json.loads(self.request_path.read_text(encoding='utf-8'))
            source['extra'] = 'changed'
            self.request_path.write_text(encode(source), encoding='utf-8')
            self.assertFalse(validate_receipt(proposal['evaluation_receipt'], self.config))

    def test_open_maintenance_gate_blocks_cli_before_creating_transition_tables(self):
        with redirect_stdout(io.StringIO()) as output:
            result = main(['--directory', str(self.root), '--config', str(self.config),
                           'stage', '--proposal', str(self.root / 'does-not-exist.json')])
        self.assertEqual(result, 1)
        self.assertIn('intraday_maintenance_must_be_closed', output.getvalue())
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='intraday_policy_transitions'").fetchone())

    def test_snapshot_does_not_create_missing_intraday_tables(self):
        self.assertEqual(snapshot(self.store.path)['staged'], [])
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='intraday_policy_transitions'").fetchone())

    def test_intraday_analysis_reuses_exact_run_without_reading_live_input_or_retrying_llm(self):
        from backend.experiments import ExperimentService, OperationalBlock
        expected = {'id': 'exact-approved-run'}
        active = {'type': 'intraday_override', 'effective_day': '2026-10-15'}
        reader = Mock(side_effect=RuntimeError('ordinary input reader reached'))
        resolver = Mock(return_value=expected)
        runner = SimpleNamespace(now=lambda: self.now,
            learning=SimpleNamespace(daily=SimpleNamespace(active=lambda: active)),
            intraday_policy=SimpleNamespace(resolve_analysis_run=resolver), input_reader=reader)
        self.assertEqual(ExperimentService._analyze(runner, retry_llm=True, automatic_retry=True), expected)
        reader.assert_not_called()
        resolver.return_value = None
        with self.assertRaises(OperationalBlock) as error:
            ExperimentService._analyze(runner)
        self.assertEqual(error.exception.code, 'intraday_policy')
        for current in (datetime(2026, 10, 15, 16, tzinfo=KST), datetime(2026, 10, 16, 10, tzinfo=KST)):
            self.now = current
            with self.assertRaisesRegex(RuntimeError, 'ordinary input reader reached'):
                ExperimentService._analyze(runner)


if __name__ == '__main__':
    unittest.main()
