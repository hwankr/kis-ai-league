"""Award-aware selection on frozen evidence; never an order or win probability.

Historical return comparison and current-book transition are separate. A
qualification witness must meet every condition on the SAME scenario. Unknown
inputs stay unknown, and volume above a qualification threshold earns no reward.
"""
from copy import deepcopy
import math

from backend.daily_inputs import validate_award_context
from backend.policy_transition import transition_preview


VERSION = "award-selection-v5-terminal-mark"
# Relative backtest return and the former promotion classes are diagnostics.
RISK_RETURN_TOLERANCE_PP = 0.0
NAV_EPSILON = 1e-6
CONTESTS = {
    "kis_ai_2026": {"rules_version": "kis-2026-reviewed-2026-10-10",
        "start_date": "2026-10-12", "end_date": "2026-11-20",
        "targets": {"gross_amount": 500000000, "trading_days": 5, "symbols": 5, "api_orders": 5},
        "fee_rate": .000140527, "sell_tax_rate": .002,
        "objective": "official_top_five_return_pursuit_with_preserved_qualification_and_protection"},
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
            "zero_session_shortening": "terminal_mark_with_three_aliases_original_weights",
            "full_worst_final_equity_role": "diagnostic_only_original_risk_gate_unchanged",
            "risk_limit_pct": 15., "existing_lot_rules": "preserved",
            "relative_return_role": "paired_validity_and_opportunity_cost_audit",
            "risk_reduction_return_tolerance_pp": RISK_RETURN_TOLERANCE_PP,
            "core_environments": ["normal", "cost_stress"],
            "reserve_environments": ["partial_delayed", "partial_delayed_cost_stress"],
            "objective": "core_equal_weight_mean_final_nav",
            "return_objective": {"environments": ["normal", "cost_stress"],
                "path_count": 12, "weights": "equal", "aggregation": "math_fsum_canonical_path_ids",
                "meaning": "fixed_scenario_diagnostic_not_probability_or_expected_live_return",
                "minimum_improvement": NAV_EPSILON, "incumbent_tie": "keep",
                "candidate_tie": "lexicographically_smallest_id_within_nav_epsilon"},
            "target_rank_max": 5, "actual_rank": None, "fifth_place_return_pct": None,
            "automatic_rank_defense": False,
            "promotion_order": ["core_equal_weight_mean_final_nav"],
            "diagnostic_classes": ["joint_qualification_robust", "joint_qualification_conditional", "partial_joint_challenge", "qualified_progress", "risk_reduction"],
            "relative_risk_gate": "worst_final_equity_not_below_frozen_incumbent",
            "diagnostic_partial_joint_gate": "preserve_and_add_core_joint_witnesses_with_qualified_progress",
            "activity_gate": "each_core_path_each_activity_deficit_not_worse_than_incumbent",
            "core_witness_gate": "preserve_incumbent_joint_core_path_ids",
            "legacy_promotion_role": "diagnostic_only_not_eligibility_or_ranking",
            "hypothetical_all_path_principal_floor": False,
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


def _valid_paired_outcome(outcome):
    metrics = outcome.get('metrics')
    return (outcome.get('decision') in {'apply', 'keep'} and isinstance(metrics, dict)
            and 'error' not in metrics and _returns(metrics, 'candidate') is not None
            and _returns(metrics, 'champion') is not None)


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
    from backend.policy_award_scenarios import TERMINAL_BASIS, ZERO_ACTIVITY
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
    terminal_aliases = {}
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
            if record.get('horizon') == 0:
                if (plan.get('version') not in {'quality-only-common-windows-v2', 'intraday-quality-only-common-windows-v2'}
                        or type(record.get('horizon')) is not int
                        or type(plan.get('remaining_sessions')) is not int or plan['remaining_sessions'] not in (1, 2)
                        or record['schedule'] != 'shortened'
                        or record.get('basis') != TERMINAL_BASIS or record.get('unique_path_count') != 1
                        or source.get('basis') != TERMINAL_BASIS or source.get('alias_of') != 'first_shortened'
                        or source.get('source_start_index') is not None or type(source.get('source_intervals')) is not int
                        or source['source_intervals'] != 0):
                    return 'scenario_coverage_unknown'
                for item in group:
                    if (item.get('basis') != TERMINAL_BASIS or type(item.get('horizon')) is not int or item['horizon'] != 0
                            or type(item.get('source_intervals')) is not int or item['source_intervals'] != 0 or item.get('source_start') is not None
                            or item.get('source_end') is not None
                            or item.get('alias_of') != 'first_shortened_' + item['environment']
                            or any(_number((item.get('future_activity') or {}).get(key)) != 0 for key in ZERO_ACTIVITY)):
                        return 'scenario_coverage_unknown'
                    signature = {key: value for key, value in item.items() if key not in {'id', 'window'}}
                    previous = terminal_aliases.setdefault(item['environment'], signature)
                    if signature != previous:
                        return 'scenario_coverage_unknown'
                    if plan['version'] == 'quality-only-common-windows-v2' and (
                            any(_number(item.get(key)) != 0 for key in ZERO_ACTIVITY)
                            or item.get('first_buy_session') is not None or item.get('max_drawdown_pct') != 0):
                        return 'scenario_coverage_unknown'
                    if plan['version'] == 'intraday-quality-only-common-windows-v2':
                        prefix = item.get('intraday_prefix')
                        preserved = ('final_equity', 'max_drawdown_pct', 'gross_amount', 'gross_buy_amount',
                            'gross_sell_amount', 'trading_days', 'symbols', 'api_orders', 'fees_paid', 'taxes_paid',
                            'buy_fill_events', 'sell_fill_events', 'final_holding_count', 'final_state_hash',
                            'final_protection', 'execution_diagnostics')
                        if (not isinstance(prefix, dict) or prefix.get('status') != 'ready'
                                or any(key not in prefix or item.get(key) != prefix[key] for key in preserved)
                                or item.get('post_prefix_book_hash') != prefix['final_state_hash']
                                or item.get('intraday_modelled_fills_are_actual') is not False
                                or item.get('first_buy_session') != (0 if prefix['gross_buy_amount'] else None)):
                            return 'scenario_coverage_unknown'
    for item in supplied:
        if (item.get('status', 'ready') != 'ready'
                or not item['id'].endswith('_' + str(item.get('environment')))
                or 'first_buy_session' not in item
                or any(_number(item.get(key)) is None for key in
                       ('gross_amount', 'trading_days', 'symbols', 'final_equity', 'max_drawdown_pct'))):
            return 'scenario_coverage_unknown'
        first_buy = item['first_buy_session']
        prefix_buy = (plan.get('version') == 'intraday-quality-only-common-windows-v2'
            and item.get('intraday_modelled_fills_are_actual') is False
            and (_number((item.get('intraday_prefix') or {}).get('gross_buy_amount')) or 0) > 0)
        if first_buy is not None and (type(first_buy) is not int or first_buy < (0 if prefix_buy else 1)
                or type(item.get('horizon')) is not int or first_buy > item['horizon']):
            return 'scenario_coverage_unknown'
        if any(float(item[key]) < 0 for key in ('gross_amount', 'trading_days', 'symbols', 'final_equity')):
            return 'scenario_coverage_unknown'
        if not 0 <= float(item['max_drawdown_pct']) <= cap + 1e-9:
            return 'scenario_risk_limit'
        if item.get('basis') == TERMINAL_BASIS:
            drawdown = _number(item.get('cumulative_protection_drawdown_pct'))
            limit = _number(item.get('cumulative_protection_limit_pct'))
            active = item.get('cumulative_protection_active')
            final = item.get('final_protection') or {}
            if (drawdown is None or drawdown < 0 or limit != 15.
                    or type(active) is not bool or type(final.get('active')) is not bool
                    or _number(final.get('limit_pct')) != limit
                    or active != (final['active'] or drawdown >= limit)):
                return 'scenario_coverage_unknown'
            if active or drawdown >= min(cap, limit):
                return 'scenario_risk_limit'
    return 'ready'


def _absolute_class(qualification, family):
    if family != 'ready':
        return 0, family
    if qualification['status'] == 'all_met':
        return 5, 'joint_qualification_robust'
    if qualification['groups']['core']['status'] == 'all_met':
        return 4, 'joint_qualification_conditional'
    return 0, 'qualification_shortfall'


def _relative_risk(candidate_paths, parent_paths, candidate_family, parent_family):
    result = {'supported': False, 'reason': 'paired_scenario_evidence_unavailable'}
    if candidate_family != 'ready' or parent_family != 'ready':
        return result
    left = {item['id']: item for item in candidate_paths['scenarios']}
    right = {item['id']: item for item in parent_paths['scenarios']}
    if (left.keys() != right.keys()
            or candidate_paths.get('scenario_plan_hash') != parent_paths.get('scenario_plan_hash')
            or any(left[key].get('path_hash') != right[key].get('path_hash')
                   or left[key].get('start_book_hash') != right[key].get('start_book_hash') for key in left)):
        return result
    candidate_worst = min(item['final_equity'] for item in left.values())
    parent_worst = min(item['final_equity'] for item in right.values())
    deltas = {key: left[key]['final_equity'] - right[key]['final_equity'] for key in left}
    result.update(candidate_worst_final_equity=candidate_worst, incumbent_worst_final_equity=parent_worst,
        worst_equity_improvement=candidate_worst - parent_worst,
        worst_same_path_equity_delta=min(deltas.values()),
        same_path_equity_worsened=sorted(key for key, value in deltas.items() if value < -1e-6),
        supported=candidate_worst + 1e-6 >= parent_worst,
        reason='worst_equity_nonworse' if candidate_worst + 1e-6 >= parent_worst else 'worst_equity_worsened')
    return result


def _core_witnesses(qualification):
    return {item['id'] for item in qualification['scenarios'] if item['group'] == 'core' and item['joint_met'] is True}


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
    relative = _relative_risk(candidate_paths, parent_paths, candidate_family, parent_family)
    result['relative_risk'] = relative
    if not relative['supported']:
        result['reason'] = relative['reason']
        return result
    if any(any(item['checks'][key] is not True for key in ('trading_days', 'symbols', 'api_orders'))
           for item in proposed.values()):
        result['reason'] = 'non_turnover_qualification_failed'
        return result
    if not _core_witnesses(parent) <= _core_witnesses(candidate):
        result['reason'] = 'incumbent_core_joint_witness_lost'
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


def _return_objective(paths, family):
    if family != 'ready':
        return None
    core = sorted((row for row in paths['scenarios']
                   if row['environment'] in {'normal', 'cost_stress'}), key=lambda row: row['id'])
    values = [_number(row.get('final_equity')) for row in core]
    if len(values) != 12 or any(value is None for value in values):
        return None
    return math.fsum(values) / 12


def _full_worst_final_equity(paths):
    """Expose full-horizon downside without changing the original family veto."""
    full = [row for row in paths.get('scenarios', []) if row.get('schedule') == 'full']
    values = [_number(row.get('final_equity')) for row in full]
    if len(full) != 12 or any(row.get('status', 'ready') != 'ready' for row in full) or None in values:
        return None
    return min(values)


def _activity_preservation(candidate, parent):
    """Preserve every core path's four activity deficits, never average them."""
    left = {row['id']: row for row in candidate['scenarios'] if row['group'] == 'core'}
    right = {row['id']: row for row in parent['scenarios'] if row['group'] == 'core'}
    result = {'supported': False, 'reason': 'core_activity_evidence_unavailable', 'worsened': [], 'unknown': []}
    if len(left) != 12 or left.keys() != right.keys():
        return result
    for identity in sorted(left):
        for field in ('gross_amount', 'trading_days', 'symbols', 'api_orders'):
            proposed = _number(left[identity]['deficits'].get(field))
            incumbent = _number(right[identity]['deficits'].get(field))
            if proposed is None or incumbent is None or min(proposed, incumbent) < 0:
                result['unknown'].append({'id': identity, 'requirement': field})
            elif proposed > incumbent + NAV_EPSILON:
                result['worsened'].append({'id': identity, 'requirement': field,
                                          'candidate_deficit': proposed, 'incumbent_deficit': incumbent})
    if result['unknown']:
        return result
    if result['worsened']:
        result['reason'] = 'core_activity_shortfall_worsened'
        return result
    result.update(supported=True, reason='core_activity_shortfall_preserved')
    return result


def _select_award_record(champion, records, frames, limits, context, *, evidence=None):
    """Maximize the fixed core NAV mean within paired qualification/risk gates."""
    from backend.policy_award_scenarios import award_scenarios, scenario_plan
    context_validator, previewer = validate_award_context, transition_preview
    context_day = context.get('signal_day') if isinstance(context, dict) else None
    if isinstance(context, dict) and context.get('version') == 'intraday-award-context-v1':
        from backend.intraday_scenarios import (validate_intraday_context, transition_preview as intraday_preview,
                                                award_scenarios as intraday_scenarios, scenario_plan as intraday_plan)
        context_validator, previewer = validate_intraday_context, intraday_preview
        award_scenarios, scenario_plan = intraday_scenarios, intraday_plan
        context_day = context.get('activation_day')
    audit = {**selection_contract(), 'status': 'blocked', 'reason': 'invalid_award_context',
             'incumbent_included': True, 'outcomes': [], 'timefolio': separate_contest_status()}
    if (not context_validator(context) or context['limits'] != limits
            or not isinstance(frames, list) or not frames or frames[-1] != context['latest_frame']):
        return None, audit
    progress = context['competition_context']
    if (not isinstance(progress, dict) or progress.get('contest') != 'kis_ai_2026'
            or not progress.get('reliable') or progress.get('period', {}).get('as_of') != context_day):
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
    parent_preview = previewer(champion, frame, limits, context)
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
        if evidence is not None:
            from backend.learning_policy import policy_id
            return deepcopy(evidence[policy_id(policy)])
        calculated = award_scenarios(policy, frames, limits, context, plan=plan)
        return {key: value for key, value in calculated.items() if key != 'scenario_plan'}
    parent_paths = replay(champion)
    parent_qualification = joint_qualification(parent_paths, progress, limits['budget'])
    parent_family = _family_status(parent_paths, parent_qualification, plan, 15.)
    parent_class, parent_reason = _absolute_class(parent_qualification, parent_family)
    audit.update(incumbent_scenarios=parent_paths, incumbent_qualification=parent_qualification,
                 incumbent_plan_class=parent_reason, incumbent_scenario_status=parent_family)
    parent_objective = _return_objective(parent_paths, parent_family)
    audit['incumbent_objective_value'] = parent_objective
    audit['incumbent_full_worst_final_equity'] = _full_worst_final_equity(parent_paths)
    parent_return = parent_profiles = None
    # Anchor paired comparisons by fixed identity, never caller-provided order.
    for _, _, outcome in sorted(records, key=lambda record: record[1]):
        metrics = outcome.get('metrics') or {}
        if _valid_paired_outcome(outcome):
            parent_profiles = _return_profiles(metrics, 'champion')
            parent_return = _returns(metrics, 'champion')
            break
    assessable = []
    for index, (proposal, identity, outcome) in enumerate(records):
        metrics = outcome.get('metrics') if isinstance(outcome.get('metrics'), dict) else {}
        candidate_return, comparison_parent = _returns(metrics, 'candidate'), _returns(metrics, 'champion')
        preview = previewer(proposal['policy'], frame, limits, context)
        paths = replay(proposal['policy'])
        qualification = joint_qualification(paths, progress, limits['budget'])
        detail = {'candidate_id': identity, 'return_gate_decision': outcome.get('decision'),
            'transition': preview, 'scenario_evidence': paths, 'qualification': qualification,
            'promotion_reason': 'evidence_unavailable', 'eligible': False, 'award_plan_supported': False,
            'qualification_risk': True}
        audit['outcomes'].append(detail)
        if not _valid_paired_outcome(outcome) or preview['status'] != 'ready':
            continue
        profiles, current_parent_profiles = _return_profiles(metrics, 'candidate'), _return_profiles(metrics, 'champion')
        if parent_profiles != current_parent_profiles or profiles.keys() != parent_profiles.keys():
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
        objective = _return_objective(paths, family)
        category, reason = _absolute_class(qualification, family)
        progress_proof = _progress_evidence(qualification, parent_qualification, paths, parent_paths,
            candidate_family=family, parent_family=parent_family, threshold=threshold)
        relative_risk = _relative_risk(paths, parent_paths, family, parent_family)
        if not category and progress_proof['supported']:
            category, reason = 2, 'qualified_progress'
            old_witnesses, new_witnesses = _core_witnesses(parent_qualification), _core_witnesses(qualification)
            if old_witnesses < new_witnesses:
                category, reason = 3, 'partial_joint_challenge'
        comparisons = list((metrics.get('scenario_comparisons') or {}).values()) or [metrics]
        reduced_risk = (float(preview['gross_buy_amount']) < float(parent_preview['gross_buy_amount'])
            and _within_return_budget(profiles, parent_profiles, RISK_RETURN_TOLERANCE_PP)
            and all(all(item['candidate'][key] <= item['champion'][key] + 1e-9
                        for key in ('max_drawdown_pct', 'stress_drawdown_pct'))
                    and any(item['candidate'][key] < item['champion'][key] - 1e-9
                            for key in ('max_drawdown_pct', 'stress_drawdown_pct')) for item in comparisons))
        immediate = preview['entry_count'] > 0
        natural = (family == 'ready'
                   and any(item.get('first_buy_session') is not None for item in paths.get('scenarios', [])))
        if not immediate and not natural:
            category, reason = 0, 'no_current_or_supported_later_improvement'
        if not category and reduced_risk:
            category, reason = 1, 'risk_reduction'
        activity = _activity_preservation(qualification, parent_qualification)
        lost_witnesses = sorted(_core_witnesses(parent_qualification) - _core_witnesses(qualification))
        witnesses = {'supported': not lost_witnesses, 'lost': lost_witnesses}
        supported = False
        if not relative_risk['supported']:
            promotion = relative_risk['reason']
        elif not activity['supported']:
            promotion = activity['reason']
        elif not witnesses['supported']:
            promotion = 'incumbent_core_joint_witness_lost'
        elif objective is None or parent_objective is None:
            promotion = 'return_objective_unavailable'
        elif objective <= parent_objective + NAV_EPSILON:
            promotion = 'return_objective_not_improved'
        else:
            supported = True
            promotion = 'return_improvement' if qualification['groups']['core']['status'] == 'all_met' else 'return_challenge'
        detail.update(scenario_status=family, progress_evidence=progress_proof,
            relative_risk_evidence=relative_risk,
            transition_class='immediate_entry' if immediate else 'after_natural_exits' if natural else 'no_entry_evidence',
            risk_reduction_evidence=reduced_risk, diagnostic_reason=reason,
            promotion_reason=promotion, eligible=supported,
            activity_preservation=activity, core_witness_preservation=witnesses,
            objective_value=objective,
            full_worst_final_equity=_full_worst_final_equity(paths),
            objective_improvement=objective - parent_objective if objective is not None and parent_objective is not None else None,
            award_plan_supported=family == 'ready' and qualification['groups']['core']['status'] == 'all_met',
            qualification_risk=family != 'ready' or qualification['groups']['core']['status'] != 'all_met',
            plan_class=category)
        if supported:
            assessable.append({'index': index, 'id': identity, 'class': category, 'return': candidate_return,
                'qualification': qualification, 'paths': paths, 'objective': objective})
    if parent_return is None:
        audit['reason'] = 'paired_return_evidence_unavailable'
        return None, audit
    audit.update(status='keep', reason='no_supported_return_improvement', eligible_candidates=len(assessable),
                 qualification_risk=parent_class < 4, decision_needed=not assessable and parent_class < 4,
                 incumbent_has_supported_qualification_plan=parent_class >= 4)
    if not assessable:
        return None, audit
    best_objective = max(item['objective'] for item in assessable)
    chosen = min((item for item in assessable if best_objective - item['objective'] <= NAV_EPSILON),
                 key=lambda item: item['id'])
    supported_plan = chosen['qualification']['groups']['core']['status'] == 'all_met'
    audit.update(status='apply', reason=audit['outcomes'][chosen['index']]['promotion_reason'],
                 selected_qualification=chosen['qualification']['status'], selected_plan_class=chosen['class'],
                 award_plan_supported=supported_plan, selected_objective_value=chosen['objective'],
                 objective_improvement=chosen['objective'] - parent_objective,
                 qualification_risk=not supported_plan, decision_needed=False)
    return chosen['index'], audit


def select_award_record(champion, records, frames, limits, context):
    """Run the native full scenario evaluation and apply the selection contract."""
    return _select_award_record(champion, records, frames, limits, context)


def verify_award_record_evidence(champion, records, frames, limits, context, audit):
    """Recompute gates/rank from a sealed native replay without simulating again.

    This verifies the decision, not authenticity of supplied path returns. The
    caller must additionally bind the immutable replay artifact and exact runtime.
    Context, common plan and current entry preview are recomputed normally.
    """
    from backend.learning_policy import policy_id
    if not isinstance(audit, dict) or audit.get('version') != VERSION:
        raise ValueError('award_evidence_contract_changed')
    outcomes = audit.get('outcomes')
    if (not isinstance(outcomes, list) or len(outcomes) != len(records)
            or [row.get('candidate_id') for row in outcomes] != [row[1] for row in records]):
        raise ValueError('award_evidence_pool_changed')
    evidence = {row['candidate_id']: row['scenario_evidence'] for row in outcomes}
    parent_id = policy_id(champion)
    if parent_id in evidence and evidence[parent_id] != audit['incumbent_scenarios']:
        raise ValueError('award_evidence_incumbent_changed')
    evidence[parent_id] = audit['incumbent_scenarios']
    index, reproduced = _select_award_record(champion, records, frames, limits, context, evidence=evidence)
    if reproduced != audit:
        raise ValueError('award_evidence_decision_mismatch')
    return index, reproduced


