"""Read-only comparison of frozen signal/holding hypotheses on saved prices.

The current-book lane is a historical analogue, never actual future returns.
The observed lane is a retrospective one-interval replay, not a promotion gate.
Run ``prepare`` before inspecting outcomes, then ``run`` after source is frozen.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.daily_inputs import freeze_award_context
from backend.learning_policy import policy_id, validate_policy
from backend.policy_capacity import SIGNAL_FAMILIES, signal_hold_packet
from backend.learning_research import _behavior_id
from backend.policy_award import joint_qualification, _activity_preservation, _family_status, _full_worst_final_equity
from backend import learning_policy, policy_award_scenarios as scenarios
from backend.source_version import DAILY_SOURCES, source_version


VERSION = 'offline-signal-holding-comparison-v1'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read_json(path):
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix == '.gz' else raw), hashlib.sha256(raw).hexdigest()


def save_new(path, value):
    raw = canonical(value)
    with path.open('xb') as stream:
        stream.write(gzip.compress(raw, mtime=0) if path.suffix == '.gz' else raw)
    return str(path.resolve())


def controls(policy):
    result = deepcopy(policy)
    result.pop('name', None)
    for strategy in result['strategies']:
        for field in ('conditions', 'rank_by', 'descending', 'holding_sessions',
                      'exit_mode', 'max_holding_sessions'):
            strategy.pop(field, None)
    return result


def candidates(champion, frames):
    packet = signal_hold_packet(champion, frames)
    result = {}
    for family in ('current', *SIGNAL_FAMILIES):
        for hold in (1, 2, 4):
            policy = deepcopy(champion if family == 'current' else packet['members'][f'T_{family}_{hold}'])
            for strategy in policy['strategies']:
                strategy['holding_sessions'] = hold
                strategy.pop('exit_mode', None)
                strategy.pop('max_holding_sessions', None)
            result[f'{family}_hold{hold}'] = validate_policy(policy)
        extension = deepcopy(result[f'{family}_hold2'])
        for strategy in extension['strategies']:
            strategy.update(exit_mode='signal_extension', max_holding_sessions=4)
        result[f'{family}_extend2to4'] = validate_policy(extension)
    expected = controls(champion)
    assert all(controls(policy) == expected for policy in result.values()), 'non-signal/holding controls changed'
    assert result['current_hold2'] == champion, 'incumbent must be an exact reference'
    return result, packet


def coverage(frames):
    return {'frames': len(frames), 'first_day': frames[0]['as_of'], 'last_day': frames[-1]['as_of'],
            'rows_per_frame_counts': dict(Counter(str(len(f['rows'])) for f in frames)),
            'execution_quotes_per_frame_counts': dict(Counter(str(len(f.get('execution_quotes', {}))) for f in frames)),
            'development_only_frames': sum(f.get('role') == 'development_only' for f in frames),
            'frame_hashes': [{'day': f['as_of'], 'hash': digest(f)} for f in frames]}


def prepare(frozen, observed):
    prior = frozen['research_inputs']
    latest = observed['learning_frames'][0]['payload']
    recorded = observed['daily_policy_contexts'][0]['payload']
    competition = observed['daily_research_preparation_observations'][0]['payload']['competition_context']
    champion = validate_policy(observed['runs'][0]['payload']['learning_policy'])
    assert champion == validate_policy(prior['champion']), 'active policy changed between saved sources'
    assert recorded['limits'] == prior['limits']
    assert recorded['initial_state']['account_protection']['limit_pct'] == 15
    assert not recorded['initial_state']['pending_orders'] and not recorded['initial_state']['unresolved_orders']
    frames = sorted({f['as_of']: f for f in [*prior['frames'], latest]}.values(), key=lambda f: f['as_of'])[-120:]
    assert all(f['as_of'] <= latest['as_of'] for f in frames)
    context = freeze_award_context(signal_day=latest['as_of'], observed_at=recorded['observed_at'],
        latest_frame=latest, initial_state=recorded['initial_state'], execution_model=recorded['execution_model'],
        competition_context=competition, limits=recorded['limits'])
    hypotheses, packet = candidates(champion, frames)
    previous_hypotheses, previous_packet = candidates(champion, prior['frames'])
    previous_context = frozen['evaluation_context']
    assert previous_context['limits'] == recorded['limits']
    assert previous_context['initial_state']['account_protection']['limit_pct'] == 15
    observed_path = [prior['frames'][-1], latest]
    assert previous_context['initial_state']['as_of'] == observed_path[0]['as_of']
    assert datetime.fromisoformat(previous_context['observed_at']) < datetime.fromisoformat(latest['observed_at'])
    assert observed_path[0]['as_of'] < latest['as_of']
    return {'version': VERSION, 'champion': champion, 'frames': frames, 'limits': recorded['limits'],
            'context': context, 'candidates': hypotheses, 'packet': packet,
            'source_coverage': coverage(prior['frames']), 'used_coverage': coverage(frames),
            'recorded_engine_versions': {'previous': previous_context['engine_version'], 'latest': recorded['engine_version']},
            'actual_following_intervals_after_current_book': 0,
            'observed_lane': {'frames': observed_path, 'context': previous_context,
                              'candidates': previous_hypotheses, 'packet': previous_packet},
            'control_hashes': {'book': digest(recorded['initial_state']),
                'existing_lots': digest(recorded['initial_state']['positions']),
                'protection': digest(recorded['initial_state']['account_protection']),
                'limits': digest(recorded['limits']), 'policy_controls': digest(controls(champion))}}


def curve_diagnostics(metric, stress, initial_capital=None):
    prefix = 'stress_' if stress else ''
    diagnostics = metric['diagnostics']['cost_stress' if stress else 'normal']
    discount = ((1 - Decimal(str(diagnostics['slippage_bps'])) / 10000)
                * (1 - Decimal(str(diagnostics['fee_rate'])) - Decimal(str(diagnostics['tax_rate']))))
    curve = []
    for point in metric[prefix + 'equity_curve']:
        stock = Decimal(str(point['position_value'])) / discount
        nav = Decimal(str(point['cash'])) + stock
        curve.append({**point, 'close_marked_equity': float(nav), 'close_marked_stock_value': float(stock),
                      'close_marked_exposure_pct': float(stock / nav * 100) if nav else 0})
    start = curve[0]['close_marked_equity']
    result = {'net_return_pct': (curve[-1]['close_marked_equity'] / start - 1) * 100,
            'return_from_current_book_pct': (curve[-1]['close_marked_equity'] / start - 1) * 100,
            'current_book_start_nav': start,
            'average_exposure_pct': math.fsum(p['close_marked_exposure_pct'] for p in curve[1:]) / (len(curve) - 1),
            'maximum_exposure_pct': max(p['close_marked_exposure_pct'] for p in curve),
            'turnover_pct_of_start_nav': metric[prefix + 'trading_activity']['gross_amount'] / start * 100,
            'protection_triggered': any(p.get('protection_active') for p in curve),
            'equity_curve': curve, 'final_state': metric[prefix + 'final_state'],
            'diagnostics': diagnostics}
    if initial_capital is not None:
        result.update(original_capital=float(initial_capital),
                      return_from_original_capital_pct=(curve[-1]['close_marked_equity'] / float(initial_capital) - 1) * 100)
    return result


def metric_with_signals(policy, path, limits, **kwargs):
    recorded_signals = []
    native = learning_policy.signals
    def capture(value, frame, **options):
        found = native(value, frame, **options)
        recorded_signals.append({'day': frame['as_of'], 'entry_context': deepcopy(options.get('entry_context')),
                                 'eligible_signals': len(found), 'symbols': [s['symbol'] for s in found]})
        return found
    with patch.object(learning_policy, 'signals', capture):
        metric = learning_policy.portfolio_metrics(policy, path, limits, **kwargs)
    if metric.get('valid'):
        assert len(recorded_signals) == 2 * len(path), 'unexpected replay signal call structure'
        metric['signal_audit'] = {'normal': recorded_signals[:len(path)], 'cost_stress': recorded_signals[len(path):]}
    return metric


def terminal_diagnostics(cell, book, limits):
    """No interval implies no measured average exposure or holding effect."""
    nav = cell['final_equity']
    stock = nav - float(book['cash'])
    exposure = stock / nav * 100 if nav else 0
    return {'net_return_pct': 0, 'return_from_current_book_pct': 0,
        'current_book_start_nav': nav, 'original_capital': float(limits['budget']),
        'return_from_original_capital_pct': (nav / float(limits['budget']) - 1) * 100,
        'average_exposure_pct': None, 'maximum_exposure_pct': exposure,
        'turnover_pct_of_start_nav': 0, 'protection_triggered': False,
        'holding_effect_identified': False, 'reason_for_unidentified_holding_effect': 'no_following_intervals',
        'equity_curve': [{'as_of': book['as_of'], 'cash': float(book['cash']),
                         'close_marked_equity': nav, 'close_marked_stock_value': stock,
                         'close_marked_exposure_pct': exposure}],
        'final_state': deepcopy(book), 'signal_audit': [], 'next_entry_signals_total': 0,
        'buy_attempts': 0, 'limit_blocked': 0, 'diagnostics': cell['execution_diagnostics']}


def summarize_rows(rows, expected_ids):
    by_id = {row['id']: row for row in rows}
    complete = (len(by_id) == len(rows) and set(by_id) == set(expected_ids)
                and all(row['status'] == 'ready' for row in rows))
    result = {'status': 'ready' if complete else 'incomplete', 'comparable': complete,
              'expected_paths': len(expected_ids), 'ready_paths': sum(row['status'] == 'ready' for row in rows)}
    if not complete:
        result.update(mean=None,
            missing_or_unavailable=[{'id': identity, 'reason': by_id.get(identity, {}).get('reason', 'path_absent')}
                                    for identity in expected_ids if by_id.get(identity, {}).get('status') != 'ready'],
            unexpected_ids=sorted(set(by_id) - set(expected_ids)),
            duplicate_ids=sorted(identity for identity, count in Counter(row['id'] for row in rows).items() if count > 1),
            known_path_diagnostics=[{key: row[key] for key in ('id', 'environment', 'schedule', 'net_return_pct',
                'final_equity', 'max_drawdown_pct') if key in row} for row in rows if row['status'] == 'ready'])
        return result
    for environment in ('normal', 'cost_stress', 'partial_delayed', 'partial_delayed_cost_stress'):
        cells = [r for r in rows if r['environment'] == environment and r['status'] == 'ready']
        by_schedule = {}
        for schedule in sorted({r['schedule'] for r in cells}):
            selected = [r for r in cells if r['schedule'] == schedule]
            keys = ('net_return_pct', 'return_from_current_book_pct', 'return_from_original_capital_pct',
                    'current_book_start_nav', 'final_equity', 'max_drawdown_pct', 'average_exposure_pct',
                    'turnover_pct_of_start_nav', 'fees_paid', 'taxes_paid', 'gross_buy_amount', 'gross_sell_amount',
                    'next_entry_signals_total', 'buy_attempts', 'limit_blocked')
            by_schedule[schedule] = {'paths': len(selected), 'horizon': selected[0]['horizon'],
                'mean': {key: math.fsum(r[key] for r in selected) / len(selected)
                         if all(r[key] is not None for r in selected) else None for key in keys},
                'worst_return_pct': min(r['net_return_pct'] for r in selected),
                'maximum_drawdown_pct': max(r['max_drawdown_pct'] for r in selected),
                'protection_triggered_paths': sum(r['protection_triggered'] for r in selected),
                'path_ids': [r['id'] for r in selected]}
        result[environment] = by_schedule
    return result


def branch_states(states, cohort_symbols, variant, policy):
    """Change only newly entered cohort exit rules; retain the rest of each book."""
    changed = deepcopy(states)
    for environment, state in changed.items():
        original_other = [p for p in states[environment]['positions'] if p['symbol'] not in cohort_symbols[environment]]
        for position in state['positions']:
            if position['symbol'] not in cohort_symbols[environment]:
                continue
            rule = position['exit_policy']
            rule['holding_sessions'] = {'hold1': 1, 'hold2': 2, 'hold4': 4, 'extend2to4': 2}[variant]
            for key in ('exit_mode', 'max_holding_sessions', 'continuation_policy', 'continuation_strategy'):
                rule.pop(key, None)
            if variant == 'extend2to4':
                rule.update(exit_mode='signal_extension', max_holding_sessions=4,
                            continuation_policy=deepcopy(policy), continuation_strategy=position['strategy_id'])
        assert [p for p in state['positions'] if p['symbol'] not in cohort_symbols[environment]] == original_other
        assert {key: value for key, value in state.items() if key != 'positions'} == {
            key: value for key, value in states[environment].items() if key != 'positions'}
    return changed


def terminal_mark(state):
    return float(Decimal(state['cash']) + sum(Decimal(p['last_close']) * p['quantity'] for p in state['positions']))


def cohort_attribution(request, plan, account_results):
    """Paired EOD snapshots: future entries off, original non-cohort lots intact."""
    limits, context = request['limits'], request['context']
    costs = scenarios._costs(None)
    output = []
    for family in ('current', *SIGNAL_FAMILIES):
        policy = request['candidates'][family + '_hold2']
        for schedule in plan['schedules']:
            for selected in schedule['selected']:
                start, horizon = selected['source_start_index'], schedule['horizon']
                if horizon == 0:
                    output.append({'family': family, 'schedule': schedule['schedule'], 'window': selected['window'],
                        'horizon': 0, 'status': 'terminal_mark_only', 'reason': 'no_following_intervals',
                        'alias_of': selected.get('alias_of'), 'cohort_cases': []})
                    continue
                source = request['frames'][start:start + horizon + 1]
                path = scenarios._path(policy, source, context['latest_frame'], context['initial_state'])
                states = {environment: deepcopy(context['initial_state']) for environment in ('normal', 'stress')}
                curves = {'normal': [], 'stress': []}
                full_rows = {}
                for environment in ('normal', 'stress'):
                    env = 'cost_stress' if environment == 'stress' else 'normal'
                    identity = f"{selected['window']}_{schedule['schedule']}_{env}"
                    full_rows[environment] = next(cell for cell in account_results[family + '_hold2']['evidence']['scenarios'] if cell['id'] == identity)
                    assert full_rows[environment]['status'] == 'ready'
                snapshots = []
                for index in range(1, len(path)):
                    previous = deepcopy(states)
                    # Original-book prefixes retain the exact Decimal arithmetic
                    # history; restarting every interval can round the final 28th digit.
                    metric = learning_policy.portfolio_metrics(policy, path[:index+1], limits,
                        initial_state=deepcopy(context['initial_state']), execution_model={'mode': 'daily_open', **costs})
                    assert metric['valid'], metric.get('error')
                    new_symbols = {}
                    for environment in ('normal', 'stress'):
                        prefix = 'stress_' if environment == 'stress' else ''
                        states[environment] = metric[prefix + 'final_state']
                        curve = metric[prefix + 'equity_curve']
                        expected = [{key: point[key] for key in curve[0]}
                                    for point in full_rows[environment]['equity_curve'][:index+1]]
                        assert curve == expected, 'prefix baseline economic curve differs'
                        if index == 1:
                            curves[environment].append(curve[0])
                        curves[environment].append(curve[-1])
                        new_symbols[environment] = sorted({p['symbol'] for p in states[environment]['positions']}
                            - {p['symbol'] for p in previous[environment]['positions']})
                    if any(new_symbols.values()):
                        snapshots.append((index, deepcopy(states), new_symbols))
                for environment in ('normal', 'stress'):
                    full = full_rows[environment]
                    assert states[environment] == full['final_state'], 'prefix baseline final state differs'
                    restored_curve = [{key: point[key] for key in curves[environment][0]} for point in full['equity_curve']]
                    assert curves[environment] == restored_curve, 'prefix baseline economic curve differs'
                cases = []
                for index, snapshot, symbols in snapshots:
                    case = {'entry_day': path[index]['as_of'], 'remaining_intervals': horizon-index,
                        'cohort_symbols': symbols, 'entry_books': snapshot,
                        'entry_book_hashes': {env: digest(state) for env, state in snapshot.items()},
                        'existing_lots_are_not_cohort': True, 'variants': {}}
                    for variant in ('hold1', 'hold2', 'hold4', 'extend2to4'):
                        variant_policy = request['candidates'][family + '_' + variant]
                        changed = branch_states(snapshot, symbols, variant, variant_policy)
                        if index == horizon:
                            case['variants'][variant] = {env: {'status': 'terminal_mark_only',
                                'reason': 'no_following_interval_to_identify_exit_effect', 'final_equity': terminal_mark(state),
                                'fees_paid': 0, 'taxes_paid': 0, 'gross_buy_amount': 0, 'gross_sell_amount': 0,
                                'final_protection': state['account_protection'], 'final_state_hash': digest(state)}
                                for env, state in changed.items()}
                            continue
                        model = {'mode': 'daily_open', **costs,
                                 'daily_buy_caps': {frame['as_of']: '0' for frame in path[index+1:]}}
                        metric = learning_policy.portfolio_metrics(policy, path[index:], limits,
                            initial_state=changed, execution_model=model)
                        assert metric['valid'], metric.get('error')
                        case['variants'][variant] = {}
                        for environment in ('normal', 'stress'):
                            stress = environment == 'stress'
                            outcome = {**scenarios._outcome(metric, stress=stress),
                                       **curve_diagnostics(metric, stress, limits['budget'])}
                            assert outcome['gross_buy_amount'] == 0, 'cohort branch must not add entries'
                            case['variants'][variant][environment] = outcome
                    for environment in ('normal', 'stress'):
                        baseline = case['variants']['hold2'][environment]
                        for variant in case['variants'].values():
                            value = variant[environment]
                            value['delta_final_nav_vs_hold2'] = value['final_equity'] - baseline['final_equity']
                            value['protection_outcome_differs_from_hold2'] = (
                                value['final_protection']['active'] != baseline['final_protection']['active'])
                    cases.append(case)
                output.append({'family': family, 'schedule': schedule['schedule'], 'window': selected['window'],
                    'horizon': horizon, 'source_start': source[0]['as_of'], 'source_end': source[-1]['as_of'],
                    'baseline_prefixes_match_account_final_state_and_curve': True, 'cohort_cases': cases})
                print(json.dumps({'stage': 'cohort', 'family': family, 'schedule': schedule['schedule'],
                                  'window': selected['window'], 'entry_cohorts': len(cases)}), flush=True)
    return {'basis': 'same_entry_EOD_book_exit_counterfactual_not_additive_account_returns',
            'additional_entries': 0, 'overlapping_deltas_must_not_be_summed': True,
            'protection_can_affect_noncohort_lots': True, 'paths': output}


def run(request, output_dir, stamp):
    before = digest(request)
    engine = source_version(DAILY_SOURCES)
    context, limits = request['context'], request['limits']
    plan = scenarios.scenario_plan(request['frames'], context)
    assert plan['status'] == 'ready', plan.get('reason')
    expected_ids = [f"{window['window']}_{schedule['schedule']}_{environment}"
                    for schedule in plan['schedules'] for window in schedule['selected']
                    for environment in ('normal', 'cost_stress', 'partial_delayed', 'partial_delayed_cost_stress')]
    source_files = {name: hashlib.sha256((Path(__file__).resolve().parents[1] / 'backend' / name).read_bytes()).hexdigest()
                    for name in DAILY_SOURCES}
    script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    verification_file = Path(__file__).resolve().parents[1] / 'tests' / 'test_compare_signal_holding.py'
    verification_hash = hashlib.sha256(verification_file.read_bytes()).hexdigest()
    def aliases(identity):
        groups = {}
        for label, policy in request['candidates'].items():
            groups.setdefault(identity(policy), []).append(label)
        return {'unique_count': len(groups), 'groups': groups}
    candidate_aliases = {'labels': len(request['candidates']),
                         'policy_id': aliases(policy_id), 'behavior_id': aliases(_behavior_id)}
    preregistration = {'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
        'engine_version': engine, 'source_files': source_files, 'script_hash': script_hash, 'request_hash': before,
        'verification_file_sha256': verification_hash,
        'candidate_ids': {label: policy_id(policy) for label, policy in request['candidates'].items()},
        'candidate_aliases': candidate_aliases,
        'control_hashes': request['control_hashes'], 'scenario_plan': plan,
        'cost_profile': scenarios._costs(None),
        'comparisons': ['hold2: current/trend/relative/recovery/liquid',
            'within each signal rule: fixed1/fixed2/fixed4/conditional profitable continuation2to4'],
        'portfolio_semantics': 'continuous; no weekly reset; existing lots keep entry exit rules; same entry rules do not imply identical filled streams',
        'ranking_rule': 'report every cell, no winner activation; only all required paths ready permits a comparable mean; no missing-path exclusion or reweighting; means separated by schedule and cost/fill environment',
        'secondary_core_objective': 'J is the equally weighted mean of the same 12 core normal/cost_stress paths across full/shortened; delta J vs current_hold2; separate from same-horizon return tables',
        'qualification_diagnostics': 'native joint qualification/activity-preservation checks are descriptive only; no actual paired/live adoption decision',
        'expected_path_ids': expected_ids,
        'observed_lane_model': {'mode': 'observed_quotes', 'fill_ratio': 1, 'slippage_bps': 0,
                               'stress_slippage_bps': 0, 'stress_fee_rate': str(learning_policy.FEE + Decimal('.001'))},
        'cohort_exit_design': {'baseline': 'each signal family hold2 normal/cost_stress continuous account path',
            'anchor': 'EOD full-book snapshot on every new-entry day; same-day new lots form a joint cohort',
            'variants': ['hold1', 'hold2', 'hold4', 'extend2to4'],
            'future_entries': 'zero daily_buy_caps after the entry snapshot; no engine change',
            'unchanged': 'cash, prices, quantities, other lots and their exits, limits, persistent 15% units/peak',
            'cost_environments': 'normal/stress retain their own distinct baseline books and entry cohorts',
            'verification': 'each original-book prefix economic curve must exactly equal the full-account prefix; final state must exactly equal the full account final state',
            'limitations': ['Overlapping branch delta NAVs are not additive and are not actual account returns.',
                '15% protection may propagate a cohort exit change to other lots; report that occurrence.',
                'Remaining zero intervals permits terminal close marking only, no exit-effect identification.',
                'Unsold positions are marked at close, never forcibly sold at the terminal boundary.']},
        'limitations': ['Historical relative-price analogues with assumed future dates, not actual subsequent realized returns.',
            'Earlier frame provenance includes current-universe eligibility and later-adjusted history; development only, not point-in-time or out-of-sample.',
            'Overlapping paths are not independent observations or probabilities.',
            'Thresholds are frozen before outcome evaluation but reuse development observations.',
            'Daily-open assumed fills and cost stresses do not prove broker execution.',
            'Observed one-interval lane uses its own prior-day candidate thresholds and preserved original engine metadata; rerun engine differs and live_eligible=false.',
            'NAV is cash plus unsold close value after actual modelled trading costs; no hypothetical terminal sale.',
            '15% account protection remains path-dependent and can stop entries; no limit or reserve is relaxed.']}
    prereg_path = save_new(output_dir / f'signal-holding-preregistered-{stamp}.json', preregistration)
    print(json.dumps({'stage': 'preregistered', 'path': prereg_path, 'candidates': len(request['candidates'])}), flush=True)
    results = {}
    for index, (label, policy) in enumerate(request['candidates'].items(), 1):
        captured = {}
        def capture(value, path, model_limits, **kwargs):
            metric = metric_with_signals(value, path, model_limits, **kwargs)
            captured[(scenarios._hash(path), kwargs['execution_model'].get('fill_ratio') == .5)] = metric
            return metric
        with patch.object(scenarios, 'portfolio_metrics', capture):
            evidence = scenarios.award_scenarios(policy, request['frames'], limits, context, plan=plan)
        for cell in evidence['scenarios']:
            if cell['status'] != 'ready':
                continue
            if cell['horizon'] == 0:
                cell.update(terminal_diagnostics(cell, context['initial_state'], limits))
                continue
            stress = cell['environment'].endswith('cost_stress')
            partial = cell['environment'].startswith('partial_delayed')
            metric = captured[(cell['path_hash'], partial)]
            cell.update(curve_diagnostics(metric, stress, limits['budget']))
            cell['signal_audit'] = metric['signal_audit']['cost_stress' if stress else 'normal']
            cell['next_entry_signals_total'] = sum(p['eligible_signals'] for p in cell['signal_audit'][:-1])
            cell.update({key: cell['diagnostics'][key] for key in ('buy_attempts', 'limit_blocked')})
        evidence.pop('scenario_plan', None)
        results[label] = {'policy_id': policy_id(policy), 'policy': policy, 'evidence': evidence,
                          'summary': summarize_rows(evidence['scenarios'], expected_ids)}
        assert digest(context['initial_state']) == request['control_hashes']['book']
        print(json.dumps({'stage': 'analogue', 'completed': index, 'total': len(request['candidates']),
                          'label': label, 'ready': evidence.get('ready_scenarios')}), flush=True)
    for value in results.values():
        value['qualification_diagnostic'] = joint_qualification(value['evidence'], context['competition_context'], limits['budget'])
        rows = value['evidence']['scenarios']
        core = [row for row in rows if row['environment'] in ('normal', 'cost_stress')]
        value['objective_diagnostic'] = {'not_an_adoption_decision': True,
            'J_core_mean_nav': math.fsum(row['final_equity'] for row in core) / 12 if value['summary']['comparable'] else None,
            'worst_full_nav': _full_worst_final_equity(value['evidence']),
            'native_family_status': _family_status(value['evidence'], value['qualification_diagnostic'], plan, 15),
            'risk_veto_paths': [row['id'] for row in rows if row['status'] == 'ready' and (
                row['max_drawdown_pct'] > 15 + 1e-9 or row.get('basis') == scenarios.TERMINAL_BASIS
                and (row.get('cumulative_protection_active') or row.get('cumulative_protection_drawdown_pct', 0) >= 15))]}
    reference = results['current_hold2']
    for value in results.values():
        candidate_j, parent_j = value['objective_diagnostic']['J_core_mean_nav'], reference['objective_diagnostic']['J_core_mean_nav']
        value['objective_diagnostic']['delta_J_vs_current_hold2'] = (
            candidate_j - parent_j if candidate_j is not None and parent_j is not None else None)
        value['activity_veto_diagnostic'] = _activity_preservation(value['qualification_diagnostic'], reference['qualification_diagnostic'])
    cohorts = cohort_attribution(request, plan, results)
    observed = {}
    lane = request['observed_lane']
    model = preregistration['observed_lane_model']
    for label, policy in lane['candidates'].items():
        metric = metric_with_signals(policy, lane['frames'], limits,
            initial_state=deepcopy(lane['context']['initial_state']), execution_model=model)
        observed[label] = {'policy_id': policy_id(policy), 'valid': metric['valid'], 'error': metric.get('error'),
                           'live_eligible': False, 'basis': 'retrospective_observed_prices_assumed_fills'}
        if metric['valid']:
            for stress in (False, True):
                observed[label]['cost_stress' if stress else 'normal'] = {
                    **scenarios._outcome(metric, stress=stress), **curve_diagnostics(metric, stress, limits['budget']),
                    'signal_audit': metric['signal_audit']['cost_stress' if stress else 'normal']}
    assert digest(request) == before, 'frozen request mutated'
    assert source_version(DAILY_SOURCES) == engine, 'source changed during comparison'
    assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == script_hash, 'comparison script changed during run'
    assert hashlib.sha256(verification_file.read_bytes()).hexdigest() == verification_hash, 'verification file changed during run'
    for name, expected in request['source_files'].items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected, 'original source changed during run'
    result = {'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
        'preregistration': prereg_path, 'preregistration_hash': digest(preregistration), 'engine_version': engine,
        'control_hashes_unchanged': True, 'request_unchanged': True, 'sources_unchanged': True,
        'all_candidate_paths_comparable': all(value['summary']['comparable'] for value in results.values()),
        'candidate_aliases': candidate_aliases,
        'operational_changes': False, 'live_api_calls': False, 'actual_future_intervals': 0,
        'source_coverage': request['source_coverage'], 'used_coverage': request['used_coverage'],
        'scenario_plan': plan, 'analogue_comparisons': results,
        'cohort_exit_attribution': cohorts,
        'observed_lane': {'start': lane['frames'][0]['as_of'], 'end': lane['frames'][-1]['as_of'],
            'intervals': 1, 'recorded_engine_versions': request['recorded_engine_versions'],
            'source_book_hash': digest(lane['context']['initial_state']), 'results': observed}}
    result_path = save_new(output_dir / f'signal-holding-result-{stamp}.json.gz', result)
    summary_path = save_new(output_dir / f'signal-holding-summary-{stamp}.json', {
        'result': result_path, 'preregistration': prereg_path, 'engine_version': engine,
        'all_candidate_paths_comparable': result['all_candidate_paths_comparable'],
        'candidate_aliases': candidate_aliases,
        'analogue_comparisons': {label: value['summary'] for label, value in results.items()},
        'qualification_and_objective_diagnostics': {label: {
            'qualification': {key: value['qualification_diagnostic'].get(key) for key in
                              ('status', 'evaluated_scenarios', 'qualifying_scenarios', 'groups')},
            'objective': value['objective_diagnostic'], 'activity_veto': value['activity_veto_diagnostic']}
            for label, value in results.items()},
        'observed_lane': {label: {key: value[key] for key in ('valid', 'error', 'live_eligible')} |
            {env: {key: value[env][key] for key in ('net_return_pct', 'max_drawdown_pct', 'gross_buy_amount',
                'gross_sell_amount', 'average_exposure_pct', 'fees_paid', 'taxes_paid')} for env in ('normal', 'cost_stress') if env in value}
            for label, value in observed.items()}})
    print(json.dumps({'stage': 'complete', 'result': result_path, 'summary': summary_path}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prepare_args = sub.add_parser('prepare')
    prepare_args.add_argument('--frozen', type=Path, required=True)
    prepare_args.add_argument('--observed', type=Path, required=True)
    run_args = sub.add_parser('run')
    run_args.add_argument('--request', type=Path, required=True)
    for child in (prepare_args, run_args):
        child.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_root = args.output_dir.resolve()
    def guard(event, arguments):
        if event.startswith(('socket.', 'sqlite3.connect', 'subprocess.')) or event in {'os.system', 'os.spawn', 'os.exec'}:
            raise PermissionError('offline comparison forbids network, databases, and processes')
        if event == 'open':
            path, mode, flags = arguments
            writable = isinstance(mode, str) and any(c in mode for c in 'wax+') or bool(
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writable and isinstance(path, (str, bytes, os.PathLike)) and not Path(os.fsdecode(path)).resolve().is_relative_to(output_root):
                raise PermissionError('offline comparison write outside output directory')
    sys.addaudithook(guard)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    if args.command == 'prepare':
        frozen, frozen_hash = read_json(args.frozen)
        observed, observed_hash = read_json(args.observed)
        request = prepare(frozen, observed)
        request['source_files'] = {str(args.frozen.resolve()): frozen_hash, str(args.observed.resolve()): observed_hash}
        assert digest(request) == digest(json.loads(canonical(request))), 'request hash is not JSON roundtrip stable'
        path = save_new(output_root / f'signal-holding-request-{stamp}.json.gz', request)
        print(json.dumps({'stage': 'prepared', 'request': path, 'request_hash': digest(request),
                          'control_hashes': request['control_hashes'],
                          'coverage': {key: value for key, value in request['used_coverage'].items()
                                       if key != 'frame_hashes'}}, ensure_ascii=False))
    else:
        request, _ = read_json(args.request)
        for name, expected in request['source_files'].items():
            assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected, 'original source changed'
        run(request, output_root, stamp)


if __name__ == '__main__':
    main()
