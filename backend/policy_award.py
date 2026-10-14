"""Award-aware selection on frozen evidence; never an order or win probability.

Historical return comparison and current-book transition are separate. A
qualification witness must meet every condition on the SAME scenario. Unknown
inputs stay unknown, and volume above a qualification threshold earns no reward.
"""
from copy import deepcopy
import math

from backend.daily_inputs import validate_award_context
from backend.policy_transition import transition_preview


VERSION = "award-selection-v2"
# Relative backtest return is an opportunity-cost audit, not an award gate.
# The separate safety-only exception still cannot worsen matched returns.
RISK_RETURN_TOLERANCE_PP = 0.0
CONTESTS = {
    "kis_ai_2026": {"rules_version": "kis-2026-reviewed-2026-10-10",
        "start_date": "2026-10-12", "end_date": "2026-11-20",
        "targets": {"gross_amount": 500000000, "trading_days": 5, "symbols": 5, "api_orders": 5},
        "fee_rate": .000140527, "sell_tax_rate": .002,
        "objective": "qualification_and_nonnegative_final_return_then_return_rank"},
    "timefolio_rfm_13": {"rules_version": "timefolio-13-reviewed-2026-10-10",
        "start_date": "2026-10-01", "end_date": "2026-11-30",
        "weekly_turnover_pct": 5., "excluded_after_missed_weeks": 4,
        "fee_rate": .001, "sell_tax_rate": .002,
        "stock_weight_cap_pct": 15., "stock_exceptions": {"005930": 40., "000660": 30.},
        "sector_cap": "max(2 * benchmark_weight, 10%)", "small_cap_total_weight_pct": 30.,
        "objective": "independent_weekly_qualification_and_return_rank"},
}


def selection_contract():
    return {"version": VERSION, "contest_id": "kis_ai_2026", "return_tolerance_pp": None,
            "rules": deepcopy(CONTESTS["kis_ai_2026"]),
            "calendar_basis": "weekday_estimate_with_two_session_shortening",
            "risk_limit_pct": 15., "existing_lot_rules": "preserved",
            "relative_return_role": "opportunity_cost_audit_except_risk_reduction",
            "risk_reduction_return_tolerance_pp": RISK_RETURN_TOLERANCE_PP,
            "core_environments": ["normal", "cost_stress"],
            "reserve_environments": ["partial_delayed", "partial_delayed_cost_stress"],
            "promotion_order": ["joint_qualification_robust", "joint_qualification_conditional", "qualified_progress", "risk_reduction"],
            "progress_threshold": "ceil_remaining_gross_shortfall_divided_by_remaining_sessions",
            "turnover_above_target_reward": 0, "win_probability": None}


def separate_contest_status():
    # A cost-only KIS replay cannot establish Timefolio eligibility: its actual
    # ledger, weekly NAV denominators and destination sector checks are needed.
    return {"contest_id": "timefolio_rfm_13", "role": "shadow_only", "status": "unavailable",
            "reason": "independent_ledger_weekly_nav_and_sector_evidence_required",
            "rules": deepcopy(CONTESTS["timefolio_rfm_13"]),
            "affects_kis_selection": False, "mirror_intent_changed": False}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _qualification_summary(scenarios, targets):
    """Keep known failures separate from incomplete source/condition coverage."""
    count = len(scenarios)
    evaluated = sum(item['coverage_complete'] for item in scenarios)
    qualifying = sum(item['joint_met'] is True for item in scenarios)
    complete = bool(count) and evaluated == count
    status = ('all_met' if qualifying == count else 'some_met' if qualifying else 'none_met') if complete else 'unknown'
    conditions = {}
    for key, target in targets.items():
        known = [item['deficits'][key] for item in scenarios if item['deficits'][key] is not None]
        conditions[key] = {'target': target,
            'met_scenarios': sum(item['checks'][key] is True for item in scenarios),
            'failed_scenarios': sum(item['checks'][key] is False for item in scenarios),
            'unknown_scenarios': sum(item['checks'][key] is None for item in scenarios),
            'worst_known_deficit': max(known) if known else None,
            'worst_known_normalized_deficit': max(known) / target if known else None}
    known_shortfalls = [value for item in scenarios for value in item['normalized_deficits'].values() if value is not None]
    return {'status': status, 'grade': {'all_met': 2, 'some_met': 1, 'none_met': 0}.get(status, -1),
        'scenario_count': count, 'evaluated_scenarios': evaluated, 'qualifying_scenarios': qualifying,
        'source_ready_scenarios': sum(item['source_ready'] for item in scenarios),
        'known_failed_scenarios': sum(bool(item['known_failed_conditions']) for item in scenarios),
        'unknown_coverage_scenarios': count - evaluated,
        'unresolved_joint_scenarios': sum(not item['known_failed_conditions'] and not item['coverage_complete'] for item in scenarios),
        'coverage_complete': complete, 'coverage_fraction': evaluated / count if count else None,
        'conditions': conditions,
        'worst_shortfall': max(item['shortfall'] for item in scenarios) if complete else None,
        'worst_known_shortfall': max(known_shortfalls) if known_shortfalls else None,
        'gross_shortfall_amount': conditions['gross_amount']['worst_known_deficit'],
        'final_equity_shortfall_amount': conditions['final_equity']['worst_known_deficit']}


def joint_qualification(paths, progress, budget):
    """Add disjoint future model fills to actual totals, never mix scenarios.

    Symbols use a conservative union lower bound because the public actual
    context has counts, not a ticker set. Repeated partial fills are not assumed
    to be distinct API orders. Neither missing field is replaced with zero.
    """
    result = {"status": "unknown", "grade": -1, "worst_shortfall": None,
              "qualifying_scenarios": 0, "scenario_count": 0, "scenarios": [], "groups": {}, "win_probability": None}
    if not isinstance(paths, dict) or paths.get("status") not in {"ready", "unavailable"} or not progress.get("reliable"):
        return result
    supplied = paths.get("scenarios")
    if (not isinstance(supplied, list) or not supplied
            or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] for item in supplied)
            or len({item["id"] for item in supplied}) != len(supplied)):
        return result
    actual = {"gross_amount": _number(progress.get("pace", {}).get("gross_amount"))}
    actual.update({key: _number(progress.get("metrics", {}).get(key, {}).get("value"))
                   for key in ("trading_days", "symbols", "api_orders")})
    targets = {**CONTESTS["kis_ai_2026"]["targets"], "final_equity": float(budget)}
    for path in supplied:
        values = {}
        source_ready = path.get('status', 'ready') == 'ready'
        for key in actual:
            current, added = actual[key], _number(path.get(key)) if source_ready else None
            if current is None or current < 0:
                values[key] = None
            elif current >= targets[key]:
                values[key] = current
            elif added is None or added < 0:
                values[key] = None
            elif key == "symbols":
                values[key] = max(current, added)
            else:
                values[key] = current + added
        values["final_equity"] = _number(path.get("final_equity")) if source_ready else None
        checks = {key: value >= targets[key] if value is not None else None for key, value in values.items()}
        complete = source_ready and all(value is not None for value in checks.values())
        deficits = {key: max(0., targets[key] - value) if value is not None else None for key, value in values.items()}
        normalized = {key: value / targets[key] if value is not None else None for key, value in deficits.items()}
        shortfall = max(normalized.values()) if complete else None
        environment = path.get('environment')
        group = ('core' if environment in {'normal', 'cost_stress'} else 'reserve'
                 if environment in {'partial_delayed', 'partial_delayed_cost_stress'} else 'unclassified')
        result["scenarios"].append({"id": path["id"], "values": values, "checks": checks,
            'environment': environment, 'group': group, 'source_ready': source_ready,
            'coverage_complete': complete, 'known_failed_conditions': sorted(key for key, value in checks.items() if value is False),
            'unknown_conditions': sorted(key for key, value in checks.items() if value is None),
            'deficits': deficits, 'normalized_deficits': normalized,
            'gross_shortfall_amount': deficits['gross_amount'], 'final_equity_shortfall_amount': deficits['final_equity'],
            'path_hash': path.get('path_hash'), 'scenario_plan_hash': path.get('scenario_plan_hash', paths.get('scenario_plan_hash')),
            'execution_diagnostics': {key: _number((path.get('execution_diagnostics') or {}).get(key))
                for key in ('buy_attempts', 'sell_attempts', 'partial_orders', 'unfilled_orders', 'limit_blocked',
                            'untradeable_bars', 'ineligible_quotes', 'fees_paid', 'taxes_paid', 'slippage_bps', 'final_holding_count')},
            "joint_met": all(checks.values()) if complete else None, "shortfall": shortfall})
    result.update(_qualification_summary(result['scenarios'], targets))
    result['scenario_plan_hash'] = paths.get('scenario_plan_hash')
    result['groups'] = {group: _qualification_summary([item for item in result['scenarios'] if item['group'] == group], targets)
                        for group in ('core', 'reserve', 'unclassified')}
    return result


def _returns(metrics, side):
    comparisons = list((metrics.get("scenario_comparisons") or {}).values()) or [metrics]
    values = [{key: _number(item.get(side, {}).get(key)) for key in
               ("net_return_pct", "stress_return_pct", "max_drawdown_pct", "stress_drawdown_pct")}
              for item in comparisons]
    if not values or any(value is None for item in values for value in item.values()):
        return None
    return {key: (min if "return" in key else max)(item[key] for item in values) for key in values[0]}


def _return_profiles(metrics, side):
    comparisons = metrics.get("scenario_comparisons") or {"paired": metrics}
    return {identity: deepcopy(value[side]) for identity, value in comparisons.items()}


def _within_return_budget(candidate, reference, tolerance):
    return (candidate.keys() == reference.keys() and all(
        candidate[identity][key] >= reference[identity][key] - tolerance - 1e-9
        for identity in reference for key in ("net_return_pct", "stress_return_pct")))


def _strict_return_improvement(candidate, reference):
    return (candidate.keys() == reference.keys() and all(
        candidate[identity][key] > reference[identity][key] + 1e-9
        for identity in reference for key in ("net_return_pct", "stress_return_pct")))


def _family_status(paths, qualification, plan, cap):
    """All candidates need the same complete 24-path contract, never a subset."""
    expected = {f'{window}_{schedule}_{environment}'
        for window in ('first', 'middle', 'recent') for schedule in ('full', 'shortened')
        for environment in ('normal', 'cost_stress', 'partial_delayed', 'partial_delayed_cost_stress')}
    supplied = paths.get('scenarios') or []
    by_id = {item.get('id'): item for item in supplied}
    schedules = plan.get('schedules') or []
    if (plan.get('status') != 'ready' or paths.get('status') != 'ready'
            or not plan.get('hash') or paths.get('scenario_plan_hash') != plan['hash']
            or len(schedules) != 2 or {item.get('schedule') for item in schedules} != {'full', 'shortened'}
            or any(len(item.get('selected', [])) != 3 or {source.get('window') for source in item['selected']}
                   != {'first', 'middle', 'recent'} for item in schedules)
            or len(supplied) != 24 or set(by_id) != expected
            or qualification.get('evaluated_scenarios') != 24
            or qualification.get('groups', {}).get('core', {}).get('scenario_count') != 12
            or qualification.get('groups', {}).get('reserve', {}).get('scenario_count') != 12):
        return 'scenario_coverage_unknown'
    for record in plan.get('schedules', []):
        for source in record['selected']:
            prefix = source['window'] + '_' + record['schedule']
            group = [by_id[prefix + '_' + environment] for environment in
                     ('normal', 'cost_stress', 'partial_delayed', 'partial_delayed_cost_stress')]
            if (any(item.get('scenario_plan_hash') != plan['hash']
                    or item.get('source_start_index') != source['source_start_index']
                    or item.get('start_book_hash') != plan.get('start_book_hash') for item in group)
                    or not group[0].get('path_hash')
                    or len({item.get('path_hash') for item in group}) != 1):
                return 'scenario_coverage_unknown'
    for item in supplied:
        if (item.get('status', 'ready') != 'ready'
                or not item['id'].endswith('_' + str(item.get('environment')))
                or 'first_buy_session' not in item
                or any(_number(item.get(key)) is None for key in
                       ('gross_amount', 'trading_days', 'symbols', 'final_equity', 'max_drawdown_pct'))):
            return 'scenario_coverage_unknown'
        first_buy = item['first_buy_session']
        if first_buy is not None and (type(first_buy) is not int or first_buy < 1
                or type(item.get('horizon')) is not int or first_buy > item['horizon']):
            return 'scenario_coverage_unknown'
        if any(float(item[key]) < 0 for key in ('gross_amount', 'trading_days', 'symbols', 'final_equity')):
            return 'scenario_coverage_unknown'
        if not 0 <= float(item['max_drawdown_pct']) <= cap + 1e-9:
            return 'scenario_risk_limit'
    return 'ready'


def _absolute_class(qualification, family):
    if family != 'ready':
        return 0, family
    if any(item['checks']['final_equity'] is not True for item in qualification['scenarios']):
        return 0, 'final_equity_floor'
    if qualification['status'] == 'all_met':
        return 4, 'joint_qualification_robust'
    if qualification['groups']['core']['status'] == 'all_met':
        return 3, 'joint_qualification_conditional'
    return 0, 'qualification_shortfall'


def _progress_evidence(candidate, parent, candidate_paths, parent_paths, *, candidate_family, parent_family, threshold):
    result = {'supported': False, 'required_improvement_amount': threshold,
              'worst_deficit_improvement_amount': None, 'reason': 'paired_scenario_evidence_unavailable'}
    if candidate_family != 'ready' or parent_family != 'ready':
        return result
    proposed = {item['id']: item for item in candidate['scenarios']}
    incumbent = {item['id']: item for item in parent['scenarios']}
    left = {item['id']: item for item in candidate_paths['scenarios']}
    right = {item['id']: item for item in parent_paths['scenarios']}
    if (set(proposed) != set(incumbent) or candidate_paths.get('scenario_plan_hash') != parent_paths.get('scenario_plan_hash')
            or any(left[key].get('path_hash') != right[key].get('path_hash')
                   or left[key].get('start_book_hash') != right[key].get('start_book_hash') for key in left)):
        return result
    if any(any(item['checks'][key] is not True for key in ('final_equity', 'trading_days', 'symbols', 'api_orders'))
           for item in proposed.values()):
        result['reason'] = 'non_turnover_qualification_failed'
        return result
    if any(proposed[key]['deficits']['gross_amount'] > incumbent[key]['deficits']['gross_amount'] + 1e-6 for key in proposed):
        result['reason'] = 'some_path_turnover_shortfall_worsened'
        return result
    improvement = max(item['deficits']['gross_amount'] for item in incumbent.values()) - max(
        item['deficits']['gross_amount'] for item in proposed.values())
    result['worst_deficit_improvement_amount'] = improvement
    if improvement <= 1e-6 or improvement + 1e-6 < threshold:
        result['reason'] = 'less_than_predeclared_one_session_progress'
        return result
    result.update(supported=True, reason='qualified_progress')
    return result


def _plan_rank(item):
    rows = item['paths'].get('scenarios') or []
    equities = [_number(row.get('final_equity')) for row in rows]
    normal = [_number(row.get('final_equity')) for row in rows if row.get('environment') == 'normal']
    drawdowns = [_number(row.get('max_drawdown_pct')) for row in rows]
    worst = min(equities) if equities and all(value is not None for value in equities) else -math.inf
    ordinary = min(normal) if normal and all(value is not None for value in normal) else -math.inf
    risk = max(drawdowns) if drawdowns and all(value is not None for value in drawdowns) else item['return']['stress_drawdown_pct']
    progress_rank = -item['qualification']['gross_shortfall_amount'] if item['class'] == 2 else 0
    return item['class'], progress_rank, worst, ordinary, -risk, item['index'] is None, item['id']


def select_award_record(champion, records, frames, limits, context):
    """Promote absolute joint qualification, with separately named exceptions."""
    from backend.policy_award_scenarios import award_scenarios, scenario_plan
    audit = {**selection_contract(), 'status': 'blocked', 'reason': 'invalid_award_context',
             'incumbent_included': True, 'outcomes': [], 'timefolio': separate_contest_status()}
    if (not validate_award_context(context) or context['limits'] != limits
            or not isinstance(frames, list) or not frames or frames[-1] != context['latest_frame']):
        return None, audit
    progress = context['competition_context']
    if (not isinstance(progress, dict) or progress.get('contest') != 'kis_ai_2026'
            or not progress.get('reliable') or progress.get('period', {}).get('as_of') != context['signal_day']):
        audit['reason'] = 'contest_progress_unavailable'
        return None, audit
    remaining = _number(progress.get('pace', {}).get('remaining_sessions'))
    actual_gross = _number(progress.get('pace', {}).get('gross_amount'))
    if remaining is None or remaining < 0 or not remaining.is_integer() or actual_gross is None or actual_gross < 0:
        audit['reason'] = 'contest_progress_unavailable'
        return None, audit
    if remaining == 0:
        audit.update(status='keep', reason='contest_has_no_remaining_sessions')
        return None, audit
    threshold = math.ceil(max(0., CONTESTS['kis_ai_2026']['targets']['gross_amount'] - actual_gross) / remaining)
    audit['progress_threshold_amount'] = threshold
    frame = context['latest_frame']
    parent_preview = transition_preview(champion, frame, limits, context)
    audit['incumbent_transition'] = parent_preview
    if parent_preview['status'] != 'ready':
        audit['reason'] = 'incumbent_transition_unavailable'
        return None, audit
    if parent_preview.get('protection', {}).get('active'):
        audit.update(status='keep', reason='account_protection_active')
        return None, audit
    plan = scenario_plan(frames, context)
    audit['scenario_plan'] = plan
    def replay(policy):
        evidence = award_scenarios(policy, frames, limits, context, plan=plan)
        return {key: value for key, value in evidence.items() if key != 'scenario_plan'}
    parent_paths = replay(champion)
    parent_qualification = joint_qualification(parent_paths, progress, limits['budget'])
    parent_family = _family_status(parent_paths, parent_qualification, plan, 15.)
    parent_class, parent_reason = _absolute_class(parent_qualification, parent_family)
    audit.update(incumbent_scenarios=parent_paths, incumbent_qualification=parent_qualification,
                 incumbent_plan_class=parent_reason, incumbent_scenario_status=parent_family)
    parent_return = parent_profiles = None
    assessable = []
    for index, (proposal, identity, outcome) in enumerate(records):
        metrics = outcome.get('metrics') or {}
        candidate_return, comparison_parent = _returns(metrics, 'candidate'), _returns(metrics, 'champion')
        preview = transition_preview(proposal['policy'], frame, limits, context)
        paths = replay(proposal['policy'])
        qualification = joint_qualification(paths, progress, limits['budget'])
        detail = {'candidate_id': identity, 'return_gate_decision': outcome.get('decision'),
            'transition': preview, 'scenario_evidence': paths, 'qualification': qualification,
            'promotion_reason': 'evidence_unavailable', 'eligible': False, 'award_plan_supported': False}
        audit['outcomes'].append(detail)
        if (outcome.get('decision') == 'blocked' or candidate_return is None or comparison_parent is None
                or preview['status'] != 'ready'):
            continue
        profiles, current_parent_profiles = _return_profiles(metrics, 'candidate'), _return_profiles(metrics, 'champion')
        if parent_profiles is None:
            parent_profiles, parent_return = current_parent_profiles, comparison_parent
        if parent_profiles != current_parent_profiles:
            detail['promotion_reason'] = 'unpaired_parent_evidence'
            continue
        detail['relative_return_audit'] = {key: {field: profiles[key][field] - parent_profiles[key][field]
            for field in ('net_return_pct', 'stress_return_pct')} for key in profiles}
        cap = _number(metrics.get('maximum_drawdown_pct', 15.))
        if (cap is None or not 0 < cap <= 15.
                or min(candidate_return[key] for key in ('max_drawdown_pct', 'stress_drawdown_pct')) < 0
                or max(candidate_return[key] for key in ('max_drawdown_pct', 'stress_drawdown_pct')) > cap + 1e-9):
            detail['promotion_reason'] = 'drawdown_limit'
            continue
        family = _family_status(paths, qualification, plan, cap)
        category, reason = _absolute_class(qualification, family)
        progress_proof = _progress_evidence(qualification, parent_qualification, paths, parent_paths,
            candidate_family=family, parent_family=parent_family, threshold=threshold)
        if not category and progress_proof['supported']:
            category, reason = 2, 'qualified_progress'
        comparisons = list((metrics.get('scenario_comparisons') or {}).values()) or [metrics]
        reduced_risk = (float(preview['gross_buy_amount']) < float(parent_preview['gross_buy_amount'])
            and _within_return_budget(profiles, parent_profiles, RISK_RETURN_TOLERANCE_PP)
            and all(all(item['candidate'][key] <= item['champion'][key] + 1e-9
                        for key in ('max_drawdown_pct', 'stress_drawdown_pct'))
                    and any(item['candidate'][key] < item['champion'][key] - 1e-9
                            for key in ('max_drawdown_pct', 'stress_drawdown_pct')) for item in comparisons))
        immediate = preview['entry_count'] > 0
        natural = (category >= 2 and family == 'ready'
                   and any(item.get('first_buy_session') is not None for item in paths.get('scenarios', [])))
        if not immediate and not natural:
            category, reason = 0, 'no_current_or_supported_later_improvement'
        if not category and reduced_risk:
            category, reason = 1, 'risk_reduction'
        detail.update(scenario_status=family, progress_evidence=progress_proof,
            transition_class='immediate_entry' if immediate else 'after_natural_exits' if natural else 'no_entry_evidence',
            risk_reduction_evidence=reduced_risk, promotion_reason=reason, eligible=bool(category),
            award_plan_supported=category >= 3, plan_class=category)
        if category:
            assessable.append({'index': index, 'id': identity, 'class': category, 'return': candidate_return,
                'qualification': qualification, 'paths': paths})
    if parent_return is None:
        audit['reason'] = 'paired_return_evidence_unavailable'
        return None, audit
    audit.update(status='keep', reason='no_supported_plan', eligible_candidates=len(assessable))
    if not assessable:
        return None, audit
    incumbent = {'index': None, 'id': '', 'class': parent_class, 'return': parent_return,
                 'qualification': parent_qualification, 'paths': parent_paths}
    chosen = max([incumbent, *assessable], key=_plan_rank)
    if chosen['index'] is None:
        audit['reason'] = 'incumbent_tie_or_qualification_preference'
        return None, audit
    audit.update(status='apply', reason=audit['outcomes'][chosen['index']]['promotion_reason'],
                 selected_qualification=chosen['qualification']['status'], selected_plan_class=chosen['class'],
                 award_plan_supported=chosen['class'] >= 3)
    return chosen['index'], audit


