"""Research receives whole-account contest evidence without widening execution."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.experiment_store import ExperimentStore
from backend.experiments import ExperimentService
from backend.learning_policy import BASELINE_POLICY
from tests.test_learning import LIMITS
from tests.test_learning_research import frames


class CompetitionResearchRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.service = ExperimentService.__new__(ExperimentService)
        self.service.store = ExperimentStore(Path(temporary.name) / 'experiments.sqlite3')
        self.policy = {**LIMITS, 'account_id': 'competition', 'fingerprint': 'bound-account'}
        self.service.store.save_settings({'policy': self.policy, 'enabled': False, 'user_paused': True})
        self.service._profile = Mock()
        self.service.now = lambda: datetime.fromisoformat('2026-10-13T16:30:00+09:00')
        self.service.learning = SimpleNamespace(state=lambda: {'fingerprint': 'bound-account'})
        self.progress = {'account': {'id': 'competition'}, 'status': 'ok', 'stale': False,
                         'coverage': 'complete', 'pace': {'status': 'behind'}}
        self.service.competition_reader = Mock(return_value=self.progress)

    def test_non_competition_account_never_reads_contest(self):
        self.service.store.save_setting('policy', {**self.policy, 'account_id': 'paper'})
        self.assertIsNone(self.service._competition_research_context())
        self.service.competition_reader.assert_not_called()

    def test_read_failure_or_account_change_is_unknown_not_zero(self):
        self.service.competition_reader.side_effect = RuntimeError('private-token')
        self.assertEqual(self.service._competition_research_context(), {'status': 'unavailable'})
        def changed():
            self.service.store.save_setting('policy', {**self.policy, 'fingerprint': 'changed'})
            return self.progress
        self.service.competition_reader.side_effect = changed
        self.assertEqual(self.service._competition_research_context(), {'status': 'unavailable'})

    def test_mismatched_profile_or_result_does_not_enter_research(self):
        self.service._profile.side_effect = ValueError('configuration mismatch')
        self.assertEqual(self.service._competition_research_context(), {'status': 'unavailable'})
        self.service.competition_reader.assert_not_called()
        self.service._profile.side_effect = None
        self.service.competition_reader.return_value = {'account': {'id': 'paper'}, 'status': 'ok'}
        self.assertEqual(self.service._competition_research_context(), {'status': 'unavailable'})

    def test_one_frozen_read_reaches_proposal_and_provenance_without_orders(self):
        received = {}
        def provenance(*args, **kwargs):
            received['provenance'] = deepcopy(kwargs['competition_context'])
            return {}
        def propose(*args, **kwargs):
            received['proposal'] = deepcopy(kwargs['competition_context'])
            return {'policy': BASELINE_POLICY}
        with patch('backend.learning_proposals.proposal_context', side_effect=provenance), \
                patch('backend.learning_research.propose_candidate', side_effect=propose):
            self.service._propose_learning(BASELINE_POLICY, frames(1), LIMITS, [])
        self.service.competition_reader.assert_called_once_with()
        self.assertEqual(received, {'provenance': self.progress, 'proposal': self.progress})
        self.assertEqual(self.service.store.setting('policy'), self.policy)
        self.assertFalse(self.service.store.setting('enabled'))
        self.assertTrue(self.service.store.setting('user_paused'))
        self.assertEqual(self.service.store.orders(), [])


if __name__ == '__main__':
    unittest.main()
