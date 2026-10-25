"""Explicit historical analogues from today's frozen book, never forecasts."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
import re

from backend.learning_policy import (FEE, TAX, KST, LEGACY_IDS, _bar, _execution_model,
                                     _initial_state, _state_decimal, portfolio_metrics, validate_policy)
from backend.policy_transition import transition_preview


VERSION = 'policy-award-scenarios-v3'
PLAN_VERSION = 'quality-only-common-windows-v2'
BASIS = 'historical_analogue_scenario'
TERMINAL_BASIS = 'zero_interval_terminal_mark'
ZERO_ACTIVITY = ('gross_amount', 'gross_buy_amount', 'gross_sell_amount', 'trading_days',
                 'symbols', 'api_orders', 'buy_fill_events', 'sell_fill_events', 'fees_paid', 'taxes_paid')


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False, default=str).encode()).hexdigest()


def _base():
    return {'version': VERSION, 'basis': BASIS, 'status': 'unavailable', 'reason': None,
            'scenarios': [], 'assumptions': [
                'First/middle/recent complete common windows are chosen by source coverage before policy evaluation.',
                'Overlapping historical paths are scenarios, not independent samples, probabilities or forecasts.',
                'Every path starts from the same current book and current completed-close signals.',
                'Later OHLC prices preserve historical relative moves; historical features and eligibility are assumptions.',
                'Selection metadata retains its historical dates; scope uses the recorded completeness and liquidity values.',
                'Future weekday labels are assumed sessions, not a verified exchange calendar.',
                'Gross amounts and trading days are additional modelled fills only; actual totals are not added here.',
                'Final equity marks unsold holdings at close without forced terminal sales.',
                'A zero-session shortened schedule is one terminal snapshot with three explicit window aliases; its original weights remain unchanged.',
                'Fill events do not prove accepted API orders; api_orders is unknown for modelled fills and zero for no additional intervals.',
                'Daily-open fills cannot establish future broker, quote or destination-contest eligibility.']}


def _unavailable(identity, window, horizon, environment, reason):
    return {'id': identity, 'window': window, 'horizon': horizon, 'environment': environment,
            'stress': environment != 'normal', 'status': 'unavailable', 'reason': reason,
            **dict.fromkeys(('gross_amount', 'gross_buy_amount', 'gross_sell_amount', 'trading_days',
                            'symbols', 'api_orders', 'final_equity', 'max_drawdown_pct', 'first_buy_session'))}


def _history(frames, latest):
    if not isinstance(frames, list) or not frames:
        raise ValueError('historical_frames_unavailable')
    ordered, prior = [], None
    observed = datetime.fromisoformat(latest['observed_at'])
    for frame in frames:
        day = frame.get('as_of')
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError('invalid_historical_day')
        when = datetime.fromisoformat(frame.get('observed_at', ''))
        if (when.utcoffset() is None or when < datetime.combine(date.fromisoformat(day), time(16), KST)
                or when > observed or day > latest['as_of']):
            raise ValueError('historical_frame_not_observed')
        if prior is not None and day <= prior:
            raise ValueError('unordered_historical_frames')
        calendar = frame.get('session_days')
        if (not isinstance(calendar, list) or not calendar or calendar != sorted(set(calendar))
                or calendar[-1] != day or any(not isinstance(session, str)
                    or date.fromisoformat(session).isoformat() != session for session in calendar)):
            raise ValueError('historical_calendar_unavailable')
        # Do not close calendar gaps by assigning consecutive assumed future dates.
        if prior and any(prior < session < day for session in calendar):
            raise ValueError('missing_historical_session')
        prior = day
        ordered.append(frame)
    return ordered


def _rows(frame):
    rows = frame.get('rows')
    if not isinstance(rows, list) or not rows or frame.get('missing_symbols'):
        raise ValueError('incomplete_historical_frame')
    result = {}
    for row in rows:
        symbol = row.get('symbol')
        if (not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9]{6}', symbol)
                or symbol in result or row.get('board') not in {'KOSPI', 'KOSDAQ'}
                or type(row.get('learning_eligible')) is not bool or not isinstance(row.get('features'), dict)):
            raise ValueError('invalid_historical_row')
        _bar(row)
        result[symbol] = row
    return result


def _legacy_observed(frame):
    baseline = frame.get('baseline_signals')
    return (isinstance(baseline, list) and bool(baseline)
            and {item.get('strategy_id') for item in baseline} == set(LEGACY_IDS)
            and len({(item.get('strategy_id'), item.get('symbol')) for item in baseline}) == len(baseline)
            and all(item.get('status') == 'ready' and item.get('action') in {'buy', 'hold', 'avoid'}
                    and item.get('symbol') in {row['symbol'] for row in frame['rows']}
                    for item in baseline))


def _assumed_days(start, count):
    day, values = date.fromisoformat(start), [start]
    while len(values) <= count:
        day += timedelta(days=1)
        if day.weekday() < 5:
            values.append(day.isoformat())
    return values


def _reason(error, default='invalid_historical_path'):
    value = str(error)
    return value if re.fullmatch(r'[a-z][a-z0-9_]*', value) else default


def _plan_inputs(frames, context):
    latest = context['latest_frame']
    competition = context.get('competition_context')
    if not isinstance(competition, dict) or not isinstance(competition.get('pace'), dict):
        raise ValueError('remaining_sessions_unavailable')
    horizon = competition['pace'].get('remaining_sessions')
    if type(horizon) is not int or not 1 <= horizon <= 366:
        raise ValueError('remaining_sessions_unavailable')
    end = competition.get('period', {}).get('end_date')
    if not isinstance(end, str) or date.fromisoformat(end).isoformat() != end:
        raise ValueError('competition_end_unavailable')
    if _assumed_days(latest['as_of'], horizon)[-1] > end:
        raise ValueError('remaining_sessions_exceed_competition_period')
    return latest, _history(frames, latest), horizon, end


def scenario_plan(frames, context):
    """Select common source windows from completeness alone, before any policy.

    Every currently available symbol may be selected by a candidate. Requiring
    that same universe throughout a window avoids candidate-specific coverage
    filtering. Missing history stays missing; no rows or baseline signals are
    synthesized. The incumbent's original signal evidence is checked separately.
    """
    result = {'version': PLAN_VERSION, 'status': 'unavailable', 'reason': None,
              'selection_rule': 'chronological_first_middle_recent_of_complete_common_windows',
              'performance_inputs_used': False, 'schedules': [], 'overlap': [],
              'independent_paths': False}
    try:
        latest, history, horizon, end = _plan_inputs(frames, context)
        current = _rows(latest)
        book = context['initial_state']
        positions = book.get('positions')
        if (not isinstance(positions, list) or any(not isinstance(item, dict)
                or not isinstance(item.get('symbol'), str) for item in positions)):
            raise ValueError('invalid_current_book')
        held = {item['symbol'] for item in positions}
        if not held <= current.keys():
            raise ValueError('holding_current_price_missing')
        result.update(history_hash=_hash(history), current_frame_hash=_hash(latest),
                      start_book_hash=_hash(book), remaining_sessions=horizon,
                      competition_end=end, required_current_symbols=sorted(current),
                      required_holding_symbols=sorted(held))
        quality = []
        result['frame_coverage'] = []
        for frame in history:
            try:
                rows = _rows(frame)
                if not held <= rows.keys():
                    raise ValueError('holding_path_price_missing')
                if not rows.keys() <= current.keys():
                    raise ValueError('normalization_price_missing')
                if not current.keys() <= rows.keys():
                    raise ValueError('current_universe_path_price_missing')
                quality.append(None)
            except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError) as error:
                quality.append(_reason(error))
            missing = frame.get('missing_symbols')
            result['frame_coverage'].append({'day': frame['as_of'],
                'row_count': len(frame.get('rows') or []),
                'missing_symbol_count': len(missing) if isinstance(missing, list) else None,
                'missing_current_holdings': sorted(held & set(missing or [])),
                'reason': quality[-1]})
        selected_windows = []
        for label, length in [('full', horizon), ('shortened', max(0, horizon - 2))]:
            if not length:
                result['schedules'].append({'schedule': label, 'horizon': 0,
                    'basis': TERMINAL_BASIS, 'unique_path_count': 1,
                    'available_window_count': 0, 'tested_window_count': 0,
                    'excluded_reason_counts': {}, 'source_windows': [],
                    'selected': [{'window': window, 'status': 'ready', 'basis': TERMINAL_BASIS,
                        'alias_of': 'first_shortened', 'source_start_index': None,
                        'source_start': None, 'source_end': None, 'source_intervals': 0}
                        for window in ('first', 'middle', 'recent')]})
                continue
            records = []
            for start in range(max(0, len(history) - length)) if length else []:
                source = history[start:start + length + 1]
                failures = sorted({reason for reason in quality[start:start + length + 1] if reason})
                records.append({'source_start_index': start, 'source_start': source[0]['as_of'],
                    'source_end': source[-1]['as_of'], 'source_intervals': length,
                    'status': 'unavailable' if failures else 'ready', 'reasons': failures})
            valid = [item for item in records if item['status'] == 'ready']
            counts = {}
            for item in records:
                for failure in item['reasons']:
                    counts[failure] = counts.get(failure, 0) + 1
            chosen, used = [], set()
            # A sparse history must not count one source path three times.
            ranks = [('first', 0), ('middle', (len(valid) - 1) // 2), ('recent', len(valid) - 1)]
            for window, rank in ranks:
                if not length:
                    failure = 'insufficient_shortened_schedule'
                elif not records:
                    failure = 'insufficient_historical_horizon'
                elif not valid:
                    failure = 'no_complete_common_window'
                elif rank in used:
                    failure = 'insufficient_distinct_quality_windows'
                else:
                    chosen.append({'window': window, **deepcopy(valid[rank])})
                    used.add(rank)
                    selected_windows.append({'schedule': label, 'window': window, **valid[rank]})
                    continue
                chosen.append({'window': window, 'status': 'unavailable', 'reason': failure,
                               'source_start_index': None, 'source_intervals': length})
            result['schedules'].append({'schedule': label, 'horizon': length,
                'available_window_count': len(valid), 'tested_window_count': len(records),
                'excluded_reason_counts': counts, 'source_windows': records, 'selected': chosen})
        for index, left in enumerate(selected_windows):
            for right in selected_windows[index + 1:]:
                start = max(left['source_start_index'], right['source_start_index'])
                end_index = min(left['source_start_index'] + left['source_intervals'],
                                right['source_start_index'] + right['source_intervals'])
                shared = max(0, end_index - start)
                if shared:
                    result['overlap'].append({'left': left['window'] + '_' + left['schedule'],
                        'right': right['window'] + '_' + right['schedule'],
                        'shared_intervals': shared,
                        'fraction_of_shorter': shared / min(left['source_intervals'], right['source_intervals'])})
        complete = all(item['status'] == 'ready' for schedule in result['schedules'] for item in schedule['selected'])
        result.update(status='ready' if complete else 'unavailable',
                      reason=None if complete else 'incomplete_quality_plan')
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError) as error:
        result['reason'] = _reason(error, 'invalid_analogue_context')
    result['hash'] = _hash(result)
    return result


def _path(policy, source, latest, book, *, simulation_anchor_day=None):
    """Keep the current anchor, then transplant a per-symbol relative OHLC path."""
    current, anchor = _rows(latest), _rows(source[0])
    anchor_day = simulation_anchor_day or latest['as_of']
    if (date.fromisoformat(anchor_day).isoformat() != anchor_day
            or anchor_day < latest['as_of']):
        raise ValueError('invalid_simulation_anchor_day')
    dates = _assumed_days(anchor_day, len(source) - 1)
    scales = {symbol: Decimal(str(current[symbol]['close'])) / Decimal(str(row['close']))
              for symbol, row in anchor.items() if symbol in current}
    needed = {position['symbol'] for position in book['positions']}
    if not needed <= scales.keys():
        raise ValueError('holding_anchor_price_missing')
    result = [deepcopy(latest)]
    if simulation_anchor_day is not None:
        # This is a model-only time label, never a newly observed market frame.
        result[0].update(as_of=anchor_day, observed_at=anchor_day + 'T16:05:00+09:00',
            simulation_anchor={'basis': 'no_further_intraday_fills_counterfactual',
                'source_as_of': latest['as_of'], 'source_observed_at': latest['observed_at'],
                'source_frame_hash': _hash(latest), 'observed_close': False})
    result[0]['session_days'] = [dates[0]]
    for index, original in enumerate(source[1:], 1):
        rows = _rows(original)
        if not needed <= rows.keys():
            raise ValueError('holding_path_price_missing')
        if not rows.keys() <= scales.keys():
            raise ValueError('normalization_price_missing')
        frame = deepcopy(original)
        frame.update(as_of=dates[index], observed_at=dates[index] + 'T16:05:00+09:00',
                     session_days=dates[:index + 1])
        frame.pop('execution_quotes', None)
        for row in frame['rows']:
            scale = scales[row['symbol']]
            for field in ('open', 'high', 'low', 'close'):
                row[field] = float(Decimal(str(row[field])) * scale)
        result.append(frame)
    if policy['kind'] == 'legacy' and any(not _legacy_observed(frame) for frame in result):
        raise ValueError('historical_legacy_signals_unavailable')
    if policy['kind'] == 'legacy':
        # The historical replay removes ineligible baseline buys before its legacy
        # quota denominator. Do not turn that known difference from live into proof.
        for frame in result:
            eligible = {row['symbol']: row['learning_eligible'] for row in frame['rows']}
            if any(item['action'] == 'buy' and not eligible[item['symbol']]
                   for item in frame['baseline_signals']):
                raise ValueError('legacy_eligibility_quota_unsupported')
    return result


def _costs(profile):
    # No future observed quotes are implied by the frozen execution-profile evidence.
    profile = {} if profile is None else profile
    if not isinstance(profile, dict) or set(profile) - {
            'fee_rate', 'tax_rate', 'slippage_bps', 'stress_fee_rate', 'stress_slippage_bps'}:
        raise ValueError('invalid_analogue_cost_profile')
    model = _execution_model({'mode': 'daily_open', 'fee_rate': FEE, 'tax_rate': TAX, **profile})
    return {key: str(model[key]) for key in
            ('fee_rate', 'tax_rate', 'slippage_bps', 'stress_fee_rate', 'stress_slippage_bps')}


def _outcome(metric, *, stress):
    suffix = 'stress_' if stress else ''
    activity = metric[suffix + 'trading_activity']
    final = metric[suffix + 'final_state']
    curve = metric[suffix + 'equity_curve']
    diagnostics = metric['diagnostics']['cost_stress' if stress else 'normal']
    # Replay's internal protection mark includes liquidation costs. Award NAV is
    # cash plus unsold close value; remove only those hypothetical mark discounts.
    discount = ((1 - Decimal(str(diagnostics['slippage_bps'])) / 10000)
                * (1 - Decimal(str(diagnostics['fee_rate'])) - Decimal(str(diagnostics['tax_rate']))))
    equities = [Decimal(str(point['cash'])) + Decimal(str(point['position_value'])) / discount for point in curve]
    peak, drawdown = equities[0], Decimal(0)
    for equity in equities:
        peak = max(peak, equity)
        drawdown = max(drawdown, (1 - equity / peak) * 100)
    final_equity = Decimal(final['cash']) + sum((Decimal(item['last_close']) * item['quantity']
                                              for item in final['positions']), Decimal(0))
    return {'status': 'ready', 'reason': None,
            **{key: activity[key] for key in ('gross_amount', 'gross_buy_amount', 'gross_sell_amount',
                                             'trading_days', 'symbols')},
            'api_orders': None, 'final_equity': float(final_equity), 'max_drawdown_pct': float(drawdown),
            'first_buy_session': next((index for index, point in enumerate(curve)
                                       if point['gross_buy_amount'] > 0), None),
            'buy_fill_events': activity['buy_fill_events'], 'sell_fill_events': activity['sell_fill_events'],
            'final_holding_count': len(final['positions']),
            'final_protection': deepcopy(final.get('account_protection')),
            'valuation_basis': 'cash_plus_close_marked_unsold_holdings',
            'fill_basis': 'modelled_fills', 'final_state_hash': _hash(final),
            'execution_diagnostics': {**{key: diagnostics.get(key) for key in
                ('buy_attempts', 'sell_attempts', 'partial_orders', 'unfilled_orders', 'limit_blocked',
                 'untradeable_bars', 'ineligible_quotes', 'fees_paid', 'taxes_paid', 'slippage_bps')},
                 'final_holding_count': len(final['positions'])},
            'fees_paid': diagnostics['fees_paid'], 'taxes_paid': diagnostics['taxes_paid']}


def _cumulative_protection(book, marked_equities):
    """Observe existing protection capital and peak without resetting either."""
    protection = book.get('account_protection')
    if (not isinstance(protection, dict) or set(protection) != {'units', 'peak', 'active', 'limit_pct'}
            or type(protection['active']) is not bool):
        raise ValueError('invalid_terminal_protection')
    units, peak, limit = (_state_decimal(protection[key]) for key in ('units', 'peak', 'limit_pct'))
    if not units or not peak or not 0 < limit <= 100 or not marked_equities:
        raise ValueError('invalid_terminal_protection')
    drawdown = Decimal(0)
    for equity in marked_equities:
        unit = _state_decimal(equity) / units
        peak = max(peak, unit)
        drawdown = max(drawdown, (1 - unit / peak) * 100)
    return {'cumulative_protection_drawdown_pct': float(drawdown),
            'cumulative_protection_active': protection['active'] or drawdown >= limit,
            'cumulative_protection_limit_pct': float(limit)}


def _terminal_outcome(book, frame, limits, *, protection_fee=FEE, protection_tax=TAX):
    """Mark a validated book once; no time, fills, fees or lot state advance."""
    rows = _rows(frame)
    budget = _state_decimal(limits['budget'])
    if (book.get('as_of') != frame['as_of'] or not budget
            or 'budget' in book and _state_decimal(book['budget']) != budget
            or any(book.get(key, 0) != 0 for key in ('pending_orders', 'unresolved_orders'))):
        raise ValueError('invalid_terminal_book')
    cash, reserved, positions, _ = _initial_state(book, budget, frame['as_of'])
    if reserved:
        raise ValueError('unsettled_terminal_reservations')
    for symbol, position in positions.items():
        if symbol not in rows or position['last_close'] != _state_decimal(rows[symbol]['close']):
            raise ValueError('terminal_position_price_mismatch')
    fee, tax = _state_decimal(protection_fee), _state_decimal(protection_tax)
    if fee + tax >= 1:
        raise ValueError('invalid_terminal_protection_costs')
    holdings = sum((position['last_close'] * position['quantity'] for position in positions.values()), Decimal(0))
    protected_equity = cash + holdings * (1 - fee - tax)
    if 'equity' in book and abs(_state_decimal(book['equity']) - protected_equity) > Decimal('.000001'):
        raise ValueError('terminal_book_equity_mismatch')
    diagnostics = {key: 0 for key in ('buy_attempts', 'sell_attempts', 'partial_orders', 'unfilled_orders',
        'limit_blocked', 'untradeable_bars', 'ineligible_quotes', 'fees_paid', 'taxes_paid')}
    diagnostics.update(slippage_bps=0, final_holding_count=len(positions))
    return {'status': 'ready', 'reason': None, **dict.fromkeys(ZERO_ACTIVITY, 0),
        'final_equity': float(cash + holdings), 'max_drawdown_pct': 0., 'first_buy_session': None,
        'final_holding_count': len(positions), 'final_protection': deepcopy(book['account_protection']),
        'valuation_basis': 'cash_plus_close_marked_unsold_holdings', 'fill_basis': 'no_additional_intervals',
        'final_state_hash': _hash(book), 'execution_diagnostics': diagnostics,
        'future_activity': dict.fromkeys(ZERO_ACTIVITY, 0),
        **_cumulative_protection(book, [protected_equity])}


def award_scenarios(policy, frames, limits, context, *, cost_profile=None, plan=None):
    """Return fixed historical paths with incremental activity and common book.

    Normal/cost-stress and 50%-fill/one-session-delayed-sell paths are explicit
    assumptions, never samples of future probabilities. No current book is
    inserted into old calendar time, and no aggregate past return is applied to it.
    """
    result = _base()
    try:
        policy = validate_policy(deepcopy(policy))
        latest = context['latest_frame']
        preview = transition_preview(policy, latest, limits, context)
        if preview['status'] != 'ready':
            raise ValueError(preview['reasons'][0] if preview['reasons'] else 'current_transition_unavailable')
        latest, history, horizon, _ = _plan_inputs(frames, context)
        expected_plan = scenario_plan(frames, context)
        if plan is not None:
            if not isinstance(plan, dict) or plan.get('version') != PLAN_VERSION:
                raise ValueError('scenario_plan_version_mismatch')
            if plan.get('hash') != _hash({key: value for key, value in plan.items() if key != 'hash'}):
                raise ValueError('scenario_plan_hash_mismatch')
            if plan != expected_plan:
                raise ValueError('scenario_plan_source_mismatch')
        plan = expected_plan
        result['scenario_plan'] = deepcopy(plan)
        if not plan['schedules']:
            raise ValueError(plan['reason'])
        costs = _costs(cost_profile)
        book = deepcopy(context['initial_state'])
        result.update(horizon=horizon, calendar_basis='weekday_estimate',
                      start_book_hash=_hash(book), current_frame_hash=_hash(latest),
                      history_hash=_hash(history), cost_profile=costs,
                      scenario_plan_hash=plan['hash'],
                      execution_context_basis='fixed_analogue_assumptions')
        for schedule_record in plan['schedules']:
            schedule, length = schedule_record['schedule'], schedule_record['horizon']
            for source_record in schedule_record['selected']:
                window, start = source_record['window'], source_record['source_start_index']
                prefix = f'{window}_{schedule}'
                variants = [('normal', False, False), ('cost_stress', True, False),
                            ('partial_delayed', False, True), ('partial_delayed_cost_stress', True, True)]
                try:
                    if source_record['status'] != 'ready':
                        raise ValueError(source_record['reason'])
                    if not length:
                        terminal = _terminal_outcome(book, latest, limits)
                        for environment, _, _ in variants:
                            current = _unavailable(f'{prefix}_{environment}', window, 0, environment, None)
                            current.update(terminal, schedule=schedule, source_start=None, source_end=None,
                                assumed_start=None, assumed_end=latest['as_of'],
                                path_hash=_hash({'basis': TERMINAL_BASIS, 'frame': latest}),
                                source_start_index=None, source_intervals=0, basis=TERMINAL_BASIS,
                                alias_of=f'first_shortened_{environment}',
                                start_book_hash=result['start_book_hash'], scenario_plan_hash=plan['hash'])
                            result['scenarios'].append(deepcopy(current))
                        continue
                    source = history[start:start + length + 1]
                    path = _path(policy, source, latest, book)
                    metrics = {}
                    for environment, stress, partial in variants:
                        identity = f'{prefix}_{environment}'
                        if partial not in metrics:
                            model = {'mode': 'daily_open', **costs}
                            if partial:
                                model.update(fill_ratio=.5, sell_delay_sessions=1)
                            metrics[partial] = portfolio_metrics(policy, path, limits,
                                initial_state=deepcopy(book), execution_model=model)
                        metric = metrics[partial]
                        current = _unavailable(identity, window, length, environment, metric.get('error'))
                        current.update(schedule=schedule, source_start=source[0]['as_of'],
                                       source_end=source[-1]['as_of'], assumed_start=path[1]['as_of'],
                                       assumed_end=path[-1]['as_of'], path_hash=_hash(path),
                                       source_start_index=start, source_intervals=length,
                                       start_book_hash=result['start_book_hash'], scenario_plan_hash=plan['hash'])
                        if metric['valid']:
                            current.update(_outcome(metric, stress=stress))
                        result['scenarios'].append(current)
                except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError) as error:
                    reason = _reason(error)
                    for environment, _, _ in variants:
                        unavailable = _unavailable(f'{prefix}_{environment}', window, length, environment, reason)
                        unavailable.update(schedule=schedule, scenario_plan_hash=plan['hash'],
                                           source_start_index=start)
                        if source_record['status'] == 'ready':
                            unavailable.update(source_start=source_record['source_start'],
                                               source_end=source_record['source_end'])
                        result['scenarios'].append(unavailable)
        ready = sum(item['status'] == 'ready' for item in result['scenarios'])
        result.update(status='ready' if ready == len(result['scenarios']) else 'unavailable',
                      ready_scenarios=ready, scenario_count=len(result['scenarios']),
                      reason=None if ready == len(result['scenarios']) else 'incomplete_scenario_family')
        return result
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError) as error:
        reason = str(error)
        result['reason'] = reason if re.fullmatch(r'[a-z][a-z0-9_]*', reason) else 'invalid_analogue_context'
        return result
