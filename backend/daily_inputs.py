"""Immutable signal-day inputs; current buy permission remains an executor check."""
from copy import deepcopy
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
import hashlib

from backend.experiment_store import encode
from backend.learning_policy import build_frame
from backend.kis import KST
from backend.research_rules import MINIMUM_SESSIONS


VERSION = 'daily-inputs-v1'
AWARD_VERSION = 'award-context-v1'
RESEARCH_VERSION = 'daily-research-inputs-v1'
ENTRY_VERSION = 'frozen-entry-selection-v1'


def _hash(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def freeze_entry_selection(policy, initial_state):
    """Bind an opt-in ranked signal set to its original held/reserved symbols."""
    from backend.learning_policy import canonical_entry_context, policy_id
    if not policy.get('held_before_top_n', False):
        return None
    value = {'version': ENTRY_VERSION, 'policy_id': policy_id(policy),
             'context': canonical_entry_context(initial_state)}
    value['hash'] = _hash(value)
    return value


def pending_entry_selection(pending):
    if not pending['policy'].get('held_before_top_n', False):
        return None
    context = pending.get('award_context')
    if not validate_award_context(context) or pending.get('award_context_hash') != context['hash']:
        raise ValueError('entry_selection_book_unavailable')
    return freeze_entry_selection(pending['policy'], context['initial_state'])


def validate_entry_selection(run, policy, *, expected=None, frame=None):
    """Never repair a missing/tampered context from the current live book."""
    if not policy.get('held_before_top_n', False):
        return True
    from backend.learning_policy import policy_id, signals
    try:
        value = run['entry_selection']
        if (set(value) != {'version', 'policy_id', 'context', 'hash'}
                or value['version'] != ENTRY_VERSION or value['policy_id'] != policy_id(policy)
                or value['hash'] != _hash({key: item for key, item in value.items() if key != 'hash'})
                or expected is not None and value != expected):
            return False
        # Validate shape even when only a compact executable run is available.
        context = value['context']
        book = {'positions': [{'symbol': symbol} for symbol in context['held_symbols']],
                'reserved_entries': [{'symbol': symbol} for symbol in context['reserved_symbols']]}
        if freeze_entry_selection(policy, book) != value:
            return False
        if frame is not None:
            actual = [item for item in run['signals'] if item.get('policy_id')]
            return actual == signals(policy, frame, entry_context=context)
        return True
    except (KeyError, TypeError, ValueError):
        return False


def _frame(data):
    return {key: value for key, value in build_frame(data).items() if key != 'observed_at'}


def _numeric(value):
    if value is None:
        return None
    try:
        text = format(Decimal(str(value)), 'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text
    except (ValueError, InvalidOperation):
        return str(value)


def _source_hash(data):
    """Also detect corrected lookback bars that happen to leave derived features equal."""
    day = str(data['as_of'])
    calendars = {board: sorted({str(value) for value in days if str(value) <= day})
                 for board, days in data.get('calendars', {}).items()}
    histories, benchmarks = {}, {}
    for row in data['rows']:
        series = {str(key): value for key, value in data.get('histories', {}).get(row['symbol'], {}).items()
                  if str(key) <= day}
        sessions = calendars.get(row['board'], sorted(series))[-max(61, MINIMUM_SESSIONS):]
        bars = {}
        for session in sessions:
            bar = series.get(session)
            if not isinstance(bar, dict):
                bars[session] = None
                continue
            values = {}
            for key in ('open', 'high', 'low', 'close', 'volume', 'turnover'):
                values[key] = _numeric(bar.get(key))
            bars[session] = values
        histories[row['symbol']] = bars
    for board in ('KOSPI', 'KOSDAQ'):
        series = {str(key): value for key, value in data.get('benchmarks', {}).get(board, {}).items()
                  if str(key) <= day}
        sessions = calendars.get(board, sorted(series))[-21:]
        benchmarks[board] = {session: _numeric(series.get(session)) for session in sessions}
    return _hash({'as_of': day, 'histories': histories, 'calendars': calendars, 'benchmarks': benchmarks})


def freeze_inputs(data):
    frame = _frame(data)
    universe = sorted(({'symbol': row['symbol'], 'name': str(row.get('name', row['symbol'])),
                        'board': row['board'], 'learning_eligible': row.get('learning_eligible', True) is True,
                        'status': row.get('status'), 'selection': deepcopy(row.get('selection')),
                        'as_of': str(row['as_of']) if row.get('as_of') is not None else None}
                       for row in data['rows']), key=lambda row: row['symbol'])
    observed = str(data['observed_at'])
    return {'version': VERSION, 'frame': frame, 'hash': _hash(frame),
            'universe': universe, 'source_hash': _source_hash(data), 'observed_at': observed,
            'context_hash': _hash({'universe': universe, 'observed_at': observed})}


def frozen_data(contract, data):
    """Keep the original universe and eligibility; never create new current-day candidates."""
    if not validate_inputs(contract, data):
        raise ValueError('daily_signal_inputs_changed')
    result = deepcopy(data)
    result['observed_at'] = contract['observed_at']
    current_rows = {row['symbol']: row for row in result['rows']}
    result['rows'] = [{**current_rows[row['symbol']], **deepcopy(row)} for row in contract['universe']]
    return result


def validate_inputs(contract, data):
    if not isinstance(contract, dict) or not isinstance(data, dict):
        return False
    try:
        if contract.get('version') != VERSION or _hash(contract['frame']) != contract['hash']:
            return False
        if _hash({'universe': contract['universe'], 'observed_at': contract['observed_at']}) != contract['context_hash']:
            return False
        if 'baseline_signals' in contract and _hash(contract['baseline_signals']) != contract.get('baseline_hash'):
            return False
        if str(data['as_of']) != contract['frame']['as_of']:
            return False
        current_rows = {row['symbol']: row for row in data['rows']}
        if len(current_rows) != len(data['rows']):
            return False
        rows = []
        for original in contract['universe']:
            current = current_rows.get(original['symbol'])
            if current is None:
                return False
            rows.append({**current, 'learning_eligible': original['learning_eligible']})
        scoped = {**data, 'rows': rows}
        return (_frame(scoped) == contract['frame'] and _source_hash(scoped) == contract['source_hash'])
    except (KeyError, TypeError, ValueError):
        return False


def input_matches(pending, data):
    contract = pending.get('signal_inputs')
    if 'signal_inputs' in pending:
        return (isinstance(contract, dict) and contract.get('hash') == pending.get('market_input_hash')
                and validate_inputs(contract, data))
    return _hash(_frame(data)) == pending.get('market_input_hash')


def frame_for_pending(pending, data):
    if not input_matches(pending, data):
        raise ValueError('daily_signal_inputs_changed')
    contract = pending.get('signal_inputs')
    return deepcopy(contract['frame']) if contract is not None else build_frame(data)


def freeze_award_context(*, signal_day, observed_at, latest_frame, initial_state,
                         execution_model, competition_context, limits):
    """Freeze a contemporaneous migration input, never a historical book override."""
    from backend.learning_research import public_competition_context
    value = {'version': AWARD_VERSION, 'signal_day': str(signal_day), 'observed_at': observed_at,
             'latest_frame': deepcopy(latest_frame), 'initial_state': deepcopy(initial_state),
             'execution_model': deepcopy(execution_model),
             'competition_context': public_competition_context(competition_context),
             'limits': deepcopy(limits)}
    value['hash'] = _hash(value)
    if not validate_award_context(value):
        raise ValueError('invalid_award_context')
    return value


def validate_award_context(value):
    try:
        if (not isinstance(value, dict) or value.get('version') != AWARD_VERSION
                or set(value) != {'version', 'signal_day', 'observed_at', 'latest_frame',
                    'initial_state', 'execution_model', 'competition_context', 'limits', 'hash'}
                or value['hash'] != _hash({key: item for key, item in value.items() if key != 'hash'})):
            return False
        observed = datetime.fromisoformat(value['observed_at'])
        book = value['initial_state']
        amounts = {key: Decimal(str(value['limits'][key])) for key in ('budget', 'order_cap', 'daily_buy_limit')}
        cash, reserved = Decimal(str(book['cash'])), Decimal(str(book.get('reserved_cash', 0)))
        return (observed.utcoffset() is not None
                and observed.astimezone(KST).date().isoformat() == value['signal_day']
                and observed.astimezone(KST).time() >= time(16)
                and value['latest_frame']['as_of'] == value['signal_day']
                and value['initial_state']['as_of'] == value['signal_day']
                and not value['initial_state'].get('pending_orders')
                and not value['initial_state'].get('unresolved_orders')
                and isinstance(value['execution_model'], dict)
                and (value['competition_context'] is None or isinstance(value['competition_context'], dict))
                and set(value['limits']) == {'budget', 'order_cap', 'daily_buy_limit'}
                and all(item.is_finite() and item > 0 for item in amounts.values())
                and amounts['order_cap'] <= amounts['daily_buy_limit'] <= amounts['budget']
                and cash.is_finite() and reserved.is_finite() and 0 <= reserved <= cash
                and ('budget' not in book or Decimal(str(book['budget'])) == amounts['budget']))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False


def freeze_research_inputs(champion, frames, limits, history, award_context):
    value = {'version': RESEARCH_VERSION, 'champion': deepcopy(champion), 'frames': deepcopy(frames),
             'limits': deepcopy(limits), 'history': deepcopy(history), 'award_context': deepcopy(award_context)}
    value['hash'] = _hash(value)
    if not validate_research_inputs(value):
        raise ValueError('invalid_daily_research_inputs')
    return value


def validate_research_inputs(value):
    try:
        return (isinstance(value, dict) and value.get('version') == RESEARCH_VERSION
                and set(value) == {'version', 'champion', 'frames', 'limits', 'history', 'award_context', 'hash'}
                and value['hash'] == _hash({key: item for key, item in value.items() if key != 'hash'})
                and validate_award_context(value['award_context'])
                and value['limits'] == value['award_context']['limits']
                and isinstance(value['champion'], dict) and isinstance(value['frames'], list)
                and bool(value['frames']) and value['frames'][-1] == value['award_context']['latest_frame']
                and isinstance(value['history'], list))
    except (KeyError, TypeError, ValueError):
        return False
