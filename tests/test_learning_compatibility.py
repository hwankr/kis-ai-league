"""Execution exceptions never grant research or activation compatibility."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.learning_compatibility import (live_entry_version_matches,
                                            scorecard_version_matches, version_matches)


class CompatibilityTests(unittest.TestCase):
    def test_only_equal_versions_or_exact_direct_pair_are_compatible(self):
        with patch('backend.learning_compatibility.REPORTING_ONLY_UPGRADES', {('old', 'report'), ('report', 'next')}):
            self.assertTrue(version_matches('old', 'old'))
            self.assertTrue(version_matches('old', 'report'))
            self.assertFalse(version_matches('old', 'next'))
            self.assertFalse(version_matches('report', 'old'))
            self.assertFalse(version_matches('old', 'changed-evaluator'))
            self.assertFalse(version_matches(None, None))


class LiveEntryCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.run = {
            'id': 'frozen-run', 'as_of': '2026-10-21',
            'created_at': '2026-10-21T07:08:13+00:00', 'status': 'ready',
            'learning_engine_version': 'old-learning', 'version_id': 'analysis',
            'learning_policy': {'kind': 'rules', 'allocation': 4},
            'signals': [{'symbol': '000080', 'score': 3}],
            'entry_selection': {'context': {'held_symbols': ['006280']}},
            'input_hash': 'input', 'collection_hash': 'collection',
            'llm_status': {'status': 'ready'}, 'error': None,
        }
        digest = hashlib.sha256(json.dumps(self.run, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        self.entry = ('old-learning', 'new-observation', self.run['id'], digest)
        registration = patch('backend.learning_compatibility.LIVE_ENTRY_RUN_UPGRADES',
                             frozenset({self.entry}))
        registration.start()
        self.addCleanup(registration.stop)

    def test_same_engine_needs_no_execution_exception(self):
        with patch('backend.learning_compatibility.LIVE_ENTRY_RUN_UPGRADES', frozenset()):
            self.assertTrue(live_entry_version_matches(self.run, 'old-learning'))
            self.assertFalse(live_entry_version_matches(self.run, 'new-observation'))

    def test_exact_frozen_summary_is_accepted_without_mutation(self):
        original = deepcopy(self.run)
        self.assertTrue(live_entry_version_matches(self.run, 'new-observation'))
        self.assertEqual(self.run, original)
        self.assertTrue(live_entry_version_matches(dict(reversed(list(self.run.items()))),
                                                  'new-observation'))

    def test_every_summary_field_is_pinned(self):
        for key in self.run:
            with self.subTest(changed=key):
                changed = deepcopy(self.run)
                changed[key] = 'changed'
                self.assertFalse(live_entry_version_matches(changed, 'new-observation'))
            with self.subTest(missing=key):
                changed = deepcopy(self.run)
                del changed[key]
                self.assertFalse(live_entry_version_matches(changed, 'new-observation'))
        for key in ('signals', 'learning_policy', 'entry_selection'):
            with self.subTest(nested=key):
                changed = deepcopy(self.run)
                if key == 'signals':
                    changed[key][0]['score'] += 1
                elif key == 'learning_policy':
                    changed[key]['allocation'] += 1
                else:
                    changed[key]['context']['held_symbols'].append('007310')
                self.assertFalse(live_entry_version_matches(changed, 'new-observation'))
        self.assertFalse(live_entry_version_matches({**self.run, 'extra': True},
                                                  'new-observation'))

    def test_execution_registration_does_not_change_research_or_activation_checks(self):
        self.assertFalse(version_matches('old-learning', 'new-observation'))
        self.assertFalse(scorecard_version_matches('old-learning', 'new-observation'))
        self.assertFalse(live_entry_version_matches({**self.run, 'analysis': {}},
                                                  'new-observation'))
        with patch('backend.learning_compatibility.REPORTING_ONLY_UPGRADES',
                   {('old-learning', 'reporting-only')}):
            self.assertTrue(version_matches('old-learning', 'reporting-only'))
            self.assertFalse(live_entry_version_matches(self.run, 'reporting-only'))

    def test_no_reverse_transitive_or_future_engine_allowance(self):
        self.assertFalse(live_entry_version_matches(self.run, 'later-observation'))
        changed = {**self.run, 'learning_engine_version': 'new-observation'}
        self.assertFalse(live_entry_version_matches(changed, 'old-learning'))
        self.assertFalse(live_entry_version_matches(changed, 'later-observation'))

    def test_invalid_identity_or_non_json_summary_fails_closed(self):
        for run in (None, [], {}, {**self.run, 'learning_engine_version': None},
                    {**self.run, 'learning_engine_version': True},
                    {**self.run, 'id': None}, {**self.run, 'signals': float('nan')},
                    {**self.run, 'signals': object()}):
            with self.subTest(run_type=type(run).__name__):
                self.assertFalse(live_entry_version_matches(run, 'new-observation'))
        for current in (None, '', True):
            self.assertFalse(live_entry_version_matches(self.run, current))


class FrozenAnalysisReuseTests(unittest.TestCase):
    """Exercise real analysis orchestration and indexing using only a temp DB."""
    def setUp(self):
        from backend.experiment_analysis import build_snapshot
        from backend.experiment_store import ExperimentStore
        from backend.experiments import ExperimentService

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = datetime(2026, 10, 21, 17, 30, tzinfo=timezone(timedelta(hours=9)))
        self.data = {
            'as_of': '2026-10-21', 'observed_at': self.now.isoformat(),
            'rows': [{'symbol': '005930', 'name': 'test', 'board': 'KOSPI',
                      'status': 'ok', 'as_of': '2026-10-21', 'learning_eligible': True,
                      'selection': {'status': 'selected'}}],
            'histories': {'005930': {'2026-10-21': {
                'open': 1000, 'high': 1002, 'low': 999, 'close': 1001,
                'volume': 1000, 'turnover': 1001000}}},
            'calendars': {'KOSPI': ['2026-10-21']}, 'benchmarks': {},
        }
        self.policy = {
            'kind': 'rules', 'name': 'frozen selection', 'cash_reserve': 0.,
            'max_positions': 20, 'entry_slippage_bps': 100, 'cancel_after_minutes': 10,
            'held_before_top_n': True,
            'strategies': [{'id': 'momentum', 'name': 'momentum', 'weight': 1.,
                'conditions': [], 'rank_by': 'turnover', 'descending': True, 'top_n': 1,
                'holding_sessions': 5, 'stop_loss_pct': 0., 'take_profit_pct': 0.}],
        }
        # Avoid constructor/service startup entirely. Only _analyze and the real
        # temporary store execute; no app, broker or research worker can start.
        service = self.service = ExperimentService.__new__(ExperimentService)
        service.store = ExperimentStore(self.root / 'experiments.sqlite3')
        service.config_path = self.root / 'unused.toml'
        service.now = lambda: self.now
        service.learning = SimpleNamespace(version='old-learning',
                                           daily=SimpleNamespace(active=lambda: None))
        service.input_reader = lambda: deepcopy(self.data)
        service._adopted_policy = lambda: deepcopy(self.policy)
        service._entry_selection_book = lambda: {'positions': [], 'reserved_entries': []}
        service._timestamp = lambda: self.now.isoformat()
        service._get_llm = Mock(return_value=None)
        service._retry_due = Mock(return_value=True)
        for method in ('_refresh_shadow', '_learn', '_prepare_daily', '_event', '_llm_retry_state'):
            setattr(service, method, Mock())
        service.analyzer = Mock(side_effect=lambda **data: {
            'decisions': [], 'input_hash': 'fixture-input', 'version_id': 'analysis-v1',
            'llm_status': {'status': 'ready'},
            'input': build_snapshot(**{k: v for k, v in data.items() if k != 'llm'}),
        })
        version = patch('backend.experiment_analysis.analysis_version', return_value='analysis-v1')
        version.start()
        self.addCleanup(version.stop)
        self.original = service._analyze()
        self.summary = service.store.runs(details=False)[0]
        self.original_payload = self.raw_original()
        digest = hashlib.sha256(json.dumps(self.summary, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        registration = patch('backend.learning_compatibility.LIVE_ENTRY_RUN_UPGRADES',
            frozenset({('old-learning', 'new-observation', self.original['id'], digest)}))
        registration.start()
        self.addCleanup(registration.stop)
        service.learning.version = 'new-observation'
        for method in ('_refresh_shadow', '_learn', '_prepare_daily'):
            getattr(service, method).reset_mock()

    def raw_original(self):
        with self.service.store.connect() as db:
            return db.execute('SELECT payload FROM runs WHERE id=?',
                              (self.original['id'],)).fetchone()[0]

    def assert_new_analysis_preserves_original(self):
        result = self.service._analyze(automatic_retry=True)
        self.assertNotEqual(result['id'], self.original['id'])
        self.assertEqual(result['learning_engine_version'], 'new-observation')
        self.assertEqual(len(self.service.store.runs(details=False)), 2)
        self.assertEqual(self.raw_original(), self.original_payload)

    def test_automatic_same_frozen_input_reuses_original_and_keeps_research_hooks(self):
        self.data['observed_at'] = (self.now + timedelta(minutes=1)).isoformat()
        result = self.service._analyze(automatic_retry=True)
        self.assertEqual(result, self.summary)
        self.assertEqual(len(self.service.store.runs(details=False)), 1)
        self.service.analyzer.assert_called_once()
        self.service._learn.assert_called_once_with(self.data, self.summary)
        self.service._prepare_daily.assert_called_once_with(self.data, self.summary)
        self.assertEqual(self.raw_original(), self.original_payload)
        self.assertEqual(self.service.store.setting('analysis_attempt')['run_id'], self.original['id'])

    def test_normal_automatic_startup_reuses_run_after_reconciliation_cycle(self):
        service = self.service
        service.stopping = False
        service.learning.enabled = True
        service.learning.daily.pending = lambda: None
        service.candidate_service = SimpleNamespace(snapshot=lambda: {
            'status': 'complete', 'stale': False})
        service.store.save_setting('enabled', True)
        service._enforce_deadline = Mock()
        service._initialize_policy = Mock(return_value={'account_id': 'paper'})
        service._operation_failures = Mock(return_value={})
        service._cycle = Mock()
        service._market_open = Mock(return_value=False)
        service._success = Mock()
        self.assertTrue(service._automatic())
        service._cycle.assert_called_once()
        service._learn.assert_called_once_with(self.data, self.summary)
        self.assertEqual(len(service.store.runs(details=False)), 1)
        service.analyzer.assert_called_once()
        self.assertEqual(self.raw_original(), self.original_payload)

    def test_changed_market_input_is_not_hidden_by_execution_exception(self):
        self.data['histories']['005930']['2026-10-21']['volume'] += 1
        self.assert_new_analysis_preserves_original()

    def test_changed_learning_frame_cannot_reuse_same_analysis_input_key(self):
        from backend.experiment_analysis import build_snapshot
        before = build_snapshot(**self.data)
        self.data['rows'][0]['learning_eligible'] = False
        self.assertEqual(build_snapshot(**self.data), before)
        self.assert_new_analysis_preserves_original()

    def test_new_day_is_not_hidden_by_execution_exception(self):
        self.now += timedelta(days=1)
        self.data['as_of'] = '2026-10-22'
        self.data['observed_at'] = self.now.isoformat()
        self.data['rows'][0]['as_of'] = '2026-10-22'
        self.data['histories']['005930']['2026-10-22'] = deepcopy(
            self.data['histories']['005930']['2026-10-21'])
        self.data['calendars']['KOSPI'].append('2026-10-22')
        self.assert_new_analysis_preserves_original()

    def test_new_policy_is_not_hidden_by_execution_exception(self):
        self.policy['strategies'][0]['top_n'] = 2
        self.assert_new_analysis_preserves_original()

    def test_reuse_still_requires_original_entry_selection_validation(self):
        from backend.experiments import OperationalBlock
        with patch('backend.daily_inputs.validate_entry_selection', return_value=False):
            with self.assertRaises(OperationalBlock):
                self.service._analyze(automatic_retry=True)
        self.assertEqual(len(self.service.store.runs(details=False)), 1)
        self.assertEqual(self.raw_original(), self.original_payload)


if __name__ == '__main__':
    unittest.main()
