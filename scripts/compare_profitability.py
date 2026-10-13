"""Offline, reproducible profitability comparisons. Never opens a trading service.

Freeze the baseline source and observed inputs before comparing changes::
    .venv/Scripts/python.exe scripts/compare_profitability.py freeze
    .venv/Scripts/python.exe scripts/compare_profitability.py compare

All outputs are research only. Historical rule replay is reused development
data; missing historical LLM signals are never generated or replaced by cash.
"""
from __future__ import annotations

import argparse
import ast
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import gzip
import hashlib
import importlib
import importlib.util
import io
import inspect
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / '.local/profitability-improvement-2026-10-13'
BASELINE = '4e25f018857b32e2b7e2af3cbc583139b153bec1'
OPTIONAL_POLICY = {'allocation_mode', 'candidate_scope'}
OPTIONAL_STRATEGY = {'exit_mode', 'max_holding_sessions'}
METRICS = ('valid', 'error', 'net_return_pct', 'stress_return_pct',
           'max_drawdown_pct', 'stress_drawdown_pct', 'closed_trades',
           'sessions', 'intervals', 'half_returns')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def read_json(path):
    path = Path(path)
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8-sig') as stream:
        return json.load(stream)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    if path.suffix == '.gz':
        raw = gzip.compress(raw, mtime=0)
    path.write_bytes(raw)


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    return db


def strip_options(policy):
    policy = deepcopy(policy)
    for name in OPTIONAL_POLICY:
        policy.pop(name, None)
    for strategy in policy['strategies']:
        for name in OPTIONAL_STRATEGY:
            strategy.pop(name, None)
    return policy


def summarize(result):
    value = {key: result.get(key) for key in METRICS}
    for key in ('diagnostics', 'equity_curve', 'stress_equity_curve', 'daily_log_returns', 'stress_daily_log_returns'):
        value[key] = result.get(key)
    curve = result.get('equity_curve', [])
    if curve:
        exposures = [row['position_value'] / row['equity'] * 100 for row in curve]
        value['mean_marked_exposure_pct'] = sum(exposures) / len(exposures)
        value['max_marked_exposure_pct'] = max(exposures)
        costs = (result.get('diagnostics') or {}).get('normal') or {}
        marking_factor = ((1 - costs.get('fee_rate', 0) - costs.get('tax_rate', 0))
                          * (1 - costs.get('slippage_bps', 0) / 10000))
        gross = [item / marking_factor for item in exposures]
        value['mean_gross_exposure_pct'] = sum(gross) / len(gross)
        value['max_gross_exposure_pct'] = max(gross)
        value['daily_gross_exposure_pct'] = [{'as_of': point['as_of'], 'value': exposure}
                                            for point, exposure in zip(curve, gross)]
    return value


def paired_delta(old, new):
    if not old.get('valid') or not new.get('valid'):
        return {'status': 'unavailable', 'old_error': old.get('error'), 'new_error': new.get('error')}
    return {key + '_delta_pp': new[key] - old[key] for key in
            ('net_return_pct', 'stress_return_pct', 'max_drawdown_pct', 'stress_drawdown_pct')}


def engine_fingerprints(root):
    names = ('learning_policy.py', 'portfolio_plan.py', 'daily_evaluation.py', 'learning_research.py',
             'learning_evaluation.py', 'learning_attribution.py')
    return {name: hashlib.sha256((Path(root) / 'backend' / name).read_bytes()).hexdigest()
            for name in names if (Path(root) / 'backend' / name).exists()}


def freeze_current_engine(output):
    """All new-policy cells run one source snapshot, even while others edit."""
    before = engine_fingerprints(ROOT)
    target = output / 'candidate-source'
    (target / 'backend').mkdir(parents=True, exist_ok=True)
    for path in (ROOT / 'backend').glob('*.py'):
        (target / 'backend' / path.name).write_bytes(path.read_bytes())
    if engine_fingerprints(ROOT) != before or engine_fingerprints(target) != before:
        raise ValueError('current_engine_changed_while_freezing_retry')
    return target, before


def freeze_baseline(output, ref):
    commit = subprocess.check_output(['git', 'rev-parse', ref + '^{commit}'], cwd=ROOT, text=True).strip()
    archive = subprocess.check_output(['git', 'archive', '--format=zip', commit, 'backend'], cwd=ROOT)
    target = output / 'baseline'
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for item in bundle.infolist():
            destination = (target / item.filename).resolve()
            if not destination.is_relative_to(target.resolve()):
                raise ValueError('unsafe_archive_path')
            if not item.is_dir():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(bundle.read(item))
    files = {str(p.relative_to(target)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in target.rglob('*.py')}
    return {'commit': commit, 'files': files}


def worker_call(output, code_root, request, filename):
    request_path = output / (filename + '-request.json.gz')
    result_path = output / (filename + '.json.gz')
    write_json(request_path, request)
    subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), 'worker',
                    '--output', str(output), '--code-root', str(code_root),
                    '--request', str(request_path), '--result', str(result_path)],
                   cwd=code_root, check=True)
    return read_json(result_path)


def freeze(output, ref=BASELINE):
    if (output / 'frozen-input.json.gz').exists():
        raise ValueError('frozen_input_exists_choose_another_output_directory')
    output.mkdir(parents=True, exist_ok=True)
    baseline = freeze_baseline(output, ref)
    with closing(readonly(ROOT / '.local/experiments/experiments.sqlite3')) as db:
        settings = {key: json.loads(value) for key, value in db.execute('SELECT key,value FROM settings')}
        policy = settings['policy']
        frames = [json.loads(raw) for raw, in db.execute('SELECT payload FROM learning_frames ORDER BY day')]
        decisions = [json.loads(raw) for raw, in db.execute('SELECT payload FROM daily_policy_decisions WHERE fingerprint=? ORDER BY day', (policy['fingerprint'],))]
    if not frames or not decisions:
        raise ValueError('observed_frames_and_development_proposal_required')
    latest = frames[-1]
    cutoff = latest['as_of']
    rows = [{key: row[key] for key in ('symbol', 'name', 'board', 'learning_eligible')} for row in latest['rows']]
    history_root = ROOT / '.local/candidates/history'
    histories, file_hashes = {}, {}
    for row in rows:
        path = history_root / ('stock-' + row['symbol'] + '.json')
        if not path.exists():
            continue
        raw = path.read_bytes()
        content = json.loads(raw)
        if content.get('kind') != 'stock' or content.get('symbol') != row['symbol']:
            raise ValueError('history_identity_mismatch')
        histories[row['symbol']] = {day: value for day, value in content['rows'].items() if day <= cutoff}
        file_hashes[path.name] = hashlib.sha256(raw).hexdigest()
    benchmarks = {}
    for board, symbol in (('KOSPI', '0001'), ('KOSDAQ', '1001')):
        path = history_root / ('index-' + symbol + '.json')
        raw = path.read_bytes()
        benchmarks[board] = {day: value for day, value in json.loads(raw)['rows'].items() if day <= cutoff}
        file_hashes[path.name] = hashlib.sha256(raw).hexdigest()
    captured_at = datetime.now(timezone.utc).isoformat()
    current = {'as_of': cutoff, 'observed_at': captured_at, 'rows': rows, 'histories': histories,
               'benchmarks': benchmarks, 'calendars': {board: sorted(values) for board, values in benchmarks.items()}}
    limits = {key: policy[key] for key in ('budget', 'daily_buy_limit', 'order_cap')}
    anchor = strip_options(decisions[-1]['proposal']['policy'])
    for strategy in anchor['strategies']:
        strategy.update(holding_sessions=5, stop_loss_pct=0.0, take_profit_pct=0.0)
    plan = {'version': 1, 'fixed_at': captured_at, 'baseline_commit': baseline['commit'],
            'account_id': policy['account_id'], 'environment': 'paper', 'limits': limits,
            'decision_rule': 'Report every prespecified contrast, including losses; no automatic promotion.',
            'as_of': cutoff, 'development_sessions': 120, 'anchor_policy': anchor,
            'normal_costs': {'fee_rate': .000140527, 'tax_rate': .002, 'slippage_bps': 10},
            'stress_costs': {'fee_rate': .000140527, 'tax_rate': .002, 'slippage_bps': 20},
            'timefolio_cost_sensitivity': {'fee_rate': .001, 'tax_rate': .002, 'slippage_bps': 10,
                                           'stress_slippage_bps': 20, 'note': 'Buy/sell commission 10bp each plus sell tax 20bp; not a broker-fill or mirror simulation'},
            'initial_state': 'cash; identical 100m budget; additional account anchor comparison only when observed quotes support it',
            'variants': ['unchanged', 'evaluation_alignment', 'equal_eligible', 'score_rank', 'candidate_pool_selection', 'liquid_top20', 'liquid_all', 'signal_extension_max20', 'combined'],
            'known_before_plan': 'The prior 120-observation candidate result and one-interval daily decision were already inspected.',
            'exposure_control': 'Identical hard limits plus actual daily/mean/max exposure. Allocation-only variants additionally capped at baseline same-day buy cost; this does not guarantee equal actual exposure. No return-proportional rescaling.',
            'buy_cap_basis': 'Paired counterfactual same-day baseline fill cost, excluding future returns; not a pre-session executable allocation rule.',
            'evidence_limits': ['Historical rule data are reused development data, not independent validation.',
                                'Current surviving universe/eligibility and adjusted-price vintage are not historical point-in-time eligibility.',
                                'No historical LLM decisions are generated. Four-strategy comparison uses stored observed frames only.',
                                'Daily-bar fill and liquidation-cost assumptions are not realized competition returns.',
                                'One interval cannot exercise a five-session holding-time change.']}
    write_json(output / 'plan.json', plan)
    prepared = worker_call(output, output / 'baseline', {'action': 'prepare', 'root': str(ROOT),
                            'current_input': current, 'limit': 120}, 'prepared-development')
    frozen = {'captured_at': captured_at, 'baseline': baseline, 'plan_sha256': digest(plan), 'plan': plan,
              'history_file_sha256': file_hashes, 'observed_frames': frames, 'development': prepared,
              'daily_decision': {key: decisions[-1].get(key) for key in ('as_of', 'decision', 'metrics', 'reason')},
              'proposal_policy': decisions[-1]['proposal']['policy']}
    write_json(output / 'frozen-input.json.gz', frozen)
    report = {'captured_at': captured_at, 'baseline_commit': baseline['commit'], 'plan_sha256': digest(plan),
              'input_sha256': digest(frozen), 'observed_days': [f['as_of'] for f in frames],
              'development_days': [prepared['frames'][0]['as_of'], prepared['frames'][-1]['as_of']],
              'development_sessions': len(prepared['frames']), 'symbols': len(rows)}
    write_json(output / 'input-manifest.json', report)
    print(json.dumps(report, ensure_ascii=True))


def raw_frame(source, frame):
    """Recreate the exact per-close input; never include prices after that close."""
    day = frame['as_of']
    days = [x for x in source['calendars']['KOSPI'] if x <= day][-63:]
    return {'as_of': day, 'observed_at': day + 'T16:00:00+09:00',
            'rows': [{key: row[key] for key in ('symbol', 'name', 'board', 'learning_eligible')} for row in frame['rows']],
            'histories': {symbol: {date: bar for date, bar in values.items() if date in days}
                          for symbol, values in source['histories'].items()},
            'calendars': {board: days for board in source['calendars']},
            'benchmarks': {board: {date: value for date, value in values.items() if date in days}
                           for board, values in source['benchmarks'].items()}}


def worker(request, code_root):
    sys.path.insert(0, str(Path(code_root).resolve()))
    policy_module = importlib.import_module('backend.learning_policy')
    add_buy_cost_instrumentation(policy_module)
    if request['action'] == 'prepare':
        history_module = importlib.import_module('backend.learning_history')
        original = history_module.build_frame
        source = {'histories': {}, 'benchmarks': {}, 'calendars': {}}
        def capture(data, *args, **kwargs):
            for kind in ('histories', 'benchmarks'):
                for symbol, series in data[kind].items():
                    source[kind].setdefault(symbol, {}).update(series)
            for board, days in data['calendars'].items():
                source['calendars'][board] = sorted(set(source['calendars'].get(board, [])) | set(days))
            return original(data, *args, **kwargs)
        history_module.build_frame = capture
        frames = history_module.development_frames(Path(request['root']), request['current_input'], limit=request['limit'])
        if len(frames) != request['limit']:
            raise ValueError('development_window_incomplete')
        return {'frames': frames, 'raw_source': source}
    if request['action'] == 'evaluate':
        data = request['data']
        frames = data['frames']
        if data.get('raw_source'):
            frames = [{**policy_module.build_frame(raw_frame(data['raw_source'], frame)), 'role': 'development_only',
                       'provenance': frame.get('provenance', {})} for frame in frames]
        results = {}
        for name, policy in request['policies'].items():
            results[name] = summarize(policy_module.portfolio_metrics(policy, deepcopy(frames), request['limits'],
                                      initial_state=request.get('initial_state'), execution_model=request.get('execution_model')))
        return {'results': results, 'frame_digest': digest(frames),
                'feature_names': sorted(frames[0]['rows'][0]['features']), 'days': [x['as_of'] for x in frames]}
    if request['action'] == 'candidate_pool':
        research = importlib.import_module('backend.learning_research')
        captured = []
        evaluate = research.portfolio_metrics
        def capture(policy, *args, **kwargs):
            result = evaluate(policy, *args, **kwargs)
            captured.append((deepcopy(policy), deepcopy(result)))
            return result
        research.portfolio_metrics = capture
        champion = request['champion']
        proposal = research.propose_candidate(champion, request['frames'], request['limits'], [], llm=None)
        pool = []
        for candidate, metrics in captured:
            if policy_module.policy_id(candidate) == policy_module.policy_id(champion):
                continue
            family, changes = research._policy_changes(champion, candidate)
            pool.append({'policy': candidate, 'parent_id': policy_module.policy_id(champion),
                         'method': 'grammar_development', 'change_family': family, 'changes': changes,
                         'hypothesis': 'Prespecified deterministic offline grammar',
                         'hypothesis_test': {'metric': 'D11', 'direction': 'positive'},
                         'development': {**{k: metrics.get(k) for k in METRICS},
                                         'score': research._score(metrics), 'role': 'development_only'}})
        return {'selected': proposal, 'pool': pool}
    if request['action'] == 'select_candidate':
        daily = importlib.import_module('backend.daily_evaluation')
        proposal = request['proposal']
        evaluator = daily.evaluate_daily_candidate
        if request.get('gate') == 'nonworse':
            spec = importlib.util.spec_from_file_location('comparison_old_daily_gate', request['baseline_daily_path'])
            old_daily = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(old_daily)
            def evaluator(*args, **kwargs):
                result = old_daily.evaluate_daily_candidate(*args, **kwargs)
                metrics = result.get('metrics') or {}
                candidate = metrics.get('candidate') or {}
                if candidate:
                    metrics['selection_return_pct'] = candidate.get('net_return_pct')
                    metrics['selection_stress_return_pct'] = candidate.get('stress_return_pct')
                return result
        if request.get('new_selector'):
            selected, result = daily.select_daily_candidate(request['champion'], proposal, request['frames'], request['limits'], evaluator=evaluator)
        else:
            selected = proposal
            result = evaluator(request['champion'], proposal, request['frames'], request['limits'])
        active_policy = selected['policy'] if result['decision'] == 'apply' else request['champion']
        metrics = summarize(policy_module.portfolio_metrics(active_policy, request['frames'], request['limits']))
        return {'selected_policy': selected['policy'], 'effective_policy': active_policy, 'evaluation': result, 'metrics': metrics}
    raise ValueError('unsupported_worker_action')


def add_buy_cost_instrumentation(module):
    """Add only a diagnostic curve field to the old source; trading is untouched."""
    source = inspect.getsource(module._replay)
    tree = ast.parse(source)
    changed = False
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == 'curve'
                and node.func.attr == 'append' and len(node.args) == 1 and isinstance(node.args[0], ast.Dict)):
            keys = [key.value for key in node.args[0].keys if isinstance(key, ast.Constant)]
            if 'buy_cost' not in keys:
                node.args[0].keys.append(ast.Constant('buy_cost'))
                node.args[0].values.append(ast.Call(func=ast.Name(id='float', ctx=ast.Load()),
                                                  args=[ast.Name(id='spent', ctx=ast.Load())], keywords=[]))
                changed = True
            if 'buy_cost_exact' not in keys:
                node.args[0].keys.append(ast.Constant('buy_cost_exact'))
                node.args[0].values.append(ast.Call(func=ast.Name(id='format', ctx=ast.Load()),
                                                  args=[ast.Name(id='spent', ctx=ast.Load()), ast.Constant('f')], keywords=[]))
                changed = True
    if changed:
        ast.fix_missing_locations(tree)
        exec(compile(tree, module.__file__ + ':diagnostic_buy_cost', 'exec'), module.__dict__)


def variants(anchor):
    result = {'unchanged': deepcopy(anchor)}
    for mode in ('equal_eligible', 'score_rank'):
        result[mode] = {**deepcopy(anchor), 'allocation_mode': mode}
    for scope in ('liquid_top20', 'liquid_all'):
        result[scope] = {**deepcopy(anchor), 'candidate_scope': scope}
    extended = deepcopy(anchor)
    for strategy in extended['strategies']:
        strategy.update(exit_mode='signal_extension', max_holding_sessions=20)
    result['signal_extension_max20'] = extended
    result['combined'] = {**deepcopy(extended), 'allocation_mode': 'score_rank', 'candidate_scope': 'liquid_all'}
    return result


def sensitivity_terms(output, plan):
    amendment_path = output / 'cost-model-amendment.json'
    if amendment_path.exists():
        amendment = read_json(amendment_path)
        return amendment['execution_model'], amendment['note'], amendment
    model = {key: value for key, value in plan['timefolio_cost_sensitivity'].items() if key != 'note'}
    return model, plan['timefolio_cost_sensitivity']['note'], None


def update_sensitivity(output):
    """Recompute only corrected broker-cost sensitivity using the same snapshots."""
    frozen = read_json(output / 'frozen-input.json.gz')
    report = read_json(output / 'comparison.json')
    plan = frozen['plan']
    if digest(read_json(output / 'plan.json')) != frozen['plan_sha256']:
        raise ValueError('comparison_plan_changed')
    candidate_root = output / 'candidate-source'
    if engine_fingerprints(candidate_root) != report['current_engine_sha256']:
        raise ValueError('candidate_source_changed')
    policies = variants(plan['anchor_policy'])
    policies['combined_without_selection'] = policies.pop('combined')
    policies['combined'] = variants(report['candidate_selection']['new']['effective_policy'])['combined']
    model, note, amendment = sensitivity_terms(output, plan)
    request = {'action': 'evaluate', 'data': frozen['development'], 'limits': plan['limits'],
               'execution_model': model, 'policies': {'baseline': strip_options(plan['anchor_policy'])}}
    old = worker_call(output, output / 'baseline', request, 'baseline-timefolio-cost')
    new = worker_call(output, candidate_root, {**request, 'policies': policies}, 'current-timefolio-cost')
    report['timefolio_cost_sensitivity'] = {'model': model, 'note': note, 'baseline': old['results']['baseline'],
        'variants': new['results'], 'deltas': {key: paired_delta(old['results']['baseline'], value) for key, value in new['results'].items()}}
    report['cost_model_amendment'] = amendment
    report['current_engine_stable_during_comparison'] = engine_fingerprints(ROOT) == report['current_engine_sha256']
    write_json(output / 'comparison.json', report)
    print(json.dumps({'current_engine_stable': report['current_engine_stable_during_comparison'],
                      'corrected_timefolio_model': model, 'deltas': report['timefolio_cost_sensitivity']['deltas']}, ensure_ascii=True))


def compare(output):
    candidate_root, engine_before = freeze_current_engine(output)
    frozen = read_json(output / 'frozen-input.json.gz')
    if digest(read_json(output / 'plan.json')) != frozen['plan_sha256']:
        raise ValueError('comparison_plan_changed')
    for filename, expected in frozen['baseline']['files'].items():
        if hashlib.sha256((output / 'baseline' / filename).read_bytes()).hexdigest() != expected:
            raise ValueError('baseline_source_changed')
    plan = frozen['plan']
    base_request = {'action': 'evaluate', 'data': frozen['development'], 'limits': plan['limits'],
                    'execution_model': {**plan['normal_costs'], 'stress_slippage_bps': plan['stress_costs']['slippage_bps']},
                    'policies': {'baseline': strip_options(plan['anchor_policy'])}}
    old = worker_call(output, output / 'baseline', base_request, 'baseline-development')
    pool_path = output / 'frozen-candidate-pool.json.gz'
    if pool_path.exists():
        pool = read_json(pool_path)
    else:
        pool = worker_call(output, output / 'baseline', {'action': 'candidate_pool',
                           'champion': plan['anchor_policy'], 'frames': frozen['development']['frames'],
                           'limits': plan['limits']}, 'frozen-candidate-pool')
    selection_request = {'action': 'select_candidate', 'champion': plan['anchor_policy'],
                         'proposal': {**pool['selected'], 'candidate_pool': pool['pool']},
                         'frames': frozen['development']['frames'], 'limits': plan['limits']}
    old_selection = worker_call(output, output / 'baseline', selection_request, 'baseline-selection')
    new_selection = worker_call(output, candidate_root, {**selection_request, 'new_selector': True}, 'current-selection')
    factorial = {}
    for selector in ('single', 'pool'):
        for gate in ('nonworse', 'absolute_15pct'):
            key = selector + '__' + gate
            factorial[key] = worker_call(output, candidate_root, {**selection_request, 'new_selector': selector == 'pool',
                    'gate': gate, 'baseline_daily_path': str(output / 'baseline/backend/daily_evaluation.py')},
                    'selection-factorial-' + key)
    policies = variants(plan['anchor_policy'])
    policies['combined_without_selection'] = policies.pop('combined')
    policies['combined'] = variants(new_selection['effective_policy'])['combined']
    new = worker_call(output, candidate_root, {**base_request, 'policies': policies}, 'current-development')
    unchanged_delta = paired_delta(old['results']['baseline'], new['results']['unchanged'])
    if unchanged_delta.get('status') == 'unavailable' or any(abs(value) > 1e-8 for value in unchanged_delta.values()):
        raise ValueError('unchanged_policy_did_not_reproduce_baseline')
    legacy = {'kind': 'legacy', 'name': 'Existing four strategies', 'strategies': [], 'cash_reserve': 0.0,
              'max_positions': 20, 'entry_slippage_bps': 100, 'cancel_after_minutes': 10}
    observed_request = {**base_request, 'data': {'frames': frozen['observed_frames']}, 'policies': {'baseline': legacy}}
    old_observed = worker_call(output, output / 'baseline', observed_request, 'baseline-observed')
    new_observed = worker_call(output, candidate_root, {**observed_request, 'policies': {'unchanged': legacy}}, 'current-observed')
    baseline = old['results']['baseline']
    buy_caps = {point['as_of']: point['buy_cost_exact'] for point in baseline['equity_curve']}
    capped_request = {**base_request,
                      'policies': {key: policies[key] for key in ('unchanged', 'equal_eligible', 'score_rank')},
                      'execution_model': {**base_request['execution_model'], 'daily_buy_caps': buy_caps}}
    capped = worker_call(output, candidate_root, capped_request, 'current-allocation-capped')
    sensitivity_model, sensitivity_note, cost_amendment = sensitivity_terms(output, plan)
    sensitivity_old = worker_call(output, output / 'baseline', {**base_request, 'execution_model': sensitivity_model}, 'baseline-timefolio-cost')
    sensitivity_new = worker_call(output, candidate_root, {**base_request, 'policies': policies, 'execution_model': sensitivity_model}, 'current-timefolio-cost')
    report = {'baseline_commit': frozen['baseline']['commit'], 'as_of': plan['as_of'],
              'current_engine_sha256': engine_before,
              'current_engine_stable_during_comparison': engine_fingerprints(ROOT) == engine_before,
              'baseline_instrumentation': 'AST adds curve buy_cost and exact Decimal string buy_cost_exact only; regression confirms all existing outputs unchanged.',
              'input_sha256': digest(frozen), 'plan_sha256': frozen['plan_sha256'],
              'development': {'basis': 'reused_development_data', 'baseline': baseline, 'variants': new['results'],
                              'anchor_role': 'Predefined rule-only research anchor; this is not the live four-strategy champion.',
                              'deltas_vs_baseline': {key: paired_delta(baseline, value) for key, value in new['results'].items()}},
              'evaluation_alignment': {'unchanged_replay_delta': unchanged_delta,
                                        'account_context_return_effect': None,
                                        'reason': 'These inputs freeze close bars and observed signals, not a pre-anchor real-book execution-context series. No account-context return effect is estimated.',
                                        'interpretation': 'Unchanged mathematical replay is a consistency check; it does not estimate the profitability of corrected daily evaluation context.'},
              'combined_vs_old_selected_policy': {'baseline': old_selection['metrics'],
                                                  'candidate': new['results']['combined'],
                                                  'delta': paired_delta(old_selection['metrics'], new['results']['combined']),
                                                  'note': 'The old selector already chose this baseline. Do not attribute old selection gains to the new implementation.'},
              'scope_pair': {'from': 'liquid_top20', 'to': 'liquid_all',
                             'delta': paired_delta(new['results']['liquid_top20'], new['results']['liquid_all'])},
              'candidate_selection': {'frozen_pool_sha256': digest(pool), 'candidate_count': len(pool['pool']),
                                      'old': old_selection, 'new': new_selection,
                                      'delta': paired_delta(old_selection['metrics'], new_selection['metrics'])},
              'selection_gate_factorial': {'basis': 'additional_diagnostic_requested_after_first_result_same_frozen_pool',
                                           'cells': factorial,
                                           'selection_effect_nonworse': paired_delta(factorial['single__nonworse']['metrics'], factorial['pool__nonworse']['metrics']),
                                           'selection_effect_15pct': paired_delta(factorial['single__absolute_15pct']['metrics'], factorial['pool__absolute_15pct']['metrics']),
                                           'gate_effect_single': paired_delta(factorial['single__nonworse']['metrics'], factorial['single__absolute_15pct']['metrics']),
                                           'gate_effect_pool': paired_delta(factorial['pool__nonworse']['metrics'], factorial['pool__absolute_15pct']['metrics']),
                                           'note': 'Absolute drawdown cap is a relaxed acceptance rule, not evidence of return improvement.'},
              'allocation_buy_cap_control': {'basis': plan['buy_cap_basis'],
                                             'warning': 'Same daily purchase ceiling, not matched actual exposure. Different exits and integer shares can change holdings.',
                                             'baseline_daily_buy_caps': buy_caps, 'baseline': baseline,
                                             'variants': capped['results'],
                                             'controlled_baseline': capped['results']['unchanged'],
                                             'deltas': {key: paired_delta(capped['results']['unchanged'], value) for key, value in capped['results'].items()}},
              'timefolio_cost_sensitivity': {'model': sensitivity_model,
                                            'note': sensitivity_note,
                                            'baseline': sensitivity_old['results']['baseline'],
                                            'variants': sensitivity_new['results'],
                                            'deltas': {key: paired_delta(sensitivity_old['results']['baseline'], value)
                                                       for key, value in sensitivity_new['results'].items()}},
              'cost_model_amendment': cost_amendment,
              'observed': {'basis': 'stored_signals_counterfactual_not_realized_returns', 'days': old_observed['days'],
                           'baseline': old_observed['results']['baseline'], 'current': new_observed['results']['unchanged'],
                           'delta': paired_delta(old_observed['results']['baseline'], new_observed['results']['unchanged'])},
              'evidence_limits': plan['evidence_limits']}
    write_json(output / 'comparison.json', report)
    print(json.dumps({'development_deltas': report['development']['deltas_vs_baseline'], 'observed_delta': report['observed']['delta']}, ensure_ascii=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'compare', 'sensitivity', 'worker'))
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--baseline', default=BASELINE)
    parser.add_argument('--code-root', type=Path)
    parser.add_argument('--request', type=Path)
    parser.add_argument('--result', type=Path)
    args = parser.parse_args()
    if args.action == 'freeze':
        freeze(args.output.resolve(), args.baseline)
    elif args.action == 'compare':
        compare(args.output.resolve())
    elif args.action == 'sensitivity':
        update_sensitivity(args.output.resolve())
    else:
        write_json(args.result, worker(read_json(args.request), args.code_root))


if __name__ == '__main__':
    main()
