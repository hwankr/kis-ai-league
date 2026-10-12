"""Pure development comparison for a provisional daily paper-policy change.

This is a selection rule on reused historical data, not a significance test or
proof of improvement. Relative loss reduction may qualify; positive returns and
completed trades are not required. No account limits or observations are changed.
"""
from copy import deepcopy
from datetime import date, datetime, time
from decimal import Decimal
import math

from backend.learning_evaluation import _return_and_drawdown, _series
from backend.learning_policy import KST, LEGACY_IDS, _day, _instant, portfolio_metrics, validate_policy
from backend.learning_research import _behavior_id, _policy_changes


VERSION = "daily-development-v1"
EPSILON = 1e-9
METRICS = ("net_return_pct", "stress_return_pct", "max_drawdown_pct", "stress_drawdown_pct")


def _finite(value):
    if isinstance(value, (float, Decimal)) and not math.isfinite(value):
        raise ValueError("nonfinite_input")
    if isinstance(value, dict):
        for item in value.values():
            _finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _finite(item)


def _metrics(raw, dates, recent):
    _finite(raw)
    normal, stress, actual_dates = _series(raw)
    if actual_dates != dates or len(normal) != len(dates) - 1:
        raise ValueError("unpaired_evaluation_dates")
    for returns, key in ((normal, "equity_curve"), (stress, "stress_equity_curve")):
        equities = [item.get("equity") for item in raw[key]]
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in equities):
            raise ValueError("invalid_equity_curve")
        if any(not math.isclose(math.log(after / before), change, rel_tol=1e-7, abs_tol=1e-10)
               for before, after, change in zip(equities, equities[1:], returns)):
            raise ValueError("inconsistent_evaluation_returns")
    net, drawdown = _return_and_drawdown(normal)
    stressed, stress_drawdown = _return_and_drawdown(stress)
    result = dict(zip(METRICS, (net, stressed, drawdown, stress_drawdown)))
    for key, value in result.items():
        if key in raw and (type(raw[key]) not in (int, float) or
                           not math.isclose(raw[key], value, rel_tol=1e-7, abs_tol=1e-7)):
            raise ValueError("inconsistent_evaluation_summary")
    result["recent_stress_return_pct"] = math.expm1(math.fsum(stress[-recent:])) * 100
    _finite(result)
    return result


def _observed_legacy_suffix(frames):
    """Never turn unknown historical LLM/legacy decisions into a cash policy."""
    start = len(frames)
    for index in range(len(frames) - 1, -1, -1):
        frame = frames[index]
        if frame.get("role") == "development_only":
            break
        try:
            captured = _instant(frame.get("captured_at"))
        except ValueError:
            break
        decisions = frame.get("baseline_signals")
        if (not isinstance(decisions, list) or not decisions or any(
                not isinstance(item, dict) or item.get("strategy_id") not in LEGACY_IDS
                or item.get("status") != "ready" or item.get("action") not in {"buy", "hold", "avoid"}
                for item in decisions)
                or {item["strategy_id"] for item in decisions} != set(LEGACY_IDS)):
            break
        if start < len(frames):
            following = frames[start]
            if captured >= datetime.combine(date.fromisoformat(_day(following["as_of"])), time(9), KST):
                break
            if any(_day(frame["as_of"]) < _day(day) < _day(following["as_of"])
                   for day in following.get("session_days", [])):
                break
        start = index
    return frames[start:]


def evaluate_daily_candidate(champion, proposal, frames, limits, *, evaluator=None):
    """Compare both policies on identical development data and trading costs.

    Two frames suffice to compute one return interval. A legacy parent
    requires the latest consecutive observations with all four real decisions;
    unknown historical signals cannot be reconstructed or treated as cash.
    A candidate must improve normal and stressed returns without worsening either
    drawdown. The latest third (at most 20 intervals) is diagnostic, not a gate.
    Missing/invalid inputs block a decision; valid but unimproved proposals keep
    the current policy. The caller decides when a provisional change takes effect.
    """
    result = {"decision": "blocked", "reason": "일일 비교 자료를 확인할 수 없습니다.",
              "metrics": {"basis": "development_only"}, "changes": [], "version": VERSION}
    try:
        _finite((champion, proposal, frames, limits))
        parent = validate_policy(deepcopy(champion))
        if not isinstance(proposal, dict):
            raise ValueError("invalid_proposal")
        candidate = validate_policy(deepcopy(proposal.get("policy")))
        if not isinstance(limits, dict):
            raise ValueError("invalid_limits")
        money = [Decimal(str(limits[key])) for key in ("budget", "order_cap", "daily_buy_limit")]
        if any(not value.is_finite() for value in money) or not 0 < money[1] <= money[2] <= money[0]:
            raise ValueError("invalid_limits")
        if "limits" in proposal and proposal["limits"] != limits:
            raise ValueError("proposal_changes_limits")
        if _behavior_id(parent) == _behavior_id(candidate):
            return {**result, "decision": "keep", "reason": "현재 정책과 실제 동작이 같습니다."}
        if candidate["kind"] != "rules":
            raise ValueError("unsupported_policy_transition")
        if parent["kind"] == "rules" and [item["id"] for item in parent["strategies"]] != [
                item["id"] for item in candidate["strategies"]]:
            raise ValueError("strategy_slots_changed")
        family, changes = _policy_changes(parent, candidate)
        if family is None or not changes:
            raise ValueError("multiple_or_unsupported_changes")
        if parent["cancel_after_minutes"] != candidate["cancel_after_minutes"]:
            raise ValueError("unevaluable_cancel_change")
        result["changes"] = changes
        result["metrics"]["change_family"] = family
        if not isinstance(frames, list):
            raise ValueError("invalid_frames")
        dates = [_day(frame.get("as_of")) for frame in frames]
        if any(before >= after for before, after in zip(dates, dates[1:])):
            raise ValueError("unordered_frames")
        if parent["kind"] == "legacy":
            observed = _observed_legacy_suffix(frames)
            result["metrics"].update(source_basis="observed_legacy_suffix", excluded_frames=len(frames) - len(observed),
                                     observed_baseline_frames=len(observed))
            if len(observed) < 2:
                result["reason"] = "기존 네 전략의 확인된 연속 관측 2개가 필요합니다."
                raise ValueError("insufficient_observed_baseline")
            frames = observed
            dates = [_day(frame["as_of"]) for frame in frames]
        if len(frames) < 2:
            result["reason"] = "수익률 비교를 위한 연속 관측 2개가 필요합니다."
            raise ValueError("insufficient_frames")
        intervals = len(frames) - 1
        recent = max(1, min(20, intervals // 3))
        evaluate = evaluator or portfolio_metrics
        try:
            raw_parent = evaluate(deepcopy(parent), deepcopy(frames), deepcopy(limits))
            raw_candidate = evaluate(deepcopy(candidate), deepcopy(frames), deepcopy(limits))
        except Exception:
            raise ValueError("evaluation_failed") from None
        a, b = (_metrics(raw, dates, recent) for raw in (raw_parent, raw_candidate))
        delta = {key: b[key] - a[key] for key in b}
        checks = {"normal_improvement": delta["net_return_pct"] > EPSILON,
                  "stress_improvement": delta["stress_return_pct"] > EPSILON,
                  "drawdown_not_worse": all(delta[key] <= EPSILON for key in ("max_drawdown_pct", "stress_drawdown_pct")),
                  "recent_improvement": delta["recent_stress_return_pct"] > EPSILON}
        result["metrics"].update(intervals=intervals, recent_intervals=recent, start_day=dates[0],
            end_day=dates[-1], champion=a, candidate=b, delta=delta, checks=checks,
            loss_reduction_only=b["net_return_pct"] <= 0 or b["stress_return_pct"] <= 0)
        _finite(result)
        if all(checks[key] for key in ("normal_improvement", "stress_improvement", "drawdown_not_worse")):
            reason = ("개발 비교에서 비용·낙폭 개선: 손실 축소의 잠정 적용"
                      if result["metrics"]["loss_reduction_only"] else
                      "개발 비교에서 비용·낙폭 개선: 잠정 적용")
            return {**result, "decision": "apply", "reason": reason}
        return {**result, "decision": "keep", "reason": "개발 비교에서 비용·낙폭 개선 기준 미달"}
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError) as error:
        reason = str(error)
        known = {"invalid_proposal", "invalid_limits", "proposal_changes_limits", "unsupported_policy_transition",
                 "strategy_slots_changed", "multiple_or_unsupported_changes", "unevaluable_cancel_change",
                 "invalid_frames", "insufficient_frames", "unordered_frames", "evaluation_failed", "nonfinite_input",
                 "unpaired_evaluation_dates", "invalid_equity_curve", "inconsistent_evaluation_returns",
                 "inconsistent_evaluation_summary", "insufficient_observed_baseline"}
        result["metrics"]["error"] = reason if reason in known else "invalid_evaluation_input"
        return result
