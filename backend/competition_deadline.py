"""Inclusive contest dates from docs/competition-rules.md (Korea time)."""
from datetime import date

from backend.kis import KST


KIS_LAST_DAY = date(2026, 11, 20)
TIMEFOLIO_LAST_DAY = date(2026, 11, 30)
KIS_ENDED_MESSAGE = "KIS 대회가 종료되어 새 주문을 중지했습니다."


def ended(now, last_day):
    # The rules specify dates, not an intraday closing time. The final date
    # remains inclusive; existing session-hour guards still apply that day.
    if now.utcoffset() is None:
        raise ValueError("competition_clock_requires_timezone")
    return now.astimezone(KST).date() > last_day
