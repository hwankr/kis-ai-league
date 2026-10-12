"""Offline durability and attribution of unselected LLM proposals."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.experiment_store import ExperimentStore
from backend.experiments import ExperimentService
from backend.learning_policy import BASELINE_POLICY, policy_id
from backend.learning_proposals import proposal_context, save_llm_proposal
from tests.test_learning import LIMITS, candidate
from tests.test_learning_research import frames


class ProposalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ExperimentStore(Path(temporary.name) / 'experiments.sqlite3')
        self.state = {'fingerprint': 'research-account', 'engine_version': 'frozen-engine'}
        self.at = '2026-10-12T07:30:00+00:00'
        self.context = proposal_context(BASELINE_POLICY, frames(1), LIMITS, [],
                                        self.state, None, self.at)

    def rows(self, store=None):
        with (store or self.store).connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM learning_proposals')]

    def test_restart_and_repeat_preserve_first_observation_without_trial_or_permission_change(self):
        self.store.save_settings({'enabled': False, 'user_paused': True, 'learning': self.state})
        save_llm_proposal(self.store, candidate(), context=self.context, captured_at=self.at)
        restarted = ExperimentStore(self.store.path)
        save_llm_proposal(restarted, candidate(), context=self.context,
                          captured_at='2026-10-12T07:31:00+00:00')
        rows = self.rows(restarted)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['captured_at'], self.at)
        self.assertEqual(rows[0]['role'], 'development_only')
        self.assertEqual(rows[0]['representation'], 'validated_normalized_policy')
        self.assertFalse(restarted.setting('enabled'))
        self.assertTrue(restarted.setting('user_paused'))
        self.assertEqual(restarted.setting('learning'), self.state)

    def test_distinct_research_accounts_do_not_deduplicate_each_other(self):
        save_llm_proposal(self.store, candidate(), context=self.context, captured_at=self.at)
        changed = {**self.context, 'fingerprint': 'other-research-account'}
        save_llm_proposal(self.store, candidate(), context=changed, captured_at=self.at)
        self.assertEqual(len(self.rows()), 2)

    def test_service_freezes_research_binding_before_provider_changes_current_settings(self):
        self.store.save_setting('learning', self.state)
        service = ExperimentService.__new__(ExperimentService)
        service.store = self.store
        service.now = lambda: datetime.fromisoformat(self.at)
        service.learning = SimpleNamespace(state=lambda: self.store.setting('learning'))
        policy = candidate()
        store = self.store

        class Provider:
            def public_config(self):
                return {'provider': 'codex', 'model': 'test-model', 'api_key': 'private-key',
                        'endpoint': 'private-endpoint'}

            def generate(self, prompt, payload, schema):
                store.save_setting('learning', {'fingerprint': 'changed', 'engine_version': 'changed'})
                return deepcopy(policy)

        observations = frames(1)
        result = service._propose_learning(BASELINE_POLICY, observations, LIMITS, [], llm=Provider())
        row = self.rows()[0]
        self.assertEqual(result['method'], 'llm_bootstrap')
        self.assertEqual(row['candidate_id'], policy_id(result['policy']))
        self.assertEqual(row['fingerprint'], self.state['fingerprint'])
        self.assertEqual(row['engine_version'], self.state['engine_version'])
        self.assertEqual(row['as_of'], observations[-1]['as_of'])
        self.assertEqual(row['provider'], {'provider': 'codex', 'model': 'test-model'})
        self.assertNotIn('private-', json.dumps(row))

    def test_optional_provider_metadata_does_not_disable_research(self):
        class BrokenMetadata:
            def public_config(self):
                raise ValueError('metadata unavailable')

        for provider in (object(), BrokenMetadata()):
            with self.subTest(provider=type(provider).__name__):
                context = proposal_context(BASELINE_POLICY, frames(1), LIMITS, [],
                                           self.state, provider, self.at)
                self.assertEqual(context['provider'], {})

    def test_service_wires_durable_proposer_and_propagates_storage_failure(self):
        config = self.store.path.parent / 'config.toml'
        config.write_text('', encoding='utf-8')
        service = ExperimentService(config, directory=self.store.path.parent,
                                    now=lambda: datetime.fromisoformat(self.at))
        self.addCleanup(service.close)
        self.assertEqual(service.learning.proposer, service._propose_learning)

        class Provider:
            def public_config(self):
                return {'provider': 'codex'}

            def generate(self, *args):
                return candidate()

        with patch('backend.learning_proposals.save_llm_proposal', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                service._propose_learning(BASELINE_POLICY, frames(1), LIMITS, [], llm=Provider())


if __name__ == '__main__':
    unittest.main()
