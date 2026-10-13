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


VERSION = "daily-development-v2"
EPSILON = 1e-9
MAXIMUM_DRAWDOWN_PCT = 15.
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


def _evaluation_window(frames, context):
    """Use only a book/model frozen at the actual replay anchor, never today retroactively."""
    if context is None:
        return frames, {}, {"replay_basis": "cash_daily_open", "context_applied": False,
                            "account_protection_inherited": False}
    if not isinstance(context, dict):
        raise ValueError("invalid_evaluation_context")
    book = context.get("initial_state")
    model = context.get("execution_model")
    if book is None and model is None and set(context) <= {"maximum_drawdown_pct"}:
        return frames, {}, {"replay_basis": "cash_daily_open", "context_applied": False,
                            "account_protection_inherited": False}
    if not isinstance(book, dict) or not isinstance(model, dict):
        raise ValueError("invalid_evaluation_context")
    if book.get("pending_orders") or book.get("unresolved_orders"):
        raise ValueError("unresolved_evaluation_book")
    anchor = _day(book.get("as_of"))
    index = next((index for index, frame in enumerate(frames) if _day(frame.get("as_of")) == anchor), None)
    if index is None:
        raise ValueError("evaluation_anchor_missing")
    selected = frames[index:]
    if len(selected) < 2:
        raise ValueError("insufficient_context_intervals")
    observed = _instant(context.get("observed_at"))
    if (observed < datetime.combine(date.fromisoformat(anchor), time(16), KST)
            or observed >= datetime.combine(date.fromisoformat(_day(selected[1]["as_of"])), time(9), KST)):
        raise ValueError("evaluation_context_not_known_at_anchor")
    return selected, {"initial_state": deepcopy(book), "execution_model": deepcopy(model)}, {
        "replay_basis": "frozen_book_execution_model", "context_applied": True,
        "account_protection_inherited": isinstance(book.get("account_protection"), dict),
        "context_anchor": anchor, "context_observed_at": observed.isoformat(),
        "context_excluded_frames": index}


def _comparison(a, b, drawdown_limit):
    delta = {key: b[key] - a[key] for key in b}
    checks = {"normal_improvement": delta["net_return_pct"] > EPSILON,
              "stress_improvement": delta["stress_return_pct"] > EPSILON,
              "drawdown_not_worse": all(delta[key] <= EPSILON for key in ("max_drawdown_pct", "stress_drawdown_pct")),
              "drawdown_within_limit": all(b[key] <= drawdown_limit + EPSILON
                                          for key in ("max_drawdown_pct", "stress_drawdown_pct")),
              "recent_improvement": delta["recent_stress_return_pct"] > EPSILON}
    eligible = all(checks[key] for key in ("normal_improvement", "stress_improvement", "drawdown_within_limit"))
    return {"champion": a, "candidate": b, "delta": delta, "checks": checks, "eligible": eligible}


def evaluate_daily_candidate(champion, proposal, frames, limits, *, evaluator=None, evaluation_context=None):
    """Compare both policies on identical development data and trading costs.

    Two frames suffice to compute one return interval. A legacy parent
    requires the latest consecutive observations with all four real decisions;
    unknown historical signals cannot be reconstructed or treated as cash.
    A candidate must improve normal and stressed returns within the 15% path
    drawdown cap. Relative drawdown and the latest third are diagnostics. This
    comparison cap does not replace the separate live account risk protection.
    Missing/invalid inputs block a decision; valid but unimproved proposals keep
    the current policy. The caller decides when a provisional change takes effect.
    """
    result = {"decision": "blocked", "reason": "일일 비교 자료를 확인할 수 없습니다.",
              "metrics": {"basis": "development_only"}, "changes": [], "version": VERSION}
    try:
        _finite((champion, proposal, frames, limits, evaluation_context))
        drawdown_limit = (evaluation_context or {}).get("maximum_drawdown_pct", MAXIMUM_DRAWDOWN_PCT)
        if type(drawdown_limit) not in (int, float) or not 0 < drawdown_limit <= MAXIMUM_DRAWDOWN_PCT:
            raise ValueError("invalid_evaluation_drawdown_limit")
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
        frames, options, context_metrics = _evaluation_window(frames, evaluation_context)
        result["metrics"].update(context_metrics)
        dates = [_day(frame["as_of"]) for frame in frames]
        if len(frames) < 2:
            result["reason"] = "수익률 비교를 위한 연속 관측 2개가 필요합니다."
            raise ValueError("insufficient_frames")
        intervals = len(frames) - 1
        recent = max(1, min(20, intervals // 3))
        evaluate = evaluator or portfolio_metrics
        try:
            raw_parent = evaluate(deepcopy(parent), deepcopy(frames), deepcopy(limits), **deepcopy(options))
            raw_candidate = evaluate(deepcopy(candidate), deepcopy(frames), deepcopy(limits), **deepcopy(options))
        except Exception:
            raise ValueError("evaluation_failed") from None
        a, b = (_metrics(raw, dates, recent) for raw in (raw_parent, raw_candidate))
        compared = _comparison(a, b, drawdown_limit)
        scenarios = {}
        model = options.get("execution_model", {})
        if "scenarios" in model:
            expected = [item["id"] for item in model["scenarios"]]
            if (set(raw_parent.get("scenario_results", {})) != set(expected)
                    or set(raw_candidate.get("scenario_results", {})) != set(expected)):
                raise ValueError("unpaired_execution_scenarios")
            for identity in expected:
                scenarios[identity] = _comparison(*(_metrics(raw["scenario_results"][identity], dates, recent)
                                                     for raw in (raw_parent, raw_candidate)), drawdown_limit)
        comparisons = list(scenarios.values()) or [compared]
        result["metrics"].update(intervals=intervals, recent_intervals=recent, start_day=dates[0],
            end_day=dates[-1], **{key: compared[key] for key in ("champion", "candidate", "delta", "checks")},
            maximum_drawdown_pct=drawdown_limit, risk_limit_basis="comparison_path_not_account_protection",
            scenario_comparisons=scenarios,
            selection_return_pct=min(item["candidate"]["net_return_pct"] for item in comparisons),
            selection_stress_return_pct=min(item["candidate"]["stress_return_pct"] for item in comparisons),
            loss_reduction_only=any(item["candidate"][key] <= 0 for item in comparisons
                                    for key in ("net_return_pct", "stress_return_pct")))
        _finite(result)
        if all(item["eligible"] for item in comparisons):
            reason = ("개발 비교에서 비용 후 수익 개선·낙폭 한도 충족: 손실 축소의 잠정 적용"
                      if result["metrics"]["loss_reduction_only"] else
                      "개발 비교에서 비용 후 수익 개선·낙폭 한도 충족: 잠정 적용")
            return {**result, "decision": "apply", "reason": reason}
        return {**result, "decision": "keep", "reason": "개발 비교에서 비용 후 수익 개선·낙폭 한도 기준 미달"}
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError) as error:
        reason = str(error)
        known = {"invalid_proposal", "invalid_limits", "proposal_changes_limits", "unsupported_policy_transition",
                 "strategy_slots_changed", "multiple_or_unsupported_changes", "unevaluable_cancel_change",
                 "invalid_frames", "insufficient_frames", "unordered_frames", "evaluation_failed", "nonfinite_input",
                 "unpaired_evaluation_dates", "invalid_equity_curve", "inconsistent_evaluation_returns",
                 "inconsistent_evaluation_summary", "insufficient_observed_baseline", "invalid_evaluation_context",
                 "unresolved_evaluation_book", "evaluation_anchor_missing", "insufficient_context_intervals",
                 "evaluation_context_not_known_at_anchor", "unpaired_execution_scenarios"}
        known.add("invalid_evaluation_drawdown_limit")
        result["metrics"]["error"] = reason if reason in known else "invalid_evaluation_input"
        return result


def select_daily_candidate(champion, proposal, frames, limits, *, evaluator=None, evaluation_context=None):
    """One final decision from a bounded, already generated development pool.

    Every candidate sees the same inputs and gates. Reusing these data for
    selection remains development evidence; this is not a multiplicity-adjusted
    test. Old saved single-proposal records retain their single-candidate path.
    """
    from backend.learning_policy import policy_id
    from backend.learning_research import MAX_CANDIDATES
    evaluate = evaluator or evaluate_daily_candidate
    pool = proposal.get("candidate_pool") if isinstance(proposal, dict) else None
    supplied = pool if pool is not None else [proposal]
    try:
        if not isinstance(supplied, list) or not 1 <= len(supplied) <= MAX_CANDIDATES:
            raise ValueError("invalid_candidate_pool")
        ids = [policy_id(item["policy"]) for item in supplied]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate_candidate_pool")
    except (ValueError, TypeError, KeyError):
        return deepcopy(proposal), {"decision": "blocked", "reason": "개발 후보 묶음을 확인할 수 없습니다.",
            "metrics": {"basis": "development_only", "error": "invalid_candidate_pool"}, "changes": [], "version": VERSION}
    records = []
    for item, identity in zip(supplied, ids):
        options = {"evaluation_context": deepcopy(evaluation_context)} if evaluation_context is not None else {}
        result = evaluate(deepcopy(champion), deepcopy(item), deepcopy(frames), deepcopy(limits), **options)
        records.append((item, identity, result))
    eligible = [record for record in records if record[2].get("decision") == "apply"]
    def rank(record):
        metrics = record[2].get("metrics") or {}
        return (*tuple(metrics.get(key, -math.inf) for key in ("selection_return_pct", "selection_stress_return_pct")), record[1])
    selected = max(eligible, key=rank) if eligible else next(
        (record for record in records if record[1] == policy_id(proposal["policy"])), records[0])
    selected_proposal = deepcopy(selected[0])
    if pool is not None:
        selected_proposal["candidate_pool"] = deepcopy(pool)
    result = deepcopy(selected[2])
    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        metrics["selection"] = {"objective": "net_return_with_cost_stress_and_drawdown_cap",
            "basis": "reused_development_data", "prospective": False,
            "candidates_evaluated": len(records), "eligible_candidates": len(eligible),
            "selected_candidate_id": selected[1], "original_candidate_id": policy_id(proposal["policy"]),
            "pool_role": "full_development_pool" if pool is not None else "legacy_single_proposal",
            "outcomes": [{"candidate_id": identity, "decision": outcome.get("decision"),
                          "reason": outcome.get("reason"),
                          "net_return_pct": (outcome.get("metrics") or {}).get("selection_return_pct"),
                          "stress_return_pct": (outcome.get("metrics") or {}).get("selection_stress_return_pct")}
                         for _, identity, outcome in records]}
    return selected_proposal, result
