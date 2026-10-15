"""Local approved intraday transition command; no broker or analysis calls.

prepare reads the original ledger and sealed native selection artifact. Only
stage/activate mutate the experiment DB, under runner and submission locks and
a confirmed closed maintenance gate. Ordinary cycles only resolve an exact run.
"""
import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from backend.daily_inputs import freeze_entry_selection, validate_award_context
from backend.experiment_store import ExperimentStore
from backend.intraday_policy import (IntradayPolicyService, RECEIPT_BINDINGS, RECEIPT_VERSION,
                                     VERSION, canonical_hash, seal, _require, _sealed, _stamp)
from backend.kis import KST
from backend.learning_policy import policy_id, signals, validate_policy
from backend.request_gate import file_lock


APPROVAL_ID = 'qualification-live-2026-10-15'
EFFECTIVE_DAY = '2026-10-15'
ARTIFACT_VERSION = 'intraday-selection-artifact-v1'
ACTIVE_ORDER_STATES = {'submitting', 'unknown', 'submitted', 'partial', 'cancel_pending'}
ORDER_FIELDS = ('id', 'fingerprint', 'order_date', 'strategy_id', 'symbol', 'side', 'quantity',
                'status', 'filled_quantity', 'filled_amount', 'remaining_quantity', 'limit_price',
                'exit_policy', 'exit_pending', 'policy_id', 'run_id')


def runtime_versions(config_path):
    from backend.experiment_analysis import analysis_version
    from backend.source_version import DAILY_SOURCES, LEARNING_SOURCES, source_version
    return {'daily': source_version(DAILY_SOURCES), 'learning': source_version(LEARNING_SOURCES),
            'analysis': analysis_version(config_path)}


def read_json(path):
    path = Path(path)
    if path.suffix == '.gz':
        with gzip.open(path, 'rt', encoding='utf-8') as handle:
            return json.load(handle)
    return json.loads(path.read_text(encoding='utf-8'))


def _amount(value):
    number = Decimal(str(value))
    _require(number.is_finite(), 'intraday_economic_value_invalid')
    return format(number.normalize(), 'f') if number else '0'


def economic_book(book):
    """Common original-close/live-ledger projection; never carries current marks."""
    lots = []
    default_exit = {'holding_sessions': 5, 'stop_loss_pct': 0, 'take_profit_pct': 0}
    for position in book['positions']:
        lots.append({**{key: deepcopy(position[key]) for key in
                        ('symbol', 'strategy_id', 'quantity', 'entry_date')},
            'cost': _amount(position['cost']), 'exit_pending': position.get('exit_pending') is True,
            'exit_policy': deepcopy(position.get('exit_policy') or default_exit)})
    reserves = [{**{key: item[key] for key in ('symbol', 'strategy_id')}, 'cost': _amount(item['cost'])}
                for item in book.get('reserved_entries', [])]
    return {**{key: _amount(book.get(key, 0)) for key in ('budget', 'cash', 'reserved_cash')},
            'positions': sorted(lots, key=lambda item: (item['strategy_id'], item['symbol'], item['entry_date'])),
            'reserved_entries': sorted(reserves, key=lambda item: (item['strategy_id'], item['symbol'])),
            'pending_entries': deepcopy(book.get('pending_entries', [])),
            'pending_orders': book.get('pending_orders', 0), 'unresolved_orders': book.get('unresolved_orders', 0)}


def guard_snapshot(db, config_path):
    """Read all CAS facts from this transaction; no service construction or writes."""
    from backend.experiments import ExperimentService
    from backend.learning_account import owned_book
    read = IntradayPolicyService._read
    policy = read(db, 'policy', {})
    fingerprint = policy.get('fingerprint')
    orders = [json.loads(row[0]) for row in db.execute('SELECT payload FROM orders ORDER BY created_at,id')]
    orders = [row for row in orders if row.get('fingerprint') == fingerprint]
    positions, metrics = ExperimentService._portfolio(None, orders)
    # Marks and session ages are intentionally absent from economic_book().
    book = owned_book(policy, positions, metrics, orders, {item['symbol']: '1' for item in positions},
                      as_of=EFFECTIVE_DAY)
    account = read(db, 'learning_account:' + str(fingerprint), {})
    risk = account.get('risk') or {}
    stable_risk = {key: deepcopy(risk.get(key)) for key in ('active', 'limit_pct', 'peak', 'triggered_at', 'reset_at')}
    stable_risk.update(units=account.get('units'), budget=account.get('budget'), account_peak=account.get('peak'))
    _require(risk.get('active') is False and risk.get('limit_pct') == 15., 'intraday_risk_state_not_ready')
    economic_orders = [{key: deepcopy(row.get(key)) for key in ORDER_FIELDS} for row in orders]
    pending = [row['id'] for row in orders if row.get('status') in ACTIVE_ORDER_STATES]
    unresolved = [row['id'] for row in orders if row.get('status') in {'submitting', 'unknown', 'cancel_pending'}]
    state = read(db, 'daily_policy:' + str(fingerprint), {})
    parent = state.get('active') or {}
    source_id = parent.get('source_decision_id') or parent.get('decision_id')
    source = db.execute('SELECT payload FROM daily_policy_preparations WHERE decision_id=?', (source_id,)).fetchone()
    _require(source is not None, 'intraday_source_missing')
    source_contract = json.loads(source[0])['signal_inputs']
    mirror_control = read_json(Path(config_path).resolve().parent / '.local' / 'timefolio-mirror-control.json')
    _require(mirror_control.get('version') == 1 and type(mirror_control.get('desired_running')) is bool,
             'intraday_mirror_intent_unverified')
    mirror_intent = {key: mirror_control[key] for key in ('version', 'desired_running')}
    mirror_binding = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mirror_runtime'").fetchone():
        binding = db.execute('SELECT payload FROM mirror_runtime WHERE id=1').fetchone()
        if binding:
            binding = json.loads(binding[0])
            mirror_binding = {key: binding.get(key) for key in ('source_account', 'source_fingerprint',
                'account_key', 'contest', 'baseline_order_ids', 'initialized_at')}
    controls = {key: read(db, key, None if key == 'mirror_intent' else False) for key in
                ('enabled', 'user_paused', 'mirror_intent')}
    controls.update(policy=policy, risk=stable_risk, mirror_binding=mirror_binding, mirror_process_intent=mirror_intent,
        economic_orders_hash=canonical_hash(economic_orders),
        runtime_hash=canonical_hash(runtime_versions(config_path)), source_pool_hash=canonical_hash(source_contract),
        reservation_revision=canonical_hash([row for row in economic_orders if row['id'] in pending]),
        protection_revision=canonical_hash(stable_risk))
    return {'fingerprint': fingerprint, 'account_id': policy.get('account_id'), 'controls': controls,
            'economic_book': economic_book(book), 'pending_orders': pending, 'unresolved_orders': unresolved,
            'protected': risk.get('active'),
            'blocked': any(row.get('state') == 'open' and row.get('blocking') is True
                           for row in read(db, 'issues', []))}


def validate_artifact(artifact, config_path):
    """Verify native audit gates/rank using stored path results, without replaying."""
    from backend.daily_evaluation import validate_capacity_result_records
    from backend.intraday_scenarios import validate_intraday_context
    from backend.policy_award import selection_contract, verify_award_record_evidence
    from backend.policy_capacity import signal_hold_packet, validate_capacity_member
    _require(_sealed(artifact) and artifact.get('version') == ARTIFACT_VERSION,
             'intraday_artifact_invalid')
    _require(artifact['approval_id'] == APPROVAL_ID and artifact['effective_day'] == EFFECTIVE_DAY
             and artifact['signal_day'] < EFFECTIVE_DAY, 'intraday_artifact_approval_invalid')
    versions = runtime_versions(config_path)
    _require(artifact['runtime'] == versions and artifact['runtime_hash'] == canonical_hash(versions)
             and artifact['selection_contract'] == selection_contract(), 'intraday_artifact_runtime_changed')
    source = read_json(artifact['source_request_path'])
    _require(canonical_hash(source) == artifact['source_request_hash'], 'intraday_source_request_changed')
    closing = artifact['closing_context']
    _require(validate_award_context(closing) and closing == source['award_context']
             and closing['hash'] == artifact['source_context_hash']
             and closing['signal_day'] == artifact['signal_day'], 'intraday_closing_context_changed')
    context = artifact['intraday_context']
    _require(validate_intraday_context(context) and context['source_award_context'] == closing
             and context['activation_day'] == EFFECTIVE_DAY
             and economic_book(context['current_book']) == economic_book(closing['initial_state']),
             'intraday_current_book_changed')
    _require(_stamp(artifact['created_at']) >= _stamp(context['observed_at'])
             and _stamp(artifact['created_at']) < _stamp(artifact['expires_at']), 'intraday_artifact_time_invalid')
    champion, frames, limits = artifact['parent_policy'], source['frames'], source['limits']
    _require(canonical_hash(artifact['parent_active']) == artifact['parent_active_hash']
             and artifact['parent_active']['policy'] == champion
             and artifact['parent_active']['candidate_id'] == policy_id(champion),
             'intraday_artifact_parent_invalid')
    _require(frames[-1] == closing['latest_frame'] and limits == closing['limits'], 'intraday_request_frame_changed')
    pool, evaluation = artifact['pool'], artifact['evaluation']
    _require(isinstance(pool, list) and len(pool) == 16 and canonical_hash(pool) == artifact['pool_hash'],
             'intraday_full_pool_required')
    packet = signal_hold_packet(champion, frames)
    identities = [policy_id(item['policy']) for item in pool]
    _require(len(set(identities)) == 16 and set(identities) == set(packet['candidate_ids']),
             'intraday_fixed_pool_changed')
    # This validator checks every pool member/metadata against one reconstructed packet.
    validate_capacity_member(champion, pool[0], pool, limits=limits, frames=frames)
    records, index, audit = evaluation['records'], evaluation['selected_index'], evaluation['audit']
    _require(isinstance(records, list) and len(records) == 16 and type(index) is int and 0 <= index < 16
             and all(len(record) == 3 and record[0] == proposal and record[1] == identity
                     for record, proposal, identity in zip(records, pool, identities))
             and audit.get('status') == 'apply'
             and artifact['selected_candidate_id'] == identities[index]
             and artifact['evaluation_hash'] == canonical_hash(evaluation), 'intraday_selection_invalid')
    validate_capacity_result_records(champion, records, frames, limits)
    verified_index, _ = verify_award_record_evidence(champion, records, frames, limits, context, audit)
    _require(verified_index == index, 'intraday_selected_index_changed')
    return source, records[index][0]['policy']


def validate_receipt(receipt, config_path):
    try:
        artifact = read_json(receipt['artifact_path'])
        _require(artifact['hash'] == receipt['artifact_hash'], 'intraday_artifact_changed')
        source, selected = validate_artifact(artifact, config_path)
        closing = artifact['closing_context']
        expected = {'fingerprint': artifact['fingerprint'], 'account_id': 'competition',
            'signal_day': artifact['signal_day'], 'effective_day': artifact['effective_day'],
            'engine_version': artifact['runtime']['daily'], 'learning_engine_version': artifact['runtime']['learning'],
            'analysis_version': artifact['runtime']['analysis'], 'source_decision_id': artifact['source_decision_id'],
            'source_decision_hash': artifact['source_decision_hash'],
            'source_preparation_hash': artifact['source_preparation_hash'],
            'closing_context_hash': closing['hash'], 'selected_candidate_id': policy_id(selected),
            'selection_hash': artifact['evaluation_hash'],
            'closing_economic_book_hash': canonical_hash(economic_book(closing['initial_state'])),
            'entry_selection': freeze_entry_selection(selected, closing['initial_state']),
            'broker_days': artifact['intraday_context']['session_days'],
            'parent_active_hash': artifact['parent_active_hash']}
        return all(receipt.get(key) == value for key, value in expected.items())
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, ArithmeticError):
        return False


def create_service(store, *, config_path, now, stopping=None):
    versions = runtime_versions(config_path)
    return IntradayPolicyService(store, now=now, engine_version=versions['daily'],
        learning_engine_version=versions['learning'], analysis_version=versions['analysis'],
        approval_id=APPROVAL_ID, effective_day=EFFECTIVE_DAY,
        guard_reader=lambda db: guard_snapshot(db, config_path),
        evaluation_validator=lambda receipt: validate_receipt(receipt, config_path), stopping=stopping)


@contextmanager
def readonly_connection(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        yield db
    finally:
        db.close()


def prepare(artifact_path, *, db_path, config_path, now):
    """Produce an immutable proposal from approved evidence; no DB mutation."""
    artifact_path = Path(artifact_path).resolve()
    artifact = read_json(artifact_path)
    request, selected = validate_artifact(artifact, config_path)
    _require(now.utcoffset() is not None and now.astimezone(KST).date().isoformat() == EFFECTIVE_DAY,
             'intraday_prepare_day_invalid')
    with readonly_connection(db_path) as db:
        guard = guard_snapshot(db, config_path)
        fingerprint = guard['fingerprint']
        state = IntradayPolicyService._read(db, 'daily_policy:' + fingerprint, {})
        parent = state['active']
        _require(canonical_hash(parent) == artifact['parent_active_hash']
                 and parent == artifact['parent_active'] and artifact['fingerprint'] == fingerprint,
                 'intraday_artifact_parent_changed')
        identity = artifact['source_decision_id']
        decision = json.loads(db.execute('SELECT payload FROM daily_policy_decisions WHERE id=?', (identity,)).fetchone()[0])
        source = json.loads(db.execute('SELECT payload FROM daily_policy_preparations WHERE decision_id=?', (identity,)).fetchone()[0])
        _require(canonical_hash(decision) == artifact['source_decision_hash']
                 and canonical_hash(source) == artifact['source_preparation_hash'], 'intraday_source_changed')
    # The read transaction is closed before proof construction or filesystem work.
    closing = artifact['closing_context']
    seed, contract = decision['comparison_seed'], source['signal_inputs']
    _require(all(seed[key] == closing[key] for key in ('initial_state', 'execution_model', 'limits', 'signal_day'))
             and all(value == closing['latest_frame'].get(key) for key, value in contract['frame'].items()
                     if key != 'baseline_signals')
             and contract['baseline_signals'] == closing['latest_frame']['baseline_signals'],
             'intraday_original_closing_context_changed')
    _require(guard['economic_book'] == economic_book(closing['initial_state'])
             and not guard['blocked'] and not guard['protected']
             and not guard['pending_orders'] and not guard['unresolved_orders'], 'intraday_live_book_changed')
    protection = artifact['intraday_context']['current_book']['account_protection']
    live_risk = guard['controls']['risk']
    _require(all(_amount(live_risk[key]) == _amount(protection[key]) for key in ('units', 'peak', 'limit_pct'))
             and live_risk['active'] is protection['active'], 'intraday_live_protection_changed')
    transition_id = 'intraday-' + uuid4().hex
    created = now.astimezone(timezone.utc).isoformat()
    selection = freeze_entry_selection(selected, closing['initial_state'])
    decisions = deepcopy(contract['baseline_signals']) + signals(selected, contract['frame'], entry_context=selection['context'])
    original = source['prepared_run']
    versions = artifact['runtime']
    analysis = deepcopy(original['analysis'])
    analysis.update(decisions=decisions, version_id=versions['analysis'])
    base = {key: value for key, value in analysis['input'].items() if key != 'observed_at'}
    base['analysis_version'] = versions['analysis']
    base_key = canonical_hash(base)
    collection_hash = canonical_hash([base_key, policy_id(selected), versions['learning'],
                                      {key: value for key, value in contract['frame'].items() if key != 'observed_at'}])
    run = {'id': uuid4().hex, 'as_of': artifact['signal_day'], 'created_at': created, 'status': 'ready',
           'input_hash': original['input_hash'], 'version_id': versions['analysis'], 'collection_hash': collection_hash,
           'signals': decisions, 'error': original.get('error'), 'llm_status': original.get('llm_status'),
           'analysis': analysis, 'learning_policy': selected, 'learning_engine_version': versions['learning'],
           'intraday_transition_id': transition_id, 'entry_selection': selection}
    proposal = {'version': VERSION, 'id': transition_id, 'fingerprint': fingerprint, 'account_id': 'competition',
        'created_at': created, 'expires_at': artifact['expires_at'], 'effective_day': EFFECTIVE_DAY,
        'signal_day': artifact['signal_day'], 'parent_active': parent, 'limits': deepcopy(parent['limits']),
        'controls_hash': canonical_hash(guard['controls']), 'economic_book_hash': canonical_hash(guard['economic_book']),
        'source_decision_id': identity, 'source_decision_hash': artifact['source_decision_hash'],
        'source_preparation_hash': artifact['source_preparation_hash'], 'closing_context_hash': closing['hash'],
        'policy': selected, 'candidate_id': policy_id(selected), 'prepared_run': run,
        'engine_version': versions['daily'], 'learning_engine_version': versions['learning'],
        'analysis_version': versions['analysis'], 'authorization': {'id': APPROVAL_ID,
            'scope': 'intraday_policy_transition', 'issued_at': artifact['created_at'],
            'expires_at': artifact['expires_at'], 'parent_revision': parent['revision']}}
    receipt = seal({'version': RECEIPT_VERSION, 'decision': 'apply', 'selected_candidate_id': policy_id(selected),
        'selection_hash': artifact['evaluation_hash'], 'artifact_path': str(artifact_path), 'artifact_hash': artifact['hash'],
        'closing_economic_book_hash': canonical_hash(economic_book(closing['initial_state'])),
        'entry_selection': selection, 'parent_active_hash': canonical_hash(parent),
        'broker_days': artifact['intraday_context']['session_days'],
        **{key: proposal[key] for key in RECEIPT_BINDINGS}})
    _require(validate_receipt(receipt, config_path), 'intraday_prepared_receipt_invalid')
    return seal({**proposal, 'evaluation_receipt': receipt})


def snapshot(db_path):
    with readonly_connection(db_path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intraday_policy_transitions'").fetchone():
            return {'version': VERSION, 'staged': [], 'active': None}
        fingerprint = IntradayPolicyService._read(db, 'policy', {}).get('fingerprint')
        rows = db.execute('SELECT id,effective_day FROM intraday_policy_transitions WHERE fingerprint=?',
                          (fingerprint,)).fetchall()
        active = IntradayPolicyService._read(db, 'daily_policy:' + str(fingerprint), {}).get('active')
        return {'version': VERSION, 'staged': [{'id': row[0], 'effective_day': row[1]} for row in rows],
                'active': active if active and active.get('type') == 'intraday_override' else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('action', choices=('prepare', 'stage', 'activate', 'snapshot'))
    parser.add_argument('--artifact', type=Path)
    parser.add_argument('--proposal', type=Path)
    parser.add_argument('--transition-id')
    parser.add_argument('--broker-days', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    now = lambda: datetime.now(KST)
    db_path = args.directory / 'experiments.sqlite3'
    try:
        if args.action == 'prepare':
            _require(args.artifact is not None and args.output is not None, 'intraday_prepare_paths_required')
            result = prepare(args.artifact, db_path=db_path, config_path=args.config, now=now())
        elif args.action == 'snapshot':
            result = snapshot(db_path)
        else:
            from backend.maintenance import MaintenanceGate
            with file_lock(Path(args.config).resolve().parent / '.local' / 'launcher.lock', blocking=False):
                with file_lock(args.directory / 'runner.lock', blocking=False):
                    with file_lock(args.directory / 'submission.lock', blocking=False):
                        _require(MaintenanceGate(args.directory).snapshot()['closed'] is True,
                                 'intraday_maintenance_must_be_closed')
                        store = ExperimentStore(db_path)
                        service = create_service(store, config_path=args.config, now=now)
                        if args.action == 'stage':
                            proposal = read_json(args.proposal)
                            success = service.stage(proposal)
                            result = {'success': success, 'transition_id': proposal['id'], 'error': service.last_error}
                        else:
                            calendar = read_json(args.broker_days)
                            _require(calendar['date'] == EFFECTIVE_DAY and calendar['is_trading_day'] is True
                                     and bool(calendar['source']) and 0 <= (now() - _stamp(calendar['observed_at'])).total_seconds() <= 180,
                                     'intraday_broker_evidence_stale')
                            success = service.activate(args.transition_id, broker_days=calendar['session_days'])
                            result = {'success': success, 'transition_id': args.transition_id, 'error': service.last_error}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open('x', encoding='utf-8') as handle:
                json.dump(result, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        print(json.dumps(result if args.action == 'snapshot' and not args.output else {
                          'status': 'ok' if result.get('success', True) else 'blocked',
                          'output': str(args.output) if args.output else None,
                          **({key: result.get(key) for key in ('success', 'transition_id', 'error')}
                             if args.action in ('stage', 'activate') else {})}, ensure_ascii=False))
        return 0 if result.get('success', True) else 1
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, ArithmeticError) as error:
        print(json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
