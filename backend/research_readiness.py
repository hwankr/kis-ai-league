"""Preparation evidence for new daily research; never retries a frozen decision."""
from copy import deepcopy
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
import hashlib

from backend.experiment_store import encode
from backend.kis import KST


WAITING = 'waiting_research_inputs'


def input_readiness(context, day, now):
    context = context or {}
    observation = context.get('research_observation')
    reasons = []
    if observation is not None and (not isinstance(observation, dict) or observation.get('status') != 'complete'):
        reasons.append('research_observation_incomplete')
    progress = context.get('competition_context')
    source = context.get('competition_observation')
    if progress is not None or source is not None:
        try:
            period = progress['period']
            if (progress.get('status') != 'ok' or progress.get('reliable') is not True
                    or period['as_of'] != day or not period['start_date'] <= day <= period['end_date']
                    or progress.get('pace', {}).get('status') in (None, 'unavailable')):
                raise ValueError
            for key in ('trading_days', 'symbols', 'api_orders'):
                value = progress['metrics'][key]['value']
                if type(value) is not int or value < 0:
                    raise ValueError
            gross = progress['pace']['gross_amount']
            if isinstance(gross, bool) or not Decimal(str(gross)).is_finite() or Decimal(str(gross)) < 0:
                raise ValueError
            # The public aggregates deliberately omit account identifiers. Check
            # the separately allowlisted source metadata before accepting them.
            if source is not None:
                stamp = datetime.fromisoformat(source['updated_at'])
                if (source.get('account_id') != 'competition' or source.get('status') != 'ok'
                        or source.get('coverage') != 'complete' or source.get('stale') is not False
                        or source.get('period') != period or stamp.utcoffset() is None
                        or stamp.astimezone(KST).date().isoformat() != day
                        or stamp.astimezone(KST).time() < time(16)
                        or not 0 <= (now - stamp).total_seconds() <= 180):
                    raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError, InvalidOperation):
            reasons.append('competition_progress_unavailable')
    return {'status': 'waiting' if reasons else 'ready', 'reasons': reasons}


def record_preparation(store, fingerprint, day, context, readiness, observed_at):
    """Append aggregate source responses, outside immutable frame/decision tables."""
    value = {'fingerprint': fingerprint, 'day': day, 'observed_at': observed_at,
             'readiness': deepcopy(readiness),
             **{key: deepcopy((context or {}).get(key)) for key in
                ('competition_context', 'competition_observation', 'research_observation')}}
    payload = encode(value)
    identity = hashlib.sha256(payload.encode()).hexdigest()
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS daily_research_preparation_observations '
                   '(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL)')
        db.execute('INSERT OR IGNORE INTO daily_research_preparation_observations VALUES (?,?,?,?)',
                   (identity, fingerprint, day, payload))
    return identity
