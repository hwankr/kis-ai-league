"""Pure development comparison for a provisional daily paper-policy change.

This is a selection rule on reused historical data, not a significance test or
proof of improvement. Relative loss reduction may qualify; positive returns and
completed trades are not required. No account limits or observations are changed.
"""
from copy import deepcopy
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import math

from backend.experiment_store import encode
from backend.learning_evaluation import _return_and_drawdown, _series
from backend.learning_policy import KST, LEGACY_IDS, _day, _instant, portfolio_metrics, validate_policy
from backend.learning_research import _behavior_id, _policy_changes, _public_activity


VERSION = "daily-award-development-v7"
ANCHOR_SELECTION_VERSION = "consecutive-observed-sessions-v1"
EPSILON = 1e-9
MAXIMUM_DRAWDOWN_PCT = 15.
METRICS = ("net_return_pct", "stress_return_pct", "max_drawdown_pct", "stress_drawdown_pct")
PORTFOLIO_INPUT_ERRORS = frozenset({"protection_quote_missing", "protection_quote_after_decision"})


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
    if isinstance(raw, dict) and raw.get("valid") is False:
        error = raw.get("error")
        if isinstance(error, str) and error in PORTFOLIO_INPUT_ERRORS:
            raise ValueError(error)
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


def _replay_input_errors(raw):
    if not isinstance(raw, dict):
        return set()
    outcomes = [raw]
    scenarios = raw.get("scenario_results")
    if isinstance(scenarios, dict):
        outcomes.extend(scenarios.values())
    return {item["error"] for item in outcomes
            if isinstance(item, dict) and item.get("valid") is False
            and isinstance(item.get("error"), str) and item["error"] in PORTFOLIO_INPUT_ERRORS}


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
    selection = context.get('anchor_selection')
    if selection is not None:
        if (not isinstance(selection, dict) or selection.get('version') != ANCHOR_SELECTION_VERSION
                or selection.get('selected_anchor') != anchor
                or selection.get('end_day') != selected[-1]['as_of']
                or selection.get('frame_days') != [frame['as_of'] for frame in selected]
                or selection.get('frames_hash') != hashlib.sha256(encode(selected).encode()).hexdigest()
                or selection.get('book_hash') != hashlib.sha256(encode(book).encode()).hexdigest()
                or any(frame.get('role') == 'development_only' for frame in selected)):
            raise ValueError('invalid_anchor_selection')
    observed = _instant(context.get("observed_at"))
    if (observed < datetime.combine(date.fromisoformat(anchor), time(16), KST)
            or observed >= datetime.combine(date.fromisoformat(_day(selected[1]["as_of"])), time(9), KST)):
        raise ValueError("evaluation_context_not_known_at_anchor")
    return selected, {"initial_state": deepcopy(book), "execution_model": deepcopy(model)}, {
        "replay_basis": "frozen_book_execution_model", "context_applied": True,
        "account_protection_inherited": isinstance(book.get("account_protection"), dict),
        "context_anchor": anchor, "context_observed_at": observed.isoformat(),
        "context_excluded_frames": index,
        **({'anchor_selection': deepcopy(selection)} if selection is not None else {})}


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


def evaluate_daily_candidate(champion, proposal, frames, limits, *, evaluator=None, evaluation_context=None,
                             candidate_pool=None):
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
    replay_errors = set()
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
        capacity = None
        if parent['kind'] == 'rules' and 'capacity_packets' in proposal:
            from backend.policy_capacity import validate_capacity_member
            capacity = validate_capacity_member(parent, proposal, candidate_pool, limits=limits, frames=frames)
            result['metrics']['capacity_contract'] = capacity
        if _behavior_id(parent) == _behavior_id(candidate):
            return {**result, "decision": "keep", "reason": "현재 정책과 실제 동작이 같습니다."}
        if candidate["kind"] != "rules":
            raise ValueError("unsupported_policy_transition")
        if parent["kind"] == "rules" and [item["id"] for item in parent["strategies"]] != [
                item["id"] for item in candidate["strategies"]]:
            raise ValueError("strategy_slots_changed")
        family, changes = _policy_changes(parent, candidate)
        if capacity is not None and (family is None or capacity['version'] == 'signal-hold-packet-v1'):
            family, changes = 'capacity', capacity['changes']
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
        replay_errors = _replay_input_errors(raw_parent) | _replay_input_errors(raw_candidate)
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
        result["metrics"]["paired_trading_activity"] = {
            side: {"normal": _public_activity(raw.get("trading_activity")),
                   "cost_stress": _public_activity(raw.get("stress_trading_activity"))}
            for side, raw in (("champion", raw_parent), ("candidate", raw_candidate))}
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
                 "evaluation_context_not_known_at_anchor", "unpaired_execution_scenarios", "invalid_anchor_selection"}
        known.update({"invalid_evaluation_drawdown_limit", "invalid_capacity_contract",
                      "invalid_portfolio_evaluation", "invalid_daily_return", "missing_daily_returns",
                      "interval_count_mismatch", "missing_evaluation_dates", "invalid_evaluation_dates",
                      "cost_scenarios_not_aligned"})
        known.update(PORTFOLIO_INPUT_ERRORS)
        result["metrics"]["error"] = reason if reason in known else "invalid_evaluation_input"
        if replay_errors:
            result["metrics"]["replay_diagnostics"] = {"stage": "portfolio_replay", "codes": sorted(replay_errors)}
        return result


def _apply_capacity_dependencies(champion, records):
    from backend.learning_policy import policy_id
    # The compound exception needs measured controls, not just well-formed
    # policies. A valid but unimproved control is still evidence; a failed
    # computation cannot authorize the allocation/exit interaction.
    def measured(metrics):
        return isinstance(metrics, dict) and all(type(metrics.get(key)) in (int, float)
            and math.isfinite(metrics[key]) for key in METRICS)
    def parent_evidence(metrics):
        scenarios = metrics.get('scenario_comparisons') or {}
        if (not measured(metrics.get('champion')) or not isinstance(scenarios, dict)
                or any(not isinstance(value, dict) or not measured(value.get('champion'))
                       for value in scenarios.values())):
            return None
        return (metrics['champion'], {key: value['champion'] for key, value in scenarios.items()})
    original_evidence = {}
    for _, identity, outcome in records:
        evidence = outcome.get('metrics') or {}
        parent = parent_evidence(evidence)
        valid = (outcome.get('decision') in {'apply', 'keep'} and not evidence.get('error')
                 and measured(evidence.get('candidate')) and parent is not None
                 and all(measured(value.get('candidate')) for value in
                         (evidence.get('scenario_comparisons') or {}).values()))
        original_evidence[identity] = (valid, parent)
    for _, _, outcome in records:
        metrics = outcome.get('metrics') or {}
        if metrics.get('change_family') != 'capacity' or outcome.get('decision') not in {'apply', 'keep'}:
            continue
        parent = parent_evidence(metrics)
        failures = []
        for packet in metrics['capacity_contract']['control_packets']:
            for role in packet.get('required_roles', packet['member_ids']):
                identity = packet['member_ids'][role]
                if identity == policy_id(champion):
                    valid = parent is not None
                else:
                    valid, control_parent = original_evidence.get(identity, (False, None))
                    valid = valid and parent is not None and control_parent == parent
                if not valid:
                    failures.append({'packet_hash': packet['packet_hash'], 'role': role,
                                     'candidate_id': identity})
        if failures:
            outcome.update(decision='blocked', reason='배분·청산 공동 변경의 대조군 비교 자료를 확인할 수 없습니다.')
            metrics.update(error='capacity_control_evidence_unavailable', capacity_control_failures=failures)
    return original_evidence


def validate_capacity_result_records(champion, records, frames, limits):
    """Recheck required measured controls on an immutable native replay receipt."""
    from backend.learning_policy import policy_id
    from backend.policy_capacity import validate_capacity_member
    if not isinstance(records, list) or not records:
        raise ValueError('capacity_result_records_invalid')
    pool = [record[0] for record in records]
    for proposal, identity, outcome in records:
        if identity != policy_id(proposal['policy']):
            raise ValueError('capacity_result_identity_changed')
        if outcome.get('decision') not in {'apply', 'keep', 'blocked'}:
            raise ValueError('capacity_result_decision_invalid')
        if 'capacity_packets' in proposal:
            expected = validate_capacity_member(champion, proposal, pool, frames=frames, limits=limits)
            if (outcome.get('metrics') or {}).get('capacity_contract') != expected:
                raise ValueError('capacity_result_contract_changed')
            if (expected.get('changes') and outcome.get('decision') in {'apply', 'keep'}
                    and (outcome.get('metrics') or {}).get('change_family') != 'capacity'):
                raise ValueError('capacity_result_family_changed')
    checked = deepcopy(records)
    _apply_capacity_dependencies(champion, checked)
    if checked != records:
        raise ValueError('capacity_result_controls_unverified')
    return True


def select_daily_candidate(champion, proposal, frames, limits, *, evaluator=None, evaluation_context=None,
                           award_context=None):
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
        if evaluate is evaluate_daily_candidate:
            options['candidate_pool'] = deepcopy(supplied)
        result = evaluate(deepcopy(champion), deepcopy(item), deepcopy(frames), deepcopy(limits), **options)
        records.append((item, identity, result))
    original_evidence = _apply_capacity_dependencies(champion, records)
    eligible = [record for record in records if record[2].get("decision") == "apply"]
    def rank(record):
        metrics = record[2].get("metrics") or {}
        return (*tuple(metrics.get(key, -math.inf) for key in ("selection_return_pct", "selection_stress_return_pct")), record[1])
    award = None
    award_index = None
    if award_context is not None and award_context.get("competition_context") is not None:
        from backend.policy_award import select_award_record
        award_index, award = select_award_record(champion, records, frames, limits, award_context)
    selected = (records[award_index] if award_index is not None else
                max(eligible, key=rank) if eligible and award is None else next(
                    (record for record in records if record[1] == policy_id(proposal["policy"])), records[0]))
    selected_proposal = deepcopy(selected[0])
    if pool is not None:
        selected_proposal["candidate_pool"] = deepcopy(pool)
    result = deepcopy(selected[2])
    if award is not None:
        result["decision"] = award["status"]
        reason = {"return_improvement": "공통 개발 경로의 비용 후 평균 최종자산 개선과 보호 조건을 확인한 잠정 적용",
                  "return_challenge": "비용 후 평균 최종자산 개선을 확인한 잠정 도전: 수상요건 미달 위험은 남아 있음",
                  "no_supported_return_improvement": "수익 우선·수상 활동요건·보호 조건을 함께 통과한 대안이 없어 현행 유지",
                  "return_improvement_with_capacity": "현재 보유 전이와 비용 후 수익 개선을 확인한 잠정 적용",
                  "qualification_tradeoff": "허용 수익 범위에서 동일 경로의 수상요건 개선을 확인한 잠정 적용",
                  "joint_qualification_robust": "공통 기본·예비 경로의 공동 수상요건과 위험 한도를 충족한 잠정 적용",
                  "joint_qualification_conditional": "기본 경로의 공동 요건 충족·현행 대비 최악 자산 비악화를 확인한 조건부 적용",
                  "partial_joint_challenge": "기본 경로의 공동 충족 증가·거래금액 부족 개선·최악 자산 비악화를 확인한 조건부 도전",
                  "qualified_progress": "현행 대비 최악 자산을 낮추지 않고 거래금액 부족 개선을 확인한 잠정 적용",
                  "risk_reduction": "현재 신규 노출·비교 낙폭 감소를 확인한 위험 축소 적용",
                  "no_supported_plan": "수상요건 미달 위험: 검증된 대안 미확보로 현행 유지",
                  "no_supported_award_improvement": "수상요건·현재 보유를 반영한 개선 근거 부족: 현행 유지",
                  "incumbent_tie_or_qualification_preference": "공동 평가에서 현행 유지 우선",
                  "account_protection_active": "누적 손실 보호 중: 현행 보호·청산 유지",
                  "contest_has_no_remaining_sessions": "대회 잔여 거래세션 없음: 현행 유지"}
        result["reason"] = reason.get(award["reason"], "수상요건·현재 보유의 필수 자료 미확인: 신규 정책 보류")
        if not isinstance(result.get("metrics"), dict):
            result["metrics"] = {}
        result["metrics"]["award_selection"] = award
    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        contracts = {}
        by_id = {identity: outcome for _, identity, outcome in records}
        parent_sources = {}
        # Audit provenance must not depend on which linked candidate happens
        # to be projected last, especially one whose own comparison failed.
        for _, source_id, source in sorted(records, key=lambda record: record[1]):
            valid, parent = original_evidence[source_id]
            if valid:
                contract = (source.get('metrics') or {}).get('capacity_contract') or {}
                for packet in contract.get('control_packets', []):
                    parent_sources.setdefault(packet['packet_hash'], (source_id, parent))
        for _, _, outcome in records:
            contract = (outcome.get('metrics') or {}).get('capacity_contract')
            for packet in (contract or {}).get('control_packets', []):
                controls = {}
                for role, identity in packet['member_ids'].items():
                    control = by_id.get(identity)
                    raw = (control or {}).get('metrics') or {}
                    controls[role] = {'candidate_id': identity,
                        'basis': 'incumbent_reference' if identity == policy_id(champion) else 'same_pool_evaluation',
                        'decision': (control or {}).get('decision', 'keep' if identity == policy_id(champion) else 'blocked'),
                        'metrics': {key: deepcopy(raw[key]) for key in ('candidate', 'scenario_comparisons',
                            'selection_return_pct', 'selection_stress_return_pct', 'maximum_drawdown_pct', 'error') if key in raw}}
                    if identity == policy_id(champion):
                        source_id, parent = parent_sources.get(packet['packet_hash'], (None, None))
                        controls[role]['source_candidate_id'] = source_id
                        controls[role]['metrics'] = {'champion': deepcopy(parent[0]) if parent else None,
                            'scenario_comparisons': {key: {'champion': deepcopy(value)}
                                for key, value in (parent[1] if parent else {}).items()}}
                contracts[packet['packet_hash']] = {'version': packet['version'], 'top_n': packet['top_n'],
                    'base_id': packet['base_id'], 'actual_parent_id': packet['actual_parent_id'],
                    'packet_hash': packet['packet_hash'], 'controls': controls}
        if contracts:
            metrics['capacity_controls'] = list(contracts.values())
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
        if award is not None:
            metrics["selection"].update(objective="core_mean_final_equity_with_qualification_preservation_and_risk_gates",
                return_objective=deepcopy(award.get("return_objective")),
                eligible_candidates=award.get("eligible_candidates", 0),
                selected_candidate_id=selected[1] if award_index is not None else policy_id(champion),
                incumbent_selected=award_index is None,
                outcomes=deepcopy(award["outcomes"]))
    return selected_proposal, result
