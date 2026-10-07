"""Frozen, allocation-free research hypotheses shared by history and observation."""
from decimal import Decimal, InvalidOperation

RULE_ID = "pullback-recovery-v1"
MINIMUM_SESSIONS = 63
RULE_SPEC = {
    "id": RULE_ID,
    "trend": "C[D-3] > SMA20[D-3] > SMA60[D-3] and C[D] > SMA60[D]",
    "pullback": "C[D-1] < C[D-2] < C[D-3]",
    "recovery": "C[D] > H[D-1]",
    "entry": "next session open (hypothetical)",
    "exit": "open after five holding sessions; entry session counts as one",
    "order_enabled": False,
}


def evaluate_pullback(bars, calendar):
    """Evaluate only the last supplied completed session; never fill missing bars.

    Calendar is the exchange-session sequence; bars are date-keyed OHLCV/turnover.
    Callers own the as-of boundary and universe/eligibility selection.
    """
    invalid = {"eligible": False, "signal": False, "trend": False, "reason": "insufficient_history"}
    if len(calendar) < MINIMUM_SESSIONS:
        return invalid
    days = list(calendar)[-MINIMUM_SESSIONS:]
    if len(set(days)) != len(days) or days != sorted(days):
        return {**invalid, "reason": "invalid_calendar"}
    values = []
    for day in days:
        try:
            row = {field: Decimal(str(bars[day][field]))
                   for field in ("open", "high", "low", "close", "volume", "turnover")}
            if (not all(value.is_finite() for value in row.values())
                    or min(row[field] for field in ("open", "high", "low", "close")) <= 0
                    or row["low"] > min(row["open"], row["close"])
                    or row["high"] < max(row["open"], row["close"])
                    or row["volume"] < 0 or row["turnover"] < 0):
                raise ValueError()
            values.append(row)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return {**invalid, "reason": "invalid_or_missing_bar"}
    closes = [row["close"] for row in values]
    # Compare sums to avoid rounding an average at a strict boundary.
    trend = (closes[-4] * 20 > sum(closes[-23:-3])
             and sum(closes[-23:-3]) * 3 > sum(closes[:-3])
             and closes[-1] * 60 > sum(closes[-60:]))
    pullback = closes[-2] < closes[-3] < closes[-4]
    recovery = closes[-1] > values[-2]["high"]
    signal = trend and pullback and recovery
    return {"eligible": True, "trend": trend, "signal": signal,
            "reason": "signal" if signal else "no_trend" if not trend else
                      "no_pullback" if not pullback else "no_recovery"}
