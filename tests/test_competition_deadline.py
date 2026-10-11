from datetime import datetime, timedelta, timezone
import unittest

from backend.competition_deadline import KIS_LAST_DAY, TIMEFOLIO_LAST_DAY, ended
from backend.kis import KST


class CompetitionDeadlineTests(unittest.TestCase):
    def test_last_date_is_inclusive_and_next_midnight_is_blocked_in_any_timezone(self):
        for last_day in (KIS_LAST_DAY, TIMEFOLIO_LAST_DAY):
            cutoff = datetime.combine(last_day + timedelta(days=1), datetime.min.time(), KST)
            for zone in (KST, timezone.utc, timezone(timedelta(hours=-7))):
                with self.subTest(last_day=last_day, timezone=zone):
                    self.assertFalse(ended((cutoff - timedelta(microseconds=1)).astimezone(zone), last_day))
                    self.assertTrue(ended(cutoff.astimezone(zone), last_day))
                    self.assertTrue(ended((cutoff + timedelta(days=3)).astimezone(zone), last_day))

    def test_ambiguous_clock_is_rejected(self):
        with self.assertRaises(ValueError):
            ended(datetime(2026, 11, 20), KIS_LAST_DAY)
