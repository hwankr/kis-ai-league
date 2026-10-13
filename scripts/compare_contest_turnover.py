"""Freeze a read-only development replay; never change accounts or policies.

The source history is reused, survivor-biased development data. Outputs are
descriptive activity scenarios, not future return or contest-completion forecasts.
Rerunning an output directory reuses its frozen request, not the current database.
"""
from copy import deepcopy
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.learning_policy import portfolio_metrics


def freeze(output):
    source = ROOT / '.local/profitability-improvement-2026-10-13/current-development-request.json.gz'
    raw = source.read_bytes()
    historical = json.loads(gzip.decompress(raw))
    uri = (ROOT / '.local/experiments/experiments.sqlite3').as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as db:
        operating = json.loads(db.execute("SELECT value FROM settings WHERE key='policy'").fetchone()[0])
        daily = json.loads(db.execute('SELECT value FROM settings WHERE key=?',
                                     ('daily_policy:' + operating['fingerprint'],)).fetchone()[0])
    pending = daily.get('pending')
    if not pending:
        raise ValueError('pending_policy_required_to_freeze_comparison')
    limits = {key: operating[key] for key in ('budget', 'order_cap', 'daily_buy_limit')}
    variants = []
    for name in ('pending', 'equal_eligible', 'equal_top8', 'equal_top8_hold5',
                 'equal_top8_order10m', 'equal_top8_day25m_order10m'):
        policy, money = deepcopy(pending['policy']), deepcopy(limits)
        if name != 'pending':
            policy['allocation_mode'] = 'equal_eligible'
        if 'top8' in name:
            for strategy in policy['strategies']:
                strategy['top_n'] = 8
        if 'hold5' in name:
            for strategy in policy['strategies']:
                strategy['holding_sessions'] = 5
        if 'order10m' in name:
            money['order_cap'] = '10000000'
        if 'day25m' in name:
            money['daily_buy_limit'] = '25000000'
        variants.append({'name': name, 'policy': policy, 'limits': money})
    request = {'basis': 'reused_development_data_not_prospective',
               'source': str(source.relative_to(ROOT)), 'source_sha256': hashlib.sha256(raw).hexdigest(),
               'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
               'pending_signal_day': pending['signal_day'], 'frames': historical['data']['frames'],
               'variants': variants,
               'limitations': ['Current surviving universe, not historical membership.',
                               'Cash anchor; current account holdings are not backdated.',
                               'Daily-open fills with 10/20bp normal/stress slippage and official costs.',
                               'Selected after examining pending policy; these are exploratory comparisons.',
                               '30-interval scaling and rolling windows are historical scenarios, not forecasts.',
                               'Changed limits/holding length are diagnostic only; no live mutation.']}
    with gzip.open(output / 'request.json.gz', 'wt', encoding='utf-8') as file:
        json.dump(request, file, ensure_ascii=False, sort_keys=True)
    return request


def compare(output, *, extend_slots=False):
    output.mkdir(parents=True, exist_ok=True)
    frozen = output / 'request.json.gz'
    if frozen.exists():
        with gzip.open(frozen, 'rt', encoding='utf-8') as file:
            request = json.load(file)
    else:
        request = freeze(output)
    if extend_slots:
        names = {item['name'] for item in request['variants']}
        added = []
        for count in (16, 20):
            name = f'equal_top{count}_slots20'
            if name in names:
                continue
            item = deepcopy(request['variants'][0])
            item['name'] = name
            item['policy'].update(allocation_mode='equal_eligible', max_positions=20)
            for strategy in item['policy']['strategies']:
                strategy['top_n'] = count
            request['variants'].append(item)
            added.append(name)
        if added:
            request.setdefault('amendments', []).append({
                'at': datetime.now(timezone.utc).isoformat(), 'added': added,
                'basis': 'exploratory_after_initial_results',
                'reason': 'Compare the remaining position-slot bottleneck under identical hard limits and exits.'})
            with gzip.open(frozen, 'wt', encoding='utf-8') as file:
                json.dump(request, file, ensure_ascii=False, sort_keys=True)
    rows = []
    for variant in request['variants']:
        result = portfolio_metrics(variant['policy'], request['frames'], variant['limits'])
        if not result['valid']:
            raise ValueError(result['error'])
        activity = result['trading_activity']
        days = [point['gross_buy_amount'] + point['gross_sell_amount'] for point in result['equity_curve'][1:]]
        windows = [sum(days[index:index + 30]) for index in range(max(0, len(days) - 29))]
        rows.append({'name': variant['name'], 'limits': variant['limits'],
            **{key: result[key] for key in ('net_return_pct', 'stress_return_pct', 'max_drawdown_pct',
                                            'stress_drawdown_pct', 'closed_trades', 'trading_activity', 'stress_trading_activity')},
            'gross_30_interval_scaled': activity['mean_daily_gross_amount'] * 30,
            'rolling_30_intervals': {'count': len(windows), 'minimum': min(windows) if windows else None,
                'median': statistics.median(windows) if windows else None,
                'maximum': max(windows) if windows else None,
                'at_least_500m': sum(value >= 500_000_000 for value in windows)},
            'last_30_buy_amount': sum(point['gross_buy_amount'] for point in result['equity_curve'][-30:])})
    result = {key: request[key] for key in ('basis', 'source', 'source_sha256', 'git_head', 'pending_signal_day', 'limitations')}
    result.update(days=[request['frames'][0]['as_of'], request['frames'][-1]['as_of']],
                  intervals=len(request['frames']) - 1, results=rows,
                  amendments=request.get('amendments', []),
                  source_hashes={name: hashlib.sha256((ROOT / 'backend' / name).read_bytes()).hexdigest()
                                 for name in ('learning_policy.py', 'portfolio_plan.py')})
    (output / 'comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(output / 'comparison.json'), 'results': rows}, ensure_ascii=False))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--extend-slots', action='store_true', help='Append two labelled exploratory position-slot comparisons.')
    arguments = parser.parse_args()
    compare(arguments.output.resolve(), extend_slots=arguments.extend_slots)
