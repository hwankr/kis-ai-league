"""Exact, expiring execution recovery. Never a research/activation version alias."""
from datetime import time
import hashlib
import json
from pathlib import Path

from backend.experiment_store import encode
from backend.kis import KST


class RecoveryError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode('utf-8')).hexdigest()


def deployment_hash():
    return digest(encode({path.name: digest(path.read_bytes())
                          for path in sorted(Path(__file__).parent.glob('*.py'))}))


# Captured when experiments imports this module at startup; hot source replacement
# cannot grant a previously loaded process a new recovery permission.
LOADED_DEPLOYMENT = deployment_hash()


def resolve(service, *, run_id=None, token=None):
    """Read a reviewed external manifest and its immutable evidence; no writes/I/O to a broker.

    The operator seals the final source/config hashes only after offline review.
    Absence/expiry grants nothing. A present invalid manifest fails closed.
    Current market inputs and the actual prior session must ALSO pass the runner's
    full frozen-input validator before this summary may be used for execution.
    """
    path = service.directory / 'entry-recovery.json'
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        if len(raw) > 16384:
            raise ValueError('oversized_manifest')
        manifest = json.loads(raw)
        current = service.now().astimezone(KST)
        if (manifest['execution_day'] != current.date().isoformat()
                or not time(9) <= current.time() < time(16)):
            return None
        if (type(manifest['version']) is not int or manifest['version'] != 1 or token is not None and digest(raw) != token
                or run_id is not None and manifest['run_id'] != run_id):
            raise ValueError('manifest_identity')
        from backend.experiment_analysis import analysis_version, make_llm
        policy, active = service._policy(), service.learning.daily.active()
        adopted = service._adopted_policy()
        if (not active or not adopted or active['policy'] != adopted
                or service.learning.daily.pending() is not None
                or manifest['active_sha256'] != digest(encode(active))
                or manifest['policy_sha256'] != digest(encode(policy))
                or manifest['config_sha256'] != digest(service.config_path.read_bytes())
                or manifest['deployment_sha256'] != LOADED_DEPLOYMENT
                or deployment_hash() != LOADED_DEPLOYMENT
                or manifest['current_analysis'] != analysis_version(service.config_path)
                or manifest['current_learning'] != service.learning.version
                or manifest['current_daily'] != service.learning.daily.engine_version
                or manifest['llm'] != make_llm(service.config_path).public_config()):
            raise ValueError('execution_binding')
        with service.store.connect() as db:
            db.execute('BEGIN')
            full = db.execute('SELECT payload FROM runs WHERE id=?', (manifest['run_id'],)).fetchone()
            summary = db.execute('SELECT summary FROM run_index WHERE id=?', (manifest['run_id'],)).fetchone()
            frame = db.execute('SELECT payload FROM learning_frames WHERE day=?', (manifest['signal_day'],)).fetchone()
            book = db.execute('SELECT payload FROM daily_policy_contexts WHERE fingerprint=? AND day=?',
                              (policy['fingerprint'], manifest['signal_day'])).fetchone()
            # A failed newer run may own the issue. Pin it as rejected evidence;
            # this never grants that run execution or changes any of its bytes.
            for identity, pins in manifest.get('rejected_runs', {}).items():
                rejected = db.execute('SELECT payload FROM runs WHERE id=?', (identity,)).fetchone()
                rejected_summary = db.execute('SELECT summary FROM run_index WHERE id=?', (identity,)).fetchone()
                if (not rejected or not rejected_summary or digest(rejected[0]) != pins['run']
                        or digest(rejected_summary[0]) != pins['summary']):
                    raise ValueError('rejected_run_evidence')
        records = dict(zip(('run', 'summary', 'frame', 'book'), (full, summary, frame, book)))
        if any(row is None or digest(row[0]) != manifest['records'][name] for name, row in records.items()):
            raise ValueError('original_evidence')
        run = json.loads(summary[0])
        original = json.loads(full[0])
        context = json.loads(book[0])
        from backend.daily_inputs import freeze_entry_selection
        if (run.get('id') != manifest['run_id'] or 'analysis' in run
                or {k: v for k, v in original.items() if k != 'analysis'} != run
                or run['version_id'] != manifest['original_analysis']
                or run['learning_engine_version'] != manifest['original_learning']
                or context['engine_version'] != manifest['original_daily']
                or context['initial_state']['as_of'] != manifest['signal_day']
                or context['initial_state'].get('pending_orders') or context['initial_state'].get('unresolved_orders')
                or context['limits'] != {k: str(policy[k]) for k in ('budget', 'order_cap', 'daily_buy_limit')}
                or run['entry_selection'] != freeze_entry_selection(adopted, context['initial_state'])
                or active.get('effective_day') == manifest['execution_day'] and active.get('run_id') != run['id']
                or run['as_of'] != manifest['signal_day'] or run['learning_policy'] != adopted
                or original['analysis']['llm_config'] != manifest['llm']):
            raise ValueError('original_binding')
        return {'run': run, 'token': digest(raw), 'failure_owners': manifest.get('failure_owners', {}),
                'rejected_run_ids': sorted(manifest.get('rejected_runs', {}))}
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as error:
        raise RecoveryError('entry_recovery_evidence_mismatch') from error
