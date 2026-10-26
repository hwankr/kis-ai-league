"""Seal fixed policies at today's close, then append saved session observations.

seal --request comparison.json.gz --output-dir evidence
advance --experiment experiment.json --state state.json.gz --observed export.json
        [export2.json ...] --output-dir evidence

Offline diagnostics only. No activation, account writes, or automatic scheduling.
Each update replays the entire path from the sealed book, preserving each shadow's
cash, lots, exit rules, pending fills and cumulative protection across sessions.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.daily_inputs import validate_award_context
from backend.kis import KST
from backend.learning_policy import FEE, validate_policy
from backend.source_version import DAILY_SOURCES, source_version
from scripts.compare_signal_holding import (canonical, controls, curve_diagnostics, digest,
    install_offline_guard, metric_with_signals, read_json, save_new, validate_observed_path)


VERSION = 'offline-continuous-policy-experiment-v1'


def sources():
    return {'engine': source_version(DAILY_SOURCES),
            'tools': source_version(('compare_signal_holding.py', 'observe_policy_experiment.py'),
                                    root=Path(__file__).parent)}


def _instant(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.utcoffset() is None:
        raise ValueError('aware_timestamp_required')
    return result


def _with_hash(value):
    return {**value, 'hash': digest(value)}


def _check_hash(value, name):
    if value.get('hash') != digest({key: item for key, item in value.items() if key != 'hash'}):
        raise ValueError(name + '_hash_changed')


def seal(request, *, now=None):
    now = _instant(now or datetime.now(timezone.utc))
    context = request['context']
    if not validate_award_context(context):
        raise ValueError('invalid_award_context')
    anchor, book = context['latest_frame'], context['initial_state']
    validate_observed_path([anchor])
    if (now.astimezone(KST).date().isoformat() != anchor['as_of']
            or _instant(anchor['observed_at']) > now or _instant(context['observed_at']) > now):
        raise ValueError('seal_requires_current_completed_session')
    if (context['limits'] != request['limits']
            or book['account_protection']['limit_pct'] != 15):
        raise ValueError('experiment_controls_changed')
    policies = {label: validate_policy(policy) for label, policy in request['candidates'].items()}
    reference = validate_policy(request['champion'])
    if (policies.get('current_hold2') != reference or len(policies) < 2
            or any(controls(policy) != controls(reference) for policy in policies.values())):
        raise ValueError('experiment_policy_controls_changed')
    model = {'mode': 'observed_quotes', 'fill_ratio': 1, 'slippage_bps': 0,
             'stress_slippage_bps': 0, 'stress_fee_rate': str(FEE + Decimal('.001'))}
    experiment = _with_hash({'version': VERSION, 'created_at': now.isoformat(),
        'source_request_hash': digest(request), 'sources': sources(),
        'observation_engine_version': request['recorded_engine_versions']['latest'],
        'anchor_frame': deepcopy(anchor), 'initial_state': deepcopy(book),
        'limits': deepcopy(context['limits']), 'policies': policies,
        'execution_models': {'full': model,
                             'partial_delayed': {**model, 'fill_ratio': .5, 'sell_delay_sessions': 1}},
        'basis': 'fixed_shadow_books_observed_prices_assumed_fills', 'live_eligible': False,
        'limitations': ['Development-selected hypotheses; subsequent sessions only begin after this seal.',
            'Recorded calendars must agree and include every session; jointly omitted sessions cannot be detected.',
            'Missing required prices stop the whole update; observed untradeability remains an unfilled attempt.',
            'Model fills at recorded quote times are not broker fills or actual account returns.',
            'Every policy and cost/fill environment retains its own book; no real-book reset or candidate retuning.',
            'No automatic promotion, probability claim, or substitution for the normal adoption gates.']})
    return experiment, _with_hash({'version': VERSION, 'experiment_hash': experiment['hash'],
        'frames': [deepcopy(anchor)], 'intervals': 0, 'results': {}, 'status': 'waiting_for_observed_session'})


def _validate(experiment, state, now):
    _check_hash(experiment, 'experiment')
    _check_hash(state, 'state')
    if (experiment['version'] != VERSION or state['version'] != VERSION
            or state['experiment_hash'] != experiment['hash']):
        raise ValueError('experiment_identity_changed')
    if experiment['sources'] != sources():
        raise ValueError('experiment_engine_or_tools_changed')
    if (_instant(experiment['created_at']) > now
            or not state['frames'] or state['frames'][0] != experiment['anchor_frame']
            or validate_observed_path(state['frames']) != state['intervals']):
        raise ValueError('experiment_anchor_or_intervals_changed')
    expected = set(experiment['policies']) if state['intervals'] else set()
    if set(state['results']) != expected:
        raise ValueError('experiment_results_incomplete')
    for frame in state['frames']:
        if _instant(frame['observed_at']) > now:
            raise ValueError('future_observation')


def advance(experiment, state, observations, *, now=None):
    """All-or-nothing pure update. Later live books are never replay inputs."""
    now = _instant(now or datetime.now(timezone.utc))
    _validate(experiment, state, now)
    known = {frame['as_of']: frame for frame in state['frames']}
    incoming = {}
    for observation in observations:
        contexts = {}
        for item in observation['daily_policy_contexts']:
            context = item['payload']
            day = context['initial_state']['as_of']
            if day in contexts and contexts[day] != context:
                raise ValueError('conflicting_observation_context')
            contexts[day] = context
        for item in observation['learning_frames']:
            frame = item['payload']
            day = frame['as_of']
            context = contexts.get(day)
            if (context is None or context['engine_version'] != experiment['observation_engine_version']
                    or context['limits'] != experiment['limits']):
                raise ValueError('observation_engine_or_limits_changed')
            if day in known:
                if frame != known[day]:
                    raise ValueError('recorded_observation_changed')
                continue
            if day in incoming and incoming[day] != frame:
                raise ValueError('conflicting_new_observation')
            if (day <= state['frames'][-1]['as_of']
                    or _instant(frame['observed_at']) <= _instant(experiment['created_at'])):
                raise ValueError('observation_before_seal_or_cursor')
            if _instant(frame['observed_at']) > now:
                raise ValueError('future_observation')
            incoming[day] = deepcopy(frame)
    if not incoming:
        return deepcopy(state)
    path = [*deepcopy(state['frames']), *(incoming[day] for day in sorted(incoming))]
    intervals = validate_observed_path(path)
    results = {}
    for label, policy in experiment['policies'].items():
        results[label] = {}
        for model_name, model in experiment['execution_models'].items():
            metric = metric_with_signals(policy, path, experiment['limits'],
                initial_state=deepcopy(experiment['initial_state']), execution_model=model)
            if not metric.get('valid'):
                raise ValueError(f"invalid_shadow_replay:{label}:{model_name}:{metric.get('error')}")
            result = {}
            for stress, environment in ((False, 'normal'), (True, 'cost_stress')):
                prefix = 'stress_' if stress else ''
                value = {**curve_diagnostics(metric, stress, experiment['limits']['budget']),
                         'trading_activity': metric[prefix + 'trading_activity'],
                         'max_drawdown_pct': metric[prefix + 'drawdown_pct'] if stress else metric['max_drawdown_pct'],
                         'signal_audit': metric['signal_audit'][environment]}
                if state['intervals']:
                    prior = state['results'][label][model_name][environment]
                    if value['equity_curve'][:len(state['frames'])] != prior['equity_curve']:
                        raise ValueError('shadow_history_changed')
                result[environment] = value
            results[label][model_name] = result
    _validate(experiment, state, now)  # Source changes during replay invalidate the update.
    return _with_hash({'version': VERSION, 'experiment_hash': experiment['hash'],
        'frames': path, 'intervals': intervals, 'results': results, 'status': 'observed'})


def save_artifact(path, value):
    """An old-cursor retry may find the same completed, immutable artifact."""
    try:
        return save_new(path, value)
    except FileExistsError:
        previous, _ = read_json(path)
        if previous != value:
            raise ValueError('existing_artifact_differs') from None
        return str(path.resolve())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('seal')
    create.add_argument('--request', type=Path, required=True)
    update = commands.add_parser('advance')
    update.add_argument('--experiment', type=Path, required=True)
    update.add_argument('--state', type=Path, required=True)
    update.add_argument('--observed', type=Path, nargs='+', required=True)
    for child in (create, update):
        child.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    install_offline_guard(args.output_dir)
    if args.command == 'seal':
        request, _ = read_json(args.request)
        experiment, state = seal(request)
        experiment_path = save_artifact(args.output_dir / f"experiment-{experiment['hash']}.json", experiment)
    else:
        experiment, _ = read_json(args.experiment)
        previous, _ = read_json(args.state)
        observations = [read_json(path)[0] for path in args.observed]
        state = advance(experiment, previous, observations)
        experiment_path = str(args.experiment.resolve())
        if state == previous:
            print(canonical({'status': 'unchanged', 'state': str(args.state.resolve())}).decode('utf-8'))
            return
    state_path = save_artifact(args.output_dir / f"state-{state['hash']}.json.gz", state)
    print(canonical({'experiment': experiment_path, 'state': state_path, 'status': state['status'],
                     'intervals': state['intervals'], 'live_eligible': False}).decode('utf-8'))


if __name__ == '__main__':
    main()
