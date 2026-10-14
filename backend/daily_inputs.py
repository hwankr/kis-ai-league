"""Immutable signal-day inputs; current buy permission remains an executor check."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib

from backend.experiment_store import encode
from backend.learning_policy import build_frame
from backend.research_rules import MINIMUM_SESSIONS


VERSION = 'daily-inputs-v1'


def _hash(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


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
