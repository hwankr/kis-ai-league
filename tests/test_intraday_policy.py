"""Offline one-shot transition, immutable provenance and transaction regression tests."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.daily_inputs import freeze_entry_selection
from backend.experiment_store import ExperimentStore, encode
from backend.intraday_policy import (IntradayPolicyService, RECEIPT_BINDINGS, RECEIPT_VERSION,
                                     VERSION, canonical_hash, seal)
from backend.kis import KST
from backend.learning_policy import policy_id, signals, validate_policy
from tests.test_learning_policy import policy, row


class IntradayPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ExperimentStore(Path(temporary.name) / 'test.sqlite3')
        self.current = datetime(2026, 10, 15, 12, 0, tzinfo=KST)
        self.days = ['2026-10-13', '2026-10-14', '2026-10-15']
        self.limits = {'budget': '100000000', 'order_cap': '4166666', 'daily_buy_limit': '16666666'}
        self.parent_policy = validate_policy(policy(name='기존 정책', max_positions=6))
        self.candidate = validate_policy(policy(name='선정 결과', held_before_top_n=True,
                                                no_same_day_reentry=True, allocation_mode='equal_eligible'))
        self.parent = {'policy': self.parent_policy, 'candidate_id': policy_id(self.parent_policy),
                       'fingerprint': 'test-account', 'limits': self.limits, 'decision_id': 'old-decision',
                       'signal_day': '2026-10-14', 'parent_id': 'older-candidate',
                       'effective_day': '2026-10-15', 'run_id': 'old-run', 'revision': 1,
                       'adopted_at': '2026-10-15T00:05:21+00:00'}
        self.initial_book = {'positions': [{'symbol': '005930', 'quantity': 4, 'cost': '280000',
                                           'exit_policy': None}], 'reserved_entries': []}
        self.frame = {'as_of': '2026-10-14', 'session_days': self.days[:-1],
                      'rows': [row('005930'), row('000660')], 'baseline_signals': []}
        original_input = {'as_of': '2026-10-14', 'rows': self.frame['rows'],
                          'observed_at': '2026-10-14T16:15:00+09:00'}
        old_signals = signals(self.parent_policy, self.frame)
        self.old_run = {'id': 'old-run', 'as_of': '2026-10-14', 'created_at': '2026-10-14T16:30:00+09:00',
                        'status': 'ready', 'input_hash': canonical_hash(original_input), 'version_id': 'old-analysis',
                        'collection_hash': 'old-collection', 'signals': old_signals,
                        'learning_policy': self.parent_policy, 'learning_engine_version': 'old-learning',
                        'daily_decision_id': 'old-decision', 'analysis': {'input': original_input,
                        'input_hash': canonical_hash(original_input), 'version_id': 'old-analysis',
                        'decisions': old_signals}}
        self.source = {'prepared_run': self.old_run, 'signal_inputs': {
            'frame': self.frame, 'baseline_signals': [], 'hash': canonical_hash(self.frame)}}
        self.decision = {'id': 'old-decision', 'decision': 'apply', 'activation': deepcopy(self.parent),
                         'original_context': self.initial_book}
        with self.store.connect() as db:
            db.executescript('''
                CREATE TABLE daily_policy_decisions (id TEXT PRIMARY KEY, fingerprint TEXT, day TEXT, payload TEXT);
                CREATE TABLE daily_policy_preparations (decision_id TEXT PRIMARY KEY, fingerprint TEXT, day TEXT, payload TEXT);
                CREATE TABLE learning_policies (id TEXT PRIMARY KEY, payload TEXT);
            ''')
            db.execute('INSERT INTO daily_policy_decisions VALUES (?,?,?,?)',
                       ('old-decision', 'test-account', '2026-10-14', encode(self.decision)))
            db.execute('INSERT INTO daily_policy_preparations VALUES (?,?,?,?)',
                       ('old-decision', 'test-account', '2026-10-14', encode(self.source)))
        self.store.add_run(self.old_run)
        self.store.reserve_order({'id': 'legacy-lot', 'created_at': '2026-10-12T09:10:00+09:00',
                                  'quantity': 4, 'exit_policy': None}, 'legacy-intent')
        self.store.save_settings({'policy': {'account_id': 'competition', 'fingerprint': 'test-account',
            'execution_strategy': 'all-strategies-v1', **self.limits}, 'enabled': True, 'user_paused': False,
            'mirror_intent': {'enabled': True, 'account_id': 'mirror'}, 'test_book': self.initial_book,
            'daily_policy:test-account': {'active': deepcopy(self.parent), 'revision': 1, 'status': 'active',
                                          'last_decision': {'id': 'old-decision', 'decision': 'apply'}}})
        self.service = self.make_service()
        self.proposal = self.make_proposal()

    def make_service(self, **changes):
        arguments = {'now': lambda: self.current, 'engine_version': 'new-daily',
                     'learning_engine_version': 'new-learning', 'analysis_version': 'new-analysis',
                     'approval_id': 'human-approval-20261015', 'effective_day': '2026-10-15',
                     'guard_reader': self.guard, 'evaluation_validator': lambda value: value == self.receipt}
        arguments.update(changes)
        return IntradayPolicyService(self.store, **arguments)

    def guard(self, db):
        read = self.service._read
        controls = {key: read(db, key) for key in ('policy', 'enabled', 'user_paused', 'mirror_intent')}
        controls.update(runtime_hash='exact-runtime', source_pool_hash='frozen-pool',
                        reservation_revision=read(db, 'test_reservation_revision', 0),
                        protection_revision=read(db, 'test_protection_revision', 0))
        return {'fingerprint': 'test-account', 'account_id': 'competition', 'controls': controls,
                'economic_book': read(db, 'test_book'), 'pending_orders': read(db, 'test_pending', []),
                'unresolved_orders': read(db, 'test_unresolved', []),
                'protected': read(db, 'test_protected', False), 'blocked': read(db, 'test_blocked', False)}

    def make_proposal(self):
        created = self.current.isoformat()
        expires = (self.current + timedelta(minutes=30)).isoformat()
        selection = freeze_entry_selection(self.candidate, self.initial_book)
        learned = signals(self.candidate, self.frame, entry_context=selection['context'])
        run = {**deepcopy(self.old_run), 'id': 'intraday-run', 'created_at': created,
               'signals': learned, 'learning_policy': self.candidate, 'learning_engine_version': 'new-learning',
               'version_id': 'new-analysis', 'collection_hash': 'new-collection',
               'intraday_transition_id': 'transition-20261015', 'entry_selection': selection}
        run.pop('daily_decision_id')
        run['analysis'].update(version_id='new-analysis', decisions=learned)
        with self.store.connect() as db:
            guard = self.guard(db)
        value = {'version': VERSION, 'id': 'transition-20261015', 'fingerprint': 'test-account',
            'account_id': 'competition', 'created_at': created, 'expires_at': expires,
            'effective_day': '2026-10-15', 'signal_day': '2026-10-14', 'parent_active': deepcopy(self.parent),
            'limits': self.limits, 'controls_hash': canonical_hash(guard['controls']),
            'economic_book_hash': canonical_hash(guard['economic_book']), 'source_decision_id': 'old-decision',
            'source_decision_hash': canonical_hash(self.decision), 'source_preparation_hash': canonical_hash(self.source),
            'closing_context_hash': canonical_hash(self.initial_book), 'policy': self.candidate,
            'candidate_id': policy_id(self.candidate), 'prepared_run': run, 'engine_version': 'new-daily',
            'learning_engine_version': 'new-learning', 'analysis_version': 'new-analysis',
            'authorization': {'id': 'human-approval-20261015', 'scope': 'intraday_policy_transition',
                'issued_at': created, 'expires_at': expires, 'parent_revision': 1}}
        self.receipt = seal({'version': RECEIPT_VERSION, 'decision': 'apply',
            'selected_candidate_id': value['candidate_id'], 'selection_hash': canonical_hash({'actual_result': 'apply'}),
            'parent_active_hash': canonical_hash(self.parent),
            'closing_economic_book_hash': value['economic_book_hash'], 'entry_selection': selection,
            **{key: value[key] for key in RECEIPT_BINDINGS}})
        return seal({**value, 'evaluation_receipt': self.receipt})

    def originals(self):
        with self.store.connect() as db:
            return {name: db.execute(query).fetchall() for name, query in {
                'decision': 'SELECT * FROM daily_policy_decisions',
                'preparation': 'SELECT * FROM daily_policy_preparations',
                'old_run': "SELECT * FROM runs WHERE id='old-run'",
                'orders': 'SELECT * FROM orders',
                'controls': "SELECT * FROM settings WHERE key NOT LIKE 'daily_policy:%' ORDER BY key"}.items()}

    def activate(self):
        self.assertTrue(self.service.stage(self.proposal), self.service.last_error)
        self.assertTrue(self.service.activate(self.proposal['id'], broker_days=self.days), self.service.last_error)

    def test_atomic_activation_preserves_originals_and_resolves_exact_run_after_restart(self):
        originals = self.originals()
        old_decision_summary = self.store.setting('daily_policy:test-account')['last_decision']
        self.assertTrue(self.service.stage(self.proposal), self.service.last_error)
        self.assertEqual(len(self.store.runs()), 1)
        self.assertEqual(self.store.setting('daily_policy:test-account')['active'], self.parent)
        self.current += timedelta(minutes=1)
        self.assertTrue(self.service.activate(self.proposal['id'], broker_days=self.days), self.service.last_error)
        self.assertEqual(self.originals(), originals)
        state = self.store.setting('daily_policy:test-account')
        self.assertEqual(state['revision'], 2)
        self.assertEqual(state['active']['run_id'], 'intraday-run')
        self.assertEqual(state['last_decision'], old_decision_summary)
        self.assertEqual(state['active']['adopted_at'], self.current.astimezone(timezone.utc).isoformat())
        self.assertNotIn('decision_id', state['active'])
        restored = self.make_service()
        self.assertEqual(restored.resolve_run(today='2026-10-15', broker_days=self.days), self.proposal['prepared_run'])
        self.assertIsNone(restored.last_error)
        self.assertEqual(restored.snapshot()['active']['revision'], 2)

    def test_stage_identical_retry_and_conflicting_payload_or_approval(self):
        self.assertTrue(self.service.stage(self.proposal))
        self.assertTrue(self.service.stage(deepcopy(self.proposal)))
        changed = deepcopy(self.proposal)
        changed['prepared_run']['id'] = 'other-run'
        self.assertFalse(self.service.stage(seal(changed)))
        self.assertEqual(self.service.last_error, 'intraday_transition_conflict')
        changed['id'] = 'other-transition'
        changed['prepared_run']['intraday_transition_id'] = changed['id']
        self.assertFalse(self.service.stage(seal(changed)))
        self.assertEqual(self.service.last_error, 'intraday_approval_already_used')

    def test_lost_activation_response_retry_does_not_republish(self):
        self.activate()
        revision = self.store.run_revision()
        self.assertFalse(self.service.activate(self.proposal['id'], broker_days=self.days))
        self.assertEqual(self.service.last_error, 'intraday_already_activated')
        self.assertEqual(self.store.run_revision(), revision)
        self.assertEqual(self.store.setting('daily_policy:test-account')['revision'], 2)

    def test_expiry_prevents_activation_but_does_not_expire_a_published_run(self):
        self.assertTrue(self.service.stage(self.proposal))
        self.current += timedelta(minutes=30)
        self.assertFalse(self.service.activate(self.proposal['id'], broker_days=self.days))
        self.assertEqual(self.store.setting('daily_policy:test-account')['revision'], 1)
        self.current -= timedelta(minutes=1)
        self.assertTrue(self.service.activate(self.proposal['id'], broker_days=self.days))
        self.current += timedelta(minutes=2)
        self.assertIsNotNone(self.service.resolve_run(today='2026-10-15', broker_days=self.days))

    def test_broker_prior_day_must_match_frozen_close_and_today_must_exist(self):
        self.assertTrue(self.service.stage(self.proposal))
        for days in ([], ['2026-10-14'], ['2026-10-13', '2026-10-15'], ['2026-10-14', 'invalid']):
            with self.subTest(days=days):
                self.assertFalse(self.service.activate(self.proposal['id'], broker_days=days))
                self.assertEqual(self.store.setting('daily_policy:test-account')['revision'], 1)

    def test_guard_cas_blocks_book_control_reservation_protection_and_parent_changes(self):
        self.assertTrue(self.service.stage(self.proposal))
        original_state = self.store.setting('daily_policy:test-account')
        cases = [('test_book', {'positions': []}), ('enabled', False), ('user_paused', True),
                 ('test_reservation_revision', 1), ('test_protection_revision', 1),
                 ('test_pending', ['pending']), ('test_unresolved', ['unknown']),
                 ('test_protected', True), ('test_blocked', True),
                 ('mirror_intent', {'enabled': False}),
                 ('daily_policy:test-account', {**original_state, 'revision': 2}),
                 ('daily_policy:test-account', {**original_state, 'pending': {'id': 'next-daily'}})]
        for key, value in cases:
            previous = self.store.setting(key)
            self.store.save_setting(key, value)
            with self.subTest(key=key, value=value):
                self.assertFalse(self.service.activate(self.proposal['id'], broker_days=self.days))
                self.assertEqual(len(self.store.runs()), 1)
            with self.store.connect() as db:
                if previous is None:
                    db.execute('DELETE FROM settings WHERE key=?', (key,))
                else:
                    db.execute('UPDATE settings SET value=? WHERE key=?', (encode(previous), key))
        self.assertTrue(self.service.activate(self.proposal['id'], broker_days=self.days), self.service.last_error)

    def test_account_limits_exact_versions_and_external_validator_are_required(self):
        self.assertTrue(self.service.stage(self.proposal))
        for key in ('engine_version', 'learning_engine_version', 'analysis_version'):
            with self.subTest(key=key):
                service = self.make_service(**{key: 'changed'})
                self.assertFalse(service.activate(self.proposal['id'], broker_days=self.days))
                self.assertEqual(service.last_error, 'intraday_engine_changed')
        for key, value in (('account_id', 'other'), ('order_cap', '5000000'), ('fingerprint', 'other')):
            original = self.store.setting('policy')
            self.store.save_setting('policy', {**original, key: value})
            self.assertFalse(self.service.activate(self.proposal['id'], broker_days=self.days))
            self.store.save_setting('policy', original)
        service = self.make_service(evaluation_validator=lambda _: False)
        self.assertFalse(service.activate(self.proposal['id'], broker_days=self.days))
        self.assertEqual(service.last_error, 'intraday_evaluation_unverified')

    def test_no_backdating_live_input_replacement_extra_signals_or_old_daily_id(self):
        changes = [('created_at', '2026-10-15T08:55:00+09:00'),
                   ('daily_decision_id', 'old-decision'), ('status', 'failed')]
        for key, value in changes:
            proposal = deepcopy(self.proposal)
            proposal['prepared_run'][key] = value
            self.assertFalse(self.service.stage(seal(proposal)))
        for edit in ('input', 'signals', 'entry_selection'):
            proposal = deepcopy(self.proposal)
            run = proposal['prepared_run']
            if edit == 'input':
                run['analysis']['input']['rows'][0]['close'] = 999
            elif edit == 'signals':
                run['signals'].append({'symbol': '123456', 'action': 'buy'})
                run['analysis']['decisions'] = run['signals']
            else:
                run.pop('entry_selection')
            with self.subTest(edit=edit):
                self.assertFalse(self.service.stage(seal(proposal)))
        self.assertEqual(len(self.store.runs()), 1)

    def test_approval_scope_id_day_hash_and_selection_are_bound(self):
        for key, value in (('scope', 'daily_activation'), ('id', 'unknown-approval'), ('parent_revision', 2)):
            proposal = deepcopy(self.proposal)
            proposal['authorization'][key] = value
            self.assertFalse(self.service.stage(seal(proposal)))
        proposal = deepcopy(self.proposal)
        proposal['policy']['max_positions'] = 19
        self.assertFalse(self.service.stage(seal(proposal)))
        proposal = deepcopy(self.proposal)
        proposal['evaluation_receipt']['decision'] = 'keep'
        proposal['evaluation_receipt'] = seal(proposal['evaluation_receipt'])
        self.assertFalse(self.service.stage(seal(proposal)))
        service = self.make_service(effective_day='2026-10-16')
        self.assertFalse(service.stage(self.proposal))
        proposal = deepcopy(self.proposal)
        proposal['hash'] = '0' * 64
        self.assertFalse(self.service.stage(proposal))

    def test_changed_source_original_blocks_publication(self):
        self.assertTrue(self.service.stage(self.proposal))
        with self.store.connect() as db:
            changed = {**self.decision, 'extra': 'changed'}
            db.execute('UPDATE daily_policy_decisions SET payload=?', (encode(changed),))
        self.assertFalse(self.service.activate(self.proposal['id'], broker_days=self.days))
        self.assertEqual(self.service.last_error, 'intraday_source_changed')
        self.assertEqual(len(self.store.runs()), 1)

    def test_index_failure_rolls_back_run_pointer_and_activation_together(self):
        self.assertTrue(self.service.stage(self.proposal))
        original_state = self.store.setting('daily_policy:test-account')
        with patch.object(self.store, '_index_run', side_effect=RuntimeError('disk failure')):
            with self.assertRaisesRegex(RuntimeError, 'disk failure'):
                self.service.activate(self.proposal['id'], broker_days=self.days)
        self.assertEqual(len(self.store.runs()), 1)
        self.assertEqual(self.store.setting('daily_policy:test-account'), original_state)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM intraday_policy_activations').fetchone()[0], 0)
        self.assertTrue(self.service.activate(self.proposal['id'], broker_days=self.days))

    def test_simultaneous_activation_has_exactly_one_winner(self):
        self.assertTrue(self.service.stage(self.proposal))
        services = [self.make_service(), self.make_service()]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda service: service.activate(self.proposal['id'], broker_days=self.days), services))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.store.runs()), 2)
        self.assertEqual(self.store.setting('daily_policy:test-account')['revision'], 2)

    def test_resolver_rejects_changed_run_index_pointer_or_future_session(self):
        self.activate()
        with self.store.connect() as db:
            original = db.execute("SELECT summary FROM run_index WHERE id='intraday-run'").fetchone()[0]
            db.execute("UPDATE run_index SET summary='{}' WHERE id='intraday-run'")
        self.assertIsNone(self.service.resolve_run(today='2026-10-15', broker_days=self.days))
        with self.store.connect() as db:
            db.execute("UPDATE run_index SET summary=? WHERE id='intraday-run'", (original,))
        self.current += timedelta(days=1)
        self.assertIsNone(self.service.resolve_run(today='2026-10-16', broker_days=self.days + ['2026-10-16']))
        self.current -= timedelta(days=1)
        state = self.store.setting('daily_policy:test-account')
        state['active']['run_id'] = 'old-run'
        self.store.save_setting('daily_policy:test-account', state)
        self.assertIsNone(self.service.resolve_run(today='2026-10-15', broker_days=self.days))

    def test_resolver_never_falls_back_to_unregistered_post_open_analysis(self):
        run = deepcopy(self.proposal['prepared_run'])
        run['id'] = 'unregistered-latest'
        self.store.add_run(run)
        self.assertIsNone(self.service.resolve_run(today='2026-10-15', broker_days=self.days))
        self.activate()
        self.assertEqual(self.service.resolve_run(today='2026-10-15', broker_days=self.days)['id'], 'intraday-run')


if __name__ == '__main__':
    unittest.main()
