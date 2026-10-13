"""Checkout conversions must not change evaluation identity or widen compatibility."""
from pathlib import Path
import tempfile
import unittest

from backend.learning_compatibility import scorecard_version_matches, version_matches
from backend.source_version import DAILY_SOURCES, LEARNING_SOURCES, source_version


LEGACY_SCORECARDS = (
    '3c4aab6471f3d2453474a289cb7cb8c74f8cbf015d7f97d2f872639ef5f94655',
    'a9c9b6f1994138a3daac25d432fca3cd5f14e68ad17778117abd38635004b9d4',
    'd98ff33aff3c5882e7ef3d433e6836153516364b30e9e229d3866ce23f7b78cf',
    '51703606e3d768336d3f770c8fb1acc5126945d1ae80ef9c323523487a00cede',
    '10b9e8644d4116a5827dc556727ef39ded67e77b2c67ab713721208f016f8bcb',
)


class SourceVersionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        backend = Path(__file__).resolve().parents[1] / 'backend'
        self.sources = {name: (backend / name).read_bytes().replace(b'\r\n', b'\n')
                        for name in set(LEARNING_SOURCES + DAILY_SOURCES)}
        self.write_sources('lf')

    def write_sources(self, endings):
        for name, data in self.sources.items():
            if endings == 'crlf':
                data = data.replace(b'\n', b'\r\n')
            elif endings == 'mixed':
                lines = data.splitlines(keepends=True)
                data = b''.join(line.replace(b'\n', b'\r\n') if i % 2 else line
                                for i, line in enumerate(lines))
            (self.root / name).write_bytes(data)

    def test_learning_and_daily_identities_survive_lf_crlf_and_mixed_checkouts(self):
        for names in (LEARNING_SOURCES, DAILY_SOURCES):
            expected = source_version(names)
            for endings in ('lf', 'crlf', 'mixed'):
                with self.subTest(engine=names[0], endings=endings):
                    self.write_sources(endings)
                    actual = source_version(names, root=self.root)
                    self.assertEqual(actual, expected)
                    self.assertTrue(version_matches(expected, actual))

    def test_reviewed_scorecard_migrations_do_not_validate_old_trials_or_runs(self):
        current = source_version(LEARNING_SOURCES)
        reviewed = 'py-lf-v1:4d07d05ba03cc65e975844877bd5b2a7596d70f337cf4e9ccacaf7626aa8db03'
        for old in LEGACY_SCORECARDS:
            with self.subTest(old=old):
                self.assertTrue(scorecard_version_matches(old, reviewed))
                self.assertFalse(version_matches(old, reviewed))
                # Allocation, exits and inherited risk are now materially different.
                self.assertFalse(scorecard_version_matches(old, current))
                self.assertFalse(version_matches(old, current))
                self.assertFalse(scorecard_version_matches(current, old))
        self.assertFalse(scorecard_version_matches(reviewed, current))
        self.assertFalse(scorecard_version_matches('unreviewed', current))

    def test_cost_and_comparison_changes_reject_old_and_current_versions(self):
        changes = (
            ('learning_policy.py', b'0.000140527', b'0.0002'),
            ('learning_evaluation.py', b'CAMPAIGN_ALPHA = .10', b'CAMPAIGN_ALPHA = .20'),
        )
        for name, old_text, replacement in changes:
            with self.subTest(file=name):
                self.write_sources('lf')
                self.assertIn(old_text, self.sources[name])
                (self.root / name).write_bytes(self.sources[name].replace(old_text, replacement, 1))
                for names in (LEARNING_SOURCES, DAILY_SOURCES):
                    current = source_version(names)
                    changed = source_version(names, root=self.root)
                    self.assertNotEqual(current, changed)
                    self.assertFalse(version_matches(current, changed))
                    self.assertFalse(scorecard_version_matches(current, changed))
                    for old in LEGACY_SCORECARDS:
                        self.assertFalse(scorecard_version_matches(old, changed))

    def test_source_bytes_other_than_physical_crlf_remain_significant(self):
        path = self.root / 'literal.py'
        names = ('literal.py',)
        path.write_bytes(b'text = "\\r\\n"\n')
        original = source_version(names, root=self.root)
        path.write_bytes(b'text = "\\r\\n"\r\n')
        self.assertEqual(source_version(names, root=self.root), original)
        path.write_bytes(b'text = "\\n"\n')
        self.assertNotEqual(source_version(names, root=self.root), original)


if __name__ == '__main__':
    unittest.main()
