"""Forward-only research quotes, independent of live first-decision evidence."""
from copy import deepcopy
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from backend.experiment_store import encode
from backend.kis import KST


VERSION = 'research-quote-batch-v1'
BUDGET = 5
PREFIX = 'research_observation_batch:'


def _hash(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def _symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z0-9]{6,12}', value):
        raise ValueError('invalid_research_observation_symbol')
    return value


def _controls(policy):
    return {key: policy.get(key) for key in ('fingerprint', 'execution_strategy', 'budget', 'order_cap', 'daily_buy_limit')}


def _active(state):
    active = state.get('active') or {}
    return {'revision': state.get('revision'),
            'active': {key: active.get(key) for key in ('candidate_id', 'run_id', 'effective_day', 'limits')}}


def book_symbols(value):
    """Only coverage requirements: no replay, economic decision or book mutation."""
    result = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {'positions', 'reserved_entries', 'pending_entries', 'pending_orders', 'unresolved_orders'}:
                if isinstance(nested, list):
                    result.update(_symbol(item['symbol']) for item in nested
                                  if isinstance(item, dict) and 'symbol' in item)
            result.update(book_symbols(nested))
    elif isinstance(value, list):
        for nested in value:
            result.update(book_symbols(nested))
    return result


def scope_for(*, day, fingerprint, frames, books, provenance=(), runtime_revision=None):
    """A quoted historical name can remain in any counterfactual descendant book."""
    frames = [frame for frame in frames if frame['as_of'] < day]
    if not frames or not fingerprint:
        return None
    latest = frames[-1]
    captured = datetime.fromisoformat(latest['captured_at'])
    if captured.utcoffset() is None or captured >= datetime.combine(
            datetime.fromisoformat(day).date(), time(9), KST):
        return None
    pool = {_symbol(row['symbol']) for row in latest['rows']}
    historical = set()
    sources = []
    for frame in frames:
        quotes = {key: frame.get(key, {}) for key in ('execution_quotes', 'protection_quotes')}
        for values in quotes.values():
            if not isinstance(values, dict):
                raise ValueError('invalid_research_observation_source')
            historical.update(_symbol(symbol) for symbol in values)
        sources.append([frame['as_of'], frame.get('captured_at'),
                        (frame.get('signal_inputs') or {}).get('hash'), _hash(quotes)])
    held = book_symbols(books)
    # Changes to live cash/quantity/as_of do not invalidate price coverage.
    # Outside-pool holdings and immutable seed/context revisions do invalidate it.
    book_revision = _hash([list(provenance), sorted(held - pool - historical)])
    required = sorted(pool | historical | held)
    value = {'version': VERSION, 'day': day, 'fingerprint': fingerprint,
             'signal_day': latest['as_of'], 'source_revision': _hash([sources, runtime_revision]),
             'book_revision': book_revision, 'pool_revision': _hash(sorted(pool)),
             'required_revision': _hash(required), 'required': required}
    if runtime_revision:
        value.update(policy_revision=_hash(runtime_revision['controls']),
                     active_revision=_hash(runtime_revision['daily_state']))
    value['id'] = _hash(value)
    return value


def current_scope(service, policy):
    fresh_policy = service._policy()
    if _controls(fresh_policy) != _controls(policy):
        return None
    day = service.now().astimezone(KST).date().isoformat()
    frames = service.learning.frames()
    books = [service._entry_selection_book(), service.learning.state()]
    daily = service.store.setting('daily_policy:' + policy['fingerprint'], {})
    runtime_revision = {'learning_engine': service.learning.version,
        'daily_engine': service.learning.daily.engine_version, 'daily_state': _active(daily),
        'controls': _controls(fresh_policy)}
    provenance = []
    # Read all stored source frames, including older quote names outside today's
    # pool. No valid replay prefix is required to retain this conservative set.
    with service.store.connect() as db:
        rows = db.execute('SELECT payload FROM learning_frames WHERE day<? ORDER BY day', (day,)).fetchall()
        if rows:
            frames = [json.loads(row[0]) for row in rows]
        for frame_day, raw in db.execute('SELECT day,payload FROM daily_policy_contexts '
                'WHERE fingerprint=? AND day<? ORDER BY day', (policy['fingerprint'], day)):
            value = json.loads(raw)
            books.append(value['initial_state'])
            provenance.append(['context', frame_day, _hash(value)])
        for identity, raw in db.execute('SELECT id,payload FROM daily_policy_followups '
                'WHERE fingerprint=? ORDER BY id', (policy['fingerprint'],)):
            value = json.loads(raw)
            seed = value.get('comparison_seed')
            if isinstance(seed, dict) and seed.get('signal_day', day) < day:
                books.append(seed)
                provenance.append(['followup', identity, _hash(seed)])
    return scope_for(day=day, fingerprint=policy.get('fingerprint'), frames=frames,
                     books=books, provenance=provenance, runtime_revision=runtime_revision)


def _new(scope):
    return {'scope': deepcopy(scope), 'status': 'collecting_protection',
            'protection_quotes': {}, 'execution_quotes': {},
            'attempts': {}, 'protection_completed_at': None, 'completed_at': None}


def _observed(value, now, day):
    observed = datetime.fromisoformat(value)
    if (observed.utcoffset() is None or observed.astimezone(KST).date().isoformat() != day
            or not 0 <= (now - observed).total_seconds() <= 180):
        raise ValueError('invalid_research_observation_time')
    return observed


def _price(value):
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('invalid_research_observation_price') from None
    if not price.is_finite() or price <= 0:
        raise ValueError('invalid_research_observation_price')
    return price


def protection_quote(broker, symbol, now, day):
    # Valuation does not require permission to trade or a positive recent minute.
    # A suspended holding still needs its real observed valuation, never a close
    # substituted from a later frame or an execution-price permission.
    quote = broker.quote(symbol)
    observed = _observed(quote['as_of'], now(), day)
    return {'price': str(_price(quote['price'])), 'observed_at': observed.isoformat(),
            'source': 'research_quote'}


def execution_quote(broker, symbol, now, day, buy_eligible):
    quote = broker.quote(symbol)
    observed = _observed(quote['as_of'], now(), day)
    result = {'price': None, 'eligible': False, 'buy_eligible': bool(buy_eligible),
              'observed_at': observed.isoformat()}
    if not quote.get('eligible'):
        if quote.get('reason') == 'status_unknown':
            raise ValueError('research_quote_status_unknown')
        return result
    session = broker.market_session(symbol)
    last = datetime.fromisoformat(session['last_trade_at'])
    price = _price(quote['price'])
    if (last.utcoffset() is None or session['session_date'] != day
            or not 0 <= (now() - last).total_seconds() <= 180 or session['volume'] <= 0
            or price < 1000 or price != price.to_integral_value()):
        return result
    return {**result, 'price': str(price), 'eligible': True,
            'volume': session['volume'], 'last_trade_at': session['last_trade_at']}


def _save(store, scope, phase=None, symbol=None, quote=None, at=None, failed=False):
    """Short compare/merge transaction; no lock survives a broker call."""
    key = PREFIX + scope['id']
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        policy = db.execute("SELECT value FROM settings WHERE key='policy'").fetchone()
        current = json.loads(policy[0]) if policy else {}
        daily = db.execute('SELECT value FROM settings WHERE key=?',
                           ('daily_policy:' + scope['fingerprint'],)).fetchone()
        active = _active(json.loads(daily[0]) if daily else {})
        if (_hash(_controls(current)) != scope['policy_revision']
                or _hash(active) != scope['active_revision']):
            raise ValueError('research_observation_scope_conflict')
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        batch = json.loads(row[0]) if row else _new(scope)
        if batch['scope'] != scope:
            raise ValueError('research_observation_scope_conflict')
        if phase is not None and batch['status'] == phase:
            field = 'protection_quotes' if phase == 'collecting_protection' else 'execution_quotes'
            batch.setdefault('attempts', {}).setdefault(phase, {})[symbol] = {
                'at': at, 'status': 'unavailable' if failed else 'observed'}
            if not failed:
                batch[field].setdefault(symbol, deepcopy(quote))
            if set(batch[field]) == set(scope['required']):
                if phase == 'collecting_protection':
                    batch.update(status='collecting_execution', protection_completed_at=at)
                else:
                    batch.update(status='complete', completed_at=at)
        db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value '
                   'WHERE settings.value!=excluded.value', (key, encode(batch)))
    return batch


def observe(service, policy, *, budget=BUDGET):
    scope = current_scope(service, policy)
    if scope is None or not scope['required']:
        return
    batch = _save(service.store, scope)
    if batch['status'] == 'complete':
        return
    broker = service._broker(policy)
    if scope['day'] not in broker.session_days(scope['signal_day'], scope['day']):
        return
    account = None
    owned = None
    for _ in range(min(BUDGET, max(0, budget))):
        if (getattr(service, 'stopping', False) or not service._market_open()
                or service._policy().get('fingerprint') != scope['fingerprint']):
            return
        # Re-check revisions before each quote and after network I/O. A completed
        # old scope remains immutable, but never satisfies a changed requirement.
        if current_scope(service, policy) != scope:
            return
        phase = batch['status']
        if phase == 'complete':
            return
        field = 'protection_quotes' if phase == 'collecting_protection' else 'execution_quotes'
        attempted = batch.get('attempts', {}).get(phase, {})
        symbol = min((symbol for symbol in scope['required'] if symbol not in batch[field]),
                     key=lambda item: (item in attempted, attempted.get(item, {}).get('at', ''), item))
        try:
            if phase == 'collecting_protection':
                quote = protection_quote(broker, symbol, service.now, scope['day'])
            else:
                if account is None:
                    account = broker.snapshot()
                    positions, _ = service._portfolio(service._owned_orders(policy))
                    owned = {}
                    for position in positions:
                        owned[position['symbol']] = owned.get(position['symbol'], 0) + position['quantity']
                quote = execution_quote(broker, symbol, service.now, scope['day'],
                    account.get('holdings', {}).get(symbol, {}).get('quantity', 0) <= owned.get(symbol, 0))
                if datetime.fromisoformat(quote['observed_at']) < datetime.fromisoformat(batch['protection_completed_at']):
                    raise ValueError('research_execution_before_protection_barrier')
        except Exception:
            if (service._policy().get('fingerprint') == scope['fingerprint']
                    and current_scope(service, policy) == scope):
                _save(service.store, scope, phase, symbol, at=service._timestamp(), failed=True)
            raise
        if (not service._market_open() or service._policy().get('fingerprint') != scope['fingerprint']
                or current_scope(service, policy) != scope):
            return
        batch = _save(service.store, scope, phase, symbol, quote, service._timestamp())


def _complete(batch, scope, now):
    try:
        if (not scope or batch.get('scope') != scope or batch.get('status') != 'complete'
                or set(batch.get('protection_quotes', {})) != set(scope['required'])
                or set(batch.get('execution_quotes', {})) != set(scope['required'])):
            return False
        def instant(value):
            stamp = datetime.fromisoformat(value)
            if (stamp.utcoffset() is None or stamp.astimezone(KST).date().isoformat() != scope['day']
                    or stamp > now):
                raise ValueError('invalid_research_batch_time')
            return stamp
        barrier, completed = (instant(batch[key]) for key in ('protection_completed_at', 'completed_at'))
        if barrier > completed:
            return False
        for quote in batch['protection_quotes'].values():
            if instant(quote['observed_at']) > barrier or _price(quote['price']) <= 0:
                return False
        for quote in batch['execution_quotes'].values():
            if (not barrier <= instant(quote['observed_at']) <= completed
                    or type(quote.get('eligible')) is not bool or type(quote.get('buy_eligible')) is not bool
                    or quote['eligible'] and _price(quote['price']) <= 0):
                return False
        return True
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def context(service, policy):
    scope = current_scope(service, policy)
    batch = service.store.setting(PREFIX + scope['id'], {}) if scope else {}
    if not isinstance(batch, dict):
        batch = {'status': 'unavailable'}
    metadata = {'version': VERSION, 'id': scope['id'] if scope else None,
                'status': batch.get('status', 'missing')}
    if scope:
        metadata.update({key: scope[key] for key in ('day', 'signal_day', 'source_revision',
            'book_revision', 'pool_revision', 'required_revision')})
        metadata.update(required_count=len(scope['required']),
                        protection_count=len(batch['protection_quotes']) if isinstance(batch.get('protection_quotes'), dict) else 0,
                        execution_count=len(batch['execution_quotes']) if isinstance(batch.get('execution_quotes'), dict) else 0,
                        protection_completed_at=batch.get('protection_completed_at'),
                        completed_at=batch.get('completed_at'))
    complete = _complete(batch, scope, service.now())
    if metadata['status'] == 'complete' and not complete:
        metadata['status'] = 'unavailable'
    return {'research_observation': metadata,
            'protection_quotes': deepcopy(batch['protection_quotes']) if complete else {},
            'execution_quotes': deepcopy(batch['execution_quotes']) if complete else {}}
