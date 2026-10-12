"""Reporting migrations never grant general or transitive evaluator compatibility."""
import unittest
from unittest.mock import patch

from backend.learning_compatibility import version_matches


class CompatibilityTests(unittest.TestCase):
    def test_only_equal_versions_or_exact_direct_pair_are_compatible(self):
        with patch('backend.learning_compatibility.REPORTING_ONLY_UPGRADES', {('old', 'report'), ('report', 'next')}):
            self.assertTrue(version_matches('old', 'old'))
            self.assertTrue(version_matches('old', 'report'))
            self.assertFalse(version_matches('old', 'next'))
            self.assertFalse(version_matches('report', 'old'))
            self.assertFalse(version_matches('old', 'changed-evaluator'))
            self.assertFalse(version_matches(None, None))


if __name__ == '__main__':
    unittest.main()
