"""Read-only contest turnover arithmetic; weekdays are an estimate, not a KRX calendar."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from backend.kis import KST


MONEY_FIELDS = (
    "gross_amount", "remaining_gross_amount", "expected_gross_amount", "shortfall_gross_amount",
    "required_daily_gross_amount", "next_5_required_gross_amount", "recent_5_gross_amount",
    "projected_final_gross_amount",
)


def _money(value, *, ceiling=False):
    return format(value.quantize(Decimal("1"), rounding=ROUND_CEILING if ceiling else ROUND_HALF_UP), "f")


def calculate_turnover_pacing(*, now, start_date, end_date, daily_amounts, reliable,
                              gross_target="500000000", source_updated_at=None):
    """Count zero-trade weekdays and exclude the unfinished day from observed pace.

    The 16:00 cutoff follows the contest's possible after-close fills. Required
    amounts include all known fills; recent totals and the constant-pace scenario
    use completed weekdays only. No result here changes orders or risk limits.
    """
    if now.utcoffset() is None:
        raise ValueError("competition_clock_requires_timezone")
    current = now.astimezone(KST)
    today = current.date()
    start = date.fromisoformat(start_date) if isinstance(start_date, str) else start_date
    end = date.fromisoformat(end_date) if isinstance(end_date, str) else end_date
    target = Decimal(str(gross_target))
    if start > end or not target.is_finite() or target <= 0:
        raise ValueError("invalid_competition_pacing_period_or_target")
    days = [start + timedelta(days=index) for index in range((end - start).days + 1)
            if (start + timedelta(days=index)).weekday() < 5]
    if not days:
        raise ValueError("competition_pacing_requires_weekdays")
    completed = [day for day in days if day < today or day == today and current.time() >= time(16)]
    remaining = len(days) - len(completed)
    recent = completed[-5:]
    result = {"calendar_basis": "weekday_estimate", "cutoff": "16:00_KST",
              "status": "unavailable", "as_of": current.isoformat(),
              "total_sessions": len(days), "completed_sessions": len(completed),
              "remaining_sessions": remaining, "next_5_sessions": min(5, remaining),
              "recent_5_sessions": len(recent), "target_gross_amount": _money(target),
              **dict.fromkeys(MONEY_FIELDS)}
    # A successful intraday query cannot prove a completed session's final total.
    if source_updated_at is not None:
        observed = source_updated_at
        if isinstance(observed, str):
            try:
                observed = datetime.fromisoformat(observed)
            except ValueError:
                observed = None
        if (not isinstance(observed, datetime) or observed.utcoffset() is None or observed > now
                or completed and observed < datetime.combine(completed[-1], time(16), KST)):
            reliable = False
    if not reliable:
        return result
    amounts = {}
    for raw_day, raw_amount in daily_amounts.items():
        day = date.fromisoformat(raw_day) if isinstance(raw_day, str) else raw_day
        if not start <= day <= min(today, end):
            continue
        amount = Decimal(str(raw_amount))
        if not amount.is_finite() or amount < 0:
            raise ValueError("invalid_competition_pacing_amount")
        amounts[day] = amounts.get(day, Decimal(0)) + amount
    gross = sum(amounts.values(), Decimal(0))
    closed_gross = sum((amounts.get(day, Decimal(0)) for day in completed), Decimal(0))
    outstanding = max(Decimal(0), target - gross)
    expected = target * len(completed) / len(days)
    shortfall = max(Decimal(0), expected - gross)
    daily = outstanding / remaining if remaining else None
    result.update(
        status="not_started" if today < start else "met" if outstanding == 0 else
               "ended_shortfall" if remaining == 0 else "behind" if shortfall > 0 else "on_track",
        gross_amount=_money(gross), remaining_gross_amount=_money(outstanding, ceiling=True),
        expected_gross_amount=_money(expected), shortfall_gross_amount=_money(shortfall, ceiling=True),
        required_daily_gross_amount=_money(daily, ceiling=True) if daily is not None else None,
        next_5_required_gross_amount=_money(daily * min(5, remaining), ceiling=True) if daily is not None else None,
        recent_5_gross_amount=_money(sum((amounts.get(day, Decimal(0)) for day in recent), Decimal(0))),
        projected_final_gross_amount=_money(closed_gross / len(completed) * len(days)) if completed else None,
    )
    return result
