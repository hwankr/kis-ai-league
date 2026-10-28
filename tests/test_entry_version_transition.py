"""An engine transition may reprocess, but must not redate frozen observations."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import unittest
from unittest.mock import Mock

from backend.daily_inputs import freeze_inputs, frozen_data
from backend.experiment_analysis import build_snapshot
from backend.experiment_store import encode
from backend.experiments import OperationalBlock
from backend.kis import KST
from backend.learning_policy import build_frame, policy_id
from tests import test_learning_integration as fixtures


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class EntryVersionTransitionTests(unittest.TestCase):
    make_service = fixtures.LearningIntegrationTests.make_service
    completed_input = staticmethod(fixtures.LearningIntegrationTests.completed_input)
    baseline_analysis = fixtures.LearningIntegrationTests.baseline_analysis
    set_champion = fixtures.LearningIntegrationTests.set_champion

    def setUp(self):
        fixtures.LearningIntegrationTests.setUp(self)
        # The learning cohort also contains symbols outside the baseline screener.
        self.input_data['rows'][-1]['selection'] = {'status': 'not_selected'}
        self.current = datetime(2026, 10, 2, 17, tzinfo=KST)
        self.execution = fixtures.rules_policy(held_before_top_n=True)
        self.service.learning.daily.enabled = True
        self.active = {'revision': 3, 'policy': self.execution,
                       'candidate_id': policy_id(self.execution), 'effective_day': '2026-10-01'}
        self.daily_key = 'daily_policy:' + self.service._policy()['fingerprint']
        self.service.store.save_setting(self.daily_key, {'revision': 3, 'active': self.active, 'pending': None})
        def analyzed(**data):
            result = self.baseline_analysis(**data)
            result['input_hash'] = digest(result['input'])
            return result
        self.analyzer.side_effect = analyzed
        self.service._learn = Mock()
        first = self.service._analyze()
        self.original = self.service.store.runs(details=True)[0]
        self.baseline = {k: v for k, v in self.original.items() if k != 'analysis'}
        self.frame = build_frame(self.input_data, [s for s in first['signals'] if not s.get('policy_id')])
        self.frame.update(captured_at='2026-10-02T17:00:05+09:00', signal_inputs=freeze_inputs(self.input_data))
        with self.service.store.connect() as db:
            db.execute('INSERT INTO learning_frames VALUES (?,?,?)', (self.frame['as_of'],
                digest({k: v for k, v in self.frame.items() if k not in ('observed_at', 'captured_at')}), encode(self.frame)))
        self.current = datetime(2026, 10, 2, 17, 10, tzinfo=KST)
        self.input_data['observed_at'] = self.current.isoformat()
        self.service.learning.version = 'new-engine-for-transition-test'
        self.analyzer.reset_mock()

    def reuse(self, data=None):
        data = deepcopy(data or self.input_data)
        return self.service._reprocessed_baseline_input(self.baseline, data, build_snapshot(**data),
            self.execution, self.baseline['entry_selection'])

    def test_automatic_reprocessing_preserves_input_and_passes_unchanged_validator_after_restart(self):
        self.service.store.save_setting('enabled', True)
        self.service._cycle = Mock()
        self.service._market_open = lambda: False
        self.assertTrue(self.service._automatic())
        second = self.service.store.runs(details=True)[0]
        self.assertNotEqual(second['id'], self.original['id'])
        self.assertEqual(second['created_at'], self.current.astimezone(timezone.utc).isoformat(timespec='microseconds'))
        self.assertEqual(second['analysis']['input'], self.original['analysis']['input'])
        self.assertEqual(digest(second['analysis']['input']), second['input_hash'])
        self.assertEqual(second['learning_engine_version'], self.service.learning.version)
        self.analyzer.assert_not_called()
        self.service._automatic()
        self.assertEqual(len(self.service.store.runs()), 2)
        self.service.close()
        self.service = self.make_service()
        self.service.learning.daily.enabled = True
        self.service.learning.version = second['learning_engine_version']
        self.service._refresh_shadow = Mock()
        self.service._learn = Mock()
        self.current = datetime(2026, 10, 6, 8, tzinfo=KST)
        self.input_data['observed_at'] = self.current.isoformat()
        self.assertEqual(self.service._analyze()['id'], second['id'])
        self.current = datetime(2026, 10, 6, 9, 5, tzinfo=KST)
        original_data = frozen_data(self.frame['signal_inputs'], self.input_data)
        compact = {k: v for k, v in second.items() if k != 'analysis'}
        self.assertTrue(self.service._validate_frozen_entry_run(compact, original_data, self.frame,
            self.execution, session_days=['2026-10-01', '2026-10-02', '2026-10-06']))
        self.assertEqual(self.service.learning.daily.active(), self.active)
        self.assertEqual(self.service.store.orders(), [])
        with self.service.store.connect() as db:
            self.assertEqual(json.loads(db.execute('SELECT payload FROM learning_frames').fetchone()[0]), self.frame)
            self.assertEqual(db.execute('SELECT count(*) FROM daily_policy_decisions').fetchone()[0], 0)
        self.assertEqual(self.service.store.runs(details=True)[-1], self.original)

    def test_price_change_rejects_reuse(self):
        data = deepcopy(self.input_data)
        symbol = data['rows'][0]['symbol']
        data['histories'][symbol][max(data['histories'][symbol])]['close'] += 1
        with self.assertRaises(OperationalBlock):
            self.reuse(data)

    def test_eligibility_change_rejects_reuse(self):
        data = deepcopy(self.input_data)
        data['rows'][0]['learning_eligible'] = False
        with self.assertRaises(OperationalBlock):
            self.reuse(data)

    def test_active_policy_source_change_blocks_normal_reprocessing(self):
        self.input_data['rows'][0]['learning_eligible'] = False
        with self.assertRaises(OperationalBlock):
            self.service._analyze(automatic_retry=True)
        self.assertEqual(len(self.service.store.runs()), 1)
        self.assertEqual(self.service.store.runs(details=True)[0], self.original)
        self.analyzer.assert_not_called()

    def test_old_source_bar_change_rejects_even_if_normalized_input_is_unchanged(self):
        data = deepcopy(self.input_data)
        symbol = data['rows'][-1]['symbol']
        data['histories'][symbol][min(data['histories'][symbol])]['high'] += 1
        self.assertEqual(build_snapshot(**data), build_snapshot(**self.input_data))
        with self.assertRaises(OperationalBlock):
            self.reuse(data)

    def test_future_reprocessing_time_rejects_reuse(self):
        data = deepcopy(self.input_data)
        data['observed_at'] = '2026-10-03T17:00:00+09:00'
        with self.assertRaises(OperationalBlock):
            self.reuse(data)

    def test_original_observation_time_tampering_rejects_reuse(self):
        full = deepcopy(self.original)
        full['analysis']['input']['observed_at'] = '2026-10-02T17:00:01+09:00'
        self.replace_original(full)
        with self.assertRaises(OperationalBlock):
            self.reuse()

    def test_original_hash_tampering_rejects_reuse(self):
        full = deepcopy(self.original)
        full['analysis']['input_hash'] = 'changed'
        self.replace_original(full)
        with self.assertRaises(OperationalBlock):
            self.reuse()

    def test_missing_source_contract_rejects_reuse(self):
        frame = deepcopy(self.frame)
        frame['signal_inputs'].pop('source_hash')
        with self.service.store.connect() as db:
            db.execute('UPDATE learning_frames SET payload=?,digest=?', (encode(frame),
                digest({k: v for k, v in frame.items() if k not in ('observed_at', 'captured_at')})))
        with self.assertRaises(OperationalBlock):
            self.reuse()

    def test_changed_held_context_rejects_reuse(self):
        selected = deepcopy(self.baseline['entry_selection'])
        selected['context']['held_symbols'] = ['005930']
        with self.assertRaises(OperationalBlock):
            self.service._reprocessed_baseline_input(self.baseline, self.input_data,
                build_snapshot(**self.input_data), self.execution, selected)

    def replace_original(self, full):
        with self.service.store.connect() as db:
            db.execute('UPDATE runs SET payload=? WHERE id=?', (encode(full), full['id']))


if __name__ == '__main__':
    unittest.main()
